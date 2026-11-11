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


# ---------------------------------------------------------------- 弱模型退化：挑了自洽的一份就答

HINT = {
    "unit": "天",
    "values": ["15", "30"],
    "candidates": [
        {"doc_id": "D01", "chunk_id": "D01#c2", "value": "30"},
        {"doc_id": "D02", "chunk_id": "D02#c2", "value": "15"},
    ],
}


def test_cherry_picked_conflict_is_rejected(store: DocStore) -> None:
    """只引自洽的一份（两份都写 15 天）就下结论 —— 检索线索说明还有 30 天。"""
    result = verify_answer(
        "两份细则一致，报销期限为 15 天。",
        [
            {"doc_id": "D02", "chunk_id": "D02#c2", "quote": D02_QUOTE},
            {"doc_id": "D02b", "chunk_id": "D02b#c2", "quote": "费用发生后 15 天内提交报销单，逾期提交需经财务经理批准。"},
        ],
        store,
        hint=HINT,
        question="报销期限多久？",
    )
    assert result["ok"] is False
    codes = {error["code"] for error in result["errors"]}
    assert "cherry_picked_conflict" in codes
    assert any(error.get("missing") == ["30"] for error in result["errors"])


def test_scope_limited_answer_is_exempt(store: DocStore) -> None:
    # 豁免看的是"问题给没给范围"：用户说自己是总部员工，回答只讲 15 天是合法的
    result = verify_answer(
        "总部员工按 15 天提交报销单。",
        [{"doc_id": "D02b", "chunk_id": "D02b#c2", "quote": "费用发生后 15 天内提交报销单，逾期提交需经财务经理批准。"}],
        store,
        hint=HINT,
        question="我是总部员工，报销期限多久？",
    )
    assert result["ok"] is True, result["errors"]


def test_scope_words_in_the_answer_do_not_exempt_it(store: DocStore) -> None:
    """实测弱模型会写「15 天，注意总部专用版仅适用总部」——答案里提范围词不能当豁免。"""
    result = verify_answer(
        "费用发生后 15 天内提交报销单。需注意：总部专用版仅适用于总部员工。",
        [
            {"doc_id": "D02", "chunk_id": "D02#c2", "quote": D02_QUOTE},
            {"doc_id": "D02b", "chunk_id": "D02b#c1", "quote": "本细则仅适用于总部（北京、上海办公区）员工；分支机构员工请参照所在区域的财务规定执行。"},
        ],
        store,
        hint=HINT,
        question="报销期限多久？",
    )
    assert result["ok"] is False
    assert any(error["code"] == "cherry_picked_conflict" for error in result["errors"])


def test_answer_listing_both_values_passes(store: DocStore) -> None:
    # 注意：回答里不能写 doc_id —— 编号里的数字会被当成"没有出处的数字"
    result = verify_answer(
        "差旅制度要求 30 天，费用细则要求 15 天，两者不一致。",
        [
            {"doc_id": "D01", "chunk_id": "D01#c2", "quote": D01_QUOTE},
            {"doc_id": "D02", "chunk_id": "D02#c2", "quote": D02_QUOTE},
        ],
        store,
        hint=HINT,
        question="报销期限多久？",
    )
    assert result["ok"] is True, result["errors"]


def test_hint_of_other_unit_does_not_block_money_answer(store: DocStore) -> None:
    """问的是金额、线索是期限：不该误伤。"""
    result = verify_answer(
        "出差期间市内交通费单日不超过 200 元。",
        [{"doc_id": "D02", "chunk_id": "D02#c4", "quote": "出差期间的市内交通费按实际发生额报销，单日合计不超过 200 元。"}],
        store,
        hint=HINT,
        question="出差打车一天最多能报多少？",
    )
    assert result["ok"] is True, result["errors"]
