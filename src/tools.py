"""文档检索 / 片段读取 / 版本反查工具。

约定（与 agent 层解耦的关键）：
- 所有工具都返回统一的 observation 信封，**内部捕获所有异常**，绝不把调用栈抛给模型；
- 失败用 ok=false + error_code 表达，error_code ∈ {timeout, internal_error, bad_args, not_found}；
- 工具只给「证据」和「线索」，不给结论。是否追问、是否作答，由模型决定、由代码终审。

检索为什么不接 embedding：本项目要离线可跑且要能断言「召回是否因改写而改善」。
关键词 + 2-gram + 一张小同义词表足以支撑这条链路，且完全可解释。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .normalize import (
    bigrams,
    canon_unit,
    norm_text,
    shared_bigram_count,
    time_facts,
    tokenize_query,
)
from .synonyms import expand_query

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_DIR = PROJECT_ROOT / "data" / "docs"
DEFAULT_CORPUS = PROJECT_ROOT / "data" / "corpora" / "full.json"

MIN_SCORE = 0.12
LOW_RELEVANCE = 0.30

# 检索用的虚词表：只用来算「字符覆盖率」，不影响原文
_STOP_CHARS = set(
    "的了吗呢啊吧呀哦嘛是在有我你他她它这那和与或及为其之类等就都还也很请问下个一能可要会么怎如何多少"
)

VALID_DOC_TYPES = {"policy", "faq", "any"}
VALID_TOP_K = (1, 20)


class ToolError(Exception):
    """工具内部的可预期错误，带 error_code。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 数据装载


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    chunk_id: str
    heading: str
    text: str
    title: str
    version_date: str
    doc_type: str
    status: str

    def as_dict(self, score: float | None = None) -> dict:
        payload = {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "heading": self.heading,
            "text": self.text,
            "title": self.title,
            "version_date": self.version_date,
            "doc_type": self.doc_type,
            "status": self.status,
        }
        if score is not None:
            payload["score"] = round(score, 3)
        return payload

    @property
    def searchable(self) -> str:
        return f"{self.title}{self.heading}{self.text}"


def resolve_corpus(corpus: str | Path | None) -> list[str] | None:
    """语料可以是路径、也可以是 data/corpora 下的名字；None 表示不过滤。"""
    if corpus is None:
        return None
    path = Path(corpus)
    candidates = [path, PROJECT_ROOT / "data" / "corpora" / f"{corpus}.json"]
    for candidate in candidates:
        if candidate.is_file():
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            return list(payload.get("doc_ids", []))
    raise ToolError("not_found", f"找不到语料子集：{corpus}")


