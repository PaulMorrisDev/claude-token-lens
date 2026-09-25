/* claude-token-lens service UI: page-overview.js
 *
 * The Overview page answers "What should I change next?": a sentence
 * saying how this window went, four headline numbers against the period
 * before, daily spend beside the next best actions, how the setup
 * scores, and the totals behind a disclosure.
 */

import { clear, el, goTo, state, WINDOW_OPTIONS } from "./core.js";
import { formatCell, fraction, money, moneyParts, projectName, thousands } from "./format.js";
import { fetchJson, findSection, loadRecommendations, loadReport, prefetchActions, withProject, withWindow } from "./api.js";
import {
  button,
  copyToClipboard,
  countUp,
  deltaChip,
  emptyState,
  enterInTurn,
  errorNotice,
  loadingNode,
  severityChip,
  tile,
  tileRow,
  timesText,
  toast,
} from "./ui.js";
import { renderTable } from "./grid.js";
import { pageLink, viewIntro } from "./links.js";
import { renderLogonNotice } from "./shell.js";
import { chartError, holdChart, setChartHeight } from "./charts.js";
import { dailyChanges, meter, renderChart, savingsLevers, sparkline, windowDays } from "./charts-types.js";
import { groupRecommendations, groupSavingUsd, groupTitle, listSaving } from "./page-actions.js";

// A page draw that a newer one (a new window) has replaced: its late
// answers are dropped, so they can't take the chart back.
var overviewRun = 0;

// The entrance (docs/ui.md, "Motion"): the headline figures count up,
// the chart draws in 80ms after they start, and the next best actions
// arrive in turn 80ms after that. Under reduced motion nothing moves.
var CHART_AFTER_MS = 80;
var ACTIONS_AFTER_MS = 160;

// The headline figures last drawn, by tile: a new window counts up from
// them, the first draw from 0.
var shownFigures = null;

// -- the period before -----------------------------------------------------------

var DAY_MS = 24 * 3600 * 1000;

// The period of the same length just before this window, for the
// deltas: its bounds, and how the sentence names it. None for "all" and
// "since my last change", which have nothing the same length before them.
function previousPeriod(windowValue, now) {
  var end;
  var length;
  if (/^[0-9]+$/.test(windowValue)) {
    var days = Number(windowValue);
    length = days * DAY_MS;
    end = now - length;
    return { since: end - length, until: end, phrase: days === 1 ? "the day before" : "the " + days + " days before" };
  }
  if (windowValue === "1h") return { since: now - 2 * 3600 * 1000, until: now - 3600 * 1000, phrase: "the hour before" };
  if (windowValue === "24h") return { since: now - 2 * DAY_MS, until: now - DAY_MS, phrase: "the 24 hours before" };
  if (windowValue === "today") {
    // The same hours yesterday: local midnight to this time, a day back.
    var midnight = new Date(now);
    midnight.setHours(0, 0, 0, 0);
    return { since: midnight.getTime() - DAY_MS, until: now - DAY_MS, phrase: "the same hours yesterday" };
  }
  return null;
}

function isoMinute(ms) {
  return new Date(ms).toISOString().slice(0, 16) + ":00Z";
}

// -- the summary sentence ----------------------------------------------------------

function windowLabel(value) {
  for (var i = 0; i < WINDOW_OPTIONS.length; i++) {
    if (WINDOW_OPTIONS[i].value === value) return WINDOW_OPTIONS[i].label;
  }
  return /^[0-9]+$/.test(value) ? "Last " + value + " days" : "This window";
}

// "No sessions <when>."
function windowWhen(value) {
  if (/^[0-9]+$/.test(value)) return value === "1" ? "in the last day" : "in the last " + value + " days";
  return { "1h": "in the last hour", today: "today", "24h": "in the last 24 hours", change: "since your last change" }[value] || "yet";
}

// What the window cost, as a clause: dollars on the API, a share of the
// weekly limit on a Pro or Max plan.
function spendClause(usd) {
  var amount = money(usd);
  if (!amount) return "nothing was spent";
  var units = state.units || {};
  if (units.mode !== "subscription") return "you spent " + amount.primary;
  if (units.share_per_usd === null || units.share_per_usd === undefined) return "your tokens came to " + amount.primary;
  return "you used " + amount.primary;
}

