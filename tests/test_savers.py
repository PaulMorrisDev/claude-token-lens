"""Tests for v4-saver-roi: third-party token-saver tool ROI
(``src/claude_token_lens/savers.py``).

Most branches are exercised on hand-built ``model.Turn``/
``model.TranscriptResult``/``model.SessionRecord`` instances (the
``_turn``/``_transcript``/``_session`` helpers below, matching
``test_model_swap.py``'s/``test_carry.py``'s own convention) with
hand-computed costs at the packaged Sonnet 5 rates. One test goes
through the real parser (``parse_transcript`` + ``tests/helpers.py``'s
JSONL builders) to exercise ``helpers.assert_privacy`` on a genuine
``TranscriptResult``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_token_lens import model, savers
from claude_token_lens.model import (
    Classification,
    Diagnostics,
    PricingMeta,
    ReportMeta,
    ReportModel,
    Section,
    SessionRecord,
    TranscriptMeta,
    TranscriptResult,
)
from claude_token_lens.parse import parse_transcript
from claude_token_lens.pricing import load_pricing
from claude_token_lens.render.tables import format_cell
from claude_token_lens.snapshots import Snapshot

from helpers import assert_privacy, tool_result_block, tool_use_block, turn_line, user_block_line, write_jsonl

PRICING = load_pricing()
SONNET = "claude-sonnet-5"
SONNET_RATES = PRICING.resolve_model(SONNET)


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field -- same convention as ``test_model_swap.py``'s own ``_turn``
    helper. ``turn_index`` defaults to 1 (priced)."""
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model=SONNET,
        input_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        cc_5m=0,
        cc_1h=0,
        ctx=0,
        gap_s=None,
    )
    fields.update(overrides)
    return model.Turn(**fields)


def _transcript(turns: list[model.Turn], session_id: str = "sess1", **meta_overrides) -> TranscriptResult:
    meta = TranscriptMeta(path="synthetic.jsonl", kind="top-level", session_id=session_id, **meta_overrides)
    tool_result_chars: dict = {}
    tool_result_calls: dict = {}
    for turn in turns:
        for tool_name, chars in turn.tool_result_chars_by_tool.items():
            tool_result_chars[tool_name] = tool_result_chars.get(tool_name, 0) + chars
            tool_result_calls[tool_name] = tool_result_calls.get(tool_name, 0) + 1
    return TranscriptResult(meta=meta, turns=turns, tool_result_chars=tool_result_chars, tool_result_calls=tool_result_calls)


def _session(session_id: str, top: TranscriptResult, mode: str = "build", purpose: str = "feature") -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        slug=session_id,
        first_ts="2026-09-18T12:00:00.000Z",
        last_ts="2026-09-18T12:05:00.000Z",
        top=top,
        subs=[],
        classification=Classification(mode=mode, purpose=purpose),
    )


def _cost(model_id: str, **fields) -> float:
    rates = PRICING.models[model_id]
    input_tokens = fields.get("input_tokens", 0)
    output_tokens = fields.get("output_tokens", 0)
    cc_5m = fields.get("cc_5m", 0)
    cc_1h = fields.get("cc_1h", 0)
    cache_read_tokens = fields.get("cache_read_tokens", 0)
    return (
        input_tokens / 1_000_000 * rates.input
        + output_tokens / 1_000_000 * rates.output
        + cc_5m / 1_000_000 * rates.cache_write_5m
        + cc_1h / 1_000_000 * rates.cache_write_1h
        + cache_read_tokens / 1_000_000 * rates.cache_read
    )


# -- detection -------------------------------------------------------------


def test_detect_savers_from_explicit_config_list():
    config = SimpleNamespace(savers=["my-plugin"])
    candidates = savers.detect_savers([], [], config)
    assert candidates == [savers.SaverCandidate(name="my-plugin", from_config=True, detected_via=())]


def test_detect_savers_via_mcp_tool_name_prefix():
    tr = _transcript([_turn(tool_names=("mcp__token-saver__optimize",))])
    candidates = savers.detect_savers([tr], [], None)
    assert len(candidates) == 1
    assert candidates[0].name == "token-saver"
    assert candidates[0].from_config is False
    assert "mcp_tool_prefix" in candidates[0].detected_via


def test_detect_savers_via_attribution_mcp_server():
    tr = _transcript([_turn(attribution_mcp_server="context-cache")])
    candidates = savers.detect_savers([tr], [], None)
    assert [c.name for c in candidates] == ["context-cache"]
    assert candidates[0].detected_via == ("attribution_mcp_server",)


