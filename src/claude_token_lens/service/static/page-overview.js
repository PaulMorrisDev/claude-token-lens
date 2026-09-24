/* claude-token-lens service UI: page-overview.js
 *
 * The Overview tab: summary, scorecard and where to start.
 */

import { clear, el, showTab, state } from "./core.js";
import { formatCell, thousands } from "./format.js";
import { fetchJson, findSection, loadInto, loadReport, withWindow } from "./api.js";
import { errorNotice, loadingNode } from "./ui.js";
import { renderTable } from "./grid.js";
import { tabHeading } from "./links.js";
import { renderHealth } from "./shell.js";
import { SEVERITY_LABELS, SEVERITY_ORDER } from "./page-actions.js";

var LEVEL_LABELS = { 5: "excellent", 4: "good", 3: "fair", 2: "poor", 1: "very poor" };

// Each scorecard area as a sentence about its number, plus which way is
// better and why (scorecard.py's metrics; helptext.py's "dimensions").
var DIMENSION_TEXT = {
  cache_efficiency: {
    sentence: function (v) {
      return formatCell(v, "pct") + " of cache writes rebuilt context that had expired or changed.";
    },
    better: "Lower is better: a rebuild pays again for context you already had. Usage-limit pauses are left out.",
  },
  context_hygiene: {
    sentence: function (v) {
      return "9 in 10 main session replies carried less than " + formatCell(v, "tokens") + " tokens of context.";
    },
    better: "Lower is better: every reply pays to re-read its whole context.",
  },
  agent_efficiency: {
    sentence: function (v) {
      return "Your costliest agent type costs " + formatCell(v, "str") + " times as much per run as a typical one.";
    },
    better: "Lower is better: a big gap points to one agent type worth trimming.",
  },
  config_fit: {
    sentence: function (v) {
      return formatCell(v, "int") + (v === 1 ? " setting" : " settings") + " changed during this window.";
    },
    better: "Fewer is better: frequent changes make before-and-after comparisons unreliable.",
  },
  data_quality: {
    sentence: function (v) {
      return formatCell(v, "pct") + " of tokens have a known price.";
    },
    better: "Higher is better: tokens without a price count as free, so costs read low.",
  },
};

// "<= 5.0%" -> "at most 5.0%", "> 3.00x" -> "over 3.00 times".
function boundInWords(bound) {
  return String(bound)
    .replace(/^<=\s*/, "at most ")
    .replace(/^>=\s*/, "at least ")
    .replace(/^>\s*/, "over ")
    .replace(/^<\s*/, "under ")
    .replace(/(\d)x$/, "$1 times");
}

function renderScorecardTiles(container, scorecardSection) {
  var tiles = el("div", { class: "tiles" });
  if (!scorecardSection) {
    container.appendChild(el("p", { class: "notice", text: "No scorecard data for this window." }));
    return;
  }
  var dimTable = (scorecardSection.tables || []).filter(function (t) {
    return t.name === "dimensions";
  })[0];
  var overallTable = (scorecardSection.tables || []).filter(function (t) {
    return t.name === "overall";
  })[0];

  var labels = (dimTable && dimTable.value_labels) || {};
  function plain(raw) {
    return labels[raw] || String(raw).replace(/_/g, " ");
  }
  var scored = [];
  (dimTable ? dimTable.rows : []).forEach(function (row) {
    // [dimension, level, label, metric, value, threshold]
    var dimension = row[0], level = row[1], label = row[2], metric = row[3], value = row[4], threshold = row[5];
    var tile = el("div", { class: "tile" });
    tile.appendChild(el("div", { class: "tile-dimension", text: plain(dimension) }));
    tile.appendChild(
      el("div", { class: "tile-level level-" + level }, [
        document.createTextNode(String(level)),
        el("span", { class: "tile-max", text: " / 5" }),
      ])
    );
    tile.appendChild(el("div", { class: "tile-label", text: plain(label) }));
    var copy = DIMENSION_TEXT[dimension];
    var sentence = copy && typeof value === "number" && threshold !== "no config snapshot available" ? copy.sentence(value) : plain(threshold || metric);
    tile.appendChild(el("div", { class: "tile-metric", text: sentence }));
    if (copy) tile.appendChild(el("div", { class: "tile-threshold", text: copy.better }));
    if (threshold && threshold !== "no config snapshot available") {
      tile.appendChild(
        el("div", { class: "tile-threshold", text: (level === 1 ? "Rated 1 because it is " : "Needed for this rating: ") + boundInWords(threshold) })
      );
    }
    scored.push({ dimension: dimension, level: level });
    tiles.appendChild(tile);
  });

  if (overallTable && overallTable.rows.length) {
    var overallRow = overallTable.rows[0]; // [metric, level, label]
    var overallLevel = overallRow[1];
    var overallTile = el("div", { class: "tile tile-overall" });
    overallTile.appendChild(el("div", { class: "tile-dimension", text: "Overall" }));
    overallTile.appendChild(
      el("div", { class: "tile-level level-" + overallLevel }, [
        document.createTextNode(String(overallLevel)),
        el("span", { class: "tile-max", text: " / 5" }),
      ])
    );
    overallTile.appendChild(el("div", { class: "tile-label", text: plain(overallRow[2] || LEVEL_LABELS[overallLevel] || "unmeasured") }));
    // Overall is the lowest rated area, data quality aside (scorecard.py).
    var lowest = scored.filter(function (d) {
      return d.dimension !== "data_quality" && d.level === overallLevel;
    });
    if (lowest.length) {
      overallTile.appendChild(
        el("div", {
          class: "tile-metric",
          text: "Your lowest area: " + lowest.map(function (d) { return plain(d.dimension); }).join(", ") + ". Start there.",
        })
      );
    }
    tiles.appendChild(overallTile);
  }

  container.appendChild(tiles);
  if (dimTable && dimTable.notes && dimTable.notes.length) {
    container.appendChild(
      el(
        "ul",
        { class: "notes" },
        dimTable.notes.map(function (note) {
          return el("li", { text: note });
        })
      )
    );
  }
}

