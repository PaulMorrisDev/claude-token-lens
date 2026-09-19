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
#: v0.3 addition (``init``/``apply`` milestone): where a profile/settings
#: change is written by default -- see ``profiles/diff.py``'s
#: ``render_unified_diff(scope=...)``, which this string is passed
#: straight through to.
_ALLOWED_APPLY_SCOPE = frozenset({"user", "project-local", "repo"})
#: v0.3 addition: whether a project directory is work or personal --
#: ``exclude_projects`` already excludes a slug from discovery entirely,
#: this is a softer per-project label ``init`` records when the user says
#: a project is personal without necessarily wanting it excluded outright.
_ALLOWED_PROJECT_KIND = frozenset({"work", "personal"})


class ConfigError(Exception):
    """A config or session-overrides file could not be read or is not
    valid TOML/structure. Written to stand alone as user-facing output,
    same convention as ``pricing.PricingError``.
    """


@dataclass(slots=True)
class ProjectConfig:
    """Parsed ``<config_dir>/projects/<slug>.toml`` (v0.3 ``init``
    milestone): per-project answers to the "Asked, not guessed" questions
    the plan's "Configuration layers" section names, for a project whose
    answer differs from the global ``config.toml`` default. Every field is
    ``None`` when not set in the file, meaning "fall back to the global
    ``Config`` field of the same name".
    """

    #: "work" | "personal" | None.
    kind: str | None = None
    #: Whether this project's agents/skills are shared with colleagues
    #: (drives the default ``apply_scope`` for this project specifically).
    shared_project_config: bool | None = None
    #: Whether the user launches Claude Code in this project with
    #: ``--settings``/``CLAUDE_CONFIG_DIR`` overlays rather than the
    #: project's own settings files.
    launch_overlays: bool | None = None
    #: "user" | "project-local" | "repo" | None.
    apply_scope: str | None = None


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
    #: v0.3 ``init``/``baseline`` addition: the length in days of the
    #: "onboarding capture window" started by ``init`` (plan "Running on
    #: other people's machines" / "Asked, not guessed" sections) — the
    #: minimum corpus age ``baseline`` treats as non-provisional. ``None``
    #: means no capture window has been started (``init`` has not run, or
    #: this config predates v0.3).
    capture_window: int | None = None
    #: ISO-8601 UTC timestamp (``datetime.isoformat()``) of the moment
    #: ``init`` started the capture window, or ``None`` if not started.
    #: A plain string (not parsed to ``datetime``) so a malformed value
    #: never fails config loading — ``baseline.capture_status`` is the
    #: one place that parses it, and reports rather than raises on a bad
    #: value, the same "never crash on a foreign shape" posture the
    #: module docstring already describes for ``sessions.toml``.
    capture_started: str | None = None
    #: Whether the user launches Claude Code with ``--settings``/
    #: ``CLAUDE_CONFIG_DIR`` overlays rather than each project's own
    #: settings files, as a global default (overridable per project via
    #: :class:`ProjectConfig.launch_overlays`).
    launch_overlays: bool = False
    #: Whether project-level agents/skills are shared with colleagues
    #: (e.g. committed to a shared repo), as a global default
    #: (overridable per project via
    #: :class:`ProjectConfig.shared_project_config`).
    shared_project_config: bool = False
    #: Default scope a profile ``apply`` writes to when neither the CLI
    #: nor a project's own :class:`ProjectConfig.apply_scope` says
    #: otherwise. One of ``_ALLOWED_APPLY_SCOPE``.
    apply_scope: str = "user"
    #: Per-project answers, keyed by project slug (``discovery.slug_for``)
    #: — see :class:`ProjectConfig`. Loaded from
    #: ``<config_dir>/projects/<slug>.toml`` by :func:`load_config`.
    projects: dict[str, ProjectConfig] = field(default_factory=dict)

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
        if self.capture_window is not None:
            lines.append(f"capture_window: {self.capture_window}")
        if self.capture_started is not None:
            lines.append(f"capture_started: {self.capture_started}")
        if self.launch_overlays:
            lines.append(f"launch_overlays: {self.launch_overlays}")
        if self.shared_project_config:
            lines.append(f"shared_project_config: {self.shared_project_config}")
        if self.apply_scope != "user":
            lines.append(f"apply_scope: {self.apply_scope}")
        if self.projects:
            lines.append(f"projects: {sorted(self.projects)}")
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

    capture_window = data.get("capture_window")
    if capture_window is not None and (
        not isinstance(capture_window, int) or isinstance(capture_window, bool)
    ):
        raise ConfigError(f"config file {path}: 'capture_window' must be an integer")
    config.capture_window = capture_window

    capture_started = data.get("capture_started")
    if capture_started is not None and not isinstance(capture_started, str):
        raise ConfigError(f"config file {path}: 'capture_started' must be a string")
    config.capture_started = capture_started

    launch_overlays = data.get("launch_overlays", config.launch_overlays)
    if not isinstance(launch_overlays, bool):
        raise ConfigError(f"config file {path}: 'launch_overlays' must be a boolean")
    config.launch_overlays = launch_overlays

    shared_project_config = data.get("shared_project_config", config.shared_project_config)
    if not isinstance(shared_project_config, bool):
        raise ConfigError(f"config file {path}: 'shared_project_config' must be a boolean")
    config.shared_project_config = shared_project_config

    apply_scope = data.get("apply_scope", config.apply_scope)
    if not isinstance(apply_scope, str) or apply_scope not in _ALLOWED_APPLY_SCOPE:
        raise ConfigError(
            f"config file {path}: 'apply_scope' must be one of {sorted(_ALLOWED_APPLY_SCOPE)}, got {apply_scope!r}"
        )
    config.apply_scope = apply_scope

    return config