function changeClause(current, previous, phrase) {
  if (!phrase || typeof previous !== "number" || previous < 0 || typeof current !== "number") return "";
  if (previous === 0) return current > 0 ? ", with nothing in " + phrase : "";
  if (current / previous >= 3) return ", " + timesText(current / previous) + " as much as " + phrase;
  var change = ((current - previous) / previous) * 100;
  if (Math.abs(change) < 1) return ", about the same as " + phrase;
  var rounded = Math.abs(change) >= 10 ? Math.round(Math.abs(change)) : Math.round(Math.abs(change) * 10) / 10;
  return ", " + rounded + "% " + (change > 0 ? "more" : "less") + " than " + phrase;
}

// The window, and the project when one is picked: "Last 30 days in
// claude-token-lens".
function scopeLabel() {
  return windowLabel(state.window) + (state.project ? " in " + projectName(state.project) : "");
}

// Built from fixed wording and numbers only (docs/writing-help.md).
function summarySentence(facts) {
  var first = scopeLabel() + ": " + spendClause(facts.cost) + changeClause(facts.cost, facts.previousCost, facts.phrase) + ".";
  var saving = money(facts.saving);
  var worth = facts.worth;
  var second;
  if (worth && saving) {
    second = (worth === 1 ? "1 change is" : thousands(worth) + " changes are") + " worth making, and the ways to save come to at most " + saving.primary + ".";
  } else if (worth) {
    second = worth === 1 ? "1 change is worth making." : thousands(worth) + " changes are worth making.";
  } else if (saving) {
    second = "Nothing needs changing now, but the ways to save come to at most " + saving.primary + ".";
  } else {
    second = "Nothing stands out to change.";
  }
  return first + " " + second;
}

// Before any session is read: what the tool does, in three lines.
function firstRun(container, health) {
  var scanning = health && (health.status === "starting" || (health.scan && health.scan.scanning));
  container.appendChild(
    el("div", { class: "overview-first-run" }, [
      scanning
        ? el("p", { class: "overview-summary", text: "Your Claude Code history is being read now. Figures appear here as soon as it finishes." })
        : el("p", { class: "overview-summary" }, [
            "No Claude Code sessions have been read yet. They appear here soon after you use Claude Code. ",
            pageLink("data", "Data quality"),
            " shows how many transcript files the last scan checked.",
          ]),
      el("h2", { text: "What Token Lens does for you" }),
      el("ul", { class: "overview-promise" }, [
        el("li", { text: "Reads the Claude Code transcripts on this computer and works out what each session cost. Nothing leaves your machine." }),
        el("li", { text: "Shows where the tokens went: the cache, subagents, long tool output and conversation summaries." }),
        el("li", { text: "Suggests changes as prompts and commands you copy and run yourself. It never changes your settings." }),
      ]),
    ])
  );
}

// -- the four tiles --------------------------------------------------------------------

// The price of a cache read against fresh input on the model that read
// the most from the cache in this window (report.meta.rates; the daily
// rows say which model read what).
function cacheReadRatio(meta, dailyRows) {
  var rates = (meta && meta.rates) || {};
  var reads = {};
  (dailyRows || []).forEach(function (row) {
    if (row.model) reads[row.model] = (reads[row.model] || 0) + (Number(row.cache_read_tokens) || 0);
  });
  var top = Object.keys(reads).sort(function (a, b) {
    return reads[b] - reads[a];
  })[0];
  var ratio = top && rates[top] && rates[top].cache_read_ratio;
  return typeof ratio === "number" && ratio > 0 ? ratio : null;
}

function moneyTile(label, usd, opts) {
  var parts = moneyParts(usd);
  return tile({
    label: label,
    value: parts.value,
    unit: parts.unit,
    basis: opts.basis,
    delta: opts.delta,
    hint: opts.hint || parts.secondary || null,
    note: opts.note,
    link: opts.link,
    class: opts.class,
  });
}

// Actions whose saving a Savings lever already counts.
var LEVER_RULES = { "model-tier": "model_swap", "model-tier-main": "model_swap" };