function severityRank(severity) {
  var rank = SEVERITY_ORDER.indexOf(severity);
  return rank === -1 ? SEVERITY_ORDER.length : rank;
}

function renderStartHereRecommendations(container) {
  clear(container);
  container.appendChild(loadingNode());
  fetchJson(withWindow("/api/recommendations")).then(function (result) {
    clear(container);
    var body = result.body;
    if (!body || body.ok !== true) {
      container.appendChild(errorNotice(body && body.error));
      return;
    }
    var all = body.data || [];
    if (!all.length) {
      container.appendChild(el("p", { class: "notes", text: "Nothing stands out: there are no recommendations for this window." }));
      return;
    }
    var top = all
      .map(function (rec, i) {
        return { rec: rec, i: i };
      })
      .sort(function (a, b) {
        return severityRank(a.rec.severity) - severityRank(b.rec.severity) || a.i - b.i;
      })
      .slice(0, 3);
    container.appendChild(el("p", { class: "notes", text: "The most important changes for this window, from your own sessions." }));
    var list = el("ol", { class: "start-list" });
    top.forEach(function (item) {
      var rec = item.rec;
      var li = el("li", null, [
        el("span", { class: "severity-badge severity-" + rec.severity, text: SEVERITY_LABELS[rec.severity] || rec.severity }),
        el("strong", { text: rec.title }),
      ]);
      if (rec.why) li.appendChild(el("p", { class: "start-why", text: rec.why }));
      if (rec.estimated_saving) li.appendChild(el("p", { class: "rec-saving", text: "Estimated saving: " + rec.estimated_saving }));
      list.appendChild(li);
    });
    container.appendChild(list);
    var more = el("button", {
      type: "button",
      class: "link-button",
      text: "See every recommendation, with what to change and how",
    });
    more.addEventListener("click", function () {
      showTab("recommendations", { focus: true });
    });
    container.appendChild(el("p", null, [more]));
    var quick = el("button", {
      type: "button",
      class: "link-button",
      text: "Or check one thing at a time in Quick actions",
    });
    quick.addEventListener("click", function () {
      showTab("quick", { focus: true });
    });
    container.appendChild(el("p", null, [quick]));
  });
}

function renderStartHereWeakAreas(container, scorecardSection) {
  clear(container);
  var dimTable = scorecardSection
    ? (scorecardSection.tables || []).filter(function (t) {
        return t.name === "dimensions";
      })[0]
    : null;
  if (!dimTable) return;
  var labels = dimTable.value_labels || {};
  // [dimension, level, label, metric, value, threshold]; level 0 is unmeasured.
  var weak = dimTable.rows.filter(function (row) {
    return typeof row[1] === "number" && row[1] >= 1 && row[1] <= 2;
  });
  if (!weak.length) return;
  container.appendChild(el("p", { class: "start-weak-title", text: "Scorecard areas rated poor or worse" }));
  container.appendChild(
    el(
      "ul",
      { class: "notes" },
      weak.map(function (row) {
        var copy = DIMENSION_TEXT[row[0]];
        var name = labels[row[0]] || String(row[0]).replace(/_/g, " ");
        var detail = copy && typeof row[4] === "number" ? " " + copy.sentence(row[4]) + " " + copy.better : "";
        return el("li", { text: name + " (" + (LEVEL_LABELS[row[1]] || row[2]) + ")." + detail });
      })
    )
  );
}

