"""CLI wiring tests (WP10c): the argparse skeleton (``--version``, the
default-subcommand insertion rule, the ``init``/``baseline``/``serve``
stubs) plus real end-to-end tests for every wired subcommand, against
synthetic projects under ``tmp_path`` built with ``tests/helpers``.

Every test passes ``--projects-root``/``--project`` explicitly rather
than relying on the autouse ``CLAUDE_CONFIG_DIR``/``HOME`` isolation
fixture's fake home directory, so a test's fixtures live wherever
``tmp_path`` puts them regardless of what that fixture points at --
except the cache/snapshot/log-usage tests, which need a real
``--config-dir`` and use ``tmp_path`` for that too.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

import pytest

from claude_token_lens import __version__, cli

from helpers import turn_line, write_jsonl

#: Subcommands with no real implementation yet (v0.2/v0.3 milestones).
STUB_SUBCOMMANDS = ("init", "baseline", "serve")

_GENERATED_AT_RE = re.compile(r"- Generated at:.*")


def _strip_generated_at(text: str) -> str:
    """Blank out the one line that legitimately differs between two
    otherwise-identical report runs (``ReportMeta.generated_at`` is
    ``datetime.now()`` at render time), so a byte-identical-output
    assertion isn't flaky across a wall-clock second boundary.
    """
    return _GENERATED_AT_RE.sub("- Generated at: STRIPPED", text)


def _write_project(root: Path, slug: str, n_turns: int = 3, age_seconds: int | None = None) -> Path:
    """Write one synthetic session under ``<root>/<slug>/session-1.jsonl``.

    ``age_seconds``, when given, backdates the file's mtime by that many
    seconds -- needed to get the digest cache to actually treat the file
    as cacheable (``cache.py``: a file younger than 60 seconds is always
    a live-session cache miss).
    """
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / "session-1.jsonl"
    write_jsonl(
        path,
        [
            turn_line(input_tokens=100 + i, output_tokens=20 + i, cache_read_input_tokens=30)
            for i in range(n_turns)
        ],
    )
    if age_seconds is not None:
        old = path.stat().st_mtime - age_seconds
        os.utime(path, (old, old))
    return project_dir


# -- version / default-subcommand skeleton -----------------------------------


def test_version_exits_zero_and_prints_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_no_argv_inserts_default_subcommand():
    assert cli._insert_default_subcommand([]) == ["report"]


def test_unrecognised_leading_token_inserts_default_subcommand():
    # "--verbose" is a global flag, not a subcommand, so "report" is
    # inserted ahead of it.
    assert cli._insert_default_subcommand(["--verbose"]) == ["report", "--verbose"]


def test_known_subcommand_is_left_alone():
    argv = ["sessions", "--days", "7"]
    assert cli._insert_default_subcommand(argv) == argv


def test_default_subcommand_constant_is_report():
    assert cli.DEFAULT_SUBCOMMAND == "report"


# -- --group-by choices (Fix R6) ---------------------------------------------


def test_group_by_choices_match_classify_group_keys():
    import argparse

    from claude_token_lens import classify

    parser = cli._make_parser()
    subparsers_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    report_parser = subparsers_action.choices["report"]
    group_by_action = next(a for a in report_parser._actions if a.dest == "group_by")
    assert tuple(group_by_action.choices) == tuple(sorted(classify._GROUP_KEYS))


def test_group_by_invalid_choice_exits_2_with_one_line_message(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--group-by", "profile"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    # argparse's own usage banner precedes the error line -- just check
    # the actual error line is the expected single one-line message,
    # not that the whole stderr output is short.
    error_lines = [line for line in err.splitlines() if "invalid choice" in line]
    assert len(error_lines) == 1
    assert "profile" in error_lines[0]


def test_group_by_accepts_entrypoint(tmp_path, capsys):
    # "entrypoint" is a real classify._GROUP_KEYS member that the old
    # hand-maintained choices tuple omitted entirely.
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--group-by", "entrypoint", "--json"]
    )
    assert exit_code == 0
    capsys.readouterr()


@pytest.mark.parametrize("command", STUB_SUBCOMMANDS)
def test_planned_stub_exits_2(command, capsys):
    exit_code = cli.main([command])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "planned for v0." in err
    assert command in err


def test_no_argv_with_no_data_exits_1(capsys):
    # The autouse fixture points HOME/CLAUDE_CONFIG_DIR at an empty tmp
    # dir, so the default "report" subcommand's default project (this
    # process's own cwd slug) matches nothing.
    exit_code = cli.main([])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "report" in err


# -- pricing-check / snapshot-config (unchanged from WP2/WP7) ---------------


def test_pricing_check_exits_0(capsys):
    exit_code = cli.main(["pricing-check"])
    assert exit_code == 0
    capsys.readouterr()


def test_snapshot_config_print_hook_exits_0(capsys):
    exit_code = cli.main(["snapshot-config", "--print-hook"])
    assert exit_code == 0
    capsys.readouterr()


# -- report / sessions / recache / ttl / compactions -------------------------


def test_report_renders_markdown(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith("# Claude token lens report")
    assert "## Overview" in out
    assert "## Diagnostics" in out


def test_allow_titles_flag_was_removed(capsys):
    # Fix R17: --allow-titles implied a privacy control that never
    # existed (report.py's own docstring says the keyword it still
    # accepts is a permanent no-op -- nothing captures title text to
    # gate) -- it must no longer be a recognised CLI flag at all.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--allow-titles"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "--allow-titles" in err


# -- --quiet / --verbose (Fix R21) ------------------------------------------


def test_quiet_and_verbose_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--quiet", "--verbose"])
    assert exc_info.value.code == 2


def test_verbose_prints_corpus_stats_to_stderr(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--verbose"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "[corpus]" in err


def test_default_verbosity_omits_corpus_stats(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "[corpus]" not in err


def test_load_corpus_for_args_quiet_suppresses_stats_even_if_verbose_is_also_set(tmp_path, capsys):
    # Fix R21: --quiet was accepted by argparse but never actually
    # consulted anywhere in the code -- a silent no-op. argparse's own
    # mutual-exclusion check keeps a human from passing both flags at
    # once (covered above), but _load_corpus_for_args's own contract
    # must not lean on that alone: build a Namespace with both set (as
    # a caller bypassing argparse could) and confirm --quiet still wins.
    root = tmp_path / "projects"
    project_dir = _write_project(root, "proj-a")
    config = cli.load_config(None)
    args = argparse.Namespace(
        no_cache=True,
        rebuild_cache=False,
        days=None,
        since=None,
        until=None,
        limit=None,
        window_by="mtime",
        jobs=1,
        verbose=True,
        quiet=True,
    )
    cli._load_corpus_for_args(args, config, root, [project_dir])
    err = capsys.readouterr().err
    assert "[corpus]" not in err


@pytest.mark.parametrize("command,section_title", [("sessions", "## Sessions"), ("recache", "## Re-cache"), ("ttl", "## TTL"), ("compactions", "## Compactions")])
def test_focused_subcommands_render_overview_plus_their_own_section(tmp_path, capsys, command, section_title):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main([command, "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    section_keys = [s["key"] for s in payload["report"]["sections"]]
    assert "overview" in section_keys
    assert cli._REPORT_LIKE_SECTIONS[command] in section_keys
    # No other report section should have leaked in via `include`.
    assert set(section_keys) == {"overview", cli._REPORT_LIKE_SECTIONS[command]}


def test_report_json_has_expected_top_level_keys(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload.keys()) >= {"schema_version", "tool_version", "report"}
    report = payload["report"]
    assert set(report.keys()) >= {"meta", "sections", "recommendations", "diagnostics"}
    assert isinstance(report["sections"], list) and report["sections"]


def test_report_csv_dir_writes_one_csv_per_table_plus_index(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    csv_dir = tmp_path / "csvs"
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--csv-dir", str(csv_dir)]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert (csv_dir / "_index.csv").exists()
    assert (csv_dir / "_meta.csv").exists()
    assert any(csv_dir.glob("overview__*.csv"))


def test_report_html_writes_a_file_with_no_external_references(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    html_path = tmp_path / "report.html"
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--html", str(html_path)]
    )
    assert exit_code == 0
    capsys.readouterr()
    html = html_path.read_text(encoding="utf-8")
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src" not in html


def test_report_phases_flag_adds_phases_section(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--phases", "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    section_keys = [s["key"] for s in payload["report"]["sections"]]
    assert "phases" in section_keys


def test_report_without_phases_flag_omits_phases_section(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    section_keys = [s["key"] for s in payload["report"]["sections"]]
    assert "phases" not in section_keys


def test_empty_window_exits_1_with_reason_naming_root(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "does-not-exist"]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert str(root) in err


def test_bad_pricing_path_exits_2(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    missing_pricing = tmp_path / "no-such-pricing.toml"
    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--pricing",
            str(missing_pricing),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "pricing" in err.lower()


def test_jobs_1_and_jobs_2_give_byte_identical_markdown(tmp_path, capsys):
    root = tmp_path / "projects"
    for i in range(3):
        _write_project(root, f"proj-{i}")

    outputs = []
    for jobs in (1, 2):
        exit_code = cli.main(
            [
                "report",
                "--projects-root",
                str(root),
                "--all-projects",
                "--jobs",
                str(jobs),
                "--no-cache",
            ]
        )
        assert exit_code == 0
        outputs.append(_strip_generated_at(capsys.readouterr().out))

    assert outputs[0] == outputs[1]


def test_no_cache_and_warm_cache_give_byte_identical_output(tmp_path, capsys):
    root = tmp_path / "projects"
    # Backdate the file so the digest cache doesn't treat it as a live
    # session (cache.py: mtime < 60s is always a miss).
    _write_project(root, "proj-a", age_seconds=120)
    config_dir = tmp_path / "token-lens"

    base_args = [
        "report",
        "--projects-root",
        str(root),
        "--project",
        "proj-a",
        "--config-dir",
        str(config_dir),
    ]

    exit_code = cli.main(base_args + ["--no-cache"])
    assert exit_code == 0
    no_cache_output = _strip_generated_at(capsys.readouterr().out)

    # Cold cache: config_dir/cache/ doesn't exist yet, this writes it.
    exit_code = cli.main(base_args)
    assert exit_code == 0
    cold_output = _strip_generated_at(capsys.readouterr().out)

    # Warm cache: every entry from the previous run is now a hit.
    exit_code = cli.main(base_args)
    assert exit_code == 0
    warm_output = _strip_generated_at(capsys.readouterr().out)

    assert no_cache_output == cold_output
    assert cold_output == warm_output


# -- _load_snapshots_for_config_dir (Fix R16) --------------------------


def _write_snapshot(path: Path, ts: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": ts, "user_settings": {}}), encoding="utf-8")


def test_load_snapshots_for_config_dir_finds_the_documented_parent_shape(tmp_path):
    config_dir = tmp_path / "root" / "token-lens"
    _write_snapshot(config_dir.parent / "token-lens" / "snapshots" / "a.json", "20200101T000000Z")
    found = cli._load_snapshots_for_config_dir(config_dir)
    assert [s.ts for s in found] == ["20200101T000000Z"]


def test_load_snapshots_for_config_dir_falls_back_to_treating_it_as_the_hook_base(tmp_path):
    config_dir = tmp_path / "my-custom-settings"
    _write_snapshot(config_dir / "token-lens" / "snapshots" / "a.json", "20200101T000000Z")
    # config_dir.parent (tmp_path) has no "token-lens/snapshots" under
    # it -- only the config_dir-as-base fallback finds this one.
    found = cli._load_snapshots_for_config_dir(config_dir)
    assert [s.ts for s in found] == ["20200101T000000Z"]


def test_load_snapshots_for_config_dir_prefers_parent_shape_when_both_exist(tmp_path):
    config_dir = tmp_path / "root" / "token-lens"
    _write_snapshot(config_dir.parent / "token-lens" / "snapshots" / "parent.json", "20200101T000000Z")
    _write_snapshot(config_dir / "token-lens" / "snapshots" / "base.json", "20210101T000000Z")
    found = cli._load_snapshots_for_config_dir(config_dir)
    assert [s.ts for s in found] == ["20200101T000000Z"]


# -- config-diff --------------------------------------------------------


def test_config_diff_renders_a_table(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)

    config_dir = tmp_path / "claude-home" / ".claude"
    snapshots_dir = config_dir / "token-lens" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    (snapshots_dir / "20200101T000000Z.json").write_text(
        json.dumps({"ts": "20200101T000000Z", "user_settings": {"model": "sonnet"}}),
        encoding="utf-8",
    )
    (snapshots_dir / "20200201T000000Z.json").write_text(
        json.dumps({"ts": "20200201T000000Z", "user_settings": {"model": "fable"}}),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "config-diff",
            "--projects-root",
            str(root),
            "--project",
            "proj-diff",
            "--config-dir",
            str(config_dir / "token-lens"),
            "--key",
            "user_settings.model",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Config diff: user_settings.model" in out
    assert "Sessions" in out


def test_config_diff_finds_snapshots_for_a_custom_config_dir_used_as_hook_base(tmp_path, capsys):
    """Fix R16: snapshots.load_snapshots() always appends
    "token-lens/snapshots" to whatever base it's given, and
    _resolve_config_dir() returns an explicit --config-dir as-is (no
    "token-lens" appended) -- so config_dir.parent only recovers the
    hook's actual write location when --config-dir happens to end in
    "token-lens" (as in test_config_diff_renders_a_table above). A user
    who instead points the *same* --config-dir value at both
    `snapshot-config` and `config-diff` -- the natural thing to try --
    gets snapshots written under <that-dir>/token-lens/snapshots
    (hooks/snapshot-config.py's resolve_config_dir treats an explicit
    --config-dir as the base). This must also resolve correctly.
    """
    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)

    config_dir = tmp_path / "my-custom-settings"
    snapshots_dir = config_dir / "token-lens" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    (snapshots_dir / "20200101T000000Z.json").write_text(
        json.dumps({"ts": "20200101T000000Z", "user_settings": {"model": "sonnet"}}),
        encoding="utf-8",
    )
    (snapshots_dir / "20200201T000000Z.json").write_text(
        json.dumps({"ts": "20200201T000000Z", "user_settings": {"model": "fable"}}),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "config-diff",
            "--projects-root",
            str(root),
            "--project",
            "proj-diff",
            "--config-dir",
            str(config_dir),
            "--key",
            "user_settings.model",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Config diff: user_settings.model" in out
    assert "Sessions" in out


def test_config_diff_honors_session_overrides(tmp_path, capsys, monkeypatch):
    """Fix R18: _build_session_metrics used to hardcode {} for
    classify.classify_session's overrides argument, so a manual
    sessions.toml mode/purpose correction -- honoured by every other
    report-like subcommand via _cmd_report_like's own
    load_session_overrides(config_dir) -- was silently dropped for
    config-diff alone. Spy on classify.classify_session to confirm
    config-diff now actually loads and threads sessions.toml through.
    """
    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)

    config_dir = tmp_path / "token-lens"
    config_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir = config_dir.parent / "token-lens" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    (snapshots_dir / "20200101T000000Z.json").write_text(
        json.dumps({"ts": "20200101T000000Z", "user_settings": {"model": "sonnet"}}),
        encoding="utf-8",
    )
    (snapshots_dir / "20200201T000000Z.json").write_text(
        json.dumps({"ts": "20200201T000000Z", "user_settings": {"model": "fable"}}),
        encoding="utf-8",
    )
    (config_dir / "sessions.toml").write_text(
        '[sessions."some-session-id"]\nmode = "auto"\n', encoding="utf-8"
    )

    seen_overrides: list[dict] = []
    real_classify_session = cli.classify.classify_session

    def _spy(top, subs, overrides, tz, **kwargs):
        seen_overrides.append(overrides)
        return real_classify_session(top, subs, overrides, tz, **kwargs)

    monkeypatch.setattr(cli.classify, "classify_session", _spy)

    exit_code = cli.main(
        [
            "config-diff",
            "--projects-root",
            str(root),
            "--project",
            "proj-diff",
            "--config-dir",
            str(config_dir),
            "--key",
            "user_settings.model",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert seen_overrides, "classify_session was never called"
    assert all(o == {"some-session-id": {"mode": "auto"}} for o in seen_overrides)


def test_config_diff_requires_key_or_auto_keys(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["config-diff", "--projects-root", str(root), "--project", "proj-a"])
    assert exc_info.value.code == 2


# -- ScorecardError surfaced as a clean exit-2 error (Fix R20) --------------


def test_report_exits_2_with_clean_message_on_misordered_scorecard_thresholds(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        "[thresholds.scorecard]\n"
        "cache_recache_share_pct = [50.0, 30.0, 15.0, 5.0]\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--config-dir",
            str(config_dir),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "cache_recache_share_pct" in lines[0]
    assert "Traceback" not in err


# -- --tz (Fix R24) ----------------------------------------------------------


def test_tz_flag_overrides_config_toml_for_this_run(tmp_path, capsys, monkeypatch):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('tz = "UTC"\n', encoding="utf-8")

    seen_tz: list[str | None] = []
    real_classify_session = cli.classify.classify_session

    def _spy(top, subs, overrides, tz, **kwargs):
        seen_tz.append(tz)
        return real_classify_session(top, subs, overrides, tz, **kwargs)

    monkeypatch.setattr(cli.classify, "classify_session", _spy)

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--config-dir",
            str(config_dir),
            "--tz",
            "America/New_York",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert seen_tz, "classify_session was never called"
    assert all(tz == "America/New_York" for tz in seen_tz)


def test_tz_flag_defaults_to_config_toml_value_when_absent(tmp_path, capsys, monkeypatch):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    config_dir = tmp_path / "token-lens"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('tz = "UTC"\n', encoding="utf-8")

    seen_tz: list[str | None] = []
    real_classify_session = cli.classify.classify_session

    def _spy(top, subs, overrides, tz, **kwargs):
        seen_tz.append(tz)
        return real_classify_session(top, subs, overrides, tz, **kwargs)

    monkeypatch.setattr(cli.classify, "classify_session", _spy)

    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--config-dir", str(config_dir)]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert seen_tz and all(tz == "UTC" for tz in seen_tz)


@pytest.mark.skipif(
    not zoneinfo.available_timezones(),
    reason="no tz database on this machine (see classify.py's module docstring) -- "
    "--tz can't be validated against anything here, so it's accepted uncontested",
)
def test_tz_flag_rejects_an_unknown_zone_with_a_clean_exit_2(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--tz",
            "Not/A_Real_Zone",
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "Not/A_Real_Zone" in lines[0]
    assert "Traceback" not in err


def test_tz_flag_is_accepted_uncontested_when_machine_has_no_tz_database(tmp_path, capsys, monkeypatch):
    # The inverse of the skipped test above: force the "no tz database"
    # branch regardless of what this machine actually has, and confirm
    # a clearly-bogus zone name is still accepted (degrading later to
    # local time inside classify._to_local, exactly like an unresolvable
    # config.toml value already does) rather than rejected.
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    monkeypatch.setattr(cli, "available_timezones", lambda: frozenset())

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--tz",
            "Not/A_Real_Zone",
        ]
    )
    assert exit_code == 0


# -- probe ----------------------------------------------------------------


def test_probe_command_output_has_no_string_longer_than_64_chars(tmp_path, capsys):
    root = tmp_path / "projects"
    project_dir = root / "proj-probe"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-1.jsonl",
        [
            turn_line(),
            {"type": "attachment", "attachment": {"type": "y" * 200}, "uuid": "u1"},
            {"type": "system", "subtype": "s" * 200, "uuid": "u2"},
        ],
    )
    exit_code = cli.main(["probe", "--projects-root", str(root), "--project", "proj-probe"])
    assert exit_code == 0
    out = capsys.readouterr().out
    for line in out.splitlines():
        if not line.startswith("- "):
            continue
        token = line[2:].rsplit(": ", 1)[0]
        assert len(token) <= 64, (line, len(token))


def test_probe_single_file(tmp_path, capsys):
    path = tmp_path / "one.jsonl"
    write_jsonl(path, [turn_line(), turn_line()])
    exit_code = cli.main(["probe", "--file", str(path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "files: 1" in out
    assert "assistant: 2" in out


def test_probe_missing_file_exits_2(tmp_path, capsys):
    exit_code = cli.main(["probe", "--file", str(tmp_path / "missing.jsonl")])
    assert exit_code == 2


# -- real fixture (skipped if absent) ---------------------------------------


_REAL_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "real"

pytestmark_real_fixture = pytest.mark.skipif(
    not (_REAL_FIXTURE_ROOT / "session-a").exists()
    or not any((_REAL_FIXTURE_ROOT / "session-a").glob("*.jsonl")),
    reason="tests/fixtures/real/session-a/ not present (real fixture not checked out)",
)


@pytestmark_real_fixture
def test_report_against_the_real_fixture_exits_0_with_nonempty_sections(capsys):
    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(_REAL_FIXTURE_ROOT),
            "--project",
            "session-a",
        ]
    )
    assert exit_code == 0
    markdown = capsys.readouterr().out
    assert "## Overview" in markdown
    assert "## Sessions" in markdown
    assert "## Re-cache events" in markdown
    assert "## Cache TTL break-even" in markdown

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(_REAL_FIXTURE_ROOT),
            "--project",
            "session-a",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["sections"]


# -- python -m entry point / zipapp exit-code propagation -------------------


def test_python_dash_m_version_exits_0():
    result = subprocess.run(
        [sys.executable, "-m", "claude_token_lens", "--version"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent / "src"),
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_python_dash_m_unimplemented_subcommand_exits_2():
    result = subprocess.run(
        [sys.executable, "-m", "claude_token_lens", "serve"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent / "src"),
    )
    assert result.returncode == 2
    assert "serve" in result.stderr
