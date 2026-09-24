"""CLAUDE.md review: files found on disk, joined to their usage by salted
path hash, with sections, agent-only sections, duplicates, stale
references and fix prompts (``claude_md_review``)."""

from __future__ import annotations

from pathlib import Path

from claude_token_lens import claude_md_review as cmr
from claude_token_lens import parse
from claude_token_lens.units import Units

from helpers import elasticity_with_slope

SALT = b"r" * 32
UNITS = Units(billing_mode="api", currency="USD")
PERIOD = "over the last 30 days"

SHARED = (
    "Always run the full test suite before you commit anything, and never skip the pre-commit hooks "
    "without asking first."
)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "token-lens"
    config_dir.mkdir(parents=True)
    (claude_root / "CLAUDE.md").write_text(f"# Global\n\n{SHARED}\n", encoding="utf-8")
    project = tmp_path / "repo"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("", encoding="utf-8")
    (project / ".claude" / "agents").mkdir(parents=True)
    (project / ".claude" / "agents" / "db-migrator.md").write_text("---\nname: db-migrator\n---\n", encoding="utf-8")
    (project / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    big = "Rule line that matters for every task in this repo.\n" * 40
    (project / "CLAUDE.md").write_text(
        "# Project\n\n"
        f"{SHARED}\n\n"
        "## Layout\n\nCode lives in `src/app.py` and the old entry point `src/legacy/main.py`.\n"
        "Run `npm run test` or `npm run e2e`.\n\n"
        "## db-migrator notes\n\nOnly the migration agent needs this.\n\n"
        f"## Conventions\n\n{big}",
        encoding="utf-8",
    )
    return config_dir, project


def _review(tmp_path: Path, context_files: dict | None = None):
    config_dir, project = _setup(tmp_path)
    return cmr.build_review(config_dir, context_files or {}, salt=SALT, projects=[project]), project


def test_finds_user_and_project_files_with_sections(tmp_path):
    review, project = _review(tmp_path)
    levels = {item.level: item for item in review.files}
    assert {"User", "Project"} <= set(levels)
    headings = [s.heading for s in levels["Project"].sections]
    assert headings[:4] == ["Project", "Layout", "db-migrator notes", "Conventions"]
    assert levels["Project"].id == parse.path_hash(str(project / "CLAUDE.md"), SALT)


def test_flags_agent_sections_duplicates_and_stale_references(tmp_path):
    review, _project = _review(tmp_path)
    project_file = next(item for item in review.files if item.level == "Project")
    agent_section = next(s for s in project_file.sections if s.heading == "db-migrator notes")
    assert agent_section.agents == ["db-migrator"]
    assert any("Always run the full test suite" in d["excerpt"] for d in project_file.duplicates)
    references = {item["reference"] for item in project_file.stale}
    assert "src/legacy/main.py" in references
    assert "src/app.py" not in references
    assert "npm run e2e" in references and "npm run test" not in references


def test_a_one_word_agent_name_counts_only_where_it_names_the_agent():
    """An agent called claude (or Explore, or Plan) is not named by
    "Claude Code", ".claude/" or "plan the change"."""
    names = ["claude", "Plan", "db-migrator"]
    prose = (
        "Claude Code reads `.claude/rules/` first. Claude should plan the change, then Claude runs the tests. "
        "See .claude/agents/ and the Claude Agent SDK docs."
    )
    assert cmr._agents_in("RevIXO — Claude Code Context", prose, names) == []
    assert cmr._agents_in("Notes", "Use the Plan agent first. The Plan agent reads only.", names) == ["Plan"]
    assert cmr._agents_in("Notes", "Spawn `claude` for this; `claude` has every tool.", names) == ["claude"]
    assert cmr._agents_in("Notes", 'subagent_type: "Plan" and subagent_type="Plan"', names) == ["Plan"]
    assert cmr._agents_in("db-migrator notes", "", names) == ["db-migrator"]


def test_usage_joins_by_hash_and_fixes_carry_the_undo_and_diff_step(tmp_path):
    config_dir, project = _setup(tmp_path)
    file_hash = parse.path_hash(str(project / "CLAUDE.md"), SALT)
    usage = {
        "transcripts": {"main": 4, "Explore": 2},
        "files": [
            {
                "hash": file_hash,
                "type": "Project",
                "scoped": False,
                "tokens": 700,
                "sends": 6,
                "reach": {"main": 4, "db-migrator": 2},
                "cost_usd": 1.5,
                "cost_by_reach": {"main": 1.0, "db-migrator": 0.5},
                "last_seen": "2026-09-18T12:00:00Z",
            }
        ],
    }
    review = cmr.build_review(config_dir, usage, salt=SALT, projects=[project])
    project_file = next(item for item in review.files if item.level == "Project")
    detail = cmr.file_detail(project_file, UNITS, PERIOD)
    assert detail["seen"] and detail["sends"] == 6
    assert "4 main sessions" in detail["reach_text"]
    assert detail["cost_text"]
    titles = [fix["title"] for fix in detail["fixes"]]
    assert "Move agent-only sections into agent files" in titles
    assert "Fix references to things that no longer exist" in titles
    for fix in detail["fixes"]:
        headings = [pair[0] for pair in fix["explainer"]]
        assert "Trade-off" in headings and "How to undo it" in headings
        assert "show me the diff" in fix["prompt"].lower()
    markdown = cmr.render_markdown(review, UNITS, PERIOD)
    assert "# CLAUDE.md review" in markdown and "db-migrator" in markdown


def test_trim_this_file_fix_has_no_bare_dollar_or_doubled_about_under_a_subscription():
    """UX-2 / finding F3: "Trim this file"'s effect clause
    (f"About {...} if you halve it.") must route through Units and
    ``.phrase(prefix="About ")``, never a bare "$" and never "About
    about ..." (a subscription's own share text already opens with
    "about")."""
    subscription = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    review = cmr.FileReview(
        id="trim-me",
        path=Path("CLAUDE.md"),
        level="Project",
        project="repo",
        chars=cmr.TRIM_TOKENS * 4 * 2,  # well over the trim threshold
        scoped=False,
        sections=[],
        imports=[],
        usage={"cost_usd": 3.0, "sends": {"main": 4}},
    )
    fixes = cmr.build_fixes(review, subscription, PERIOD)
    trim = next(f for f in fixes if f["title"] == "Trim this file")
    effect = next(value for label, value in trim["explainer"] if label == "Expected effect")
    assert "$" not in effect
    assert "about about" not in effect.lower()


def test_sections_ignore_headings_inside_code_fences():
    text = "# One\n\ntext\n\n```bash\n# not a heading\n```\n\n## Two\n\nmore\n"
    assert [s.heading for s in cmr.sections(text)] == ["One", "Two"]


def test_worktrees_fold_into_their_main_project(tmp_path):
    main = tmp_path / "repo"
    worktree = main / ".claude" / "worktrees" / "wt1"
    worktree.mkdir(parents=True)
    folders, worktrees = cmr.split_worktrees([main, worktree])
    assert folders == [main]
    assert list(worktrees.values()) == [[worktree]]
