"""交互会话（REPL）的测试：多轮上下文、命令、退出。不联网。"""

from __future__ import annotations

from src.repl import Session, format_result, run


def test_session_keeps_history_across_turns() -> None:
    session = Session(model_kind="mock", corpus="full")
    out: list[str] = []
    run(session, iter(["年假可以攒到明年吗？", "那试用期员工呢？", "/quit"]), out.append)
    text = "\n".join(out)
    assert "状态 answered" in text
    assert "D04#c3" in text  # 追问接住了上文主题，命中试用期条款
    assert len(session.history) == 2
    assert session.history[0]["cited_doc_ids"] == ["D03"]


def test_corpus_switch_changes_decision() -> None:
    session = Session(model_kind="mock", corpus="full")
    result_full = session.ask("报销期限多久？")
    assert result_full["status"] == "answered"  # 语料里有总部专用版 -> 可解

    keep, message = session.command("/corpus conflict_min")
    assert keep and "conflict_min" in message
    session.history.clear()
    result_min = session.ask("报销期限多久？")
    assert result_min["status"] == "conflict_pending"  # 没有范围信息 -> 必须追问


def test_model_command_and_reset() -> None:
    session = Session(model_kind="mock")
    keep, message = session.command("/model")
    assert keep and "mock" in message
    keep, message = session.command("/model real")
    assert keep and "real" in message
    # 切回 mock 再提问：测试永远不联网
    keep, message = session.command("/model mock")
    assert keep and "mock" in message
    session.ask("年假可以攒到明年吗？")
    assert session.history
    session.command("/reset")
    assert session.history == []


def test_quit_stops_the_loop() -> None:
    session = Session(model_kind="mock")
    out: list[str] = []
    run(session, iter(["/quit"]), out.append)
    assert "会话结束" in out[-1]


def test_empty_lines_are_ignored() -> None:
    session = Session(model_kind="mock")
    out: list[str] = []
    run(session, iter(["", "   ", "/quit"]), out.append)
    assert len(out) == 2  # banner + 再见


def test_format_result_shows_citations_and_trace() -> None:
    session = Session(model_kind="mock", corpus="conflict_min")
    result = session.ask("报销期限多久？")
    result["trace_steps"] = [{"step": 1, "action": "search_docs", "args": {"query": "报销期限"}, "rationale": "先检索", "observation_summary": "命中 2 段"}]
    text = format_result(result, show_trace=True)
    assert "conflict_pending" in text
    assert "D01#c2" in text and "D02#c2" in text
    assert "决策过程" in text and "先检索" in text
