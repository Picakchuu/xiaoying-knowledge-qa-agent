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
from .llm import build_model, resolve_model_kind
from .tools import DocStore, Tools
from .trace import StepTrace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_DIR = str(PROJECT_ROOT / "data" / "docs")

# ---------------------------------------------------------------- 演示页面
# 零外部依赖：没有 CDN、没有构建步骤，起服务就能点。界面本身不是考卷要求，
# 只是为了让人能"看见"状态、引用、资料缺口和步数。
PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>企业知识问答 Agent · 演示</title>
<style>
  :root { --bg:#0d1322; --panel:#151d31; --line:#26324b; --text:#e8eeff; --muted:#8ea0c4;
          --ok:#3ecf8e; --warn:#f0b429; --bad:#ef6b6b; --accent:#5b8def; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  .wrap { max-width:900px; margin:0 auto; padding:32px 20px 80px; }
  h1 { font-size:19px; margin:0 0 6px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:22px; }
  .sub code { background:#1b2540; padding:1px 6px; border-radius:5px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }
  textarea { width:100%; min-height:76px; resize:vertical; background:#0b1120; color:var(--text);
             border:1px solid var(--line); border-radius:10px; padding:12px; font:inherit; }
  .row { display:flex; gap:8px; align-items:center; margin-top:12px; flex-wrap:wrap; }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
  .chip { background:#1b2540; border:1px solid var(--line); color:#c9d6f5; border-radius:999px;
          padding:5px 12px; font-size:13px; cursor:pointer; }
  .chip:hover { border-color:var(--accent); color:#fff; }
  button.go { background:var(--accent); border:0; color:#fff; border-radius:9px;
              padding:9px 20px; font:inherit; cursor:pointer; }
  button.go[disabled] { opacity:.55; cursor:default; }
  .hint { color:var(--muted); font-size:12px; margin-left:auto; }
  #out { margin-top:22px; display:none; }
  .badge { display:inline-block; font-size:13px; padding:3px 12px; border-radius:999px;
           border:1px solid currentColor; }
  .status-answered { color:var(--ok); } .status-conflict_pending { color:var(--warn); }
  .status-insufficient { color:var(--muted); } .status-failed { color:var(--bad); }
  .status-incomplete { color:var(--bad); }
  h2 { font-size:14px; color:var(--muted); font-weight:600; margin:20px 0 8px; letter-spacing:.4px; }
  .answer { white-space:pre-wrap; }
  .cite { border-left:3px solid var(--accent); padding:8px 12px; margin:10px 0;
          background:#111a2c; border-radius:0 8px 8px 0; }
  .cite .meta { color:var(--muted); font-size:12px; }
  .cite .quote { color:#d7e2fb; font-size:14px; }
  .gaps li { color:var(--warn); }
  .steps { padding-left:20px; margin:0; }
  .steps li { margin:8px 0; }
  .steps code { background:#1b2540; padding:1px 7px; border-radius:5px; color:#cfe0ff; }
  .meta { color:var(--muted); font-size:12.5px; margin-top:14px; }
  details { margin-top:14px; color:var(--muted); font-size:12.5px; }
  pre { background:#0b1120; border:1px solid var(--line); border-radius:10px;
        padding:12px; overflow:auto; color:#b7c6e8; font-size:12px; }
  .err { color:var(--bad); white-space:pre-wrap; }
</style>
</head>
<body>
<div class="wrap">
  <h1>企业知识问答 Agent</h1>
  <div class="sub">
    依据文档原文作答，结论必须带引用；两份制度打架时会列出冲突并请你确认。
    当前语料：<code id="corpus">__CORPUS__</code>　当前模型：<code id="model">__MODEL__</code>
    <span id="slowtip"></span>
  </div>

  <div class="panel">
    <textarea id="q" placeholder="问点什么，例如：报销期限多久？"></textarea>
    <div class="chips" id="chips"></div>
    <div class="row">
      <button class="go" id="go">提问</button>
      <span class="hint" id="hint">离线 mock 决策器，秒级返回</span>
    </div>
  </div>

  <div id="out" class="panel">
    <div><span id="badge" class="badge"></span> <span class="badge" id="modelbadge"></span></div>
    <h2>回答</h2>
    <div class="answer" id="answer"></div>
    <h2>引用（逐字原文）</h2>
    <div id="cites"></div>
    <h2>资料缺口</h2>
    <ul class="gaps" id="gaps"></ul>
    <h2>决策过程（每一步都是模型选的，不是预设流程）</h2>
    <ol class="steps" id="steps"></ol>
    <div class="meta" id="meta"></div>
    <details><summary>原始 JSON</summary><pre id="raw"></pre></details>
  </div>
</div>

<script>
const EXAMPLES = [
  { label: "报销期限多久？", q: "报销期限多久？" },
  { label: "我是总部员工，报销期限多久？", q: "我是总部员工，报销期限多久？" },
  { label: "年假可以攒到明年吗？", q: "年假可以攒到明年吗？" },
  { label: "那试用期员工呢？（多轮）", q: "那试用期员工呢？",
    history: [{ question: "年假可以攒到明年吗？", answer: "不得结转至次年", status: "answered", cited_doc_ids: ["D03"] }] },
  { label: "专业版一年多少钱？（资料不足）", q: "专业版一年多少钱？" },
  { label: "公司明年会裁员吗？（资料外）", q: "公司明年会裁员吗？" },
  { label: "报销需要交纸质发票吗？（版本）", q: "报销需要交纸质发票吗？" }
];

const $ = (id) => document.getElementById(id);
const SLOW = "__SLOW__" === "1";
let currentHistory = [];
let timer = null;

function setHint(text) { $("hint").textContent = text; }
function idleHint() {
  setHint(SLOW ? "真实模型：单次约 20 秒 ~ 2 分钟，请耐心等" : "离线 mock 决策器，秒级返回");
}
function startWaiting() {
  const t0 = Date.now();
  clearInterval(timer);
  timer = setInterval(() => {
    const secs = ((Date.now() - t0) / 1000).toFixed(0);
    setHint("思考中… 已等待 " + secs + " 秒" + (SLOW ? "（真实模型较慢，通常 20 秒~2 分钟）" : ""));
  }, 250);
}
function stopWaiting() { clearInterval(timer); timer = null; idleHint(); }

function renderChips() {
  EXAMPLES.forEach((item) => {
    const el = document.createElement("button");
    el.className = "chip";
    el.textContent = item.label;
    el.onclick = () => { $("q").value = item.q; currentHistory = item.history || []; ask(); };
    $("chips").appendChild(el);
  });
}

async function ask() {
  const question = $("q").value.trim();
  if (!question) return;
  $("go").disabled = true;
  startWaiting();
  $("out").style.display = "none";
  const controller = new AbortController();
  const hardStop = setTimeout(() => controller.abort(), 300000);
  try {
    const res = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: currentHistory }),
      signal: controller.signal
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    show(data);
  } catch (err) {
    $("out").style.display = "block";
    $("badge").className = "badge status-failed";
    $("badge").textContent = "failed";
    $("answer").innerHTML = '<span class="err">请求失败：' + err + "</span>";
    $("cites").innerHTML = "";
    $("gaps").innerHTML = "";
    $("steps").innerHTML = "";
    $("meta").textContent = "";
  } finally {
    clearTimeout(hardStop);
    stopWaiting();
    $("go").disabled = false;
  }
}

function show(d) {
  $("out").style.display = "block";
  $("badge").className = "badge status-" + d.status;
  $("badge").textContent = d.status;
  $("answer").textContent = d.answer || "（无）";
  $("cites").innerHTML = "";
  (d.citations || []).forEach((c) => {
    const box = document.createElement("div");
    box.className = "cite";
    box.innerHTML = '<div class="meta">' + c.doc_id + " / " + c.chunk_id + "</div>" +
                    '<div class="quote">「' + c.quote + "」</div>";
    $("cites").appendChild(box);
  });
  if (!(d.citations || []).length) {
    $("cites").innerHTML = '<div class="meta">（无引用：这类状态不允许编造出处）</div>';
  }
  $("gaps").innerHTML = "";
  (d.gaps || []).forEach((g) => { const li = document.createElement("li"); li.textContent = g; $("gaps").appendChild(li); });
  if (!(d.gaps || []).length) { const li = document.createElement("li"); li.textContent = "无"; $("gaps").appendChild(li); }
  $("meta").textContent = "模型 " + d.model + " ｜ 步数 " + d.steps + " ｜ 工具调用 " +
    d.tool_calls + " ｜ 耗时 " + d.duration_ms + " ms ｜ 判定：" + (d.reason || "");
  $("modelbadge").textContent = d.model || "";
  const steps = d.trace_steps || [];
  $("steps").innerHTML = "";
  if (!steps.length) { $("steps").innerHTML = '<li class="meta">（无轨迹）</li>'; }
  steps.forEach((s) => {
    const li = document.createElement("li");
    const args = s.args || {};
    const detail = args.query ? "「" + args.query + "」" :
                   (args.chunk_id ? args.chunk_id : (args.topic || ""));
    li.innerHTML = "<code>" + s.action + "</code> " + detail +
      '<div class="meta">理由：' + (s.rationale || "") + "</div>" +
      '<div class="meta">看到：' + (s.observation_summary || "") + " → 下一步：" + (s.next_action || "结束") + "</div>";
    $("steps").appendChild(li);
  });
  $("raw").textContent = JSON.stringify(d, null, 2);
}

$("go").onclick = ask;
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask(); });
renderChips();
idleHint();
if (SLOW) { $("slowtip").textContent = "（真实模型较慢，页面会显示已等待秒数）"; }
</script>
</body>
</html>
"""


def build_agent(
    model: str = "auto",
    docs_dirs: list[str] | None = None,
    corpus: str = "full",
    faults: list[str] | None = None,
    trace_out: str | None = None,
    history: list[dict] | None = None,
) -> Agent:
    store = DocStore(docs_dirs=docs_dirs or [DEFAULT_DOCS_DIR], corpus=corpus)
    return Agent(
        model=build_model(resolve_model_kind(model)),
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
    result = agent.run(question.strip())
    result["trace_steps"] = agent.trace_steps(limit=12)
    return result


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

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定
        if self.path in {"/", "/index.html"}:
            store = getattr(getattr(self.agent, "tools", None), "store", None)
            corpus_name = getattr(store, "corpus_name", None) or "full"
            model_name = getattr(getattr(self.agent, "model", None), "name", "unknown")
            slow = "1" if "openai-compatible" in model_name else "0"
            html = (
                PAGE.replace("__CORPUS__", str(corpus_name))
                .replace("__MODEL__", str(model_name))
                .replace("__SLOW__", slow)
            )
            self._send_html(html)
            return
        if self.path == "/health":
            store = getattr(getattr(self.agent, "tools", None), "store", None)
            self._send(
                200,
                {
                    "ok": True,
                    "service": "knowledge-qa",
                    "endpoint": "POST /ask",
                    "ui": "GET /",
                    "model": getattr(getattr(self.agent, "model", None), "name", "unknown"),
                    "corpus": getattr(store, "corpus_name", None) or "full",
                    "chunks": len(getattr(store, "chunks", {}) or {}),
                    "limits": {"max_steps": DEFAULT_MAX_STEPS, "max_tool_calls": DEFAULT_MAX_TOOL_CALLS},
                },
            )
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
    parser.add_argument("--model", default="auto", choices=["auto", "mock", "real"])
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
