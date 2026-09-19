"""Every Appendix A1 dataclass constructs with defaults; EventKind has
every Appendix A2 member; format_cell and escape_md cover their contract.
"""

from __future__ import annotations

import pytest

from claude_token_lens import model
from claude_token_lens.render import escape_md, format_cell

# Appendix A2's detection table, plus SCHEDULED_TASK from A1: the full set
# of EventKind members the WP0 brief calls out by name. v3-limits adds
# LIMIT_HIT/LIMIT_RESUME/AGENT_TERMINATED (see model.py's module
# docstring's "Usage-limits batch" section).
A2_EVENT_KIND_NAMES = {
    "COMPACT_BOUNDARY",
    "COMPACT_SUMMARY",
    "API_ERROR",
    "MODEL_FALLBACK",
    "LOCAL_COMMAND",
    "HOOK_OUTPUT",
    "CACHE_SIGNAL",
    "REMINDER",
    "CONTEXT_INJECT",
    "QUEUE_OPERATION",
    "ATTACHMENT",
    "META",
    "TOOL_DENIAL",
    "TOOL_RESULT",
    "TASK_NOTIFICATION",
    "PEER_MESSAGE",
    "SLASH_COMMAND",
    "SCHEDULED_TASK",
    "INTERRUPT",
    "HUMAN_TEXT",
    "UNKNOWN",
    "LIMIT_HIT",
    "LIMIT_RESUME",
    "AGENT_TERMINATED",
}


def test_event_kind_has_every_a2_member():
    actual = {member.name for member in model.EventKind}
    assert actual == A2_EVENT_KIND_NAMES


def test_event_kind_is_a_str_enum():
    assert isinstance(model.EventKind.HUMAN_TEXT, str)
    assert model.EventKind.HUMAN_TEXT == "human_text"


A1_DATACLASSES = [
    model.Event,
    model.Turn,
    model.TranscriptMeta,
    model.Diagnostics,
    model.TranscriptResult,
    model.WorkflowRun,
    model.Classification,
    model.SessionRecord,
    model.CostBreakdown,
    model.Column,
    model.Table,
    model.Section,
    model.Recommendation,
    model.PricingMeta,
    model.ReportMeta,
    model.ReportModel,
]


@pytest.mark.parametrize("cls", A1_DATACLASSES, ids=[c.__name__ for c in A1_DATACLASSES])
def test_dataclass_constructs_with_defaults(cls):
    instance = cls()
    assert instance is not None


@pytest.mark.parametrize("cls", A1_DATACLASSES, ids=[c.__name__ for c in A1_DATACLASSES])
def test_dataclass_is_slotted(cls):
    assert cls.__slots__
    instance = cls()
    with pytest.raises(AttributeError):
        instance.not_a_real_field = 1


def test_report_model_nests_defaults_correctly():
    report = model.ReportModel()
    assert isinstance(report.meta, model.ReportMeta)
    assert isinstance(report.meta.pricing, model.PricingMeta)
    assert report.sections == []
    assert report.recommendations == []
    assert isinstance(report.diagnostics, model.Diagnostics)


def test_turn_references_event_kind_by_default():
    turn = model.Turn()
    assert turn.preceding_primary is model.EventKind.UNKNOWN
    assert turn.preceding_event_kinds == ()


# -- format_cell -------------------------------------------------------


@pytest.mark.parametrize("kind", ["str", "int", "float", "pct", "money", "tokens", "secs"])
def test_format_cell_none_is_dash(kind):
    assert format_cell(None, kind) == "-"


def test_format_cell_str():
    assert format_cell("hello", "str") == "hello"


def test_format_cell_int_has_thousands_separator():
    assert format_cell(1234567, "int") == "1,234,567"


def test_format_cell_float_has_two_decimals():
    assert format_cell(1234.567, "float") == "1,234.57"


def test_format_cell_pct_has_one_decimal():
    assert format_cell(12.34, "pct") == "12.3%"


def test_format_cell_money_has_two_decimals_and_currency_suffix():
    assert format_cell(12.5, "money") == "12.50 USD"
    assert format_cell(12.5, "money", currency="GBP") == "12.50 GBP"


def test_format_cell_tokens_are_integers_with_separators():
    assert format_cell(12345, "tokens") == "12,345"


def test_format_cell_secs_is_compact_duration():
    assert format_cell(83, "secs") == "1m 23s"
    assert format_cell(45, "secs") == "45s"
    assert format_cell(3723, "secs") == "1h 2m 3s"
    assert format_cell(0, "secs") == "0s"


def test_format_cell_unknown_kind_raises():
    with pytest.raises(ValueError):
        format_cell(1, "bogus")


# -- escape_md -----------------------------------------------------------


def test_escape_md_escapes_pipe_and_newline():
    assert escape_md("a|b\nc") == "a\\|b<br>c"


def test_escape_md_none_is_empty_string():
    assert escape_md(None) == ""


def test_escape_md_plain_text_is_unchanged():
    assert escape_md("plain text") == "plain text"
