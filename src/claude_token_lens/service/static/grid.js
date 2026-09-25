/* claude-token-lens service UI: grid.js
 *
 * The data grid (docs/ui.md, "Data grid") and the report sections each
 * view draws from report.json. One grid for every table on every page:
 * sticky header, sort that is kept per table (tls:sort:<table>), a
 * column chooser for wide tables (tls:cols:<table>), numbers right-
 * aligned in even-width digits, an inline bar on the lead measure, an
 * optional tint by value, readable project names, and only the visible
 * rows drawn once a table passes 200 rows. A row can link to the same
 * thing's mark in the chart above it (core.js's highlight). A table
 * that is evidence for a recommendation says so: "Feeds N actions" in
 * its header, and a mark on each row the recommendation cites.
 */

import { clear, el, highlight, listenHighlight, state, storageGet, storageSet } from "./core.js";
import { cellSortValue, formatCell, fullValue, moneyParts, moneyText, moneyUnit, NUMERIC_KINDS, PROJECT_KEYS, projectName } from "./format.js";
import { actionIndex, findSection } from "./api.js";
import { COST_CARDS, pageLink, plainText, viewForSection, viewForTable } from "./links.js";
import { button, emptyState, helpButton, motionOK, popoverButton, prose, swatch, tile, tileRow } from "./ui.js";
import { icon } from "./icons.js";

// Mirrors render/tables.py::resolve_evidence_column_kind /
// format_evidence_value: a Recommendation.evidence tuple carries no
// column reference of its own, so the cited (source_table, row_key,
// value) is looked up in the already-fetched report model to find
// which column actually holds it, and formatted with that column's
// kind -- the same number the report's own tables show, not a raw
// float. An amount reads in the billing mode (it's prose, not a grid).
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
  if (kind === "money" && typeof value === "number") return moneyText(value);
  return formatCell(value, kind, currency);
}

// -- the data grid ------------------------------------------------------

// Above this many rows only the rows in view are drawn.
var VIRTUAL_ROWS = 200;
// Above this many rows the grid scrolls in its own box, header pinned.
var TALL_ROWS = 20;
// A wide table shows this many columns until you choose more.
var LEAD_COLUMNS = 7;
// A report table longer than this shows its first REPORT_ROWS rows (in
// the order it is sorted) and a button for the rest.
var REPORT_ROWS = 10;

// Report tables listed oldest first (usage.py): capped, they show their
// latest rows.
export var NEWEST_LAST = { by_day: true, by_week: true, by_month: true, five_hour_blocks: true };

function hasKeys(object) {
  return !!object && Object.keys(object).length > 0;
}