// The most the ways to save could come to: the four Savings levers, plus
// any priced action no lever counts (lower effort, say). An action is an
// item on Actions (groupRecommendations), counted once with all its
// agent types. Every action's own saving is then at or below it.
function availableSaving(levers, groups) {
  var total = levers.reduce(function (sum, lever) {
    return sum + (lever.usd > 0 ? lever.usd : 0);
  }, 0);
  groups.forEach(function (group) {
    if (!LEVER_RULES[group.id]) total += groupSavingUsd(group);
  });
  return total;
}

function renderTiles(container, facts, meta, dailyRows) {
  var period = facts.phrase;
  var hasPrevious = !!period;
  var saving = facts.available;
  var ratio = cacheReadRatio(meta, dailyRows);
  var ratioWords = ratio ? fraction(ratio) : "";
  var subagentRuns = Math.max(0, (facts.summary.transcripts || 0) - (facts.summary.sessions || 0));

  // Whole sessions (/api/summary): the daily spend chart counts replies
  // by the day they were sent, and says so beside this figure.
  var spend = moneyTile("Spend", facts.cost, {
    delta: hasPrevious ? deltaChip(facts.cost, facts.previousCost, { period: period }) : null,
    note: "Every session with a reply in this window, earlier replies included.",
    class: "overview-spend",
  });
  var available = saving > 0
    ? moneyTile("Available saving", saving, {
        basis: "ceiling",
        note: "The ways to save overlap, so together they save less than this.",
        link: pageLink("spend/savings", "See the ways to save"),
      })
    : tile({ label: "Available saving", value: "None found", note: "Nothing in this window stands out as a saving.", link: pageLink("spend/savings", "See the ways to save") });
  var cache = moneyTile("Saved by the cache", facts.summary.cache_saved || 0, {
    basis: "estimate",
    delta: hasPrevious ? deltaChip(facts.summary.cache_saved, facts.previousSummary && facts.previousSummary.cache_saved, { period: period, upIsGood: true }) : null,
    note: ratioWords
      ? "On the model you use most, a cache read costs " + ratioWords + " the input price. This is what those reads would have cost sent fresh."
      : "What your cache reads would have cost sent fresh.",
    link: pageLink("cache/rebuilds", "See how the cache is doing"),
  });
  var sessions = tile({
    label: "Sessions",
    value: thousands(facts.summary.sessions || 0),
    hint: subagentRuns ? "and " + thousands(subagentRuns) + (subagentRuns === 1 ? " subagent run" : " subagent runs") : "No subagent runs",
    link: pageLink("spend/sessions", "See the sessions"),
  });
  container.appendChild(tileRow([spend, available, cache, sessions], { class: "overview-tiles" }));
  var counts = [tileCount(spend, "spend", facts.cost, moneyValue), tileCount(cache, "cache", facts.summary.cache_saved || 0, moneyValue), tileCount(sessions, "sessions", facts.summary.sessions || 0, wholeNumber)];
  if (saving > 0) counts.push(tileCount(available, "available", saving, moneyValue));
  return { spend: spend, saving: saving, counts: counts };
}

// A tile's figure as it counts up: its node, the value it ends on and how
// to write the values on the way (money in the billing mode's units).
function tileCount(tileNode, key, value, write) {
  return { node: tileNode.querySelector(".metric-value > span"), key: key, value: value, write: write };
}

function moneyValue(usd) {
  return moneyParts(usd).value;
}

function wholeNumber(n) {
  return thousands(Math.round(n));
}

// Count each headline figure up from the one this tile last showed.
function countTiles(counts) {
  var from = shownFigures || {};
  shownFigures = {};
  counts.forEach(function (count) {
    countUp(count.node, from[count.key] || 0, count.value, count.write);
    shownFigures[count.key] = count.value;
  });
}

// The Spend tile's trend: one point a day, from 3 days up.
function addSpendTrend(spendTile, rows) {
  if (!spendTile) return;
  var byDay = {};
  rows.forEach(function (row) {
    if (row.day) byDay[row.day] = (byDay[row.day] || 0) + (Number(row.cost) || 0);
  });
  var days = Object.keys(byDay).sort();
  if (days.length < 3) return;
  var line = sparkline(
    days.map(function (day) {
      return byDay[day];
    }),
    { width: 120, height: 24 }
  );
  if (line) spendTile.appendChild(el("div", { class: "metric-spark", title: "Daily spend, " + thousands(days.length) + " days" }, [line]));
}

