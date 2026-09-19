#!/usr/bin/env python3
"""SessionStart config-capture hook for claude-token-lens (WP7).

Standalone stdlib script. It intentionally imports nothing from the
``claude_token_lens`` package (or any third-party library) so it keeps
working when copied on its own onto a locked-down machine that only has a
plain Python 3.11+ interpreter (see the plan's "Running on other people's
machines" section and Appendix A6). The dependency runs the other way: the
``snapshot-config`` CLI subcommand loads *this* file by path.

Contract (Appendix A6, refined by the WP7 brief):

- Reads the SessionStart hook JSON from stdin: ``session_id``, ``cwd``,
  ``transcript_path``, ``source``. Stdin may be empty or malformed; both are
  tolerated (treated as ``{}``).
- Resolves the config (token-lens) directory: ``--config-dir`` wins and
  IS that directory directly; else ``<CLAUDE_CONFIG_DIR or ~/.claude>/
  token-lens`` (see :func:`resolve_config_dir`; ``settings.json``/
  ``agents/`` are read from its parent, the ``~/.claude`` root).
- Writes ``<config_dir>/snapshots/<UTC compact ts>.json`` (e.g.
  ``20260918T191200Z.json``).
- Never lets a broken Python block a session: always exits 0. On success it
  prints nothing; on any exception it writes one line to stderr and still
  exits 0.
- Idempotent: ``--min-interval SECONDS`` (default 300) skips the write when
  the newest existing snapshot is both younger than that interval and has
  the same ``content_hash`` as the snapshot that would be written now.

Redaction rule applied throughout (settings and agent frontmatter alike):
a value is kept as-is only if its key is in a small safe allowlist, or the
value itself is a ``bool``/``int`` (harmless scalars — booleans and small
integers reveal a toggle or a limit, never content). Every other value is
replaced by a shape-only marker: ``dict(n)``, ``list(n)`` or ``str(len)``.
This is deliberately generic (not just settings.json) so a new, unknown key
in a future Claude Code version degrades to a safe shape marker instead of
leaking its value.

Managed (enterprise-policy) settings (fix 7, plan "Enterprise use" section):
the platform-wide ``managed-settings.json`` a system administrator can drop
outside any user's control (macOS
``/Library/Application Support/ClaudeCode/managed-settings.json``, Linux
``/etc/claude-code/managed-settings.json``, Windows
``%ProgramData%\\ClaudeCode\\managed-settings.json``) is read the same way as
user/project settings.json and redacted with the exact same
``redact_settings`` rule into ``snapshot["managed_settings"]``. Its raw
top-level key names (never values) are additionally recorded verbatim into
``snapshot["managed_keys"]``, since the plan calls for reports to be able to
say "this lever is managed by policy" for any recommendation whose key
appears there — the key name alone ("permissions", "model", ...) carries no
content to redact. ``--managed-path`` overrides the platform default, for
tests and for the rare machine whose policy file lives somewhere else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Settings keys whose values are always safe to record verbatim: they are
#: either short enum-like strings/enums or the value type check below
#: (bool/int) already keeps them. Kept as its own list per the WP7 brief
#: rather than folded into "any scalar", because a handful of *string*
#: settings keys are explicitly named as safe and must be exempted from the
#: generic ``str(len)`` redaction that other string settings get.
SAFE_SETTINGS_KEYS = frozenset(
    {
        "model",
        "effortLevel",
        "outputStyle",
        "autoCompactWindow",
        "promptCacheTtl",
        "subagentPromptCacheTtl",
        "cleanupPeriodDays",
        "desktopSessionCleanupPeriodDays",
        "autoUpdatesChannel",
        "alwaysThinkingEnabled",
    }
)

#: Agent frontmatter keys kept verbatim (everything under "experimental."
#: is also kept — see ``redact_agent_frontmatter``). ``description`` is
#: handled separately (always ``str(len)``), never in this set.
AGENT_KEEP_KEYS = frozenset(
    {
        "name",
        "model",
        "effort",
        "maxTurns",
        "tools",
        "disallowedTools",
        "permissionMode",
        "memory",
        "omitClaudeMd",
        "skills",
        "isolation",
        "background",
    }
)

SCHEMA_VERSION = 1

_TS_FORMAT = "%Y%m%dT%H%M%SZ"


# -- config dir / small file reads --------------------------------------


def resolve_config_dir(cli_arg: str | None = None) -> Path:
    """``--config-dir`` wins -- and IS the token-lens directory itself,
    directly containing ``snapshots/``, ``hooks/`` and ``active-profile``
    -- else ``<CLAUDE_CONFIG_DIR or ~/.claude>/token-lens``.

    Fix config-dir: this used to treat an explicit ``--config-dir`` as
    the ``~/.claude`` root itself (appending ``token-lens/`` for every
    subpath internally), which disagreed with ``cli.py``'s
    ``_resolve_config_dir`` and ``snapshots.load_snapshots`` -- both of
    which have always treated an explicit ``--config-dir`` as the
    token-lens directory directly. A user pointing the same
    ``--config-dir`` value at both this hook and the CLI got snapshots
    written one directory level away from where the CLI looked for
    them. Every caller in this file that needs the ``~/.claude`` root
    itself (for ``settings.json``/``agents/``, which live one level up
    from token-lens) now reaches it via the returned path's ``.parent``.
    """
    if cli_arg:
        return Path(cli_arg)
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(env) if env else (Path.home() / ".claude")
    return root / "token-lens"


def default_managed_settings_path() -> Path:
    """The platform's system-wide ``managed-settings.json`` path (fix 7).
    This file is written by IT/policy tooling, not by the current user, so
    unlike ``resolve_config_dir`` there is no per-user env var to prefer —
    only ``--managed-path`` (handled by the caller) overrides it.
    """
    if sys.platform == "win32":
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(program_data) / "ClaudeCode" / "managed-settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def _read_json_dict(path: Path) -> dict | None:
    """Read ``path`` as a JSON object. Returns ``None`` on any I/O error,
    parse error, or if the top-level value isn't an object — the hook must
    tolerate a missing or malformed settings file, never crash on one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_active_profile(config_dir: Path) -> str | None:
    path = config_dir / "active-profile"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.strip()
    return text or None


