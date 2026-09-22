

def test_profile_prompt_lists_changed_unmanaged_keys_with_their_files():
    from claude_token_lens.fixes import profile_prompt

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
    from claude_token_lens.fixes import profile_prompt

    text = profile_prompt("Same", [{"key": "settings.model", "current_value": "x", "proposed_value": "x"}], "user")
    assert "nothing to change" in text