// -- the next best actions ---------------------------------------------------------------

// about: the action it copies for. The button shows "Copy prompt" and is
// named "Copy prompt for <action>", as codeBlockWithCopy names its
// buttons, so five of them read as five different things.
function copyPromptButton(prompt, about) {
  var node = button("Copy prompt", { variant: "quiet", icon: "prompt", class: "action-copy", label: "Copy prompt" + (about ? " for " + about : "") });
  node.addEventListener("click", function () {
    copyToClipboard(prompt).then(function (ok) {
      toast(ok ? "Prompt copied. Paste it into Claude Code." : "Couldn't copy. Open the action and copy the prompt from there.", {
        tone: ok ? "success" : "warning",
      });
    });
  });
  return node;
}

// The first five items on Actions › Recommendations, as it lists them
// (groupRecommendations): a rule for several agent types is one item,
// titled for all of them, and opens as one.
function renderActions(container, groups) {
  clear(container);
  if (!groups.length) {
    container.appendChild(emptyState("Nothing stands out to change in this window.", null, "Pick a longer window, or check back after more sessions."));
    return;
  }
  var list = el("ol", { class: "next-actions" });
  groups.slice(0, 5).forEach(function (group) {
    var many = group.members.length > 1;
    var fix = (group.members[0].fixes || [])[0];
    var title = groupTitle(group);
    var text = el("div", { class: "next-action-text" }, [el("p", { class: "next-action-title" }, [pageLink("actions/recommendations", title, { id: group.key })])]);
    var saving = listSaving(group);
    if (saving) text.appendChild(el("p", { class: "next-action-saving", text: saving }));
    var item = el("li", { class: "next-action" }, [el("div", { class: "next-action-severity" }, [severityChip(group.severity)]), text]);
    // Several agent types have a prompt each: Actions lists them.
    if (many) item.appendChild(el("div", { class: "next-action-copy" }, [pageLink("actions/recommendations", "See the " + thousands(group.members.length) + " prompts", { id: group.key })]));
    else if (fix && fix.prompt) item.appendChild(el("div", { class: "next-action-copy" }, [copyPromptButton(fix.prompt, title)]));
    list.appendChild(item);
  });
  container.appendChild(list);
  container.appendChild(
    el("p", { class: "next-actions-more" }, [
      pageLink("actions/recommendations", groups.length > 5 ? "See all " + thousands(groups.length) + " recommendations" : "See the recommendations in full"),
    ])
  );
}

// -- how the setup scores ----------------------------------------------------------------

// Each area as a sentence about its number, and which way is better
// (scorecard.py's metrics; helptext.py's "dimensions").
var DIMENSION_TEXT = {
  cache_efficiency: {
    sentence: function (v) {
      return formatCell(v, "pct") + " of cache writes rebuilt context that had expired or changed.";
    },
    better: "Lower is better: a rebuild pays again for context you already had.",
    area: "cache/rebuilds",
  },
  context_hygiene: {
    sentence: function (v) {
      return "9 in 10 main session replies carried less than " + formatCell(v, "tokens") + " tokens of context.";
    },
    better: "Lower is better: every reply pays to re-read its whole context.",
    area: "spend/usage",
  },
  agent_efficiency: {
    sentence: function (v) {
      return "Your costliest agent type costs " + formatCell(v, "float") + " times as much per run as a typical one.";
    },
    better: "Lower is better: a big gap points to one agent type worth trimming.",
    area: "agents/subagents",
  },
  config_fit: {
    sentence: function (v) {
      return formatCell(v, "int") + (v === 1 ? " setting" : " settings") + " changed during this window.";
    },
    better: "Fewer is better: frequent changes make before-and-after comparisons unreliable.",
    area: "setup/settings",
  },
  data_quality: {
    sentence: function (v) {
      return formatCell(v, "pct") + " of tokens have a known price.";
    },
    better: "Higher is better: tokens without a price count as free, so costs read low.",
    area: "data",
  },
};

