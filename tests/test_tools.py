"""检索 / 读取 / 版本反查 / 故障注入的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools import DocStore, Tools, summarize_observation

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "docs"
REGRESSION = ROOT / "data" / "regression"


@pytest.fixture()
def store() -> DocStore:
    return DocStore(corpus="full")


def test_corpus_subset_limits_documents() -> None:
    conflict_only = DocStore(corpus="conflict_min")
    assert conflict_only.doc_ids() == ["D01", "D02"]
    assert DocStore(corpus="full").doc_ids() == ["D01", "D02", "D02b", "D03", "D04", "D05", "D06", "D07", "D08"]


def test_search_hits_both_conflicting_chunks() -> None:
    tools = Tools(DocStore(corpus="conflict_min"))
    obs = tools.search_docs("报销期限多久？")
    assert obs["ok"] is True
    chunk_ids = [row["chunk_id"] for row in obs["data"]]
    assert "D01#c2" in chunk_ids
    assert "D02#c2" in chunk_ids
    hint = obs["hints"]["conflict_hint"]
    assert hint["values"] == ["15", "30"]
    assert {c["doc_id"] for c in hint["candidates"]} == {"D01", "D02"}


def test_search_filters_deprecated_by_default(store: DocStore) -> None:
    tools = Tools(store)
    default = tools.search_docs("纸质发票 报销单打印")
    assert default["ok"] is True
    assert all(row["doc_id"] != "D08" for row in default["data"])
    assert "已过滤" in default["note"]

    included = tools.search_docs("纸质发票 报销单打印", include_deprecated=True)
    assert any(row["doc_id"] == "D08" for row in included["data"])


def test_search_rewrite_expands_mouth_terms(store: DocStore) -> None:
    """口语「打车」应能改写命中「市内交通费」。"""
    tools = Tools(store)
    obs = tools.search_docs("出差打车一天最多能报多少？")
    assert obs["ok"] is True
    assert "D02#c4" in [row["chunk_id"] for row in obs["data"]]


def test_search_empty_result_is_not_an_error(store: DocStore) -> None:
    tools = Tools(store)
    obs = tools.search_docs("公司明年会裁员吗？", doc_type="faq")
    assert obs["ok"] is True
    assert obs["data"] == [] or all(row["score"] < 0.3 for row in obs["data"])


@pytest.mark.parametrize(
    ("args", "expected_note"),
    [
        ({"query": "  "}, "query"),
        ({"query": "报销", "top_k": 0}, "top_k"),
        ({"query": "报销", "doc_type": "video"}, "doc_type"),
        ({"query": "报销", "top_k": "5"}, "top_k"),
    ],
)
def test_search_rejects_bad_args(store: DocStore, args: dict, expected_note: str) -> None:
    obs = Tools(store).call("search_docs", args)
    assert obs["ok"] is False
    assert obs["error_code"] == "bad_args"
    assert expected_note in obs["note"]


def test_read_chunk_ok_and_not_found(store: DocStore) -> None:
    tools = Tools(store)
    ok = tools.read_chunk("D01", "D01#c2")
    assert ok["ok"] is True
    assert "30 天" in ok["data"]["text"]

    missing = tools.call("read_chunk", {"doc_id": "D01", "chunk_id": "D01#c99"})
    assert missing["ok"] is False
    assert missing["error_code"] == "not_found"

    wrong_doc = tools.call("read_chunk", {"doc_id": "D09", "chunk_id": "D01#c2"})
    assert wrong_doc["ok"] is False
    assert wrong_doc["error_code"] == "not_found"

    unknown_tool = tools.call("delete_everything", {})
    assert unknown_tool["error_code"] == "bad_args"


def test_fault_injection_is_consumed(store: DocStore) -> None:
    tools = Tools(store, faults=["search_docs:timeout:1"])
    first = tools.call("search_docs", {"query": "报销期限多久？"})
    second = tools.call("search_docs", {"query": "报销期限多久？"})
    assert first["ok"] is False and first["error_code"] == "timeout"
    assert second["ok"] is True
    assert [h["error_code"] for h in tools.faults.history] == ["timeout"]


def test_persistent_fault_never_recovers(store: DocStore) -> None:
    tools = Tools(store, faults=["search_docs:internal_error"])
    for _ in range(3):
        assert tools.call("search_docs", {"query": "报销期限多久？"})["error_code"] == "internal_error"


def test_list_versions_marks_deprecated(store: DocStore) -> None:
    obs = Tools(store).list_versions("报销")
    assert obs["ok"] is True
    rows = {row["doc_id"]: row for row in obs["data"]}
    assert rows["D08"]["status"] == "deprecated"
    assert rows["D01"]["status"] == "active"
    # 现行文档排在已废止之前
    statuses = [row["status"] for row in obs["data"]]
    assert statuses.index("active") < statuses.index("deprecated")


def test_docs_dir_override_changes_retrieval() -> None:
    """内容替换夹具：后给的目录按 doc_id 覆盖前面的。"""
    base = DocStore(docs_dirs=[DOCS], corpus="conflict_min")
    swapped = DocStore(docs_dirs=[DOCS, REGRESSION / "d01_45days"], corpus="conflict_min")
    assert "30 天" in base.get_chunk("D01", "D01#c2").text
    assert "45 天" in swapped.get_chunk("D01", "D01#c2").text
    assert swapped.overridden == ["D01"]

    obs = Tools(swapped).search_docs("报销期限多久？")
    assert obs["hints"]["conflict_hint"]["values"] == ["15", "45"]


def test_conflict_scope_chunks_are_attached(store: DocStore) -> None:
    """命中冲突线索时，检索结果直接附带各来源的适用范围片段（省掉模型的额外查证步）。"""
    obs = Tools(store).search_docs("报销期限多久？")
    scope = [row for row in obs["data"] if row.get("via") == "conflict_scope"]
    scope_chunks = {row["chunk_id"] for row in scope}
    # D01#c1 本身就在前 5 条命中里，所以只要求另外两篇的适用范围片段被补进来
    assert {"D02#c1", "D02b#c1"} <= scope_chunks
    assert "D01#c1" in {row["chunk_id"] for row in obs["data"]}
    assert "已附上各冲突来源的适用范围片段" in obs["note"]


def test_summarize_observation_is_one_line(store: DocStore) -> None:
    tools = Tools(store)
    ok = summarize_observation(tools.search_docs("报销期限多久？"))
    assert "D01#c2" in ok and "\n" not in ok

    tools = Tools(store, faults=["search_docs:timeout:1"])
    bad = summarize_observation(tools.call("search_docs", {"query": "报销期限多久？"}))
    assert "timeout" in bad
