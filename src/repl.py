"""交互式会话入口：进入之后就像聊天一样连续提问。

  python -m src.repl
  python -m src.repl --model real --corpus full

和 `python -m src.cli` 的区别：CLI 是"一次调用一个问题"，适合脚本与复现；
REPL 是"进入会话"，保留多轮上下文（history），可以追问「那试用期员工呢？」，
也可以随时 /model、/corpus 切换。

常用命令（输入 /help 查看）：
  /help            帮助
  /model [auto|mock|real]   查看或切换模型
  /corpus <名字|路径>       切换语料子集（full / conflict_min / no_claim / JSON 路径）
  /docs <目录>              叠加文档目录（内容替换夹具用）
  /trace on|off     是否展示每一步决策
  /reset            清空多轮上下文
  /quit             退出（Ctrl+D / Ctrl+C 也行）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agent import DEFAULT_MAX_STEPS, DEFAULT_MAX_TOOL_CALLS, Agent
from .llm import build_model, resolve_model_kind
from .tools import DocStore, Tools
from .trace import StepTrace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS = str(PROJECT_ROOT / "data" / "docs")

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


@dataclass
class Session:
    model_kind: str = "auto"
    corpus: str = "full"
    docs_dirs: list[str] | None = None
    show_trace: bool = False
    history: list[dict] = field(default_factory=list)
    trace_dir: str | None = None

    def __post_init__(self) -> None:
        self._reload()

    # -- 装配 ---------------------------------------------------------

    def _reload(self) -> None:
        dirs = self.docs_dirs or [DEFAULT_DOCS]
        self.store = DocStore(docs_dirs=[str(Path(d).resolve()) for d in dirs], corpus=self.corpus)
        self.tools = Tools(store=self.store)
        self.resolved = resolve_model_kind(self.model_kind)

    def banner(self) -> str:
        lines = [
            _c("企业知识问答 Agent · 交互模式", "1;36"),
            f"模型 {_c(self.resolved, '33')}（--model {self.model_kind}）｜语料 {_c(self.corpus, '33')}｜"
            f"上限 {DEFAULT_MAX_STEPS} 步 / {DEFAULT_MAX_TOOL_CALLS} 次调用",
            "输入问题开始，/help 看命令，/quit 退出。",
        ]
        return "\n".join(lines)

    # -- 回合 ---------------------------------------------------------

    def ask(self, question: str) -> dict:
        trace_path = None
        if self.trace_dir:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            trace_path = str(Path(self.trace_dir) / f"repl-{stamp}.jsonl")
        trace = StepTrace(trace_path) if trace_path else None
        agent = Agent(
            model=build_model(self.resolved),
            tools=self.tools,
            max_steps=DEFAULT_MAX_STEPS,
            max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
            trace=trace,
            history=self.history,
        )
        result = agent.run(question)
        result["trace_steps"] = agent.trace_steps()
        self.history.append(
            {
                "question": question,
                "answer": result.get("answer", ""),
                "status": result["status"],
                "cited_doc_ids": sorted({c["doc_id"] for c in result.get("citations") or []}),
            }
        )
        return result

    # -- 命令 ---------------------------------------------------------

    def command(self, line: str) -> tuple[bool, str]:
        """返回 (是否继续会话, 输出文本)。"""
        parts = line.strip().split()
        name = parts[0].lower()
        if name in {"/quit", "/exit", "/q"}:
            return False, "会话结束。"
        if name in {"/help", "/h", "/?"}:
            return True, __doc__.split("常用命令")[1].strip() if "常用命令" in (__doc__ or "") else "/help"
        if name == "/reset":
            self.history.clear()
            return True, "已清空多轮上下文。"
        if name == "/model":
            if len(parts) == 1:
                return True, f"当前模型：{self.resolved}（--model {self.model_kind}）"
            kind = parts[1].lower()
            if kind not in {"auto", "mock", "real"}:
                return True, "用法：/model auto|mock|real"
            self.model_kind = kind
            self.resolved = resolve_model_kind(kind)
            return True, f"已切换模型：{self.resolved}"
        if name == "/corpus":
            if len(parts) == 1:
                return True, f"当前语料：{self.corpus}"
            try:
                self.corpus = parts[1]
                self._reload()
            except Exception as exc:  # noqa: BLE001
                self.corpus = "full"
                self._reload()
                return True, f"切换失败：{exc}（已回到 full）"
            return True, f"已切换语料：{self.corpus}（{len(self.store.chunks)} 个片段）"
        if name == "/docs":
            if len(parts) == 1:
                return True, "用法：/docs <目录>（可多次，后者覆盖前者）"
            self.docs_dirs = (self.docs_dirs or []) + [parts[1]]
            self._reload()
            return True, f"已叠加文档目录：{parts[1]}"
        if name == "/trace":
            if len(parts) == 1:
                return True, f"决策轨迹展示：{'开' if self.show_trace else '关'}"
            self.show_trace = parts[1].lower() in {"on", "1", "true", "yes"}
            return True, f"决策轨迹展示：{'开' if self.show_trace else '关'}"
        return True, f"未知命令 {name}，输入 /help 查看。"


def format_result(result: dict, show_trace: bool) -> str:
    status = result["status"]
    color = {"answered": "32", "conflict_pending": "33"}.get(status, "31" if status in {"failed", "incomplete"} else "37")
    lines = [_c("─" * 56, "90"), _c(f"状态 {status}", f"1;{color}")]
    lines.append(result.get("answer") or "（无回答）")
    citations = result.get("citations") or []
    if citations:
        lines.append(_c("引用（逐字原文）", "36"))
        for citation in citations:
            lines.append(f"  {citation['doc_id']}/{citation['chunk_id']}")
            lines.append(f"    「{citation['quote']}」")
    if result.get("gaps"):
        lines.append(_c("资料缺口", "33"))
        lines.extend(f"  · {gap}" for gap in result["gaps"])
    usage = result.get("usage") or {}
    meta = (
        f"模型 {result['model']}｜步数 {result['steps']}｜调用 {result['tool_calls']}｜"
        f"耗时 {result['duration_ms']} ms"
    )
    if usage.get("total_tokens"):
        meta += f"｜tokens {usage['total_tokens']}"
    lines.append(_c(meta, "90"))
    if show_trace:
        lines.append(_c("决策过程", "36"))
        for step in result.get("trace_steps") or []:
            args = step.get("args") or {}
            detail = args.get("query") or args.get("chunk_id") or args.get("topic") or ""
            lines.append(f"  {step['step']}. {step['action']} {detail}")
            lines.append(f"     理由：{step.get('rationale', '')}")
            lines.append(f"     看到：{step.get('observation_summary', '')}")
    if result.get("trace_path"):
        lines.append(_c(f"trace：{result['trace_path']}", "90"))
    return "\n".join(lines)


def run(session: Session, lines, write, prompt: str = "你› ") -> None:
    """可测试的主循环：lines 是输入行来源，write 是输出函数。"""
    write(session.banner())
    while True:
        try:
            line = next(lines)
        except StopIteration:
            write("会话结束。")
            return
        line = (line or "").strip()
        if not line:
            continue
        if line.startswith("/"):
            keep_going, message = session.command(line)
            write(message)
            if not keep_going:
                return
            continue
        try:
            result = session.ask(line)
        except Exception as exc:  # noqa: BLE001 - 会话不中断
            write(_c(f"出错了：{exc.__class__.__name__}: {exc}", "31"))
            continue
        write(format_result(result, session.show_trace))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.repl", description="交互式知识问答会话")
    parser.add_argument("--model", default="auto", choices=["auto", "mock", "real"])
    parser.add_argument("--corpus", default="full")
    parser.add_argument("--docs-dir", action="append", default=None)
    parser.add_argument("--trace-dir", default=None, help="每轮 trace 落盘目录")
    parser.add_argument("--show-trace", action="store_true", help="启动即展示决策过程")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = Session(
        model_kind=args.model,
        corpus=args.corpus,
        docs_dirs=args.docs_dir,
        show_trace=args.show_trace,
        trace_dir=args.trace_dir,
    )

    def _lines():
        while True:
            try:
                yield input(_c("你› ", "1;36") if USE_COLOR else "你› ")
            except (EOFError, KeyboardInterrupt):
                print()
                return

    run(session, _lines(), print)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
