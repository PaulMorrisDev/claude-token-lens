"""Context files: how often each CLAUDE.md-family file and skill is sent,
to whom, and what carrying it costs (``context_files.ContextFileStats``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_token_lens import context_files, parse
from claude_token_lens.model import TranscriptMeta
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing

from helpers import attachment_line, tool_use_block, turn_line, write_jsonl

SALT = b"c" * 32


@pytest.fixture(autouse=True)
def _salt(monkeypatch):
    monkeypatch.setattr(parse, "_SALT", SALT)


def _stamp(line: dict, minute: int) -> dict:
    line["timestamp"] = f"2026-09-18T12:{minute:02d}:00.000Z"
    return line


def _transcript(tmp_path: Path, name: str, *, agent_type: str | None = None, skill_call: bool = False):
    lines = [
        _stamp(
            attachment_line(
                "instructions",
                files=[{"path": "C:/Users/u/.claude/CLAUDE.md", "type": "User", "content": "u" * 400}],
            ),
            0,
        ),
        _stamp(
            attachment_line(
                "skill_listing",
                content="- grill-me: Interview the user.\n- pdf: Read PDFs.\n",
                skillCount=2,
                names=["grill-me", "pdf"],
            ),
            0,
        ),
    ]
    for minute in (1, 2, 3):
        content = [{"type": "text", "text": "ok"}]
        if skill_call and minute == 2:
            content = [tool_use_block("Skill", "toolu_1", {"skill": "grill-me"})]
        lines.append(_stamp(turn_line(content=content, cache_read_input_tokens=1000), minute))
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), agent_type=agent_type))


def test_counts_sends_and_reach_per_file_without_paths(tmp_path):
    stats = context_files.ContextFileStats()
    pricing = load_pricing()
    stats.add(_transcript(tmp_path, "main", skill_call=True), pricing, is_main=True)
    stats.add(_transcript(tmp_path, "sub", agent_type="Explore"), pricing, is_main=False)
    data = stats.to_dict()

    assert data["transcripts"] == {"Explore": 1, "main": 1}
    [row] = data["files"]
    assert row["hash"] == parse.path_hash("C:/Users/u/.claude/CLAUDE.md", SALT)
    assert row["tokens"] == 100
    assert row["sends"] == 2
    assert row["reach"] == {"Explore": 1, "main": 1}
    assert row["cost_usd"] > 0
    assert set(row["cost_by_reach"]) == {"Explore", "main"}
    assert "Users" not in repr(data) and "Interview" not in repr(data)


def test_skills_record_listing_and_use(tmp_path):
    stats = context_files.ContextFileStats()
    pricing = load_pricing()
    stats.add(_transcript(tmp_path, "main", skill_call=True), pricing, is_main=True)
    stats.add(_transcript(tmp_path, "sub", agent_type="Explore"), pricing, is_main=False)
    skills = {row["name"]: row for row in stats.to_dict()["skills"]}

    assert skills["grill-me"]["listed"] == {"Explore": 1, "main": 1}
    assert skills["grill-me"]["invoked"] == 1
    assert skills["grill-me"]["invoked_by"] == {"main": 1}
    assert skills["pdf"]["invoked"] == 0
    assert skills["pdf"]["listing_tokens"] == round(len("- pdf: Read PDFs.") / 4)
    assert skills["pdf"]["listing_cost_usd"] > 0


def test_carrying_costs_nothing_without_pricing(tmp_path):
    stats = context_files.ContextFileStats()
    stats.add(_transcript(tmp_path, "main"), None, is_main=True)
    data = stats.to_dict()
    assert data["files"][0]["cost_usd"] == 0
    assert data["files"][0]["sends"] == 1
