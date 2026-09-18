"""User-editable configuration (WP5): ``config.toml`` and per-session
overrides (``sessions.toml``).

Two files live under ``<config_dir>/`` (default ``~/.claude/token-lens``,
or ``$CLAUDE_CONFIG_DIR/token-lens`` when that env var moves the whole
config tree — the same resolution ``pricing.py`` uses for
``pricing.toml``, deliberately kept independent here rather than
importing ``pricing.TOKEN_LENS_DIRNAME`` so this module has no
dependency beyond ``model.py``/the standard library):

- ``config.toml``: :func:`load_config` -> :class:`Config`. A missing
  file is not an error — every field just takes its documented default.
  Malformed TOML raises :class:`ConfigError`.
- ``sessions.toml``: per-session ``mode``/``purpose`` overrides that win
  ahead of ``classify.classify_mode``/``classify_purpose``'s rules (see
  ``classify.classify_session``). :func:`load_session_overrides` reads
  every entry; :func:`save_session_override` rewrites one entry in
  place, preserving every other session's entry untouched.

``tomllib`` (stdlib, read-only) has no counterpart writer, so
:func:`save_session_override` serialises TOML by hand — see
``_write_sessions_toml``. The format it writes back is deliberately the
same shape ``load_session_overrides`` reads, so a round trip through
both functions is lossless for the keys this module understands
(``mode``, ``purpose``, ``tags``); an entry section with unrecognised
extra keys is preserved as opaque scalars/lists (not dropped), but a
value shaped as a nested table under a session id is out of scope (the
plan's ``[sessions."<id>"]`` shape is flat) and is dropped with the rest
of that key silently — the same "never crash on a foreign shape"
posture ``pricing.py``/``snapshots.py`` take for optional structure.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: Directory name under the resolved Claude config root holding
#: claude-token-lens's own files. Mirrors ``pricing.TOKEN_LENS_DIRNAME``.
TOKEN_LENS_DIRNAME = "token-lens"

_ALLOWED_BILLING = frozenset({"api", "subscription"})
#: Same three values ``parse.detect_provider`` can return (fix 6).
_ALLOWED_PROVIDER = frozenset({"anthropic", "bedrock", "vertex"})


class ConfigError(Exception):
    """A config or session-overrides file could not be read or is not
    valid TOML/structure. Written to stand alone as user-facing output,
    same convention as ``pricing.PricingError``.
    """


@dataclass(slots=True)
class Config:
    """Parsed ``config.toml`` (or all-defaults when the file is absent)."""

    billing: str = "api"  # "api" | "subscription"
    #: IANA zone name, or None for the machine's own local zone.
    tz: str | None = None
    #: Free-form, passed to other packages' ``from_config`` (plan
    #: "Classification"/"RE-CACHE" sections) — this module doesn't
    #: interpret its contents, only loads/validates its shape.
    thresholds: dict = field(default_factory=dict)
    recache: dict = field(default_factory=dict)
    min_sessions: int = 5
    min_turns: int = 200
    allow_titles: bool = False
    pricing_path: str | None = None
    #: Slug regexes (``re.search``, case-insensitive — same convention as
    #: ``discovery.resolve_project_dirs``'s own ``family_regex``) for
    #: project directories to leave out of every discovery call. A slug
    #: matching any one of these is excluded even when explicitly named
    #: by ``--project`` or matched by ``--project-family``/
    #: ``--all-projects`` — this is a standing "never touch this project"
    #: list (e.g. a work project on a personal machine), not a narrower
    #: selector. Fix 6 addition.
    exclude_projects: list[str] = field(default_factory=list)
    #: Sessions older than this many days (by the same ``mtime``/
    #: ``timestamp`` window key ``discovery.find_sessions`` already
    #: understands) are outside the tool's normal window — not enforced
    #: by this module, just carried through for a future caller (report
    #: assembly/CLI) to apply as its own default ``--days`` when neither
    #: ``--days`` nor ``--since``/``--until`` is given explicitly on the
    #: command line. ``None`` means no retention window. Fix 6 addition.
    retention_days: int | None = None
    #: Force every session's ``TranscriptMeta.provider`` to this value
    #: (``"anthropic"``/``"bedrock"``/``"vertex"``) rather than deriving
    #: it per-session from the model id (see ``parse.detect_provider``) —
    #: for an account that's always on one provider and wants the report
    #: header to say so unambiguously even for a session with no model
    #: line at all. ``None`` (the default) leaves per-session detection
    #: alone. Fix 6 addition.
    provider: str | None = None

    def describe(self) -> list[str]:
        """Lines for the report header (plan "Renderers and CLI"
        section)."""
        lines = [
            f"billing: {self.billing}",
            f"timezone: {self.tz or 'local (machine)'}",
            f"min_sessions: {self.min_sessions}",
            f"min_turns: {self.min_turns}",
            f"allow_titles: {self.allow_titles}",
            f"pricing_path: {self.pricing_path or 'default (packaged/config-dir)'}",
        ]
        if self.thresholds:
            lines.append(f"thresholds: {self.thresholds}")
        if self.recache:
            lines.append(f"recache: {self.recache}")
        if self.exclude_projects:
            lines.append(f"exclude_projects: {self.exclude_projects}")
        if self.retention_days is not None:
            lines.append(f"retention_days: {self.retention_days}")
        if self.provider is not None:
            lines.append(f"provider: {self.provider}")
        return lines


def _default_config_dir() -> Path:
    """``~/.claude/token-lens``, or ``$CLAUDE_CONFIG_DIR/token-lens`` when
    ``CLAUDE_CONFIG_DIR`` moves the whole config tree elsewhere. Mirrors
    ``pricing._default_token_lens_dir()`` (see the module docstring for
    why this isn't a shared import).
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / TOKEN_LENS_DIRNAME


def _resolve_config_dir(config_dir: str | Path | None) -> Path:
    return Path(config_dir) if config_dir is not None else _default_config_dir()


def _read_toml(path: Path, *, what: str) -> dict | None:
    """Read and parse one TOML file. Returns ``None`` if it doesn't
    exist; raises :class:`ConfigError` if it can't be read or isn't
    valid TOML.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {what}: {path} ({exc})") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed {what} ({path}): {exc}") from exc


# -- config.toml --------------------------------------------------------


def _build_config(data: dict, path: Path) -> Config:
    config = Config()

    billing = data.get("billing", config.billing)
    if not isinstance(billing, str) or billing not in _ALLOWED_BILLING:
        raise ConfigError(
            f"config file {path}: 'billing' must be one of {sorted(_ALLOWED_BILLING)}, got {billing!r}"
        )
    config.billing = billing

    tz = data.get("tz")
    if tz is not None and not isinstance(tz, str):
        raise ConfigError(f"config file {path}: 'tz' must be a string")
    config.tz = tz

    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ConfigError(f"config file {path}: 'thresholds' must be a table")
    config.thresholds = dict(thresholds)

    recache = data.get("recache", {})
    if not isinstance(recache, dict):
        raise ConfigError(f"config file {path}: 'recache' must be a table")
    config.recache = dict(recache)

    min_sessions = data.get("min_sessions", config.min_sessions)
    if not isinstance(min_sessions, int) or isinstance(min_sessions, bool):
        raise ConfigError(f"config file {path}: 'min_sessions' must be an integer")
    config.min_sessions = min_sessions

    min_turns = data.get("min_turns", config.min_turns)
    if not isinstance(min_turns, int) or isinstance(min_turns, bool):
        raise ConfigError(f"config file {path}: 'min_turns' must be an integer")
    config.min_turns = min_turns

    allow_titles = data.get("allow_titles", config.allow_titles)
    if not isinstance(allow_titles, bool):
        raise ConfigError(f"config file {path}: 'allow_titles' must be a boolean")
    config.allow_titles = allow_titles

    pricing_path = data.get("pricing_path")
    if pricing_path is not None and not isinstance(pricing_path, str):
        raise ConfigError(f"config file {path}: 'pricing_path' must be a string")
    config.pricing_path = pricing_path

    exclude_projects = data.get("exclude_projects", [])
    if not isinstance(exclude_projects, list) or not all(
        isinstance(item, str) for item in exclude_projects
    ):
        raise ConfigError(f"config file {path}: 'exclude_projects' must be a list of strings")
    config.exclude_projects = list(exclude_projects)

    retention_days = data.get("retention_days")
    if retention_days is not None and (
        not isinstance(retention_days, int) or isinstance(retention_days, bool)
    ):
        raise ConfigError(f"config file {path}: 'retention_days' must be an integer")
    config.retention_days = retention_days

    provider = data.get("provider")
    if provider is not None and (not isinstance(provider, str) or provider not in _ALLOWED_PROVIDER):
        raise ConfigError(
            f"config file {path}: 'provider' must be one of {sorted(_ALLOWED_PROVIDER)}, got {provider!r}"
        )
    config.provider = provider

    return config


def load_config(config_dir: str | Path | None = None) -> Config:
    """Load ``<config_dir>/config.toml`` (``config_dir`` defaults to
    ``~/.claude/token-lens``, honouring ``CLAUDE_CONFIG_DIR``). A missing
    file returns every-field-default :class:`Config`. Raises
    :class:`ConfigError` for a file that can't be read, isn't valid
    TOML, or has a field of the wrong shape/value.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "config.toml"
    data = _read_toml(path, what="config file")
    if data is None:
        return Config()
    return _build_config(data, path)


# -- sessions.toml --------------------------------------------------------


def _parse_session_entry(entry: dict) -> dict:
    override: dict = {}
    mode = entry.get("mode")
    if isinstance(mode, str):
        override["mode"] = mode
    purpose = entry.get("purpose")
    if isinstance(purpose, str):
        override["purpose"] = purpose
    tags = entry.get("tags")
    if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
        override["tags"] = list(tags)
    return override


def load_session_overrides(config_dir: str | Path | None = None) -> dict[str, dict]:
    """Load ``<config_dir>/sessions.toml``: ``session_id -> {mode?,
    purpose?, tags?}``. A missing file returns ``{}``. Raises
    :class:`ConfigError` for a file that can't be read or isn't valid
    TOML — same posture as :func:`load_config`, so a corrupted overrides
    file is surfaced rather than silently ignored (which would silently
    drop a user's classification corrections).
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "sessions.toml"
    data = _read_toml(path, what="session overrides file")
    if data is None:
        return {}

    sessions_raw = data.get("sessions")
    if not isinstance(sessions_raw, dict):
        return {}

    result: dict[str, dict] = {}
    for session_id, entry in sessions_raw.items():
        if not isinstance(entry, dict):
            continue
        override = _parse_session_entry(entry)
        if override:
            result[str(session_id)] = override
    return result


def _toml_escape_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_string(value: str) -> str:
    return f'"{_toml_escape_string(value)}"'


def _toml_format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_format_value(item) for item in value) + "]"
    raise ConfigError(f"cannot serialise value of type {type(value).__name__} to TOML: {value!r}")


def _write_sessions_toml(path: Path, sessions_table: dict[str, dict]) -> None:
    lines: list[str] = []
    for session_id, entry in sessions_table.items():
        if not isinstance(entry, dict) or not entry:
            continue
        lines.append(f"[sessions.{_toml_string(session_id)}]")
        for key, value in entry.items():
            lines.append(f"{key} = {_toml_format_value(value)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip("\n") + "\n" if lines else ""
    path.write_text(text, encoding="utf-8")


def save_session_override(
    config_dir: str | Path | None,
    session_id: str,
    mode: str | None = None,
    purpose: str | None = None,
) -> None:
    """Set ``mode``/``purpose`` for ``session_id`` in
    ``<config_dir>/sessions.toml``, rewriting the whole file but
    preserving every other session's entry (and any other key already
    present on this session's own entry, e.g. ``tags``) untouched.
    Passing ``mode=None``/``purpose=None`` (the default) leaves that
    field as it already was — it does not clear it.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "sessions.toml"

    sessions_table: dict[str, dict] = {}
    data = _read_toml(path, what="session overrides file")
    if data is not None:
        raw_sessions = data.get("sessions")
        if isinstance(raw_sessions, dict):
            for sid, entry in raw_sessions.items():
                if isinstance(entry, dict):
                    sessions_table[str(sid)] = dict(entry)

    entry = dict(sessions_table.get(session_id, {}))
    if mode is not None:
        entry["mode"] = mode
    if purpose is not None:
        entry["purpose"] = purpose
    sessions_table[session_id] = entry

    _write_sessions_toml(path, sessions_table)


__all__ = [
    "TOKEN_LENS_DIRNAME",
    "ConfigError",
    "Config",
    "load_config",
    "load_session_overrides",
    "save_session_override",
]
