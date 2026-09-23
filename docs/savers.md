# Third-party token-saver tool ROI (v4-saver-roi)

An increasing number of installed MCP servers, plugins and skills claim
to save tokens: context compressors, log/output suppressors, "delegate
simple tasks to a local model" servers, memory/knowledge-graph servers,
and structural or SQL-backed code-navigation servers that stand in for
`Grep`/`Read`/`Glob`. This feature answers the only question that
actually matters about any of them: once its own overhead is paid for,
does it net save anything?

`savers.py` does this. It never assumes a name is a saver by guesswork
alone (an explicit `config.toml` allowlist is always honoured), and it
never reports its present/absent comparison as anything stronger than
"observed, not controlled" — the same causality caveat `compare.py`'s
own module docstring states about its arm comparisons.

## What this is, precisely

Three stages:

1. **Detection** (`detect_savers`) — merges an explicit `config.toml`
   `[savers]` list with auto-detection: every MCP server name observed
   in a turn's own `attribution_mcp_server` or in an
   `mcp__<server>__<tool>` tool-call name, every name recorded in a
   config snapshot's `mcp_servers.names`/`enabled_plugins`, and every
   `attribution_skill` value, checked against a fixed, case-insensitive
   regex: `token|saver|savior|optimi[sz]|compress|context|memory|cache|
   lean|trim|condens`. A name matching only via the regex is a
   candidate on lexical grounds alone — it is not a claim the tool does
   anything. A name with no lexical hint at all (a codename, an
   acronym) is only ever found via the explicit `[savers]` list.
2. **ROI** (`compute_saver_roi`) — for each detected candidate:
   - **Overhead**: turns and cost directly attributed to the saver,
     its own tool-call count and mean result size, and a distinct-tool-
     name count as a schema/prefix-load footprint proxy (no tokenizer
     runs over the actual schema bytes anywhere in this codebase).
   - **Effect**: cost/session, new tokens/session, cache-creation/turn,
     mean tool-result tokens/turn, re-cache share, compactions/session
     and turns/session, compared between sessions where the saver was
     present versus absent, both overall and stratified by
     purpose/mode — every arm below `min_sessions_per_arm` (default 5,
     mirroring `Config.min_sessions`) is marked `sample_ok = no` rather
     than shown as if it were reliable.
   - **Search substitution**: for a saver whose real job might be a
     code-search replacement — native `Grep`/`Glob`/`Read`/shell-search
     calls per session, the saver's own tool calls per session, mean
     result size on each side, and the carry-cost implication of each
     (reusing `carry.compute_carry` per arm, the same per-turn
     read/write pricing model the `carry` report section itself uses).
   - **Verdict**: net saving per session = (cost/session absent −
     cost/session present) − overhead/session, labelled
     "observed, not controlled" and naming any other config key that
     also differed between the two arms' representative snapshots (so a
     saver never gets sole credit, or sole blame, for a change that
     coincided with something else changing).
3. **Rendering** (`build_section`) and **recommending**
   (`RULES["saver-tool-roi"]`) — see below.

## The `savers` report section

| Table | What it shows |
|---|---|
| `savers_detected` | One row per candidate: name, whether it came from `config.toml`'s `[savers]` list, and every auto-detection source it matched. |
| `savers_overhead` | One row per candidate: attributed turns and cost, tool-call count, total and mean tool-result size, and the distinct-tool-name schema-footprint proxy. |
| `savers_effect_by_stratum` | One row per (candidate, stratum) — `"all"` plus one row per observed purpose/mode combination — with present-arm and absent-arm figures side by side for every effect metric, and a sample-OK flag. |
| `savers_search_substitution` | One row per (candidate, arm): native search calls/session, the saver's own calls/session, mean result size on each side, and each side's carry cost/session. |
| `savers_verdict` | One row per candidate: cost/session on each arm, gross saving, overhead, net saving, other co-changed config keys, and a keep/disable/inconclusive verdict. |

When no candidate is detected at all, every table renders empty and the
section carries a note pointing at `config.toml`'s `[savers]` table —
this is the expected result on a machine that runs no saver, and is
itself informative (see "Reading an empty report" below).

## Reading the `savers_search_substitution` table

