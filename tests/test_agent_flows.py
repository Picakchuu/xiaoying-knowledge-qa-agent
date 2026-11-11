"""端到端：mock 决策器跑通全部验收路径 + trace 协议校验。

mock 的意义是验证**流程与控制**（动作选择、工具结果驱动、校验打回、上限收尾），
它不证明真实模型的判断质量——那条路径见 docs/03-evaluation.md 的真模型记录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent import Agent
from src.llm import MockModel
from src.tools import DocStore, Tools
from src.trace import StepTrace

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "docs"
REGRESSION = ROOT / "data" / "regression"


def run_agent(
    question: str,
    corpus: str = "full",
    faults: list[str] | None = None,
    history: list[dict] | None = None,
    docs_dirs: list[str] | None = None,
    max_steps: int = 12,
    max_tool_calls: int = 8,
    trace_path: Path | None = None,
) -> dict:
    store = DocStore(docs_dirs=docs_dirs or [DOCS], corpus=corpus)
    tools = Tools(store=store, faults=faults)
    trace = StepTrace(trace_path) if trace_path else None
    agent = Agent(
        model=MockModel(),
        tools=tools,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        trace=trace,
        history=history or [],
    )
    return agent.run(question)


# ---------------------------------------------------------------- 验收路径


def test_a1_answered_with_verbatim_quote() -> None:
    result = run_agent("年假可以攒到明年吗？")
    assert result["status"] == "answered"
    assert [c["chunk_id"] for c in result["citations"]] == ["D03#c2"]
    assert "不得结转" in result["citations"][0]["quote"]
    assert result["citations"][0]["quote"] in (ROOT / "data/docs/D03.json").read_text(encoding="utf-8")


def test_a2_conflict_requires_user_confirmation() -> None:
    result = run_agent("报销期限多久？", corpus="conflict_min")
    assert result["status"] == "conflict_pending"
    doc_ids = {c["doc_id"] for c in result["citations"]}
    assert doc_ids == {"D01", "D02"}
    answer = result["answer"]
    assert "30" in answer and "15" in answer
    assert result["gaps"] == ["适用制度范围"]


def test_a2b_conflict_resolved_with_scope() -> None:
    result = run_agent("报销期限多久？", corpus="full")
    assert result["status"] == "answered"
    cited = {c["doc_id"] for c in result["citations"]}
    assert {"D01", "D02b"}.issubset(cited)
    assert "总部" in result["answer"] and "15" in result["answer"] and "30" in result["answer"]


def test_a3_out_of_scope_is_insufficient() -> None:
    result = run_agent("公司明年会裁员吗？", corpus="no_claim")
    assert result["status"] == "insufficient"
    assert result["citations"] == []
    assert result["gaps"]


def test_a3b_missing_detail_is_insufficient() -> None:
    result = run_agent("专业版一年多少钱？")
    assert result["status"] == "insufficient"
    assert any("价格" in gap for gap in result["gaps"])


@pytest.mark.parametrize("question", ["年假可以攒到明年吗？", "年假没用完能留到明年吗？"])
def test_a4_paraphrase_hits_same_source(question: str) -> None:
    result = run_agent(question)
    assert result["status"] == "answered"
    assert result["citations"][0]["chunk_id"] == "D03#c2"


def test_a4c_query_rewrite_hits_synonym() -> None:
    result = run_agent("出差打车一天最多能报多少？")
    assert result["status"] == "answered"
    assert result["citations"][0]["chunk_id"] == "D02#c4"
    assert "200" in result["answer"]


def test_a5_followup_keeps_context() -> None:
    history = [
        {
            "question": "年假可以攒到明年吗？",
            "answer": "不得结转至次年",
            "status": "answered",
            "cited_doc_ids": ["D03"],
        }
    ]
    result = run_agent("那试用期员工呢？", history=history)
    assert result["status"] == "answered"
    assert result["citations"][0]["chunk_id"] == "D04#c3"
    assert "不享受年假" in result["answer"]


def test_a6_transient_failure_recovers() -> None:
    result = run_agent("报销期限多久？", corpus="conflict_min", faults=["search_docs:timeout:2"])
    assert result["status"] == "conflict_pending"
    assert result["tool_calls"] >= 3


def test_a6b_persistent_failure_is_failed() -> None:
    result = run_agent("报销期限多久？", corpus="conflict_min", faults=["search_docs:timeout"])
    assert result["status"] == "failed"
    assert result["citations"] == []


def test_a6b_circuit_breaker_stops_after_three_failures() -> None:
    """真模型会一直换 query 重试到步数上限；连续 3 次失败后必须由代码收口。"""

    class AlwaysSearch:
        name = "always-search"

        def decide(self, ctx):  # noqa: ANN001 - 测试桩
            return {"action": "search_docs", "args": {"query": "报销期限"}, "why": "再试一次"}

    agent = Agent(
        model=AlwaysSearch(),
        tools=Tools(DocStore(corpus="conflict_min"), faults=["search_docs:timeout"]),
        max_steps=12,
        max_tool_calls=8,
    )
    result = agent.run("报销期限多久？")
    assert result["status"] == "failed"
    assert result["tool_calls"] == 3
    assert "连续失败" in result["answer"]
    assert result["citations"] == []


def test_gaps_backfilled_when_answer_admits_missing() -> None:
    """模型说「文档未提供 X」却没填 missing 时，代码兜底补缺口（真模型实测踩到）。"""
    agent = Agent(model=MockModel(), tools=Tools(DocStore(corpus="full")))
    assert agent._infer_gaps("专业版一年多少钱？", "文档未提供具体价格，请联系销售。", None) == ["具体价格/报价单"]
    assert agent._infer_gaps("年假能攒吗？", "当年未休完的年假不得结转至次年。", None) == []
    assert agent._infer_gaps("随便问", "文档未提供相关信息", ["自定义缺口"]) == ["自定义缺口"]


def test_a7_deprecated_document_not_used_as_current() -> None:
    result = run_agent("报销需要交纸质发票吗？")
    assert result["status"] == "answered"
    assert all(c["doc_id"] != "D08" for c in result["citations"])
    assert "已废止" in result["answer"]


# ---------------------------------------------------------------- 上限


def test_b1_tool_call_limit_returns_incomplete() -> None:
    result = run_agent("年假可以攒到明年吗？", max_steps=3, max_tool_calls=1)
    assert result["status"] == "incomplete"
    assert "tool_call_limit" in result["reason"] or "step_limit" in result["reason"]
    assert result["gaps"]


def test_b1_step_limit_returns_incomplete() -> None:
    result = run_agent("报销期限多久？", corpus="conflict_min", max_steps=2, max_tool_calls=8)
    assert result["status"] == "incomplete"
    assert "step_limit" in result["reason"]


# ---------------------------------------------------------------- 内容替换（C1/C2）


def test_c1_content_swap_changes_conflict_values() -> None:
    baseline = run_agent("报销期限多久？", corpus="conflict_min")
    swapped = run_agent(
        "报销期限多久？",
        corpus="conflict_min",
        docs_dirs=[str(DOCS), str(REGRESSION / "d01_45days")],
    )
    assert "30" in baseline["answer"]
    assert "45" in swapped["answer"]
    assert "30" not in swapped["answer"].replace("30 天执行", "X")  # 旧值不该再出现
    assert swapped["status"] == "conflict_pending"


def test_c2_content_swap_changes_answer() -> None:
    baseline = run_agent("年假可以攒到明年吗？")
    swapped = run_agent(
        "年假可以攒到明年吗？",
        docs_dirs=[str(DOCS), str(REGRESSION / "d03_carryover_allowed")],
    )
    assert "不得结转" in baseline["answer"]
    assert "可结转" in swapped["answer"]
    assert swapped["citations"][0]["chunk_id"] == "D03#c2"


# ---------------------------------------------------------------- trace 协议

# 不用 pytest 的 tmp_path：本机沙箱的临时目录不可写（WinError 5），
# 统一写到工作区内的 .tmp/（已进 .gitignore）。
TRACE_DIR = ROOT / ".tmp" / "tests"


def _trace_file(name: str) -> Path:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    return TRACE_DIR / name


def test_trace_records_required_fields() -> None:
    trace_file = _trace_file("trace_required_fields.jsonl")
    result = run_agent("报销期限多久？", corpus="conflict_min", trace_path=trace_file)
    assert result["trace_path"] == str(trace_file)

    records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert records[0]["type"] == "run_start"
    assert records[-1]["type"] == "run_end"
    assert records[-1]["status"] == result["status"]

    steps = [record for record in records if record["type"] == "step"]
    assert len(steps) == result["steps"]
    for record in steps:
        assert {"step", "action", "args", "rationale", "observation_summary", "next_action"} <= set(record)
        assert record["rationale"] and len(record["rationale"]) < 120  # 只记一句话，不记思维链
    assert steps[0]["next_action"] == "read_chunk"  # 第一步检索后确实去读了原文
    assert any(record["type"] == "decision" for record in records)
    assert not any("思维" in json.dumps(record, ensure_ascii=False) for record in steps)


def test_trace_does_not_leak_reasoning_blob() -> None:
    trace_file = _trace_file("trace_size.jsonl")
    run_agent("年假可以攒到明年吗？", trace_path=trace_file)
    payload = trace_file.read_text(encoding="utf-8")
    assert payload.count("\n") < 40  # 单用例 trace 不该膨胀
