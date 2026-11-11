"""归一化与特征抽取的单元测试。"""

from __future__ import annotations

import pytest

from src.normalize import (
    bigrams,
    canon_unit,
    contains_quote,
    extract_facts,
    fact_covered,
    norm_text,
    shared_bigram_count,
    time_facts,
    unit_equivalents,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("费用发生后 30 天内提交", "费用发生后30天内提交"),
        ("报销期限多久？", "报销期限多久"),
        ("ＡＢＣ　１２３", "abc123"),
        ("「电子发票」（需查验）", "电子发票需查验"),
    ],
)
def test_norm_text_unifies_width_space_and_punct(raw: str, expected: str) -> None:
    assert norm_text(raw) == expected


def test_norm_text_converts_chinese_numerals_before_units() -> None:
    assert norm_text("三十天内提交") == "30天内提交"
    assert norm_text("十五个工作日") == "15个工作日"
    # 不跟单位的词不转换，避免把「不管」之类的词搞坏
    assert norm_text("不管怎样") == "不管怎样"


def test_contains_quote_accepts_numeral_variants() -> None:
    chunk = "员工应在费用发生后 30 天内提交报销申请。"
    assert contains_quote(chunk, "30 天内提交报销申请")
    assert contains_quote(chunk, "三十天内提交报销申请")
    assert not contains_quote(chunk, "员工应在费用发生后 45 天内提交报销申请")


def test_bigrams_are_cjk_pairs_plus_ascii_words() -> None:
    assert bigrams("报销期限") == {"报销", "销期", "期限"}
    assert bigrams("PDF 导出") == {"pdf", "导出"}


def test_extract_facts_quantity_unit_and_date() -> None:
    facts = extract_facts("费用发生后 30 天内提交，自 2026-02-01 起施行，单日不超过 200 元。")
    kinds = {(f["kind"], f["value"], f["unit"]) for f in facts}
    assert ("quantity", "30", "天") in kinds
    assert ("date", "2026-02-01", "date") in kinds
    assert ("quantity", "200", "元") in kinds


def test_time_facts_excludes_money() -> None:
    facts = time_facts("单日不超过 200 元，30 天内提交，6 个月内使用")
    assert {f["value"] for f in facts} == {"30", "6"}
    assert all(f["unit"] != "元" for f in facts)


def test_fact_covered_handles_unit_aliases() -> None:
    quote = norm_text("员工应在费用发生后 30 天内提交")
    fact = extract_facts("30 日")[0]
    assert fact_covered(fact, quote)
    assert not fact_covered(extract_facts("30 个工作日")[0], quote)


def test_unit_helpers() -> None:
    assert canon_unit("日") == "天"
    assert "日" in unit_equivalents("天")
    assert "个工作日" in unit_equivalents("工作日")


def test_shared_bigram_count_detects_same_topic() -> None:
    left = "费用发生后 30 天内提交报销申请"
    right = "费用发生后 15 天内提交报销单"
    assert shared_bigram_count(left, right) >= 2
    assert shared_bigram_count(left, "机密数据禁止通过个人邮箱外发") < 2