def test_detect_savers_via_attribution_skill():
    tr = _transcript([_turn(attribution_skill="compress-context")])
    candidates = savers.detect_savers([tr], [], None)
    assert [c.name for c in candidates] == ["compress-context"]
    assert candidates[0].detected_via == ("attribution_skill",)


def test_detect_savers_via_snapshot_mcp_servers():
    snap = Snapshot(path=Path("snap.json"), ts="2026-09-18T00:00:00.000Z", data={"mcp_servers": {"names": ["memory-cache"]}})
    candidates = savers.detect_savers([], [snap], None)
    assert [c.name for c in candidates] == ["memory-cache"]
    assert candidates[0].detected_via == ("snapshot_mcp_servers",)


def test_detect_savers_via_snapshot_enabled_plugins():
    snap = Snapshot(path=Path("snap.json"), ts="2026-09-18T00:00:00.000Z", data={"enabled_plugins": ["lean-tokens@marketplace"]})
    candidates = savers.detect_savers([], [snap], None)
    assert [c.name for c in candidates] == ["lean-tokens@marketplace"]
    assert candidates[0].detected_via == ("snapshot_enabled_plugins",)


def test_detect_savers_ignores_non_matching_names():
    tr = _transcript([_turn(tool_names=("mcp__myserver__foo",), attribution_mcp_server="myserver")])
    candidates = savers.detect_savers([tr], [], None)
    assert candidates == []


def test_detect_savers_merges_config_and_auto_detected():
    tr = _transcript([_turn(attribution_mcp_server="token-buddy")])
    config = SimpleNamespace(savers=["codename-x"])
    candidates = savers.detect_savers([tr], [], config)
    names = {c.name: c for c in candidates}
    assert set(names) == {"codename-x", "token-buddy"}
    assert names["codename-x"].from_config is True
    assert names["codename-x"].detected_via == ()
    assert names["token-buddy"].from_config is False


# -- overhead ----------------------------------------------------------------


def test_overhead_arithmetic():
    fields = dict(input_tokens=1_000_000, output_tokens=0)
    attributed_turn = _turn(
        turn_index=1,
        attribution_mcp_server="acme-cache",
        tool_names=("mcp__acme-cache__lookup",),
        tool_result_chars_by_tool={"mcp__acme-cache__lookup": 400},
        **fields,
    )
    plain_turn = _turn(turn_index=2, input_tokens=500_000)
    tr = _transcript([attributed_turn, plain_turn])

    overhead = savers._compute_overhead(
        [savers.SaverCandidate(name="acme-cache", detected_via=("attribution_mcp_server",))],
        [tr],
        PRICING,
    )
    ov = overhead["acme-cache"]
    assert ov.turns == 1
    assert ov.cost_usd == pytest.approx(_cost(SONNET, **fields))
    assert ov.calls == 1
    assert ov.result_chars == 400
    assert ov.mean_result_chars == pytest.approx(400.0)
    assert ov.distinct_tool_names == 1


def test_overhead_zero_calls_reports_none_mean():
    tr = _transcript([_turn(attribution_mcp_server="acme-cache")])
    overhead = savers._compute_overhead(
        [savers.SaverCandidate(name="acme-cache")],
        [tr],
        PRICING,
    )
    ov = overhead["acme-cache"]
    assert ov.calls == 0
    assert ov.mean_result_chars is None


# -- effect + verdict ----------------------------------------------------------


def _present_absent_stats(present_cost_fields, absent_cost_fields, sessions_per_arm=5, extra_th=None):
    """Build ``sessions_per_arm`` present sessions (one turn attributed to
    "acme-cache", priced at ``present_cost_fields``) and the same number
    of absent sessions (one plain turn, priced at ``absent_cost_fields``),
    then run the full ``compute_saver_roi`` pipeline."""
    results: list[TranscriptResult] = []
    sessions: list[SessionRecord] = []
    for i in range(sessions_per_arm):
        turn = _turn(attribution_mcp_server="acme-cache", tool_names=("mcp__acme-cache__lookup",), **present_cost_fields)
        tr = _transcript([turn], session_id=f"present-{i}")
        results.append(tr)
        sessions.append(_session(f"present-{i}", tr))
    for i in range(sessions_per_arm):
        turn = _turn(**absent_cost_fields)
        tr = _transcript([turn], session_id=f"absent-{i}")
        results.append(tr)
        sessions.append(_session(f"absent-{i}", tr))

    th = extra_th or savers.SaverThresholds()
    return savers.compute_saver_roi(results, sessions, [], PRICING, th)


