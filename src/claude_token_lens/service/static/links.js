/* claude-token-lens service UI: links.js
 *
 * Where things live: the sidebar's pages and their segments, which page
 * shows each report section and table, the #/page/segment routes, and
 * links between pages.
 */

import { el, goTo } from "./core.js";

// ======================================================================
// Pages and segments
// ======================================================================

// The sidebar, in order. A page with segments shows them as a segmented
// control under its title; a page without shows its own intro. `foot`
// pages sit at the bottom of the sidebar. `window: false` marks a page
// whose figures don't depend on the window, so the header says so
// instead of offering the picker. Ids are lower-case words joined by
// hyphens: they appear in the address bar (#/spend/usage).
export var PAGES = [
  {
    id: "overview",
    label: "Overview",
    icon: "overview",
    intro: "What to change next, and where your tokens went in this window.",
  },
  {
    id: "actions",
    label: "Actions",
    icon: "actions",
    segments: [
      {
        id: "recommendations",
        label: "Recommendations",
        intro: "Changes worth making, most important first. Each one comes with a prompt or command you can copy.",
      },
      {
        id: "checks",
        label: "Checks",
        intro:
          "One question per way of saving tokens, answered from your own sessions, with the evidence and a fix you can copy. Nothing here changes Claude Code by itself.",
      },
    ],
  },
  {
    id: "spend",
    label: "Spend",
    icon: "spend",
    segments: [
      { id: "usage", label: "Usage", intro: "Usage over time, by model and project, and in five-hour blocks." },
      {
        id: "savings",
        label: "Savings",
        intro:
          "What you could save: shorter tool output, earlier conversation summaries, cheaper models, and replies that did no useful work.",
      },
      { id: "sessions", label: "Sessions", intro: "Every session, newest first. Pick one to see its replies on a timeline." },
    ],
  },
  {
    id: "cache",
    label: "Cache",
    icon: "cache",
    segments: [
      {
        id: "rebuilds",
        label: "Rebuilds",
        intro:
          "When Claude Code had to rebuild the prompt cache, and why. A rebuild writes the whole conversation to the cache again, at the cache-write price.",
      },
      {
        id: "lifetime",
        label: "Lifetime (TTL)",
        intro: "How long the prompt cache stays warm, and whether a longer cache lifetime would have paid for itself.",
      },
    ],
  },
  {
    id: "agents",
    label: "Agents & context",
    icon: "agents",
    segments: [
      { id: "subagents", label: "Subagents", intro: "What your subagents cost, and what each one is given when it starts." },
      { id: "quality", label: "Quality", intro: "How subagent work went: runs that were retried, workflows, and how you split the work." },
      {
        id: "context",
        label: "Context",
        intro:
          "What Claude reads at the start of every session and subagent: your CLAUDE.md files and the skills list. How often each is sent, what it costs, and how to trim it.",
      },
    ],
  },
  {
    id: "habits",
    label: "Work habits",
    icon: "habits",
    intro:
      "How the way you work shapes what it costs. The habits that would have saved the most in your own sessions, each with an example to copy.",
  },
  {
    id: "setup",
    label: "Setup",
    icon: "setup",
    segments: [
      {
        id: "settings",
        label: "Settings",
        intro: "Your Claude Code settings, how they changed, and how this window compares with your baseline.",
      },
      {
        id: "profiles",
        label: "Profiles",
        intro:
          "Groups of settings. Make one from a goal with an estimate of its effect, compare it with yours, and see what each change you made did.",
      },
      {
        id: "capture",
        label: "Capture",
        window: false,
        intro:
          "Metrics capture: short tags that tell Token Lens what each piece of work was and how it went, so suggestions fit how you work. Choose how much, and see what it costs.",
      },
    ],
  },
  {
    id: "data",
    label: "Data quality",
    icon: "data",
    foot: true,
    intro: "What Token Lens installed and what to expect, how much of your data it could read, and anything it had to skip.",
  },
  {
    id: "glossary",
    label: "Glossary",
    icon: "glossary",
    foot: true,
    window: false,
    intro: "The words this dashboard uses, in plain English.",
  },
];

export function findPage(pageId) {
  for (var i = 0; i < PAGES.length; i++) {
    if (PAGES[i].id === pageId) return PAGES[i];
  }
  return null;
}

export function findSegment(page, segmentId) {
  var segments = (page && page.segments) || [];
  for (var i = 0; i < segments.length; i++) {
    if (segments[i].id === segmentId) return segments[i];
  }
  return null;
}

// Every view a route can show, as "page" or "page/segment", in sidebar
// order: what app.js mounts a renderer for.
export var VIEW_KEYS = PAGES.reduce(function (keys, page) {
  if (!page.segments) return keys.concat([page.id]);
  return keys.concat(
    page.segments.map(function (segment) {
      return page.id + "/" + segment.id;
    })
  );
}, []);

// The page and segment a view key names, or null for an unknown key.
export function viewFor(key) {
  var parts = String(key || "").split("/");
  var page = findPage(parts[0]);
  if (!page) return null;
  if (!page.segments) return parts.length === 1 ? { key: page.id, page: page, segment: null } : null;
  var segment = findSegment(page, parts[1]);
  return segment ? { key: page.id + "/" + segment.id, page: page, segment: segment } : null;
}

// ======================================================================
// Routes: #/<page>[/<segment>][?w=<window>&...]
// ======================================================================