function readJson(key) {
  var raw = storageGet(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (err) {
    return null;
  }
}

// The value a column holds for a row: row[index] for a report table's
// arrays, row[key] for an API list's objects, or the column's own
// value(row).
function columnValue(column, row) {
  if (column.value) return column.value(row);
  return Array.isArray(row) ? row[column.index] : row[column.key];
}

function isNumeric(column) {
  return !!NUMERIC_KINDS[column.kind];
}

// The inline bar goes on one column: the spec's, else the first money
// column, else the first tokens column.
function barColumn(columns, spec) {
  if (spec.bar !== undefined) return spec.bar;
  var money = -1;
  var tokens = -1;
  columns.forEach(function (column, i) {
    if (column.kind === "money" && money === -1) money = i;
    if (column.kind === "tokens" && tokens === -1) tokens = i;
  });
  return money !== -1 ? money : tokens;
}

function columnMaxima(columns, rows) {
  return columns.map(function (column) {
    var max = 0;
    rows.forEach(function (row) {
      var value = columnValue(column, row);
      if (typeof value === "number" && isFinite(value) && value > max) max = value;
    });
    return max;
  });
}

function barNode(value, max) {
  // Built as a markup string for the same HTML5-foreign-content reason
  // icons.js uses, so no namespace literal is needed. value and max are
  // numbers from the report JSON, never interpolated as text.
  var width = ((Math.max(0, Math.min(1, value / max)) * 60) || 0).toFixed(1);
  return el("span", {
    class: "bar-track",
    html: '<svg viewBox="0 0 60 8" class="bar-svg" aria-hidden="true" preserveAspectRatio="none"><rect x="0" y="1" width="' + width + '" height="6" rx="1.5"></rect></svg>',
  });
}

// The display of one cell, as text or a node.
function cellContent(column, row, value, spec, rowKind) {
  if (column.render) return column.render(row, value);
  var kind = column.kind;
  // Table.row_kinds: how to format this row's "str" cells.
  if (rowKind && column.index > 0 && kind === "str" && typeof value === "number") kind = rowKind;
  if (PROJECT_KEYS[column.key] && typeof value === "string") {
    return el("span", { class: "entity-name", title: value, text: projectName(value) });
  }
  var labels = spec.valueLabels;
  if (labels && typeof value === "string" && Object.prototype.hasOwnProperty.call(labels, value)) {
    return el("span", { class: "value-label", title: value, "data-raw": value, text: labels[value] });
  }
  // A cell can't hold a link (a row opens its own detail): a page link
  // in its text reads as the page's name.
  var text = formatCell(value, kind, state.currency);
  return typeof text === "string" ? plainText(text) : text;
}

// A text column holding sentences wraps as prose; any other text (a
// name, a model, a key) stays on one line, so "claude-haiku-4-5" never
// breaks across lines. A column drawn by its own render is left alone.
var PROSE_CHARS = 40;

function proseColumns(columns, rows) {
  var wraps = {};
  columns.forEach(function (column) {
    if (column.render || NUMERIC_KINDS[column.kind]) return;
    wraps[column.index] = rows.some(function (row) {
      var value = columnValue(column, row);
      return typeof value === "string" && value.length > PROSE_CHARS;
    });
  });
  return wraps;
}

// Report tables read as a heat grid: agent by quality signal, where the
// darker cell is the signal to look at first.
var TINT_TABLES = ["quality_by_agent"];

// A grid for spec (see docs/ui.md for the whole contract):
//   id        stable across draws: the saved sort and columns hang on it
//   columns   [{key, label, kind, help, render(row, value), value(row),
//              sortValue(row), nowrap}]; a report table's columns get
//              their index from their position
//   rows      arrays (a report table) or objects (an API list)
//   lead      column keys shown until more are chosen (else the first 7
//             of a table wider than 8)
//   bar       the index of the column with the inline bar (-1: none)
//   tint      true: percentage cells shade by value, each column against
//             its own largest (the Quality grid)
//   rowAction {label(row), run(row, tr)}: each row opens something
//   rowKey    row -> the key evidence links and pulses use
//   rowClass  row -> a class for its <tr> (e.g. "row-unchanged"), or null
//   link      {scope, key(row)}: hovering or focusing a row lights the
//             chart mark with the same key in that scope, and back
//   swatch    row -> the colour the row's entity has on the chart, shown
//             as a swatch before its first cell, or null
//   sortable  false for a form laid out as a table
//   limit     show the first limit rows and a "Show all N rows" button,
//             when there are more than limit + 2 (else all of them)
//   empty     what to say when there are no rows
//   caption   the table's name, read aloud
//   valueLabels, rowGroups, rowKinds: the report Table's own fields
export function dataGrid(spec) {
  var columns = spec.columns.map(function (column, i) {
    return Object.assign({ index: i, kind: column.kind || "str" }, column);
  });
  var rows = spec.rows || [];
  var gridId = spec.id;
  var sortable = spec.sortable !== false && rows.length > 1;
  var wrap = el("div", { class: "grid" + (spec.class ? " " + spec.class : ""), "data-grid": gridId });
  // Rows that are evidence for an action: rowMark(key) -> a node for the
  // row's first cell, or null. Set later through wrap.grid.markRows,
  // once the recommendations have loaded.
  var rowMark = spec.rowMark || null;

  if (!rows.length) {
    wrap.appendChild(emptyState(spec.empty || "Nothing to show for this window.", null, spec.emptyNext));
    return wrap;
  }

  // -- which columns show ------------------------------------------------
  var chooserOn = columns.length > LEAD_COLUMNS + 1;
  var defaultKeys = columns
    .filter(function (column, i) {
      if (!chooserOn) return true;
      if (spec.lead) return i === 0 || spec.lead.indexOf(column.key) !== -1;
      return i < LEAD_COLUMNS;
    })
    .map(function (column) {
      return column.key;
    });
  var savedKeys = chooserOn ? readJson("tls:cols:" + gridId) : null;
  var shownKeys = Array.isArray(savedKeys) && savedKeys.length ? savedKeys : defaultKeys;

  // -- sort: the saved one, read back ------------------------------------
  var sort = sortable ? readJson("tls:sort:" + gridId) : null;
  if (sort && !columns.some(function (c) { return c.key === sort.key; })) sort = null;

  var maxima = columnMaxima(columns, rows);
  var proseCols = proseColumns(columns, rows);
  var bar = barColumn(columns, spec);
  if (rows.length < 2) bar = -1;

  var toolbar = null;
  var chooserButton = null;
  if (chooserOn) {
    toolbar = el("div", { class: "grid-toolbar" });
    chooserButton = button("", { variant: "quiet", icon: "table", class: "grid-columns" });
    toolbar.appendChild(
      popoverButton(chooserButton, buildChooser, { class: "grid-chooser", label: "Columns to show", align: "end" })
    );
    wrap.appendChild(toolbar);
  }

  var scroller = el("div", { class: "grid-scroll", tabIndex: -1 });
  var table = el("table", { id: gridId, class: "data-grid" + (spec.tint ? " grid-tint" : "") });
  if (spec.caption) table.appendChild(el("caption", { class: "visually-hidden", text: spec.caption }));
  var thead = el("thead");
  var tbody = el("tbody");
  table.appendChild(thead);
  table.appendChild(tbody);
  scroller.appendChild(table);
  wrap.appendChild(scroller);

  var virtual = rows.length > VIRTUAL_ROWS;
  if (virtual) scroller.classList.add("grid-virtual");
  var orderedRows = rows;
  var rowHeight = 33;
  // A long table opens on its top rows; the rest are one click away.
  var limited = !virtual && spec.limit > 0 && rows.length > spec.limit + 2;
  var expanded = false;
  var moreButton = null;

  // A grid drawing more than TALL_ROWS rows scrolls in its own box, so
  // a capped table only does once all its rows show.
  function fitBox() {
    var tall = (limited && !expanded ? spec.limit : rows.length) > TALL_ROWS;
    scroller.classList.toggle("grid-tall", tall);
    if (tall) {
      scroller.tabIndex = 0;
      scroller.setAttribute("role", "region");
      scroller.setAttribute("aria-label", (spec.caption || "Table") + ", scrolls");
    } else if (!scroller.classList.contains("grid-wide")) {
      scroller.tabIndex = -1;
      scroller.removeAttribute("role");
      scroller.removeAttribute("aria-label");
    }
  }
  fitBox();

  function visibleColumns() {
    return columns.filter(function (column) {
      return shownKeys.indexOf(column.key) !== -1 || column.index === 0;
    });
  }

  function updateChooserLabel() {
    if (!chooserButton) return;
    var label = chooserButton.querySelector(".button-label");
    var text = "Columns (" + visibleColumns().length + " of " + columns.length + ")";
    if (label) label.textContent = text;
    else chooserButton.appendChild(el("span", { class: "button-label", text: text }));
  }

  function buildChooser(body) {
    body.appendChild(el("p", { class: "popover-title", text: "Columns to show" }));
    var list = el("div", { class: "chooser-list" });
    columns.forEach(function (column) {
      var id = gridId + "-col-" + column.index;
      var box = el("input", { type: "checkbox", id: id, checked: shownKeys.indexOf(column.key) !== -1 || column.index === 0, disabled: column.index === 0 });
      box.addEventListener("change", function () {
        if (box.checked && shownKeys.indexOf(column.key) === -1) shownKeys = shownKeys.concat([column.key]);
        if (!box.checked) shownKeys = shownKeys.filter(function (k) { return k !== column.key; });
        storageSet("tls:cols:" + gridId, JSON.stringify(shownKeys));
        drawHead();
        drawBody();
        updateChooserLabel();
      });
      list.appendChild(el("label", { class: "chooser-item", for: id }, [box, el("span", { text: column.label || column.key })]));
    });
    body.appendChild(list);
    var all = button("Show all", {
      variant: "quiet",
      action: function () {
        shownKeys = columns.map(function (c) { return c.key; });
        storageSet("tls:cols:" + gridId, JSON.stringify(shownKeys));
        list.querySelectorAll("input").forEach(function (b) { b.checked = true; });
        drawHead();
        drawBody();
        updateChooserLabel();
      },
    });
    var reset = button("Back to the usual", {
      variant: "quiet",
      action: function () {
        shownKeys = defaultKeys.slice();
        storageSet("tls:cols:" + gridId, JSON.stringify(shownKeys));
        list.querySelectorAll("input").forEach(function (b, i) { b.checked = shownKeys.indexOf(columns[i].key) !== -1 || i === 0; });
        drawHead();
        drawBody();
        updateChooserLabel();
      },
    });
    body.appendChild(el("div", { class: "chooser-actions" }, [all, reset]));
  }

  // A column of a table whose rows each carry their own kind (the
  // Overview totals: counts, tokens, money in one column) reads as
  // numeric when every value in it is a number, so its header lines up
  // with its right-aligned cells.
  function headerNumeric(column) {
    if (isNumeric(column)) return true;
    if (!spec.rowKinds || column.index === 0 || !rows.length) return false;
    return rows.every(function (row) {
      var value = columnValue(column, row);
      return value === null || value === undefined || typeof value === "number";
    });
  }

  // Column help opens from a (?) in the header, one column at a time.
  function headerCell(column) {
    var th = el("th", { scope: "col", class: (headerNumeric(column) ? "num" : "") + (column.key === "__select" ? " col-select" : ""), "data-key": column.key });
    var label = el("span", { class: "th-label", text: column.label || column.key });
    th.appendChild(label);
    if (column.kind === "money") th.appendChild(el("span", { class: "unit", text: " " + moneyUnit() }));
    if (column.help) {
      var helpBtn = button("?", { class: "col-help-btn", label: "What is " + (column.label || column.key) + "?" });
      popoverButton(
        helpBtn,
        function (body) {
          body.appendChild(el("p", { class: "popover-title", text: column.label || column.key }));
          body.appendChild(el("p", null, prose(column.help)));
        },
        { class: "help-popover", label: column.label || column.key, focusInside: false }
      );
      // A click or Enter on the (?) must not also sort the column.
      helpBtn.addEventListener("click", function (event) {
        event.stopPropagation();
      });
      helpBtn.addEventListener("keydown", function (event) {
        event.stopPropagation();
      });
      th.appendChild(helpBtn);
    }
    if (sortable) {
      th.tabIndex = 0;
      th.classList.add("sortable");
      var active = sort && sort.key === column.key;
      th.setAttribute("aria-sort", active ? (sort.ascending ? "ascending" : "descending") : "none");
      var indicator = el("span", { class: "sort-indicator" });
      if (active) indicator.appendChild(icon(sort.ascending ? "arrow-up" : "arrow-down", { size: 12 }));
      th.appendChild(indicator);
      th.addEventListener("click", function () {
        sortBy(column);
      });
      th.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sortBy(column);
        }
      });
    }
    return th;
  }

  function drawHead() {
    clear(thead);
    var tr = el("tr");
    visibleColumns().forEach(function (column) {
      tr.appendChild(headerCell(column));
    });
    thead.appendChild(tr);
  }

  function sortValue(column, row) {
    return column.sortValue ? column.sortValue(row) : columnValue(column, row);
  }

  function applySort() {
    if (!sort) {
      orderedRows = rows;
      return;
    }
    var column = columns.filter(function (c) { return c.key === sort.key; })[0];
    orderedRows = rows.slice().sort(function (a, b) {
      var av = sortValue(column, a);
      var bv = sortValue(column, b);
      var an = typeof av === "number" ? av : parseFloat(av);
      var bn = typeof bv === "number" ? bv : parseFloat(bv);
      var cmp;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else if (!isNaN(an) && !isNaN(bn) && av !== null && bv !== null && isNumeric(column)) cmp = an - bn;
      else cmp = cellSortValue(av).localeCompare(cellSortValue(bv));
      return sort.ascending ? cmp : -cmp;
    });
  }

  // First press: biggest first for a number, A to Z for text; the next
  // press turns it round.
  function sortBy(column) {
    var ascending = sort && sort.key === column.key ? !sort.ascending : !isNumeric(column);
    sort = { key: column.key, ascending: ascending };
    storageSet("tls:sort:" + gridId, JSON.stringify(sort));
    applySort();
    var focusedKey = document.activeElement && document.activeElement.getAttribute && document.activeElement.getAttribute("data-key");
    drawHead();
    drawBody();
    if (focusedKey) {
      var again = thead.querySelector('th[data-key="' + CSS.escape(focusedKey) + '"]');
      if (again) again.focus();
    }
  }

  function bodyRow(row) {
    var tr = el("tr");
    var key = spec.rowKey ? spec.rowKey(row) : Array.isArray(row) ? row[0] : null;
    if (key !== null && key !== undefined) tr.setAttribute("data-row-key", String(key));
    var rowClass = spec.rowClass ? spec.rowClass(row) : null;
    if (rowClass) tr.classList.add(rowClass);
    var rowKind = spec.rowKinds && Array.isArray(row) && typeof row[0] === "string" ? spec.rowKinds[row[0]] : null;
    if (spec.link) linkRow(tr, spec.link.key(row));
    var colour = spec.swatch ? spec.swatch(row) : null;
    visibleColumns().forEach(function (column, position) {
      var value = columnValue(column, row);
      // Same rule as the header (headerNumeric): in a table whose rows
      // carry their own kinds, a number is right-aligned even in a row
      // with no kind listed.
      var numeric = isNumeric(column) || (!!spec.rowKinds && column.index > 0 && typeof value === "number");
      var wrapClass = numeric ? "num" : proseCols[column.index] ? "cell-prose" : column.nowrap || proseCols[column.index] === false ? "nowrap" : "";
      var td = el("td", { class: wrapClass + " col-" + column.key, "data-sort": cellSortValue(value) });
      var content = cellContent(column, row, value, spec, rowKind);
      var full = fullValue(value, column.kind);
      if (full) td.title = plainText(full);
      if (column.index === bar && typeof value === "number" && value > 0 && maxima[column.index] > 0) {
        td.appendChild(
          el("span", { class: "bar-cell" }, [barNode(value, maxima[column.index]), typeof content === "string" ? el("span", { text: content }) : content])
        );
      } else if (typeof content === "string") {
        td.textContent = content;
      } else if (content) {
        td.appendChild(content);
      }
      if (colour && position === 0) td.insertBefore(swatch(colour), td.firstChild);
      if (spec.tint && column.kind === "pct" && typeof value === "number" && maxima[column.index] > 0 && column.index !== bar) {
        td.classList.add("tint-" + Math.max(1, Math.min(5, Math.ceil((value / maxima[column.index]) * 5))));
      }
      tr.appendChild(td);
    });
    if (rowMark && key !== null && key !== undefined) markRow(tr, rowMark(String(key)));
    if (spec.rowAction) {
      tr.classList.add("clickable");
      tr.tabIndex = 0;
      tr.setAttribute("aria-label", spec.rowAction.label(row));
      var run = function () {
        spec.rowAction.run(row, tr);
      };
      tr.addEventListener("click", function (event) {
        // A control inside the row keeps its own click.
        if (event.target.closest && event.target.closest("button, a, input, select, label")) return;
        run();
      });
      tr.addEventListener("keydown", function (event) {
        if (event.target !== tr) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          run();
        }
      });
    }
    return tr;
  }

  function markRow(tr, mark) {
    if (!mark || !tr.firstChild) return;
    tr.classList.add("is-evidence");
    tr.firstChild.appendChild(mark);
  }

  // A row that means the same thing as a chart mark: pointing at either
  // lights both.
  function linkRow(tr, key) {
    if (key === null || key === undefined) return;
    tr.setAttribute("data-link-key", String(key));
    var on = function () {
      highlight(spec.link.scope, key);
    };
    var off = function () {
      highlight(spec.link.scope, null);
    };
    tr.addEventListener("mouseenter", on);
    tr.addEventListener("mouseleave", off);
    tr.addEventListener("focusin", on);
    tr.addEventListener("focusout", off);
  }

  function spacer(height) {
    var tr = el("tr", { class: "grid-spacer", "aria-hidden": "true" });
    var td = el("td", { colSpan: visibleColumns().length });
    td.style.height = height + "px";
    tr.appendChild(td);
    return tr;
  }

  // Row groups (Table.row_groups): a heading row wherever the group
  // changes -- only in the table's own order; a sorted table drops them.
  function drawBody() {
    clear(tbody);
    if (virtual) {
      drawWindow();
      return;
    }
    var group = null;
    var groups = sort ? null : spec.rowGroups;
    var drawn = limited && !expanded ? (fromEnd() ? orderedRows.slice(-spec.limit) : orderedRows.slice(0, spec.limit)) : orderedRows;
    drawn.forEach(function (row) {
      var rowGroup = groups && Array.isArray(row) && typeof row[0] === "string" ? groups[row[0]] : null;
      if (rowGroup && rowGroup !== group) {
        group = rowGroup;
        tbody.appendChild(el("tr", { class: "row-group" }, [el("th", { scope: "colgroup", colSpan: visibleColumns().length, text: group })]));
      }
      tbody.appendChild(bodyRow(row));
    });
  }

  // Only the rows in view (and a margin either side), between two
  // spacers as tall as the rows left out.
  var windowStart = -1;
  function drawWindow(force) {
    var viewport = scroller.clientHeight || 560;
    var start = Math.max(0, Math.floor(scroller.scrollTop / rowHeight) - 15);
    var end = Math.min(orderedRows.length, start + Math.ceil(viewport / rowHeight) + 30);
    if (!force && start === windowStart && tbody.firstChild) return;
    windowStart = start;
    clear(tbody);
    tbody.appendChild(spacer(start * rowHeight));
    for (var i = start; i < end; i++) tbody.appendChild(bodyRow(orderedRows[i]));
    tbody.appendChild(spacer((orderedRows.length - end) * rowHeight));
  }

  function rowIndex(rowKey) {
    for (var i = 0; i < orderedRows.length; i++) {
      var row = orderedRows[i];
      var key = spec.rowKey ? spec.rowKey(row) : Array.isArray(row) ? row[0] : null;
      if (String(key) === String(rowKey)) return i;
    }
    return -1;
  }

  // A dated table (spec.limitFrom "end", oldest row first) folds to its
  // latest rows while it keeps its own order.
  function fromEnd() {
    return spec.limitFrom === "end" && !sort;
  }

  function setExpanded(open) {
    expanded = open;
    moreButton.setAttribute("aria-expanded", open ? "true" : "false");
    moreButton.querySelector(".button-label").textContent = open ? (fromEnd() ? "Show the latest " : "Show the first ") + spec.limit : "Show all " + rows.length + " rows";
    fitBox();
    drawBody();
  }

  if (limited) {
    moreButton = button("Show all " + rows.length + " rows", {
      variant: "link",
      action: function () {
        setExpanded(!expanded);
      },
    });
    moreButton.setAttribute("aria-expanded", "false");
    moreButton.setAttribute("aria-controls", gridId);
    wrap.appendChild(el("div", { class: "grid-more" }, [moreButton]));
    // An evidence link's row may be past the first rows: show them all
    // (pulseRow calls this). Returns whether the key is one of the rows.
    table.gridScrollTo = function (rowKey) {
      if (rowIndex(rowKey) === -1) return false;
      if (!expanded) setExpanded(true);
      return true;
    };
  }

  if (virtual) {
    // An evidence link's row may be out of the drawn window: scroll the
    // grid so it is drawn (pulseRow calls this). Returns whether the
    // key is one of the grid's rows.
    table.gridScrollTo = function (rowKey) {
      var index = rowIndex(rowKey);
      if (index === -1) return false;
      var viewport = scroller.clientHeight || 560;
      scroller.scrollTop = Math.max(0, index * rowHeight - viewport / 2 + rowHeight / 2);
      drawWindow(true);
      return true;
    };
    var pending = false;
    scroller.addEventListener("scroll", function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () {
        pending = false;
        drawWindow();
      });
    });
    // Measure a real row once it is on screen, then draw to fit.
    requestAnimationFrame(function () {
      var sample = tbody.querySelector("tr:not(.grid-spacer)");
      if (sample && sample.getBoundingClientRect().height) rowHeight = sample.getBoundingClientRect().height;
      drawWindow(true);
    });
  }

  applySort();
  drawHead();
  drawBody();
  updateChooserLabel();
  // A grid wider than its box scrolls sideways: it becomes a Tab stop
  // so the arrow keys can scroll it, and its first column stays put.
  // Checked whenever the box changes size, so a grid drawn inside a
  // closed "More tables" is checked when it opens.
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(function () {
      var wide = scroller.clientWidth > 0 && scroller.scrollWidth > scroller.clientWidth + 1;
      scroller.classList.toggle("grid-wide", wide);
      if (wide && scroller.tabIndex !== 0) {
        scroller.tabIndex = 0;
        if (!scroller.hasAttribute("role")) {
          scroller.setAttribute("role", "region");
          scroller.setAttribute("aria-label", (spec.caption || "Table") + ", scrolls sideways");
        }
      }
    }).observe(scroller);
  }
  if (spec.link) {
    // Dropped once the grid has been on the page and left it.
    var seen = false;
    listenHighlight(function (scope, key) {
      if (!wrap.isConnected) return !seen;
      seen = true;
      if (scope !== spec.link.scope) return true;
      Array.prototype.forEach.call(tbody.querySelectorAll("tr[data-link-key]"), function (tr) {
        tr.classList.toggle("is-linked", key !== null && tr.getAttribute("data-link-key") === String(key));
      });
      return true;
    });
  }
  wrap.grid = {
    table: table,
    scroller: scroller,
    // Marks the rows drawn now; rows drawn later (sorting, scrolling a
    // long grid) are marked as they are drawn.
    markRows: function (mark) {
      rowMark = mark;
      Array.prototype.forEach.call(tbody.querySelectorAll("tr[data-row-key]"), function (tr) {
        if (!tr.classList.contains("is-evidence")) markRow(tr, mark(tr.getAttribute("data-row-key")));
      });
    },
  };
  return wrap;
}