# -- redaction ------------------------------------------------------------


def _redact_generic(value):
    """Shape-only redaction used for any key with no special handling:
    bool/int kept (a toggle or a limit, not content), dict/list/str reduced
    to a length marker, everything else (``None``, float) kept as-is.
    """
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, dict):
        return f"dict({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    if isinstance(value, str):
        return f"str({len(value)})"
    return value


def redact_settings_value(key: str, value):
    if key in SAFE_SETTINGS_KEYS:
        return value
    return _redact_generic(value)


def redact_settings(settings: dict) -> dict:
    """Redact a whole settings object (user settings.json or a project
    settings*.json), key by key, using the same allowlist for both.
    """
    if not isinstance(settings, dict):
        return {}
    return {key: redact_settings_value(key, value) for key, value in settings.items()}


def redact_agent_frontmatter(parsed: dict) -> dict:
    """Redact one agent's already-flattened frontmatter dict (see
    ``parse_frontmatter``). ``description`` always becomes ``str(len)``;
    the keep-list (and anything under ``experimental.``) is kept verbatim;
    everything else falls back to the generic shape-only redaction so an
    unrecognised frontmatter field never leaks its value.
    """
    out: dict = {}
    for key, value in parsed.items():
        if key == "description":
            out[key] = f"str({len(value)})" if isinstance(value, str) else "str(0)"
            continue
        if key in AGENT_KEEP_KEYS or key.startswith("experimental."):
            out[key] = value
            continue
        out[key] = _redact_generic(value)
    return out


# -- tiny YAML-ish frontmatter parser (no PyYAML) --------------------------