// A route from location.hash. A page with segments and none named (or
// an unknown one) gets segment null, for the router to fill in with the
// page's last-used or first segment. Anything else is not a route.
export function parseHash(hash) {
  var match = /^#\/([a-z]+(?:-[a-z]+)*)(?:\/([a-z]+(?:-[a-z]+)*))?(?:\?(.*))?$/.exec(hash || "");
  if (!match) return null;
  var page = findPage(match[1]);
  if (!page) return null;
  var segment = page.segments ? findSegment(page, match[2]) : null;
  var params = {};
  new URLSearchParams(match[3] || "").forEach(function (value, name) {
    params[name] = value;
  });
  return { page: page, segment: segment, params: params };
}

// The hash for a view key and its parameters; the window (w) goes first
// so every address says which window it shows.
export function formatHash(key, params) {
  var query = new URLSearchParams();
  var names = Object.keys(params || {}).sort(function (a, b) {
    return (a === "w" ? 0 : 1) - (b === "w" ? 0 : 1) || (a < b ? -1 : a > b ? 1 : 0);
  });
  names.forEach(function (name) {
    var value = params[name];
    if (value !== null && value !== undefined && value !== "") query.set(name, value);
  });
  var text = query.toString();
  return "#/" + key + (text ? "?" + text : "");
}

// The old tab keys (tls:activeTab, before the sidebar), for the one-time
// move to a route.
export var OLD_TAB_VIEWS = {
  overview: "overview",
  quick: "actions/checks",
  recommendations: "actions/recommendations",
  sessions: "spend/sessions",
  savings: "spend/savings",
  usage: "spend/usage",
  cache: "cache/rebuilds",
  ttl: "cache/lifetime",
  agents: "agents/subagents",
  context: "agents/context",
  habits: "habits",
  config: "setup/settings",
  profiles: "setup/profiles",
  capture: "setup/capture",
  diagnostics: "data",
  glossary: "glossary",
};

// ======================================================================
// Which view shows each report section and table
// ======================================================================

// Section key -> view. Anything not listed lands on Data quality, so a
// new section is never silently dropped when report.py grows one.
export var SECTION_PAGE_MAP = {
  // The Overview draws these itself: the scorecard as tiles, the
  // overview section's totals under Details (its other tables are placed
  // by TABLE_PAGE_MAP).
  scorecard: "overview",
  overview: "overview",
  // Spend.
  usage: "spend/usage",
  // Subscription only: how many tokens a usage limit holds.
  elasticity: "spend/usage",
  compactions: "spend/usage",
  phases: "spend/usage",
  // Savings reads each of these from its own report-backed route
  // (/api/carry and the rest); mapped so they never fall through to
  // Data quality when the whole report is walked.
  carry: "spend/savings",
  compaction_sim: "spend/savings",
  model_swap: "spend/savings",
  waste: "spend/savings",
  sessions: "spend/sessions",
  // Cache. recache_by_group arrives today as a table inside recache;
  // mapped too, so a report that promotes it to its own section still
  // lands here. Usage-limit pauses force the same full re-write the
  // rebuild sections count, so they sit beside them.
  recache: "cache/rebuilds",
  recache_by_group: "cache/rebuilds",
  limits: "cache/rebuilds",
  // Lifetime reads /api/ttl; mapped for the same reason as Savings.
  ttl: "cache/lifetime",
  // Agents & context.
  agent_startup: "agents/subagents",
  agents: "agents/subagents",
  quality: "agents/quality",
  workflows: "agents/quality",
  workstyle: "agents/quality",
  context_budget: "agents/context",
  habits: "habits",
  // Setup. Settings draws the config section's tables once, from
  // /api/config-diff?auto_keys=1, and skips the section itself.
  config: "setup/settings",
  baseline_comparison: "setup/settings",
  // The capture section's one table is report-only; the Capture segment
  // shows its own figures from /api/capture.
  capture: "setup/capture",
};

// "section.table" -> view, for a table that lives somewhere other than
// its section's view. Checked before SECTION_PAGE_MAP.
export var TABLE_PAGE_MAP = {
  // The Overview's totals stay there; cost by model is spend detail.
  "overview.totals": "overview",
  "overview.by_model": "spend/usage",
  // Which setup worked best for each kind of task feeds profiles.
  "habits.habits_setups": "setup/profiles",
};

export function viewForSection(sectionKey) {
  return SECTION_PAGE_MAP[sectionKey] || "data";
}

export function viewForTable(sectionKey, tableName) {
  return TABLE_PAGE_MAP[sectionKey + "." + tableName] || viewForSection(sectionKey);
}

// ======================================================================
// Page intros and links
// ======================================================================

// The one line under the page header saying what the view answers. The
// view's title is the page header's h1, so a view adds no heading of
// its own.
export function viewIntro(container, key) {
  var view = viewFor(key);
  var text = view ? (view.segment ? view.segment.intro : view.page.intro) : "";
  if (text) container.appendChild(el("p", { class: "view-intro", text: text }));
}

// A link to another view: a real #/ address (so it opens in a new tab
// and shows in the status bar), which moves focus to the new page's
// title when followed here. Without text it reads as the view's name.
export function pageLink(key, text) {
  var link = el("a", { class: "page-link", href: formatHash(key, {}), text: text || viewLabel(key) });
  link.addEventListener("click", function (event) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    goTo(key, { focus: true });
  });
  return link;
}

export function captureLink(text) {
  return pageLink("setup/capture", text);
}

// The page and segment a view key names, as the reader sees it:
// "Spend › Usage" (the same in the README and docs).
export function viewLabel(key) {
  var view = viewFor(key);
  if (!view) return key;
  return view.segment ? view.page.label + " \u203a " + view.segment.label : view.page.label;
}
