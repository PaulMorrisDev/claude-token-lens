

def test_profile_prompt_lists_changed_unmanaged_keys_with_their_files():
    from claudeglass.fixes import profile_prompt

    rows = [
        {"key": "settings.model", "current_value": "opus", "proposed_value": "sonnet"},
        {"key": "settings.effortLevel", "current_value": "high", "proposed_value": "high"},
        {"key": "settings.promptCacheTtl", "current_value": None, "proposed_value": "1h", "managed": True},
        {"key": "agents.reviewer.effort", "current_value": None, "proposed_value": "low"},
        {"key": "env.MAX_THINKING_TOKENS", "current_value": None, "proposed_value": "8000"},
        {"key": "agents.reviewer.experimental.cacheTtl", "current_value": None, "proposed_value": "1h"},
    ]
    text = profile_prompt("Lean", rows, "repo")
    assert 'In .claude/settings.json, set model to "sonnet" (now: opus).' in text
    assert "effortLevel" not in text and "promptCacheTtl" not in text
    assert 'In .claude/agents/reviewer.md, set effort in the frontmatter to "low" (now: not set).' in text
    assert "environment variable MAX_THINKING_TOKENS" in text
    assert "everyone who works in this project" in text
    assert 'set experimental.cacheTtl in the frontmatter to "1h"' in text


def test_profile_prompt_says_so_when_nothing_changes():
    from claudeglass.fixes import profile_prompt

    text = profile_prompt("Same", [{"key": "settings.model", "current_value": "x", "proposed_value": "x"}], "user")
    assert "nothing to change" in text


def test_build_fixes_gives_an_actionable_workflow_rule_a_where_trade_off_undo_explainer_and_a_prompt():
    """UX-8 (rest): a recommendation with no ``SettingChange`` (pure
    workflow advice) used to get either a prompt-only fix with an empty
    ``explainer`` (the three ids in the old ``_WORKFLOW_PROMPTS``) or no
    fix at all (every other id) -- neither carried a where/trade-off/undo
    entry. Every id now gets one via ``_WORKFLOW_EXPLAINER``."""
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="batch-instructions",
        severity="advice",
        category="workflow",
        title="Queued instructions are re-writing the cache prefix",
        action="Batch queued instructions into a single message.",
        lever=None,
    )
    fixes = build_fixes(rec)
    assert len(fixes) == 1
    fix = fixes[0]
    headings = [pair[0] for pair in fix["explainer"]]
    assert headings == ["Where and who it affects", "Trade-off", "How to undo it"]
    assert fix["prompt"]  # a self-contained request Claude can act on
    assert "{" not in fix["prompt"]  # every placeholder was filled in


def test_build_fixes_gives_a_purely_informational_workflow_card_an_explainer_but_no_prompt():
    """cache-read-dominance, data-quality and window-budget propose no
    change at all -- they still get the explainer (stating so plainly),
    but ``prompt`` is empty: there is nothing to ask Claude to do."""
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="cache-read-dominance",
        severity="info",
        category="workflow",
        title="Cache reads already dominate spend",
        action="There is little further caching upside here.",
        lever=None,
    )
    fixes = build_fixes(rec)
    assert len(fixes) == 1
    assert fixes[0]["prompt"] == ""
    assert fixes[0]["explainer"]


def test_build_fixes_still_gives_nothing_for_an_id_with_no_workflow_entry_at_all():
    """A ``rec.id`` that appears in neither ``_WORKFLOW_EXPLAINER`` nor
    ``_WORKFLOW_PROMPTS`` gets no fix -- unchanged from before UX-8."""
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(id="not-a-real-id", title="x", action="x", lever=None)
    assert build_fixes(rec) == []


def test_render_fix_skips_the_ask_claude_block_for_an_empty_prompt():
    """UX-8's informational workflow cards have an explainer but no
    prompt (see above) -- ``_render_fix``/``_fix_html`` must not print an
    empty "Ask Claude to do it" code block for them."""
    from claudeglass.render.html import _fix_html
    from claudeglass.render.markdown import _render_fix

    fix = {"key": None, "agent": None, "explainer": [["Trade-off", "None."]], "command": None, "prompt": ""}
    md_lines = _render_fix(fix)
    assert not any("Ask Claude to do it" in line for line in md_lines)
    html = _fix_html(fix)
    assert "Ask Claude to do it" not in html
