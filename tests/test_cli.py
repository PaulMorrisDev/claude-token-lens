"""CLI wiring tests (WP10c): the argparse skeleton (``--version``, the
default-subcommand insertion rule, the ``init``/``baseline`` stubs) plus
real end-to-end tests for every wired subcommand, against synthetic
projects under ``tmp_path`` built with ``tests/helpers``.

``serve`` (S1-api) is no longer a stub -- ``tests/test_service_api.py``
and ``tests/test_service_egress.py`` exercise it directly (including
``cli.main(["serve", "--once", ...])``), so it was removed from
``STUB_SUBCOMMANDS`` below and from the "unimplemented subcommand exits
2" subprocess smoke test, which now names ``init`` instead -- the same
testing intent (an unimplemented subcommand's ``python -m`` invocation
exits 2) without hanging on ``serve``'s ``serve_forever()``.

Every test passes ``--projects-root``/``--project`` explicitly rather
than relying on the autouse ``CLAUDE_CONFIG_DIR``/``HOME`` isolation
fixture's fake home directory, so a test's fixtures live wherever
``tmp_path`` puts them regardless of what that fixture points at --
except the cache/snapshot/log-usage tests, which need a real
``--config-dir`` and use ``tmp_path`` for that too.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

import pytest

from claude_token_lens import __version__, cli

from helpers import assert_privacy, turn_line, write_jsonl

#: Subcommands with no real implementation yet (v0.3 milestone). "serve"
#: (v0.2) is wired for real as of S1-api -- see module docstring.
STUB_SUBCOMMANDS = ("init", "baseline")

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


@pytest.mark.parametrize("command", STUB_SUBCOMMANDS)
def test_stub_subcommand_help_text_is_marked_planned(command, capsys):
    # Fix R25: init/baseline/serve's --help listing used to read as
    # "planned for v0.3" prose with no visual marker distinguishing a
    # stub from a real subcommand at a glance in the full listing;
    # confirm the top-level --help output now leads each with the same
    # "(planned)" tag the generic not-implemented-yet fallback uses.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if command in line]
    assert lines, f"{command} not found in --help output"
    assert any("(planned)" in line for line in lines)


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


# -- snapshot-config --project-dir / probe-config (schema 2) ----------------
#
# --project-dir (not --project) is deliberate: the common parser already
# defines a repeatable --project meaning "a project slug to filter a
# report by" (see _build_common_parser); a single directory-path override
# for these two subcommands needed its own name to avoid a silent
# argparse option-string collision (both flags are otherwise attached to
# every subcommand via parents=[common]).


def test_snapshot_config_project_dir_runs_the_hook_for_an_explicit_directory(tmp_path, capsys):
    project = tmp_path / "some-project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"model": "opus"}), encoding="utf-8"
    )
    config_dir = tmp_path / "token-lens"

    exit_code = cli.main(
        [
            "snapshot-config",
            "--config-dir",
            str(config_dir),
            "--project-dir",
            str(project),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    snapshot_path = Path(out.strip())
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["project_settings"]
    project_entry = next(iter(snapshot["project_settings"].values()))
    assert project_entry["model"] == "opus"


def test_snapshot_config_without_project_dir_uses_cwd(tmp_path, capsys, monkeypatch):
    project = tmp_path / "cwd-project"
    project.mkdir(parents=True)
    config_dir = tmp_path / "token-lens"
    monkeypatch.chdir(project)

    exit_code = cli.main(["snapshot-config", "--config-dir", str(config_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    snapshot = json.loads(Path(out.strip()).read_text(encoding="utf-8"))
    # cwd_hash is a hash, not the raw path -- just confirm a snapshot was
    # actually produced for *some* cwd rather than failing outright.
    assert snapshot["cwd_hash"].startswith("sha256:")


def test_probe_config_renders_markdown_with_no_raw_paths(tmp_path, capsys):
    project = tmp_path / "probe-project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"model": "sonnet", "effortLevel": "high"}), encoding="utf-8"
    )
    config_dir = tmp_path / "token-lens"

    exit_code = cli.main(
        [
            "probe-config",
            "--config-dir",
            str(config_dir),
            "--project-dir",
            str(project),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out

    assert out.startswith("# Config probe:")
    assert "## Settings layers" in out
    assert "## Effective config" in out
    assert "| model | sonnet |" in out
    assert "project_shared" in out  # the layer name, not a raw path

    assert_privacy(out)
    # Never write a snapshot file to disk -- probe-config only prints.
    assert not (config_dir / "snapshots").exists()


def test_probe_config_defaults_to_the_current_directory(tmp_path, capsys, monkeypatch):
    project = tmp_path / "cwd-probe-project"
    project.mkdir(parents=True)
    config_dir = tmp_path / "token-lens"
    monkeypatch.chdir(project)

    exit_code = cli.main(["probe-config", "--config-dir", str(config_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith("# Config probe:")


def test_probe_config_no_effective_keys_notes_it(tmp_path, capsys):
    project = tmp_path / "empty-project"
    project.mkdir(parents=True)
    config_dir = tmp_path / "token-lens"

    exit_code = cli.main(
        ["probe-config", "--config-dir", str(config_dir), "--project-dir", str(project)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no settings layer defines any allowlisted key" in out


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


def _force_patch_set_text(monkeypatch, text: str = "--- a/settings.json\n+++ b/settings.json\n") -> None:
    """Force ``recommend.render_patch_set`` to return ``text`` regardless
    of whether the tiny synthetic fixture actually earns any
    recommendations -- the patch-set-placement tests below care about
    *where* the text lands for each output mode, not about which
    recommendation rules fire.
    """
    from claude_token_lens import recommend

    monkeypatch.setattr(recommend, "render_patch_set", lambda recs: text)


def test_report_json_patch_set_is_embedded_as_a_json_key_not_appended(tmp_path, capsys, monkeypatch):
    # Fix cli/patch-set-json: `--json --patch-set` used to print the
    # patch-set text as trailing lines after the JSON blob, so
    # `json.loads` on stdout would raise. It must now be valid JSON
    # with the patch set embedded under a top-level "patch_set" key,
    # and nothing else printed to stdout.
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--json", "--patch-set"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # would raise if anything trailed the JSON blob
    assert payload["patch_set"] == patch_text


def test_report_json_without_patch_set_flag_omits_the_key(tmp_path, capsys, monkeypatch):
    _force_patch_set_text(monkeypatch)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "patch_set" not in payload


def test_report_html_patch_set_writes_a_sibling_file(tmp_path, capsys, monkeypatch):
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    html_path = tmp_path / "out" / "report.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--html", str(html_path), "--patch-set"]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert html_path.exists()
    assert (html_path.parent / "patch-set.txt").read_text(encoding="utf-8") == patch_text


def test_report_csv_dir_patch_set_writes_a_file_in_the_dir(tmp_path, capsys, monkeypatch):
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    csv_dir = tmp_path / "csvs"
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--csv-dir", str(csv_dir), "--patch-set"]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert (csv_dir / "patch-set.txt").read_text(encoding="utf-8") == patch_text


def test_report_markdown_patch_set_keeps_appending_to_stdout(tmp_path, capsys, monkeypatch):
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--patch-set"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.rstrip("\n").endswith(patch_text.rstrip("\n"))


def test_report_patch_set_flag_with_no_recommendations_prints_nothing_extra(tmp_path, capsys, monkeypatch):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")

    baseline_exit = cli.main(["report", "--projects-root", str(root), "--project", "proj-a"])
    assert baseline_exit == 0
    baseline_out = _strip_generated_at(capsys.readouterr().out)

    _force_patch_set_text(monkeypatch, "")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--patch-set"]
    )
    assert exit_code == 0
    out = _strip_generated_at(capsys.readouterr().out)
    assert out == baseline_out


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


def test_config_diff_finds_snapshots_written_by_the_hook_at_the_same_config_dir(tmp_path, capsys):
    """Fix config-dir: an explicit --config-dir now means the same thing
    everywhere -- the token-lens directory itself, with snapshots
    directly under it (see hooks/snapshot-config.py's
    resolve_config_dir and snapshots.load_snapshots docstrings). A user
    who points the *same* --config-dir value at both `snapshot-config`
    and `config-diff` -- the natural thing to try, and previously
    broken (fix R16 papered over it with a dual-fallback that this fix
    removes in favour of one real convention) -- must have config-diff
    find exactly what the hook wrote.
    """
    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)

    config_dir = tmp_path / "my-custom-token-lens"
    snapshots_dir = config_dir / "snapshots"  # the hook's own layout for this same --config-dir
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


def test_snapshot_config_hook_and_config_diff_agree_on_the_same_config_dir(tmp_path, capsys):
    """End-to-end regression for fix config-dir: run the real
    snapshot-config hook (as a subprocess, the way it's actually
    invoked -- see tests/test_hook.py) against a --config-dir, then run
    config-diff against that exact same --config-dir and confirm it
    picks up the snapshot the hook just wrote. This is the concrete
    "point the same --config-dir at both" scenario the reconciliation
    fixes.
    """
    hook_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "claude_token_lens"
        / "hooks"
        / "snapshot-config.py"
    )
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"model": "sonnet"}), encoding="utf-8"
    )
    config_dir = home / ".claude" / "token-lens"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    hook_result = subprocess.run(
        [sys.executable, str(hook_path), "--config-dir", str(config_dir)],
        input=json.dumps({"session_id": "s1"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert hook_result.returncode == 0

    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)
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
    # Was "serve" -- now a real subcommand (S1-api) that would hang in
    # serve_forever() here instead of exiting; "init" is still a v0.3
    # stub and keeps this test's original intent. See module docstring.
    result = subprocess.run(
        [sys.executable, "-m", "claude_token_lens", "init"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent / "src"),
    )
    assert result.returncode == 2
    assert "init" in result.stderr


def test_statusline_cli_forwards_config_dir_flag(tmp_path, monkeypatch, capsys):
    """``claude-token-lens statusline --config-dir PATH`` must actually
    write there. A v0.2 release bug: ``_cmd_statusline`` parsed
    ``--config-dir`` via the "common" argparse group (it's in ``--help``
    for every subcommand) but never forwarded it to
    ``statusline.main()``, which always fell back to
    ``$CLAUDE_CONFIG_DIR``/``~/.claude`` -- silently writing
    ``usage-log.csv``/``statusline-keys.json`` to the real config dir
    even when a caller explicitly asked for an isolated one.
    """
    explicit_config_dir = tmp_path / "explicit-dir"
    payload = {"rate_limits": {"five_hour": {"used_percentage": 7}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    rc = cli.main(["statusline", "--config-dir", str(explicit_config_dir)])

    assert rc == 0
    assert (explicit_config_dir / "usage-log.csv").exists()
