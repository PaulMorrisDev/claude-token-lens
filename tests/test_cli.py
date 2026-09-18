"""CLI skeleton tests: --version, the default-subcommand insertion rule,
and every stub subcommand's "not implemented" exit.
"""

from __future__ import annotations

import pytest

from claude_token_lens import __version__, cli

#: Subcommands that have a real implementation and so must be excluded from
#: the "every stub exits 2" sweep below. Update this set as each work
#: package replaces another stub.
IMPLEMENTED = {"pricing-check", "snapshot-config"}

STUB_SUBCOMMANDS = tuple(c for c in cli.SUBCOMMANDS if c not in IMPLEMENTED)


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


@pytest.mark.parametrize("command", STUB_SUBCOMMANDS)
def test_every_subcommand_stub_exits_2(command, capsys):
    exit_code = cli.main([command])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not implemented" in err
    assert command in err


def test_pricing_check_exits_0(capsys):
    exit_code = cli.main(["pricing-check"])
    assert exit_code == 0
    capsys.readouterr()


def test_snapshot_config_print_hook_exits_0(capsys):
    exit_code = cli.main(["snapshot-config", "--print-hook"])
    assert exit_code == 0
    capsys.readouterr()


def test_no_argv_defaults_to_report_stub(capsys):
    exit_code = cli.main([])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "report" in err


def test_default_subcommand_constant_is_report():
    assert cli.DEFAULT_SUBCOMMAND == "report"