Two counting conventions are mixed deliberately, and stated in both the
table's own notes and `ASSUMPTIONS`, rather than silently blended into
one figure: a `Grep`/`Glob`/`Read` call is counted per (turn, tool)
pair (`carry.compute_carry`'s own grain), while a Bash/PowerShell search
call (a command whose redacted prefix starts `rg`/`grep`/`find`/
`Select-String`) is counted once per *turn*, because the frozen `Turn`
contract keeps only that turn's first shell command as `cmd_prefix`. A
turn that ran `rg` twice is indistinguishable from one that ran it
once.

## The `saver-tool-roi` recommendation

`RULES["saver-tool-roi"]` reads the *rendered* `savers_verdict` (and,
when it helps the case, `savers_search_substitution`) table — never the
raw stats, the same "rules only read the finished report" convention
every other standalone `RULES` module in this codebase follows — and
fires one `Recommendation` per candidate whose verdict row is
`sample_ok = yes` and whose net saving per session clears
`SaverThresholds.net_saving_usd_min` (default $0.01) in either
direction:

- **Net saving positive** — "keep it enabled". If the saver's own
  overhead exceeds `overhead_share_pct` (default 20%) of its gross
  saving, the action additionally suggests asking it to return smaller
  results. If its search-substitution row shows both fewer native
  search calls and a smaller mean result size in the present arm, the
  action cites both as supporting evidence — the owner's own working
  theory about their employer's saver being a code-search replacement.
- **Net saving negative** — "not paying for itself", naming the
  projected saving from disabling it.

The lever is always `mcpServers.<name>`, even for a candidate found
only as a plugin, scoped `"managed"` when `mcpServers`/`enabledPlugins` appears in the
joined snapshot's `managed_keys` (the org-pushed managed-settings case),
`"user"` otherwise.

## Reading an empty report

A `savers_detected` table with no rows is not a bug: it means nothing
in the observed corpus's MCP servers, plugins, or skills matched the
detection regex or the explicit allowlist. If you believe you *do* run
a saver — the common case is a workplace-provided MCP server whose name
gives no lexical hint (a codename, a project acronym) — add it to
`config.toml` explicitly:

```toml
[savers]
names = ["my-mcp-server"]
```

Check the Config report section, or run `claude-token-lens
probe-config`, for the exact MCP server and plugin names this tool
already has on file before typing one in.

## API and wiring

Nothing calls this module yet: `report.build_report` does not add the
`savers` section, `recommend.recommend()` does not run its rule, and no
CLI subcommand or dashboard tab prints it. That is deliberate until
these are fixed:

- Detection is a name match, so ordinary tools whose names contain
  "context", "memory" or "cache" (a documentation server, a notes
  server) become candidates.
- A saver configured for every session leaves no "absent" sessions, and
  when both groups exist they differ in workload, so the verdict cannot
  separate the tool from the work.
- The rule's lever, `mcpServers.<name>`, is not a settings key the
  report's fixes can change (MCP servers live in `.mcp.json` or
  `~/.claude.json`), and its action text writes dollar amounts directly
  rather than following the billing mode.

Call it directly:

```python
from claude_token_lens import savers

candidates = savers.detect_savers(results, snapshots, config)         # list[SaverCandidate]
stats = savers.compute_saver_roi(results, sessions, snapshots, pricing, thresholds)
section = savers.build_section(stats, thresholds)                     # -> Section(key="savers", ...)
recs = savers.RULES["saver-tool-roi"](report, thresholds, snapshot)
```

- `savers.detect_savers(results, snapshots, config) -> list[SaverCandidate]`
- `savers.compute_saver_roi(results, sessions, snapshots, rates, thresholds=None) -> SaverStats`
- `savers.build_section(stats, thresholds=None) -> Section`
- `savers.RULES["saver-tool-roi"](report, thresholds, snapshot=None) -> list[Recommendation]`
- `savers.SaverThresholds` — `min_sessions_per_arm` (5), `overhead_share_pct` (20.0), `net_saving_usd_min` (0.01), `configured_names` (from `config.savers`); `.from_config(data, config=None)` and `.describe()` follow the same convention as `RecommendThresholds`/`ScorecardThresholds`.
- `savers.ASSUMPTIONS` — this module's own modelling caveats; fold into any parent "assumptions" listing.

To wire it in, fold `savers.build_section(...)`'s `Section` into the
report's section list and `RULES["saver-tool-roi"]` into
`recommend.recommend()` the way `carry.RULES`/`waste.RULES` already
are. `compute_saver_roi` takes
`sessions` as already-built `SessionRecord`s (`classify.
build_session_record`'s own output) — it does not classify sessions
itself.
