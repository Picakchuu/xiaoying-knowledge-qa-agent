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
