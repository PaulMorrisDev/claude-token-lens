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
    assert statuses["models"] == statuses["cache"] == statuses["tool-output"] == statuses["quality"] == "no_data"


def test_files_on_disk_without_transcript_records_are_no_data_not_ok(tmp_path):
    """A skill or CLAUDE.md file on disk that no session in the window
    recorded says "not enough data", never "nothing to do"."""
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={}, recommendations=[]))
    claude_root = ctx.config_dir.parent
    (claude_root / "CLAUDE.md").write_text("# Rules\n\nBe brief.\n", encoding="utf-8")
    skill = claude_root / "skills" / "tidy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tidy\ndescription: Tidy things.\n---\n", encoding="utf-8")
    for check_id in ("skills", "claude-md"):
        result = qa.run(check_id, ctx)
        assert result["status"] == "no_data", (check_id, result["summary"])
        assert "over the last 14 days" in result["summary"]


def test_skills_check_keeps_a_skill_a_claude_code_tool_loads_out_of_the_hide_list(tmp_path):
    def usage(name):
        return {"name": name, "listing_tokens": 40, "listed": {"main": 5}, "listing_cost_usd": 0.5}

    skills = [usage("artifact-capabilities"), usage("keybindings-help"), usage("loop")]
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={"skills": skills}, recommendations=[]))
    result = qa.run("skills", ctx)
    assert result["status"] == "act"
    assert [row[0] for row in result["table"]["rows"]] == ["keybindings-help", "loop"]
    hide_all, *rest = result["fixes"]
    assert "artifact-capabilities" not in hide_all["command"]
    assert rest[-1]["title"] == "artifact-capabilities: list it by name only"
    [tip] = result["tips"]
    assert tip["title"] == "artifact-capabilities: needed by the Artifact tool"


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


def _cap_row(usd_saved, carry_cost_usd):
    return {"setting": "BASH_MAX_OUTPUT_LENGTH", "value": "15000", "cap_tokens": 3750, "results": 10,
            "results_affected": 2, "tokens_saved": 5000, "usd_saved": usd_saved, "carry_cost_usd": carry_cost_usd}


def test_tool_output_prices_the_bash_cap_from_its_own_saving(tmp_path):
    model = _full_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables.append(_table("carry_output_cap_savings", [_cap_row(2.0, 8.0)]))
    [fix] = qa.run("tool-output", _ctx(tmp_path, model=model))["fixes"]
    effect = dict(fix["explainer"])["Expected effect"]
    assert "would have cut 2 of 10 results" in effect and "saved up to 2.00 USD" in effect


def test_tool_output_explains_a_bash_cap_that_would_save_little(tmp_path):
    model = _full_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables.append(_table("carry_output_cap_savings", [_cap_row(0.3, 8.0)]))
    result = qa.run("tool-output", _ctx(tmp_path, model=model))
    assert result["fixes"] == []
    tip = next(t for t in result["tips"] if t["title"].startswith("BASH_MAX_OUTPUT_LENGTH"))
    assert "0.30 USD" in tip["text"] and "8.00 USD" in tip["text"]


def test_compaction_says_the_summary_point_is_already_set_when_it_is(tmp_path):
    """The last column is against the sessions as they ran, so the row
    for the window already set can read cheaper than 0%."""
    model = NS(sections=[NS(key="compaction_sim", tables=[_table("compaction_sim_by_window", [
        {"window": 300000, "compactions_per_session": 2.0, "cost": 75.0, "delta_pct": -25.0},
        {"window": "none", "compactions_per_session": 1.0, "cost": 100.0, "delta_pct": 0.0},
    ])])], context_files={}, recommendations=[])
    result = qa.run("compaction", _ctx(tmp_path, model=model, effective={"autoCompactWindow": 300000}))
    assert result["status"] == "ok" and result["fixes"] == []
    assert result["summary"].startswith("You already summarise at 300,000 tokens")
    assert "at most 2 times a session" in result["summary"]
    assert qa.run("compaction", _ctx(tmp_path, model=model))["status"] == "act"


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


def _quality_model(agents: list[dict], setups: list[dict] | None = None, failing: list[dict] | None = None):
    return NS(sections=[NS(key="quality", tables=[
        _table("quality_by_agent", agents),
        _table("quality_by_setup", setups or []),
        _table("quality_failing_tools", failing or []),
    ])], recommendations=[], context_files={})