// Bring a row into view and pulse it (an evidence link's target). A
// virtualised grid scrolls to the row first. grid: the table's id or
// the table itself. Returns false when the row isn't in the grid.
export function pulseRow(grid, rowKey) {
  var table = typeof grid === "string" ? document.getElementById(grid) : grid;
  if (!table) return false;
  var selector = 'tr[data-row-key="' + CSS.escape(String(rowKey)) + '"]';
  var row = table.querySelector(selector);
  if (!row && typeof table.gridScrollTo === "function" && table.gridScrollTo(rowKey)) row = table.querySelector(selector);
  if (!row) return false;
  pulseNode(row, "row-target");
  return true;
}

// Bring any evidence target into view and pulse it: a grid row
// ("row-target") or a block that isn't a grid, such as a scorecard area
// ("block-target").
export function pulseNode(node, cls) {
  cls = cls || "block-target";
  node.scrollIntoView({ block: "center", behavior: motionOK() ? "smooth" : "instant" });
  node.classList.remove(cls);
  void node.offsetWidth;
  node.classList.add(cls);
  if (motionOK()) {
    setTimeout(function () {
      node.classList.remove(cls);
    }, 1400);
    return;
  }
  // Reduced motion: the highlight holds still until the next click or
  // key, so a slower reader never loses the row.
  function clearTarget() {
    node.classList.remove(cls);
    document.removeEventListener("pointerdown", clearTarget, true);
    document.removeEventListener("keydown", clearTarget, true);
  }
  setTimeout(function () {
    document.addEventListener("pointerdown", clearTarget, true);
    document.addEventListener("keydown", clearTarget, true);
  }, 0);
}

