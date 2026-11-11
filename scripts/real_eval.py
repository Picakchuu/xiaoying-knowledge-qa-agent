"""真实模型评测：把 15 组验收用例用真模型跑一遍，逐条断言并出报告。

  MODEL_NAME=deepseek-chat python scripts/real_eval.py --out examples/real-eval-deepseek-chat.md
  python scripts/real_eval.py --only a2_conflict,a2c           # 只跑某几条
  python scripts/real_eval.py --max-steps 8                    # 收紧预算

和 pytest / selfcheck 的区别：
  - pytest  = 代码级断言，模型客户端全打桩，不联网；
  - selfcheck = 用 mock 决策器跑验收行为（快、可重复、但不证明模型质量）；
  - real_eval = 真模型跑同一批用例，断言"状态 / 引用 / 缺口 / 上限"，
                并记录 tokens、耗时、失败原因。需要 .env 里的 MODEL_API_KEY。

评测不追求"每次都一样"：真模型有随机性，所以断言的是**行为区间**（状态、引用可核、不许编造），
不是"必须 5 步"。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_examples import CASES  # noqa: E402

from src.agent import DEFAULT_MAX_STEPS, DEFAULT_MAX_TOOL_CALLS, Agent  # noqa: E402
from src.llm import build_model, resolve_model_kind  # noqa: E402
from src.tools import DocStore, Tools  # noqa: E402
from src.validator import verify_citations  # noqa: E402

NO_CITATION = {"insufficient", "failed", "incomplete"}


def run_case(case: dict, max_steps: int, max_tool_calls: int) -> dict:
    docs_dirs = case.get("docs_dirs") or [ROOT / "data" / "docs"]
    store = DocStore(docs_dirs=[str(d) for d in docs_dirs], corpus=case["corpus"])
    agent = Agent(
        model=build_model("real"),
        tools=Tools(store=store, faults=case.get("faults")),
        max_steps=min(max_steps, case.get("max_steps", max_steps)),
        max_tool_calls=min(max_tool_calls, case.get("max_tool_calls", max_tool_calls)),
        history=case.get("history") or [],
    )
    started = time.perf_counter()
    result = agent.run(case["question"])
    elapsed = int((time.perf_counter() - started) * 1000)
    result["trace_steps"] = agent.trace_steps()
    result["_elapsed_ms"] = elapsed
    result["_problems"] = judge(case, result, store)
    return result


def judge(case: dict, result: dict, store: DocStore) -> list[str]:
    problems: list[str] = []
    actual = [c["chunk_id"] for c in result.get("citations") or []]
    expected = case.get("real_statuses") or [case["status"]]
    if result["status"] not in expected:
        problems.append(f"状态 {result['status']} ∉ 期望 {expected}")
    for chunk_id in case.get("cites") or []:
        if chunk_id not in actual:
            problems.append(f"缺引用 {chunk_id}")
    if result["status"] in NO_CITATION and actual:
        problems.append("该状态不应带引用")
    if result["status"] == "conflict_pending" and len({c["doc_id"] for c in result["citations"]}) < 2:
        problems.append("conflict_pending 需要 ≥2 个不同文档")
    if actual:
        _, errors = verify_citations(result["citations"], store)
        if errors:
            problems.append("引用不合法：" + errors[0]["code"])
    answer = result.get("answer") or ""
    if any(word in answer for word in ("未提供", "没有提供", "未给出", "未写明")) and not result.get("gaps"):
        problems.append("回答说不清楚但 gaps 为空")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/real_eval.py")
    parser.add_argument("--only", default=None, help="逗号分隔的用例 id 片段")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    parser.add_argument("--out", default=str(Path("examples") / "real-eval.md"))
    parser.add_argument("--json", action="store_true", help="同时写一份 JSON")
    args = parser.parse_args(argv)

    model_kind = resolve_model_kind("auto")
    if model_kind != "real":
        print("没有配置 MODEL_API_KEY，无法做真实模型评测（这是预期行为，不是失败）", file=sys.stderr)
        return 2

    cases = [case for case in CASES if not case.get("mock_only")]
    if args.only:
        needles = [part.strip() for part in args.only.split(",") if part.strip()]
        cases = [case for case in cases if any(n in case["id"] for n in needles)]

    rows: list[dict] = []
    for case in cases:
        result = run_case(case, args.max_steps, args.max_tool_calls)
        usage = result.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        row = {
            "id": case["id"],
            "expect": case["expect"],
            "status": result["status"],
            "expected_status": case["status"],
            "steps": result["steps"],
            "task_calls": result["tool_calls"],
            "cites": [c["chunk_id"] for c in result.get("citations") or []],
            "gaps": result.get("gaps") or [],
            "tokens": usage.get("total_tokens"),
            "reasoning": details.get("reasoning_tokens"),
            "elapsed_ms": result["_elapsed_ms"],
            "problems": result["_problems"],
            "answer": result.get("answer") or "",
            "model": result.get("model"),
        }
        rows.append(row)
        mark = "PASS" if not row["problems"] else "FAIL"
        print(
            f"[{mark}] {row['id']:<28} {row['status']:<17} steps={row['steps']:>2} "
            f"cites={len(row['cites'])} {row['elapsed_ms']:>6}ms tokens={row['tokens']}",
            flush=True,
        )
        for problem in row["problems"]:
            print(f"        ✗ {problem}", flush=True)

    passed = sum(1 for row in rows if not row["problems"])
    total_tokens = sum(row["tokens"] or 0 for row in rows)
    total_ms = sum(row["elapsed_ms"] for row in rows)

    lines = [
        f"# 真实模型评测报告（{model_kind}）",
        "",
        f"- 模型：`{rows[0]['model'] if rows else 'n/a'}`",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 用例：{len(rows)} 条｜通过：{passed}/{len(rows)}｜总 tokens：{total_tokens}｜总耗时：{total_ms / 1000:.0f}s",
        "",
        "| 用例 | 期望 | 实际 | 步数 | 工具调用 | 引用 | tokens | 耗时 | 结果 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['id']}` | {row['expected_status']} | {row['status']} | {row['steps']} | "
            f"{row['task_calls']} | {len(row['cites'])} | {row['tokens']} | {row['elapsed_ms']} ms | "
            f"{'PASS' if not row['problems'] else 'FAIL'} |"
        )
    failures = [row for row in rows if row["problems"]]
    if failures:
        lines += ["", "## 失败明细", ""]
        for row in failures:
            lines.append(f"### `{row['id']}`")
            lines.append("")
            lines.append(f"- 问题：{'；'.join(row['problems'])}")
            lines.append(f"- 回答：{row['answer'][:300]}")
            lines.append("")
    else:
        lines += ["", "全部用例通过：状态、引用逐字可核、该拒答的零引用、冲突状态带 ≥2 份来源。"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告：{out}", flush=True)
    if args.json:
        (out.with_suffix(".json")).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
