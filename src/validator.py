"""引用校验器：让「模型说的每句话」都能落回原文。

这是本项目防幻觉的核心，也是「观察 -> 调整」循环里最容易看见的一环：
校验失败的结果会作为 observation 回灌给模型，模型必须改引用、改答案或转成追问。

三级校验：
1. quote 存在性   —— quote 必须是 chunk.text 的逐字片段（归一化后子串）
2. 数值 grounding —— 回答里的每个数字/期限/金额都必须能在某条 cite 的 quote 里找到出处
3. 未处理冲突     —— 模型同时引用了两个不同来源的互斥数值，却只回答其中一个 -> 打回

边界（写进 docs/03-evaluation.md）：只做「字面可验证」，不做语义等价判断。
模型若只引用冲突来源中的一份，代码层无法识别——靠检索线索 + prompt + 评测集兜底。
"""

from __future__ import annotations

from typing import Any

from .normalize import (
    canon_unit,
    extract_facts,
    fact_covered,
    norm_text,
    time_facts,
    unit_equivalents,
)
from .tools import DocStore, ToolError

ERROR_CODES = (
    "empty_answer",
    "missing_citations",
    "bad_citation_shape",
    "unknown_citation",
    "citation_not_found",
    "missing_number_grounding",
    "unhandled_conflict",
)


def _err(code: str, message: str, **detail: Any) -> dict:
    return {"code": code, "message": message, **detail}


def _dedupe(errors: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for error in errors:
        key = (error["code"], error.get("value"), error.get("unit"), error.get("chunk_id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(error)
    return unique


def verify_citations(citations: Any, store: DocStore) -> tuple[list[tuple[Any, str]], list[dict]]:
    """只校验引用本身是否真实存在（ask_user 场景也用它）。"""
    errors: list[dict] = []
    verified: list[tuple[Any, str]] = []
    if not isinstance(citations, list) or not citations:
        return [], [_err("missing_citations", "没有提供任何引用")]
    for index, cite in enumerate(citations):
        if not isinstance(cite, dict):
            errors.append(_err("bad_citation_shape", f"第 {index} 条引用不是对象"))
            continue
        doc_id = cite.get("doc_id")
        chunk_id = cite.get("chunk_id")
        quote = cite.get("quote")
        if not all(isinstance(x, str) and x.strip() for x in (doc_id, chunk_id, quote)):
            errors.append(
                _err("bad_citation_shape", f"第 {index} 条引用必须含非空的 doc_id/chunk_id/quote")
            )
            continue
        try:
            chunk = store.get_chunk(doc_id.strip(), chunk_id.strip())
        except ToolError as exc:
            errors.append(
                _err("unknown_citation", f"引用不存在：{exc.message}", doc_id=doc_id, chunk_id=chunk_id)
            )
            continue
        normalized_quote = norm_text(quote)
        if not normalized_quote or normalized_quote not in norm_text(chunk.text):
            errors.append(
                _err(
                    "citation_not_found",
                    f"引用的原文片段不在 {chunk.chunk_id} 中，疑似凭记忆改写",
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    quote=quote,
                )
            )
            continue
        verified.append((chunk, quote))
    return verified, errors


def check_numbers(answer_text: str, verified: list[tuple[Any, str]]) -> list[dict]:
    """回答中每个数字/期限/金额都必须出现在某条已核实引用里。"""
    errors: list[dict] = []
    quotes = [norm_text(quote) for _, quote in verified]
    for fact in extract_facts(answer_text):
        if any(fact_covered(fact, quote) for quote in quotes):
            continue
        errors.append(
            _err(
                "missing_number_grounding",
                f"回答里的 {fact['raw']} 没有出现在任何引用原文中",
                value=fact["value"],
                unit=fact["unit"],
            )
        )
    return errors


def check_conflict_handling(answer_text: str, verified: list[tuple[Any, str]]) -> list[dict]:
    """引用了互斥来源却只答一个值 -> 打回，要求并列或转 ask_user。"""
    errors: list[dict] = []
    per_doc: dict[str, set[tuple[str, str]]] = {}
    for chunk, _ in verified:
        for fact in time_facts(chunk.text):
            per_doc.setdefault(chunk.doc_id, set()).add((canon_unit(fact["unit"]), fact["value"]))

    answer_normalized = norm_text(answer_text)
    units = {unit for facts in per_doc.values() for unit, _ in facts}
    for unit in units:
        values = {value for facts in per_doc.values() for u, value in facts if u == unit}
        docs = {doc for doc, facts in per_doc.items() if any(u == unit for u, _ in facts)}
        if len(values) < 2 or len(docs) < 2:
            continue
        missing = [
            value
            for value in sorted(values)
            if not any(f"{value}{alias}" in answer_normalized for alias in unit_equivalents(unit))
        ]
        if missing:
            errors.append(
                _err(
                    "unhandled_conflict",
                    f"引用了 {len(docs)} 份来源，{unit}出现互斥取值 {sorted(values)}，"
                    f"但回答没有提到 {missing}；必须并列说明冲突或转为请求确认",
                    unit=unit,
                    values=sorted(values),
                    missing=missing,
                )
            )
    return errors


def verify_answer(
    answer_text: str,
    citations: Any,
    store: DocStore,
    require_conflict_disclosure: bool = True,
) -> dict:
    """`answer` 动作的终审。ok=True 才允许以 answered 状态结束。"""
    errors: list[dict] = []
    if not isinstance(answer_text, str) or not answer_text.strip():
        errors.append(_err("empty_answer", "回答正文为空"))
    verified, cite_errors = verify_citations(citations, store)
    errors.extend(cite_errors)
    if not errors:
        errors.extend(check_numbers(answer_text, verified))
        if require_conflict_disclosure:
            errors.extend(check_conflict_handling(answer_text, verified))
    errors = _dedupe(errors)
    return {
        "ok": not errors,
        "errors": errors,
        "verified": [{"doc_id": c.doc_id, "chunk_id": c.chunk_id, "quote": q} for c, q in verified],
        "stats": {
            "citations_checked": len(citations) if isinstance(citations, list) else 0,
            "citations_ok": len(verified),
            "numbers_checked": len(extract_facts(answer_text or "")),
        },
    }


def format_errors(result: dict) -> str:
    """把校验失败转成给模型看的一句话（回灌 observation 用）。"""
    return "；".join(f"[{e['code']}] {e['message']}" for e in result.get("errors", []))
