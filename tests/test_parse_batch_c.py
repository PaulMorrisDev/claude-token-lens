"""Batch C additive fields (model.py/parse.py): ``Turn.tool_use_ids``,
``TranscriptMeta.provider``/``entrypoint``/``claude_version`` first-seen
capture. See model.py's module docstring for the full field list.

Each test exercises the behaviour through a full ``parse_transcript``
pass over a small synthetic fixture, matching this codebase's own
convention (see test_parse_attribution.py).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from claude_token_lens.model import TranscriptMeta
from claude_token_lens.parse import detect_provider, parse_transcript

from helpers import assert_privacy, tool_use_block, turn_line, user_str_line, write_jsonl


# -- Turn.tool_use_ids -----------------------------------------------------


def test_tool_use_ids_collected_in_encounter_order(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[
                tool_use_block("Read", "tu_a", {"file_path": "x.py"}),
                tool_use_block("Bash", "tu_b", {"command": "ls"}),
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].tool_use_ids == ("tu_a", "tu_b")


def test_tool_use_ids_empty_when_turn_has_no_tool_use_blocks(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].tool_use_ids == ()


def test_tool_use_ids_union_across_lines_sharing_one_message_id(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", content=[tool_use_block("Read", "tu_a", {"file_path": "x.py"})]),
        turn_line(message_id="msg_1", content=[tool_use_block("Bash", "tu_b", {"command": "ls"})]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert len(result.turns) == 1
    assert result.turns[0].tool_use_ids == ("tu_a", "tu_b")


# -- detect_provider ---------------------------------------------------------


def test_detect_provider_bedrock_forms():
    assert detect_provider("anthropic.claude-sonnet-5-20260101-v1:0") == "bedrock"
    assert detect_provider("us.anthropic.claude-sonnet-5-20260101-v1:0") == "bedrock"
    assert detect_provider("some-custom-id-v1:0") == "bedrock"


def test_detect_provider_vertex_form():
    assert detect_provider("claude-sonnet-5@20260101") == "vertex"


def test_detect_provider_default_is_anthropic():
    assert detect_provider("claude-sonnet-5") == "anthropic"


def test_detect_provider_empty_is_none():
    assert detect_provider(None) is None
    assert detect_provider("") is None


# -- TranscriptMeta.provider (derived from the first turn with a model) ----


def test_provider_derived_from_first_turn_model(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", model="us.anthropic.claude-sonnet-5-v1:0")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.provider == "bedrock"


def test_provider_defaults_to_anthropic_for_ordinary_model_id(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", model="claude-sonnet-5")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.provider == "anthropic"


def test_provider_from_turns_overrides_meta_supplied_value(tmp_path: Path):
    # A subagent's load_meta-derived guess (from its .meta.json model
    # alias) is superseded by the transcript's own turn once parsed.
    lines = [turn_line(message_id="msg_1", model="claude-sonnet-5@20260101")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    meta = TranscriptMeta(path=str(path), provider="anthropic")
    result = parse_transcript(path, meta)
    assert result.meta.provider == "vertex"
    assert meta.provider == "anthropic"  # input object never mutated


def test_provider_stays_none_when_no_turn_has_a_model(tmp_path: Path):
    lines = [user_str_line("hi", origin={"kind": "human"})]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.provider is None


# -- TranscriptMeta.entrypoint / claude_version (first-seen) ----------------


def test_entrypoint_and_claude_version_captured_from_first_line(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", entrypoint="cli", version="2.1.0"),
        turn_line(message_id="msg_2", entrypoint="sdk-python", version="2.1.1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.entrypoint == "cli"
    assert result.meta.claude_version == "2.1.0"


def test_entrypoint_captured_from_non_assistant_line(tmp_path: Path):
    lines = [
        user_str_line("hi", origin={"kind": "human"}, entrypoint="sdk-typescript", version="3.0.0"),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.entrypoint == "sdk-typescript"
    assert result.meta.claude_version == "3.0.0"


def test_entrypoint_and_claude_version_default_to_none_when_absent(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.entrypoint is None
    assert result.meta.claude_version is None


def test_caller_supplied_entrypoint_and_version_are_not_overwritten(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", entrypoint="cli", version="9.9.9")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    meta = TranscriptMeta(path=str(path), entrypoint="sdk-python", claude_version="1.0.0")
    result = parse_transcript(path, meta)
    assert result.meta.entrypoint == "sdk-python"
    assert result.meta.claude_version == "1.0.0"


def test_parse_transcript_never_mutates_the_input_meta_object(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", entrypoint="cli", version="1.0.0", model="claude-sonnet-5@20260101")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    meta = TranscriptMeta(path=str(path))
    result = parse_transcript(path, meta)
    assert meta.entrypoint is None
    assert meta.claude_version is None
    assert meta.provider is None
    assert result.meta is not meta
    assert result.meta.entrypoint == "cli"
    assert result.meta.claude_version == "1.0.0"
    assert result.meta.provider == "vertex"


def test_batch_c_fields_pass_privacy_scan(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            entrypoint="cli",
            version="1.0.0",
            model="us.anthropic.claude-sonnet-5-v1:0",
            content=[tool_use_block("Bash", "tu_a", {"command": "cd /c/Dev/x && pytest"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert_privacy(result)
