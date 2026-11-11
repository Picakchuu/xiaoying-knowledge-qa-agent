"""批量生成 examples/：每例一个目录（input.json / output.json / output.md / trace.jsonl）。

用法：
  python scripts/make_examples.py --mode mock          # 全部用例走离线 mock（秒级）
  python scripts/make_examples.py --mode real --only a2_conflict,a2b_scoped_conflict,a2c_scoped_answered
  python scripts/make_examples.py --mode all           # mock 全量 + 真实模型那几条

真实模型那几条需要 .env 里的 MODEL_API_KEY；没有 key 时会跳过并打印原因，不会中断 mock 结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agent import DEFAULT_MAX_STEPS, DEFAULT_MAX_TOOL_CALLS, Agent  # noqa: E402
from src.cli import render_markdown  # noqa: E402
from src.llm import DecisionError, build_model  # noqa: E402
from src.tools import DocStore, Tools  # noqa: E402
from src.trace import StepTrace  # noqa: E402

DOCS = ROOT / "data" / "docs"
REGRESSION = ROOT / "data" / "regression"
EXAMPLES = ROOT / "examples"

FOLLOWUP_HISTORY = [
    {
        "question": "年假可以攒到明年吗？",
        "answer": "当年未休完的年假不得结转至次年。",
        "status": "answered",
        "cited_doc_ids": ["D03"],
    }
]

CASES: list[dict] = [
    {"id": "case_a1_answered", "question": "年假可以攒到明年吗？", "corpus": "full",
     "expect": "明确答案：引用 D03#c2", "status": "answered", "cites": ["D03#c2"]},
    {"id": "case_a2_conflict", "question": "报销期限多久？", "corpus": "conflict_min", "real": True,
     "expect": "文档冲突：并列 D01(30 天)/D02(15 天)", "status": "conflict_pending", "cites": ["D01#c2", "D02#c2"]},
    {"id": "case_a2b_scoped_conflict", "question": "报销期限多久？", "corpus": "full", "real": True,
     "expect": "冲突查证：查到总部专用版后追问", "status": "conflict_pending", "cites": ["D01#c2", "D02b#c2"]},
    {"id": "case_a2c_scoped_answered", "question": "我是总部员工，报销期限多久？", "corpus": "full", "real": True,
     "expect": "冲突可解：条件给定时按 D02b 作答", "status": "answered", "cites": ["D02b#c2"]},
    {"id": "case_a3_out_of_scope", "question": "公司明年会裁员吗？", "corpus": "no_claim",
     "expect": "资料外：无引用", "status": "insufficient", "cites": []},
    {"id": "case_a3b_missing_detail", "question": "专业版一年多少钱？", "corpus": "full",
     "expect": "资料不足：缺口=具体价格", "status": "insufficient", "cites": []},
    {"id": "case_a4a_paraphrase", "question": "年假没用完能留到明年吗？", "corpus": "full",
     "expect": "改写问法：与 A1 同源 D03#c2", "status": "answered", "cites": ["D03#c2"]},
    {"id": "case_a4c_query_rewrite", "question": "出差打车一天最多能报多少？", "corpus": "full",
     "expect": "改写检索：命中 D02#c4（200 元）", "status": "answered", "cites": ["D02#c4"]},
    {"id": "case_a5_followup", "question": "那试用期员工呢？", "corpus": "full",
     "history": FOLLOWUP_HISTORY, "expect": "上下文追问：引用 D04#c3", "status": "answered", "cites": ["D04#c3"]},
    {"id": "case_a6_tool_transient", "question": "报销期限多久？", "corpus": "conflict_min",
     "faults": ["search_docs:timeout:2"], "expect": "工具失败（瞬时）：重试后恢复",
     "status": "conflict_pending", "cites": ["D01#c2", "D02#c2"]},
    {"id": "case_a6b_tool_down", "question": "报销期限多久？", "corpus": "conflict_min",
     "faults": ["search_docs:timeout"], "expect": "工具失败（持续）：failed，不编答案",
     "status": "failed", "cites": []},
    {"id": "case_a7_deprecated", "question": "报销需要交纸质发票吗？", "corpus": "full",
     "expect": "版本意识：不把已废止的 D08 当现行", "status": "answered", "cites": ["D01#c3"]},
    {"id": "case_b1_limit", "question": "年假可以攒到明年吗？", "corpus": "full",
     "max_steps": 3, "max_tool_calls": 1, "expect": "上限：incomplete + 明确 reason",
     "status": "incomplete", "cites": []},
    {"id": "case_c1_content_swap", "question": "报销期限多久？", "corpus": "conflict_min",
     "docs_dirs": [DOCS, REGRESSION / "d01_45days"], "expect": "内容替换：冲突值变成 45 vs 15",
     "status": "conflict_pending", "cites": ["D01#c2", "D02#c2"]},
    {"id": "case_c2_content_swap", "question": "年假可以攒到明年吗？", "corpus": "full",
     "docs_dirs": [DOCS, REGRESSION / "d03_carryover_allowed"], "expect": "内容替换：答案反转为「可结转」",
     "status": "answered", "cites": ["D03#c2"]},
]


def run_case(case: dict, model_kind: str) -> dict:
    out_dir = EXAMPLES / case["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    docs_dirs = case.get("docs_dirs") or [DOCS]
    store = DocStore(docs_dirs=[str(d) for d in docs_dirs], corpus=case["corpus"])
    tools = Tools(store=store, faults=case.get("faults"))
    agent = Agent(
        model=build_model(model_kind),
        tools=tools,
        max_steps=case.get("max_steps", DEFAULT_MAX_STEPS),
        max_tool_calls=case.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS),
        trace=StepTrace(out_dir / "trace.jsonl"),
        history=case.get("history") or [],
    )
    result = agent.run(case["question"])
    result["expect"] = case["expect"]
    result["mode"] = model_kind

    inputs = {
        "id": case["id"],
        "question": case["question"],
        "history": case.get("history") or [],
        "corpus": case["corpus"],
        "faults": case.get("faults") or [],
        "docs_dir": [str(Path(d).relative_to(ROOT)) for d in docs_dirs],
        "limits": {
            "max_steps": case.get("max_steps", DEFAULT_MAX_STEPS),
            "max_tool_calls": case.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS),
        },
        "expect": case["expect"],
    }
    (out_dir / "input.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "output.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "output.md").write_text(render_markdown(result), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/make_examples.py")
    parser.add_argument("--mode", choices=["mock", "real", "all"], default="mock")
    parser.add_argument("--only", default=None, help="只跑这些用例（逗号分隔的 id 片段）")
    args = parser.parse_args(argv)

    selected = CASES
    if args.only:
        needles = [part.strip() for part in args.only.split(",") if part.strip()]
        selected = [case for case in CASES if any(n in case["id"] for n in needles)]

    modes: list[tuple[str, list[dict]]] = []
    if args.mode in {"mock", "all"}:
        modes.append(("mock", [c for c in selected if not c.get("real")]))
    if args.mode in {"real", "all"}:
        modes.append(("real", [c for c in selected if c.get("real")]))

    failures = 0
    for model_kind, cases in modes:
        for case in cases:
            try:
                result = run_case(case, model_kind)
            except DecisionError as exc:
                print(f"[skip] {case['id']}（{model_kind}）：{exc}", flush=True)
                continue
            print(
                f"[{model_kind:4}] {case['id']:<26} status={result['status']:<17} "
                f"steps={result['steps']:>2} calls={result['tool_calls']:>2} "
                f"cites={len(result['citations'])}",
                flush=True,
            )
            if result["status"] == "failed":
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