def test_effect_and_verdict_positive_net_saving():
    present_fields = dict(input_tokens=1_000_000)  # cost = 2.0 USD (Sonnet input rate)
    absent_fields = dict(input_tokens=3_000_000)  # cost = 6.0 USD
    stats = _present_absent_stats(present_fields, absent_fields)

    rows = stats.effect["acme-cache"]
    overall = next(r for r in rows if r.stratum == "all")
    assert overall.sample_ok is True
    assert overall.present.cost_per_session == pytest.approx(_cost(SONNET, **present_fields))
    assert overall.absent.cost_per_session == pytest.approx(_cost(SONNET, **absent_fields))

    verdict = next(v for v in stats.verdicts if v.saver == "acme-cache")
    assert verdict.sample_ok is True
    expected_gross = _cost(SONNET, **absent_fields) - _cost(SONNET, **present_fields)
    expected_overhead = _cost(SONNET, **present_fields)  # the only attributed turn per present session
    assert verdict.gross_saving_per_session == pytest.approx(expected_gross)
    assert verdict.overhead_per_session == pytest.approx(expected_overhead)
    assert verdict.net_saving_per_session == pytest.approx(expected_gross - expected_overhead)
    assert verdict.verdict == "keep"


def test_effect_and_verdict_negative_net_saving():
    # Present sessions cost more even after subtracting the saver's own
    # overhead -- net saving negative, verdict should be "disable".
    present_fields = dict(input_tokens=3_000_000)  # cost = 6.0 USD, all attributed to acme-cache
    absent_fields = dict(input_tokens=1_000_000)  # cost = 2.0 USD
    stats = _present_absent_stats(present_fields, absent_fields)

    verdict = next(v for v in stats.verdicts if v.saver == "acme-cache")
    assert verdict.net_saving_per_session < 0
    assert verdict.verdict == "disable"


def test_effect_below_min_sample_is_inconclusive():
    present_fields = dict(input_tokens=1_000_000)
    absent_fields = dict(input_tokens=3_000_000)
    stats = _present_absent_stats(present_fields, absent_fields, sessions_per_arm=3)

    rows = stats.effect["acme-cache"]
    overall = next(r for r in rows if r.stratum == "all")
    assert overall.sample_ok is False

    verdict = next(v for v in stats.verdicts if v.saver == "acme-cache")
    assert verdict.sample_ok is False
    assert verdict.verdict == "inconclusive"


def test_effect_stratified_by_purpose_and_mode():
    results: list[TranscriptResult] = []
    sessions: list[SessionRecord] = []
    for i in range(5):
        turn = _turn(attribution_mcp_server="acme-cache", input_tokens=1_000_000)
        tr = _transcript([turn], session_id=f"present-a-{i}")
        results.append(tr)
        sessions.append(_session(f"present-a-{i}", tr, mode="build", purpose="feature"))
    for i in range(5):
        turn = _turn(input_tokens=1_000_000)
        tr = _transcript([turn], session_id=f"absent-a-{i}")
        results.append(tr)
        sessions.append(_session(f"absent-a-{i}", tr, mode="build", purpose="feature"))

    stats = savers.compute_saver_roi(results, sessions, [], PRICING, savers.SaverThresholds())
    rows = stats.effect["acme-cache"]
    strata = {r.stratum for r in rows}
    assert "all" in strata
    assert any("purpose=feature" in s and "mode=build" in s for s in strata)


# -- search substitution -------------------------------------------------------


def test_search_substitution_present_vs_absent():
    present_turn1 = _turn(
        turn_index=1,
        attribution_mcp_server="acme-cache",
        tool_names=("mcp__acme-cache__lookup",),
        tool_result_chars_by_tool={"mcp__acme-cache__lookup": 400},  # 100 tokens
    )
    present_turn2 = _turn(turn_index=2)
    present_tr = _transcript([present_turn1, present_turn2], session_id="present-1")

    absent_turn1 = _turn(
        turn_index=1,
        tool_names=("Grep",),
        tool_result_chars_by_tool={"Grep": 4000},  # 1000 tokens
    )
    absent_turn2 = _turn(turn_index=2)
    absent_tr = _transcript([absent_turn1, absent_turn2], session_id="absent-1")

    present_session = _session("present-1", present_tr)
    absent_session = _session("absent-1", absent_tr)

    th = savers.SaverThresholds(min_sessions_per_arm=1)
    stats = savers.compute_saver_roi([present_tr, absent_tr], [present_session, absent_session], [], PRICING, th)

    arms = {a.arm: a for a in stats.search_substitution["acme-cache"]}
    assert arms["present"].sample_ok is True
    assert arms["present"].saver_calls_per_session == pytest.approx(1.0)
    assert arms["present"].native_calls_per_session == pytest.approx(0.0)
    assert arms["present"].saver_mean_result_tokens == pytest.approx(100.0)

    assert arms["absent"].native_calls_per_session == pytest.approx(1.0)
    assert arms["absent"].saver_calls_per_session == pytest.approx(0.0)
    assert arms["absent"].native_mean_result_tokens == pytest.approx(1000.0)