_AGENT = {"agent_type": "claude-implementer", "runs": 40, "unfinished_pct": 30.0, "turn_limit_pct": 20.0,
          "tool_errors_pct": 2.0, "shell_errors_pct": 3.0, "corrections_pct": None, "max_tokens_pct": 0.0}
_WORSE = {"agent_type": "claude-implementer", "model": "claude-haiku-4-5-20251001", "effort": "high", "runs": 20,
          "setup_verdict": "worse",
          "difference": "Lower: replies per run 14.5 against 91.4; Worse: tool calls that failed 8.4% against 2.2%.",
          "compared_model": "claude-sonnet-5", "compared_effort": "high"}
_MIXED = {**_WORSE, "setup_verdict": "mixed",
          "difference": "Better: agent runs that didn't finish 0% against 14%; Worse: tool calls that failed 8.4% against 2.2%."}


def test_quality_is_ok_when_nothing_stands_out(tmp_path):
    calm = {**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([calm])))
    assert result["status"] == "ok" and not result["fixes"]


def test_quality_flags_an_agent_that_runs_out_of_turns_with_a_tip(tmp_path):
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([_AGENT])))
    assert result["status"] == "act"
    assert result["table"]["rows"][0][-1] == "30% of runs didn't finish"
    assert result["summary"] == "1 agent often fails or doesn't finish."
    tip = result["tips"][0]
    assert tip["title"] == "claude-implementer: runs often don't finish"
    assert tip["text"].startswith("20% of its runs most likely ran out of turns")


def test_quality_ignores_agents_with_too_few_runs(tmp_path):
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([{**_AGENT, "runs": 3}])))
    assert result["status"] == "ok"


def test_a_worse_setup_offers_the_model_it_was_compared_with(tmp_path):
    calm = {**_AGENT, "unfinished_pct": 3.0}
    ctx = _ctx(tmp_path, model=_quality_model([calm], [_WORSE]),
               effective_agents={"claude-implementer": {"model": "haiku"}})
    result = qa.run("quality", ctx)
    assert result["status"] == "act"
    assert result["table"]["rows"][-1][-1] == (
        "On claude-haiku-4-5-20251001, effort high: Worse: tool calls that failed 8.4% against 2.2%."
    )
    fix = result["fixes"][0]
    assert FIX_KEYS <= set(fix)
    assert (fix["key"], fix["agent"], fix["title"]) == ("model", "claude-implementer", "claude-implementer: back to sonnet")
    assert "--dry-run" in fix["command"]
    # Going back up a tier costs more; it doesn't risk more replies.
    tradeoff = dict(fix["explainer"])["Trade-off"]
    assert tradeoff.startswith("A larger model costs more per token")


def test_a_mixed_setup_is_a_tip_not_a_switch(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [_MIXED]),
               effective_agents={"claude-implementer": {"model": "haiku"}})
    result = qa.run("quality", ctx)
    assert result["status"] == "ok" and not result["fixes"]
    [tip] = result["tips"]
    assert tip["title"] == "claude-implementer: mixed results on claude-haiku-4-5-20251001, effort high"
    assert "nothing to switch" in tip["text"]


def test_models_check_does_not_suggest_a_model_the_agent_did_worse_on(tmp_path):
    model = _full_model()
    model.sections.append(NS(key="quality", tables=[
        _table("quality_by_setup", [{**_WORSE, "agent_type": "Explore"}]),
    ]))
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert [fix["agent"] for fix in result["fixes"]] == [None]
    [tip] = result["tips"]
    assert tip["title"] == "Explore: haiku not suggested"
    assert "it did worse than on claude-sonnet-5" in tip["text"]


def test_a_project_agents_fix_edits_the_projects_agent_file(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [_WORSE]),
               effective_agents={"claude-implementer": {"source": "project", "model": "haiku"}})
    fix = qa.run("quality", ctx)["fixes"][0]
    assert "--scope repo --project-dir ." in fix["command"]
    assert "In .claude/agents/claude-implementer.md" in fix["prompt"]


def test_a_worse_setup_no_longer_in_use_is_a_tip_not_a_fix(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [_WORSE]),
               effective_agents={"claude-implementer": {"model": "sonnet"}})
    result = qa.run("quality", ctx)
    assert not result["fixes"]
    assert "no longer uses that setup" in result["tips"][0]["text"]


def test_the_main_session_doing_worse_is_a_tip(tmp_path):
    main = {**_WORSE, "agent_type": "(main session)"}
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [main])))
    assert not result["fixes"] and result["tips"][0]["title"] == "Main session did worse on claude-haiku-4-5-20251001"
