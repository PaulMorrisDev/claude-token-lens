"""``config.py``'s ``[capture]`` table: loading and validating it,
:func:`set_capture` (presets, one-by-one metrics, the on/off stamps, the
change log) and the atomic, table-keeping ``config.toml`` writes it
relies on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_token_lens import capture_catalogue
from claude_token_lens.config import (
    CAPTURE_LOG_NAME,
    CaptureConfig,
    ConfigError,
    load_capture_log,
    load_config,
    set_capture,
    write_config_values,
)

NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)


def _write(tmp_path, text: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")


def test_capture_is_off_by_default(tmp_path):
    capture = load_config(config_dir=tmp_path).capture
    assert capture == CaptureConfig()
    assert not capture.is_on and capture.active_metrics() == ()


def test_a_capture_table_is_loaded(tmp_path):
    _write(tmp_path, """
[capture]
level = "custom"
metrics = ["task", "fit"]
sample = 25
until = "2026-10-01"
projects = ["claude-token-lens", "!secret"]
feedback = ["feedback_skill", "feedback_note"]
coaching = ["coaching_line"]
enabled_at = "2026-09-24T06:00:00+00:00"
""")
    capture = load_config(config_dir=tmp_path).capture
    assert capture.level == "custom" and capture.sample == 25
    assert capture.active_metrics() == ("task", "result", "fit", "feedback_skill", "feedback_note")
    assert capture.projects == ["claude-token-lens", "!secret"]
    assert not capture.expired(NOW)
    assert capture.expired(datetime(2026, 10, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("body, message", [
    ('capture = "on"', "'capture' must be a table"),
    ('[capture]\nlevel = "max"', "'capture.level'"),
    ('[capture]\nsample = 30', "'capture.sample'"),
    ('[capture]\nsample = true', "'capture.sample'"),
    ('[capture]\nmetrics = ["task", "mood"]', "'mood'"),
    ('[capture]\nmetrics = ["coaching_line"]', "'coaching_line'"),
    ('[capture]\nuntil = "next week"', "'capture.until'"),
    ('[capture]\nprojects = ["("]', "bad pattern"),
    ('[capture]\nfeedback = ["survey"]', "'survey'"),
    ('[capture]\ncoaching = "yes"', "must be a list"),
])
def test_a_bad_capture_table_is_named(tmp_path, body, message):
    _write(tmp_path, body)
    with pytest.raises(ConfigError, match=message):
        load_config(config_dir=tmp_path)


def test_turning_capture_on_stamps_it_and_logs_the_change(tmp_path):
    capture = set_capture(tmp_path, level="essentials", now=NOW)
    assert capture.level == "essentials" and capture.enabled_at == "2026-09-24T06:00:00+00:00"
    assert load_config(config_dir=tmp_path).capture == capture
    log = load_capture_log(tmp_path)
    assert log == [{
        "ts": "2026-09-24T06:00:00+00:00",
        "level": "essentials",
        "changed": {"level": {"from": "off", "to": "essentials"}},
    }]


def test_a_change_that_changes_nothing_is_not_logged(tmp_path):
    set_capture(tmp_path, level="standard", now=NOW)
    set_capture(tmp_path, level="standard", now=NOW)
    assert len(load_capture_log(tmp_path)) == 1


def test_raising_the_level_keeps_the_first_on_stamp(tmp_path):
    set_capture(tmp_path, level="essentials", now=NOW)
    later = set_capture(tmp_path, level="deep", now=datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert later.enabled_at == "2026-09-24T06:00:00+00:00"


def test_metrics_one_by_one_become_custom_or_the_preset_they_match(tmp_path):
    custom = set_capture(tmp_path, metrics=["task", "rules"], now=NOW)
    assert custom.level == "custom" and custom.metrics == ["task", "result", "rules"]
    preset = set_capture(tmp_path, metrics=list(capture_catalogue.level_metrics("standard")), now=NOW)
    assert preset.level == "standard" and preset.metrics == []
    assert set_capture(tmp_path, metrics=[], now=NOW).level == "off"


def test_turning_capture_off_clears_the_stamp_and_the_end_date(tmp_path):
    set_capture(tmp_path, level="essentials", until="2026-10-01T00:00:00+00:00", now=NOW)
    off = set_capture(tmp_path, level="off", now=NOW)
    assert (off.level, off.enabled_at, off.until) == ("off", "", "")


def test_set_capture_keeps_the_rest_of_config_toml(tmp_path):
    _write(tmp_path, 'billing = "subscription"\n\n[savers]\nnames = ["rtk"]\n')
    set_capture(tmp_path, level="essentials", sample=50, feedback=["feedback_note", "feedback_skill"], now=NOW)
    config = load_config(config_dir=tmp_path)
    assert config.savers == ["rtk"]
    assert config.capture.sample == 50
    assert config.capture.feedback == ["feedback_skill", "feedback_note"]  # catalogue order


def test_set_capture_rejects_bad_values_before_writing(tmp_path):
    with pytest.raises(ConfigError):
        set_capture(tmp_path, metrics=["mood"], now=NOW)
    with pytest.raises(ConfigError):
        set_capture(tmp_path, level="essentials", sample=33, now=NOW)
    with pytest.raises(ConfigError):
        set_capture(tmp_path, level="essentials", coaching=["nagging"], now=NOW)
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / CAPTURE_LOG_NAME).exists()


def test_set_capture_refuses_when_config_toml_cannot_be_rewritten_in_place(tmp_path):
    _write(tmp_path, '[thresholds]\nnested = { too = "deep" }\n')
    with pytest.raises(ConfigError, match="config.toml.new"):
        set_capture(tmp_path, level="essentials", now=NOW)
    assert "[capture]" not in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert not (tmp_path / CAPTURE_LOG_NAME).exists()


def test_the_dot_new_fallback_keeps_flat_tables(tmp_path):
    _write(tmp_path, '[savers]\nnames = ["rtk"]\n')
    path = write_config_values(tmp_path, {"capture": {"level": "free"}, "thresholds": {"nested": {"too": "deep"}}})
    assert path.name == "config.toml.new"
    text = path.read_text(encoding="utf-8")
    assert '[savers]\nnames = ["rtk"]' in text
    assert '[capture]\nlevel = "free"' in text
    assert "nested" not in text


def test_config_writes_leave_no_temporary_files(tmp_path):
    set_capture(tmp_path, level="essentials", now=NOW)
    set_capture(tmp_path, level="off", now=NOW)
    assert sorted(p.name for p in tmp_path.iterdir()) == [CAPTURE_LOG_NAME, "config.toml"]


def test_an_unreadable_log_line_is_skipped(tmp_path):
    set_capture(tmp_path, level="essentials", now=NOW)
    with open(tmp_path / CAPTURE_LOG_NAME, "a", encoding="utf-8") as handle:
        handle.write("not json\n[1, 2]\n")
    assert len(load_capture_log(tmp_path)) == 1
    assert load_capture_log(tmp_path / "missing") == []


def test_describe_mentions_capture_only_when_on(tmp_path):
    assert not any(line.startswith("capture") for line in load_config(config_dir=tmp_path).describe())
    set_capture(tmp_path, level="deep", sample=10, now=NOW)
    assert "capture: deep, 10% of sessions" in load_config(config_dir=tmp_path).describe()