// -- report tables and sections ------------------------------------------------

// The Glossary > How costs work card behind each section's figures,
// linked from the end of its "How to read this".
var SECTION_CARDS = {
  recache: "cache-rebuilds",
  recache_by_group: "cache-rebuilds",
  limits: "billing-mode",
  ttl: "cache-writes",
  model_swap: "model-choice",
  agent_startup: "startup-context",
  context_budget: "startup-context",
  carry: "tool-output",
  compaction_sim: "conversation-summaries",
  compactions: "conversation-summaries",
  elasticity: "billing-mode",
};

function sectionCard(sectionKey) {
  var slug = SECTION_CARDS[sectionKey];
  for (var i = 0; slug && i < COST_CARDS.length; i++) if (COST_CARDS[i].slug === slug) return COST_CARDS[i];
  return null;
}

// A section or table's header: its heading, then the (i) that opens
// "How to read this". card: the cost card it links to, if any.
export function headRow(heading, help, subject, card) {
  var row = el("div", { class: "block-head" }, [heading]);
  var helpNode = helpButton(help, subject, card);
  if (helpNode) row.appendChild(helpNode);
  return row;
}

// Notes under a table or section, with page links and glossary terms. A
// short note or two stay in view; more, or longer, fold away: they say
// how the figures were worked out, which few readers need, and keep the
// page short.
var NOTES_IN_VIEW_CHARS = 240;