def test_search_substitution_counts_shell_search_calls():
    turn1 = _turn(turn_index=1, tool_names=("Bash",), cmd_prefix="rg TODO src/")
    turn2 = _turn(turn_index=2)
    tr = _transcript([turn1, turn2], session_id="absent-shell")
    saver_turn = _turn(attribution_mcp_server="acme-cache")
    saver_tr = _transcript([saver_turn], session_id="present-shell")

    session_absent = _session("absent-shell", tr)
    session_present = _session("present-shell", saver_tr)

    th = savers.SaverThresholds(min_sessions_per_arm=1)
    stats = savers.compute_saver_roi([tr, saver_tr], [session_absent, session_present], [], PRICING, th)

    arms = {a.arm: a for a in stats.search_substitution["acme-cache"]}
    assert arms["absent"].native_calls_per_session == pytest.approx(1.0)


# -- co-changed keys -----------------------------------------------------------


def test_verdict_reports_co_changed_keys_excluding_presence_signal():
    present_snap = Snapshot(
        path=Path("p.json"),
        ts="2026-09-18T00:00:00.000Z",
        data={"mcp_servers": {"names": ["acme-cache"]}, "user_settings": {"some_setting": "on"}},
    )
    absent_snap = Snapshot(
        path=Path("a.json"),
        ts="2026-09-17T00:00:00.000Z",
        data={"mcp_servers": {"names": []}, "user_settings": {"some_setting": "off"}},
    )
    results: list[TranscriptResult] = []
    sessions: list[SessionRecord] = []
    for i in range(5):
        turn = _turn(attribution_mcp_server="acme-cache", input_tokens=1_000_000)
        tr = _transcript([turn], session_id=f"present-{i}")
        results.append(tr)
        rec = _session(f"present-{i}", tr)
        rec.first_ts = "2026-09-18T12:00:00.000Z"
        sessions.append(rec)
    for i in range(5):
        turn = _turn(input_tokens=1_000_000)
        tr = _transcript([turn], session_id=f"absent-{i}")
        results.append(tr)
        rec = _session(f"absent-{i}", tr)
        rec.first_ts = "2026-09-17T12:00:00.000Z"
        sessions.append(rec)

    stats = savers.compute_saver_roi(results, sessions, [present_snap, absent_snap], PRICING, savers.SaverThresholds())
    verdict = next(v for v in stats.verdicts if v.saver == "acme-cache")
    assert "user_settings.some_setting" in verdict.co_changed_keys
    assert "mcp_servers.names" not in verdict.co_changed_keys


# -- build_section -------------------------------------------------------------


def test_build_section_no_candidates_has_explanatory_note():
    stats = savers.SaverStats(candidates=[], overhead={}, effect={}, search_substitution={}, verdicts=[])
    section = savers.build_section(stats)
    assert section.key == "savers"
    table_names = {t.name for t in section.tables}
    assert table_names == {
        "savers_detected",
        "savers_overhead",
        "savers_effect_by_stratum",
        "savers_search_substitution",
        "savers_verdict",
    }
    assert any("no token-saver tool detected" in note.lower() for note in section.notes)


def test_every_cell_renders_without_error():
    present_fields = dict(input_tokens=1_000_000)
    absent_fields = dict(input_tokens=3_000_000)
    stats = _present_absent_stats(present_fields, absent_fields)
    section = savers.build_section(stats)
    for table in section.tables:
        for row in table.rows:
            for column, cell in zip(table.columns, row):
                format_cell(cell, column.kind)


# -- RULES / recommendation ------------------------------------------------------


def _report_with_section(section: Section) -> ReportModel:
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[section],
        recommendations=[],
        diagnostics=Diagnostics(),
    )


def _assert_evidence_resolves(report: ReportModel, rec) -> None:
    for label, value, source_table, row_key in rec.evidence:
        section_key, table_name = source_table.split(".", 1)
        section = next(s for s in report.sections if s.key == section_key)
        table = next(t for t in section.tables if t.name == table_name)
        row = next(r for r in table.rows if r[0] == row_key)
        assert any(cell == value for cell in row), (label, value, row)


