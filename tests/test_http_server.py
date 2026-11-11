"""HTTP 服务器端到端：真起一个子进程服务，用真实 socket 打过去。

为什么不用进程内线程服务器：本机沙箱不允许同一进程内做 loopback 回环
（连接建立即被断开），而"子进程服务器 + 父进程客户端"是评审机器上的真实形态，
也更接近部署时的样子。连不上时给出明确 skip 原因，不伪装成 PASS。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str, timeout: float = 10) -> tuple[int, dict | str, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            return response.status, json.loads(body), content_type
        return response.status, body, content_type


def _post(port: int, payload: dict, timeout: float = 60) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/ask",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


@pytest.fixture(scope="module")
def live_server():
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "src.http_app",
            "--port",
            str(port),
            "--model",
            "mock",
            "--corpus",
            "conflict_min",
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ready = False
    for _ in range(15):
        try:
            _get(port, "/health", timeout=0.5)
            ready = True
            break
        except Exception:  # noqa: BLE001 - 启动期间连不上是正常的
            time.sleep(0.2)
    if not ready:
        process.kill()
        process.wait(timeout=5)
        pytest.skip("本机沙箱无法建立 loopback 连接，跳过 HTTP 端到端测试")
    try:
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def test_health_reports_model_and_corpus(live_server: int) -> None:
    status, data, _ = _get(live_server, "/health")
    assert status == 200
    assert data["model"] == "mock"
    assert data["corpus"] == "conflict_min"
    assert data["chunks"] > 0
    assert data["limits"]["max_steps"] == 12


def test_root_serves_the_demo_page(live_server: int) -> None:
    status, html, content_type = _get(live_server, "/")
    assert status == 200
    assert content_type.startswith("text/html")
    assert 'id="q"' in html
    assert 'fetch("/ask"' in html
    assert "__MODEL__" not in html and "__CORPUS__" not in html  # 占位符必须已替换
    assert "mock" in html


def test_ask_roundtrip_has_consistent_steps(live_server: int) -> None:
    data = _post(live_server, {"question": "报销期限多久？"})
    assert data["status"] == "conflict_pending"
    assert len(data["citations"]) == 2
    # 页面上的「步数」与「决策过程」条数必须一致（曾经的跨请求串味 bug）
    assert len(data["trace_steps"]) == data["steps"]


def test_two_requests_do_not_share_state(live_server: int) -> None:
    first = _post(live_server, {"question": "年假可以攒到明年吗？"})
    second = _post(live_server, {"question": "那试用期员工呢？"})
    assert first["trace_steps"][0]["args"]["query"] != second["trace_steps"][0]["args"]["query"]
    assert len(second["trace_steps"]) == second["steps"]


def test_unknown_path_returns_404(live_server: int) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(live_server, "/nope")
    assert exc.value.code == 404


def test_bad_body_returns_400(live_server: int) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{live_server}/ask",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 400
