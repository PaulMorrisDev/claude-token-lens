/* claude-token-lens service UI: grid.js
 *
 * The data grid (docs/ui.md, "Data grid") and the report sections each
 * view draws from report.json. One grid for every table on every page:
 * sticky header, sort that is kept per table (tls:sort:<table>), a
 * column chooser for wide tables (tls:cols:<table>), numbers right-
 * aligned in even-width digits, an inline bar on the lead measure, an
 * optional tint by value, readable project names, and only the visible
 * rows drawn once a table passes 200 rows. A row can link to the same
 * thing's mark in the chart above it (core.js's highlight).
 */

import { clear, el, highlight, listenHighlight, state, storageGet, storageSet } from "./core.js";
import { cellSortValue, formatCell, fullValue, moneyText, moneyUnit, NUMERIC_KINDS, PROJECT_KEYS, projectName } from "./format.js";
import { findSection } from "./api.js";
import { viewForSection, viewForTable } from "./links.js";
import { button, emptyState, helpButton, motionOK, popoverButton, swatch } from "./ui.js";
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
  return formatCell(value, kind, state.currency);
}

// A text column holding sentences wraps as prose; any other text (a
// name, a model, a key) stays on one line, so "claude-haiku-4-5" never
// breaks across lines. A column drawn by its own render is left alone.
var PROSE_CHARS = 40;

function proseColumns(columns, rows) {
  var prose = {};
  columns.forEach(function (column) {
    if (column.render || NUMERIC_KINDS[column.kind]) return;
    prose[column.index] = rows.some(function (row) {
      var value = columnValue(column, row);
      return typeof value === "string" && value.length > PROSE_CHARS;
    });
  });
  return prose;
}

