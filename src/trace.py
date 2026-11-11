"""执行 trace：每步「动作 / 参数 / 返回摘要 / 后续动作 / 一句话理由」。

刻意只记模型自述的一句 `why`，不记任何内部思维过程。
「后续动作」要等下一条决策才知道，所以记录会在缓冲区里多留一步，等下一步决策到达时再落盘。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tools import summarize_observation


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class StepTrace:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        self._pending: dict | None = None
        self._handle = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8")

    # -- 生命周期 -----------------------------------------------------

    def start(self, meta: dict) -> None:
        self._emit({"type": "run_start", "ts": _now(), **meta})

    def choose(self, step: int, action: str, args: dict, why: str) -> None:
        """模型选择了一个动作：先把上一条记录的 next_action 补上。"""
        if self._pending is not None:
            self._pending["next_action"] = action
            self._flush()
        self._pending = {
            "type": "step",
            "step": step,
            "ts": _now(),
            "action": action,
            "args": args,
            "rationale": why,
            "observation_summary": "",
            "ok": None,
            "error_code": None,
            "next_action": None,
        }

    def observe(self, observation: dict) -> None:
        if self._pending is None:
            return
        self._pending["ok"] = bool(observation.get("ok"))
        self._pending["error_code"] = observation.get("error_code")
        self._pending["observation_summary"] = summarize_observation(observation)

    def decision(self, step: int, final_action: str, status: str, reason: str) -> None:
        if self._pending is not None:
            self._pending["next_action"] = "end"
            self._flush()
        self._emit(
            {
                "type": "decision",
                "step": step,
                "ts": _now(),
                "final_action": final_action,
                "status": status,
                "reason": reason,
            }
        )

    def end(self, payload: dict) -> None:
        if self._pending is not None:
            self._pending["next_action"] = "end"
            self._flush()
        self._emit({"type": "run_end", "ts": _now(), **payload})
        if self._handle:
            self._handle.close()
            self._handle = None

    # -- 内部 ---------------------------------------------------------

    def _flush(self) -> None:
        record = self._pending
        self._pending = None
        if record is not None:
            self._emit(record)

    def _emit(self, record: dict) -> None:
        self.records.append(record)
        if self._handle:
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()


class NullTrace(StepTrace):
    """不落盘时用，接口一致。"""

    def __init__(self) -> None:  # noqa: D107 - 显式不带路径
        super().__init__(None)


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
