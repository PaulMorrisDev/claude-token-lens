/* claude-token-lens service UI: grid.js
 *
 * Report tables: sortable data tables, their help, and the sections
 * each tab draws from report.json.
 */

import { clear, el, state, storageSet } from "./core.js";
import { cellSortValue, formatCell, NUMERIC_KINDS } from "./format.js";
import { findSection } from "./api.js";
import { SECTION_TAB_MAP } from "./links.js";

// Mirrors render/tables.py::resolve_evidence_column_kind /
// format_evidence_value: a Recommendation.evidence tuple carries no
// column reference of its own, so the cited (source_table, row_key,
// value) is looked up in the already-fetched report model to find
// which column actually holds it, and formatted with that column's
// kind -- the same number the report's own tables show, not a raw
// float.
function cellMatches(cell, value) {
  if (typeof cell === "boolean" || typeof value === "boolean") return cell === value;
  var cellNum = Number(cell);
  var valueNum = Number(value);
  if (!isNaN(cellNum) && !isNaN(valueNum) && cell !== null && cell !== "" && value !== null && value !== "") {
    return Math.abs(cellNum - valueNum) < 1e-9;
  }
  return cell === value || String(cell) === String(value);
}

function resolveEvidenceColumnKind(report, sourceTable, rowKey, value) {
  if (!report || typeof sourceTable !== "string") return "str";
  var dot = sourceTable.indexOf(".");
  if (dot === -1) return "str";
  var sectionKey = sourceTable.slice(0, dot);
  var tableName = sourceTable.slice(dot + 1);
  var section = findSection(report, sectionKey);
  if (!section) return "str";
  for (var t = 0; t < section.tables.length; t++) {
    var table = section.tables[t];
    if (table.name !== tableName) continue;
    for (var r = 0; r < table.rows.length; r++) {
      var row = table.rows[r];
      if (!row.length || !cellMatches(row[0], rowKey)) continue;
      for (var c = 0; c < row.length && c < table.columns.length; c++) {
        if (cellMatches(row[c], value)) return table.columns[c].kind;
      }
    }
  }
  return "str";
}

export function formatEvidenceValue(report, value, sourceTable, rowKey, currency) {
  var kind = resolveEvidenceColumnKind(report, sourceTable, rowKey, value);
  return formatCell(value, kind, currency);
}

// -- generic Section/Table renderer (Cache/TTL/Agents/Config/Usage/
//    Diagnostics tabs, per docs/ui.md and this work package's brief) -

var sortState = {}; // table id -> {index, ascending}

function tableMaxima(table) {
  var maxima = table.columns.map(function () {
    return 0;
  });
  table.rows.forEach(function (row) {
    row.forEach(function (value, i) {
      var kind = table.columns[i] && table.columns[i].kind;
      if ((kind === "tokens" || kind === "money") && typeof value === "number") {
        maxima[i] = Math.max(maxima[i], value);
      }
    });
  });
  return maxima;
}

// "How to read this": the three-part help (model.Help) a section or
// table carries -- what it shows, how to read it, when to act.
export function helpBlock(help) {
  if (!help || !(help.shows || help.read || help.act)) return null;
  var details = el("details", { class: "how-to-read" });
  details.appendChild(el("summary", { text: "How to read this" }));
  var list = el("dl");
  [
    ["What it shows", help.shows],
    ["How to read it", help.read],
    ["When to act", help.act],
  ].forEach(function (pair) {
    if (!pair[1]) return;
    list.appendChild(el("dt", { text: pair[0] }));
    list.appendChild(el("dd", { text: pair[1] }));
  });
  details.appendChild(list);
  return details;
}

