"""CLI 入口测试：参数解析、JSON 输出、多轮输入文件、产物落盘。"""

from __future__ import annotations

import json
from pathlib import Path

from src.cli import main

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "tests" / "cli_out"


def test_cli_json_conflict(capsys) -> None:
    code = main(["--question", "报销期限多久？", "--corpus", "conflict_min", "--model", "mock", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "conflict_pending"
    assert len(data["citations"]) == 2


def test_cli_markdown_output(capsys) -> None:
    code = main(["--question", "年假可以攒到明年吗？", "--model", "mock"])
    assert code == 0
    text = capsys.readouterr().out
    assert "# 年假可以攒到明年吗？" in text
    assert "D03#c2" in text


def test_cli_input_file_with_history(capsys) -> None:
    code = main(["--input", str(ROOT / "examples" / "case_a5_followup" / "input.json"), "--model", "mock", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "answered"
    assert data["citations"][0]["chunk_id"] == "D04#c3"


def test_cli_writes_out_dir(capsys) -> None:
    if OUT.exists():
        for path in OUT.rglob("*"):
            if path.is_file():
                path.unlink()
    code = main(
        [
            "--question",
            "报销期限多久？",
            "--corpus",
            "conflict_min",
            "--model",
            "mock",
            "--out-dir",
            str(OUT),
            "--json",
        ]
    )
    assert code == 0
    capsys.readouterr()
    for name in ("output.json", "output.md", "trace.jsonl"):
        assert (OUT / name).is_file(), name


def test_cli_requires_a_question(capsys) -> None:
    code = main(["--model", "mock"])
    assert code == 2
    assert "缺少问题" in capsys.readouterr().out
