"""ReAct 主循环：模型选动作 -> 执行工具 -> 观察结果 -> 模型调整或结束。

三条硬约束（都是考卷直接点的）：
1. 终止状态由**代码**判定，不由模型自称：模型说 answered 但校验不过，就打回去重来；
2. 步数 / 工具调用次数双上限，超限返回 incomplete 并列出已完成动作；
3. 每个决策都进 trace（动作、参数、返回摘要、后续动作、一句话理由）。
"""

from __future__ import annotations

import time
from typing import Any

from .actions import TERMINAL_ACTIONS, ActionError, parse_action
from .llm import DecisionError
from .tools import Tools, obs_err, obs_ok
from .trace import NullTrace, StepTrace, elapsed_ms
from .validator import format_errors, verify_answer, verify_citations

DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_TOOL_CALLS = 8
MAX_ASK_USER_ROUNDS = 2

FALLBACK_ANSWER = ""


class Agent:
    def __init__(
        self,
        model: Any,
        tools: Tools | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        trace: StepTrace | None = None,
        history: list[dict] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools or Tools()
        self.max_steps = max(1, int(max_steps))
        self.max_tool_calls = max(1, int(max_tool_calls))
        self.trace = trace or NullTrace()
        self.history = history or []

    # ---------------------------------------------------------------- 主循环

    def run(self, question: str) -> dict:
        started = time.perf_counter()
        observations: list[dict] = []
        tool_calls = 0
        parse_failures = 0
        ask_user_rounds = 0
        self.trace.start(
            {
                "model": getattr(self.model, "name", "unknown"),
                "question": question,
                "history_len": len(self.history),
                "docs_dir": getattr(self.tools.store, "docs_dirs", None),
                "corpus": getattr(self.tools.store, "corpus_name", None),
                "faults": list(self.tools.faults.rules),
                "limits": {"max_steps": self.max_steps, "max_tool_calls": self.max_tool_calls},
            }
        )

        for step in range(1, self.max_steps + 1):
            ctx = {
                "question": question,
                "history": self.history,
                "observations": observations,
                "tool_calls": tool_calls,
                "step": step - 1,
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
            }
            try:
                raw = self.model.decide(ctx)
            except Exception as exc:  # noqa: BLE001 - 模型调用任何异常都要可回灌
                observation = obs_err("model", "model_error", f"模型调用失败：{exc}")
                self.trace.choose(step, "(model_error)", {}, str(exc))
                self.trace.observe(observation)
                observations.append(observation)
                parse_failures += 1
                if parse_failures > 1:
                    return self._finish(
                        question, "failed", FALLBACK_ANSWER, [], ["模型输出不可用"],
                        step - 1, tool_calls, started, "模型连续输出不可用", observations,
                    )
                continue

            try:
                action = parse_action(raw)
            except ActionError as exc:
                observation = obs_err("parser", exc.code, exc.message)
                self.trace.choose(step, "(invalid_output)", {}, "模型输出无法解析")
                self.trace.observe(observation)
                observations.append(observation)
                parse_failures += 1
                if parse_failures > 1:
                    return self._finish(
                        question, "failed", FALLBACK_ANSWER, [], ["模型动作格式不合法"],
                        step, tool_calls, started, "模型输出不可解析", observations,
                    )
                continue

            self.trace.choose(step, action["action"], action["args"], action["why"])

            # ---- 终止类动作：代码终审 ----
            if action["action"] in TERMINAL_ACTIONS:
                verdict = self._judge(action, ask_user_rounds, observations)
                if verdict["final"]:
                    self.trace.observe(verdict["observation"])
                    return self._finish(
                        question,
                        verdict["status"],
                        verdict["answer"],
                        verdict["citations"],
                        verdict["gaps"],
                        step,
                        tool_calls,
                        started,
                        verdict["reason"],
                        observations,
                    )
                if verdict.get("ask_user_round"):
                    ask_user_rounds += 1
                self.trace.observe(verdict["observation"])
                observations.append(verdict["observation"])
                continue

            # ---- 执行类动作 ----
            if action["action"] == "citation_check":
                result = verify_answer(
                    action["args"].get("answer_text", ""),
                    action["args"].get("citations"),
                    self.tools.store,
                    hint=self._latest_hint(observations),
                )
                observation = (
                    obs_ok("citation_check", result, "引用校验通过", None)
                    if result["ok"]
                    else obs_err("citation_check", "validation_failed", format_errors(result))
                )
                if not observation["ok"]:
                    observation["data"] = result
                observations.append(observation)
                self.trace.observe(observation)
                continue

            if tool_calls >= self.max_tool_calls:
                return self._finish_incomplete(question, step, tool_calls, started, "tool_call_limit", observations)

            observation = self.tools.call(action["action"], action["args"])
            tool_calls += 1
            observations.append(observation)
            self.trace.observe(observation)

        return self._finish_incomplete(question, self.max_steps, tool_calls, started, "step_limit", observations)

    # ---------------------------------------------------------------- 终审

    @staticmethod
    def _latest_hint(observations: list[dict]) -> dict | None:
        """最近一次检索给出的冲突线索（没有就返回 None）。"""
        for observation in reversed(observations):
            if observation.get("tool") == "search_docs" and observation.get("ok"):
                hint = (observation.get("hints") or {}).get("conflict_hint")
                if hint:
                    return hint
        return None

    def _judge(self, action: dict, ask_user_rounds: int, observations: list[dict]) -> dict:
        name = action["action"]
        args = action["args"]

        if name == "answer":
            result = verify_answer(
                args.get("answer_text", ""),
                args.get("citations"),
                self.tools.store,
                hint=self._latest_hint(observations),
            )
            if result["ok"]:
                return {
                    "final": True,
                    "status": "answered",
                    "answer": args.get("answer_text", ""),
                    "citations": result["verified"],
                    "gaps": args.get("missing") or [],
                    "reason": "引用逐字可核、数值均有出处，校验通过",
                    "observation": obs_ok("validator", result, "引用校验通过", None),
                }
            return {
                "final": False,
                "observation": {
                    "ok": False,
                    "tool": "validator",
                    "data": result,
                    "error_code": "validation_failed",
                    "note": "回答未通过校验，请修正引用或改为 ask_user/finish：" + format_errors(result),
                },
            }

        if name == "ask_user":
            citations = args.get("citations") or []
            verified, errors = verify_citations(citations, self.tools.store) if citations else ([], [])
            doc_ids = {chunk.doc_id for chunk, _ in verified}
            if len(doc_ids) < 2:
                if ask_user_rounds >= MAX_ASK_USER_ROUNDS:
                    return {
                        "final": True,
                        "status": "insufficient",
                        "answer": args.get("question", ""),
                        "citations": [{"doc_id": c.doc_id, "chunk_id": c.chunk_id, "quote": q} for c, q in verified],
                        "gaps": args.get("missing") or ["足够的冲突证据"],
                        "reason": "要求用户确认，但无法给出两份以上互相冲突的原文证据",
                        "observation": obs_ok("validator", {}, "转 insufficient", None),
                    }
                detail = format_errors({"errors": errors}) if errors else "引用不足两份"
                return {
                    "final": False,
                    "ask_user_round": True,
                    "observation": {
                        "ok": False,
                        "tool": "validator",
                        "data": {"errors": errors},
                        "error_code": "insufficient_citations",
                        "note": f"ask_user 需要至少两份互相冲突的原文引用（{detail}），请先 read_chunk 再引用",
                    },
                }
            return {
                "final": True,
                "status": "conflict_pending",
                "answer": args.get("question", ""),
                "citations": [{"doc_id": c.doc_id, "chunk_id": c.chunk_id, "quote": q} for c, q in verified],
                "gaps": args.get("missing") or [],
                "reason": "存在互斥来源且缺乏仲裁依据，按红线请求用户确认",
                "observation": obs_ok("validator", {}, "转 conflict_pending", None),
            }

        # finish
        status = args.get("status", "insufficient")
        note = args.get("summary", "")
        if status == "answered":
            return {
                "final": True,
                "status": "insufficient",
                "answer": note,
                "citations": [],
                "gaps": args.get("missing") or ["可直接引用的原文片段"],
                "reason": "finish 声明 answered 但没有经过校验的回答，按红线降级为 insufficient",
                "observation": obs_ok("validator", {}, "降级为 insufficient", None),
            }
        return {
            "final": True,
            "status": status,
            "answer": note,
            "citations": [],
            "gaps": args.get("missing") or [],
            "reason": f"模型主动以 {status} 结束：{note}",
            "observation": obs_ok("validator", {}, f"以 {status} 结束", None),
        }

    # ---------------------------------------------------------------- 收尾

    def _finish(self, question, status, answer, citations, gaps, steps, tool_calls, started, reason, observations):
        payload = {
            "question": question,
            "status": status,
            "answer": answer,
            "citations": citations,
            "gaps": gaps,
            "steps": steps,
            "tool_calls": tool_calls,
            "model": getattr(self.model, "name", "unknown"),
            "reason": reason,
            "duration_ms": elapsed_ms(started),
        }
        usage = getattr(self.model, "last_usage", None)
        if usage:
            payload["usage"] = usage
        self.trace.decision(steps, "end", status, reason)
        self.trace.end(payload)
        payload["trace_path"] = str(self.trace.path) if getattr(self.trace, "path", None) else None
        return payload

    def _finish_incomplete(self, question, steps, tool_calls, started, reason, observations):
        done = [f"{o.get('tool')}:{o.get('error_code') or 'ok'}" for o in observations]
        summary = (
            f"达到上限（{reason}）：已用 {steps} 步 / {tool_calls} 次工具调用仍未得出结论。"
        )
        payload = self._finish(
            question,
            "incomplete",
            summary,
            [],
            [f"未完成：{reason}"],
            steps,
            tool_calls,
            started,
            f"达到上限 {reason}，已完成动作：{', '.join(done) or '无'}",
            observations,
        )
        return payload
