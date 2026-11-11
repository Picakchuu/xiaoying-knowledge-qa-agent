"""一条命令自测：跑全部离线验收用例，逐条核对「状态 / 引用 / 不变量」。

用法：
  python scripts/selfcheck.py            # 12 组离线用例，秒级
  python scripts/selfcheck.py --verbose  # 额外打印每条的判定依据

它不替代 pytest（pytest 管代码级断言），管的是「验收行为」：
  - 状态是否等于该用例的预期；
  - answered 必须带引用，且每条 quote 都能逐字命中原文；
  - conflict_pending 必须来自 ≥2 个不同文档；
  - insufficient / failed / incomplete 不允许带引用（防编造）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_examples import CASES  # noqa: E402

from src.agent import DEFAULT_MAX_STEPS, DEFAULT_MAX_TOOL_CALLS, Agent  # noqa: E402
from src.llm import MockModel  # noqa: E402
from src.tools import DocStore, Tools  # noqa: E402
from src.validator import verify_citations  # noqa: E402

NO_CITATION_STATUSES = {"insufficient", "failed", "incomplete"}


def check_case(case: dict) -> tuple[dict, list[str]]:
    docs_dirs = case.get("docs_dirs") or [ROOT / "data" / "docs"]
    store = DocStore(docs_dirs=[str(d) for d in docs_dirs], corpus=case["corpus"])
    tools = Tools(store=store, faults=case.get("faults"))
    agent = Agent(
        model=MockModel(),
        tools=tools,
        max_steps=case.get("max_steps", DEFAULT_MAX_STEPS),
        max_tool_calls=case.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS),
        history=case.get("history") or [],
    )
    result = agent.run(case["question"])

    problems: list[str] = []
    actual_chunks = [c["chunk_id"] for c in result.get("citations") or []]
    if result["status"] != case["status"]:
        problems.append(f"状态 {result['status']} ≠ 期望 {case['status']}")
    for chunk_id in case.get("cites") or []:
        if chunk_id not in actual_chunks:
            problems.append(f"缺引用 {chunk_id}")
    if result["status"] in NO_CITATION_STATUSES and actual_chunks:
        problems.append("该状态不应带引用")
    if result["status"] == "conflict_pending":
        docs = {c["doc_id"] for c in result["citations"]}
        if len(docs) < 2:
            problems.append("conflict_pending 需要 ≥2 个不同文档的引用")
    if actual_chunks:
        _, errors = verify_citations(result["citations"], store)
        if errors:
            problems.append("引用不合法：" + errors[0]["code"])
    return result, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/selfcheck.py")
    parser.add_argument("--verbose", action="store_true", help="打印每条用例的判定依据与回答摘要")
    args = parser.parse_args(argv)

    cases = [c for c in CASES if not c.get("real")]  # 真实模型用例是静态记录，不在此复跑
    passed = 0
    total_quotes = 0
    print("=" * 108)
    print(f"{'用例':<28}{'验收点':<38}{'状态':<18}{'引用':>4}{'步':>3}{'调用':>4}  结果")
    print("-" * 108)
    for case in cases:
        result, problems = check_case(case)
        total_quotes += len(result.get("citations") or [])
        ok = not problems
        passed += ok
        print(
            f"{case['id']:<28}{case['expect']:<38}{result['status']:<18}"
            f"{len(result.get('citations') or []):>4}{result['steps']:>3}{result['tool_calls']:>4}  "
            f"{'PASS' if ok else 'FAIL'}"
        )
        if problems:
            for problem in problems:
                print(f"    ✗ {problem}")
        if args.verbose:
            print(f"    回答：{(result.get('answer') or '')[:90]}")
            print(f"    依据：{result.get('reason', '')[:90]}")
            for citation in result.get("citations") or []:
                print(f"    引用：{citation['chunk_id']}「{citation['quote'][:50]}」")
    print("=" * 108)
    print(f"{passed}/{len(cases)} PASS，引用 {total_quotes} 条全部逐字命中原文")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
