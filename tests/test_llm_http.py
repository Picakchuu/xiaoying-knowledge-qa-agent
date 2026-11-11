"""真实模型客户端（requests 直连）的单元测试：全部打桩，不联网。"""

from __future__ import annotations

import json

import pytest

import src.llm as llm
from src.llm import DecisionError, OpenAICompatibleModel

FINISH = '{"action":"finish","args":{"status":"insufficient","summary":"ok"},"why":"收尾"}'


class FakeResponse:
    def __init__(self, status: int, payload: dict, text: str | None = None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


def _ok(content: str = FINISH, usage: dict | None = None) -> FakeResponse:
    return FakeResponse(
        200,
        {"choices": [{"message": {"content": content}}], "usage": usage or {"total_tokens": 42}},
    )


def test_happy_path_uses_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        seen.append(json)
        return _ok()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    model = OpenAICompatibleModel(api_key="k", base_url="https://example.test/v1", model_name="m")
    action = model.decide({"question": "q"})
    assert action["action"] == "finish"
    assert model.last_json_mode is True
    assert model.last_usage == {"total_tokens": 42}
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert model.endpoint == "https://example.test/v1/chat/completions"


def test_auth_error_surfaces_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm.requests, "post", lambda *args, **kwargs: FakeResponse(401, {"error": "bad key"})
    )
    model = OpenAICompatibleModel(api_key="k")
    with pytest.raises(DecisionError, match="401"):
        model.decide({"question": "q"})


def test_fallback_when_response_format_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        calls.append(dict(json))
        if "response_format" in json:
            return FakeResponse(400, {"error": "response_format is unsupported"})
        return _ok()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    model = OpenAICompatibleModel(api_key="k")
    action = model.decide({"question": "q"})
    assert action["action"] == "finish"
    assert model.last_json_mode is False
    assert len(calls) == 2 and "response_format" not in calls[1]


def test_non_json_content_is_a_decision_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: _ok(content="这不是 JSON"))
    model = OpenAICompatibleModel(api_key="k")
    with pytest.raises(DecisionError):
        model.decide({"question": "q"})


def test_trailing_explanation_after_json_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测 deepseek-chat 会在 JSON 后面再跟一段说明或第二个对象。"""
    content = FINISH + "\n\n说明：以上是下一步动作。"
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: _ok(content=content))
    model = OpenAICompatibleModel(api_key="k")
    assert model.decide({"question": "q"})["action"] == "finish"


def test_two_json_objects_take_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    content = FINISH + '\n{"action":"finish","args":{"status":"failed","summary":"x"}}'
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: _ok(content=content))
    model = OpenAICompatibleModel(api_key="k")
    assert model.decide({"question": "q"})["args"]["status"] == "insufficient"


def test_code_fenced_json_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    content = "```json\n" + FINISH + "\n```"
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: _ok(content=content))
    model = OpenAICompatibleModel(api_key="k")
    assert model.decide({"question": "q"})["action"] == "finish"


def test_malformed_payload_is_a_decision_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: FakeResponse(200, {"foo": 1}))
    model = OpenAICompatibleModel(api_key="k")
    with pytest.raises(DecisionError):
        model.decide({"question": "q"})


def test_missing_api_key() -> None:
    with pytest.raises(DecisionError):
        OpenAICompatibleModel(api_key="")
