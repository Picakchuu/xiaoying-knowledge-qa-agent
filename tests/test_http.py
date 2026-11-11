"""HTTP 薄封装的冒烟测试（不监听端口，直接测处理函数与页面）。"""

from __future__ import annotations

from src.http_app import PAGE, build_agent, handle_ask


def test_page_is_self_contained() -> None:
    """页面不能依赖 CDN：离线/内网评审也能打开。"""
    assert 'fetch("/ask"' in PAGE
    assert "http://" not in PAGE.replace("http://127.0.0.1", "")
    assert "https://" not in PAGE


def test_page_has_corpus_placeholder() -> None:
    assert "__CORPUS__" in PAGE


def test_page_lists_acceptance_examples() -> None:
    for marker in ("报销期限多久？", "年假可以攒到明年吗？", "公司明年会裁员吗？"):
        assert marker in PAGE


def test_page_shows_decision_steps_section() -> None:
    assert "决策过程" in PAGE
    assert "trace_steps" in PAGE


def test_page_shows_model_and_waiting_hint() -> None:
    """页面要能告诉用户当前模型是谁、以及正在等多久（真模型一轮可能 1~2 分钟）。"""
    assert "__MODEL__" in PAGE and "__SLOW__" in PAGE
    assert "已等待" in PAGE
    assert "真实模型" in PAGE


def test_page_explains_client_abort() -> None:
    """浏览器 5 分钟超时中止时，要给出可操作的原因，而不是裸的 AbortError。"""
    assert "超过 5 分钟" in PAGE
    assert "服务端还是旧版本" in PAGE
    assert "AbortError" in PAGE


def test_handle_ask_includes_trace_steps() -> None:
    agent = build_agent(model="mock", corpus="conflict_min")
    result = handle_ask({"question": "报销期限多久？"}, agent)
    steps = result["trace_steps"]
    assert len(steps) >= 3
    assert steps[0]["action"] == "search_docs"
    assert steps[0]["rationale"]
    assert steps[-1]["action"] in {"ask_user", "answer", "finish"}


def test_trace_steps_are_per_run_not_accumulated() -> None:
    """同一个 Agent 连续跑两轮，第二轮不能带上第一轮的步骤（页面曾因此串味）。"""
    agent = build_agent(model="mock", corpus="full")
    first = handle_ask({"question": "年假可以攒到明年吗？"}, agent)
    second = handle_ask({"question": "那试用期员工呢？"}, agent)
    assert len(first["trace_steps"]) == first["steps"]
    assert len(second["trace_steps"]) == second["steps"]
    assert "试用期" in str(second["trace_steps"][0]["args"].get("query", ""))


def test_server_builds_fresh_agent_per_request() -> None:
    from src.http_app import Server

    app = Server(model="mock", corpus="conflict_min")
    first, second = app.new_agent(), app.new_agent()
    assert first is not second
    assert app.model_name() == "mock"
    assert len(app.preview.chunks) > 0


def test_handle_ask_returns_full_payload() -> None:
    agent = build_agent(model="mock", corpus="conflict_min")
    result = handle_ask({"question": "报销期限多久？"}, agent)
    assert result["status"] == "conflict_pending"
    assert result["answer"]
    assert len(result["citations"]) == 2
    assert result["steps"] > 0


def test_handle_ask_rejects_empty_question() -> None:
    agent = build_agent(model="mock", corpus="conflict_min")
    result = handle_ask({}, agent)
    assert result["status"] == "failed"
    assert "question" in result["error"]


def test_handle_ask_accepts_history() -> None:
    agent = build_agent(model="mock", corpus="full")
    history = [
        {
            "question": "年假可以攒到明年吗？",
            "answer": "不得结转",
            "status": "answered",
            "cited_doc_ids": ["D03"],
        }
    ]
    result = handle_ask({"question": "那试用期员工呢？", "history": history}, agent)
    assert result["status"] == "answered"
    assert result["citations"][0]["chunk_id"] == "D04#c3"
