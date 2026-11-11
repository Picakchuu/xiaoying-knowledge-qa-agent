"""HTTP 薄封装的冒烟测试（不监听端口，直接测处理函数）。"""

from __future__ import annotations

from src.http_app import build_agent, handle_ask


def test_handle_ask_returns_full_payload() -> None:
    agent = build_agent(corpus="conflict_min")
    result = handle_ask({"question": "报销期限多久？"}, agent)
    assert result["status"] == "conflict_pending"
    assert result["answer"]
    assert len(result["citations"]) == 2
    assert result["steps"] > 0


def test_handle_ask_rejects_empty_question() -> None:
    agent = build_agent(corpus="conflict_min")
    result = handle_ask({}, agent)
    assert result["status"] == "failed"
    assert "question" in result["error"]


def test_handle_ask_accepts_history() -> None:
    agent = build_agent(corpus="full")
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