def _parse_scalar(raw: str):
    """Parse one YAML-ish scalar: quoted strings, ``[a, b]`` inline lists,
    booleans, null, int, float, else the bare string. Good enough for the
    handful of shapes Claude Code agent frontmatter actually uses — this is
    not a general YAML parser.
    """
    raw = raw.strip()
    if raw == "":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_frontmatter(text: str) -> dict:
    """Parse the ``---``-delimited frontmatter block at the top of an agent
    markdown file into a flat dict. Top-level ``key: value`` scalars are
    kept as-is; a top-level ``key:`` with no value starts a nested map
    whose two-space-indented ``child: value`` lines are flattened to
    ``key.child`` (the shape the plan's ``experimental.cacheTtl`` example
    needs). Returns ``{}`` if the file has no frontmatter block at all.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}

    result: dict = {}
    last_top_key: str | None = None
    for raw_line in lines[1:end]:
        if not raw_line.strip():
            continue
        if raw_line[:1] in (" ", "\t") and last_top_key is not None:
            stripped = raw_line.strip()
            if ":" not in stripped:
                continue
            child_key, _, rest = stripped.partition(":")
            result[f"{last_top_key}.{child_key.strip()}"] = _parse_scalar(rest)
            continue
        stripped = raw_line.strip()
        if ":" not in stripped:
            last_top_key = None
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            last_top_key = key
            continue
        result[key] = _parse_scalar(rest)
        last_top_key = None
    return result


def _load_agents(agents_dir: Path) -> dict:
    """Every ``*.md`` directly under ``agents_dir``, keyed by its frontmatter
    ``name`` (falling back to the filename stem), redacted per-field.
    """
    result: dict = {}
    if not agents_dir.is_dir():
        return result
    for md_path in sorted(agents_dir.glob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_frontmatter(text)
        if not parsed:
            continue
        name = parsed.get("name") or md_path.stem
        result[str(name)] = redact_agent_frontmatter(parsed)
    return result


# -- hashing / timestamps ---------------------------------------------------


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_prefixed(text: str) -> str:
    return f"sha256:{_sha256_hex(text)}"


def _now_ts(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(_TS_FORMAT)


def _content_hash(snapshot: dict) -> str:
    """sha256 of the snapshot with ``ts``/``session_id``/``transcript_path``/
    ``source`` excluded, so an idempotent re-run (same config, new session,
    new timestamp) compares equal.
    """
    payload = {
        key: value
        for key, value in snapshot.items()
        if key not in ("ts", "session_id", "transcript_path", "source")
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


# -- snapshot assembly -------------------------------------------------------


def _load_stdin_json() -> dict:
    """Read and parse the hook's stdin payload. Empty, unreadable or
    malformed stdin all tolerate down to ``{}`` — the hook must never fail
    a session start over a stdin problem.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_snapshot(
    stdin_data: dict,
    cwd_override: str | None,
    config_dir: Path,
    managed_path: Path | str | None = None,
) -> dict:
    """Build the full A6-shaped snapshot dict (unwritten) for the given
    hook stdin payload, cwd override and resolved config directory.

    ``config_dir`` is the token-lens directory (see
    :func:`resolve_config_dir`); ``settings.json``/``agents/`` live one
    level up, at ``config_dir.parent`` (the ``~/.claude`` root).

    ``managed_path`` overrides the platform default from
    :func:`default_managed_settings_path` (used by tests and by the
    ``--managed-path`` CLI flag); it is read even if missing, since most
    machines have no managed-settings file at all and that must degrade to
    ``{}``/``[]`` rather than an error.
    """
    session_id = stdin_data.get("session_id")
    transcript_path = stdin_data.get("transcript_path")
    source = stdin_data.get("source")
    cwd = cwd_override or stdin_data.get("cwd") or os.getcwd()
    cwd_path = Path(cwd)
    claude_root = config_dir.parent

    claude_version = os.environ.get("CLAUDE_CODE_VERSION") or None
    profile_id = _read_active_profile(config_dir)

    user_settings_raw = _read_json_dict(claude_root / "settings.json") or {}
    user_settings = redact_settings(user_settings_raw)

    resolved_managed_path = Path(managed_path) if managed_path else default_managed_settings_path()
    managed_settings_raw = _read_json_dict(resolved_managed_path) or {}
    managed_settings = redact_settings(managed_settings_raw)
    managed_keys = sorted(managed_settings_raw.keys())

    project_settings: dict = {}
    for name in ("settings.json", "settings.local.json"):
        path = cwd_path / ".claude" / name
        data = _read_json_dict(path)
        if data is not None:
            project_settings[_sha256_hex(str(path))] = redact_settings(data)

    mcp_names: set[str] = set()
    raw_mcp_servers = user_settings_raw.get("mcpServers")
    if isinstance(raw_mcp_servers, dict):
        mcp_names.update(str(k) for k in raw_mcp_servers)
    mcp_json = _read_json_dict(cwd_path / ".mcp.json")
    if mcp_json is not None:
        project_mcp_servers = mcp_json.get("mcpServers")
        if isinstance(project_mcp_servers, dict):
            mcp_names.update(str(k) for k in project_mcp_servers)
    dot_claude_json = _read_json_dict(Path.home() / ".claude.json")
    if dot_claude_json is not None:
        global_mcp_servers = dot_claude_json.get("mcpServers")
        if isinstance(global_mcp_servers, dict):
            mcp_names.update(str(k) for k in global_mcp_servers)

    enabled_mcpjson = user_settings_raw.get("enabledMcpjsonServers")
    disabled_mcpjson = user_settings_raw.get("disabledMcpjsonServers")

    mcp_servers = {
        "names": sorted(mcp_names),
        "enabled_mcpjson_servers": sorted(str(x) for x in enabled_mcpjson)
        if isinstance(enabled_mcpjson, list)
        else [],
        "disabled_mcpjson_servers": sorted(str(x) for x in disabled_mcpjson)
        if isinstance(disabled_mcpjson, list)
        else [],
    }

    enabled_plugins_raw = user_settings_raw.get("enabledPlugins")
    if isinstance(enabled_plugins_raw, dict):
        enabled_plugins = sorted(str(k) for k in enabled_plugins_raw)
    elif isinstance(enabled_plugins_raw, list):
        enabled_plugins = sorted(str(x) for x in enabled_plugins_raw)
    else:
        enabled_plugins = []

    agents: dict = {}
    agents.update(_load_agents(claude_root / "agents"))
    agents.update(_load_agents(cwd_path / ".claude" / "agents"))

    env_names = sorted(
        name
        for name in os.environ
        if name.startswith("ANTHROPIC_") or name.startswith("CLAUDE_")
    )

    snapshot = {
        "schema": SCHEMA_VERSION,
        "ts": _now_ts(),
        "session_id": session_id,
        "transcript_path": transcript_path,
        "source": source,
        "cwd_hash": _sha256_prefixed(cwd),
        "claude_version": claude_version,
        "profile_id": profile_id,
        "user_settings": user_settings,
        "managed_settings": managed_settings,
        "managed_keys": managed_keys,
        "project_settings": project_settings,
        "mcp_servers": mcp_servers,
        "enabled_plugins": enabled_plugins,
        "agents": agents,
        "env_names": env_names,
    }
    snapshot["content_hash"] = _content_hash(snapshot)
    return snapshot


