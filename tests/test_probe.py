"""``probe.py``: the content-free schema histogram (plan "Risks and gaps"
item 4). Exercises ``probe_line``/``probe_file``/``probe_paths``/
``render_probe`` directly against crafted JSON objects and JSONL files --
never through the real parser, since the whole point of ``probe`` is to
stay useful against a line shape ``parse.py`` would reject.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_token_lens import probe

from helpers import write_jsonl


def test_probe_line_counts_line_type_and_keys():
    result = probe.ProbeResult()
    probe.probe_line({"type": "assistant", "message": {}, "uuid": "u1"}, result)
    assert result.line_types["assistant"] == 1
    assert result.keys_by_type["assistant"]["type"] == 1
    assert result.keys_by_type["assistant"]["message"] == 1
    assert result.keys_by_type["assistant"]["uuid"] == 1


def test_probe_line_missing_type_is_counted_separately():
    result = probe.ProbeResult()
    probe.probe_line({"foo": "bar"}, result)
    assert result.line_types[probe._MISSING_TYPE] == 1


def test_probe_line_records_attachment_type():
    result = probe.ProbeResult()
    probe.probe_line({"type": "attachment", "attachment": {"type": "hook_success"}}, result)
    assert result.attachment_types["hook_success"] == 1


def test_probe_line_ignores_attachment_type_when_not_attachment_line():
    result = probe.ProbeResult()
    probe.probe_line({"type": "user", "attachment": {"type": "hook_success"}}, result)
    assert result.attachment_types == {}


def test_probe_line_records_system_subtype():
    result = probe.ProbeResult()
    probe.probe_line({"type": "system", "subtype": "compact_boundary"}, result)
    assert result.system_subtypes["compact_boundary"] == 1


def test_probe_line_records_version_field_as_string():
    result = probe.ProbeResult()
    probe.probe_line({"type": "assistant", "version": "1.2.3"}, result)
    assert result.version_values["1.2.3"] == 1


def test_probe_line_records_numeric_version_field():
    result = probe.ProbeResult()
    probe.probe_line({"type": "assistant", "version": 7}, result)
    assert result.version_values["7"] == 1


def test_probe_line_ignores_boolean_version_field():
    # bool is a subclass of int; must not be recorded as a version number.
    result = probe.ProbeResult()
    probe.probe_line({"type": "assistant", "version": True}, result)
    assert result.version_values == {}


def test_clip_never_exceeds_max_value_chars():
    long_value = "x" * 500
    result = probe.ProbeResult()
    probe.probe_line({"type": "attachment", "attachment": {"type": long_value}}, result)
    (recorded,) = result.attachment_types.keys()
    assert len(recorded) <= probe.MAX_VALUE_CHARS


def test_clip_leaves_short_values_untouched():
    result = probe.ProbeResult()
    probe.probe_line({"type": "system", "subtype": "compact_boundary"}, result)
    (recorded,) = result.system_subtypes.keys()
    assert recorded == "compact_boundary"


# -- file-level: unparsable / truncated lines -------------------------------


def test_probe_file_counts_unparsable_lines(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"type": "assistant"}\nnot json at all\n{"type": "user"}\n', encoding="utf-8")
    result = probe.probe_file(path)
    assert result.lines == 3
    assert result.unparsable_lines == 1
    assert result.line_types["assistant"] == 1
    assert result.line_types["user"] == 1


def test_probe_file_tolerates_a_truncated_final_line(tmp_path):
    # A live session file being appended to mid-write: the last line has
    # no trailing newline and is itself incomplete JSON.
    path = tmp_path / "live.jsonl"
    path.write_text('{"type": "assistant"}\n{"type": "user", "message": {"rol', encoding="utf-8")
    result = probe.probe_file(path)
    assert result.line_types["assistant"] == 1
    assert result.unparsable_lines == 1


def test_probe_file_counts_non_object_json_as_unparsable(tmp_path):
    path = tmp_path / "weird.jsonl"
    path.write_text('[1, 2, 3]\n"just a string"\n', encoding="utf-8")
    result = probe.probe_file(path)
    assert result.unparsable_lines == 2


def test_probe_paths_combines_multiple_files(tmp_path):
    write_jsonl(tmp_path / "a.jsonl", [{"type": "assistant"}])
    write_jsonl(tmp_path / "b.jsonl", [{"type": "user"}, {"type": "user"}])
    result = probe.probe_paths([tmp_path / "a.jsonl", tmp_path / "b.jsonl"])
    assert result.files == 2
    assert result.lines == 3
    assert result.line_types["assistant"] == 1
    assert result.line_types["user"] == 2


# -- render_probe -------------------------------------------------------


def test_render_probe_contains_expected_headings():
    result = probe.ProbeResult()
    probe.probe_line({"type": "assistant"}, result)
    text = probe.render_probe(result)
    assert "# claude-token-lens probe" in text
    assert "## line types" in text
    assert "## keys by line type" in text
    assert "## attachment types" in text
    assert "## system subtypes" in text
    assert "## version field values" in text


def test_render_probe_never_emits_a_token_longer_than_64_chars():
    """Every recorded field is clipped to <= 64 chars (probe.py's
    MAX_VALUE_CHARS), so the rendered report -- pasteable into a public
    bug report -- can never carry a message-shaped string through
    regardless of what a pathological transcript contains.
    """
    result = probe.ProbeResult()
    probe.probe_line(
        {
            "type": "attachment",
            "attachment": {"type": "y" * 300},
            "some_very_long_key_name_" * 5: "irrelevant",
            "version": "z" * 300,
        },
        result,
    )
    probe.probe_line({"type": "system", "subtype": "s" * 300}, result)
    text = probe.render_probe(result)
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        token = line[2:].rsplit(": ", 1)[0]
        assert len(token) <= probe.MAX_VALUE_CHARS, (line, len(token))


def test_render_probe_says_none_for_empty_sections():
    result = probe.ProbeResult()
    probe.probe_line({"type": "assistant"}, result)
    text = probe.render_probe(result)
    assert "(none)" in text  # no attachments/system lines/version fields seen
