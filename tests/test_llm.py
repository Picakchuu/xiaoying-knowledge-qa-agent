"""模型装配与体检的单元测试（不联网）。"""

from __future__ import annotations

import pytest

from src.llm import build_model, ping


def test_build_model_mock_by_default() -> None:
    assert build_model("mock").name == "mock"
    assert build_model("offline").name == "mock"


def test_build_model_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        build_model("gpt-5")


def test_ping_reports_config_error_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # 不读 .env（用户本地可能有 key），也不允许环境变量里存在 key
    monkeypatch.setattr("src.llm.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("MODEL_API_KEY", "")
    result = ping()
    assert result["ok"] is False
    assert result["stage"] == "config"
    assert "MODEL_API_KEY" in result["error"]