def _build_project_config(data: dict, path: Path) -> ProjectConfig:
    project = ProjectConfig()

    kind = data.get("kind")
    if kind is not None and (not isinstance(kind, str) or kind not in _ALLOWED_PROJECT_KIND):
        raise ConfigError(
            f"project config {path}: 'kind' must be one of {sorted(_ALLOWED_PROJECT_KIND)}, got {kind!r}"
        )
    project.kind = kind

    shared_project_config = data.get("shared_project_config")
    if shared_project_config is not None and not isinstance(shared_project_config, bool):
        raise ConfigError(f"project config {path}: 'shared_project_config' must be a boolean")
    project.shared_project_config = shared_project_config

    launch_overlays = data.get("launch_overlays")
    if launch_overlays is not None and not isinstance(launch_overlays, bool):
        raise ConfigError(f"project config {path}: 'launch_overlays' must be a boolean")
    project.launch_overlays = launch_overlays

    apply_scope = data.get("apply_scope")
    if apply_scope is not None and (
        not isinstance(apply_scope, str) or apply_scope not in _ALLOWED_APPLY_SCOPE
    ):
        raise ConfigError(
            f"project config {path}: 'apply_scope' must be one of {sorted(_ALLOWED_APPLY_SCOPE)}, got {apply_scope!r}"
        )
    project.apply_scope = apply_scope

    return project


def load_project_configs(config_dir: str | Path | None = None) -> dict[str, ProjectConfig]:
    """Load every ``<config_dir>/projects/<slug>.toml`` file into
    ``slug -> ProjectConfig`` (keyed by filename stem). A missing
    ``projects/`` directory returns ``{}``. Raises :class:`ConfigError`
    for a file that can't be read, isn't valid TOML, or has a field of
    the wrong shape/value — same posture as :func:`load_config`.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    projects_dir = resolved_dir / "projects"
    if not projects_dir.is_dir():
        return {}

    result: dict[str, ProjectConfig] = {}
    for toml_path in sorted(projects_dir.glob("*.toml")):
        data = _read_toml(toml_path, what="project config file")
        if data is None:
            continue
        result[toml_path.stem] = _build_project_config(data, toml_path)
    return result


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
    config = Config() if data is None else _build_config(data, path)
    config.projects = load_project_configs(resolved_dir)
    return config


def save_project_config(
    config_dir: str | Path | None, slug: str, project_config: ProjectConfig
) -> Path:
    """Write ``<config_dir>/projects/<slug>.toml`` from
    ``project_config``, overwriting any existing file for that slug
    whole (unlike :func:`save_session_override`, a project's answers are
    a handful of scalars set once by ``init``, not an incrementally
    merged table, so there is no "preserve other keys" concern here).
    Only fields that are not ``None`` are written. Returns the path
    written.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    projects_dir = resolved_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    path = projects_dir / f"{slug}.toml"

    lines: list[str] = []
    if project_config.kind is not None:
        lines.append(f"kind = {_toml_format_value(project_config.kind)}")
    if project_config.shared_project_config is not None:
        lines.append(f"shared_project_config = {_toml_format_value(project_config.shared_project_config)}")
    if project_config.launch_overlays is not None:
        lines.append(f"launch_overlays = {_toml_format_value(project_config.launch_overlays)}")
    if project_config.apply_scope is not None:
        lines.append(f"apply_scope = {_toml_format_value(project_config.apply_scope)}")

    text = "\n".join(lines) + "\n" if lines else ""
    path.write_text(text, encoding="utf-8")
    return path


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


