"""Tests for ``service/monthly_job.py`` (``serve --monthly-report DIR``):
the previous month's report is written once when missing, not rewritten
while it is there, written again for the next month when the clock
moves on, and a failure is logged rather than raised. Also checks that
``serve --once --monthly-report DIR`` runs the job. A fake clock and
temp folders throughout; no real service is started."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from claude_token_lens import cli
from claude_token_lens.service.contracts import ServeOptions
from claude_token_lens.service.monthly_job import MonthlyReportJob

from helpers import turn_line, write_jsonl


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _setup(tmp_path):
    projects_root = tmp_path / "projects"
    project = projects_root / "my-project"
    project.mkdir(parents=True)
    write_jsonl(project / "aug.jsonl", [turn_line(timestamp="2026-08-14T12:00:00.000Z")])
    write_jsonl(project / "sep.jsonl", [turn_line(timestamp="2026-09-14T12:00:00.000Z")])
    options = ServeOptions(
        projects_root=projects_root,
        config_dir=tmp_path / "token-lens",
        monthly_report_dir=tmp_path / "reports",
    )
    return options


def _job(options, clock, logs):
    return MonthlyReportJob(options, now_fn=clock, log=logs.append)


def test_writes_last_months_report_once_when_missing(tmp_path):
    options = _setup(tmp_path)
    clock = _Clock(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
    logs: list[str] = []
    job = _job(options, clock, logs)

    paths = job.run_once()
    assert [p.name for p in paths] == ["claude-token-lens-2026-08.md", "claude-token-lens-2026-08.html"]
    assert all(p.is_file() for p in paths)
    assert "2026-08" in paths[0].read_text(encoding="utf-8")
    assert any("for 2026-08 written to" in line for line in logs)

    # Already there: nothing is rewritten on the next check.
    before = paths[0].stat().st_mtime_ns
    assert job.run_once() == []
    assert paths[0].stat().st_mtime_ns == before


def test_next_month_is_written_when_the_clock_moves_on(tmp_path):
    options = _setup(tmp_path)
    clock = _Clock(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
    job = _job(options, clock, [])
    job.run_once()
    clock.now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    assert [p.name for p in job.run_once()] == ["claude-token-lens-2026-09.md", "claude-token-lens-2026-09.html"]
    names = sorted(p.name for p in options.monthly_report_dir.iterdir())
    assert names == [
        "claude-token-lens-2026-08.html",
        "claude-token-lens-2026-08.md",
        "claude-token-lens-2026-09.html",
        "claude-token-lens-2026-09.md",
    ]


def test_a_deleted_report_is_written_again(tmp_path):
    options = _setup(tmp_path)
    clock = _Clock(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
    job = _job(options, clock, [])
    md, _html = job.run_once()
    md.unlink()
    assert [p.name for p in job.run_once()] == ["claude-token-lens-2026-08.md", "claude-token-lens-2026-08.html"]


def test_an_empty_month_is_written_with_a_note(tmp_path):
    options = _setup(tmp_path)
    clock = _Clock(datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc))
    logs: list[str] = []
    paths = _job(options, clock, logs).run_once()
    assert [p.name for p in paths] == ["claude-token-lens-2026-06.md", "claude-token-lens-2026-06.html"]
    assert any("no sessions found for 2026-06" in line for line in logs)


def test_failure_is_logged_not_raised(tmp_path):
    options = ServeOptions(
        projects_root=tmp_path / "no-projects-here",
        config_dir=tmp_path / "token-lens",
        monthly_report_dir=tmp_path / "reports",
    )
    logs: list[str] = []
    job = _job(options, _Clock(datetime(2026, 9, 15, tzinfo=timezone.utc)), logs)
    assert job.run_once() == []
    assert logs and "monthly report for 2026-08 not written" in logs[0]
    assert not (tmp_path / "reports").exists()


def test_bad_config_is_logged_not_raised(tmp_path):
    options = _setup(tmp_path)
    options.config_dir.mkdir()
    (options.config_dir / "config.toml").write_text("billing = [not toml", encoding="utf-8")
    logs: list[str] = []
    assert _job(options, _Clock(datetime(2026, 9, 15, tzinfo=timezone.utc)), logs).run_once() == []
    assert "not written" in logs[0]


def test_start_checks_at_once_in_the_background_and_stop_ends_it(tmp_path):
    options = _setup(tmp_path)
    written = threading.Event()
    job = MonthlyReportJob(
        options,
        now_fn=_Clock(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)),
        check_interval_s=3600,
        log=lambda text: written.set() if "written to" in text else None,
    )
    job.start()
    job.start()  # idempotent
    try:
        assert written.wait(30)
    finally:
        job.stop()
    assert job._thread is None
    assert (options.monthly_report_dir / "claude-token-lens-2026-08.md").is_file()


def test_serve_once_runs_the_monthly_job(tmp_path, monkeypatch):
    options = _setup(tmp_path)
    calls = []
    monkeypatch.setattr(MonthlyReportJob, "run_once", lambda self: calls.append(self.out_dir) or [])
    rc = cli.main(
        [
            "serve",
            "--once",
            "--projects-root",
            str(options.projects_root),
            "--config-dir",
            str(options.config_dir),
            "--monthly-report",
            str(options.monthly_report_dir),
        ]
    )
    assert rc == 0
    assert calls == [options.monthly_report_dir]


def test_serve_once_without_the_flag_writes_no_report(tmp_path, monkeypatch):
    options = _setup(tmp_path)
    calls = []
    monkeypatch.setattr(MonthlyReportJob, "run_once", lambda self: calls.append(1) or [])
    assert cli.main(
        ["serve", "--once", "--projects-root", str(options.projects_root), "--config-dir", str(options.config_dir)]
    ) == 0
    assert calls == []
