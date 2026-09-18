"""Tests for WP5: ``config.toml``/``sessions.toml`` loading and
per-session override round-tripping (``src/claude_token_lens/config.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_token_lens.config import (
    Config,
    ConfigError,
    load_config,
    load_session_overrides,
    save_session_override,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "config"


# --------------------------------------------------------------------
# load_config: defaults
# --------------------------------------------------------------------


def test_missing_config_file_returns_defaults(tmp_path):
    config = load_config(config_dir=tmp_path / "does-not-exist")
    assert config == Config()
    assert config.billing == "api"
    assert config.tz is None
    assert config.thresholds == {}
    assert config.recache == {}
    assert config.min_sessions == 5
    assert config.min_turns == 200
    assert config.allow_titles is False
    assert config.pricing_path is None


def test_default_config_dir_honours_claude_config_dir_env_var(monkeypatch, tmp_path):
    # The autouse fixture in conftest.py already points CLAUDE_CONFIG_DIR
    # at a throwaway home; point it somewhere else explicitly here to
    # prove load_config (with no config_dir argument) actually reads it.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text('billing = "subscription"\n', encoding="utf-8")

    config = load_config()
    assert config.billing == "subscription"


# --------------------------------------------------------------------
# load_config: full parse
# --------------------------------------------------------------------


def test_valid_config_parses_every_field(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text(
        (FIXTURES / "config_valid.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    config = load_config(config_dir=token_lens_dir)
    assert config.billing == "subscription"
    assert config.tz == "Europe/London"
    assert config.min_sessions == 8
    assert config.min_turns == 150
    assert config.allow_titles is True
    assert config.pricing_path == "/custom/pricing.toml"
    assert config.thresholds == {"ctx_floor": 20000, "cr_ratio": 0.2}
    assert config.recache == {"full_expiry_cr": 2000, "huge_ctx": 200000}


def test_partial_config_falls_back_to_defaults_for_missing_fields(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text('min_sessions = 12\n', encoding="utf-8")

    config = load_config(config_dir=token_lens_dir)
    assert config.min_sessions == 12
    # Every other field keeps its documented default.
    assert config.billing == "api"
    assert config.tz is None
    assert config.min_turns == 200
    assert config.allow_titles is False


# --------------------------------------------------------------------
# load_config: malformed -> ConfigError
# --------------------------------------------------------------------


def test_malformed_toml_syntax_raises_config_error(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text(
        (FIXTURES / "config_bad.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(config_dir=token_lens_dir)


def test_invalid_billing_value_raises_config_error(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text('billing = "invoice"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="billing"):
        load_config(config_dir=token_lens_dir)


def test_wrong_type_field_raises_config_error(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text('min_sessions = "five"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="min_sessions"):
        load_config(config_dir=token_lens_dir)


def test_thresholds_must_be_a_table(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text('thresholds = "nope"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="thresholds"):
        load_config(config_dir=token_lens_dir)


def test_thresholds_classify_subdict_round_trips_as_a_nested_table(tmp_path):
    """Fix 5: classify.py reads its own overrides out of
    Config.thresholds["classify"]["mode"/"purpose"] (see
    classify.mode_and_purpose_thresholds_from_config) -- thresholds
    itself stays this module's plain free-form dict, so a nested TOML
    table under it just round-trips as a nested dict with no special
    parsing needed here.
    """
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "config.toml").write_text(
        "[thresholds.classify.mode]\n"
        "overnight_night_turn_share = 0.15\n"
        "\n"
        "[thresholds.classify.purpose]\n"
        "local_llm_min_hits = 1\n",
        encoding="utf-8",
    )

    config = load_config(config_dir=token_lens_dir)
    assert config.thresholds == {
        "classify": {
            "mode": {"overnight_night_turn_share": 0.15},
            "purpose": {"local_llm_min_hits": 1},
        }
    }

    from claude_token_lens.classify import mode_and_purpose_thresholds_from_config

    mode_t, purpose_t = mode_and_purpose_thresholds_from_config(config.thresholds)
    assert mode_t == {"overnight_night_turn_share": 0.15}
    assert purpose_t == {"local_llm_min_hits": 1}

    assert any("classify" in line for line in config.describe())


# --------------------------------------------------------------------
# Config.describe()
# --------------------------------------------------------------------


def test_describe_lists_every_field_as_readable_lines():
    config = Config(billing="subscription", tz="Europe/London", min_sessions=3)
    lines = config.describe()
    assert any("billing: subscription" in line for line in lines)
    assert any("Europe/London" in line for line in lines)
    assert any("min_sessions: 3" in line for line in lines)


def test_describe_shows_local_when_tz_unset():
    lines = Config().describe()
    assert any("local (machine)" in line for line in lines)


# --------------------------------------------------------------------
# load_session_overrides
# --------------------------------------------------------------------


def test_missing_sessions_file_returns_empty_dict(tmp_path):
    assert load_session_overrides(config_dir=tmp_path / "does-not-exist") == {}


def test_load_session_overrides_parses_mode_purpose_and_tags(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "sessions.toml").write_text(
        (FIXTURES / "sessions_seed.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    overrides = load_session_overrides(config_dir=token_lens_dir)
    assert overrides["session-existing-1"] == {"mode": "interactive", "purpose": "general-dev"}
    assert overrides["session-existing-2"] == {
        "mode": "overnight",
        "purpose": "refactor",
        "tags": ["billing", "urgent"],
    }


def test_malformed_sessions_toml_raises_config_error(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "sessions.toml").write_text('[sessions."x"\nmode = "interactive"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_session_overrides(config_dir=token_lens_dir)


# --------------------------------------------------------------------
# save_session_override
# --------------------------------------------------------------------


def test_save_session_override_creates_file_when_absent(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    save_session_override(token_lens_dir, "session-new", mode="interactive", purpose="review")

    overrides = load_session_overrides(config_dir=token_lens_dir)
    assert overrides == {"session-new": {"mode": "interactive", "purpose": "review"}}


def test_save_session_override_preserves_other_entries(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    token_lens_dir.mkdir()
    (token_lens_dir / "sessions.toml").write_text(
        (FIXTURES / "sessions_seed.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    save_session_override(token_lens_dir, "session-existing-1", mode="overnight")

    overrides = load_session_overrides(config_dir=token_lens_dir)
    # The touched entry only has "mode" overwritten; "purpose" stays.
    assert overrides["session-existing-1"] == {"mode": "overnight", "purpose": "general-dev"}
    # The untouched entry is byte-for-byte preserved, including "tags".
    assert overrides["session-existing-2"] == {
        "mode": "overnight",
        "purpose": "refactor",
        "tags": ["billing", "urgent"],
    }


def test_save_session_override_updates_only_the_given_fields(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    save_session_override(token_lens_dir, "session-x", mode="interactive", purpose="planning")
    save_session_override(token_lens_dir, "session-x", purpose="test-triage")

    overrides = load_session_overrides(config_dir=token_lens_dir)
    # mode was left as None on the second call, so it must be untouched.
    assert overrides["session-x"] == {"mode": "interactive", "purpose": "test-triage"}


def test_save_session_override_round_trips_special_characters(tmp_path):
    token_lens_dir = tmp_path / "token-lens"
    session_id = 'weird "id" with\\backslash'
    save_session_override(token_lens_dir, session_id, mode="mixed")

    overrides = load_session_overrides(config_dir=token_lens_dir)
    assert overrides[session_id] == {"mode": "mixed"}