class DocStore:
    """按目录装载文档；后面的目录按 doc_id 覆盖前面的（用于内容替换夹具）。"""

    def __init__(
        self,
        docs_dirs: Iterable[str | Path] | None = None,
        corpus: str | Path | None = None,
    ) -> None:
        self.docs: dict[str, dict] = {}
        self.overridden: list[str] = []
        for directory in docs_dirs or [DEFAULT_DOCS_DIR]:
            base = Path(directory)
            if not base.is_dir():
                raise ToolError("bad_args", f"文档目录不存在：{directory}")
            for path in sorted(base.glob("*.json")):
                doc = json.loads(path.read_text(encoding="utf-8"))
                doc_id = doc["doc_id"]
                if doc_id in self.docs:
                    self.overridden.append(doc_id)
                self.docs[doc_id] = doc
        self.corpus_doc_ids = resolve_corpus(corpus)
        self.chunks: dict[str, Chunk] = {}
        for doc in self.docs.values():
            if self.corpus_doc_ids is not None and doc["doc_id"] not in self.corpus_doc_ids:
                continue
            for chunk in doc["chunks"]:
                self.chunks[chunk["chunk_id"]] = Chunk(
                    doc_id=doc["doc_id"],
                    chunk_id=chunk["chunk_id"],
                    heading=chunk.get("heading", ""),
                    text=chunk["text"],
                    title=doc.get("title", ""),
                    version_date=doc.get("version_date", ""),
                    doc_type=doc.get("doc_type", "any"),
                    status=doc.get("status", "active"),
                )

    # -- 查询 ---------------------------------------------------------

    def get_chunk(self, doc_id: str, chunk_id: str) -> Chunk:
        if doc_id not in self.docs:
            raise ToolError("not_found", f"文档 {doc_id} 不在当前语料中")
        chunk = self.chunks.get(chunk_id)
        if chunk is None or chunk.doc_id != doc_id:
            raise ToolError("not_found", f"片段 {chunk_id} 不在当前语料中")
        return chunk

    def doc_ids(self) -> list[str]:
        return sorted({c.doc_id for c in self.chunks.values()})

    def search(
        self,
        query: str,
        top_k: int = 5,
        doc_type: str = "any",
        include_deprecated: bool = False,
    ) -> tuple[list[Chunk], list[float], dict]:
        query_text = norm_text(query)
        grams = tokenize_query(query)
        expansions = expand_query(query_text)
        for term in expansions:
            grams |= bigrams(term)
        content_chars = {ch for ch in query_text if ch not in _STOP_CHARS}
        exact_terms = [t for t in (norm_text(e) for e in expansions) if len(t) >= 2]

        scored: list[tuple[float, Chunk]] = []
        filtered_deprecated = 0
        for chunk in self.chunks.values():
            if doc_type != "any" and chunk.doc_type != doc_type:
                continue
            if chunk.status == "deprecated" and not include_deprecated:
                filtered_deprecated += 1
                continue
            chunk_text = norm_text(chunk.searchable)
            chunk_grams = bigrams(chunk.searchable)
            matched = len(grams & chunk_grams)
            gram_score = matched / max(len(grams), 1)
            char_coverage = (
                len(content_chars & set(chunk_text)) / max(len(content_chars), 1)
                if content_chars
                else 0.0
            )
            if matched == 0 and char_coverage < 0.3:
                continue
            heading_hit = 0.10 if grams & bigrams(chunk.heading) else 0.0
            exact_bonus = 0.20 if any(term in chunk_text for term in exact_terms) else 0.0
            score = 0.55 * gram_score + 0.35 * char_coverage + heading_hit + exact_bonus
            if score < MIN_SCORE:
                continue
            scored.append((score, chunk))

        scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
        top = scored[:top_k]
        chunks = [chunk for _, chunk in top]
        scores = [score for score, _ in top]

        return chunks, scores, {"filtered_deprecated": filtered_deprecated}


def _conflict_hint(chunks: list[Chunk]) -> dict:
    """跨文档、同一时间单位、不同数值 -> 冲突线索。

    只是线索：确实在谈同一件事（正文共享 >= 2 个 2-gram）才给，避免「5000 元审批额 vs 200 元交通额」
    这类不同事项被误报。是否追问仍由模型决定。
    """
    from itertools import combinations

    by_unit: dict[str, list[dict]] = {}
    texts: dict[str, str] = {}
    for chunk in chunks:
        texts[chunk.chunk_id] = chunk.text
        for fact in time_facts(chunk.text):
            by_unit.setdefault(canon_unit(fact["unit"]), []).append(
                {"doc_id": chunk.doc_id, "chunk_id": chunk.chunk_id, "value": fact["value"]}
            )

    for unit, entries in by_unit.items():
        unique: list[dict] = []
        for entry in entries:
            if entry not in unique:
                unique.append(entry)
        values = sorted({e["value"] for e in unique})
        docs = {e["doc_id"] for e in unique}
        if len(values) < 2 or len(docs) < 2:
            continue
        # 不同文档、且正文确实在谈同一件事，才认为值得提示
        related = any(
            shared_bigram_count(texts[left["chunk_id"]], texts[right["chunk_id"]]) >= 2
            for left, right in combinations(unique, 2)
            if left["doc_id"] != right["doc_id"]
        )
        if not related:
            continue
        return {"unit": unit, "values": values, "candidates": unique}
    return {}


