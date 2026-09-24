"""``capture_view``: what the Capture tab and the banner show, from
``[capture]``, the catalogue, a replayed history and measured usage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_token_lens import capture, capture_catalogue as catalogue, capture_view
from claude_token_lens.config import CaptureConfig
from claude_token_lens.hook_health import CaptureHookHealth, HookSpec
from claude_token_lens.units import Units

from helpers import assert_privacy, elasticity_with_slope

API = Units(billing_mode="api")


def _past() -> capture.History:
    return capture.History(
        days=14, sessions=20, cycles=200, subagents=40, main_notes=24, sub_notes=40,
        main_note=2e-6, sub_note=1e-6, sub_note_no_rules=0.0, reply_tag=4e-6, report_tag=4e-6,
        brief_tag=4e-6, spend=50.0,
    )


def _rows(data) -> dict:
    return {row["id"]: row for section in data["sections"] for row in section["metrics"]}


def test_off_invites_with_the_essentials_estimate():
    data = capture_view.view(CaptureConfig(), past=_past(), units=API)
    assert_privacy(data)
    banner = data["banner"]
    assert banner["on"] is False
    assert banner["headline"].startswith("Metrics capture is off. At Essentials it would have cost about ")
    assert "USD a week" in banner["headline"] and "of what you spent" in banner["headline"]
    levels = {level["id"]: level for level in data["levels"]}
    assert levels["free"]["estimate"] is None  # no Claude tokens
    assert levels["essentials"]["estimate"]["usd"] < levels["deep"]["estimate"]["usd"]
    assert levels["off"]["current"] is True
    rows = _rows(data)
    assert rows["task"]["on"] is False and rows["task"]["estimate"]["usd"] > 0
    assert rows["session_end"]["estimate"] is None and not rows["session_end"]["asks_claude"]
    assert rows["prompt_features"]["on"] is True and rows["prompt_features"]["toggle"] is False


def test_off_without_history_still_invites():
    data = capture_view.view(CaptureConfig())
    assert data["banner"]["headline"] == (
        "Metrics capture is off. Turn it on to get suggestions that fit how you work."
    )
    assert data["history"] is None and all(level["estimate"] is None for level in data["levels"])


def _on(**kw) -> CaptureConfig:
    return CaptureConfig(level="essentials", enabled_at="2026-09-20T10:00:00+00:00", **kw)


def _use(**kw) -> capture.CaptureUsage:
    use = capture.CaptureUsage(since="2026-09-20T10:00:00+00:00", sessions=4, subagents=6, spend=10.0, **kw)
    use._add("main", note_chars=4000, note_cost=0.02, tag_chars=400, tag_cost=0.01)
    use.by_metric = {"task": 0.012, "result": 0.004}
    return use


def test_on_headline_shows_level_tokens_amount_and_coverage():
    data = capture_view.view(_on(), past=_past(), units=API, use=_use(cycles=10, tagged_cycles=9))
    headline = data["banner"]["headline"]
    assert headline.startswith("Metrics capture: Essentials · since 2026-09-20 · 1,100 tokens · 0.03 USD")
    assert "(0.3% of spend)" in headline and "tagged on 90.0% of messages" in headline
    rows = _rows(data)
    assert rows["task"]["actual"]["usd"] == 0.012
    assert rows["task"]["answers"] == 0 and rows["task"]["target"] == capture.enough_target("task")
    assert data["measured"]["scopes"]["main"]["note_tokens"] == 1000


def test_on_notes_low_coverage_enough_data_and_expiry():
    use = _use(cycles=30, tagged_cycles=6)
    use.answers = {m: 1000 for m in catalogue.level_metrics("essentials")}
    capture_config = _on(until="2026-09-21T00:00:00+00:00")
    data = capture_view.view(capture_config, units=API, use=use, now=datetime(2026, 9, 22, tzinfo=timezone.utc))
    notes = data["banner"]["notes"]
    assert data["config"]["expired"] is True and data["config"]["effective"] is False
    assert any(note.startswith("Its end time (2026-09-21 00:00) has passed") for note in notes)
    assert any("tagged only 20.0% of your messages" in note for note in notes)
    assert any(note.startswith("Enough collected for every metric on") for note in notes)


# -- capture ROI: what it costs against what depends on it -----------------


def test_roi_is_none_without_a_weekly_cost_to_price():
    data = capture_view.view(_on(), units=API, use=_use())
    assert data["roi"] is None
    assert not any("Capture cost about" in note for note in data["banner"]["notes"])


def test_roi_prices_capture_against_what_depends_on_it():
    data = capture_view.view(_on(), units=API, use=_use(), weekly_cost=2.0, dependent_value=5.0)
    roi = data["roi"]
    assert roi["cost"]["usd"] == 2.0 and roi["value"]["usd"] == 5.0 and roi["measured"] is True
    assert (
        "Capture cost about 2.00 USD a week; suggestions that rely on it are worth about 5.00 USD a week."
        in data["banner"]["notes"]
    )


def test_roi_says_so_instead_of_a_zero_when_nothing_measured_depends_on_capture():
    data = capture_view.view(_on(), units=API, use=_use(), weekly_cost=2.0, dependent_value=None)
    roi = data["roi"]
    assert roi["cost"]["usd"] == 2.0 and roi["value"] is None and roi["measured"] is False
    assert "Capture cost about 2.00 USD a week; nothing measured yet relies on it." in data["banner"]["notes"]


def test_roi_adds_no_banner_note_when_nothing_was_spent():
    data = capture_view.view(_on(), units=API, use=_use(), weekly_cost=0.0, dependent_value=None)
    assert data["roi"]["cost"]["usd"] == 0.0
    assert not any("Capture cost about" in note for note in data["banner"]["notes"])


def test_roi_banner_has_no_bare_dollar_or_doubled_about_or_doubled_weekly_under_a_subscription():
    """UX-2 / finding F3: a subscription's ROI banner note must route
    through Units, never a bare "$", never double "about" (the "about"
    manually prepended in ``_banner`` used to collide with a subscription
    share's own "about X% of your weekly usage limit"), and never say
    "...weekly usage limit a week" (the roi cost/value used to keep the
    "a week" period suffix even once the primary text already read as a
    share of the *weekly* usage limit)."""
    subscription = Units(billing_mode="subscription", elasticity=elasticity_with_slope())
    data = capture_view.view(_on(), units=subscription, use=_use(), weekly_cost=2.0, dependent_value=5.0)
    note = next(n for n in data["banner"]["notes"] if n.startswith("Capture cost"))
    assert "$" not in note
    assert "about about" not in note.lower()
    assert "usage limit a week" not in note.lower()


def test_on_with_no_notes_seen_says_the_hook_may_be_blocked():
    use = capture.CaptureUsage(since="2026-09-20T10:00:00+00:00")
    data = capture_view.view(_on(), units=API, use=use, started_since=5)
    assert "no captured sessions yet" in data["banner"]["headline"]
    assert any("No capture note seen in the 5 sessions" in note for note in data["banner"]["notes"])
    # Half the sessions sampled out: 5 started is not enough to say so.
    data = capture_view.view(_on(sample=50), units=API, use=use, started_since=5)
    assert not any("No capture note" in note for note in data["banner"]["notes"])


def test_hook_problems_are_counted_never_quoted():
    spec = HookSpec(catalogue.HOOK_SCRIPT, "SessionStart", catalogue.SESSION_START_MATCHER)
    health = CaptureHookHealth(
        settings_path=Path("C:/Users/someone-private/.claude/settings.json"),
        needed=(spec,),
        problems=["The command runs C:/Users/someone-private/hook.py, which does not exist."],
    )
    block = capture_view.hooks_block(health)
    assert block["ok"] is False and block["problems"] == 1
    assert "someone-private" not in str(block)
    assert "1 capture hook entry in settings.json can't run" in block["summary"]
    data = capture_view.view(_on(), units=API, hooks=health)
    assert block["summary"] in data["banner"]["notes"]


def test_missing_hook_marks_the_metrics_that_need_it():
    spec = HookSpec(catalogue.HOOK_SCRIPT, "SubagentStart")
    health = CaptureHookHealth(settings_path=Path("settings.json"), needed=(spec,), missing=(spec,))
    rows = _rows(capture_view.view(_on(), units=API, hooks=health))
    assert rows["result"]["needs_hook"] is True
    assert rows["task"]["needs_hook"] is False
    assert rows["size"]["needs_hook"] is False  # off


def test_describe_and_config_block():
    assert capture_view.describe(CaptureConfig()) == "Off"
    config = _on(sample=25, until="2026-10-01T12:30:00+00:00")
    assert capture_view.describe(config) == "Essentials (since 2026-09-20, until 2026-10-01 12:30, 25% of sessions)"
    block = capture_view.config_block(CaptureConfig(level="free", projects=["secret-client"]))
    assert block["projects_limited"] is True and "secret-client" not in str(block)
    assert block["metrics"] == list(catalogue.level_metrics("free"))


def test_change_commands():
    before = _on()
    assert capture_view.change_commands(before, {"level": "off"}) == ["claude-token-lens capture off"]
    assert capture_view.change_commands(before, {"level": "deep"}) == ["claude-token-lens capture level deep"]
    assert capture_view.change_commands(before, {"metrics": ["task", "fit"]}) == [
        "claude-token-lens capture enable fit",
        "claude-token-lens capture disable brief level shift retry session_end waits permissions turn_signals",
    ]
    assert capture_view.change_commands(before, {"feedback": ["feedback_note"], "sample": 50}) == [
        "claude-token-lens capture enable feedback_note",
        "claude-token-lens capture on --sample 50",
    ]


def test_amount_text_follows_the_billing_mode():
    assert capture_view.amount_text(API, 0) == "nothing"
    assert capture_view.amount_text(API, 0.001, "a week") == "under 0.01 USD a week"
    subscription = Units(billing_mode="subscription")
    assert capture_view.amount_text(subscription, 1.5) == "1.50 USD list-price equivalent"


def test_units_basis():
    assert API.basis() == "Amounts are what the tokens cost at list price."
    assert Units(billing_mode="subscription").basis().startswith(
        "Amounts are list-price equivalents, not what you are charged."
    )


def test_several_missing_hooks_make_one_sentence_and_a_list():
    specs = (
        HookSpec(catalogue.HOOK_SCRIPT, "SessionStart", catalogue.SESSION_START_MATCHER),
        HookSpec(catalogue.HOOK_SCRIPT, "SubagentStart"),
    )
    health = CaptureHookHealth(settings_path=Path("settings.json"), needed=specs, missing=specs)
    block = capture_view.hooks_block(health)
    assert block["summary"] == (
        "settings.json lacks 2 of the hook entries your metrics need, so they aren't captured. "
        "Run 'claude-token-lens capture connect' to fix it."
    )
    assert len(block["missing"]) == 2


def test_share_text_never_reads_as_zero():
    assert capture_view.share_text(None) == ""
    assert capture_view.share_text(0.0) == "0.0%"
    assert capture_view.share_text(0.01) == "under 0.1%"
    assert capture_view.share_text(0.3) == "0.3%"


# -- feedback --------------------------------------------------------------


def test_a_missing_feedback_skill_is_a_row_note_and_a_banner_note_even_with_capture_off():
    data = capture_view.view(CaptureConfig(feedback=["feedback_skill"]), skill="missing")
    row = _rows(data)["feedback_skill"]
    assert row["needs_install"] is True
    assert row["install_note"] == capture_view.SKILL_STATES["missing"]
    assert row["install_command"] == capture_view.FEEDBACK_COMMAND
    assert data["banner"]["on"] is False and data["banner"]["notes"] == [capture_view.SKILL_NOTES["missing"]]
    installed = capture_view.view(CaptureConfig(feedback=["feedback_skill"]), skill="installed")
    assert _rows(installed)["feedback_skill"]["needs_install"] is False and installed["banner"]["notes"] == []
    # Off, nothing is asked of the skill file at all.
    assert _rows(capture_view.view(CaptureConfig(), skill="missing"))["feedback_skill"]["needs_install"] is False


def test_a_missing_brief_skill_is_a_row_note_and_a_banner_note():
    config = CaptureConfig(coaching=["brief_templates"])
    data = capture_view.view(config, brief_skill="missing")
    row = _rows(data)["brief_templates"]
    assert row["needs_install"] is True
    assert row["install_note"] == capture_view.BRIEF_SKILL_STATES["missing"] == "The /tl-brief skill isn't installed"
    assert row["install_command"] == capture_view.BRIEF_COMMAND == "claude-token-lens capture brief on"
    assert data["banner"]["notes"] == [capture_view.BRIEF_SKILL_NOTES["missing"]]
    assert data["commands"]["brief"] == capture_view.BRIEF_COMMAND
    installed = capture_view.view(config, brief_skill="installed")
    assert _rows(installed)["brief_templates"]["needs_install"] is False and installed["banner"]["notes"] == []
    # The feedback skill's state never marks the brief row, and the other way round.
    both = capture_view.view(CaptureConfig(feedback=["feedback_skill"]), skill="installed", brief_skill="missing")
    assert _rows(both)["brief_templates"]["needs_install"] is False
    assert _rows(both)["feedback_skill"]["needs_install"] is False

def test_feedback_runs_are_priced_over_the_last_days_and_counted_toward_enough():
    use = capture.CaptureUsage(since="", feedback_runs=3, feedback_cost=0.05, feedback_answered=2)
    data = capture_view.view(
        CaptureConfig(feedback=["feedback_skill", "dashboard_rating"]), units=API, feedback_use=use,
        skill="installed", ratings=4,
    )
    rows = _rows(data)
    assert rows["feedback_skill"]["actual_label"] == f"Over the last {capture.HISTORY_DAYS} days"
    assert rows["feedback_skill"]["actual"]["usd"] == 0.05
    assert (rows["feedback_skill"]["answers"], rows["feedback_skill"]["target"]) == (2, capture.ENOUGH["feedback"])
    assert rows["dashboard_rating"]["answers"] == 4 and rows["dashboard_rating"]["actual"] is None
    assert data["feedback"]["runs"] == 3 and data["feedback"]["ratings"] == 4
    assert rows["task"]["actual_label"] == "Since it was turned on"


def test_status_line_toggles_say_when_the_status_line_is_someone_elses():
    config = CaptureConfig(feedback=["feedback_note"], coaching=["coaching_line"])
    rows = _rows(capture_view.view(config, statusline=False))
    assert rows["feedback_note"]["statusline_note"] == capture_view.STATUSLINE_NOTES["feedback_note"]
    assert rows["coaching_line"]["statusline_note"] == capture_view.STATUSLINE_NOTES["coaching_line"]
    for statusline in (True, None):
        rows = _rows(capture_view.view(config, statusline=statusline))
        assert rows["feedback_note"]["statusline_note"] is None and rows["coaching_line"]["statusline_note"] is None
