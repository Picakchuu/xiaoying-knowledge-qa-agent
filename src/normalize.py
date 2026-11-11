"""文本归一化与特征抽取。

【为什么要单独一层】
检索打分与引用校验必须共用同一套归一化，否则会出现「检索命中了，quote 校验却说找不到原文」
的自相矛盾。所有规则集中在这里，改一处全链路生效。

设计取舍：
- 只做字符级归一化（全半角、空白、标点、中文数字），**不做语义改写**。语义一致性交给模型，
  代码只负责「这段原文到底存不存在」和「这个数字有没有出处」。
- 归一化故意偏宽松（去标点、中文数字转阿拉伯），宁可放过同义表述，也不误杀正确引用。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# ---------------------------------------------------------------- 基础常量

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 单位按长度倒序，保证「个工作日」优先于「日」被匹配到
UNITS: tuple[str, ...] = (
    "个工作日",
    "工作日",
    "万行",
    "万元",
    "个月",
    "天",
    "日",
    "月",
    "年",
    "元",
    "%",
    "次",
    "人",
    "位",
    "小时",
    "分钟",
    "周",
)

# 单位别名 -> 规范单位（用于跨写法比对）
UNIT_CANON: dict[str, str] = {
    "日": "天",
    "个工作日": "工作日",
}

# 时间类单位：冲突线索只在这一族里比对，避免「5000 元 vs 200 元」这种不同事项误报
TIME_UNITS: frozenset[str] = frozenset({"天", "工作日", "个月", "月", "年", "周", "小时", "分钟"})

_PUNCT_RE = re.compile(r"[\s,，。、；;：:！!？?（）()【】\[\]「」『』“”\"'‘’·—…《》<>]")
_UNIT_ALT = "|".join(sorted(UNITS, key=len, reverse=True))

_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
_FACT_RE = re.compile(r"(\d+(?:\.\d+)?)(" + _UNIT_ALT + r")")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

_CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUM_RE = re.compile(
    r"([零〇一二两三四五六七八九十百千]+)(?=(" + _UNIT_ALT + r"))"
)


def _cn_to_int(text: str) -> int:
    """中文数字 -> 整数，支持到千位（三十 -> 30，一百二十 -> 120）。"""
    total = 0
    section = 0
    current = 0
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch == "十":
            section += (current or 1) * 10
            current = 0
        elif ch == "百":
            section += (current or 1) * 100
            current = 0
        elif ch == "千":
            section += (current or 1) * 1000
            current = 0
        else:  # pragma: no cover - 正则由 _CN_NUM_RE 保证不会走到这里
            return -1
    return section + current


def _cn_sub(match: re.Match[str]) -> str:
    value = _cn_to_int(match.group(1))
    # 0 或异常大值不转换，避免把「万一」「十字」之类的词搞坏
    if 0 < value < 100000:
        return str(value)
    return match.group(0)


# ---------------------------------------------------------------- 归一化


def norm_text(text: str) -> str:
    """统一写法：NFKC、小写、去空白与标点、中文数字（后接单位时）转阿拉伯数字。"""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _PUNCT_RE.sub("", normalized)
    normalized = _CN_NUM_RE.sub(_cn_sub, normalized)
    return normalized


def contains_quote(chunk_text: str, quote: str) -> bool:
    """quote 是否是 chunk 的原文片段（归一化后子串判定）。"""
    q = norm_text(quote)
    if not q:
        return False
    return q in norm_text(chunk_text)


# ---------------------------------------------------------------- 特征


def bigrams(text: str) -> set[str]:
    """中文取字符 2-gram，英文/数字取整词。用于检索打分与相似度判断。"""
    normalized = norm_text(text)
    grams: set[str] = set()
    for i in range(len(normalized) - 1):
        left, right = normalized[i], normalized[i + 1]
        if _CJK_RE.match(left) and _CJK_RE.match(right):
            grams.add(left + right)
    grams.update(re.findall(r"[a-z0-9]+", normalized))
    return grams


def canon_unit(unit: str) -> str:
    return UNIT_CANON.get(unit, unit)


def unit_equivalents(unit: str) -> set[str]:
    """同一单位的等价写法（天/日、工作日/个工作日、月/个月）。"""
    unit = canon_unit(unit)
    equivalents = {unit}
    for alias, canonical in UNIT_CANON.items():
        if canonical == unit:
            equivalents.add(alias)
    return equivalents


def extract_facts(text: str) -> list[dict]:
    """抽取「数值 + 单位」事实。

    返回项形如 {"value": "30", "unit": "天", "raw": "30天", "kind": "quantity"}；
    日期按 {"kind": "date", "value": "2026-02-01"} 处理；无单位裸数字 kind="number"。
    """
    normalized = norm_text(text)
    facts: list[dict] = []
    consumed: set[int] = set()

    for match in _DATE_RE.finditer(normalized):
        facts.append({"kind": "date", "value": match.group(0), "unit": "date", "raw": match.group(0)})
        consumed.update(range(match.start(), match.end()))

    for match in _FACT_RE.finditer(normalized):
        if match.start() in consumed:
            continue
        raw_unit = match.group(2)
        facts.append(
            {
                "kind": "quantity",
                "value": match.group(1),
                "unit": canon_unit(raw_unit),
                "unit_raw": raw_unit,
                "raw": match.group(0),
            }
        )
        consumed.update(range(match.start(), match.end()))

    for match in _NUMBER_RE.finditer(normalized):
        if match.start() in consumed:
            continue
        consumed.update(range(match.start(), match.end()))
        facts.append({"kind": "number", "value": match.group(0), "unit": "", "raw": match.group(0)})

    return facts


def time_facts(text: str) -> list[dict]:
    """只取时间类数值事实（冲突线索用）。"""
    return [f for f in extract_facts(text) if f["kind"] == "quantity" and f["unit"] in TIME_UNITS]


def fact_covered(fact: dict, quote_normalized: str) -> bool:
    """这条数值事实能否在这段（已归一化的）原文里找到出处。"""
    if fact["kind"] == "date":
        return fact["value"] in quote_normalized
    if fact["kind"] == "number":
        return fact["value"] in quote_normalized
    for unit in unit_equivalents(fact["unit"]):
        if f"{fact['value']}{unit}" in quote_normalized:
            return True
    return False


def shared_bigram_count(left: str, right: str) -> int:
    return len(bigrams(left) & bigrams(right))


def tokenize_query(text: str) -> set[str]:
    """把查询切成检索用的 2-gram，并过滤高频虚词带来的噪声。"""
    grams = bigrams(text)
    noise = {"的了", "了呢", "了吗", "吗我", "请问", "一下", "什么", "怎么", "如何", "多少", "可以"}
    return {g for g in grams if g not in noise}


def ensure_iterable(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        return [value]
    return list(value)