# -- idempotent write --------------------------------------------------------


def _snapshots_dir(config_dir: Path) -> Path:
    return config_dir / "snapshots"


def _find_latest_snapshot(snapshots_dir: Path) -> Path | None:
    if not snapshots_dir.is_dir():
        return None
    files = sorted(p for p in snapshots_dir.glob("*.json") if p.is_file())
    return files[-1] if files else None


def _snapshot_age_seconds(ts: str, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    try:
        parsed = datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - parsed).total_seconds()


def _should_skip(new_snapshot: dict, snapshots_dir: Path, min_interval: int) -> bool:
    latest_path = _find_latest_snapshot(snapshots_dir)
    if latest_path is None:
        return False
    existing = _read_json_dict(latest_path)
    if existing is None:
        return False
    age = _snapshot_age_seconds(str(existing.get("ts", "")))
    if age is None or age >= min_interval:
        return False
    return existing.get("content_hash") == new_snapshot.get("content_hash")


def snapshot_and_get_path(
    config_dir: Path,
    cwd: str | None,
    min_interval: int = 300,
    stdin_data: dict | None = None,
    managed_path: Path | str | None = None,
) -> tuple[Path | None, bool]:
    """Build a snapshot for ``cwd`` and write it unless idempotency skips
    it. Returns ``(path, written)``: ``path`` is the snapshot that now
    represents current config — either the file just written, or (when
    skipped) the most recent existing one; ``written`` says which. ``path``
    is ``None`` only when the write was skipped and no snapshot exists yet
    (shouldn't happen, since a skip requires a latest snapshot to compare
    against — kept for defensiveness).
    """
    snapshot = build_snapshot(stdin_data or {}, cwd, config_dir, managed_path=managed_path)
    snapshots_dir = _snapshots_dir(config_dir)
    if _should_skip(snapshot, snapshots_dir, min_interval):
        return _find_latest_snapshot(snapshots_dir), False

    snapshots_dir.mkdir(parents=True, exist_ok=True)
    out_path = snapshots_dir / f"{snapshot['ts']}.json"
    out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return out_path, True


