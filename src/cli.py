"""CLI 入口：一条命令跑一个用例，输出 JSON + Markdown，trace 落盘。

例子：
  python -m src.cli --question "报销期限多久？" --model mock --corpus conflict_min --json
  python -m src.cli --input examples/case_a2_conflict/input.json --model real --out-dir examples/case_a2_conflict
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import DEFAULT_MAX_STEPS, DEFAULT_MAX_TOOL_CALLS, Agent
from .llm import build_model
from .tools import DocStore, Tools
from .trace import StepTrace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_DIR = PROJECT_ROOT / "data" / "docs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="企业知识问答 Agent（ReAct + 引用校验 + 冲突处理）",
    )
    parser.add_argument("--question", help="用户问题")
    parser.add_argument("--input", help="输入 JSON：{question, history, corpus, docs_dir, faults}")
    parser.add_argument("--model", default="mock", choices=["mock", "real"], help="mock=离线规则决策器；real=OpenAI 兼容模型")
    parser.add_argument("--docs-dir", action="append", default=None, help="文档目录，可重复；后出现的按 doc_id 覆盖前面的")
    parser.add_argument("--corpus", default=None, help="语料子集：名字（full/conflict_min/no_claim）或 JSON 路径")
    parser.add_argument("--fault", action="append", default=None, help="故障注入 tool:error_code[:n]，可重复")
    parser.add_argument("--trace-out", default=None, help="trace JSONL 路径")
    parser.add_argument("--out-dir", default=None, help="输出目录：写 output.json / output.md / trace.jsonl")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    parser.add_argument("--session-file", default=None, help="多轮会话：读入/写回历史（JSON 数组）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--pretty", action="store_true", help="JSON 缩进输出")
    return parser


def render_markdown(result: dict) -> str:
    lines = [
        f"# {result['question']}",
        "",
        f"- 状态：`{result['status']}`",
        f"- 模型：`{result['model']}`｜步数：{result['steps']}｜工具调用：{result['tool_calls']}"
        f"｜耗时：{result['duration_ms']} ms",
        f"- 判定依据：{result.get('reason', '')}",
        "",
        "## 回答",
        "",
        result.get("answer") or "（无）",
        "",
        "## 引用",
        "",
    ]
    citations = result.get("citations") or []
    if citations:
        for citation in citations:
            lines += [
                f"**{citation['doc_id']} / {citation['chunk_id']}**",
                "",
                f"> {citation['quote']}",
                "",
            ]
    else:
        lines += ["（无引用）", ""]
    lines += ["## 资料缺口", ""]
    gaps = result.get("gaps") or []
    lines += [f"- {gap}" for gap in gaps] if gaps else ["- 无"]
    if result.get("trace_path"):
        lines += ["", f"Trace：`{result['trace_path']}`"]
    return "\n".join(lines) + "\n"


def load_session(path: str | None) -> list[dict]:
    if not path:
        return []
    file = Path(path)
    if not file.is_file():
        return []
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def save_session(path: str | None, history: list[dict]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    payload: dict = {}
    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    question = args.question or payload.get("question")
    if not question:
        print("缺少问题：用 --question 或 --input 指定", flush=True)
        return 2

    docs_dirs = args.docs_dir or payload.get("docs_dir") or [str(DEFAULT_DOCS_DIR)]
    corpus = args.corpus or payload.get("corpus") or "full"
    faults = args.fault or payload.get("faults") or []
    history = payload.get("history") or load_session(args.session_file)

    out_dir = Path(args.out_dir) if args.out_dir else None
    trace_path = args.trace_out or (str(out_dir / "trace.jsonl") if out_dir else None)

    store = DocStore(docs_dirs=[str(Path(d).resolve()) for d in docs_dirs], corpus=corpus)
    tools = Tools(store=store, faults=faults)
    trace = StepTrace(trace_path) if trace_path else None
    agent = Agent(
        model=build_model(args.model),
        tools=tools,
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
        trace=trace,
        history=history,
    )
    result = agent.run(question)

    if args.session_file:
        history = history + [
            {
                "question": question,
                "answer": result.get("answer", ""),
                "status": result["status"],
                "cited_doc_ids": sorted({c["doc_id"] for c in result.get("citations") or []}),
            }
        ]
        save_session(args.session_file, history)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "output.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "output.md").write_text(render_markdown(result), encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