export function notesList(notes, seen) {
  var list = el(
    "ul",
    { class: "notes" },
    notes.map(function (note) {
      return el("li", null, prose(note, seen));
    })
  );
  var length = notes.reduce(function (total, note) {
    return total + String(note).length;
  }, 0);
  if (notes.length <= 2 && length <= NOTES_IN_VIEW_CHARS) return list;
  return el("details", { class: "disclosure notes-detail" }, [
    el("summary", { text: "How these figures are worked out (" + (notes.length === 1 ? "1 note" : notes.length + " notes") + ")" }),
    list,
  ]);
}

// The actions a table or row is evidence for (api.js's actionIndex), as
// a button that lists them, each linked to its detail in Actions.
// chip: the header's "Feeds N actions"; otherwise a row's small mark.
function feedsButton(actions, chip) {
  var words = actions.length === 1 ? "1 action" : actions.length + " actions";
  var trigger = chip
    ? el("button", { type: "button", class: "chip chip-accent feeds-chip" }, [icon("actions", { size: 12 }), el("span", { text: "Feeds " + words })])
    : el("button", { type: "button", class: "row-feeds", "aria-label": "Evidence for " + words, title: "Evidence for " + words }, [icon("actions", { size: 12 })]);
  return popoverButton(
    trigger,
    function (body) {
      body.appendChild(el("p", { class: "popover-title", text: chip ? "Actions these figures feed" : "Actions this row is evidence for" }));
      body.appendChild(
        el(
          "ul",
          { class: "feeds-list" },
          actions.map(function (action) {
            return el("li", null, [pageLink("actions/recommendations", action.title, { id: action.key })]);
          })
        )
      );
    },
    { class: "feeds-popover", label: chip ? "Actions these figures feed" : "Actions this row is evidence for" }
  );
}

