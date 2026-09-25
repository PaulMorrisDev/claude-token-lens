/* claude-token-lens service UI: page-cache.js
 *
 * The Cache page: Rebuilds and Lifetime.
 */

import { clear, el, state } from "./core.js";
import { compactNumber, fraction, moneyParts, thousands } from "./format.js";
import { findSection, loadInto, loadReport, withWindow } from "./api.js";
import { chip, errorNotice, loadingNode, tile, tileRow } from "./ui.js";
import { renderMappedSections, renderReportBackedSection } from "./grid.js";
import { pageLink, viewIntro } from "./links.js";

// ======================================================================
// Cache, Rebuilds (recache and limits sections + a quick /api/recache
// stat strip)
// ======================================================================

export function renderCache(panel) {
  clear(panel);
  viewIntro(panel, "cache/rebuilds");

  var explainer = el("div", { id: "cache-explainer" });
  panel.appendChild(explainer);

  var quickContainer = el("div", { id: "cache-quick" });
  panel.appendChild(quickContainer);
  loadInto(quickContainer, "/api/recache", renderRecacheQuickStats, { skeleton: "tiles" });

  var sectionContainer = el("div", { id: "cache-sections" });
  panel.appendChild(sectionContainer);
  sectionContainer.appendChild(loadingNode("Loading the report", "rows"));
  loadReport().then(function (result) {
    clear(sectionContainer);
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    renderCacheExplainer(result.report, explainer);
    renderMappedSections(result.report, "cache/rebuilds", sectionContainer);
  });
}

// -- what the cache does for you ----------------------------------------------------------
// Three facts with your numbers from this window: what reading from the
// cache saved, what rebuilds cost, and whether a longer lifetime would
// pay. The multipliers come from your pricing (report.meta.rates).

function reportTable(report, sectionKey, name) {
  var section = findSection(report, sectionKey);
  var tables = (section && section.tables) || [];
  for (var i = 0; i < tables.length; i++) if (tables[i].name === name) return tables[i];
  return null;
}

// A table's rows as {column key: value} objects.
function rowObjects(table) {
  if (!table) return [];
  return table.rows.map(function (row) {
    var named = {};
    table.columns.forEach(function (column, index) {
      named[column.key] = row[index];
    });
    return named;
  });
}

// The prices of the model you spent most on in this window.
function mainRates(report) {
  var rates = (report.meta && report.meta.rates) || {};
  var byModel = reportTable(report, "overview", "by_model");
  var ids = byModel
    ? byModel.rows
        .map(function (row) {
          return String(row[0]);
        })
        .filter(function (id) {
          return rates[id];
        })
    : [];
  var id = ids[0] || Object.keys(rates)[0];
  return id ? rates[id] : null;
}

function costCardLink(slug, text) {
  return pageLink("glossary/how-costs-work", text, { card: slug });
}

