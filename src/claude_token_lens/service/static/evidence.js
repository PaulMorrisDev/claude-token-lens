/* claude-token-lens service UI: evidence.js
 *
 * The numbers behind a recommendation, one click away. Each evidence
 * entry names a report table ("section.table") and a row; a link opens
 * the page that shows that table, opens "More tables" or "Details" if
 * it sits inside one, scrolls to the row and pulses it. A table no page
 * shows (one left to the full report, or a section with no page) opens
 * in a drawer instead, so every link lands somewhere.
 */

import { clear, el, goTo, state } from "./core.js";
import { findSection, loadReport } from "./api.js";
import { formatEvidenceValue, pulseNode, pulseRow, renderTable } from "./grid.js";
import { drawer, emptyState, loadingNode } from "./ui.js";
import { formatHash, scopeParams, SECTION_PAGE_MAP, TABLE_PAGE_MAP, viewForTable, viewLabel } from "./links.js";

// "section.table" -> its two parts.
export function splitSource(sourceTable) {
  var text = String(sourceTable || "");
  var dot = text.indexOf(".");
  return dot === -1 ? { section: text, table: "" } : { section: text.slice(0, dot), table: text.slice(dot + 1) };
}

export function findTable(report, sourceTable) {
  var parts = splitSource(sourceTable);
  var section = report ? findSection(report, parts.section) : null;
  var tables = (section && section.tables) || [];
  for (var i = 0; i < tables.length; i++) {
    if (tables[i].name === parts.table) return { section: section, table: tables[i] };
  }
  return null;
}

// The view that shows a table, or null when only the drawer can: a
// table left to the full report, or one from a section no page shows.
export function evidenceView(report, sourceTable) {
  var parts = splitSource(sourceTable);
  var found = findTable(report, sourceTable);
  if (found && found.table.dashboard === "report") return null;
  if (!TABLE_PAGE_MAP[sourceTable] && !SECTION_PAGE_MAP[parts.section]) return null;
  return viewForTable(parts.section, parts.table);
}

// A table's title and a row's label as the dashboard shows them.
export function evidenceNames(report, sourceTable, rowKey) {
  var found = findTable(report, sourceTable);
  var table = found ? found.table : null;
  var tableLabel = table && table.title ? table.title : splitSource(sourceTable).table.replace(/_/g, " ");
  var rowLabel = table && table.value_labels && table.value_labels[rowKey] ? table.value_labels[rowKey] : rowKey;
  return { table: tableLabel, row: rowLabel === null || rowLabel === undefined ? "" : String(rowLabel) };
}

// Show the row behind a number: on its page, or in the drawer.
export function openEvidence(report, sourceTable, rowKey) {
  var view = evidenceView(report, sourceTable);
  if (!view) {
    tableDrawer(sourceTable, rowKey);
    return;
  }
  goTo(view, { params: { t: sourceTable, row: rowKey === null || rowKey === undefined ? null : String(rowKey) } });
}

// A recommendation's evidence as a list: one item per row it cites,
// with a link to that row and the values taken from it (formatted as
// their columns are).
export function evidenceList(report, evidence) {
  var groups = [];
  var byPlace = {};
  (evidence || []).forEach(function (tuple) {
    var place = tuple[2] + "|" + tuple[3];
    if (!byPlace[place]) {
      byPlace[place] = { sourceTable: tuple[2], rowKey: tuple[3], values: [] };
      groups.push(byPlace[place]);
    }
    byPlace[place].values.push(tuple);
  });
  var list = el("ul", { class: "evidence-list" });
  groups.forEach(function (group) {
    list.appendChild(evidenceItem(report, group));
  });
  return list;
}