// Once the recommendations load: the header chip and the row marks.
function markFeeds(wrap, head, gridNode, tableName) {
  actionIndex().then(function (index) {
    var actions = index.byTable[tableName];
    if (!actions || !actions.length) return;
    if (!head) {
      head = el("div", { class: "block-head block-head-help" });
      wrap.insertBefore(head, wrap.firstChild);
    }
    head.appendChild(feedsButton(actions, true));
    if (gridNode && gridNode.grid) {
      gridNode.grid.markRows(function (key) {
        var rowActions = index.byRow[tableName + "\n" + key];
        return rowActions && rowActions.length ? feedsButton(rowActions, false) : null;
      });
    }
  });
}

// -- a one-row table as tiles ---------------------------------------------------

// A summary table (one row) with lead columns (helptext.py) reads as a
// strip of at most this many tiles, its other figures in "All figures".
var STRIP_TILES = 4;

// The column indexes a summary table shows as tiles, or null for a table
// that stays a grid.
function summaryColumns(table) {
  if (!table.rows || table.rows.length !== 1 || !table.lead_columns || !table.lead_columns.length) return null;
  // A table that leads with its row key is a list that happens to have
  // one row today (one agent type, one project): it stays a grid.
  if (table.columns.length && table.lead_columns.indexOf(table.columns[0].key) !== -1) return null;
  var indexes = [];
  table.lead_columns.forEach(function (key) {
    for (var i = 0; i < table.columns.length; i++) if (table.columns[i].key === key) indexes.push(i);
  });
  return indexes.length ? indexes.slice(0, STRIP_TILES) : null;
}

