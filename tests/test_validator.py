"""引用校验器单元测试：三类错误码 + 归一化边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools import DocStore
from src.validator import (
    check_conflict_handling,
    check_numbers,
    format_errors,
    verify_answer,
    verify_citations,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store() -> DocStore:
    return DocStore(corpus="full")


D01_QUOTE = "员工应在费用发生后 30 天内提交报销申请，逾期不予受理。提交入口为财务共享中心系统。"
D02_QUOTE = "费用发生后 15 天内须在系统中提交报销单；超期提交需附情况说明并经财务经理批准。"
D03_QUOTE = "当年未休完的年假不得结转至次年；未休年假由公司按日工资的 200% 支付补偿。"


def test_valid_answer_passes(store: DocStore) -> None:
    result = verify_answer(
        "当年未休完的年假不得结转至次年，未休部分按日工资的 200% 支付补偿。",
        [{"doc_id": "D03", "chunk_id": "D03#c2", "quote": D03_QUOTE}],
        store,
    )
    assert result["ok"] is True, result["errors"]
    assert result["stats"]["citations_ok"] == 1


def test_quote_must_be_verbatim(store: DocStore) -> None:
    result = verify_answer(
        "报销期限 15 天。",
        [{"doc_id": "D02", "chunk_id": "D02#c2", "quote": "差旅报销 15 天内提交"}],
        store,
    )
    assert result["ok"] is False
    assert {e["code"] for e in result["errors"]} == {"citation_not_found"}


def test_number_must_be_grounded(store: DocStore) -> None:
    """引 D02 却说 30 天：这是最典型的错误引用。"""
    result = verify_answer(
        "报销期限是 30 天。",
        [{"doc_id": "D02", "chunk_id": "D02#c2", "quote": D02_QUOTE}],
        store,
    )
    assert result["ok"] is False
    codes = {e["code"] for e in result["errors"]}
    assert "missing_number_grounding" in codes
    assert any(e.get("value") == "30" for e in result["errors"])


def test_unhandled_conflict_is_rejected(store: DocStore) -> None:
    """同时引用两份互斥来源，却只回答其中一个值 -> 打回。"""
    result = verify_answer(
        "报销期限是 15 天。",
        [
            {"doc_id": "D01", "chunk_id": "D01#c2", "quote": D01_QUOTE},
            {"doc_id": "D02", "chunk_id": "D02#c2", "quote": D02_QUOTE},
        ],
        store,
    )
    assert result["ok"] is False
    conflict = [e for e in result["errors"] if e["code"] == "unhandled_conflict"]
    assert conflict and conflict[0]["missing"] == ["30"]


def test_conflict_disclosed_passes(store: DocStore) -> None:
    result = verify_answer(
        "总部员工按 15 天提交；其他区域员工按 30 天提交。两份文件未说明相互适用范围。",
        [
            {"doc_id": "D01", "chunk_id": "D01#c2", "quote": D01_QUOTE},
            {"doc_id": "D02", "chunk_id": "D02#c2", "quote": D02_QUOTE},
        ],
        store,
    )
    assert result["ok"] is True, result["errors"]


def test_unknown_citation_reported(store: DocStore) -> None:
    result = verify_answer(
        "报销期限是 15 天。",
        [{"doc_id": "D99", "chunk_id": "D99#c1", "quote": "随便一句话"}],
        store,
    )
    assert result["ok"] is False
    assert {e["code"] for e in result["errors"]} == {"unknown_citation"}


@pytest.mark.parametrize(
    "citation",
    [
        {"doc_id": "D01", "chunk_id": "D01#c2"},  # 缺 quote
        {"doc_id": "", "chunk_id": "D01#c2", "quote": "x"},
        {"doc_id": "D01", "chunk_id": "D01#c2", "quote": "   "},
    ],
)
def test_bad_citation_shape(store: DocStore, citation: dict) -> None:
    result = verify_answer("任何回答", [citation], store)
    assert result["ok"] is False
    assert any(e["code"] == "bad_citation_shape" for e in result["errors"])


def test_empty_answer_and_missing_citations(store: DocStore) -> None:
    result = verify_answer("", [], store)
    codes = {e["code"] for e in result["errors"]}
    assert {"empty_answer", "missing_citations"} <= codes


def test_chinese_numeral_quote_matches_arabic_source(store: DocStore) -> None:
    """模型把「30 天」写成「三十天」，引用仍然成立。"""
    result = verify_answer(
        "三十天内提交报销申请。",
        [{"doc_id": "D01", "chunk_id": "D01#c2", "quote": "员工应在费用发生后三十天内提交报销申请"}],
        store,
    )
    assert result["ok"] is True, result["errors"]


def test_verify_citations_for_ask_user_flow(store: DocStore) -> None:
    verified, errors = verify_citations(
        [
            {"doc_id": "D01", "chunk_id": "D01#c2", "quote": D01_QUOTE},
            {"doc_id": "D02", "chunk_id": "D02#c2", "quote": "编造的原文"},
        ],
        store,
    )
    assert len(verified) == 1
    assert [e["code"] for e in errors] == ["citation_not_found"]


def test_check_numbers_and_conflict_are_independent(store: DocStore) -> None:
    verified = [
        (store.get_chunk("D03", "D03#c2"), D03_QUOTE),
    ]
    assert check_numbers("补偿标准是 200%，", verified) == []
    assert check_numbers("补偿标准是 300%。", verified)[0]["code"] == "missing_number_grounding"
    assert check_conflict_handling("不得结转至次年。", verified) == []


def test_format_errors_is_model_readable(store: DocStore) -> None:
    result = verify_answer(
        "报销期限 30 天。",
        [{"doc_id": "D02", "chunk_id": "D02#c2", "quote": D02_QUOTE}],
        store,
    )
    text = format_errors(result)
    assert "[missing_number_grounding]" in text
