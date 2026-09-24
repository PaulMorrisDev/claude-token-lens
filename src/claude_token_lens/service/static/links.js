/* claude-token-lens service UI: links.js
 *
 * Where things live: which tab shows each report section, tab titles
 * and intros, and links between tabs.
 */

import { el, showTab } from "./core.js";

// Section key -> tab, per this work package's brief. Anything not
// listed here lands in Diagnostics so a new section is never silently
// dropped when report.py grows one.
export var SECTION_TAB_MAP = {
  recache: "cache",
  // Review finding 20: docs/ui.md documents recache_by_group as part of
  // this map too. It never actually arrives as a section's own `key`
  // today -- report.py's _build_recache_section appends it as an extra
  // *table* inside the "recache" section rather than a section in its
  // own right -- but it costs nothing to map here now, so a future
  // refactor that promotes it to its own section lands on the Cache
  // tab without anyone having to remember to update this file too.
  recache_by_group: "cache",
  // v3-limits wiring: usage-cap pauses force the exact full-expiry
  // re-cache recache/ttl already attribute cost to -- grouped onto
  // Cache alongside them rather than Diagnostics's parse-quality
  // counters.
  limits: "cache",
  ttl: "ttl",
  agent_startup: "agents",
  agents: "agents",
  quality: "agents",
  workflows: "agents",
  workstyle: "agents",
  habits: "habits",
  // The capture section's one table is report-only; the Capture tab
  // shows its own figures from /api/capture.
  capture: "capture",
  sessions: "sessions",
  // The Config tab renders the config section's tables once, from
  // /api/config-diff?auto_keys=1 (renderConfig skips it here).
  config: "config",
  context_budget: "config",
  baseline_comparison: "config",
  // The scorecard is the Overview tab's tiles, never a generic table.
  scorecard: "overview",
  usage: "usage",
  // Subscription only: how many tokens a usage limit holds.
  elasticity: "usage",
  compactions: "usage",
  phases: "usage",
  // v4 wiring round: carry/compaction_sim/model_swap/waste each have
  // their own dedicated report-backed route (/api/carry etc., fetched
  // directly by the Savings tab in page-spend.js, the same way ttl's own
  // entry above keeps it off Diagnostics even though nothing ever calls
  // renderMappedSections(report, "ttl", ...)) -- mapped here purely so
  // they don't fall through to the Diagnostics tab's default when the
  // full report.json is walked there.
  carry: "savings",
  compaction_sim: "savings",
  model_swap: "savings",
  waste: "savings",
};

// ======================================================================
// Tab titles, intros and cross-tab links
// ======================================================================

// One h2 per tab: its title (matching index.html's tab button) and a
// one-line intro saying what question the tab answers.
var TAB_TITLES = {
  overview: "Overview",
  quick: "Quick actions",
  sessions: "Sessions",
  cache: "Cache",
  ttl: "Cache lifetime (TTL)",
  savings: "Savings",
  agents: "Agents",
  context: "Context files",
  config: "Config",
  profiles: "Profiles",
  recommendations: "Recommendations",
  usage: "Usage",
  habits: "Work habits",
  capture: "Capture",
  diagnostics: "Data quality",
  glossary: "Glossary",
};

var TAB_INTROS = {
  overview: "Your totals for the window, and a scorecard of where your tokens go.",
  quick:
    "One question per way of saving tokens, answered from your own sessions in the window, with the evidence and a fix you can copy. Nothing here changes Claude Code by itself.",
  sessions: "Every session, newest first. Pick one to see its replies on a timeline.",
  cache:
    "When Claude Code had to rebuild the prompt cache, and why. A rebuild writes the whole conversation to the cache again, at the cache-write price.",
  ttl: "How long the prompt cache stays warm, and whether a longer cache lifetime would have paid for itself.",
  savings:
    "Estimates of what you could save: shorter tool output, earlier conversation summaries, cheaper models, and replies that did no useful work.",
  agents: "What your subagents cost, what they are given when they start, and what they hand back.",
  context:
    "What Claude reads at the start of every session and subagent: your CLAUDE.md files and the skills list. How often each is sent, what it costs, and how to trim it.",
  config: "Your Claude Code settings, how they changed, and how much of the context window is used before you type.",
  profiles:
    "Groups of settings: make one from a goal with an estimate of its effect, compare it with yours, apply it, and see what each change you made did.",
  recommendations: "Changes worth making, most important first.",
  usage: "Usage over time, by project, and in five-hour blocks.",
  habits:
    "How the way you work shapes what it costs, and the habits that would have saved the most in your own sessions, with an example to copy for each.",
  capture:
    "Metrics capture: short notes and tags that tell this tool what each piece of work was and how it went, so suggestions fit how you work. Choose how much, and see what it costs.",
  diagnostics:
    "What this tool installed and what to expect, how much of your data could be read, and anything the parser had to skip.",
  glossary: "The words this dashboard uses, in plain English.",
};

export function tabHeading(panel, tabKey) {
  panel.appendChild(el("h2", { text: TAB_TITLES[tabKey] || tabKey }));
  if (TAB_INTROS[tabKey]) panel.appendChild(el("p", { class: "tab-intro", text: TAB_INTROS[tabKey] }));
}

export function tabLink(tabKey, text) {
  var link = el("button", { type: "button", class: "link-button", text: text });
  link.addEventListener("click", function () {
    showTab(tabKey, { focus: true });
  });
  return link;
}

export function captureTabLink(text) {
  return tabLink("capture", text);
}
