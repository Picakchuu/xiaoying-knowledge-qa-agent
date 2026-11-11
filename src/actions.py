"""动作空间：把模型输出收束成可校验的结构。

模型只允许「提议」动作，参数必须落在枚举范围内；解析失败不抛栈，而是变成一条可回灌的
observation（error_code=bad_model_output），让模型自己改——这也是观察-调整的一环。
"""

from __future__ import annotations

import json
from typing import Any

VALID_STATUSES = ("answered", "conflict_pending", "insufficient", "failed", "incomplete")

TOOL_ACTIONS = ("search_docs", "read_chunk", "list_versions")
TERMINAL_ACTIONS = ("answer", "ask_user", "finish")
BUILTIN_ACTIONS = ("citation_check",)
ALL_ACTIONS = TOOL_ACTIONS + TERMINAL_ACTIONS + BUILTIN_ACTIONS

REQUIRED: dict[str, tuple[str, ...]] = {
    "search_docs": ("query",),
    "read_chunk": ("doc_id", "chunk_id"),
    "list_versions": ("topic",),
    "citation_check": ("answer_text", "citations"),
    "ask_user": ("question",),
    "answer": ("answer_text", "citations"),
    "finish": ("status", "summary"),
}

# 容忍常见的字段别名：模型把 answer_text 写成 text / answer 时不必整轮报废
ALIASES: dict[str, dict[str, str]] = {
    "answer": {"text": "answer_text", "answer": "answer_text", "quote": "answer_text"},
    "citation_check": {"text": "answer_text", "answer": "answer_text"},
    "ask_user": {"question_text": "question", "text": "question"},
    "finish": {"status_text": "status", "summary_text": "summary", "reason": "summary"},
}

# 参数类型白名单：防止模型往工具里塞奇怪结构
_INT_ARGS = {"top_k"}
_BOOL_ARGS = {"include_deprecated"}
_STR_ARGS = {"query", "doc_id", "chunk_id", "topic", "doc_type", "answer_text", "question", "summary"}

_CITATION_FIELDS = ("doc_id", "chunk_id", "quote")


class ActionError(Exception):
    """模型输出不可执行。code 固定为 bad_model_output，便于统计。"""

    code = "bad_model_output"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
    return "\n".join(lines).strip()


def _normalize_citations(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ActionError("citations 必须是数组")
    citations: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ActionError(f"第 {index} 条引用必须是对象")
        citation = {}
        for field in _CITATION_FIELDS:
            value = item.get(field)
            citation[field] = value.strip() if isinstance(value, str) else value
        citations.append(citation)
    return citations


def parse_action(raw: Any) -> dict:
    """把模型原始输出解析成 {"action", "args", "why"}。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError as exc:
            raise ActionError(f"输出不是合法 JSON：{exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ActionError("输出必须是 JSON 对象")

    name = raw.get("action") or raw.get("name") or raw.get("tool")
    if not isinstance(name, str) or not name.strip():
        raise ActionError("缺少 action 字段")
    name = name.strip()
    if name not in ALL_ACTIONS:
        raise ActionError(f"未知动作 {name}，可用动作：{', '.join(ALL_ACTIONS)}")

    args = raw.get("args")
    if args is None:
        args = {key: value for key, value in raw.items() if key not in {"action", "name", "tool", "why", "reason"}}
    if not isinstance(args, dict):
        raise ActionError("args 必须是对象")

    for alias, target in ALIASES.get(name, {}).items():
        if alias in args and target not in args:
            args[target] = args.pop(alias)

    for key in REQUIRED[name]:
        if key not in args:
            raise ActionError(f"动作 {name} 缺少参数 {key}")

    for key, value in list(args.items()):
        if key in _INT_ARGS and not isinstance(value, bool) and not isinstance(value, int):
            raise ActionError(f"参数 {key} 必须是整数")
        if key in _BOOL_ARGS and not isinstance(value, bool):
            raise ActionError(f"参数 {key} 必须是布尔值")
        if key in _STR_ARGS and not isinstance(value, str):
            raise ActionError(f"参数 {key} 必须是字符串")

    if name in {"answer", "citation_check"}:
        args["citations"] = _normalize_citations(args.get("citations"))
    if name == "ask_user":
        for key in ("missing", "options"):
            value = args.get(key)
            if value is not None and not isinstance(value, list):
                raise ActionError(f"参数 {key} 必须是数组")
        if args.get("citations") is not None:
            args["citations"] = _normalize_citations(args["citations"])
    if name == "finish":
        status = args.get("status")
        if status not in VALID_STATUSES:
            raise ActionError(f"status 必须是 {list(VALID_STATUSES)} 之一")

    why = raw.get("why") or ""
    if not isinstance(why, str):
        why = str(why)
    return {"action": name, "args": args, "why": why.strip()}