// The recommendations that move each area, most direct first.
var DIMENSION_ACTIONS = {
  cache_efficiency: ["ttl-switch", "notification-invalidation", "cache-read-dominance", "env-disable-prompt-caching", "baseline-bloat"],
  context_hygiene: ["compaction-churn", "long-context-share", "agent-report-size"],
  agent_efficiency: [
    "model-tier",
    "spawn-cost",
    "spawn-claude-md",
    "spawn-shared-claude-md",
    "spawn-unused-mcp",
    "spawn-unused-skills",
    "spawn-read-only-tools",
    "spawn-task-prompt",
    "effort-mismatch",
    "subagent-volume",
  ],
  data_quality: ["data-quality", "pricing-coverage"],
};

// Level 5-4 good, 3 fair, 2 poor, 1 very poor; 0 is not measured.
function levelStatus(level) {
  if (level >= 4) return "good";
  if (level === 3) return "warn";
  if (level === 2) return "serious";
  if (level === 1) return "critical";
  return "";
}

function tableNamed(section, name) {
  return ((section && section.tables) || []).filter(function (t) {
    return t.name === name;
  })[0];
}

function renderScorecard(container, section, groups) {
  var dimTable = tableNamed(section, "dimensions");
  if (!dimTable || !dimTable.rows.length) {
    container.appendChild(emptyState("No scores for this window: it had no sessions to rate.", null, "Pick a longer window."));
    return;
  }
  var labels = dimTable.value_labels || {};
  function plain(raw) {
    return labels[raw] || String(raw).replace(/_/g, " ");
  }
  var overall = tableNamed(section, "overall");
  var overallRow = overall && overall.rows[0]; // [metric, level, label]
  if (overallRow) {
    var lowest = dimTable.rows.filter(function (row) {
      return row[0] !== "data_quality" && row[1] === overallRow[1];
    });
    var line = el("p", { class: "scorecard-overall" }, [
      el("span", { text: "Overall: " }),
      meter(overallRow[1], { label: "Overall score", status: levelStatus(overallRow[1]), text: overallRow[1] ? plain(overallRow[2]) + ", " + overallRow[1] + " of 5" : "Not measured" }),
    ]);
    if (lowest.length && overallRow[1]) {
      line.appendChild(
        el("span", {
          class: "scorecard-overall-why",
          text:
            "Set by your lowest area" +
            (lowest.length === 1 ? ", " : "s, ") +
            lowest
              .map(function (row) {
                return plain(row[0]).toLowerCase();
              })
              .join(" and ") +
            ".",
        })
      );
    }
    container.appendChild(line);
  }
  // A rule's first item on Actions: a rule for several agent types is
  // one item there, named for all of them.
  var byId = {};
  groups.forEach(function (group) {
    if (!byId[group.id]) byId[group.id] = group;
  });
  // Tagged like a report table, so evidence links from a recommendation
  // find the row (evidence.js).
  var strip = el("ul", { class: "scorecard-strip", "data-table-name": "dimensions" });
  dimTable.rows.forEach(function (row) {
    // [dimension, level, label, metric, value, threshold]
    var dimension = row[0], level = row[1], value = row[4], threshold = row[5];
    var copy = DIMENSION_TEXT[dimension];
    var measured = typeof level === "number" && level > 0;
    var item = el("li", { class: "score-item", "data-row-key": String(dimension) }, [
      el("h3", { class: "score-name", text: plain(dimension) }),
      meter(level, { label: plain(dimension), status: levelStatus(level), text: measured ? plain(row[2]) : "Not measured" }),
    ]);
    var noSnapshot = threshold === "no config snapshot available";
    item.appendChild(
      el("p", {
        class: "score-sentence",
        text: copy && typeof value === "number" && !noSnapshot ? copy.sentence(value) : plain(threshold || row[3]),
      })
    );
    if (copy) item.appendChild(el("p", { class: "score-better", text: copy.better }));
    var links = el("p", { class: "score-links" });
    if (copy) links.appendChild(pageLink(copy.area));
    var mover = (DIMENSION_ACTIONS[dimension] || [])
      .map(function (id) {
        return byId[id];
      })
      .filter(Boolean)[0];
    if (mover && level < 5) {
      links.appendChild(el("span", { class: "score-mover" }, [el("span", { text: "What moves it: " }), pageLink("actions/recommendations", groupTitle(mover), { id: mover.key })]));
    }
    if (links.childNodes.length) item.appendChild(links);
    strip.appendChild(item);
  });
  container.appendChild(strip);
}