# ---------------------------------------------------------------- 故障注入


class FaultInjector:
    """按 `tool:error_code[:n]` 注入故障，n 缺省表示一直失败。

    用完即止（n 次后自动恢复），用来演示「工具失败 -> 模型换 query 重试」这条路径。
    """

    def __init__(self, specs: Iterable[str] | None = None) -> None:
        self.rules: dict[str, dict] = {}
        self.history: list[dict] = []
        for spec in specs or []:
            parts = spec.split(":")
            tool = parts[0].strip()
            code = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "timeout"
            remaining = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 10**9
            self.rules[tool] = {"code": code, "remaining": remaining}

    def take(self, tool: str) -> str | None:
        rule = self.rules.get(tool)
        if not rule:
            return None
        if rule["remaining"] <= 0:
            return None
        rule["remaining"] -= 1
        self.history.append({"tool": tool, "error_code": rule["code"]})
        return rule["code"]


# ---------------------------------------------------------------- 观测信封


def obs_ok(tool: str, data: Any, note: str = "", hints: dict | None = None) -> dict:
    payload = {"ok": True, "tool": tool, "data": data, "error_code": None, "note": note}
    if hints:
        payload["hints"] = hints
    return payload


def obs_err(tool: str, error_code: str, note: str) -> dict:
    return {"ok": False, "tool": tool, "data": None, "error_code": error_code, "note": note}


# ---------------------------------------------------------------- 工具集合