export function renderTable(table, tableId, currency) {
  var maxima = tableMaxima(table);
  var wrap = el("div", { class: "table-wrap" });
  wrap.appendChild(el("h4", { text: table.title || table.name }));
  var tableHelp = helpBlock(table.help);
  if (tableHelp) wrap.appendChild(tableHelp);

  // Column help: a real button per header (keyboard and touch, never a
  // title= tooltip alone) that shows one column's help at a time in
  // the line above the table.
  var colHelpId = tableId + "-colhelp";
  var colHelp = el("p", { class: "col-help", id: colHelpId, role: "status", hidden: true });
  var helpButtons = [];
  function showColumnHelp(index, button) {
    var open = button.getAttribute("aria-expanded") === "true";
    helpButtons.forEach(function (b) {
      b.setAttribute("aria-expanded", "false");
    });
    if (open) {
      colHelp.hidden = true;
      return;
    }
    var column = table.columns[index];
    colHelp.textContent = (column.label || column.key) + ": " + column.help;
    colHelp.hidden = false;
    button.setAttribute("aria-expanded", "true");
  }

  var tableEl = el("table", { id: tableId });
  var thead = el("thead");
  var headRow = el("tr");
  table.columns.forEach(function (column, index) {
    var isNumeric = !!NUMERIC_KINDS[column.kind];
    var th = el("th", {
      class: isNumeric ? "num" : null,
      "data-index": String(index),
      tabIndex: 0,
      "aria-sort": "none",
    });
    th.appendChild(document.createTextNode(column.label || column.key));
    if (column.help) {
      var helpBtn = el("button", {
        type: "button",
        class: "col-help-btn",
        text: "?",
        "aria-label": "What is " + (column.label || column.key) + "?",
        "aria-expanded": "false",
        "aria-controls": colHelpId,
      });
      helpBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        showColumnHelp(index, helpBtn);
      });
      // Enter/Space on the button must not also sort the column.
      helpBtn.addEventListener("keydown", function (event) {
        event.stopPropagation();
      });
      helpButtons.push(helpBtn);
      th.appendChild(helpBtn);
    }
    th.appendChild(el("span", { class: "sort-indicator", text: "" }));
    th.addEventListener("click", function () {
      sortTable(table, tableEl, tableId, index, currency, maxima);
    });
    th.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        sortTable(table, tableEl, tableId, index, currency, maxima);
      }
    });
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  tableEl.appendChild(thead);

  var tbody = el("tbody");
  tableEl.appendChild(tbody);
  renderTableBody(table.rows, table.columns, tbody, currency, maxima, table.value_labels, table.row_groups, table.row_kinds);
  if (helpButtons.length) wrap.appendChild(colHelp);
  wrap.appendChild(tableEl);

  if (table.notes && table.notes.length) {
    wrap.appendChild(
      el(
        "ul",
        { class: "notes" },
        table.notes.map(function (note) {
          return el("li", { text: note });
        })
      )
    );
  }
  return wrap;
}