// -- the page ------------------------------------------------------------------------------------

export function renderOverview(panel) {
  var run = ++overviewRun;
  function current() {
    return run === overviewRun;
  }
  clear(panel);
  viewIntro(panel, "overview");

  var notices = el("div", { class: "overview-notices" });
  var sentence = el("div", { class: "overview-lead", "aria-live": "polite" }, [loadingNode("Loading this window", "lines")]);
  var tilesHost = el("div", { class: "overview-tiles-host" }, [loadingNode("Loading the headline numbers", "tiles")]);
  var chartHost = el("div", { class: "overview-chart" });
  var actionsHost = el("div", { class: "panel-body" }, [loadingNode("Loading the next best actions", "rows")]);
  var actionsPanel = el("section", { class: "panel overview-actions", "aria-labelledby": "overview-actions-title" }, [
    el("header", { class: "panel-head" }, [
      el("div", { class: "panel-title-row" }, [el("h2", { class: "panel-title", id: "overview-actions-title", text: "Next best actions" })]),
      el("p", { class: "panel-intro", text: "The changes that matter most for this window, from your own sessions." }),
    ]),
    actionsHost,
  ]);
  var scoreHost = el("div", null, [loadingNode("Loading the scores", "tiles")]);
  var scoreSection = el("section", { class: "overview-scores", "aria-labelledby": "overview-scores-title" }, [
    el("h2", { id: "overview-scores-title", text: "How your setup scores" }),
    el("p", { class: "section-intro", text: "Five areas rated 1 to 5 for this window. Each links to where to look and to the change that would help most." }),
    scoreHost,
  ]);
  var details = el("details", { class: "disclosure overview-details", id: "overview-details" });
  details.appendChild(el("summary", { text: "Totals, and how amounts are counted" }));
  var detailsBody = el("div", { class: "overview-details-body" });
  details.appendChild(detailsBody);

  var main = el("div", { class: "overview-main" }, [chartHost, actionsPanel]);
  var body = el("div", { class: "overview-body" }, [
    tilesHost,
    main,
    scoreSection,
    details,
  ]);
  panel.appendChild(notices);
  panel.appendChild(sentence);
  panel.appendChild(body);

  // Every load starts at once; the drawing waits for the report, which
  // sets the billing mode every amount is written in.
  var reportLoad = loadReport();
  var healthLoad = fetchJson("/api/health");
  var summaryLoad = fetchJson(withWindow("/api/summary"));
  var previous = previousPeriod(state.window, Date.now());
  var previousLoad = previous
    ? fetchJson(withProject("/api/summary?since=" + encodeURIComponent(isoMinute(previous.since)) + "&until=" + encodeURIComponent(isoMinute(previous.until))))
    : Promise.resolve(null);
  var dailyLoad = fetchJson(withWindow("/api/daily-usage") + "&split=agent");
  var impactLoad = fetchJson("/api/impact");
  // Recommendations are built from the report, so they follow it.
  var recsLoad = reportLoad.then(function () {
    return loadRecommendations();
  });
  if (!holdChart(chartHost, "daily-spend", { slot: "overview" })) chartHost.appendChild(loadingNode("Loading daily spend", "chart"));
  // Refit once both the chart and the actions are drawn, whichever lands last.
  var actionsDrawn = false;
  var chartDrawn = false;
  // When the headline figures began to count: the chart draws in after.
  var entrance = null;
  function fitChart() {
    if (!current() || !actionsDrawn || !chartDrawn) return;
    chartHeight = fittedChartHeight(main, chartHost, actionsPanel);
    setChartHeight("daily-spend", { slot: "overview" }, chartHeight);
  }

  healthLoad.then(function (result) {
    if (!current()) return;
    var health = result.body && result.body.ok ? result.body.data : null;
    renderLogonNotice(health, notices);
  });

  var figuresDrawn = Promise.all([reportLoad, summaryLoad, previousLoad, recsLoad, healthLoad, dailyLoad]).then(function (loaded) {
    if (!current()) return;
    var reportResult = loaded[0];
    var summaryBody = loaded[1].body;
    var previousBody = loaded[2] && loaded[2].body;
    var recsBody = loaded[3].body;
    var healthBody = loaded[4].body;
    var dailyBody = loaded[5].body;
    var dailyRows = dailyBody && dailyBody.ok === true ? dailyBody.data || [] : [];
    clear(sentence);
    clear(tilesHost);
    var summaryError = summaryBody && summaryBody.ok !== true ? summaryBody.error : null;
    var projectError = !!summaryError && String(summaryError.message).indexOf("'project'") !== -1;
    if (summaryError && state.window === "change" && summaryError.code === "bad_request" && !projectError) {
      // "Since my last change" with no change recorded has nowhere to start.
      sentence.appendChild(
        el("p", { class: "overview-summary", text: "No change recorded yet, so this window has nowhere to start. Pick another window, or come back after you change a setting." })
      );
      body.hidden = true;
      return;
    }
    if (!summaryBody || summaryBody.ok !== true) {
      sentence.appendChild(errorNotice(summaryBody && summaryBody.error, function () {
        goTo("overview", { force: true });
      }));
      body.hidden = true;
      return;
    }
    var summary = summaryBody.data || {};
    var report = reportResult && reportResult.report;
    var meta = (report && report.meta) || {};
    var recs = recsBody && recsBody.ok === true ? recsBody.data || [] : [];
    // Counted as Actions lists them: one item per rule and severity,
    // whatever the number of agent types it's for.
    var groups = groupRecommendations(recs);

    if (!summary.sessions) {
      body.hidden = true;
      if (state.window === "all" && !state.project) {
        firstRun(sentence, healthBody && healthBody.ok ? healthBody.data : null);
      } else if (state.project) {
        sentence.appendChild(
          el("p", {
            class: "overview-summary",
            text: "No sessions in " + projectName(state.project) + " " + windowWhen(state.window) + ". Pick a longer window, or all projects.",
          })
        );
      } else {
        sentence.appendChild(el("p", { class: "overview-summary", text: "No sessions " + windowWhen(state.window) + ". Pick a longer window to see older ones." }));
      }
      return;
    }

    var levers = report ? savingsLevers(tablesOf(report)) : [];
    var facts = {
      summary: summary,
      previousSummary: previousBody && previousBody.ok === true ? previousBody.data : null,
      cost: summary.total_cost || 0,
      phrase: previous ? previous.phrase : null,
    };
    facts.previousCost = facts.previousSummary ? facts.previousSummary.total_cost || 0 : null;
    facts.available = availableSaving(levers, groups);
    var tiles = renderTiles(tilesHost, facts, meta, dailyRows);
    addSpendTrend(tiles.spend, dailyRows);
    countTiles(tiles.counts);
    entrance = performance.now();
    facts.saving = tiles.saving;
    facts.worth = groups.filter(function (group) {
      return group.severity === "action" || group.severity === "advice";
    }).length;
    sentence.appendChild(el("p", { class: "overview-summary", text: summarySentence(facts) }));

    if (recsBody && recsBody.ok === true) {
      renderActions(actionsHost, groups);
      enterInTurn(actionsHost.querySelectorAll(".next-action"), ACTIONS_AFTER_MS);
    } else {
      clear(actionsHost);
      actionsHost.appendChild(errorNotice(recsBody && recsBody.error));
    }
    actionsDrawn = true;
    fitChart();

    clear(scoreHost);
    clear(detailsBody);
    if (reportResult.error) {
      scoreHost.appendChild(errorNotice(reportResult.error));
      detailsBody.appendChild(errorNotice(reportResult.error));
      return;
    }
    renderScorecard(scoreHost, findSection(report, "scorecard"), groups);
    renderDetails(detailsBody, report);

  });

  // The chart waits for the headline figures, so it draws in after them
  // and at the height of the actions beside it. Figures that failed to
  // draw still let it draw; their error is thrown on its own, as it
  // would have been.
  var figuresDone = figuresDrawn.then(null, function (err) {
    setTimeout(function () {
      throw err;
    });
  });
  var chartDone = Promise.all([dailyLoad, impactLoad, reportLoad, figuresDone, summaryLoad]).then(function (loaded) {
    if (!current() || body.hidden) return;
    var daily = loaded[0].body;
    if (!daily || daily.ok !== true) {
      chartError(chartHost, "daily-spend", daily && daily.error, function () {
        goTo("overview", { force: true });
      }, { slot: "overview", titleTag: "h2" });
      return;
    }
    var summaryBody = loaded[4].body;
    renderChart(
      chartHost,
      "daily-spend",
      // Every day of the window, and the Spend tile's figure, so the
      // chart's reading says why its total differs.
      Object.assign(
        { rows: daily.data || [], split: "agent", changes: dailyChanges(loaded[1].body), sessionsTotal: summaryBody && summaryBody.ok === true ? summaryBody.data.total_cost : null },
        windowDays(state.window)
      ),
      {
        slot: "overview",
        titleTag: "h2",
        height: chartHeight,
        delay: entrance === null ? 0 : Math.max(0, entrance + CHART_AFTER_MS - performance.now()),
        // A day leads to the sessions active on it.
        open: function (day) {
          goTo("spend/sessions", { params: { day: day } });
        },
      }
    );
    chartDrawn = true;
    fitChart();
  });

  // Once the page has settled, Actions' figures load while it is idle.
  chartDone.then(function () {
    if (current()) prefetchActions();
  });
}