class Tools:
    """agent 只通过 `call(name, args)` 触达工具。"""

    def __init__(self, store: DocStore | None = None, faults: Iterable[str] | None = None) -> None:
        self.store = store or DocStore()
        self.faults = FaultInjector(faults)
        self.calls: list[dict] = []

    # -- 参数校验 -----------------------------------------------------

    @staticmethod
    def _str_arg(args: dict, key: str, required: bool = True) -> str:
        value = args.get(key)
        if value is None:
            if required:
                raise ToolError("bad_args", f"缺少参数 {key}")
            return ""
        if not isinstance(value, str) or not value.strip():
            raise ToolError("bad_args", f"参数 {key} 必须是非空字符串")
        return value.strip()

    # -- 入口 ---------------------------------------------------------

    def call(self, name: str, args: dict | None) -> dict:
        args = args or {}
        handler = {
            "search_docs": self.search_docs,
            "read_chunk": self.read_chunk,
            "list_versions": self.list_versions,
        }.get(name)
        if handler is None:
            return obs_err(name or "unknown", "bad_args", f"未知工具：{name}")
        if not isinstance(args, dict):
            return obs_err(name, "bad_args", "args 必须是对象")
        self.calls.append({"tool": name, "args": args})
        injected = self.faults.take(name)
        if injected:
            return obs_err(name, injected, f"{name} 注入故障（{injected}），本次调用未执行")
        try:
            return handler(**args)
        except ToolError as exc:
            return obs_err(name, exc.code, exc.message)
        except TypeError as exc:
            return obs_err(name, "bad_args", f"参数不匹配：{exc}")
        except Exception as exc:  # pragma: no cover - 兜底，正常路径不该走到
            return obs_err(name, "internal_error", f"工具内部错误：{exc.__class__.__name__}")

    # -- 具体工具 -----------------------------------------------------

    def search_docs(
        self,
        query: str,
        top_k: int = 5,
        doc_type: str = "any",
        include_deprecated: bool = False,
    ) -> dict:
        query = self._str_arg({"query": query}, "query")
        if not isinstance(top_k, int) or not (VALID_TOP_K[0] <= top_k <= VALID_TOP_K[1]):
            raise ToolError("bad_args", f"top_k 必须是 {VALID_TOP_K[0]}~{VALID_TOP_K[1]} 的整数")
        if doc_type not in VALID_DOC_TYPES:
            raise ToolError("bad_args", f"doc_type 只能是 {sorted(VALID_DOC_TYPES)}")
        if not isinstance(include_deprecated, bool):
            raise ToolError("bad_args", "include_deprecated 必须是布尔值")

        chunks, scores, meta = self.store.search(query, top_k, doc_type, include_deprecated)
        data = [chunk.as_dict(score) for chunk, score in zip(chunks, scores)]
        hint = _conflict_hint(chunks)

        notes = [f"命中 {len(data)} 段"]
        if meta["filtered_deprecated"]:
            notes.append(f"已过滤 {meta['filtered_deprecated']} 段已废止文档")
        if data and scores[0] < LOW_RELEVANCE:
            notes.append(f"最高分 {scores[0]:.2f} 偏低，可能是无关命中，建议改写查询")
        if hint:
            notes.append(
                "线索：同一时间单位出现不同数值（"
                + " / ".join(f"{c['doc_id']}={c['value']}{hint['unit']}" for c in hint["candidates"])
                + "），是否属同一事项需自行核对"
            )
        return obs_ok("search_docs", data, "；".join(notes), {"conflict_hint": hint} if hint else None)

    def read_chunk(self, doc_id: str, chunk_id: str) -> dict:
        doc_id = self._str_arg({"doc_id": doc_id}, "doc_id")
        chunk_id = self._str_arg({"chunk_id": chunk_id}, "chunk_id")
        chunk = self.store.get_chunk(doc_id, chunk_id)
        return obs_ok(
            "read_chunk",
            chunk.as_dict(),
            f"读到 {chunk.chunk_id}（{chunk.heading or chunk.title}，{chunk.version_date}，{chunk.status}）",
        )

    def list_versions(self, topic: str) -> dict:
        topic = self._str_arg({"topic": topic}, "topic")
        keywords = [norm_text(t) for t in [topic, *expand_query(norm_text(topic))] if t]
        rows: list[dict] = []
        for doc_id in sorted(self.store.docs):
            doc = self.store.docs[doc_id]
            body = norm_text(doc.get("title", "") + "".join(c["text"] for c in doc["chunks"]))
            if not any(key in body for key in keywords):
                continue
            rows.append(
                {
                    "doc_id": doc_id,
                    "title": doc.get("title", ""),
                    "version_date": doc.get("version_date", ""),
                    "doc_type": doc.get("doc_type", ""),
                    "status": doc.get("status", "active"),
                    "chunk_ids": [c["chunk_id"] for c in doc["chunks"]],
                }
            )
        # 先按版本日期倒序，再稳定排序把已废止的沉到后面（最新且现行排最前）
        rows.sort(key=lambda row: row["version_date"], reverse=True)
        rows.sort(key=lambda row: row["status"] == "deprecated")
        note = f"共 {len(rows)} 篇相关文档" + ("（含已废止，需注意 status）" if any(r["status"] == "deprecated" for r in rows) else "")
        return obs_ok("list_versions", rows, note)


# ---------------------------------------------------------------- 给 trace 用的一句话摘要


def summarize_observation(observation: dict) -> str:
    tool = observation.get("tool", "?")
    if not observation.get("ok"):
        return f"error={observation.get('error_code')}：{observation.get('note', '')}"
    data = observation.get("data")
    note = observation.get("note", "")
    if tool == "search_docs":
        items = data or []
        if not items:
            return f"未命中任何片段；{note}".strip("；")
        hits = "、".join(f"{d['chunk_id']}({d.get('score', 0):.2f})" for d in items)
        # note 的第一段就是「命中 N 段」，这里避免重复
        rest = note.split("；", 1)[1] if "；" in note else ""
        return f"命中 {len(items)} 段：{hits}" + (f"；{rest}" if rest else "")
    if tool == "read_chunk" and isinstance(data, dict):
        return f"读到 {data['chunk_id']}（{data.get('heading') or data.get('title')}，{data.get('version_date')}，{data.get('status')}）"
    if tool == "list_versions" and isinstance(data, list):
        brief = "、".join(f"{r['doc_id']}({r['version_date']},{r['status']})" for r in data[:6])
        return f"返回 {len(data)} 篇：{brief}" + (f"；{note}" if note else "")
    return note or "已执行"