// A grid for spec (see docs/ui.md for the whole contract):
//   id        stable across draws: the saved sort and columns hang on it
//   columns   [{key, label, kind, help, render(row, value), value(row),
//              sortValue(row), nowrap}]; a report table's columns get
//              their index from their position
//   rows      arrays (a report table) or objects (an API list)
//   lead      column keys shown until more are chosen (else the first 7
//             of a table wider than 8)
//   bar       the index of the column with the inline bar (-1: none)
//   tint      true: numeric cells shade by value (the Quality grid)
//   rowAction {label(row), run(row, tr)}: each row opens something
//   rowKey    row -> the key evidence links and pulses use
//   rowClass  row -> a class for its <tr> (e.g. "row-unchanged"), or null
//   link      {scope, key(row)}: hovering or focusing a row lights the
//             chart mark with the same key in that scope, and back
//   swatch    row -> the colour the row's entity has on the chart, shown
//             as a swatch before its first cell, or null
//   sortable  false for a form laid out as a table
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
  var prose = proseColumns(columns, rows);
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

  var scroller = el("div", { class: "grid-scroll", tabIndex: rows.length > TALL_ROWS ? 0 : -1 });
  if (rows.length > TALL_ROWS) {
    scroller.classList.add("grid-tall");
    scroller.setAttribute("role", "region");
    scroller.setAttribute("aria-label", (spec.caption || "Table") + ", scrolls");
  }
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
          body.appendChild(el("p", { text: column.help }));
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
      var wrapClass = numeric ? "num" : prose[column.index] ? "cell-prose" : column.nowrap || prose[column.index] === false ? "nowrap" : "";
      var td = el("td", { class: wrapClass + " col-" + column.key, "data-sort": cellSortValue(value) });
      var content = cellContent(column, row, value, spec, rowKind);
      var full = fullValue(value, column.kind);
      if (full) td.title = full;
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
      if (spec.tint && numeric && typeof value === "number" && maxima[column.index] > 0 && column.index !== bar) {
        td.classList.add("tint-" + Math.max(1, Math.min(5, Math.ceil((value / maxima[column.index]) * 5))));
      }
      tr.appendChild(td);
    });
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
    orderedRows.forEach(function (row) {
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

  if (virtual) {
    // An evidence link's row may be out of the drawn window: scroll the
    // grid so it is drawn (pulseRow calls this). Returns whether the
    // key is one of the grid's rows.
    table.gridScrollTo = function (rowKey) {
      var index = -1;
      for (var i = 0; i < orderedRows.length; i++) {
        var row = orderedRows[i];
        var key = spec.rowKey ? spec.rowKey(row) : Array.isArray(row) ? row[0] : null;
        if (String(key) === String(rowKey)) {
          index = i;
          break;
        }
      }
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
  wrap.grid = { table: table, scroller: scroller };
  return wrap;
}

// Bring a row into view and pulse it (an evidence link's target). A
// virtualised grid scrolls to the row first. Returns false when the row
// isn't in the grid.
export function pulseRow(gridId, rowKey) {
  var table = document.getElementById(gridId);
  if (!table) return false;
  var selector = 'tr[data-row-key="' + CSS.escape(String(rowKey)) + '"]';
  var row = table.querySelector(selector);
  if (!row && typeof table.gridScrollTo === "function" && table.gridScrollTo(rowKey)) row = table.querySelector(selector);
  if (!row) return false;
  row.scrollIntoView({ block: "center", behavior: motionOK() ? "smooth" : "instant" });
  row.classList.remove("row-target");
  void row.offsetWidth;
  row.classList.add("row-target");
  if (motionOK()) {
    setTimeout(function () {
      row.classList.remove("row-target");
    }, 1400);
    return true;
  }
  // Reduced motion: the highlight holds still until the next click or
  // key, so a slower reader never loses the row.
  function clearTarget() {
    row.classList.remove("row-target");
    document.removeEventListener("pointerdown", clearTarget, true);
    document.removeEventListener("keydown", clearTarget, true);
  }
  setTimeout(function () {
    document.addEventListener("pointerdown", clearTarget, true);
    document.addEventListener("keydown", clearTarget, true);
  }, 0);
  return true;
}

// -- report tables and sections ------------------------------------------------

// A section or table's header: its heading, then the (i) that opens
// "How to read this".
export function headRow(heading, help, subject) {
  var row = el("div", { class: "block-head" }, [heading]);
  var helpNode = helpButton(help, subject);
  if (helpNode) row.appendChild(helpNode);
  return row;
}

// options.heading false: the caller has already titled the table (a
// table shown away from its section, under its own section heading).
export function renderTable(table, tableId, currency, options) {
  var wrap = el("div", { class: "table-wrap" });
  if (!options || options.heading !== false) {
    wrap.appendChild(headRow(el("h3", { text: table.title || table.name }), table.help, table.title || table.name));
  } else if (table.help) {
    var helpNode = helpButton(table.help, table.title || table.name);
    if (helpNode) wrap.appendChild(el("div", { class: "block-head block-head-help" }, [helpNode]));
  }
  wrap.appendChild(
    dataGrid({
      id: tableId,
      caption: table.title || table.name,
      columns: table.columns,
      rows: table.rows,
      lead: table.lead_columns || null,
      valueLabels: table.value_labels,
      rowGroups: table.row_groups,
      rowKinds: table.row_kinds,
      empty: "No rows for this window.",
    })
  );
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

// Tables by dashboard placement (helptext.py's table audit): "keep"
// shown, "advanced" collapsed into "More tables", "report" left to the
// CLI report (and the JSON/CSV exports), with a line saying so.
export function renderPlacedTables(container, tables, currency, idPrefix, sectionTitle) {
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
      // A section's one table often shares its title: say it once.
      var sameTitle = sectionTitle && (table.title || table.name) === sectionTitle;
      container.appendChild(renderTable(table, tableId, currency, sameTitle ? { heading: false } : null));
    }
  });
  if (advanced.length) {
    var details = el("details", { class: "advanced-detail" });
    details.appendChild(el("summary", { text: "More tables (" + advanced.length + ")" }));
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
  var block = el("section", { class: "report-section", "data-section": section.key || null });
  // Sections are h2 and their tables h3: the page title is the one h1.
  block.appendChild(headRow(el("h2", { class: "section-title", text: section.title || section.key }), section.help, section.title || section.key));
  if (section.intro) block.appendChild(el("p", { class: "section-intro", text: section.intro }));
  renderPlacedTables(block, section.tables || [], currency, idPrefix || section.key, section.title || section.key);
  if (section.notes && section.notes.length) {
    block.appendChild(
      el(
        "ul",
        { class: "notes" },
        section.notes.map(function (note) {
          return el("li", { text: note });
        })
      )
    );
  }
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