# -- install / print-hook (used by the CLI subcommand, not the hook itself) -


def install_hook(config_dir: Path) -> Path:
    """Copy this script into ``<config_dir>/hooks/`` (``config_dir`` is
    the token-lens directory -- see :func:`resolve_config_dir`). Never
    touches settings.json — pair with ``hook_fragment_text()`` for the
    fragment the user pastes in themselves.
    """
    dest_dir = config_dir / "hooks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "snapshot-config.py"
    shutil.copy2(Path(__file__).resolve(), dest)
    return dest


def hook_fragment_text() -> str:
    """The settings.json ``hooks`` fragment to paste in, for Windows and
    POSIX, using the commands named in the plan's "Running on other
    people's machines" section (both must exit 0 and print nothing on
    error so a broken Python never blocks a session or blanks the status
    line — this script already guarantees that).
    """
    windows_command = (
        r'py -3 "%USERPROFILE%\.claude\token-lens\hooks\snapshot-config.py"'
    )
    posix_command = 'python3 "$HOME/.claude/token-lens/hooks/snapshot-config.py"'

    def _fragment(command: str) -> str:
        return json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": command}]}
                    ]
                }
            },
            indent=2,
        )

    return (
        "Merge this into the \"hooks\" key of ~/.claude/settings.json\n"
        "(SessionStart may already have other entries — add to the list,\n"
        "don't replace it). Install the script first with --install-hook,\n"
        "or point the command at wherever you copied hooks/snapshot-config.py.\n"
        "\n"
        "Windows:\n"
        f"{_fragment(windows_command)}\n"
        "\n"
        "POSIX (Linux/macOS):\n"
        f"{_fragment(posix_command)}"
    )


# -- CLI entry point ----------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="snapshot-config.py")
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--cwd", default=None, help="override stdin's cwd")
    parser.add_argument(
        "--print", action="store_true", dest="print_only",
        help="print the snapshot JSON instead of writing it",
    )
    parser.add_argument("--min-interval", type=int, default=300)
    parser.add_argument(
        "--managed-path", default=None,
        help="override the platform managed-settings.json path (testing, or a "
        "non-standard policy location)",
    )
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> None:
    stdin_data = _load_stdin_json()
    config_dir = resolve_config_dir(args.config_dir)

    if args.print_only:
        snapshot = build_snapshot(stdin_data, args.cwd, config_dir, managed_path=args.managed_path)
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return

    snapshot_and_get_path(
        config_dir,
        args.cwd or stdin_data.get("cwd"),
        min_interval=args.min_interval,
        stdin_data=stdin_data,
        managed_path=args.managed_path,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = _parse_args(argv)  # -h/--help/bad-flag exits are a deliberate CLI
    # usage error, not a session-start failure, so argparse's own SystemExit
    # is left alone. Everything below this point is a session-start
    # failure mode and must never propagate as a non-zero exit.
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - must never fail a session start
        print(f"snapshot-config: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
