"""Tests for the v0.3 ``init`` milestone's additions to
``src/claude_token_lens/config.py``: :class:`ProjectConfig`,
``<config_dir>/projects/<slug>.toml`` loading/writing, the new
top-level ``Config`` fields, and the generic ``config.toml`` writer
(:func:`write_config_values`/``_dump_toml_table``) with its ``.toml.new``
fallback.
"""

from __future__ import annotations

import pytest

from claude_token_lens import config as config_module
from claude_token_lens.config import (
    Config,
    ConfigError,
    ProjectConfig,
    load_config,
    load_project_configs,
    save_project_config,
    write_config_values,
)


# --------------------------------------------------------------------
# Config: new top-level fields
# --------------------------------------------------------------------


def test_new_top_level_fields_default_sensibly():
    config = Config()
    assert config.capture_window is None
    assert config.capture_started is None
    assert config.launch_overlays is False
    assert config.shared_project_config is False
    assert config.apply_scope == "user"
    assert config.projects == {}


def test_load_config_parses_new_top_level_fields(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text(
        "capture_window = 14\n"
        'capture_started = "2026-09-01T00:00:00+00:00"\n'
        "launch_overlays = true\n"
        "shared_project_config = true\n"
        'apply_scope = "repo"\n',
        encoding="utf-8",
    )
    config = load_config(config_dir=token_lens_dir)
    assert config.capture_window == 14
    assert config.capture_started == "2026-09-01T00:00:00+00:00"
    assert config.launch_overlays is True
    assert config.shared_project_config is True
    assert config.apply_scope == "repo"


def test_invalid_apply_scope_raises(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text('apply_scope = "somewhere-else"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_dir=token_lens_dir)


# --------------------------------------------------------------------
# ProjectConfig / load_project_configs / save_project_config
# --------------------------------------------------------------------


def test_load_project_configs_missing_directory_returns_empty(tmp_path):
    assert load_project_configs(config_dir=tmp_path / "token-lens") == {}


def test_save_and_load_project_config_round_trips(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    project = ProjectConfig(
        kind="work", shared_project_config=True, launch_overlays=False, apply_scope="project-local"
    )
    path = save_project_config(token_lens_dir, "my-slug", project)
    assert path == token_lens_dir / "projects" / "my-slug.toml"
    assert path.is_file()

    loaded = load_project_configs(token_lens_dir)
    assert loaded == {"my-slug": project}


def test_save_project_config_only_writes_non_none_fields(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    project = ProjectConfig(kind="personal")
    path = save_project_config(token_lens_dir, "slug-a", project)
    text = path.read_text(encoding="utf-8")
    assert "kind" in text
    assert "shared_project_config" not in text
    assert "launch_overlays" not in text
    assert "apply_scope" not in text

    loaded = load_project_configs(token_lens_dir)
    assert loaded["slug-a"] == ProjectConfig(kind="personal")


def test_load_project_configs_reads_every_file_keyed_by_stem(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    save_project_config(token_lens_dir, "proj-a", ProjectConfig(kind="work"))
    save_project_config(token_lens_dir, "proj-b", ProjectConfig(kind="personal"))

    loaded = load_project_configs(token_lens_dir)
    assert set(loaded) == {"proj-a", "proj-b"}
    assert loaded["proj-a"].kind == "work"
    assert loaded["proj-b"].kind == "personal"


def test_invalid_project_kind_raises(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    projects_dir = token_lens_dir / "projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "bad.toml").write_text('kind = "hobby"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_project_configs(token_lens_dir)


def test_load_config_populates_projects(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    save_project_config(token_lens_dir, "proj-a", ProjectConfig(kind="work"))

    config = load_config(config_dir=token_lens_dir)
    assert "proj-a" in config.projects
    assert config.projects["proj-a"].kind == "work"


# --------------------------------------------------------------------
# write_config_values
# --------------------------------------------------------------------


def test_write_config_values_creates_file_from_scratch(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    path = write_config_values(token_lens_dir, {"billing": "subscription"})
    assert path == token_lens_dir / "config.toml"
    config = load_config(config_dir=token_lens_dir)
    assert config.billing == "subscription"


def test_write_config_values_preserves_untouched_top_level_keys(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    write_config_values(token_lens_dir, {"billing": "subscription", "min_sessions": 10})
    write_config_values(token_lens_dir, {"min_turns": 500})

    config = load_config(config_dir=token_lens_dir)
    assert config.billing == "subscription"
    assert config.min_sessions == 10
    assert config.min_turns == 500


def test_write_config_values_merges_nested_table_key_by_key(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    write_config_values(token_lens_dir, {"thresholds": {"a": 1}})
    write_config_values(token_lens_dir, {"thresholds": {"b": 2}})

    config = load_config(config_dir=token_lens_dir)
    assert config.thresholds == {"a": 1, "b": 2}


def test_write_config_values_rejects_an_invalid_merged_value(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    with pytest.raises(ConfigError):
        write_config_values(token_lens_dir, {"billing": "not-a-real-mode"})
    # Nothing was written -- validation happens before any write.
    assert not (token_lens_dir / "config.toml").exists()


def test_write_config_values_falls_back_to_dot_new_for_unsupported_shape(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir(parents=True)
    # Seed an existing config.toml so the fallback path's "leave the
    # existing file untouched" guarantee is actually exercised.
    (token_lens_dir / "config.toml").write_text('billing = "api"\n', encoding="utf-8")

    path = write_config_values(
        token_lens_dir, {"thresholds": {"nested": {"too": "deep"}}}
    )
    assert path == token_lens_dir / "config.toml.new"
    assert path.is_file()
    # The real config.toml is untouched.
    assert (token_lens_dir / "config.toml").read_text(encoding="utf-8") == 'billing = "api"\n'


def test_write_config_values_round_trips_through_load_config(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    write_config_values(
        token_lens_dir,
        {
            "billing": "subscription",
            "capture_window": 7,
            "capture_started": "2026-09-19T00:00:00+00:00",
            "launch_overlays": True,
            "shared_project_config": True,
            "apply_scope": "repo",
            "exclude_projects": ["work-thing"],
        },
    )
    config = load_config(config_dir=token_lens_dir)
    assert config.billing == "subscription"
    assert config.capture_window == 7
    assert config.capture_started == "2026-09-19T00:00:00+00:00"
    assert config.launch_overlays is True
    assert config.shared_project_config is True
    assert config.apply_scope == "repo"
    assert config.exclude_projects == ["work-thing"]


def test_write_config_values_escapes_control_characters_and_round_trips(tmp_path):
    """SEC-P5: a value with control characters (an embedded newline, a
    tab, a carriage return, a quote, a backslash, and a C0 byte with no
    short escape of its own) must not corrupt config.toml -- each one
    becomes an escape sequence on the same physical line as the rest of
    the value, and load_config reads the exact original string back.
    """
    token_lens_dir = tmp_path / "token-lens"
    tricky = 'line one\nline two\ttabbed\r"quoted"\\backslash\x01\x7fend'
    write_config_values(token_lens_dir, {"pricing_path": tricky})

    lines = (token_lens_dir / "config.toml").read_text(encoding="utf-8").splitlines()
    pricing_lines = [line for line in lines if line.startswith("pricing_path")]
    # A raw control character in the value would have split it across
    # physical lines (or worse); escaped, it's exactly one.
    assert len(pricing_lines) == 1
    line = pricing_lines[0]
    assert "\\n" in line and "\\t" in line and "\\r" in line
    assert '\\"quoted\\"' in line
    assert "\\\\backslash" in line
    assert "\\u0001" in line and "\\u007f" in line

    config = load_config(config_dir=token_lens_dir)
    assert config.pricing_path == tricky


def test_write_atomic_verify_toml_refuses_to_replace_the_file_with_bad_toml(tmp_path):
    """SEC-P5: re-parses before replacing. A would-be writer bug that
    produced text that isn't valid TOML must never reach the real file
    -- _write_atomic(verify_toml=True) catches it first and leaves the
    existing file (and no stray temp file) behind."""
    path = tmp_path / "config.toml"
    path.write_text('billing = "api"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="does not parse back"):
        config_module._write_atomic(path, 'billing = "unterminated\n', verify_toml=True)

    assert path.read_text(encoding="utf-8") == 'billing = "api"\n'
    assert list(path.parent.iterdir()) == [path]
