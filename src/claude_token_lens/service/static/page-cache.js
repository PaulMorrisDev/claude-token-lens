/* claude-token-lens service UI: page-cache.js
 *
 * The Cache page: Rebuilds and Lifetime.
 */

import { clear, el } from "./core.js";
import { formatCell, thousands } from "./format.js";
import { loadInto, loadReport, withWindow } from "./api.js";
import { errorNotice, loadingNode } from "./ui.js";
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
  loadInto(quickContainer, "/api/recache", renderRecacheQuickStats);

  var sectionContainer = el("div", { id: "cache-sections" });
  panel.appendChild(sectionContainer);
  sectionContainer.appendChild(loadingNode());
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
// card's title for anyone matching it against the CLI report.
var REBUILD_CAUSES = [
  { key: "full-expiry", label: "Cache expired while idle" },
  { key: "prefix-invalidated", label: "Cache invalidated by a change" },
  { key: "limit-expiry", label: "Cache expired during a usage-limit pause" },
];

function renderRecacheQuickStats(data, container) {
  var bySignature = data.by_signature || {};
  var cards = el("div", { class: "stat-cards" });
  REBUILD_CAUSES.forEach(function (cause) {
    var entry = bySignature[cause.key] || { turns: 0, cache_creation_tokens: 0 };
    cards.appendChild(
      el("div", { class: "stat-card", title: cause.key }, [
        el("div", { class: "stat-label", text: cause.label }),
        el("div", { class: "stat-value", text: thousands(entry.turns || 0) + " rebuilds" }),
        el("div", { class: "notes", text: formatCell(entry.cache_creation_tokens, "tokens") + " tokens written to the cache" }),
      ])
    );
  });
  container.appendChild(el("h2", { text: "Cache rebuilds by cause (all history)" }));
  container.appendChild(cards);
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
  loadInto(container, withWindow("/api/ttl"), function (data, target) {
    renderTtlData(data, target);
  });
}

function renderTtlData(data, container) {
  renderReportBackedSection(data, container, "ttl", "No TTL simulation data for this window.");
}
