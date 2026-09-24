/* claude-token-lens service UI: page-cache.js
 *
 * The Cache page: Rebuilds and Lifetime.
 */

import { clear, el } from "./core.js";
import { compactNumber, thousands } from "./format.js";
import { loadInto, loadReport, withWindow } from "./api.js";
import { chip, errorNotice, loadingNode, tile, tileRow } from "./ui.js";
import { renderMappedSections, renderReportBackedSection } from "./grid.js";
import { viewIntro } from "./links.js";

// ======================================================================
// Cache, Rebuilds (recache and limits sections + a quick /api/recache
// stat strip)
// ======================================================================

export function renderCache(panel) {
  clear(panel);
  viewIntro(panel, "cache/rebuilds");

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
    renderMappedSections(result.report, "cache/rebuilds", sectionContainer);
  });
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
    el("div", { class: "block-head" }, [el("h2", { class: "section-title", text: "Cache rebuilds by cause" }), chip("All time", { icon: "clock", class: "all-time-chip" })])
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
