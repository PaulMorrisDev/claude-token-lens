"""Quick actions: every check answers, and its fixes keep the fix
contract (``quick_actions``)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from claude_token_lens import quick_actions as qa
from claude_token_lens.model import Recommendation
from claude_token_lens.units import Units

from test_whatif import _model, _table

UNITS = Units(billing_mode="api", currency="USD")
FIX_KEYS = {"key", "agent", "explainer", "command", "command_warning", "prompt", "title"}


def _ctx(tmp_path, model=None, **kw):
    config_dir = tmp_path / ".claude" / "token-lens"
    config_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "projects").mkdir(exist_ok=True)
    return qa.Context(
        model=model if model is not None else _full_model(),
        units=UNITS,
        period="over the last 14 days",
        config_dir=config_dir,
        effective=kw.get("effective", {}),
        effective_agents=kw.get("effective_agents", {}),
    )


def _full_model():
    model = _model()
    swap = model.sections[0].tables[0]
    swap.columns += [NS(key="observed_model"), NS(key="best_cheaper_alternative_model"), NS(key="saving_usd"),
                     NS(key="saving_pct")]
    swap.rows[0] += ["claude-opus-4-7", "claude-sonnet-4-5", 40.0, 40.0]
    swap.rows[1] += ["claude-sonnet-4-5", "claude-haiku-4-5-20251001", 8.0, 80.0]
    model.sections[4].tables[0].columns.append(NS(key="output_tokens"))
    model.sections[4].tables[0].rows[0].append(1000)
    model.sections.append(NS(key="carry", tables=[_table("carry_by_tool", [
        {"key": "Bash", "result_count": 10, "tokens_entered": 5000, "mean_turns_carried": 30, "carry_cost_usd": 8.0},
        {"key": "Read", "result_count": 10, "tokens_entered": 5000, "mean_turns_carried": 30, "carry_cost_usd": 2.0},
    ])]))
    model.sections.append(NS(key="waste", tables=[_table("waste_by_cause", [
        {"cause": "tool-error", "turns": 3, "cost_usd": 1.0, "lever": "Check paths first."},
    ])]))
    model.recommendations = [
        Recommendation(id="long-tool-waits", title="Tools often wait on you", action="Pre-approve routine tools."),
    ]
    return model


def test_every_check_answers_with_a_valid_status_and_fix_contract(tmp_path):
    ctx = _ctx(tmp_path)
    for check_id in qa.CHECK_IDS:
        result = qa.run(check_id, ctx)
        assert result["status"] in ("act", "ok", "no_data"), check_id
        assert result["summary"], check_id
        for fix in result["fixes"]:
            assert FIX_KEYS <= set(fix), (check_id, fix)
            assert fix["prompt"] and fix["title"], check_id


def test_an_empty_report_is_no_data_everywhere_but_never_fails(tmp_path):
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={}, recommendations=[]))
    statuses = {row["id"]: row["status"] for row in qa.run_all(ctx)}
    assert set(statuses) == set(qa.CHECK_IDS)
    assert statuses["models"] == statuses["cache"] == statuses["tool-output"] == "no_data"


def test_models_check_offers_a_fix_per_cheaper_model_with_a_dry_run_command(tmp_path):
    result = qa.run("models", _ctx(tmp_path, effective_agents={"Explore": {}}))
    assert result["status"] == "act"
    titles = [fix["title"] for fix in result["fixes"]]
    assert titles == ["Main session: use sonnet", "Explore: use haiku"]
    explore = result["fixes"][1]
    assert explore["command"].endswith("--dry-run") and "--agent Explore" in explore["command"]
    assert any("Saves 8.00 USD" in text for _h, text in explore["explainer"])
    assert result["table"]["rows"][0][0] == "Main session"


def test_tool_output_offers_the_bash_cap_as_a_prompt_only(tmp_path):
    result = qa.run("tool-output", _ctx(tmp_path))
    [fix] = result["fixes"]
    assert fix["key"] == "BASH_MAX_OUTPUT_LENGTH" and fix["command"] is None
    assert '"env"' in fix["prompt"] and "diff" in fix["prompt"]


def test_habits_are_tips_not_settings(tmp_path):
    result = qa.run("habits", _ctx(tmp_path))
    assert result["status"] == "act"
    assert result["tips"] == [{"title": "Tools often wait on you", "text": "Pre-approve routine tools."}]


def test_markdown_carries_the_table_tips_and_fixes(tmp_path):
    text = qa.render_markdown(qa.run("models", _ctx(tmp_path)))
    assert text.startswith("## Is each agent on the cheapest model")
    assert "| Agent |" in text and "### Explore: use haiku" in text and "```bash" in text


def test_unknown_check_raises(tmp_path):
    with pytest.raises(KeyError):
        qa.run("nope", _ctx(tmp_path))
