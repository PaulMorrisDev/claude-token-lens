"""Tests for the ``cli serve`` flags added by S1-integration: deliverable
1.a's ``--billing-mode``/``--monthly-report`` (wired into
``ServeOptions``) and deliverable 2.e's ``--purge`` (deletes
``<config-dir>/service.db`` and its WAL/SHM sidecars).

These live in their own file (rather than in ``tests/test_cli.py``,
which is out of this work package's writable scope) and exercise
``cli.main(["serve", ...])`` directly, using ``--once`` throughout so
the process never reaches ``serve_forever()``, and monkeypatching
``service.serve.run`` where a test only cares what ``ServeOptions``
the CLI built rather than actually running the watcher/API.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from claude_token_lens import cli

from helpers import turn_line, write_jsonl


def _capture_options(monkeypatch):
    """Patch ``service.serve.run`` to record the ``ServeOptions`` (and
    ``once``/``allow_remote``) it was called with, instead of actually
    opening a store or binding a socket.
    """
    captured: dict = {}

    def fake_run(options, *, once=False, allow_remote=False):
        captured["options"] = options
        captured["once"] = once
        captured["allow_remote"] = allow_remote
        return 0

    fake_module = types.ModuleType("claude_token_lens.service.serve")
    fake_module.run = fake_run
    fake_module.STORE_FILENAME = "service.db"
    monkeypatch.setitem(sys.modules, "claude_token_lens.service.serve", fake_module)
    return captured


# -- --billing-mode / --monthly-report (deliverable 1.a) ---------------------


def test_billing_mode_defaults_to_api_with_no_config_file(tmp_path: Path, monkeypatch):
    captured = _capture_options(monkeypatch)
    config_dir = tmp_path / "config"

    rc = cli.main(
        ["serve", "--once", "--projects-root", str(tmp_path / "projects"), "--config-dir", str(config_dir)]
    )
    assert rc == 0
    assert captured["options"].billing_mode == "api"
    assert captured["options"].monthly_report_dir is None


def test_billing_mode_defaults_from_config_toml(tmp_path: Path, monkeypatch):
    captured = _capture_options(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('billing = "subscription"\n', encoding="utf-8")

    rc = cli.main(
        ["serve", "--once", "--projects-root", str(tmp_path / "projects"), "--config-dir", str(config_dir)]
    )
    assert rc == 0
    assert captured["options"].billing_mode == "subscription"


def test_billing_mode_cli_flag_overrides_config_toml(tmp_path: Path, monkeypatch):
    captured = _capture_options(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('billing = "subscription"\n', encoding="utf-8")

    rc = cli.main(
        [
            "serve",
            "--once",
            "--projects-root",
            str(tmp_path / "projects"),
            "--config-dir",
            str(config_dir),
            "--billing-mode",
            "api",
        ]
    )
    assert rc == 0
    assert captured["options"].billing_mode == "api"


def test_billing_mode_rejects_unknown_choice(tmp_path: Path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "serve",
                "--once",
                "--projects-root",
                str(tmp_path / "projects"),
                "--config-dir",
                str(tmp_path / "config"),
                "--billing-mode",
                "bogus",
            ]
        )
    assert exc_info.value.code == 2


def test_monthly_report_dir_is_passed_through(tmp_path: Path, monkeypatch):
    captured = _capture_options(monkeypatch)
    report_dir = tmp_path / "reports"

    rc = cli.main(
        [
            "serve",
            "--once",
            "--projects-root",
            str(tmp_path / "projects"),
            "--config-dir",
            str(tmp_path / "config"),
            "--monthly-report",
            str(report_dir),
        ]
    )
    assert rc == 0
    assert captured["options"].monthly_report_dir == report_dir


# -- --purge (deliverable 2.e) ------------------------------------------------


def test_purge_without_yes_lists_and_refuses(tmp_path: Path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / "service.db"
    db_path.write_text("not a real db", encoding="utf-8")

    rc = cli.main(["serve", "--purge", "--config-dir", str(config_dir)])
    assert rc == 2
    out = capsys.readouterr()
    assert str(db_path) in out.out
    assert db_path.exists()  # nothing was deleted


def test_purge_with_yes_deletes_db_and_sidecars(tmp_path: Path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = config_dir / "service.db"
    wal_path = config_dir / "service.db-wal"
    shm_path = config_dir / "service.db-shm"
    db_path.write_text("not a real db", encoding="utf-8")
    wal_path.write_text("wal", encoding="utf-8")
    shm_path.write_text("shm", encoding="utf-8")

    rc = cli.main(["serve", "--purge", "--yes", "--config-dir", str(config_dir)])
    assert rc == 0
    assert not db_path.exists()
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_purge_with_nothing_to_delete_is_a_clean_no_op(tmp_path: Path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    rc = cli.main(["serve", "--purge", "--yes", "--config-dir", str(config_dir)])
    assert rc == 0
    out = capsys.readouterr()
    assert "nothing to delete" in out.out


def test_purge_never_reaches_service_serve_run(tmp_path: Path, monkeypatch):
    # --purge is a terminal action -- it must never fall through to
    # actually starting the watcher/API.
    captured = _capture_options(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "service.db").write_text("x", encoding="utf-8")

    rc = cli.main(["serve", "--purge", "--yes", "--config-dir", str(config_dir)])
    assert rc == 0
    assert captured == {}


__all__: list[str] = []