// -- lining the chart up with the actions ------------------------------------------------

var CHART_HEIGHT = 300;
var CHART_MAX_HEIGHT = 560;
// The height the chart last fitted to: a window change redraws at it, so
// the morph doesn't shrink the chart and grow it again.
var chartHeight = CHART_HEIGHT;

// How tall a panel's content is, whatever height the grid stretched it to.
function contentHeight(node) {
  var top = node.getBoundingClientRect().top;
  var bottom = top;
  Array.prototype.forEach.call(node.children, function (child) {
    var rect = child.getBoundingClientRect();
    if (rect.height > 0) bottom = Math.max(bottom, rect.bottom);
  });
  var style = getComputedStyle(node);
  return bottom - top + parseFloat(style.paddingBottom) + parseFloat(style.borderBottomWidth);
}

// From 1440 up the chart and the actions sit side by side. The chart
// grows to the actions' height, so neither panel ends in a blank band.
function fittedChartHeight(main, chartHost, actionsPanel) {
  var chart = chartHost.querySelector(".chart");
  var svg = chart && chart.querySelector(".chart-plot > svg");
  if (!svg || getComputedStyle(main).gridTemplateColumns.split(" ").length < 2) return CHART_HEIGHT;
  var drawn = Number(svg.getAttribute("height")) || CHART_HEIGHT;
  var target = drawn + contentHeight(actionsPanel) - contentHeight(chart);
  return Math.round(Math.max(CHART_HEIGHT, Math.min(CHART_MAX_HEIGHT, target)));
}

