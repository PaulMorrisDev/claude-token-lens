# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository scaffold: licence, changelog, security policy, `pyproject.toml`,
  the frozen `model.py` data contract, `render/tables.py` cell-formatting
  primitives, and the `cli.py` argparse skeleton with subcommand stubs.

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