def test_rule_fires_keep_when_net_saving_positive():
    present_fields = dict(input_tokens=1_000_000)
    absent_fields = dict(input_tokens=3_000_000)
    stats = _present_absent_stats(present_fields, absent_fields)
    th = savers.SaverThresholds()
    section = savers.build_section(stats, th)
    report = _report_with_section(section)

    recs = savers.RULES["saver-tool-roi"](report, th, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "saver-tool-roi"
    assert rec.lever == "mcpServers.acme-cache"
    assert rec.scope == "user"
    assert "keep" in rec.action.lower() or "paying for itself" in rec.title.lower()
    _assert_evidence_resolves(report, rec)


def test_rule_fires_disable_when_net_saving_negative():
    present_fields = dict(input_tokens=3_000_000)
    absent_fields = dict(input_tokens=1_000_000)
    stats = _present_absent_stats(present_fields, absent_fields)
    th = savers.SaverThresholds()
    section = savers.build_section(stats, th)
    report = _report_with_section(section)

    recs = savers.RULES["saver-tool-roi"](report, th, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert "not paying for itself" in rec.title.lower()
    _assert_evidence_resolves(report, rec)


def test_rule_suppressed_below_min_sample():
    present_fields = dict(input_tokens=1_000_000)
    absent_fields = dict(input_tokens=3_000_000)
    stats = _present_absent_stats(present_fields, absent_fields, sessions_per_arm=3)
    th = savers.SaverThresholds()
    section = savers.build_section(stats, th)
    report = _report_with_section(section)

    recs = savers.RULES["saver-tool-roi"](report, th, snapshot=None)
    assert recs == []


def test_rule_cites_displaced_native_calls_when_keep_and_substitution_holds():
    present_turn1 = _turn(turn_index=1, attribution_mcp_server="acme-cache", tool_names=("mcp__acme-cache__lookup",), tool_result_chars_by_tool={"mcp__acme-cache__lookup": 400}, input_tokens=1_000_000)
    present_turn2 = _turn(turn_index=2)
    absent_turn1 = _turn(turn_index=1, tool_names=("Grep",), tool_result_chars_by_tool={"Grep": 4000}, input_tokens=3_000_000)
    absent_turn2 = _turn(turn_index=2)

    results: list[TranscriptResult] = []
    sessions: list[SessionRecord] = []
    for i in range(5):
        tr = _transcript([present_turn1, present_turn2], session_id=f"present-{i}")
        results.append(tr)
        sessions.append(_session(f"present-{i}", tr))
    for i in range(5):
        tr = _transcript([absent_turn1, absent_turn2], session_id=f"absent-{i}")
        results.append(tr)
        sessions.append(_session(f"absent-{i}", tr))

    th = savers.SaverThresholds()
    stats = savers.compute_saver_roi(results, sessions, [], PRICING, th)
    section = savers.build_section(stats, th)
    report = _report_with_section(section)

    recs = savers.RULES["saver-tool-roi"](report, th, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert "displaces native" in rec.action.lower() or "smaller" in rec.action.lower()
    _assert_evidence_resolves(report, rec)


# -- privacy ----------------------------------------------------------------------


def test_privacy(tmp_path: Path):
    top_lines = [
        turn_line(
            message_id="msg_1",
            model=SONNET,
            input_tokens=1000,
            output_tokens=500,
            content=[tool_use_block("mcp__acme-cache__lookup", "tu_1", {"query": "irrelevant"})],
        ),
        user_block_line([tool_result_block("tu_1", "some tool result content")]),
        turn_line(message_id="msg_2", model=SONNET, input_tokens=2000, output_tokens=800),
    ]
    top_path = tmp_path / "top.jsonl"
    write_jsonl(top_path, top_lines)
    top = parse_transcript(top_path, TranscriptMeta(path=str(top_path), kind="top-level", session_id="sess1"))

    session = _session("sess1", top)
    snap = Snapshot(path=Path("snap.json"), ts="2026-09-18T00:00:00.000Z", data={"mcp_servers": {"names": ["acme-cache"]}})

    th = savers.SaverThresholds(min_sessions_per_arm=1)
    stats = savers.compute_saver_roi([top], [session], [snap], PRICING, th)
    section = savers.build_section(stats, th)
    assert_privacy(section)

    report = _report_with_section(section)
    recs = savers.RULES["saver-tool-roi"](report, th, snapshot=snap)
    for rec in recs:
        assert_privacy(rec)
