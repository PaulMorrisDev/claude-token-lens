# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository scaffold: licence, changelog, security policy, `pyproject.toml`,
  the frozen `model.py` data contract, `render/tables.py` cell-formatting
  primitives, and the `cli.py` argparse skeleton with subcommand stubs.
- `workstyle.build_section`, `workflows.build_section`, and
  `snapshots.build_config_section`: report `Section`/`Table` wrappers for
  archetype counts, workflow-run summaries, and the config diff table,
  matching the pattern already used by `classify`/`compaction`/`recache`/
  `ttl`.
- `recommend.py` (WP10b): `recommend()` turns an assembled `ReportModel`
  into evidence-backed `Recommendation`s, implementing every Appendix A5
  rule (`ttl-switch`, `long-tool-waits`, `notification-invalidation`,
  `batch-instructions`, `subagent-volume`, `compaction-churn`,
  `long-context-share`, `cache-read-dominance`, `baseline-bloat`,
  `agent-report-size`, `spawn-cost`, `effort-mismatch`,
  `discovery-share`, `pricing-coverage`, `data-quality`) with archetype
  gating, a minimum-sample gate, and managed-settings-aware scope
  encoding; `render_patch_set()` renders the settings/frontmatter changes
  a recommendation set implies as unified-diff-style text.
- `report.build_report` now accepts `session_overrides` and populates
  `ReportModel.recommendations` via `recommend.recommend()`, using the
  corpus's own archetype and latest config snapshot.

### Fixed

Independent review of the WP4/WP6/WP8 report-assembly modules found 12
defects, each fixed and landed as its own commit:

1. `recache.py` — apply cache-hit signatures to transcript turns before
   scoring them, instead of leaving every turn unsignatured.
2. `ttl.py` — price simulated turns per-turn via the rates lookup rather
   than a single blended rate.
3. `tests/helpers.py` — `assert_privacy` now inspects `Table` cells and
   `Section` notes, not just top-level dataclass fields.
4. `recache.py` — token-weighted control group and a prefix-invalidated
   primary-cause table (previously invalidation caused was undercounted
   for prefix-broken turns).
5. `ttl.py` — unconditional counterfactual buckets and a single,
   consistent expiry-loss basis across the 5m/1h comparison.
6. `ttl.py` — renamed `in_window_share`/`break_even_share` to
   `in_window_pct`/`break_even_pct` (stored as percents, turn 0's own
   prefix included in the shared denominator); `delta_usd`/`delta_pct`
   keep their sign and gained a `saving_usd = max(0, delta_usd)`
   companion; added a `TtlThresholds` dataclass (`from_config`/
   `describe()`, mirroring `RecacheThresholds`) threaded through the
   simulation, fidelity, and report-building functions;
   `TtlTypeStats.recommendation` changed from a property to a method
   taking `th`; `compaction.py`'s RE-CACHE trio now also reads from
   `RecacheThresholds` instead of hardcoding it a second time.
7. `events.py` — `classify_line` now tests `TOOL_DENIAL`/`TOOL_RESULT`/
   `TASK_NOTIFICATION`/`PEER_MESSAGE` before the generic `isMeta` check,
   so a line carrying both markers gets the more specific kind; `META`
   events gained a `subkind` (the line's `origin.kind`, else its leading
   XML-ish tag name, else `"plain"`).
8. `classify.py` — `classify_purpose` now checks intent signatures
   (local-llm-pipeline, workflow-run, review, test-triage, planning,
   docs-or-light-edit, refactor) before falling through to the generic
   `agent-fanout`/`general-dev` buckets; `review` relaxed to tolerate up
   to 2 edit turns, `test-triage` relaxed to drop its
   hits-vs-edit-turns comparison once hits reach 5. `build_section` now
   reports its active overnight local-time window as a note.
9. `compaction.py` — added `CompactionRecord.join_delta_s` and a
   module-level `new_tokens()` helper (`input_tokens +
   cache_creation_tokens`); post-compaction write/recache cost
   aggregates and the per-session cost column now exclude records whose
   join to their next turn took longer than 15 minutes, and the dropped-
   token share is reported against both the `cache_creation` and
   `new_tokens` denominators.
10. `workstyle.py`/`workflows.py`/`snapshots.py` — added `build_section`
    (see Added above).
11. `statusline.py` — `_last_assistant_ts` now scans the transcript tail
    with `text.split("\n")` instead of `str.splitlines()` (the latter
    also breaks on `\r`/`\v`/U+2028/U+2029, which can legally appear
    inside a JSON string value and would shear a JSONL line into
    unparsable fragments); `main()` guards
    `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` in a
    `try`/`except`, and every print now goes through a `_safe_print`
    helper that never lets a print failure escape.
12. This entry.

Two deviations from the original plan text, found and reported rather
than silently reconciled:

- **WP4 fidelity bar.** The plan's TTL break-even section (Appendix A4)
  is referenced elsewhere as flagging simulation fidelity above 5%, but
  Appendix A4 itself specifies "flag agent types above 10%" —
  `TtlThresholds.fidelity_warn_pct` implements the 10% figure that
  Appendix A4 actually states.
- **A2 META description.** Plan Appendix A2's detection table describes
  `META` lines without a `subkind`, but observed transcripts carry
  enough structure (an `origin.kind`, or a leading XML-ish tag in the
  content) to sub-classify them usefully — item 7 above adds `subkind`
  as an extension of A2's table rather than a literal implementation of
  it.

Coordinator-added fixes landed alongside the numbered list, same
one-commit-per-fix / green-tests discipline:

- `workstyle.py` — `detect_archetype` now tests `chat-only` before
  `single-model` so a chat-only session with a single resolvable model
  family is no longer misclassified.
- `parse.py` — pre-split `cache_creation` reads are now treated as a
  format difference (`Diagnostics.pre_split_turns`) and normalized via
  `ttl.normalize_ttl_split`, rather than silently mis-parsed.
- `compaction.py` — turns are now marked re-cache via the shared
  `recache.apply()` detector instead of a separate internal
  `is_recache_turn()` heuristic, so compaction's RE-CACHE-flagged
  write-cost figures use the same signature logic as every other module.

### Planned

The following are a v0.4 backlog, kept here until they are scheduled into a
milestone:

- **Budget check.** `check --weekly-tokens N --daily-usd N` exits non-zero
  when exceeded; UI banner; uses `log-usage` window data when present.
  Guardrail for overnight runs.
- **Anomaly outliers.** Sessions or spawns whose cost is more than 3 median
  absolute deviations from their mode/purpose group, with the composition
  table attached. Catches runaway agents.
- **Team aggregate.** `import` several machines' hashed-slug exports into one
  store; per-archetype comparisons across people, no text ever. For team
  leads on the work machine.
- **Scheduled reports.** `serve --weekly-report DIR` writes the Markdown/HTML
  report every Monday for sharing. Habit-forming review.
- **Opt-in local path view.** `--show-paths` (local only, never in exports)
  lists the top files by Read tokens, as token-dashboard does.