function renderCacheExplainer(report, container) {
  clear(container);
  var rates = mainRates(report) || {};
  var readWords = fraction(rates.cache_read_ratio);
  var writeWords = fraction(rates.cache_write_5m_ratio);
  var hourWords = fraction(rates.cache_write_1h_ratio);
  var tiles = [];

  var overall = rowObjects(reportTable(report, "ttl", "ttl_cache_economy")).filter(function (row) {
    return row.agent_type === "overall";
  })[0];
  if (overall) {
    var saved = moneyParts(overall.net_saving_usd);
    tiles.push(
      tile({
        label: "Saved by the cache after write costs",
        value: saved.value,
        unit: saved.unit,
        basis: "estimate",
        hint: saved.secondary || null,
        note:
          (readWords ? "Reading from the cache costs " + readWords + " the normal input price. " : "") +
          "Your sessions read " +
          compactNumber(overall.tokens_read) +
          " tokens from it. This is what that saved after paying for the cache writes.",
        link: costCardLink("cache-reads", "How cache reads save you money"),
      })
    );
  }

  var rebuilds = rowObjects(reportTable(report, "recache", "recache_summary"))[0];
  if (rebuilds) {
    var lost = moneyParts(rebuilds.avoidable_cost_usd);
    // The rebuilds that cost counts: the cache expired or a change broke
    // it. One after a usage-limit pause isn't avoidable.
    var causes = rowObjects(reportTable(report, "recache", "recache_signature_split"));
    var times = causes.reduce(function (sum, row) {
      return row.signature === "limit-expiry" ? sum : sum + (Number(row.turns) || 0);
    }, 0);
    var told =
      (causes.length ? "The cache was rebuilt " + (times === 1 ? "once" : thousands(times) + " times") : "A rebuild writes the cache again") +
      (writeWords && readWords ? ": written again at " + writeWords + " the input price, where reading it would have cost " + readWords + "." : ".");
    tiles.push(
      tile({
        label: "Cost of avoidable rebuilds",
        value: lost.value,
        unit: lost.unit,
        hint: lost.secondary || null,
        note: told + " Most follow an idle gap longer than the cache lifetime. Rebuilds after a usage-limit pause aren't counted.",
        link: costCardLink("cache-rebuilds", "What causes a rebuild"),
      })
    );
  }

  var agents = rowObjects(reportTable(report, "ttl", "ttl_break_even_share"));
  if (agents.length) {
    var gain = agents.filter(function (row) {
      return Number(row.margin) > 0;
    }).length;
    tiles.push(
      tile({
        label: "A 1-hour cache lifetime",
        value: thousands(gain) + " of " + thousands(agents.length),
        unit: "agent types would gain",
        note:
          "A cache write lasts 5 minutes by default" +
          (writeWords && hourWords ? " and costs " + writeWords + " the input price. A 1-hour write costs " + hourWords + " the input price." : ".") +
          " The longer lifetime pays only when you often come back after 5 to 60 minutes.",
        link: pageLink("cache/lifetime", "See each agent type"),
      })
    );
  }

  if (!tiles.length) return;
  var block = el("section", { class: "report-section cache-explainer", "aria-labelledby": "cache-explainer-title" });
  block.appendChild(el("div", { class: "block-head" }, [el("h2", { class: "section-title", id: "cache-explainer-title", text: "What the cache does for you" })]));
  block.appendChild(tileRow(tiles, { class: "explainer-tiles" }));
  container.appendChild(block);
}

// recache.SIGNATURES, in plain words. The raw signature stays in the
// tile's title for anyone matching it against the CLI report.
var REBUILD_CAUSES = [
  { key: "full-expiry", label: "Cache expired while idle" },
  { key: "prefix-invalidated", label: "Cache invalidated by a change" },
  { key: "limit-expiry", label: "Cache expired during a usage-limit pause" },
];

function renderRecacheQuickStats(data, container) {
  var bySignature = data.by_signature || {};
  var tiles = REBUILD_CAUSES.map(function (cause) {
    var entry = bySignature[cause.key] || { turns: 0, cache_creation_tokens: 0 };
    var node = tile({
      label: cause.label,
      value: thousands(entry.turns || 0),
      unit: entry.turns === 1 ? "rebuild" : "rebuilds",
      hint: compactNumber(entry.cache_creation_tokens || 0) + " tokens written to the cache again",
    });
    node.title = cause.key + ": " + thousands(entry.cache_creation_tokens || 0) + " tokens";
    return node;
  });
  var block = el("section", { class: "report-section cache-causes" });
  block.appendChild(
    el("div", { class: "block-head" }, [
      el("h2", { class: "section-title", text: "Cache rebuilds by cause" }),
      chip(state.project ? "All time, all projects" : "All time", { icon: "clock", class: "all-time-chip" }),
    ])
  );
  block.appendChild(tileRow(tiles));
  container.appendChild(block);
}

// ======================================================================
// Cache, Lifetime -- /api/ttl is itself Section/Table-shaped (docs/api.md), so
// it is rendered directly with the same generic table renderer used
// for report.json sections, rather than waiting on the full report.
// ======================================================================

export function renderTtl(panel) {
  clear(panel);
  viewIntro(panel, "cache/lifetime");
  var container = el("div", { id: "ttl-section" });
  panel.appendChild(container);
  loadInto(
    container,
    withWindow("/api/ttl"),
    function (data, target) {
      renderTtlData(data, target);
    },
    { skeleton: "rows" }
  );
}

function renderTtlData(data, container) {
  renderReportBackedSection(
    data,
    container,
    "ttl",
    "No cache lifetime figures for this window: none of its sessions went idle long enough to compare a 5-minute and a 1-hour lifetime.",
    "Pick a longer window to include more sessions."
  );
}