function renderSummaryCards(summary, container) {
  var cards = el("div", { class: "stat-cards" });
  // [label, value, what it counts]
  var items = [
    ["Sessions", thousands(summary.sessions || 0), "Conversations you started."],
    ["Transcripts", thousands(summary.transcripts || 0), "One per session and one per subagent run."],
    ["Cost", formatCell(summary.total_cost, "money", state.currency), "At list price for the tokens used."],
    ["Tokens", formatCell(summary.total_tokens, "tokens"), "Every token, including cheap cache reads."],
  ];
  items.forEach(function (item) {
    cards.appendChild(
      el("div", { class: "stat-card" }, [
        el("div", { class: "stat-label", text: item[0] }),
        el("div", { class: "stat-value", text: item[1] }),
        el("div", { class: "stat-hint", text: item[2] }),
      ])
    );
  });
  container.appendChild(cards);
}

function renderOverviewSummary(container) {
  return loadInto(container, withWindow("/api/summary"), renderSummaryCards);
}

export function renderOverview(panel) {
  clear(panel);
  tabHeading(panel, "overview");

  // Which billing mode the amounts follow, and why (config.toml's
  // billing, or the automatic choice from usage-limit readings).
  var billingLine = el("p", { class: "notes", id: "overview-billing" });
  panel.appendChild(billingLine);

  // "Start here": the three most important recommendations, then any
  // scorecard area rated poor or worse (filled in with the report).
  var startHere = el("section", { class: "start-here", id: "overview-start-here" });
  startHere.appendChild(el("h3", { text: "Start here" }));
  var startRecs = el("div", null, [loadingNode()]);
  var startWeak = el("div");
  startHere.appendChild(startRecs);
  startHere.appendChild(startWeak);
  panel.appendChild(startHere);

  var summaryContainer = el("div", { id: "overview-summary" });
  panel.appendChild(summaryContainer);
  renderOverviewSummary(summaryContainer);

  var scorecardContainer = el("div", { id: "overview-scorecard" });
  panel.appendChild(el("h3", { text: "Scorecard" }));
  panel.appendChild(scorecardContainer);
  scorecardContainer.appendChild(loadingNode());

  var totalsContainer = el("div", { id: "overview-totals" });
  panel.appendChild(totalsContainer);
  totalsContainer.appendChild(loadingNode());

  function renderOverviewReportSections() {
    clear(scorecardContainer);
    clear(totalsContainer);
    scorecardContainer.appendChild(loadingNode());
    totalsContainer.appendChild(loadingNode());
    loadReport().then(function (result) {
      clear(scorecardContainer);
      clear(totalsContainer);
      if (result.error) {
        scorecardContainer.appendChild(errorNotice(result.error));
        totalsContainer.appendChild(errorNotice(result.error));
        return;
      }
      var report = result.report;
      var meta = report.meta || {};
      // meta.amounts_basis says whether amounts are shares of the
      // weekly limit or list-price equivalents (older reports lack it).
      var basis = meta.amounts_basis ||
        (meta.billing_mode === "subscription"
          ? "Amounts are list-price equivalents, not what you are charged."
          : "Amounts are what the tokens cost at list price.");
      billingLine.textContent =
        (meta.billing_mode === "subscription" ? "Billing: Pro or Max plan" : "Billing: pay per token (API)") +
        (meta.billing_source ? " (" + meta.billing_source + "). " : ". ") +
        basis;
      renderScorecardTiles(scorecardContainer, findSection(report, "scorecard"));
      renderStartHereWeakAreas(startWeak, findSection(report, "scorecard"));
      // After the report, which the recommendations are built from.
      renderStartHereRecommendations(startRecs);
      var overviewSection = findSection(report, "overview");
      if (overviewSection) {
        var totalsTable = (overviewSection.tables || []).filter(function (t) {
          return t.name === "totals";
        })[0];
        if (totalsTable) totalsContainer.appendChild(renderTable(totalsTable, "overview-totals-table", state.currency));
        var byModel = (overviewSection.tables || []).filter(function (t) {
          return t.name === "by_model";
        })[0];
        if (byModel) totalsContainer.appendChild(renderTable(byModel, "overview-by-model-table", state.currency));
      } else {
        totalsContainer.appendChild(el("p", { class: "notice", text: "No overview section in this report." }));
      }
    });
  }

  renderOverviewReportSections();

  var healthContainer = el("div", { id: "overview-health" });
  panel.appendChild(el("h3", { text: "Service health" }));
  panel.appendChild(healthContainer);
  loadInto(healthContainer, "/api/health", renderHealth);
}
