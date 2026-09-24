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

Scope note: ``Event.detail`` *is* walked, keys and values, all the way
down: it is where events.py keeps what it reads from a line (retry and
spawn markers, prompt flags, capture-note codes, skill names), so an
accidental "just pass the raw value through" there would be a leak. The
other ``dict``-typed fields (``Diagnostics.ignored_line_types``,
``Classification.*_evidence``, …) are small counters keyed by closed
labels and are not walked key-by-key.

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

from claude_token_lens.model import Column, EventKind, Recommendation, Section, Table, TranscriptMeta
from claude_token_lens.parse import parse_transcript

from helpers import (
    assert_privacy,
    attachment_line,
    ignorable_line,
    system_line,
    tool_use_block,
    tool_result_block,
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
            elif f.name == "detail" and isinstance(value, dict):
                _walk_detail(value, violations, field_where)
            # other dict-typed fields intentionally not walked — see module docstring.


def _walk_detail(value, violations: list[str], where: str, key: str = "") -> None:
    """``Event.detail``, keys and values, at every depth."""
    if isinstance(value, str):
        if key not in _LONG_FIELD_ALLOWLIST and len(value) > _MAX_STR_LEN:
            violations.append(f"{where} exceeds {_MAX_STR_LEN} chars: {value!r}")
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and len(k) > _MAX_STR_LEN:
                violations.append(f"{where} key exceeds {_MAX_STR_LEN} chars: {k!r}")
            _walk_detail(v, violations, f"{where}[{k!r}]", k if isinstance(k, str) else key)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _walk_detail(item, violations, f"{where}[{i}]", key)


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


def test_privacy_a_malformed_skill_name_never_reaches_skills_invoked(tmp_path: Path):
    # SEC-P3: a Skill tool_use's own "skill" input is free text Claude
    # controls. This value is short and has no drive-letter/URL/@ shape,
    # so neither the length cap above nor assert_privacy's shape scan
    # would catch it on their own -- SKILL_NAME_PATTERN is the actual
    # gate (a space isn't in its allowed character set), and it must
    # keep a string like this out of Turn.skills_invoked altogether.
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Skill", "tu1", {"skill": "leak project codename"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].skills_invoked == ()
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
        # Parser-signals batch (SURV-4/5/6, PARSER_VERSION 19): the three
        # new line shapes this phase adds, in the same broad sweep.
        attachment_line("thinking_drop", newlyDropped={"reason": "prefix_mismatch", "blockCount": 2, "turnCount": 1}),
        attachment_line("task_status", status="completed", taskType="local_bash"),
        attachment_line("structured_output", data={"ok": True}),
        ignorable_line("cost-state", totalCostUSD=1.23, hasUnknownModelCost=False),
        turn_line(message_id="msg_2", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert len(result.events) >= 20
    _assert_no_violations(result)


# -- Parser-signals batch (SURV-4/5/6/7, PARSER_VERSION 19): dedicated ---
# -- fixtures for each new field/kind, per the phase brief's own          -
# -- "add privacy fixtures for each new field or kind" requirement.       -


def test_privacy_thinking_drop_never_carries_identifying_fields(tmp_path: Path):
    # newlyDropped can carry far more than reason/blockCount/turnCount in
    # the real corpus (request ids, model names, the actual dropped block
    # hashes/content) -- _thinking_drop_detail must only ever pick out the
    # three closed fields, never pass the rest through.
    lines = [
        attachment_line(
            "thinking_drop",
            newlyDropped={
                "reason": "prefix_mismatch",
                "blockCount": 3,
                "turnCount": 1,
                "first": "here is the actual thinking text that was dropped",
                "last": "and the final dropped block's text too",
                "clientChange": True,
                "blockHashes": ["deadbeef" * 8, "cafebabe" * 8],
                "requestId": "req_secret_12345",
                "querySource": "user-secret-source",
                "model": "claude-opus-5-secret-snapshot",
            },
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.subkind == "thinking_drop"]
    assert len(events) == 1
    detail = events[0].detail
    assert set(detail) <= {"reason", "blockCount", "turnCount"}
    assert detail["reason"] == "prefix_mismatch"
    forbidden = ("first", "last", "clientChange", "blockHashes", "requestId", "querySource", "model", "dropped text")
    for token in forbidden:
        assert token not in repr(detail)
    _assert_no_violations(result)


def test_privacy_thinking_drop_unknown_reason_becomes_other(tmp_path: Path):
    lines = [
        attachment_line("thinking_drop", newlyDropped={"reason": "some-new-internal-reason-code", "blockCount": 1}),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.subkind == "thinking_drop"]
    assert events[0].detail["reason"] == "other"
    assert "some-new-internal-reason-code" not in repr(events[0].detail)
    _assert_no_violations(result)


def test_privacy_task_status_never_reaches_description_or_paths(tmp_path: Path):
    # Mirrors test_privacy_a_malformed_skill_name_never_reaches_skills_invoked:
    # a task_status line carries plenty of identifying/free-text fields in
    # the real corpus (description, deltaSummary, outputFilePath, shell) --
    # _task_status_detail must only ever surface the two closed-vocabulary
    # words, never these.
    long_path = "C:\\Users\\paulm\\secret-project\\output\\result.json"
    lines = [
        attachment_line(
            "task_status",
            status="completed",
            taskType="local_bash",
            description="refactor the auth module to fix the login bug for jane.doe@acme.com",
            deltaSummary="changed 14 files, added retry logic",
            outputFilePath=long_path,
            shell="/bin/bash -c 'cat ~/.ssh/id_rsa'",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.kind == EventKind.TASK_STATUS]
    assert len(events) == 1
    detail = events[0].detail
    assert set(detail) <= {"status", "task_type"}
    assert detail == {"status": "completed", "task_type": "local_bash"}
    forbidden = ("refactor", "jane.doe", "acme.com", "changed 14 files", "Users", "paulm", "ssh", "id_rsa")
    for token in forbidden:
        assert token not in repr(detail)
    _assert_no_violations(result)


def test_privacy_task_status_unknown_words_become_other(tmp_path: Path):
    lines = [
        attachment_line("task_status", status="some-future-status", taskType="some-future-type"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.kind == EventKind.TASK_STATUS]
    assert events[0].detail == {"status": "other", "task_type": "other"}
    _assert_no_violations(result)


def test_privacy_structured_output_data_is_never_stored_only_its_size(tmp_path: Path):
    long_free_text = "this is the actual structured payload content " * 20
    lines = [
        attachment_line(
            "structured_output",
            data={"summary": long_free_text, "path": "C:\\Users\\paulm\\secret\\out.json"},
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.kind == EventKind.STRUCTURED_OUTPUT]
    assert len(events) == 1
    event = events[0]
    assert not event.detail
    assert event.size_chars is not None and event.size_chars > 0
    assert "structured payload content" not in repr(event)
    assert "Users" not in repr(event)
    _assert_no_violations(result)


def test_privacy_cost_state_total_is_always_a_plain_float(tmp_path: Path):
    # totalCostUSD is a line straight from Claude Code's own on-disk
    # transcript, not validated input -- a malicious/malformed value must
    # never end up stored verbatim (e.g. as a string) on TranscriptMeta.
    lines = [
        ignorable_line("cost-state", totalCostUSD="1.23; DROP TABLE users", hasUnknownModelCost=False),
        ignorable_line("cost-state", totalCostUSD=4.56, hasUnknownModelCost=True),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    meta = result.meta
    # The malformed first line is ignored (not int/float); the second,
    # well-formed line is the last one seen and wins.
    assert meta.cc_cost_usd == 4.56
    assert isinstance(meta.cc_cost_usd, float)
    assert meta.cc_cost_has_unknown_model is True
    assert "DROP TABLE" not in repr(meta)
    _assert_no_violations(result)


def test_privacy_unknown_line_type_is_sanitised_not_stored_verbatim(tmp_path: Path):
    malicious_type = "evil<script>alert(1)</script>" + ("x" * 100)
    lines = [
        ignorable_line(malicious_type),
        ignorable_line("also-not-a-real-type"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    unknown = result.parser_notes.get("unknown_line_types", {})
    assert malicious_type not in unknown
    assert "<script>" not in repr(result.parser_notes)
    # Bucketed to "other" (fails the closed token-pattern) rather than
    # dropped, so the count is still visible.
    assert unknown.get("other", 0) >= 1
    for key in unknown:
        assert len(key) <= 40
        assert "<" not in key and ">" not in key
    _assert_no_violations(result)


def test_privacy_image_and_document_blocks_never_leak_bytes_or_paths(tmp_path: Path):
    # A 1x1 PNG (valid header, tiny) plus a bogus/oversized "image" and a
    # document block -- content_block_size must only ever produce counts
    # (sized into size_chars, or bucketed into unsized_blocks), never the
    # base64 payload, decoded bytes, or any path-shaped string.
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Read", "tu1", {"file_path": "x"})],
        ),
        user_block_line(
            [
                tool_result_block(
                    "tu1",
                    [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": tiny_png_b64}},
                        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "not-real-pdf-bytes"}},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not-a-real-image"}},
                    ],
                )
            ]
        ),
        user_str_line(
            "look at this",
            message={
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": tiny_png_b64},
                    },
                ],
            },
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    blob = repr(result.parser_notes) + repr(result.events) + repr(result.tool_result_chars)
    assert tiny_png_b64 not in blob
    assert "not-real-pdf-bytes" not in blob
    assert "not-a-real-image" not in blob
    # Only counts, keyed by closed block-type labels.
    for counts in result.parser_notes.get("unsized_blocks", {}).values():
        assert isinstance(counts, int)
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


# -- R4: parse-time redaction must survive every renderer -----------------
#
# _redact_paths runs once, at parse time, on cmd_prefix/preceding_cmd_prefix
# (see parse.py's module docstring) — so the digest cache never stores the
# raw prefix and every renderer downstream (markdown/json/html/csv) only
# ever sees the already-redacted text. This is the fixture-driven,
# full-pipeline proof of that: a relative Windows path with no drive
# letter, an ssh user@host target, and an email address, none of which
# the pre-R4 regexes caught. Two of the five turns are engineered to trip
# RE-CACHE detection (big ctx, ~0 cache_read, 45 minutes after the turn
# that ran the sensitive command) so each command actually surfaces as a
# preceding_cmd_prefix in the recache section's top-command-prefix table
# — this exercises the redaction through a real rendered report, not just
# parse_transcript in isolation.


def test_redacted_commands_never_leak_through_any_renderer(tmp_path: Path):
    from claude_token_lens.config import Config
    from claude_token_lens.corpus import load_corpus
    from claude_token_lens.pricing import load_pricing
    from claude_token_lens.render.csv_out import write_csv_dir
    from claude_token_lens.render.html import render_html
    from claude_token_lens.render.json_out import render_json
    from claude_token_lens.render.markdown import render_markdown
    from claude_token_lens.report import build_report

    cmd1 = "cd Users\\paulm\\secret-repo && ssh deploy@internal-build-01.acme.local make"
    cmd2 = "git commit -m 'fix login for jane.doe@acme.com'"

    lines = [
        turn_line(message_id="msg1", timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=20),
        turn_line(
            message_id="msg2",
            timestamp="2026-09-18T12:01:00.000Z",
            input_tokens=100,
            output_tokens=20,
            content=[tool_use_block("Bash", "tu2", {"command": cmd1})],
        ),
        # 45-minute gap since msg2, big ctx, ~0 cache_read: a full-expiry
        # re-cache row whose preceding_cmd_prefix is msg2's (redacted)
        # command.
        turn_line(
            message_id="msg3",
            timestamp="2026-09-18T12:46:00.000Z",
            input_tokens=30000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=20,
        ),
        turn_line(
            message_id="msg4",
            timestamp="2026-09-18T12:47:00.000Z",
            input_tokens=100,
            output_tokens=20,
            content=[tool_use_block("Bash", "tu4", {"command": cmd2})],
        ),
        # Second 45-minute-gap full-expiry row, surfacing msg4's command.
        turn_line(
            message_id="msg5",
            timestamp="2026-09-18T13:32:00.000Z",
            input_tokens=30000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=20,
        ),
    ]
    project_dir = tmp_path / "proj-redact"
    project_dir.mkdir()
    write_jsonl(project_dir / "session-redact.jsonl", lines)

    corpus = load_corpus([project_dir])
    pricing = load_pricing()
    report = build_report(corpus, pricing, Config(), projects=("proj-redact",), window="w")

    # Sanity: the fixture actually tripped RE-CACHE detection, so the
    # commands really do reach a rendered table rather than sitting
    # unused in the corpus.
    recache_section = next(s for s in report.sections if s.key == "recache")
    summary_row = recache_section.tables[0].rows[0]
    assert summary_row[2] >= 2  # recache_turns

    assert_privacy(report)

    # "<user@host>" is _redact_paths's own redaction marker (see parse.py)
    # and legitimately contains "@" — stripped out (both raw and, for
    # HTML, html.escape'd as "&lt;user@host&gt;") before the "@" check so
    # the marker doesn't flag itself as the very leak it just fixed, while
    # a real, un-redacted "@" anywhere else in the output is still caught.
    # HTML's own <style>/<script> boilerplate (a dark-mode "@media" query,
    # the click-to-sort script) is never populated from report data, so
    # it's stripped too rather than tripping the same "@" check.
    forbidden = ("paulm", "acme", "@", "Users\\")

    def _assert_clean(name: str, text: str) -> None:
        scrubbed = text.replace("<user@host>", "").replace("&lt;user@host&gt;", "")
        if name == "html":
            scrubbed = re.sub(r"<style>.*?</style>", "", scrubbed, flags=re.DOTALL)
            scrubbed = re.sub(r"<script>.*?</script>", "", scrubbed, flags=re.DOTALL)
        for token in forbidden:
            assert token not in scrubbed, f"{name} output leaked {token!r}"

    rendered = {
        "markdown": render_markdown(report),
        "json": render_json(report),
        "html": render_html(report),
    }
    for name, text in rendered.items():
        _assert_clean(name, text)

    csv_dir = tmp_path / "csv-out"
    write_csv_dir(report, csv_dir)
    csv_text = "\n".join(p.read_text(encoding="utf-8") for p in csv_dir.rglob("*.csv"))
    _assert_clean("csv", csv_text)