// A figure as a sentence would quote it: an amount in the billing mode, a
// raw value by its label.
function factValue(table, column, value) {
  if (column.kind === "money" && typeof value === "number") return moneyText(value);
  if (typeof value === "string" && table.value_labels && table.value_labels[value]) return table.value_labels[value];
  return plainText(formatCell(value, column.kind));
}

function summaryTile(table, column, value) {
  var opts = { label: column.label || column.key };
  if (column.kind === "money" && typeof value === "number") {
    var parts = moneyParts(value);
    opts.value = parts.value;
    opts.unit = parts.unit;
    opts.hint = parts.secondary || null;
  } else {
    opts.value = el("span", { text: factValue(table, column, value), title: fullValue(value, column.kind) || null });
  }
  return tile(opts);
}

// Every figure of a summary table, as a list. A first column named
// "metric" only labels the row ("all"), so it is left out. The row key
// is on the list, for an evidence link to pulse.
function summaryFacts(table) {
  var row = table.rows[0];
  var list = el("dl", { class: "fact-list summary-facts", "data-row-key": String(row[0]) });
  table.columns.forEach(function (column, i) {
    if (i === 0 && column.key === "metric") return;
    list.appendChild(el("dt", { text: column.label || column.key }));
    list.appendChild(el("dd", { text: fullValue(row[i], column.kind) || factValue(table, column, row[i]) }));
  });
  return list;
}

// options.heading false: the caller has already titled the table (a
// table shown away from its section, under its own section heading).
// options.seen: the glossary terms its section has already explained.
export function renderTable(table, tableId, currency, options) {
  // Named for evidence links (evidence.js): report table names are
  // unique across the report.
  var wrap = el("div", { class: "table-wrap", "data-table-name": table.name || null });
  var head = null;
  if (!options || options.heading !== false) {
    head = wrap.appendChild(headRow(el("h3", { text: table.title || table.name }), table.help, table.title || table.name));
  } else if (options.helpInto) {
    // The heading above says it already: its row takes the table's
    // "How to read this" (unless it has one) and its Feeds chip.
    head = options.helpInto;
    var ownHelp = table.help && !head.querySelector(".help-button") ? helpButton(table.help, table.title || table.name) : null;
    if (ownHelp) head.appendChild(ownHelp);
  } else if (table.help) {
    var helpNode = helpButton(table.help, table.title || table.name);
    if (helpNode) head = wrap.appendChild(el("div", { class: "block-head block-head-help" }, [helpNode]));
  }
  var strip = summaryColumns(table);
  if (strip) {
    // A summary (one row): its headline figures as tiles, every figure
    // one click away.
    wrap.classList.add("summary-table");
    wrap.appendChild(
      tileRow(
        strip.map(function (i) {
          return summaryTile(table, table.columns[i], table.rows[0][i]);
        }),
        { class: "summary-tiles" }
      )
    );
    var facts = summaryFacts(table);
    wrap.appendChild(
      el("details", { class: "disclosure summary-details" }, [el("summary", { text: "All figures (" + facts.querySelectorAll("dt").length + ")" }), facts])
    );
    if (table.notes && table.notes.length) wrap.appendChild(notesList(table.notes, (options && options.seen) || new Set()));
    if (table.name) markFeeds(wrap, head, null, table.name);
    return wrap;
  }
  var gridNode = wrap.appendChild(
    dataGrid({
      id: tableId,
      caption: table.title || table.name,
      columns: table.columns,
      rows: table.rows,
      lead: table.lead_columns || null,
      valueLabels: table.value_labels,
      rowGroups: table.row_groups,
      rowKinds: table.row_kinds,
      // A table read down its rows (grouped, or one kind per row) stays
      // whole, as does one a page asks for whole (options.allRows). The
      // report sends both as {} when a table has neither.
      limit: hasKeys(table.row_groups) || hasKeys(table.row_kinds) || (options && options.allRows) ? 0 : REPORT_ROWS,
      limitFrom: NEWEST_LAST[table.name] ? "end" : "start",
      empty: "Nothing to show for this window.",
      emptyNext: "A longer window may include some.",
      tint: TINT_TABLES.indexOf(table.name) !== -1,
    })
  );
  if (table.notes && table.notes.length) wrap.appendChild(notesList(table.notes, (options && options.seen) || new Set()));
  if (table.name) markFeeds(wrap, head, gridNode, table.name);
  return wrap;
}