function evidenceItem(report, group) {
  var sourceTable = group.sourceTable, rowKey = group.rowKey;
  var names = evidenceNames(report, sourceTable, rowKey);
  var view = evidenceView(report, sourceTable);
  var row = rowKey === null || rowKey === undefined ? null : String(rowKey);
  var link = el("a", {
    class: "evidence-link",
    href: view ? formatHash(view, Object.assign(scopeParams(), { t: sourceTable, row: row })) : "#",
    text: names.table + (names.row ? ", " + names.row : ""),
  });
  link.addEventListener("click", function (event) {
    if (view && (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
    event.preventDefault();
    openEvidence(report, sourceTable, rowKey);
  });
  var values = el("dl", { class: "evidence-values" });
  group.values.forEach(function (tuple) {
    var formatted = report ? formatEvidenceValue(report, tuple[1], sourceTable, rowKey, state.currency) : String(tuple[1]);
    values.appendChild(el("div", null, [el("dt", { text: tuple[0] }), el("dd", { text: formatted })]));
  });
  return el("li", { class: "evidence-item" }, [
    el("p", { class: "evidence-source" }, [
      link,
      el("span", { class: "evidence-place", text: view ? " on " + viewLabel(view) : " (opens in a panel)" }),
    ]),
    values,
  ]);
}

// On the page an evidence link opened: wait for the table to be drawn
// (views draw once their data arrives), open what hides it, and pulse
// the row. Given up on once the view settles without it, or leaves the
// screen; then the drawer shows the table instead. A newer reveal
// cancels an older one still waiting, so only the last link followed
// pulses or opens the drawer.
var cancelReveal = null;

export function revealEvidence(panel, sourceTable, rowKey) {
  if (cancelReveal) cancelReveal();
  cancelReveal = null;
  var tableName = splitSource(sourceTable).table;
  if (!panel || !tableName) return;
  var selector = '[data-table-name="' + CSS.escape(tableName) + '"]';
  var done = false;
  var observer = null;
  var settle = null;
  var limit = null;

  function finish() {
    done = true;
    if (observer) observer.disconnect();
    clearTimeout(settle);
    clearTimeout(limit);
    if (cancelReveal === finish) cancelReveal = null;
  }
  function attempt() {
    if (done) return true;
    if (panel.hidden) {
      finish();
      return true;
    }
    var host = panel.querySelector(selector);
    if (!host) return false;
    finish();
    for (var node = host.parentElement; node && node !== panel; node = node.parentElement) {
      if (node.tagName === "DETAILS" && !node.open) node.open = true;
    }
    // A summary table's row is its "All figures" list.
    var figures = rowKey !== null && rowKey !== undefined && rowKey !== "" ? host.querySelector("details.summary-details") : null;
    if (figures) figures.open = true;
    // After layout, so a grid that just opened has its rows.
    requestAnimationFrame(function () {
      pulseIn(host, rowKey);
    });
    return true;
  }
  function giveUp() {
    if (done) return;
    if (panel.hidden) {
      finish();
      return;
    }
    // Still loading: wait for the data.
    if (panel.querySelector('[aria-busy="true"]')) {
      arm();
      return;
    }
    finish();
    tableDrawer(sourceTable, rowKey);
  }
  function arm() {
    clearTimeout(settle);
    settle = setTimeout(giveUp, 1500);
  }
  if (attempt()) return;
  cancelReveal = finish;
  observer = new MutationObserver(function () {
    if (!attempt()) arm();
  });
  observer.observe(panel, { childList: true, subtree: true });
  arm();
  limit = setTimeout(function () {
    if (done) return;
    finish();
    if (!panel.hidden) tableDrawer(sourceTable, rowKey);
  }, 90000);
}

function pulseIn(host, rowKey) {
  var hasRow = rowKey !== null && rowKey !== undefined && rowKey !== "";
  var grid = host.querySelector("table.data-grid");
  if (grid && hasRow && pulseRow(grid, rowKey)) return;
  var item = hasRow ? host.querySelector('[data-row-key="' + CSS.escape(String(rowKey)) + '"]') : null;
  if (item && item.tagName === "TR") pulseNode(item, "row-target");
  else pulseNode(item || host, "block-target");
}

// A table in a drawer: for a table no page shows, or one the page
// didn't draw for this window. The row is pulsed there.
export function tableDrawer(sourceTable, rowKey) {
  var parts = splitSource(sourceTable);
  loadReport().then(function (loaded) {
    var report = loaded && loaded.report;
    var found = findTable(report, sourceTable);
    var title = found ? found.table.title || found.table.name : "The numbers behind this";
    drawer({
      title: title,
      wide: true,
      fill: function (body) {
        if (!found) {
          body.appendChild(
            emptyState(
              "This table has nothing for this window, so the numbers can't be shown here.",
              null,
              "Pick a longer window to include more sessions."
            )
          );
          return;
        }
        var shownOn = evidenceView(report, sourceTable);
        body.appendChild(
          el("p", {
            class: "drawer-intro",
            text:
              (found.section.title ? "From " + found.section.title + ". " : "") +
              (shownOn
                ? "It's also on " + viewLabel(shownOn) + "."
                : "No page shows this table; the full report has it too (claude-token-lens report)."),
          })
        );
        var wrap = el("div");
        body.appendChild(wrap);
        wrap.appendChild(loadingNode("Drawing the table", "rows"));
        requestAnimationFrame(function () {
          clear(wrap);
          wrap.appendChild(renderTable(found.table, "drawer-" + parts.section + "-" + parts.table, state.currency, { heading: false }));
          requestAnimationFrame(function () {
            pulseIn(wrap, rowKey);
          });
        });
      },
    });
  });
}