# -- config.toml writer (v0.3 ``init``) ------------------------------------
#
# ``tomllib`` has no writer (see module docstring), and unlike
# ``sessions.toml`` (a flat table of per-session tables, handled above by
# ``_write_sessions_toml``), ``config.toml`` is a flat document of
# top-level scalars/lists plus at most one level of nested table
# (``[thresholds]``/``[recache]``) -- so it needs its own small dumper
# rather than reusing ``_write_sessions_toml``'s shape.


def _dump_toml_table(data: dict) -> str:
    """Serialise a dict of TOML-safe values to ``config.toml`` text.
    Top-level scalars/lists are written first, then one ``[section]``
    block per top-level dict value (``thresholds``/``recache``'s shape).
    Raises :class:`ConfigError` for a table nested inside a table (no
    ``config.toml`` field needs that) -- the caller uses this as the
    signal to fall back to a ``.toml.new`` file rather than overwrite the
    real one with a lossy dump.
    """
    top_lines: list[str] = []
    table_blocks: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            block = [f"[{key}]"]
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict):
                    raise ConfigError(
                        f"cannot serialise nested table under '{key}.{sub_key}' to TOML "
                        "(only one level of table nesting is supported)"
                    )
                block.append(f"{sub_key} = {_toml_format_value(sub_value)}")
            table_blocks.append("\n".join(block))
        else:
            top_lines.append(f"{key} = {_toml_format_value(value)}")
    parts = [part for part in ("\n".join(top_lines), *table_blocks) if part]
    return ("\n\n".join(parts) + "\n") if parts else ""


def write_config_values(config_dir: str | Path | None, updates: dict) -> Path:
    """Merge ``updates`` into ``<config_dir>/config.toml`` (a top-level
    dict value in ``updates`` is merged key-by-key into the existing
    table of the same name, e.g. updating one ``thresholds`` key leaves
    the others alone; anything else replaces the existing top-level key
    outright). The merged result is validated the same way
    :func:`load_config` validates a file read from disk (via
    :func:`_build_config`) before anything is written, so a bad update
    never corrupts the file. If the merged config's shape can't be
    round-tripped by :func:`_dump_toml_table` (a table nested inside a
    table), the existing ``config.toml`` is left untouched and the full
    merged config is written to ``config.toml.new`` instead, for the
    user to reconcile by hand -- the ".new fallback" this module's docs
    describe. Returns the path actually written.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "config.toml"
    existing = _read_toml(path, what="config file") or {}

    merged = dict(existing)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged_sub = dict(merged[key])
            merged_sub.update(value)
            merged[key] = merged_sub
        else:
            merged[key] = value

    # Validate before writing anything -- raises ConfigError on a bad
    # value/shape, same as loading a hand-edited file would.
    _build_config(merged, path)

    resolved_dir.mkdir(parents=True, exist_ok=True)
    try:
        text = _dump_toml_table(merged)
    except ConfigError:
        new_path = resolved_dir / "config.toml.new"
        new_path.write_text(_dump_toml_table({k: v for k, v in merged.items() if not isinstance(v, dict)}), encoding="utf-8")
        return new_path
    path.write_text(text, encoding="utf-8")
    return path


__all__ = [
    "TOKEN_LENS_DIRNAME",
    "ConfigError",
    "Config",
    "ProjectConfig",
    "load_config",
    "load_project_configs",
    "save_project_config",
    "write_config_values",
    "load_session_overrides",
    "save_session_override",
]