// Tables by dashboard placement (helptext.py's table audit): "keep"
// shown, "advanced" collapsed into "More tables", "report" left to the
// CLI report (and the JSON/CSV exports), with a line saying so.
export function renderPlacedTables(container, tables, currency, idPrefix, sectionTitle, seen) {
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
      // A section's one table often shares its title: say it once, in
      // the section's own heading row.
      var sameTitle = sectionTitle && (table.title || table.name) === sectionTitle;
      var section = sameTitle ? (container.closest && container.closest("section")) || container : null;
      var sectionHead = section ? section.querySelector(".block-head") : null;
      container.appendChild(renderTable(table, tableId, currency, { heading: !sameTitle, helpInto: sectionHead, seen: seen }));
    }
  });
  if (advanced.length) {
    var details = el("details", { class: "advanced-detail" });
    details.appendChild(el("summary", { text: "More tables (" + advanced.length + ")" }));
    advanced.forEach(function (item) {
      details.appendChild(renderTable(item.table, item.id, currency, { seen: seen }));
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

// The chart a section draws above its tables (charts-types.js's
// sectionChart), set once by app.js: grid.js can't import the charts,
// which draw their Table view with this module.
var sectionChart = null;

export function setSectionChart(draw) {
  sectionChart = draw;
}

export function renderSectionGeneric(container, section, currency, idPrefix) {
  if (!section) return;
  var block = el("section", { class: "report-section", "data-section": section.key || null });
  // Sections are h2 and their tables h3: the page title is the one h1.
  block.appendChild(
    headRow(el("h2", { class: "section-title", text: section.title || section.key }), section.help, section.title || section.key, sectionCard(section.key))
  );
  // Each glossary term is explained once per section: its first use.
  var seen = new Set();
  if (section.intro) block.appendChild(el("p", { class: "section-intro" }, prose(section.intro, seen)));
  var chart = sectionChart ? sectionChart(section) : null;
  if (chart) block.appendChild(chart);
  renderPlacedTables(block, section.tables || [], currency, idPrefix || section.key, section.title || section.key, seen);
  if (section.notes && section.notes.length) block.appendChild(notesList(section.notes, seen));
  container.appendChild(block);
}

// Every report section and table that belongs on a view (links.js's
// SECTION_PAGE_MAP and TABLE_PAGE_MAP). A section drops the tables placed
// elsewhere; a table placed here from another section's gets its own
// heading. skip: section keys the view draws some other way.
export function renderMappedSections(report, viewKey, container, skip) {
  if (!report || !Array.isArray(report.sections)) return;
  report.sections.forEach(function (section) {
    if (skip && skip.indexOf(section.key) !== -1) return;
    var here = (section.tables || []).filter(function (table) {
      return viewForTable(section.key, table.name) === viewKey;
    });
    if (viewForSection(section.key) === viewKey) {
      // The Overview draws its own section.
      if (section.key === "overview") return;
      renderSectionGeneric(container, Object.assign({}, section, { tables: here }), state.currency, "report");
      return;
    }
    here.forEach(function (table, i) {
      var block = el("section", { class: "report-section" });
      block.appendChild(el("h2", { class: "section-title", text: table.title || table.name }));
      block.appendChild(renderTable(table, "report-" + section.key + "-" + table.name + "-" + i, state.currency, { heading: false }));
      container.appendChild(block);
    });
  });
}

// Shared by every report-backed route that returns a single Section
// directly (ttl, and the carry/compaction-sim/model-swap/waste routes in
// page-cache.js and page-spend.js) rather than the full report.json.
// Defensive: docs/api.md pins this to "the same shape as the CLI's
// ... section tables" but not byte-exactly to Section (a bare
// `{tables: [...]}` or a list of Table dicts are both plausible),
// and the route returns `null` outright when the section is absent
// from the assembled report -- handle each shape rather than
// assuming one and rendering nothing on a mismatch.
export function renderReportBackedSection(data, container, idPrefix, emptyNotice, emptyNext) {
  if (data && Array.isArray(data.tables)) {
    renderSectionGeneric(container, data, state.currency, idPrefix);
  } else if (Array.isArray(data)) {
    data.forEach(function (table, i) {
      container.appendChild(renderTable(table, idPrefix + "-" + i, state.currency));
    });
  } else {
    container.appendChild(emptyState(emptyNotice, null, emptyNext));
  }
}

// A small table of values already written out (a list the page built
// itself): the grid's look, in the order given. A column whose every
// cell reads as a number is right-aligned.
export function simpleTable(columns, rows, caption, id) {
  var wrap = el("div", { class: "table-wrap" });
  if (caption) wrap.appendChild(el("h3", { text: caption }));
  var numericColumn = columns.map(function (col, i) {
    return rows.length > 0 && rows.every(function (row) {
      var cell = row[i];
      return cell === null || cell === undefined || cell === "" || cell === "-" || /^[\u2212+\-<$]?[\d,.]+(%|[KMBT]| ?[a-z]+)?$/.test(String(cell));
    });
  });
  simpleTableCount += 1;
  wrap.appendChild(
    dataGrid({
      id: id || "simple-" + simpleTableCount,
      caption: caption || null,
      sortable: false,
      bar: -1,
      columns: columns.map(function (col, i) {
        return {
          key: "c" + i,
          label: col.label,
          render: function (row) {
            var cell = row[i];
            return cell === null || cell === undefined ? "" : String(cell);
          },
          kind: numericColumn[i] && i > 0 ? "float" : "str",
        };
      }),
      rows: rows,
    })
  );
  return wrap;
}

var simpleTableCount = 0;