// The report's tables by name, across sections, for the savings levers.
function tablesOf(report) {
  var tables = {};
  (report.sections || []).forEach(function (section) {
    (section.tables || []).forEach(function (table) {
      tables[table.name] = table;
    });
  });
  return tables;
}

// Details: which billing mode the amounts follow and why, and the totals.
function renderDetails(container, report) {
  var meta = report.meta || {};
  // meta.amounts_basis says whether amounts are shares of the weekly
  // limit or list-price equivalents (older reports lack it).
  var basis =
    meta.amounts_basis ||
    (meta.billing_mode === "subscription" ? "Amounts are list-price equivalents, not what you are charged." : "Amounts are what the tokens cost at list price.");
  container.appendChild(
    el("p", {
      class: "notes",
      id: "overview-billing",
      text:
        (meta.billing_mode === "subscription" ? "Billing: Pro or Max plan" : "Billing: pay per token (API)") +
        (meta.billing_source ? " (" + meta.billing_source + "). " : ". ") +
        basis,
    })
  );
  var totals = tableNamed(findSection(report, "overview"), "totals");
  // Cost by model is on Spend, Usage (links.js's TABLE_PAGE_MAP).
  if (totals) container.appendChild(renderTable(totals, "overview-totals-table", state.currency));
  else container.appendChild(emptyState("No totals for this window: it had no sessions.", null, "Pick a longer window."));
}
