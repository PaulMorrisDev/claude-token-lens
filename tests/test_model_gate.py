"""The shared model-swap veto/gate helper (``model_gate``, PROF-05):
one merge of "did worse", "often retried", and "unfit" (metrics
capture said larger/mostly hard), with a sample-size floor none of the
four call sites this module replaces used to have."""

from __future__ import annotations

from types import SimpleNamespace as NS

from claude_token_lens import model_gate

from test_whatif import _table


def _tables(quality_by_setup=(), quality_retried=(), habits_agents=()):
    model = NS(sections=[
        NS(key="quality", tables=[
            _table("quality_by_setup", list(quality_by_setup)) if quality_by_setup else NS(name="quality_by_setup", columns=[], rows=[]),
            _table("quality_retried", list(quality_retried)) if quality_retried else NS(name="quality_retried", columns=[], rows=[]),
        ]),
        NS(key="habits", tables=[
            _table("habits_agents", list(habits_agents)) if habits_agents else NS(name="habits_agents", columns=[], rows=[]),
        ]),
    ])
    from claude_token_lens import whatif
    return whatif._Tables(model)


# -- row_unfit_reason ----------------------------------------------------------


def test_row_unfit_reason_prefers_larger_over_hard_and_retried():
    row = {"runs": 10, "fit_larger": 3, "fit_smaller": 1, "hard_pct": 90, "retried_model": 2}
    assert model_gate.row_unfit_reason(row) == "Claude said 3 of its runs needed a larger model"


def test_row_unfit_reason_falls_back_to_hard_then_retried():
    assert model_gate.row_unfit_reason({"runs": 10, "hard_pct": 60}) == "60% of its work was reported hard"
    assert model_gate.row_unfit_reason({"runs": 10, "retried_model": 1}) == (
        "a run was retried because the model wasn't enough"
    )


def test_row_unfit_reason_none_when_nothing_fires():
    assert model_gate.row_unfit_reason({"runs": 10, "fit_larger": 1, "fit_smaller": 2, "hard_pct": 10}) is None


def test_row_unfit_reason_floor_suppresses_a_thin_sample():
    # 2 of 2 runs said "larger" -- without a floor this blacklists the
    # model forever off a single retry-worthy anecdote.
    row = {"runs": 2, "fit_larger": 2, "fit_smaller": 0}
    assert model_gate.row_unfit_reason(row) == "Claude said 2 of its runs needed a larger model"
    assert model_gate.row_unfit_reason(row, min_sessions=5) is None


# -- raw / build -----------------------------------------------------------


def test_raw_merges_retried_only_where_worse_is_silent():
    tables = _tables(
        quality_by_setup=[
            {"agent_type": "reviewer", "model": "claude-haiku-4-5", "compared_model": "claude-sonnet-5",
             "setup_verdict": "worse"},
        ],
        quality_retried=[
            {"agent_type": "reviewer", "model": "claude-haiku-4-5", "runs": 10, "retried": 5, "retried_on": "claude-sonnet-5"},
            {"agent_type": "implementer", "model": "claude-haiku-4-5", "runs": 10, "retried": 5, "retried_on": "claude-sonnet-5"},
        ],
        habits_agents=[{"agent_type": "planner", "runs": 10, "fit_larger": 8, "fit_smaller": 0}],
    )
    worse, retried, unfit = model_gate.raw(tables)
    assert ("reviewer", "haiku") in worse
    assert ("reviewer", "haiku") in retried  # both computed independently
    assert ("implementer", "haiku") in retried
    assert unfit == {"planner": "Claude said 8 of its runs needed a larger model"}


def test_raw_applies_the_model_swap_min_sessions_floor_to_retried_and_unfit():
    tables = _tables(
        quality_retried=[{"agent_type": "reviewer", "model": "claude-haiku-4-5", "runs": 2, "retried": 1,
                           "retried_on": "claude-sonnet-5"}],
        habits_agents=[{"agent_type": "planner", "runs": 2, "fit_larger": 2, "fit_smaller": 0}],
    )
    worse, retried, unfit = model_gate.raw(tables)
    assert retried == {}
    assert unfit == {}


def test_build_vetoed_checks_worse_retried_and_unfit():
    tables = _tables(
        quality_by_setup=[{"agent_type": "reviewer", "model": "claude-haiku-4-5", "compared_model": "claude-sonnet-5",
                            "setup_verdict": "worse"}],
        habits_agents=[{"agent_type": "planner", "runs": 10, "fit_larger": 8, "fit_smaller": 0}],
    )
    gate = model_gate.build(tables)
    assert gate.vetoed("reviewer", "haiku") is True
    assert gate.vetoed("planner", "sonnet") is True  # unfit blocks every family, not just the compared one
    assert gate.vetoed("reviewer", "sonnet") is False
    assert gate.vetoed(None, "haiku") is False
