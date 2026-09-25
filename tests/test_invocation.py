"""The command the dashboard prints for this install (invocation.py).

The short ``claude-token-lens`` only runs when pip's Scripts folder is
on ``PATH``; a default Windows Python install leaves it off, so the
service names the form that runs here and swaps it into every command
it serves. conftest.py pins ``CLAUDE_TOKEN_LENS_COMMAND`` to the short
form for every other test; these tests clear or set it themselves.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from claude_token_lens import installer, invocation

from test_service_api import _start_server


@pytest.fixture
def detect(monkeypatch):
    """Run the detection fresh, with no override and a controlled PATH
    lookup: ``which`` maps a name to a path (missing names are None)."""
    monkeypatch.delenv(invocation.ENV_VAR, raising=False)
    invocation._detected_prefix.cache_clear()
    found: dict[str, str] = {}
    monkeypatch.setattr(invocation.shutil, "which", lambda name: found.get(name))
    monkeypatch.setattr(installer, "detect_pyz_path", lambda: None)

    def run(*, executable: str, which: dict[str, str] | None = None, pyz: Path | None = None) -> str:
        found.clear()
        found.update(which or {})
        monkeypatch.setattr(invocation.sys, "executable", executable)
        monkeypatch.setattr(installer, "detect_pyz_path", lambda: pyz)
        invocation._detected_prefix.cache_clear()
        return invocation.command_prefix()

    yield run
    invocation._detected_prefix.cache_clear()


def _scripts_dir(monkeypatch, folder: Path) -> None:
    monkeypatch.setattr(invocation.sysconfig, "get_path", lambda name, scheme=None: str(folder) if scheme is None else "")


# -- detection -----------------------------------------------------------


def test_the_env_var_wins_word_for_word(monkeypatch):
    monkeypatch.setenv(invocation.ENV_VAR, "  tl  ")
    assert invocation.command_prefix() == "tl"


def test_an_empty_env_var_falls_back_to_the_detection(detect, monkeypatch):
    monkeypatch.setenv(invocation.ENV_VAR, "   ")
    python = str(Path("/opt/py/bin/python3"))
    assert detect(executable=python) == f"{python} -m claude_token_lens"


def test_the_launcher_in_this_pythons_scripts_folder_keeps_the_short_form(detect, monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    _scripts_dir(monkeypatch, scripts)
    launcher = str(scripts / "claude-token-lens.exe")
    assert detect(executable=str(tmp_path / "python.exe"), which={"claude-token-lens": launcher}) == "claude-token-lens"


def test_a_launcher_from_another_python_is_not_trusted(detect, monkeypatch, tmp_path):
    # An older install elsewhere on PATH would run different code.
    _scripts_dir(monkeypatch, tmp_path / "mine" / "Scripts")
    python = str(tmp_path / "mine" / "python.exe")
    other = str(tmp_path / "other" / "Scripts" / "claude-token-lens.exe")
    prefix = detect(executable=python, which={"claude-token-lens": other})
    assert prefix.endswith(" -m claude_token_lens")
    assert "claude-token-lens" not in prefix


def test_no_launcher_and_python_on_path_is_this_one(detect, tmp_path):
    python = str(tmp_path / "python.exe")
    assert detect(executable=python, which={"python": python}) == "python -m claude_token_lens"


def test_python3_on_path_counts_when_python_is_missing(detect, tmp_path):
    python = str(tmp_path / "bin" / "python3")
    assert detect(executable=python, which={"python3": python}) == "python3 -m claude_token_lens"


def test_a_different_python_on_path_means_the_full_path(detect, tmp_path):
    # The user's case: the service runs one Python, PATH finds another
    # (or none), so "python -m" would miss the package.
    python = str(tmp_path / "Python314" / "python.exe")
    other = str(tmp_path / "WindowsApps" / "python.exe")
    assert detect(executable=python, which={"python": other}) == f"{python} -m claude_token_lens"


def test_the_pyz_runs_through_this_python(detect, tmp_path):
    python = str(tmp_path / "python.exe")
    pyz = tmp_path / "claude-token-lens.pyz"
    assert detect(executable=python, which={"python": python}, pyz=pyz) == f"python {pyz}"


def test_the_pyz_wins_over_a_launcher_on_path(detect, monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    _scripts_dir(monkeypatch, scripts)
    python = str(tmp_path / "python.exe")
    pyz = tmp_path / "claude-token-lens.pyz"
    which = {"python": python, "claude-token-lens": str(scripts / "claude-token-lens.exe")}
    assert detect(executable=python, which=which, pyz=pyz) == f"python {pyz}"


def test_pythonw_becomes_the_console_python_beside_it(detect, tmp_path):
    # The logon service runs windowless; pythonw prints nothing to a terminal.
    (tmp_path / "python.exe").write_bytes(b"")
    pythonw = str(tmp_path / "pythonw.exe")
    assert detect(executable=pythonw) == f"{tmp_path / 'python.exe'} -m claude_token_lens"


def test_pythonw_stays_when_no_console_python_sits_beside_it(detect, tmp_path):
    pythonw = str(tmp_path / "pythonw.exe")
    assert detect(executable=pythonw) == f"{pythonw} -m claude_token_lens"


def test_the_detection_is_worked_out_once(detect, monkeypatch, tmp_path):
    python = str(tmp_path / "python.exe")
    assert detect(executable=python, which={"python": python}) == "python -m claude_token_lens"
    monkeypatch.setattr(invocation.sys, "executable", str(tmp_path / "moved.exe"))
    assert invocation.command_prefix() == "python -m claude_token_lens"


@pytest.mark.parametrize(
    ("platform", "path", "expected"),
    [
        ("win32", r"C:\Python314\python.exe", r"C:\Python314\python.exe"),
        ("win32", r"C:\Program Files\Python314\python.exe", r'& "C:\Program Files\Python314\python.exe"'),
        ("linux", "/opt/py/bin/python3", "/opt/py/bin/python3"),
        ("linux", "/home/me/my py/bin/python3", "'/home/me/my py/bin/python3'"),
    ],
)
def test_a_path_is_quoted_only_when_it_needs_it(monkeypatch, platform, path, expected):
    monkeypatch.setattr(invocation.sys, "platform", platform)
    assert invocation._quote(path) == expected


def test_the_real_detection_names_a_form_that_runs_here(monkeypatch):
    monkeypatch.delenv(invocation.ENV_VAR, raising=False)
    invocation._detected_prefix.cache_clear()
    try:
        prefix = invocation.command_prefix()
    finally:
        invocation._detected_prefix.cache_clear()
    assert prefix == "claude-token-lens" or prefix.endswith((" -m claude_token_lens", ".pyz", '.pyz"'))
    assert "\n" not in prefix


# -- rewrite -------------------------------------------------------------

PREFIX = r'& "C:\Program Files\Python314\python.exe" -m claude_token_lens'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("claude-token-lens capture connect", f"{PREFIX} capture connect"),
        ("Run 'claude-token-lens capture connect' to add them.", f"Run '{PREFIX} capture connect' to add them."),
        ("claude-token-lens apply --dry-run", f"{PREFIX} apply --dry-run"),
        ("claude-token-lens --version", f"{PREFIX} --version"),
        ("First:\nclaude-token-lens report", f"First:\n{PREFIX} report"),
        ("(claude-token-lens report)", f"({PREFIX} report)"),
        # Prose, file names and made-up words stay as they are.
        ("claude-token-lens ships with no dependencies.", "claude-token-lens ships with no dependencies."),
        ("Download claude-token-lens.pyz first.", "Download claude-token-lens.pyz first."),
        ("claude-token-lens capture-foo", "claude-token-lens capture-foo"),
        ("claude-token-lens reports", "claude-token-lens reports"),
        ("~/.claude-token-lens capture", "~/.claude-token-lens capture"),
        ("my-claude-token-lens capture", "my-claude-token-lens capture"),
    ],
)
def test_rewrite_swaps_only_commands(text, expected):
    assert invocation.rewrite(text, PREFIX) == expected


def test_rewrite_leaves_every_subcommand_reachable():
    from claude_token_lens.cli import SUBCOMMANDS

    for word in SUBCOMMANDS:
        assert invocation.rewrite(f"claude-token-lens {word}", "tl") == f"tl {word}", word


def test_the_short_form_changes_nothing():
    value = {"a": ["claude-token-lens report"]}
    assert invocation.rewrite_payload(value, "claude-token-lens") is value


def test_rewrite_payload_reaches_nested_strings_and_leaves_keys():
    value = {
        "claude-token-lens report": "claude-token-lens report",
        "rows": [["claude-token-lens baseline", 3, None, True], ("claude-token-lens sessions",)],
        "n": 1.5,
    }
    assert invocation.rewrite_payload(value, "tl") == {
        "claude-token-lens report": "tl report",
        "rows": [["tl baseline", 3, None, True], ["tl sessions"]],
        "n": 1.5,
    }


# -- the service serves it ------------------------------------------------

# A path with a space and a quote: it must survive JSON and HTML intact.
SERVED = '& "C:\\Program Files\\Py\'s\\python.exe" -m claude_token_lens'


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv(invocation.ENV_VAR, SERVED)
    handle = _start_server(tmp_path, monkeypatch)
    try:
        yield handle
    finally:
        handle.close()
        handle.store.close()


def test_api_commands_come_in_this_installs_form(served):
    resp, payload = served.post_json("/api/capture", {"level": "essentials"})
    assert resp.status == 200
    assert payload["data"]["hooks"]["connect_command"] == f"{SERVED} capture connect"


def test_the_page_carries_the_form_for_the_dashboards_own_commands(served):
    resp, raw = served.request("GET", "/")
    assert resp.status == 200
    page = raw.decode("utf-8")
    assert f'<meta name="tl-command" content="{html.escape(SERVED, quote=True)}">' in page
    assert 'content="claude-token-lens"' not in page


def test_report_json_stays_valid_json_with_the_form_swapped_in(served):
    from claude_token_lens.service import api as service_api

    rendered = json.dumps({"report": {"notes": ["Run claude-token-lens baseline.\nclaude-token-lens report"]}})
    text = service_api._report_json_commands(rendered)
    assert json.loads(text) == {"report": {"notes": [f"Run {SERVED} baseline.\n{SERVED} report"]}}
    # Rendered the way render_json renders it.
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=2)

    resp, raw = served.request("GET", "/api/report.json")
    assert resp.status == 200
    leftover = [s for s in _strings(json.loads(raw)) if invocation._command_pattern().search(s)]
    assert leftover == []


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_index_placeholder_matches_the_one_the_service_replaces():
    from claude_token_lens.service import api as service_api

    index = Path(invocation.__file__).parent / "service" / "static" / "index.html"
    assert index.read_bytes().count(service_api._COMMAND_META) == 1
