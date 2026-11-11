"""极简 HTTP 入口（只用标准库，零依赖）。

POST /ask  {"question": "...", "history": [...]}
          -> {"status","answer","citations","gaps","steps","tool_calls",...}

启动：python -m src.http_app --port 8000
注意：这是演示用的薄封装，没有鉴权、没有并发限流；生产化不在本次范围（见 README）。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import DEFAULT_MAX_STEPS, DEFAULT_MAX_TOOL_CALLS, Agent
from .llm import build_model
from .tools import DocStore, Tools
from .trace import StepTrace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_DIR = str(PROJECT_ROOT / "data" / "docs")


def build_agent(
    model: str = "mock",
    docs_dirs: list[str] | None = None,
    corpus: str = "full",
    faults: list[str] | None = None,
    trace_out: str | None = None,
    history: list[dict] | None = None,
) -> Agent:
    store = DocStore(docs_dirs=docs_dirs or [DEFAULT_DOCS_DIR], corpus=corpus)
    return Agent(
        model=build_model(model),
        tools=Tools(store=store, faults=faults),
        max_steps=DEFAULT_MAX_STEPS,
        max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
        trace=StepTrace(trace_out) if trace_out else None,
        history=history or [],
    )


def handle_ask(payload: dict, agent: Agent) -> dict:
    question = (payload or {}).get("question")
    if not isinstance(question, str) or not question.strip():
        return {"status": "failed", "answer": "", "error": "缺少 question 字段"}
    agent.history = payload.get("history") or []
    return agent.run(question.strip())


class _Handler(BaseHTTPRequestHandler):
    server_version = "knowledge-qa/0.1"
    agent: Agent = None  # type: ignore[assignment]

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定
        if self.path in {"/", "/health"}:
            self._send(200, {"ok": True, "service": "knowledge-qa", "endpoint": "POST /ask"})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ask":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "body 必须是 JSON"})
            return
        try:
            result = handle_ask(payload, self.agent)
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
            return
        self._send(200, result)

    def log_message(self, fmt: str, *args) -> None:  # 静默，避免污染 stdout
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.http_app")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default="mock", choices=["mock", "real"])
    parser.add_argument("--corpus", default="full")
    parser.add_argument("--docs-dir", action="append", default=None)
    parser.add_argument("--fault", action="append", default=None)
    parser.add_argument("--trace-out", default=None)
    args = parser.parse_args(argv)

    _Handler.agent = build_agent(
        model=args.model,
        docs_dirs=args.docs_dir,
        corpus=args.corpus,
        faults=args.fault,
        trace_out=args.trace_out,
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"listening on http://127.0.0.1:{args.port}  (POST /ask)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
