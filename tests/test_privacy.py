"""Hard privacy rule (project plan, Architecture section; model.py module
docstring): no dataclass field ever holds message text, tool_result
content, a full file path, or a command longer than 40 characters.

This test walks every dataclass field produced by ``parse_transcript``
(and the ``discovery.load_meta``/``TranscriptMeta`` it's paired with)
across a battery of synthetic fixtures exercising every EventKind, every
system subtype, real-looking tool_use/tool_result content, and long
human-prompt text — and asserts no ``str`` field exceeds 64 characters
outside the documented allowlist, and ``cmd_prefix``/
``preceding_cmd_prefix`` never exceed 40.

Scope note: ``Event.detail`` and the other ``dict``-typed fields
(``Diagnostics.ignored_line_types``, ``Classification.*_evidence``, …)
are deliberately not walked key-by-key here — they're free-form small
counters the module controls, not a place message text could leak
through structurally. What this test guards is every *named, typed*
``str``/``str | None`` dataclass field, which is where an accidental
"just pass the raw value through" bug would actually show up.

Independent-review follow-up (task 6): every fixture below is also run
through ``helpers.assert_privacy``, a second, shape-based scan (not
length-based) that asserts no field matches a Windows drive path
(``C:\\``), a POSIX ``/home/`` path, a Windows ``\\Users\\`` path, an
MSYS/Git Bash drive path (``/c/...``), or a bare ``@`` — the concrete
regressions a length cap alone wouldn't catch (e.g. a short absolute
path under 64 chars).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from claude_token_lens.model import Column, Recommendation, Section, Table, TranscriptMeta
from claude_token_lens.parse import parse_transcript

from helpers import (
    assert_privacy,
    attachment_line,
    system_line,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

_MAX_STR_LEN = 64
_MAX_CMD_PREFIX_LEN = 40

#: Field names allowed to exceed 64 characters (plan Architecture
#: section's own allowlist, spelled out per WP1's brief): agent identity
#: strings, tool names, session/slug identifiers, attachment subkinds,
#: and the transcript file path itself.
_LONG_FIELD_ALLOWLIST = {
    "agent_type",
    "model",
    "tool_names",  # tuple[str, ...] — each element checked individually
    "session_id",
    "slug",
    "subkind",  # attachment.type values
    "path",  # TranscriptMeta.path only
}

_CMD_PREFIX_FIELDS = {"cmd_prefix", "preceding_cmd_prefix"}


def _walk(obj, violations: list[str], where: str) -> None:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            field_where = f"{where}.{f.name}"
            if f.name in _CMD_PREFIX_FIELDS:
                if isinstance(value, str) and len(value) > _MAX_CMD_PREFIX_LEN:
                    violations.append(f"{field_where} exceeds {_MAX_CMD_PREFIX_LEN} chars: {value!r}")
                continue
            if isinstance(value, str):
                if f.name not in _LONG_FIELD_ALLOWLIST and len(value) > _MAX_STR_LEN:
                    violations.append(f"{field_where} exceeds {_MAX_STR_LEN} chars: {value!r}")
            elif isinstance(value, (tuple, list)):
                for i, item in enumerate(value):
                    if isinstance(item, str):
                        if f.name not in _LONG_FIELD_ALLOWLIST and len(item) > _MAX_STR_LEN:
                            violations.append(
                                f"{field_where}[{i}] exceeds {_MAX_STR_LEN} chars: {item!r}"
                            )
                    else:
                        _walk(item, violations, f"{field_where}[{i}]")
            elif dataclasses.is_dataclass(value):
                _walk(value, violations, field_where)
            # dict-typed fields intentionally not walked — see module docstring.


def _assert_no_violations(result) -> None:
    violations: list[str] = []
    _walk(result.meta, violations, "meta")
    _walk(result.diagnostics, violations, "diagnostics")
    for i, turn in enumerate(result.turns):
        _walk(turn, violations, f"turns[{i}]")
    for i, event in enumerate(result.events):
        _walk(event, violations, f"events[{i}]")
    assert violations == []
    # Independent-review follow-up (task 6): every fixture below also
    # goes through the absolute-path/username regex scan, not just the
    # length-based walk above. helpers.assert_privacy is the reusable
    # form of this same scan for later packages.
    assert_privacy(result)


def test_privacy_long_human_prompt_is_never_stored(tmp_path: Path):
    long_prompt = "please refactor this function to handle the edge case " * 20  # > 64 chars
    lines = [
        user_str_line(long_prompt, origin={"kind": "human"}),
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    _assert_no_violations(result)


def test_privacy_long_bash_command_is_capped_at_40_chars(tmp_path: Path):
    long_command = "echo " + ("x" * 200)
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Bash", "tu1", {"command": long_command})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].cmd_prefix is not None
    assert len(result.turns[0].cmd_prefix) <= _MAX_CMD_PREFIX_LEN
    _assert_no_violations(result)


def test_privacy_long_file_path_is_never_stored_only_edit_kind(tmp_path: Path):
    long_path = "C:/Users/paulm/very/deeply/nested/project/src/module/" + ("sub/" * 20) + "file.py"
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Write", "tu1", {"file_path": long_path, "content": "..."})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].edit_kind == "real"
    _assert_no_violations(result)


_MSYS_DRIVE_RE = re.compile(r"/[a-z]/")


def test_privacy_msys_drive_path_is_never_stored(tmp_path: Path):
    # Git Bash on Windows renders "C:\Dev\x" as "/c/Dev/x"; that shape
    # must be redacted out of cmd_prefix just like the C:\ form is.
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Bash", "tu1", {"command": "cd /c/Dev/secret_project && pytest"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].cmd_prefix == "cd <path> && pytest"
    assert not _MSYS_DRIVE_RE.search(result.turns[0].cmd_prefix)
    _assert_no_violations(result)


def test_privacy_tool_result_text_is_never_stored(tmp_path: Path):
    long_result_text = "here is a large file dump " * 500
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Read", "tu1", {"file_path": "C:/x.py"})],
        ),
        user_block_line([{"type": "tool_result", "tool_use_id": "tu1", "content": long_result_text}]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.tool_result_chars["Read"] == len(long_result_text)
    _assert_no_violations(result)


def test_privacy_mcp_instructions_delta_text_is_never_stored(tmp_path: Path):
    huge_instructions = "## some-mcp-server\nlots of instruction text here " * 200
    lines = [
        attachment_line(
            "mcp_instructions_delta",
            addedNames=["some-mcp-server"],
            addedBlocks=[huge_instructions],
            removedNames=[],
        ),
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    _assert_no_violations(result)
    for event in result.events:
        assert "instruction text" not in repr(event.detail)


def test_privacy_every_event_kind_fixture(tmp_path: Path):
    """Broad sweep: one line per EventKind, all through the same walk."""
    lines = [
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
        user_str_line("a human prompt " * 10, origin={"kind": "human"}),
        user_str_line("<command-name>review</command-name>"),
        user_str_line("<scheduled-task>nightly</scheduled-task>"),
        user_str_line("[Request interrupted by user]"),
        user_str_line("(notification)", origin={"kind": "task-notification"}),
        user_str_line("hey", origin={"kind": "peer"}),
        user_str_line("(denied)", toolDenialKind="user-rejected"),
        user_str_line("[Image #1]", isMeta=True),
        user_block_line([{"type": "tool_result", "tool_use_id": "tu-x", "content": "ok " * 50}]),
        system_line("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 1000}),
        system_line("api_error", error={"status": 529}),
        system_line("model_refusal_fallback", originalModel="claude-opus-5", fallbackModel="claude-sonnet-5"),
        system_line("local_command"),
        system_line("stop_hook_summary"),
        attachment_line("hook_success", rendered="x" * 300),
        attachment_line("model", identity={"modelId": "claude-sonnet-5"}),
        attachment_line("thinking_stripped", scope="session"),
        attachment_line("deferred_tools_delta", addedNames=["Read", "Grep"], removedNames=[]),
        attachment_line("total_tokens_reminder"),
        attachment_line("environment", rendered="y" * 500),
        attachment_line("queued_command"),
        attachment_line("an_unclassified_future_type"),
        turn_line(message_id="msg_2", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert len(result.events) >= 20
    _assert_no_violations(result)


# -- Independent-review item 3: assert_privacy inspects table cells and --
# -- sections, not just TranscriptResult's own dataclass fields. ---------


def test_assert_privacy_flags_a_windows_path_in_a_table_cell():
    # The earlier assert_privacy only recursed into a list item when the
    # item was itself a dataclass, so Table.rows (list[list[str | int]])
    # was never actually scanned - a leaking cell passed silently.
    table = Table(
        name="workstyle_by_hour",
        title="Workstyle by hour",
        columns=[Column(key="hour", label="Hour", kind="int"), Column(key="note", label="Note")],
        rows=[[9, "normal"], [14, "cwd=C:\\Users\\paulm\\secret_project"]],
    )
    try:
        assert_privacy(table)
    except AssertionError as exc:
        assert any("Users" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking table cell")


def test_assert_privacy_walks_section_tables_and_notes():
    leaking_table = Table(
        name="t",
        title="T",
        columns=[Column(key="k", label="K")],
        rows=[["cd /home/paulm/project && ls"]],
    )
    section = Section(key="workstyle", title="Workstyle", tables=[leaking_table], notes=["fine note"])
    try:
        assert_privacy(section)
    except AssertionError as exc:
        assert any("/home/" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking nested table")

    clean_section = Section(key="workstyle", title="Workstyle", tables=[], notes=["nothing to see here"])
    assert_privacy(clean_section)  # no raise


def test_assert_privacy_walks_recommendation_evidence_tuples():
    rec = Recommendation(
        id="r1",
        title="Move off ad-hoc paths",
        action="Stop hardcoding paths",
        evidence=[("cmd_prefix", "cd C:\\Users\\paulm\\proj", "workstyle_by_hour", "14")],
    )
    try:
        assert_privacy(rec)
    except AssertionError as exc:
        assert any("Users" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking evidence tuple")


def test_assert_privacy_flags_url_in_table_cell():
    table = Table(
        name="t",
        title="T",
        columns=[Column(key="k", label="K")],
        rows=[["curl https://x.example/a"]],
    )
    try:
        assert_privacy(table)
    except AssertionError as exc:
        assert any("URL" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the URL in the table cell")


def test_assert_privacy_accepts_a_plain_dict():
    assert_privacy({"count": 3, "label": "fine"})
    try:
        assert_privacy({"cmd": "cd C:\\Users\\paulm\\proj"})
    except AssertionError as exc:
        assert any("Users" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking dict value")