// valueLabels: the table's display names for raw row values (e.g.
// "top-level" -> "Main session"). The raw value stays in the cell's
// title and data-raw, so the key the CLI/JSON/CSV use is one hover or
// one inspect away.
// rowGroups (Table.row_groups): a heading row wherever the group
// changes. Only in the table's own order; a sorted table drops them.
function renderTableBody(rows, columns, tbody, currency, maxima, valueLabels, rowGroups, rowKinds) {
  clear(tbody);
  var group = null;
  rows.forEach(function (row) {
    var rowGroup = rowGroups && typeof row[0] === "string" ? rowGroups[row[0]] : null;
    if (rowGroup && rowGroup !== group) {
      group = rowGroup;
      tbody.appendChild(el("tr", { class: "row-group" }, [el("th", { scope: "colgroup", colSpan: columns.length, text: group })]));
    }
    var tr = el("tr");
    // Table.row_kinds: how to format this row's "str" cells.
    var rowKind = rowKinds && typeof row[0] === "string" ? rowKinds[row[0]] : null;
    row.forEach(function (value, i) {
      var column = columns[i] || { kind: "str" };
      if (rowKind && i > 0 && column.kind === "str" && typeof value === "number") column = { kind: rowKind };
      var isNumeric = !!NUMERIC_KINDS[column.kind];
      var td = el("td", { class: isNumeric ? "num" : null, "data-sort": cellSortValue(value) });
      var display = formatCell(value, column.kind, currency);
      if ((column.kind === "tokens" || column.kind === "money") && typeof value === "number" && maxima[i] > 0) {
        // Inline SVG bar (docs/ui.md: "the TTL/recache bars" are
        // hand-built <svg> markup, not a CSS-only bar) -- built as a
        // markup string for the same HTML5-foreign-content reason
        // buildSessionTimeline uses, so no namespace literal is
        // needed. `value`/`maxima[i]` are already-parsed numbers from
        // the report JSON, never interpolated as text.
        var barPct = Math.max(0, Math.min(100, (value / maxima[i]) * 100));
        var barWidth = (barPct * 0.6).toFixed(1); // viewBox is 0..60
        var barSvg =
          '<svg viewBox="0 0 60 10" class="bar-svg" aria-hidden="true">' +
          '<rect x="0" y="2" width="' + barWidth + '" height="6" rx="1" fill="var(--chart-1)"></rect>' +
          "</svg>";
        var cellWrap = el("span", { class: "bar-cell", html: barSvg });
        cellWrap.appendChild(el("span", { text: display }));
        td.appendChild(cellWrap);
      } else if (valueLabels && typeof value === "string" && Object.prototype.hasOwnProperty.call(valueLabels, value)) {
        td.appendChild(el("span", { class: "value-label", title: value, "data-raw": value, text: valueLabels[value] }));
      } else {
        td.textContent = display;
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function sortTable(table, tableEl, tableId, index, currency, maxima) {
  var previous = sortState[tableId];
  var ascending = !(previous && previous.index === index && previous.ascending);
  sortState[tableId] = { index: index, ascending: ascending };
  storageSet("tls:sort:" + tableId, JSON.stringify(sortState[tableId]));

  var headCells = tableEl.querySelectorAll("th");
  headCells.forEach(function (th, i) {
    th.setAttribute("aria-sort", i === index ? (ascending ? "ascending" : "descending") : "none");
    var indicator = th.querySelector(".sort-indicator");
    if (indicator) indicator.textContent = i === index ? (ascending ? "^" : "v") : "";
  });

  var rows = table.rows.slice();
  rows.sort(function (a, b) {
    var av = a[index];
    var bv = b[index];
    var an = typeof av === "number" ? av : parseFloat(av);
    var bn = typeof bv === "number" ? bv : parseFloat(bv);
    var cmp;
    if (!isNaN(an) && !isNaN(bn) && av !== null && bv !== null) {
      cmp = an - bn;
    } else {
      cmp = String(av === null || av === undefined ? "" : av).localeCompare(String(bv === null || bv === undefined ? "" : bv));
    }
    return ascending ? cmp : -cmp;
  });
  renderTableBody(rows, table.columns, tableEl.querySelector("tbody"), currency, maxima, table.value_labels, null, table.row_kinds);
}

// Tables by dashboard placement (helptext.py's table audit): "keep"
// shown, "advanced" collapsed into one block, "report" left to the CLI
// report (and the JSON/CSV exports), with a line saying so.
export function renderPlacedTables(container, tables, currency, idPrefix) {
  var advanced = [];
  var reportOnly = 0;
  tables.forEach(function (table, i) {
    var tableId = idPrefix + "-" + table.name + "-" + i;
    var placement = table.dashboard || "keep";
    if (placement === "report") {
      reportOnly += 1;
    } else if (placement === "advanced") {
      advanced.push({ table: table, id: tableId });
    } else {
      container.appendChild(renderTable(table, tableId, currency));
    }
  });
  if (advanced.length) {
    var details = el("details", { class: "advanced-detail" });
    details.appendChild(el("summary", { text: "Advanced detail (" + advanced.length + ")" }));
    advanced.forEach(function (item) {
      details.appendChild(renderTable(item.table, item.id, currency));
    });
    container.appendChild(details);
  }
  if (reportOnly) {
    container.appendChild(
      el("p", {
        class: "notes",
        text:
          (reportOnly === 1 ? "1 more table is" : reportOnly + " more tables are") +
          " in the full report (claude-token-lens report).",
      })
    );
  }
}

export function renderSectionGeneric(container, section, currency, idPrefix) {
  if (!section) return;
  // Sections are h3: each tab has exactly one h2, its own title.
  container.appendChild(el("h3", { class: "section-title", text: section.title || section.key }));
  if (section.intro) container.appendChild(el("p", { class: "section-intro", text: section.intro }));
  var sectionHelp = helpBlock(section.help);
  if (sectionHelp) container.appendChild(sectionHelp);
  renderPlacedTables(container, section.tables || [], currency, idPrefix || section.key);
  if (section.notes && section.notes.length) {
    container.appendChild(
      el(
        "ul",
        { class: "notes" },
        section.notes.map(function (note) {
          return el("li", { text: note });
        })
      )
    );
  }
}

// skip: section keys the tab renders some other way.
export function renderMappedSections(report, tabKey, container, skip) {
  if (!report || !Array.isArray(report.sections)) return;
  report.sections.forEach(function (section) {
    if (section.key === "overview") return; // handled by the Overview tab directly
    if (skip && skip.indexOf(section.key) !== -1) return;
    var target = SECTION_TAB_MAP[section.key] || "diagnostics";
    if (target !== tabKey) return;
    renderSectionGeneric(container, section, state.currency, "report");
  });
}

// Shared by every report-backed route that returns a single Section
// directly (ttl, and the v4-wiring-round carry/compaction-sim/
// model-swap/waste routes in page-cache.js and page-spend.js) rather than the full report.json.
// Defensive: docs/api.md pins this to "the same shape as the CLI's
// ... section tables" but not byte-exactly to Section (a bare
// `{tables: [...]}` or a list of Table dicts are both plausible),
// and the route returns `null` outright when the section is absent
// from the assembled report -- handle each shape rather than
// assuming one and rendering nothing on a mismatch.
export function renderReportBackedSection(data, container, idPrefix, emptyNotice) {
  if (data && Array.isArray(data.tables)) {
    renderSectionGeneric(container, data, state.currency, idPrefix);
  } else if (Array.isArray(data)) {
    data.forEach(function (table, i) {
      container.appendChild(renderTable(table, idPrefix + "-" + i, state.currency));
    });
  } else {
    container.appendChild(el("p", { class: "notice", text: emptyNotice }));
  }
}

export function simpleTable(columns, rows, caption) {
  var wrap = el("div", { class: "table-wrap" });
  if (caption) wrap.appendChild(el("h4", { text: caption }));
  var table = el("table", { class: "data-table" });
  var headRow = el("tr");
  columns.forEach(function (col) {
    headRow.appendChild(el("th", { scope: "col", text: col.label }));
  });
  table.appendChild(el("thead", null, [headRow]));
  var tbody = el("tbody");
  rows.forEach(function (row) {
    var tr = el("tr");
    row.forEach(function (cell) {
      tr.appendChild(el("td", { text: cell === null || cell === undefined ? "" : String(cell) }));
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}
