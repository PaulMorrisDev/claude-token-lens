"""Skills review: each listed skill's description (read from the newest
listing, never stored), where it comes from, how often it was listed
and used, and fixes for the unused and the long-winded
(``skills_review``)."""

from __future__ import annotations

import json
from pathlib import Path

from claude_token_lens import skills_review
from claude_token_lens.units import Units

from helpers import attachment_line, write_jsonl

UNITS = Units(billing_mode="api", currency="USD")
PERIOD = "over the last 30 days"


def _usage(name: str, *, listed: int, invoked: int = 0, tokens: int = 40, cost: float = 0.5) -> dict:
    return {
        "name": name,
        "listing_tokens": tokens,
        "listed": {"main": listed, "Explore": 2},
        "listing_cost_usd": cost,
        "invoked": invoked,
        "invoked_by": {"main": invoked} if invoked else {},
        "resent_tokens": 0,
        "resent_cost_usd": 0.0,
        "attributed_turns": 0,
        "attributed_cost_usd": 0.0,
        "last_seen": "2026-09-18T12:00:00Z",
    }


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "token-lens"
    config_dir.mkdir(parents=True)
    (claude_root / "skills" / "grill-me").mkdir(parents=True)
    (claude_root / "skills" / "grill-me" / "SKILL.md").write_text("---\nname: grill-me\n---\n", encoding="utf-8")
    project = tmp_path / "repo"
    (project / ".claude" / "workflows").mkdir(parents=True)
    (project / ".claude" / "workflows" / "qa-round.js").write_text("", encoding="utf-8")
    listing = attachment_line(
        "skill_listing",
        content=(
            "- grill-me: Interview the user about a plan.\n"
            "- impeccable:impeccable: Design: polish the UI.\n"
            "- qa-round: Adversarial QA sweep.\n"
            "- dataviz: Charts.\n"
        ),
        skillCount=4,
        names=["grill-me", "impeccable:impeccable", "qa-round", "dataviz"],
    )
    (claude_root / "projects" / "C--repo").mkdir(parents=True)
    write_jsonl(claude_root / "projects" / "C--repo" / "s1.jsonl", [listing])
    return config_dir, project


def _review(tmp_path: Path, rows: list[dict]) -> dict:
    config_dir, project = _setup(tmp_path)
    return skills_review.review(config_dir, {"skills": rows}, UNITS, PERIOD, projects=[project])


def test_descriptions_are_read_from_the_newest_listing(tmp_path):
    config_dir, _project = _setup(tmp_path)
    texts = skills_review.descriptions(config_dir.parent)
    assert texts["impeccable:impeccable"] == "Design: polish the UI."
    assert texts["grill-me"] == "Interview the user about a plan."


def test_source_is_worked_out_from_disk(tmp_path):
    data = _review(tmp_path, [])
    sources = {row["name"]: row["source"] for row in data["skills"]}
    assert sources == {
        "grill-me": "user",
        "impeccable:impeccable": "plugin",
        "qa-round": "workflow",
        "dataviz": "built-in",
    }


def test_unused_skills_get_a_hide_fix_with_a_merge_prompt_and_dry_run_command(tmp_path):
    data = _review(
        tmp_path,
        [_usage("grill-me", listed=5), _usage("dataviz", listed=5), _usage("qa-round", listed=5, invoked=2)],
    )
    rows = {row["name"]: row for row in data["skills"]}
    assert rows["grill-me"]["status"] == "unused"
    assert rows["qa-round"]["status"] == "used"
    assert rows["qa-round"]["fixes"] == []
    assert data["unused"] == 2

    hide = rows["dataviz"]["fixes"][0]
    assert hide["command"] == (
        "claude-token-lens apply --set skillOverrides=dataviz:user-invocable-only --scope user --dry-run"
    )
    assert 'add "dataviz": "user-invocable-only" to skillOverrides, keeping every entry already there' in hide["prompt"]
    headings = [pair[0] for pair in hide["explainer"]]
    assert "Trade-off" in headings and "How to undo it" in headings

    # Your own skill also gets the frontmatter route.
    titles = [fix["title"] for fix in rows["grill-me"]["fixes"]]
    assert "Or turn off automatic use in the skill's own file" in titles
    frontmatter = rows["grill-me"]["fixes"][1]["prompt"]
    assert "disable-model-invocation: true" in frontmatter and "/grill-me" in frontmatter

    [hide_all] = data["fixes"]
    assert hide_all["title"] == "Hide all 2 unused skills from Claude"
    assert "skillOverrides=" in hide_all["command"] and "grill-me:user-invocable-only" in hide_all["command"]


def test_long_description_of_a_used_skill_gets_a_shorten_prompt(tmp_path):
    data = _review(tmp_path, [_usage("grill-me", listed=5, invoked=1, tokens=250)])
    row = next(row for row in data["skills"] if row["name"] == "grill-me")
    [fix] = row["fixes"]
    assert fix["title"] == "Shorten its description"
    assert "SKILL.md" in fix["prompt"] and fix["command"] is None


def test_nothing_is_stored_and_markdown_renders(tmp_path):
    data = _review(tmp_path, [_usage("dataviz", listed=5), _usage("grill-me", listed=4)])
    assert json.dumps(data)  # JSON-ready
    markdown = skills_review.render_markdown(data)
    assert markdown.startswith("# Skills review")
    assert "## Hide all 2 unused skills from Claude" in markdown
    assert "## dataviz (Built into Claude Code)" in markdown
    assert "never used" in markdown
