/* claude-token-lens service UI.
 *
 * Vanilla ES2020, no framework, no build step, no external reference of
 * any kind (see docs/ui.md's Constraints section). Everything is a
 * plain <script src="/static/app.js"> loaded by index.html under the
 * service's CSP (`script-src 'self'`, so no inline handlers/scripts
 * anywhere in this bundle).
 *
 * Every route this file talks to is one of docs/api.md's `/api/*`
 * routes, always same-origin, always the `{"ok": true, "data": ...}` /
 * `{"ok": false, "error": {...}}` envelope. `fetchJson` is the one
 * place that envelope is unwrapped; every renderer below either gets
 * `data` or a rendered inline error notice -- never a raw stack trace,
 * never a blank panel.
 *
 * No tab holds state the server doesn't already have (docs/ui.md's Data
 * flow section): `localStorage` is used only for two purely cosmetic,
 * per-viewer conveniences -- the last-selected tab and a table's last
 * sort column/direction -- never data the server is the source of
 * truth for. Reads/writes are wrapped in try/catch per the artifact
 * browser-storage guidance: a private window or blocked site data must
 * never break rendering.
 */

(function () {
  "use strict";

  // -- tiny DOM helpers ------------------------------------------------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined) return;
        if (key === "class") {
          node.className = value;
        } else if (key === "text") {
          node.textContent = value;
        } else if (key === "html") {
          // Only ever used with strings this file built itself from
          // escaped/known-safe fragments -- never with server data.
          node.innerHTML = value;
        } else if (key === "for") {
          // <label for="..."> is exposed as the `htmlFor` IDL property,
          // not `for` (a reserved word in the DOM API, not just JS).
          node.htmlFor = value;
        } else if (key === "role" || key.indexOf("data-") === 0 || key.indexOf("aria-") === 0) {
          node.setAttribute(key, value);
        } else {
          node[key] = value;
        }
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // Used only where a value must go through innerHTML (the inline-SVG
  // timeline below) rather than textContent/setAttribute.
  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function storageGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      /* private window, blocked site data, or a full quota -- ignore */
    }
  }

  // -- formatting (mirrors render/tables.py::format_cell exactly) ------

  function thousands(n) {
    return Math.round(Number(n)).toLocaleString("en-US");
  }

  function formatSecs(value) {
    var total = Math.round(value);
    var sign = total < 0 ? "-" : "";
    total = Math.abs(total);
    var hours = Math.floor(total / 3600);
    var remainder = total % 3600;
    var minutes = Math.floor(remainder / 60);
    var seconds = remainder % 60;
    var parts = [];
    if (hours) parts.push(hours + "h");
    if (hours || minutes) parts.push(minutes + "m");
    parts.push(seconds + "s");
    return sign + parts.join(" ");
  }

  var COLUMN_KINDS = ["str", "int", "float", "pct", "money", "tokens", "secs"];
  var NUMERIC_KINDS = { int: true, float: true, pct: true, money: true, tokens: true, secs: true };

  // Mirrors render/tables.py::format_cell's kind switch exactly.
  // ``toLocaleString("en-US", ...)`` is used (fixed locale, not the
  // browser's own) for thousands separators so output stays
  // deterministic regardless of the viewer's system locale.
  function formatCell(value, kind, currency) {
    currency = currency || "USD";
    if (value === null || value === undefined) return "-";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (COLUMN_KINDS.indexOf(kind) === -1) kind = "str";
    switch (kind) {
      case "str":
        // A mixed "metric / value" table: numbers read with separators.
        if (typeof value === "number" && isFinite(value)) {
          return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
        }
        return String(value);
      case "int":
      case "tokens":
        return Math.round(Number(value)).toLocaleString("en-US");
      case "float":
        return Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      case "pct":
        return Number(value).toFixed(1) + "%";
      case "money":
        return Number(value).toFixed(2) + " " + currency;
      case "secs":
        return formatSecs(Number(value));
      default:
        return String(value);
    }
  }

  function cellSortValue(value) {
    if (value === null || value === undefined) return "";
    return String(value);
  }

  // -- fetch / envelope handling ----------------------------------------

  function fetchJson(url, options) {
    return fetch(url, options)
      .then(function (res) {
        return res
          .json()
          .catch(function () {
            return { ok: false, error: { code: "bad_response", message: "response was not valid JSON (HTTP " + res.status + ")" } };
          })
          .then(function (body) {
            return { httpStatus: res.status, body: body };
          });
      })
      .catch(function (err) {
        return { httpStatus: 0, body: { ok: false, error: { code: "network_error", message: String(err && err.message ? err.message : err) } } };
      });
  }

  function errorNotice(error) {
    var code = (error && error.code) || "error";
    var message = (error && error.message) || "Something went wrong.";
    return el("div", { class: "notice error", role: "alert" }, [
      el("strong", { text: "[" + code + "] " }),
      el("span", { text: message }),
    ]);
  }

  function loadingNode(label) {
    return el("p", { class: "loading", text: label || "Loading…" });
  }

  /**
   * Fetch `url`, replacing `container`'s contents with `loading()` while
   * in flight, then either `render(data, container)` on `ok: true`, or
   * an inline error notice on `ok: false` / a network failure. Never
   * throws -- this is the one place every tab's data flow funnels
   * through, per docs/ui.md's "or shows the error.message inline...
   * never a raw stack trace" contract.
   */
  function loadInto(container, url, render, options) {
    clear(container);
    container.appendChild(loadingNode());
    return fetchJson(url, options).then(function (result) {
      clear(container);
      var body = result.body;
      if (!body || body.ok !== true) {
        container.appendChild(errorNotice(body && body.error));
        return null;
      }
      try {
        render(body.data, container);
      } catch (err) {
        container.appendChild(errorNotice({ code: "render_error", message: String(err && err.message ? err.message : err) }));
      }
      return body.data;
    });
  }

  // -- report.json cache (shared across Overview/Cache/TTL/Agents/
  //    Config/Usage/Diagnostics/Recommendations) ------------------------

  var state = {
    // Review finding 21: this used to be a single `reportPromise` shared
    // by every caller (Overview's Scorecard/Totals, and the Cache/TTL/
    // Agents/Config/Usage/Diagnostics/Recommendations tabs), memoized
    // forever after the first fetch. /api/report.json accepts a
    // `window_days` query parameter and the server memoizes its own
    // response per `(window_days, change_token)` (docs/api.md), but the
    // client-side cache didn't vary by window at all -- once Overview's
    // window selector fetched a report for one window, every tab kept
    // reading that same cached promise even after the selector changed,
    // silently showing stale data for every other window choice. Keyed
    // by the header picker's window now (WINDOW_OPTIONS), so each window
    // gets its own cache entry.
    reportPromises: {},
    currency: "USD",
    // The one window every tab reads (the picker in the header): a
    // number of days, or a named window the server resolves ("1h",
    // "today", "24h", "change", "all").
    window: "30",
  };

  // The window as a query parameter, for every window-aware route.
  function windowParam() {
    var value = state.window || "all";
    return /^[0-9]+$/.test(value) ? "window_days=" + value : "window=" + encodeURIComponent(value);
  }

  function withWindow(url) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") + windowParam();
  }

  function loadReport() {
    var key = state.window;
    if (!state.reportPromises[key]) {
      var url = withWindow("/api/report.json");
      state.reportPromises[key] = fetchJson(url).then(function (result) {
        var body = result.body;
        if (!body || body.ok === false) {
          return { error: (body && body.error) || { code: "error", message: "failed to load report" } };
        }
        // docs/api.md: unlike every other route, /api/report.json is the
        // raw rendered document ({"schema_version": ..., "report": {...}}),
        // not the {"ok": true, "data": ...} envelope -- kept unwrapped for
        // byte parity with the CLI's own `report --json` output. Accept
        // both shapes here: `body.ok === true` is an enveloped response
        // (a possible future/alternate deployment), whose report lives at
        // `body.data.report`; anything else that reached this point (no
        // `ok` key, or `ok` truthy-but-not-boolean) is the real unwrapped
        // shape, whose report is `body.report` directly.
        var report = body.ok === true ? body.data && body.data.report : body.report;
        if (report && report.meta && report.meta.pricing && report.meta.pricing.currency) {
          state.currency = report.meta.pricing.currency;
        }
        return { report: report };
      });
    }
    return state.reportPromises[key];
  }

  function findSection(report, key) {
    if (!report || !Array.isArray(report.sections)) return null;
    for (var i = 0; i < report.sections.length; i++) {
      if (report.sections[i].key === key) return report.sections[i];
    }
    return null;
  }

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

  function formatEvidenceValue(report, value, sourceTable, rowKey, currency) {
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
  function helpBlock(help) {
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

  function renderTable(table, tableId, currency) {
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
            '<rect x="0" y="2" width="' + barWidth + '" height="6" rx="1" fill="var(--bar-bg)"></rect>' +
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
  function renderPlacedTables(container, tables, currency, idPrefix) {
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

  function renderSectionGeneric(container, section, currency, idPrefix) {
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

  // Section key -> tab, per this work package's brief. Anything not
  // listed here lands in Diagnostics so a new section is never silently
  // dropped when report.py grows one.
  var SECTION_TAB_MAP = {
    recache: "cache",
    // Review finding 20: docs/ui.md documents recache_by_group as part of
    // this map too. It never actually arrives as a section's own `key`
    // today -- report.py's _build_recache_section appends it as an extra
    // *table* inside the "recache" section rather than a section in its
    // own right -- but it costs nothing to map here now, so a future
    // refactor that promotes it to its own section lands on the Cache
    // tab without anyone having to remember to update this file too.
    recache_by_group: "cache",
    // v3-limits wiring: usage-cap pauses force the exact full-expiry
    // re-cache recache/ttl already attribute cost to -- grouped onto
    // Cache alongside them rather than Diagnostics's parse-quality
    // counters.
    limits: "cache",
    ttl: "ttl",
    agent_startup: "agents",
    agents: "agents",
    quality: "agents",
    workflows: "agents",
    workstyle: "agents",
    sessions: "sessions",
    // The Config tab renders the config section's tables once, from
    // /api/config-diff?auto_keys=1 (renderConfig skips it here).
    config: "config",
    context_budget: "config",
    baseline_comparison: "config",
    // The scorecard is the Overview tab's tiles, never a generic table.
    scorecard: "overview",
    usage: "usage",
    // Subscription only: how many tokens a usage limit holds.
    elasticity: "usage",
    compactions: "usage",
    phases: "usage",
    // v4 wiring round: carry/compaction_sim/model_swap/waste each have
    // their own dedicated report-backed route (/api/carry etc., fetched
    // directly by the Savings tab below, the same way ttl's own entry
    // above keeps it off Diagnostics even though nothing ever calls
    // renderMappedSections(report, "ttl", ...)) -- mapped here purely so
    // they don't fall through to the Diagnostics tab's default when the
    // full report.json is walked there.
    carry: "savings",
    compaction_sim: "savings",
    model_swap: "savings",
    waste: "savings",
  };

  // skip: section keys the tab renders some other way.
  function renderMappedSections(report, tabKey, container, skip) {
    if (!report || !Array.isArray(report.sections)) return;
    report.sections.forEach(function (section) {
      if (section.key === "overview") return; // handled by the Overview tab directly
      if (skip && skip.indexOf(section.key) !== -1) return;
      var target = SECTION_TAB_MAP[section.key] || "diagnostics";
      if (target !== tabKey) return;
      renderSectionGeneric(container, section, state.currency, "report");
    });
  }

  // ======================================================================
  // Overview tab
  // ======================================================================

  // One h2 per tab: its title (matching index.html's tab button) and a
  // one-line intro saying what question the tab answers.
  var TAB_TITLES = {
    overview: "Overview",
    quick: "Quick actions",
    sessions: "Sessions",
    cache: "Cache",
    ttl: "Cache lifetime (TTL)",
    savings: "Savings",
    agents: "Agents",
    context: "Context files",
    config: "Config",
    profiles: "Profiles",
    recommendations: "Recommendations",
    usage: "Usage",
    diagnostics: "Data quality",
    glossary: "Glossary",
  };

  var TAB_INTROS = {
    overview: "Your totals for the window, and a scorecard of where your tokens go.",
    quick:
      "One question per way of saving tokens, answered from your own sessions in the window, with the evidence and a fix you can copy. Nothing here changes Claude Code by itself.",
    sessions: "Every session, newest first. Pick one to see its replies on a timeline.",
    cache:
      "When Claude Code had to rebuild the prompt cache, and why. A rebuild writes the whole conversation to the cache again, at the cache-write price.",
    ttl: "How long the prompt cache stays warm, and whether a longer cache lifetime would have paid for itself.",
    savings:
      "Estimates of what you could save: shorter tool output, earlier conversation summaries, cheaper models, and replies that did no useful work.",
    agents: "What your subagents cost, what they are given when they start, and what they hand back.",
    context:
      "What Claude reads at the start of every session and subagent: your CLAUDE.md files and the skills list. How often each is sent, what it costs, and how to trim it.",
    config: "Your Claude Code settings, how they changed, and how much of the context window is used before you type.",
    profiles:
      "Groups of settings: make one from a goal with an estimate of its effect, compare it with yours, apply it, and see what each change you made did.",
    recommendations: "Changes worth making, most important first.",
    usage: "Usage over time, by project, and in five-hour blocks.",
    diagnostics:
      "What this tool installed and what to expect, how much of your data could be read, and anything the parser had to skip.",
    glossary: "The words this dashboard uses, in plain English.",
  };

  function tabHeading(panel, tabKey) {
    panel.appendChild(el("h2", { text: TAB_TITLES[tabKey] || tabKey }));
    if (TAB_INTROS[tabKey]) panel.appendChild(el("p", { class: "tab-intro", text: TAB_INTROS[tabKey] }));
  }

  var LEVEL_LABELS = { 5: "excellent", 4: "good", 3: "fair", 2: "poor", 1: "very poor" };
  // Short windows show a change's effect within the hour; "Since my
  // last change" starts at the newest apply, undo or settings change.
  // A window counts every session active in it, whole.
  var WINDOW_OPTIONS = [
    { label: "Last hour", value: "1h" },
    { label: "Today", value: "today" },
    { label: "Last 24 hours", value: "24h" },
    { label: "Last 7 days", value: "7" },
    { label: "Last 30 days", value: "30" },
    { label: "Last 90 days", value: "90" },
    { label: "All time", value: "all" },
    { label: "Since my last change", value: "change" },
  ];

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
        activateTab("recommendations", { focus: true });
      });
      container.appendChild(more);
      var quick = el("button", {
        type: "button",
        class: "link-button",
        text: "Or check one thing at a time in Quick actions",
      });
      quick.addEventListener("click", function () {
        activateTab("quick", { focus: true });
      });
      container.appendChild(quick);
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

  function renderOverview(panel) {
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
        billingLine.textContent =
          (meta.billing_mode === "subscription"
            ? "Billing: Pro or Max plan. Amounts are list-price equivalents, not what you are charged"
            : "Billing: pay per token (API). Amounts are what the tokens cost at list price") +
          (meta.billing_source ? " (" + meta.billing_source + ")." : ".");
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

  function renderHealth(health, container) {
    var watcher = health.watcher || {};
    if (health.service_registered === false) {
      container.appendChild(
        el("div", { class: "notice error", role: "alert" }, [
          el("p", {
            text:
              "The service is not registered to start at logon; history older than cleanupPeriodDays will be lost after a reboot. Run: claude-token-lens install-service",
          }),
        ])
      );
    }
    var lines = [
      "status: " + (health.status || "unknown"),
      "version: " + (health.version || "-"),
      "schema version: " + (health.schema_version === undefined ? "-" : health.schema_version),
      "watcher last tick finished: " + (watcher.finished_at || "never"),
      "files scanned / parsed: " + (watcher.files_scanned || 0) + " / " + (watcher.files_parsed || 0),
      "sessions upserted: " + (watcher.sessions_upserted || 0),
      "errors this tick: " + (watcher.errors || 0),
    ];
    var list = el(
      "ul",
      { class: "notes" },
      lines.map(function (line) {
        return el("li", { text: line });
      })
    );
    container.appendChild(list);
    if (watcher.error_messages && watcher.error_messages.length) {
      container.appendChild(el("p", { text: "Recent errors:" }));
      container.appendChild(
        el(
          "ul",
          { class: "notes" },
          watcher.error_messages.map(function (msg) {
            return el("li", { text: msg });
          })
        )
      );
    }
    updateFooterHealth(health);
  }

  function updateFooterHealth(health) {
    var footer = document.getElementById("footer-health");
    if (!footer) return;
    var watcher = health.watcher || {};
    footer.textContent =
      (health.version ? "claude-token-lens " + health.version + " — " : "") +
      "Service " + (health.status || "unknown") + " — last watcher tick: " + (watcher.finished_at || "never") + " — " + (watcher.files_parsed || 0) + " files parsed, " + (watcher.errors || 0) + " errors.";
  }

  // ======================================================================
  // Sessions tab
  // ======================================================================

  var sessionsState = { limit: 50, offset: 0 };

  function renderSessions(panel) {
    clear(panel);
    tabHeading(panel, "sessions");

    var tableContainer = el("div", { id: "sessions-table" });
    var pager = el("div", { class: "pager" });
    var prevBtn = el("button", { type: "button", text: "Previous" });
    var nextBtn = el("button", { type: "button", text: "Next" });
    var rangeLabel = el("span", { text: "" });
    pager.appendChild(prevBtn);
    pager.appendChild(rangeLabel);
    pager.appendChild(nextBtn);

    panel.appendChild(pager);
    panel.appendChild(tableContainer);

    var detailContainer = el("div", { id: "session-detail" });
    panel.appendChild(detailContainer);

    var sectionContainer = el("div", { id: "sessions-sections" });
    panel.appendChild(sectionContainer);
    loadReport().then(function (result) {
      if (result.error) {
        sectionContainer.appendChild(errorNotice(result.error));
        return;
      }
      renderMappedSections(result.report, "sessions", sectionContainer);
    });

    function load() {
      rangeLabel.textContent = "Rows " + (sessionsState.offset + 1) + "–" + (sessionsState.offset + sessionsState.limit);
      prevBtn.disabled = sessionsState.offset === 0;
      var url = "/api/sessions?limit=" + sessionsState.limit + "&offset=" + sessionsState.offset;
      loadInto(tableContainer, url, function (rows, container) {
        renderSessionsTable(rows, container, detailContainer);
        nextBtn.disabled = rows.length < sessionsState.limit;
      });
    }

    prevBtn.addEventListener("click", function () {
      sessionsState.offset = Math.max(0, sessionsState.offset - sessionsState.limit);
      load();
    });
    nextBtn.addEventListener("click", function () {
      sessionsState.offset += sessionsState.limit;
      load();
    });

    load();
  }

  var SESSION_COLUMNS = [
    { key: "id", label: "Session", kind: "str" },
    { key: "slug", label: "Project", kind: "str" },
    { key: "first_ts", label: "First seen", kind: "str" },
    { key: "last_ts", label: "Last seen", kind: "str" },
    { key: "span_s", label: "Span", kind: "secs" },
    { key: "mode", label: "Mode", kind: "str" },
    { key: "purpose", label: "Purpose", kind: "str" },
    { key: "entrypoint", label: "Entrypoint", kind: "str" },
    { key: "total_cost", label: "Cost", kind: "money" },
    { key: "total_tokens", label: "Tokens", kind: "tokens" },
  ];

  // Shown only when some sessions ran somewhere else, such as WSL.
  var SOURCE_COLUMN = { key: "source", label: "Where", kind: "str" };

  function renderSessionsTable(rows, container, detailContainer) {
    if (!rows.length) {
      container.appendChild(el("p", { class: "notice", text: "No sessions in this window." }));
      return;
    }
    var columns = SESSION_COLUMNS.slice();
    if (rows.some(function (row) { return row.source && row.source !== "This computer"; })) {
      columns.splice(2, 0, SOURCE_COLUMN);
    }
    var table = el("table", { id: "sessions-list-table" });
    var thead = el("thead");
    var headRow = el("tr");
    columns.forEach(function (col) {
      headRow.appendChild(el("th", { class: NUMERIC_KINDS[col.kind] ? "num" : null, text: col.label }));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    rows.forEach(function (row) {
      var tr = el("tr", { class: "clickable", tabIndex: 0, "data-session-id": row.id });
      columns.forEach(function (col) {
        var value = row[col.key];
        tr.appendChild(el("td", { class: NUMERIC_KINDS[col.kind] ? "num" : null, text: formatCell(value, col.kind, state.currency) }));
      });
      function open() {
        renderSessionDetail(detailContainer, row.id);
      }
      tr.addEventListener("click", open);
      tr.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    container.appendChild(table);
  }

  function renderSessionDetail(container, sessionId) {
    clear(container);
    container.appendChild(loadingNode("Loading session " + sessionId + "…"));
    fetchJson("/api/session/" + encodeURIComponent(sessionId)).then(function (result) {
      clear(container);
      var body = result.body;
      if (!body || body.ok !== true) {
        container.appendChild(errorNotice(body && body.error));
        return;
      }
      buildSessionDetail(container, body.data);
    });
  }

  function buildSessionDetail(container, session) {
    var wrap = el("div", { class: "session-detail" });
    wrap.appendChild(el("h3", { text: "Session " + session.id }));

    var summaryList = el("ul", { class: "notes" }, [
      el("li", { text: "Project: " + (session.slug || "-") }),
      el("li", { text: "Where it ran: " + (session.source || "This computer") }),
      el("li", { text: "Archetype: " + (session.archetype || "-") }),
      el("li", { text: "Span: " + formatCell(session.span_s, "secs") }),
      el("li", { text: "Cost: " + formatCell(session.total_cost, "money", state.currency) }),
      el("li", { text: "Tokens: " + formatCell(session.total_tokens, "tokens") }),
      el("li", { text: "Billing mode: " + (session.billing_mode || "-") }),
      el("li", { text: "Profile: " + (session.profile_id || "none") }),
    ]);
    wrap.appendChild(summaryList);

    // -- "why was this session expensive?" (template sentences, no model) --
    var explainBox = el("div", { class: "session-explain" });
    wrap.appendChild(el("h3", { text: "Why was this session expensive?" }));
    wrap.appendChild(explainBox);
    loadInto(explainBox, "/api/session/" + encodeURIComponent(session.id) + "/explain", renderSessionExplain);

    // -- tag overrides (mode/purpose) -> POST /api/sessions/<id>/tags --
    var tagControls = el("div", { class: "tag-controls" });
    var modeLabel = el("label", { text: "Mode override" });
    var modeSelect = buildTagSelect(["", "interactive", "long-agentic", "overnight", "mixed"], (session.tags && session.tags.mode) || session.mode);
    modeLabel.appendChild(modeSelect);

    var purposeLabel = el("label", { text: "Purpose override" });
    var purposeSelect = buildTagSelect(
      ["", "local-llm-pipeline", "agent-fanout", "workflow-run", "review", "test-triage", "planning", "docs", "refactor", "general-dev"],
      (session.tags && session.tags.purpose) || session.purpose
    );
    purposeLabel.appendChild(purposeSelect);

    var applyBtn = el("button", { type: "button", text: "Apply tags" });
    var tagStatus = el("span", { class: "notes" });
    tagControls.appendChild(modeLabel);
    tagControls.appendChild(purposeLabel);
    tagControls.appendChild(applyBtn);
    tagControls.appendChild(tagStatus);
    wrap.appendChild(tagControls);

    applyBtn.addEventListener("click", function () {
      applyBtn.disabled = true;
      tagStatus.textContent = "Saving…";
      var updates = [];
      if (modeSelect.value) updates.push(["mode", modeSelect.value]);
      if (purposeSelect.value) updates.push(["purpose", purposeSelect.value]);
      if (!updates.length) {
        tagStatus.textContent = "Choose a mode or purpose first.";
        applyBtn.disabled = false;
        return;
      }
      Promise.all(
        updates.map(function (pair) {
          return fetchJson("/api/sessions/" + encodeURIComponent(session.id) + "/tags", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: pair[0], value: pair[1] }),
          });
        })
      ).then(function (results) {
        applyBtn.disabled = false;
        var failed = results.filter(function (r) {
          return !r.body || r.body.ok !== true;
        });
        if (failed.length) {
          tagStatus.textContent = "";
          tagStatus.appendChild(errorNotice(failed[0].body && failed[0].body.error));
        } else {
          tagStatus.textContent = "Saved.";
          renderSessionDetail(container, session.id);
        }
      });
    });

    // -- transcripts table (no path -- see docs/api.md's privacy rule) --
    wrap.appendChild(el("h3", { text: "Transcripts" }));
    if (session.transcripts && session.transcripts.length) {
      var tTable = el("table");
      var tHead = el("thead", null, [
        el("tr", null, ["Kind", "Agent type", "Spawn depth", "Parent agent"].map(function (h) {
          return el("th", { text: h });
        })),
      ]);
      var tBody = el(
        "tbody",
        null,
        session.transcripts.map(function (t) {
          return el("tr", null, [
            el("td", { text: t.kind || "-" }),
            el("td", { text: t.agent_type || "top-level" }),
            el("td", { class: "num", text: String(t.spawn_depth === undefined ? "-" : t.spawn_depth) }),
            el("td", { text: t.parent_agent_id || "-" }),
          ]);
        })
      );
      tTable.appendChild(tHead);
      tTable.appendChild(tBody);
      wrap.appendChild(tTable);
    } else {
      wrap.appendChild(el("p", { class: "notice", text: "No transcripts recorded." }));
    }

    // -- timeline: context size over turns with event markers ----------
    wrap.appendChild(el("h3", { text: "Timeline" }));
    wrap.appendChild(buildSessionTimeline(session));

    container.appendChild(wrap);
  }

  function renderSessionExplain(data, container) {
    container.appendChild(el("p", { class: "explain-headline", text: data.headline }));
    if (data.sentences && data.sentences.length) {
      container.appendChild(
        el(
          "ul",
          { class: "explain-sentences" },
          data.sentences.map(function (sentence) {
            return el("li", { text: sentence });
          })
        )
      );
    }
    var split = (data.cost_split || []).filter(function (part) {
      return part.share_pct >= 0.05;
    });
    if (!split.length) return;
    var table = el("table", { class: "explain-split" });
    table.appendChild(
      el("thead", null, [
        el("tr", null, [el("th", { text: "What the cost went on" }), el("th", { text: "Share" }), el("th", { text: "" })]),
      ])
    );
    table.appendChild(
      el(
        "tbody",
        null,
        split.map(function (part) {
          var bar = el("span", { class: "share-bar" });
          bar.style.width = Math.max(1, Math.round(part.share_pct)) + "%";
          return el("tr", null, [
            el("td", { text: part.label }),
            el("td", { class: "num", text: formatCell(part.share_pct, "pct") }),
            el("td", { class: "share-cell" }, [bar]),
          ]);
        })
      )
    );
    container.appendChild(table);
    container.appendChild(el("p", { class: "notes", text: "Shares are worked out at list price for each model's token counts." }));
  }

  function buildTagSelect(options, current) {
    var select = el("select");
    options.forEach(function (opt) {
      select.appendChild(el("option", { value: opt, text: opt || "(no override)" }));
    });
    if (current && options.indexOf(current) !== -1) select.value = current;
    return select;
  }

  // `/api/session/<id>` (S1-integration fix 1.g, see docs/api.md) adds
  // `turn_series`: a list of `[turn_index, ctx, cache_creation_tokens,
  // is_recache, preceding_primary]` per priced turn of the session's
  // top-level transcript, plus `markers`: `{compactions, spawns,
  // human}`, each a list of turn_index values, and (v3-limits wiring)
  // `limit_markers`: `[{ts, kind, detail}, ...]` usage-cap pause/resume/
  // agent-terminated events. It's absent (rather than an empty list)
  // whenever the store has no stored top-level transcript digest to
  // source it from -- e.g. a session ingested before the watcher parsed
  // a top-level transcript, or one whose digest failed to decode -- so
  // this still falls back to a clearly labelled placeholder rather than
  // inventing a curve.
  function findPerTurnSeries(session) {
    var series = session.turn_series;
    return Array.isArray(series) && series.length ? series : null;
  }

  function toTurnIndexSet(list) {
    var set = {};
    (list || []).forEach(function (turnIndex) {
      set[turnIndex] = true;
    });
    return set;
  }

  function buildSessionTimeline(session) {
    var series = findPerTurnSeries(session);
    if (!series) {
      return el("div", { class: "placeholder-box" }, [
        el("p", { text: "no per-turn data for this session" }),
        el("p", { class: "notes", text: "/api/session/<id> only returns turn_series once the watcher has stored this session's top-level transcript digest." }),
      ]);
    }

    var markers = session.markers || {};
    var compactionTurns = toTurnIndexSet(markers.compactions);
    var spawnTurns = toTurnIndexSet(markers.spawns);
    var humanTurns = toTurnIndexSet(markers.human);

    var width = 640, height = 180, padding = 28;
    // Finding 10: this used to compute the max via Math.max, spreading
    // the whole per-turn array as individual call arguments -- a
    // session with tens of thousands of turns could blow the engine's
    // argument-count/call-stack limit ("Maximum call stack size
    // exceeded"). A plain loop has no such limit (also cheaper: no
    // intermediate array allocation).
    var maxCtx = 0;
    for (var mi = 0; mi < series.length; mi++) {
      var ctxValue = series[mi][1] || 0;
      if (ctxValue > maxCtx) maxCtx = ctxValue;
    }
    maxCtx = maxCtx || 1;
    var n = series.length;
    var points = series.map(function (t, i) {
      var x = padding + (n > 1 ? (i / (n - 1)) * (width - 2 * padding) : 0);
      var y = height - padding - ((t[1] || 0) / maxCtx) * (height - 2 * padding);
      return [x, y];
    });

    // Built as an inline markup string rather than via
    // `document.createElementNS`: an <svg> assigned through
    // `innerHTML` into an HTML document is placed in the SVG namespace
    // automatically by the HTML5 parser's own foreign-content handling
    // (no explicit namespace URI needed), which keeps this file free of
    // the XML namespace URI's own URL-scheme literal -- see docs/ui.md's
    // "no bare URL-scheme literal anywhere in static/*" rule; that
    // namespace string names no reachable resource, but this file
    // avoids it anyway rather than relying on that distinction. Every
    // dynamic value embedded below is either a fixed-precision number
    // or passed through `escapeHtml`.
    var markerColors = { recache: "#c0392b", compaction: "#a06a00", spawn: "#2563eb", human: "#1a7f37" };
    // v3-limits wiring: distinct colours from markerColors above, drawn
    // in the blank strip above the context-size line (y well below
    // `padding`) rather than pinned to a turn's own point -- a
    // usage-limit event's `ts` falls *inside* the pause gap between two
    // turns, not at a turn_index of its own, so unlike recache/
    // compaction/spawn/human it cannot share the index-based x position
    // those markers use. Positioned instead by interpolating `ts`
    // between the session's own `first_ts`/`last_ts` (docs/limits.md's
    // "Session-timeline marker contract" / docs/ui.md).
    var limitMarkerColors = { limit_hit: "#9333ea", limit_resume: "#0891b2", agent_terminated: "#ea580c" };
    var svgParts = [];
    svgParts.push(
      '<svg viewBox="0 0 ' + width + " " + height + '" class="timeline-svg" role="img" aria-label="' +
        escapeHtml("Context size over turns for session " + session.id) +
        '">'
    );
    if (points.length > 1) {
      svgParts.push(
        '<polyline points="' +
          points
            .map(function (p) {
              return p[0].toFixed(1) + "," + p[1].toFixed(1);
            })
            .join(" ") +
          '" fill="none" stroke="var(--accent)" stroke-width="1.5"></polyline>'
      );
    } else if (points.length === 1) {
      // Finding 11: a single-turn session has exactly one point, and a
      // <polyline> needs at least two to draw anything -- it silently
      // rendered nothing at all. Draw the one point as a dot instead.
      svgParts.push(
        '<circle cx="' + points[0][0].toFixed(1) + '" cy="' + points[0][1].toFixed(1) +
          '" r="3" fill="var(--accent)"></circle>'
      );
    }
    series.forEach(function (turn, i) {
      var turnIndex = turn[0];
      var isRecache = turn[3];
      var kinds = [];
      if (isRecache) kinds.push("recache");
      if (compactionTurns[turnIndex]) kinds.push("compaction");
      if (spawnTurns[turnIndex]) kinds.push("spawn");
      if (humanTurns[turnIndex]) kinds.push("human");
      kinds.forEach(function (kind) {
        var label = escapeHtml("Turn " + (turnIndex || i + 1) + ": " + kind);
        svgParts.push(
          '<circle cx="' +
            points[i][0].toFixed(1) +
            '" cy="' +
            points[i][1].toFixed(1) +
            '" r="3" fill="' +
            (markerColors[kind] || "var(--muted)") +
            '"><title>' +
            label +
            "</title></circle>"
        );
      });
    });

    var limitMarkers = Array.isArray(session.limit_markers) ? session.limit_markers : [];
    var limitKindsSeen = {};
    if (limitMarkers.length) {
      var firstMs = Date.parse(session.first_ts);
      var lastMs = Date.parse(session.last_ts);
      var hasTimeRange = !isNaN(firstMs) && !isNaN(lastMs) && lastMs > firstMs;
      limitMarkers.forEach(function (marker) {
        var ms = Date.parse(marker.ts);
        if (isNaN(ms)) return;
        var fraction = hasTimeRange ? Math.max(0, Math.min(1, (ms - firstMs) / (lastMs - firstMs))) : 0;
        var mx = padding + fraction * (width - 2 * padding);
        var my = Math.round(padding / 2);
        var subkind = marker.detail && marker.detail.subkind;
        var label = escapeHtml(marker.kind + (subkind ? " (" + subkind + ")" : "") + " at " + marker.ts);
        limitKindsSeen[marker.kind] = true;
        svgParts.push(
          '<circle cx="' +
            mx.toFixed(1) +
            '" cy="' +
            my +
            '" r="3" fill="' +
            (limitMarkerColors[marker.kind] || "var(--muted)") +
            '"><title>' +
            label +
            "</title></circle>"
        );
      });
    }
    svgParts.push("</svg>");

    var wrap = el("div", { html: svgParts.join("") });
    var legend = el("div", { class: "timeline-legend" });
    Object.keys(markerColors).forEach(function (kind) {
      var swatch = el("span", { class: "swatch" });
      swatch.style.background = markerColors[kind];
      legend.appendChild(el("span", null, [swatch, document.createTextNode(kind)]));
    });
    Object.keys(limitMarkerColors).forEach(function (kind) {
      if (!limitKindsSeen[kind]) return;
      var swatch = el("span", { class: "swatch" });
      swatch.style.background = limitMarkerColors[kind];
      legend.appendChild(el("span", null, [swatch, document.createTextNode(kind.replace(/_/g, " "))]));
    });
    wrap.appendChild(legend);
    if (session.truncated) {
      // Finding 11: /api/session/<id> downsamples turn_series above
      // Store.MAX_TURN_SERIES_POINTS -- say so rather than silently
      // showing a thinned-out chart as the complete picture.
      wrap.appendChild(
        el("p", { class: "notes", text: "This session has many turns; the chart above is downsampled (every marked turn is kept)." })
      );
    }
    return wrap;
  }

  // ======================================================================
  // Cache tab (recache section + a quick /api/recache stat strip)
  // ======================================================================

  function renderCache(panel) {
    clear(panel);
    tabHeading(panel, "cache");

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
      renderMappedSections(result.report, "cache", sectionContainer);
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
    container.appendChild(el("h3", { text: "Cache rebuilds by cause (all history)" }));
    container.appendChild(cards);
  }

  // ======================================================================
  // TTL tab -- /api/ttl is itself Section/Table-shaped (docs/api.md), so
  // it is rendered directly with the same generic table renderer used
  // for report.json sections, rather than waiting on the full report.
  // ======================================================================

  function renderTtl(panel) {
    clear(panel);
    tabHeading(panel, "ttl");
    var container = el("div", { id: "ttl-section" });
    panel.appendChild(container);
    loadInto(container, withWindow("/api/ttl"), function (data, target) {
      renderTtlData(data, target);
    });
  }

  // Shared by every report-backed route that returns a single Section
  // directly (ttl, and the v4-wiring-round carry/compaction-sim/
  // model-swap/waste routes below) rather than the full report.json.
  // Defensive: docs/api.md pins this to "the same shape as the CLI's
  // ... section tables" but not byte-exactly to Section (a bare
  // `{tables: [...]}` or a list of Table dicts are both plausible),
  // and the route returns `null` outright when the section is absent
  // from the assembled report -- handle each shape rather than
  // assuming one and rendering nothing on a mismatch.
  function renderReportBackedSection(data, container, idPrefix, emptyNotice) {
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

  function renderTtlData(data, container) {
    renderReportBackedSection(data, container, "ttl", "No TTL simulation data for this window.");
  }

  // ======================================================================
  // Savings tab (v4 wiring round) -- carry, compaction_sim, model_swap
  // and waste each have their own dedicated report-backed route, same
  // as ttl above, fetched directly rather than waiting on the full
  // report.json. The recommendation cards these sections' rules feed
  // stay on the Recommendations tab, same as every other section --
  // this tab is the tables only.
  // ======================================================================

  var SAVINGS_SECTIONS = [
    { url: "/api/carry", id: "savings-carry", empty: "No context-carry data for this window." },
    { url: "/api/compaction-sim", id: "savings-compaction-sim", empty: "No compaction-window sweep data for this window." },
    { url: "/api/model-swap", id: "savings-model-swap", empty: "No model-swap data for this window." },
    { url: "/api/waste", id: "savings-waste", empty: "No wasted-turn data for this window." },
  ];

  function renderSavings(panel) {
    clear(panel);
    tabHeading(panel, "savings");
    SAVINGS_SECTIONS.forEach(function (spec) {
      var container = el("div", { id: spec.id });
      panel.appendChild(container);
      loadInto(container, withWindow(spec.url), function (data, target) {
        renderReportBackedSection(data, target, spec.id, spec.empty);
      });
    });
  }

  // ======================================================================
  // Agents tab (agents/workflows/workstyle sections)
  // ======================================================================

  function renderAgents(panel) {
    clear(panel);
    tabHeading(panel, "agents");
    var container = el("div", { id: "agents-sections" });
    panel.appendChild(container);
    container.appendChild(loadingNode());
    loadReport().then(function (result) {
      clear(container);
      if (result.error) {
        container.appendChild(errorNotice(result.error));
        return;
      }
      renderMappedSections(result.report, "agents", container);
    });
  }

  // ======================================================================
  // Config tab (config/scorecard sections + config-diff + baseline)
  // ======================================================================

  function renderConfig(panel) {
    clear(panel);
    tabHeading(panel, "config");

    var driftContainer = el("div", { id: "config-drift" });
    panel.appendChild(el("h3", { text: "Your settings and how they changed" }));
    panel.appendChild(driftContainer);
    loadInto(driftContainer, withWindow("/api/config-diff?auto_keys=1"), renderConfigDiff);

    var baselineContainer = el("div", { id: "config-baseline" });
    panel.appendChild(el("h3", { text: "Latest baseline" }));
    panel.appendChild(baselineContainer);
    loadInto(baselineContainer, "/api/baseline", renderBaseline);

    var sectionContainer = el("div", { id: "config-sections" });
    panel.appendChild(sectionContainer);
    sectionContainer.appendChild(loadingNode());
    loadReport().then(function (result) {
      clear(sectionContainer);
      if (result.error) {
        sectionContainer.appendChild(errorNotice(result.error));
        return;
      }
      // The config section's own tables came from /api/config-diff above.
      renderMappedSections(result.report, "config", sectionContainer, ["config"]);
    });
  }

  function renderConfigDiff(data, container) {
    if (data && Array.isArray(data.tables)) {
      renderSectionGeneric(container, data, state.currency, "config-diff");
    } else if (Array.isArray(data) && data.length && data[0] && Array.isArray(data[0].tables)) {
      data.forEach(function (section, i) {
        renderSectionGeneric(container, section, state.currency, "config-diff-" + i);
      });
    } else if (Array.isArray(data) && data.length) {
      renderPlacedTables(container, data, state.currency, "config-diff");
    } else {
      container.appendChild(el("p", { class: "notice", text: "No config drift observed across the current snapshot window." }));
    }
  }

  // v0.3: GET /api/baseline now returns {"baseline", "history",
  // "capture_status"} (docs/api.md) rather than a bare list -- the
  // shape-defensive fallbacks docs/ui.md's own "Shape-defensive
  // rendering" note flagged for trimming once api.py landed are gone;
  // this reads that shape directly.
  function renderBaseline(data, container) {
    var status = data && data.capture_status;
    if (status) {
      container.appendChild(el("p", { class: "notice", text: status.summary || "" }));
    }

    var latest = data && data.baseline;
    if (!latest) {
      container.appendChild(el("p", { class: "notice", text: "No baseline captured yet (see `claude-token-lens baseline`)." }));
      return;
    }
    if (status && status.started && !status.complete) {
      container.appendChild(
        el("p", { class: "notice", text: "Capture window open: provisional -- this baseline may change once capture completes." })
      );
    }

    var rows = data.history && data.history.length ? data.history : [latest];
    var table = el("table");
    var head = el("thead", null, [
      el("tr", null, ["Project", "Window start", "Window end", "Archetype", "Captured"].map(function (h) {
        return el("th", { text: h });
      })),
    ]);
    var body = el(
      "tbody",
      null,
      rows.map(function (row) {
        return el("tr", null, [
          // Nit 27: Store.baselines() joins in the owning project's
          // (redacted) slug specifically so this table doesn't have to
          // show the meaningless projects.id primary key -- render that
          // instead of the raw project_id the route used to be the only
          // thing available here.
          el("td", { text: row.project_slug || "-" }),
          el("td", { text: row.window_start || "-" }),
          el("td", { text: row.window_end || "-" }),
          el("td", { text: row.archetype || "-" }),
          el("td", { text: row.created_at || "-" }),
        ]);
      })
    );
    table.appendChild(head);
    table.appendChild(body);
    container.appendChild(table);
  }

  // ======================================================================
  // Profiles tab
  // ======================================================================
  //
  // v0.3: GET /api/profiles now returns {"profiles": [...each tagged
  // source: "catalogue"|"user"...], "suggested_profile_id"} (docs/api.md)
  // rather than a bare list of indexed (user-only) profiles, and GET
  // /api/profiles/<id>/diff is a real computation (profiles/diff.py)
  // rather than a 501 stub -- see that route's own docstring in api.py.

  // Filled from GET /api/profile-schema on first use: every key a
  // profile may set, with its label, type and plain-English text.
  var profileSchemaPromise = null;

  function loadProfileSchema() {
    if (!profileSchemaPromise) {
      profileSchemaPromise = fetchJson("/api/profile-schema").then(function (result) {
        return result.body && result.body.ok === true ? result.body.data : null;
      });
    }
    return profileSchemaPromise;
  }

  var PROFILE_SCOPE_LABELS = {
    user: "Your user settings, every project",
    "project-local": "This project, on your machine only",
    repo: "This project, shared with everyone who works in it",
  };

  function renderProfiles(panel) {
    clear(panel);
    tabHeading(panel, "profiles");

    // -- save what you have now, so you can compare or go back later --
    var saveCurrentRow = el("div", { class: "profile-actions" });
    var saveCurrentBtn = el("button", { type: "button", id: "profiles-save-current", text: "Save my current settings as a profile" });
    var saveCurrentStatus = el("span", { class: "notes", role: "status" });
    saveCurrentRow.appendChild(saveCurrentBtn);
    saveCurrentRow.appendChild(saveCurrentStatus);
    panel.appendChild(saveCurrentRow);
    panel.appendChild(
      el("p", {
        class: "notes",
        text: "This saves a copy in this tool's own profile folder. It never changes your Claude Code settings.",
      })
    );

    var listContainer = el("div", { id: "profiles-list" });
    var detailContainer = el("div", { id: "profiles-diff" });
    var formContainer = el("div", { id: "profiles-save-form" });

    var creatorContainer = el("div", { id: "profiles-create" });
    var impactContainer = el("div", { id: "profiles-impact" });

    panel.appendChild(el("h3", { text: "Create a profile" }));
    panel.appendChild(creatorContainer);
    panel.appendChild(el("h3", { text: "Your profiles and the built-in ones" }));
    panel.appendChild(listContainer);
    panel.appendChild(detailContainer);
    panel.appendChild(el("h3", { text: "Your changes and what they did" }));
    panel.appendChild(impactContainer);
    loadInto(impactContainer, "/api/impact", renderImpact);
    var editorDetails = el("details", { class: "advanced-detail" });
    editorDetails.appendChild(el("summary", { text: "Edit settings directly" }));
    editorDetails.appendChild(formContainer);
    panel.appendChild(editorDetails);

    var editorShown = false;

    function refreshList() {
      loadInto(listContainer, "/api/profiles", function (data, container) {
        renderProfilesList(data, container, detailContainer);
        // Built once, so a save's status line stays on screen.
        if (!editorShown) {
          editorShown = true;
          renderProfileEditor(formContainer, (data && data.profiles) || [], refreshList);
        }
      });
    }

    var replaceBtn = el("button", { type: "button", text: "Replace the saved copy", hidden: true });
    saveCurrentRow.appendChild(replaceBtn);

    function saveCurrent(replace) {
      saveCurrentBtn.disabled = true;
      replaceBtn.hidden = true;
      saveCurrentStatus.textContent = "Saving…";
      fetchJson("/api/profiles/from-current" + (replace ? "?replace=1" : ""), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).then(function (result) {
        saveCurrentBtn.disabled = false;
        var body = result.body;
        if (result.httpStatus === 409 && /already exists/.test((body && body.error && body.error.message) || "")) {
          // Saved before: ask before overwriting that copy.
          saveCurrentStatus.textContent = "You saved your settings before. Replace that copy with today's settings?";
          replaceBtn.hidden = false;
          return;
        }
        if (!body || body.ok !== true) {
          saveCurrentStatus.textContent = (body && body.error && body.error.message) || "Could not save your settings.";
          return;
        }
        var skipped = body.data.skipped_managed || [];
        saveCurrentStatus.textContent =
          'Saved as "' + (body.data.name || body.data.id) + '".' +
          (skipped.length ? " Left out, because your organisation's policy sets them: " + skipped.join(", ") + "." : "");
        refreshList();
      });
    }
    saveCurrentBtn.addEventListener("click", function () {
      saveCurrent(false);
    });
    replaceBtn.addEventListener("click", function () {
      saveCurrent(true);
    });

    renderProfileCreator(creatorContainer, refreshList);
    refreshList();
  }

  function renderProfilesList(data, container, detailContainer) {
    var profiles = (data && data.profiles) || [];
    var suggestedId = data && data.suggested_profile_id;
    if (!profiles.length) {
      container.appendChild(el("p", { class: "notice", text: "No profiles available." }));
      return;
    }
    var cards = el("div", { class: "profile-cards" });
    profiles.forEach(function (profile) {
      var isSuggested = Boolean(suggestedId) && profile.id === suggestedId;
      var card = el("article", { class: "profile-card" + (isSuggested ? " profile-card-suggested" : "") });
      var head = el("div", { class: "profile-card-head" }, [el("h4", { text: profile.name || profile.id })]);
      if (isSuggested) head.appendChild(el("span", { class: "badge badge-suggested", text: "Suggested for you" }));
      card.appendChild(head);

      var meta = [profile.source === "catalogue" ? "Built in" : "Yours"];
      if (profile.for && profile.for.length) meta.push("for " + profile.for.join(", ").replace(/-/g, " "));
      if (profile.updated_at) meta.push("saved " + String(profile.updated_at).slice(0, 10));
      card.appendChild(el("p", { class: "profile-card-meta", text: meta.join(" · ") }));

      var summary = el("p", { class: "profile-card-summary" });
      card.appendChild(summary);
      // "Changes 3 settings: Model, Effort level, ..." from the profile
      // and the schema's labels (catalogue notes are for maintainers).
      Promise.all([fetchJson("/api/profiles/" + encodeURIComponent(profile.id)), loadProfileSchema()]).then(function (results) {
        var body = results[0].body;
        if (!body || body.ok !== true) return;
        var p = body.data;
        var labels = {};
        ((results[1] && results[1].settings) || []).concat((results[1] && results[1].agents) || []).forEach(function (lever) {
          labels[lever.key] = lever.label;
        });
        var names = Object.keys(p.settings || {}).map(function (key) {
          return labels[key] || key;
        });
        Object.keys(p.agents || {}).forEach(function (agent) {
          Object.keys(p.agents[agent]).forEach(function (key) {
            names.push((labels[key] || key) + " (" + agent + ")");
          });
        });
        names = names.concat(Object.keys(p.env || {}));
        var count = p.setting_count || names.length;
        summary.textContent =
          "Changes " + count + (count === 1 ? " setting" : " settings") + (names.length ? ": " + names.join(", ") + "." : ".");
      });

      var button = el("button", { type: "button", text: "Show what it changes" });
      button.addEventListener("click", function () {
        renderProfileDetail(profile, detailContainer);
        detailContainer.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      card.appendChild(button);
      cards.appendChild(card);
    });
    container.appendChild(cards);
    if (suggestedId) {
      container.appendChild(el("p", { class: "notes", text: "Suggested for you: the profile your latest baseline matches best." }));
    }
  }

  function _diffRowValue(value) {
    if (value === null || value === undefined) return "not set";
    if (Array.isArray(value)) return value.length ? value.join(", ") : "(empty list)";
    if (typeof value === "boolean") return value ? "on" : "off";
    return String(value);
  }

  // One table for every key the profile sets: plain label, now, after,
  // and the file it would be written to under the chosen scope.
  function renderDiffRowsTable(rows) {
    var table = el("table", { class: "profile-diff-table" });
    table.appendChild(
      el("thead", null, [
        el(
          "tr",
          null,
          ["Setting", "Now", "After", "Set in"].map(function (h) {
            return el("th", { text: h });
          })
        ),
      ])
    );
    table.appendChild(
      el(
        "tbody",
        null,
        rows.map(function (row) {
          var same = JSON.stringify(row.current_value) === JSON.stringify(row.proposed_value);
          var label = row.label || row.setting || row.key;
          if (row.agent) label += " (" + row.agent + " agent)";
          var setting = el("td", null, [el("span", { text: label })]);
          if (row.description) setting.appendChild(el("div", { class: "cell-hint", text: row.description }));
          var after = el("td", { text: _diffRowValue(row.proposed_value) });
          var where = el("td", { text: row.where || row.target_file || "-" });
          if (row.managed) {
            after.textContent = _diffRowValue(row.current_value);
            where.textContent = "Locked by your organisation's policy; not changed";
          } else if (same) {
            where.textContent = "Already set; no change";
          }
          return el("tr", { class: same || row.managed ? "row-unchanged" : "" }, [
            setting,
            el("td", { text: _diffRowValue(row.current_value) }),
            after,
            where,
          ]);
        })
      )
    );
    return table;
  }

  function copyToClipboard(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
      }
    } catch (err) {
      /* clipboard unavailable (insecure context, permissions) -- silently do nothing */
    }
  }

  function codeBlockWithCopy(text) {
    var wrap = el("div", { class: "code-block" });
    var pre = el("pre", { text: text || "" });
    var button = el("button", { type: "button", class: "copy-button", text: "Copy" });
    button.addEventListener("click", function () {
      copyToClipboard(text || "");
      button.textContent = "Copied";
      setTimeout(function () {
        button.textContent = "Copy";
      }, 1500);
    });
    wrap.appendChild(pre);
    wrap.appendChild(button);
    return wrap;
  }

  function renderProfileDetail(profile, container) {
    clear(container);
    container.appendChild(el("h3", { text: "What " + (profile.name || profile.id) + " changes" }));
    var scopeRow = el("div", { class: "pager" });
    scopeRow.appendChild(el("label", { for: "profile-scope", text: "Apply it to:" }));
    var scopeSelect = el("select", { id: "profile-scope" });
    Object.keys(PROFILE_SCOPE_LABELS).forEach(function (scope) {
      scopeSelect.appendChild(el("option", { value: scope, text: PROFILE_SCOPE_LABELS[scope] }));
    });
    scopeRow.appendChild(scopeSelect);
    container.appendChild(scopeRow);
    var estimate = el("div", { class: "profile-estimate" });
    container.appendChild(estimate);
    renderProfileEstimate(profile, estimate);
    var body = el("div");
    container.appendChild(body);
    function load() {
      loadInto(
        body,
        "/api/profiles/" + encodeURIComponent(profile.id) + "/diff?scope=" + encodeURIComponent(scopeSelect.value),
        renderProfileDiff
      );
    }
    scopeSelect.addEventListener("change", load);
    load();
  }

  function renderProfileDiff(data, container) {
    (data.notes || []).forEach(function (note) {
      container.appendChild(el("p", { class: "notice", text: note }));
    });

    var rows = (data.settings || []).concat(data.agents || [], data.env || []);
    if (rows.length) {
      container.appendChild(renderDiffRowsTable(rows));
    } else {
      container.appendChild(el("p", { class: "notice", text: "This profile sets nothing." }));
    }

    var box = el("div", { class: "fix" });
    box.appendChild(el("h5", { text: "Ask Claude to do it" }));
    box.appendChild(el("p", { class: "notes", text: "Paste this into Claude Code. It shows you the diff before saving anything." }));
    box.appendChild(codeBlockWithCopy(data.prompt));
    box.appendChild(el("h5", { text: "Or run this command" }));
    box.appendChild(
      el("p", {
        class: "notes",
        text: "It shows the change without writing anything. Run it again without --dry-run to make the change; the output tells you how to undo it.",
      })
    );
    box.appendChild(codeBlockWithCopy(data.dry_run_command || data.apply_command));
    box.appendChild(el("h5", { text: "Or try it for one session" }));
    box.appendChild(el("p", { class: "notes", text: "Starts Claude Code with these settings on top of yours. Nothing is written." }));
    box.appendChild(codeBlockWithCopy(data.launch_command));
    container.appendChild(box);

    var raw = el("details", { class: "advanced-detail" });
    raw.appendChild(el("summary", { text: "Show the file changes" }));
    raw.appendChild(el("pre", { text: data.diff || "(no changes against your current settings)" }));
    container.appendChild(raw);
  }

  // -- profile editor: a form built from /api/profile-schema -----------

  function leverInput(lever, idPrefix) {
    var id = idPrefix + lever.key.replace(/[^A-Za-z0-9]/g, "-");
    var input;
    if (lever.kind === "enum" || lever.kind === "bool") {
      input = el("select", { id: id });
      input.appendChild(el("option", { value: "", text: "Leave as it is" }));
      var values = lever.kind === "bool" ? ["true", "false"] : lever.values || [];
      values.forEach(function (v) {
        var text = lever.kind === "bool" ? (v === "true" ? "On" : "Off") : v;
        input.appendChild(el("option", { value: v, text: text }));
      });
    } else if (lever.kind === "int") {
      input = el("input", { type: "number", id: id, placeholder: "Leave as it is" });
      if (lever.min !== null && lever.min !== undefined) input.min = String(lever.min);
      if (lever.max !== null && lever.max !== undefined) input.max = String(lever.max);
    } else {
      input = el("input", {
        type: "text",
        id: id,
        placeholder: lever.kind === "list[str]" ? "Comma-separated; leave empty to keep" : "Leave empty to keep",
      });
    }
    var label = el("label", { class: "lever", for: id }, [el("span", { class: "lever-label", text: lever.label })]);
    var hint = [lever.description, lever.tradeoff].filter(Boolean).join(" ");
    var field = el("div", { class: "lever-field" }, [label, input]);
    if (hint) field.appendChild(el("div", { class: "cell-hint", text: hint }));
    return { lever: lever, input: input, node: field };
  }

  function leverValue(field) {
    var raw = String(field.input.value || "").trim();
    if (!raw) return undefined;
    var kind = field.lever.kind;
    if (kind === "bool") return raw === "true";
    if (kind === "int") return parseInt(raw, 10);
    if (kind === "list[str]") {
      return raw
        .split(",")
        .map(function (s) {
          return s.trim();
        })
        .filter(Boolean);
    }
    return raw;
  }

  function setLeverValue(field, value) {
    if (value === undefined || value === null) field.input.value = "";
    else if (Array.isArray(value)) field.input.value = value.join(", ");
    else field.input.value = String(value);
  }

  function renderProfileEditor(container, profiles, onSaved) {
    clear(container);
    container.appendChild(loadingNode());
    loadProfileSchema().then(function (schema) {
      clear(container);
      if (!schema) {
        container.appendChild(errorNotice({ code: "unavailable", message: "Could not load the list of settings a profile may change." }));
        return;
      }
      buildProfileEditor(container, schema, profiles, onSaved);
    });
  }

  function buildProfileEditor(container, schema, profiles, onSaved) {
    var form = el("form", { class: "profile-form" });
    container.appendChild(
      el("p", {
        class: "notes",
        text: "Pick only the settings you want to change; anything left empty stays as it is. Saving writes a profile file for this tool. Nothing changes in Claude Code until you use the prompt or command it gives you.",
      })
    );

    var startSelect = el("select", { id: "profile-form-start" });
    startSelect.appendChild(el("option", { value: "", text: "An empty profile" }));
    profiles.forEach(function (p) {
      startSelect.appendChild(el("option", { value: p.id, text: p.name || p.id }));
    });
    var idInput = el("input", { type: "text", id: "profile-form-id", required: true, placeholder: "my-profile" });
    var nameInput = el("input", { type: "text", id: "profile-form-name", placeholder: "My profile" });
    form.appendChild(el("div", { class: "lever-field" }, [el("label", { for: "profile-form-start", text: "Start from" }), startSelect]));
    form.appendChild(
      el("div", { class: "lever-field" }, [el("label", { for: "profile-form-id", text: "Short name (lowercase letters, digits and hyphens)" }), idInput])
    );
    form.appendChild(el("div", { class: "lever-field" }, [el("label", { for: "profile-form-name", text: "Display name" }), nameInput]));

    form.appendChild(el("h4", { text: "Settings for every session" }));
    var settingFields = schema.settings.map(function (lever) {
      return leverInput(lever, "profile-setting-");
    });
    var settingsGrid = el("div", { class: "lever-grid" });
    settingFields.forEach(function (f) {
      settingsGrid.appendChild(f.node);
    });
    form.appendChild(settingsGrid);

    form.appendChild(el("h4", { text: "Settings for one agent" }));
    form.appendChild(el("p", { class: "notes", text: "Written to that agent's file. Use the agent's name as it appears on the Agents tab." }));
    var agentBlocks = [];
    var agentsWrap = el("div", { class: "agent-blocks" });
    form.appendChild(agentsWrap);
    var addAgentBtn = el("button", { type: "button", class: "link-button", text: "Add an agent" });
    form.appendChild(addAgentBtn);

    function addAgentBlock(name, values) {
      var index = agentBlocks.length;
      var nameId = "profile-agent-name-" + index;
      var nameField = el("input", { type: "text", id: nameId, placeholder: "e.g. code-reviewer" });
      nameField.value = name || "";
      var block = el("fieldset", { class: "agent-block" }, [el("legend", { text: "Agent" })]);
      block.appendChild(el("div", { class: "lever-field" }, [el("label", { for: nameId, text: "Agent name" }), nameField]));
      var grid = el("div", { class: "lever-grid" });
      var fields = schema.agents.map(function (lever) {
        var f = leverInput(lever, "profile-agent-" + index + "-");
        setLeverValue(f, (values || {})[lever.key]);
        grid.appendChild(f.node);
        return f;
      });
      block.appendChild(grid);
      var removeBtn = el("button", { type: "button", class: "link-button", text: "Remove this agent" });
      var entry = { name: nameField, fields: fields, node: block };
      removeBtn.addEventListener("click", function () {
        agentBlocks.splice(agentBlocks.indexOf(entry), 1);
        agentsWrap.removeChild(block);
      });
      block.appendChild(removeBtn);
      agentBlocks.push(entry);
      agentsWrap.appendChild(block);
    }
    addAgentBtn.addEventListener("click", function () {
      addAgentBlock("", {});
    });

    var jsonBox = el("details", { class: "advanced-detail" });
    jsonBox.appendChild(el("summary", { text: "Edit as JSON instead" }));
    var jsonInput = el("textarea", { id: "profile-form-json", rows: 8 });
    jsonBox.appendChild(
      el("p", { class: "notes", text: "While this is open, saving uses the JSON below and ignores the form. It starts as a copy of the form." })
    );
    jsonBox.appendChild(jsonInput);
    jsonBox.addEventListener("toggle", function () {
      if (jsonBox.open) jsonInput.value = JSON.stringify(collect(), null, 2);
    });
    form.appendChild(jsonBox);

    var errorNode = el("div", { class: "notice error", role: "alert", hidden: true });
    var statusNode = el("p", { class: "notes", role: "status" });
    form.appendChild(el("button", { type: "submit", text: "Save profile" }));
    form.appendChild(errorNode);
    form.appendChild(statusNode);
    container.appendChild(form);

    function collect() {
      var doc = { id: idInput.value.trim(), settings: {}, agents: {} };
      var name = nameInput.value.trim();
      if (name) doc.name = name;
      settingFields.forEach(function (f) {
        var v = leverValue(f);
        if (v !== undefined) doc.settings[f.lever.key] = v;
      });
      agentBlocks.forEach(function (block) {
        var agentName = block.name.value.trim();
        if (!agentName) return;
        var values = {};
        block.fields.forEach(function (f) {
          var v = leverValue(f);
          if (v !== undefined) values[f.lever.key] = v;
        });
        if (Object.keys(values).length) doc.agents[agentName] = values;
      });
      return doc;
    }

    startSelect.addEventListener("change", function () {
      settingFields.forEach(function (f) {
        setLeverValue(f, undefined);
      });
      agentBlocks.slice().forEach(function (block) {
        agentsWrap.removeChild(block.node);
      });
      agentBlocks.length = 0;
      if (!startSelect.value) return;
      fetchJson("/api/profiles/" + encodeURIComponent(startSelect.value)).then(function (result) {
        var body = result.body;
        if (!body || body.ok !== true) return;
        var p = body.data;
        settingFields.forEach(function (f) {
          setLeverValue(f, p.settings[f.lever.key]);
        });
        Object.keys(p.agents || {}).forEach(function (agentName) {
          addAgentBlock(agentName, p.agents[agentName]);
        });
        if (!nameInput.value) nameInput.value = (p.name || p.id) + " (my copy)";
      });
    });

    function showError(message) {
      errorNode.hidden = false;
      errorNode.textContent = message;
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      errorNode.hidden = true;
      errorNode.textContent = "";
      statusNode.textContent = "";

      var doc;
      if (jsonBox.open) {
        try {
          doc = JSON.parse(jsonInput.value);
        } catch (err) {
          showError("The JSON isn't valid: " + (err && err.message ? err.message : String(err)));
          return;
        }
        if (typeof doc !== "object" || doc === null || Array.isArray(doc)) {
          showError("The JSON must be an object, like {\"id\": \"my-profile\", \"settings\": {}}.");
          return;
        }
      } else {
        doc = collect();
      }
      if (!doc.id) {
        showError("Give the profile a short name.");
        return;
      }

      fetchJson("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(doc),
      }).then(function (result) {
        var respBody = result.body;
        if (!respBody || respBody.ok !== true) {
          showError((respBody && respBody.error && respBody.error.message) || "Could not save the profile.");
          return;
        }
        statusNode.textContent = 'Saved "' + (respBody.data.name || respBody.data.id) + '". It is in the list above.';
        if (onSaved) onSaved();
      });
    });
  }

  // ======================================================================
  // Recommendations tab
  // ======================================================================

  var SEVERITY_ORDER = ["action", "advice", "info"];
  var SEVERITY_LABELS = { action: "Do this", advice: "Worth considering", info: "For your information" };

  function renderRecommendations(panel) {
    clear(panel);
    tabHeading(panel, "recommendations");
    var noticeContainer = el("div", { id: "recommendations-notice" });
    var container = el("div", { id: "recommendations-list" });
    panel.appendChild(noticeContainer);
    panel.appendChild(container);
    container.appendChild(loadingNode());

    // v0.3: same "capture window open: provisional" notice the Config
    // tab's baseline panel shows (docs/api.md's /api/baseline
    // capture_status) -- a recommendation built while onboarding's
    // capture window is still running may change once it completes.
    fetchJson("/api/baseline").then(function (result) {
      var status = result.body && result.body.ok === true ? result.body.data.capture_status : null;
      if (status && status.started && !status.complete) {
        clear(noticeContainer);
        noticeContainer.appendChild(
          el("p", { class: "notice", text: "Capture window open: provisional -- recommendations below may change once capture completes." })
        );
      }
    });

    Promise.all([fetchJson(withWindow("/api/recommendations")), loadReport()]).then(function (results) {
      clear(container);
      var recResult = results[0];
      var reportResult = results[1];
      var body = recResult.body;
      if (!body || body.ok !== true) {
        container.appendChild(errorNotice(body && body.error));
        return;
      }
      renderRecommendationCards(body.data, container, reportResult.report || null);
    });
  }

  function renderRecommendationCards(recommendations, container, report) {
    if (!recommendations.length) {
      container.appendChild(el("p", { class: "notice", text: "No recommendations for this window — nothing stood out." }));
      return;
    }
    var bySeverity = {};
    recommendations.forEach(function (rec) {
      (bySeverity[rec.severity] = bySeverity[rec.severity] || []).push(rec);
    });
    var severities = SEVERITY_ORDER.concat(
      Object.keys(bySeverity).filter(function (s) {
        return SEVERITY_ORDER.indexOf(s) === -1;
      })
    );
    severities.forEach(function (severity) {
      var group = bySeverity[severity];
      if (!group || !group.length) return;
      var groupEl = el("div", { class: "rec-group" });
      groupEl.appendChild(el("h3", { text: (SEVERITY_LABELS[severity] || severity) + " (" + group.length + ")" }));
      group.forEach(function (rec) {
        groupEl.appendChild(renderRecommendationCard(rec, report));
      });
      container.appendChild(groupEl);
    });
  }

  // Recommendation.agent_type values that are not agent names.
  var AGENT_LABELS = {
    "top-level": "Your main session",
    unknown: "Subagents with no recorded type",
    "workflow-subagent": "Workflow subagents",
  };

  // Recommendation.scope, in plain words.
  var SCOPE_LABELS = {
    user: "your user settings, every project",
    repo: "this project's settings or agent files",
    managed: "set by your organisation's policy",
  };

  // "section.table" + raw row key -> the table's title and the row's
  // display label, falling back to the raw names.
  function evidenceSource(report, sourceTable, rowKey) {
    var dot = String(sourceTable).indexOf(".");
    var sectionKey = dot === -1 ? sourceTable : sourceTable.slice(0, dot);
    var tableName = dot === -1 ? "" : sourceTable.slice(dot + 1);
    var section = report ? findSection(report, sectionKey) : null;
    var table = section
      ? (section.tables || []).filter(function (t) {
          return t.name === tableName;
        })[0]
      : null;
    var tableLabel = table && table.title ? table.title : sourceTable;
    var rowLabel = table && table.value_labels && table.value_labels[rowKey] ? table.value_labels[rowKey] : rowKey;
    return "from " + tableLabel + ", " + rowLabel;
  }

  // One fixes.build_fix entry: the plain explainer, then the prompt
  // for Claude and (for a plain setting) the dry-run command.
  function fixTitle(fix) {
    if (fix.title) return fix.title;
    // Same rule as render/tables.py's fix_subject.
    if (!fix.key) return "What you're changing";
    var who = fix.agent ? " for " + fix.agent : fix.key === "model" ? " for your main session" : "";
    return "What you're changing: " + fix.key + who;
  }

  function renderFix(fix, collapsed) {
    var box = el(collapsed ? "details" : "div", { class: "fix" });
    if (collapsed) box.appendChild(el("summary", { text: fixTitle(fix) }));
    if (fix.explainer && fix.explainer.length) {
      if (!collapsed) box.appendChild(el("h5", { text: fixTitle(fix) }));
      var list = el("dl", { class: "fix-explainer" });
      fix.explainer.forEach(function (pair) {
        list.appendChild(el("dt", { text: pair[0] }));
        list.appendChild(el("dd", { text: pair[1] }));
      });
      box.appendChild(list);
    }
    box.appendChild(el("h5", { text: "Ask Claude to do it" }));
    box.appendChild(codeBlockWithCopy(fix.prompt));
    if (fix.command) {
      box.appendChild(el("h5", { text: "Or run this command" }));
      box.appendChild(
        el("p", { class: "notes", text: "It shows the change without writing anything. Run it again without --dry-run to make the change; the output tells you how to undo it." })
      );
      if (fix.command_warning) {
        box.appendChild(el("p", { class: "fix-warning", text: fix.command_warning }));
      }
      box.appendChild(codeBlockWithCopy(fix.command));
    }
    return box;
  }

  function renderRecommendationCard(rec, report) {
    var card = el("article", { class: "rec rec-severity-" + rec.severity });
    card.appendChild(el("h4", { text: rec.title }));
    if (rec.agent_type) {
      card.appendChild(el("div", { class: "rec-meta", text: "For: " + (AGENT_LABELS[rec.agent_type] || rec.agent_type) }));
    }
    if (rec.why) card.appendChild(el("p", { class: "rec-why", text: rec.why }));
    card.appendChild(el("p", { text: "What to do: " + rec.action }));
    if (rec.estimated_saving) {
      card.appendChild(el("p", { class: "rec-saving", text: "Estimated saving: " + rec.estimated_saving }));
    }

    var fixes = rec.fixes || [];
    if (rec.scope === "managed") {
      card.appendChild(el("p", { class: "notice", text: "Managed by policy — raise with your administrator." }));
    } else if (rec.lever && !fixes.length) {
      card.appendChild(el("p", { text: "Setting to change: " + rec.lever + " (" + (SCOPE_LABELS[rec.scope] || rec.scope) + ")" }));
    }
    if (rec.scope !== "managed") {
      // Several changes: one collapsed block each, so the card stays short.
      fixes.forEach(function (fix) {
        card.appendChild(renderFix(fix, fixes.length > 1));
      });
    }

    if (rec.evidence && rec.evidence.length) {
      var evidence = el("details", { class: "rec-evidence" });
      evidence.appendChild(el("summary", { text: "Show the numbers behind this" }));
      var list = el("ul", { class: "evidence-list" });
      rec.evidence.forEach(function (tuple) {
        var label = tuple[0], value = tuple[1], sourceTable = tuple[2], rowKey = tuple[3];
        var formatted = report ? formatEvidenceValue(report, value, sourceTable, rowKey, state.currency) : String(value);
        list.appendChild(el("li", { text: label + ": " + formatted + " (" + evidenceSource(report, sourceTable, rowKey) + ")" }));
      });
      evidence.appendChild(list);
      card.appendChild(evidence);
    }
    return card;
  }

  // ======================================================================
  // Usage tab (usage/compactions sections + a raw /api/compactions list)
  // ======================================================================

  function renderUsage(panel) {
    clear(panel);
    tabHeading(panel, "usage");

    var sectionContainer = el("div", { id: "usage-sections" });
    panel.appendChild(sectionContainer);
    sectionContainer.appendChild(loadingNode());
    loadReport().then(function (result) {
      clear(sectionContainer);
      if (result.error) {
        sectionContainer.appendChild(errorNotice(result.error));
        return;
      }
      renderMappedSections(result.report, "usage", sectionContainer);
    });

    var compactionsContainer = el("div", { id: "usage-compactions" });
    panel.appendChild(el("h3", { text: "Recent conversation summaries (compactions)" }));
    panel.appendChild(compactionsContainer);
    loadInto(compactionsContainer, "/api/compactions", renderCompactionsRaw);
  }

  var COMPACTION_COLUMNS = [
    { key: "transcript_id", label: "Transcript", kind: "str" },
    { key: "ts", label: "Time", kind: "str" },
    { key: "pre_tokens", label: "Pre tokens", kind: "tokens" },
    { key: "post_tokens", label: "Post tokens", kind: "tokens" },
    { key: "dropped_tokens", label: "Dropped tokens", kind: "tokens" },
    { key: "trigger", label: "Trigger", kind: "str" },
    { key: "join_delta_s", label: "Join delta", kind: "secs" },
  ];

  function renderCompactionsRaw(rows, container) {
    if (!rows.length) {
      container.appendChild(el("p", { class: "notice", text: "No compactions recorded." }));
      return;
    }
    var table = el("table");
    var head = el(
      "thead",
      null,
      [
        el(
          "tr",
          null,
          COMPACTION_COLUMNS.map(function (col) {
            return el("th", { class: NUMERIC_KINDS[col.kind] ? "num" : null, text: col.label });
          })
        ),
      ]
    );
    var body = el(
      "tbody",
      null,
      rows.slice(0, 50).map(function (row) {
        return el(
          "tr",
          null,
          COMPACTION_COLUMNS.map(function (col) {
            return el("td", { class: NUMERIC_KINDS[col.kind] ? "num" : null, text: formatCell(row[col.key], col.kind, state.currency) });
          })
        );
      })
    );
    table.appendChild(head);
    table.appendChild(body);
    container.appendChild(table);
    if (rows.length > 50) container.appendChild(el("p", { class: "notes", text: "Showing the first 50 of " + rows.length + "." }));
  }

  // ======================================================================
  // Diagnostics tab (phases section + the Diagnostics counters block)
  // ======================================================================

  function renderDiagnosticsTab(panel) {
    clear(panel);
    tabHeading(panel, "diagnostics");
    var setupContainer = el("div", { id: "diagnostics-setup" });
    panel.appendChild(el("h3", { text: "What this tool installed, and what to expect" }));
    panel.appendChild(setupContainer);
    loadInto(setupContainer, "/api/setup", renderSetup);
    var sectionContainer = el("div", { id: "diagnostics-sections" });
    panel.appendChild(sectionContainer);
    sectionContainer.appendChild(loadingNode());
    loadReport().then(function (result) {
      clear(sectionContainer);
      if (result.error) {
        sectionContainer.appendChild(errorNotice(result.error));
        return;
      }
      renderMappedSections(result.report, "diagnostics", sectionContainer);
    });

    // The parse-quality counters, labelled (helptext.diagnostics_table).
    var countersContainer = el("div", { id: "diagnostics-counters" });
    panel.appendChild(countersContainer);
    loadInto(countersContainer, withWindow("/api/diagnostics"), function (table, target) {
      target.appendChild(renderTable(table, "diagnostics-counters-table", state.currency));
    });
  }

  // ======================================================================
  // Shared pieces for the Quick actions, Context files and Profiles
  // additions: a plain table of display strings, and a status badge.
  // ======================================================================

  function simpleTable(columns, rows, caption) {
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

  var CHECK_STATUS = {
    act: { label: "Worth a look", cls: "severity-action" },
    ok: { label: "Nothing to do", cls: "severity-info" },
    no_data: { label: "Not enough data", cls: "severity-info" },
  };

  function statusBadge(status) {
    var info = CHECK_STATUS[status] || { label: status, cls: "severity-info" };
    return el("span", { class: "severity-badge " + info.cls, text: info.label });
  }

  function renderTips(tips, container) {
    if (!tips || !tips.length) return;
    container.appendChild(el("h5", { text: "Habits that help" }));
    container.appendChild(
      el(
        "ul",
        { class: "notes" },
        tips.map(function (tip) {
          return el("li", null, [el("strong", { text: tip.title + ". " }), el("span", { text: tip.text })]);
        })
      )
    );
  }

  function renderFixList(fixes, container) {
    (fixes || []).forEach(function (fix, i) {
      container.appendChild(renderFix(fix, i > 0));
    });
  }

  // ======================================================================
  // Quick actions tab: one question per lever, answered for the window
  // ======================================================================

  function renderQuickActions(panel) {
    clear(panel);
    tabHeading(panel, "quick");
    var list = el("div", { class: "quick-list" });
    panel.appendChild(list);
    loadInto(list, withWindow("/api/quick-actions"), function (data, container) {
      (data.checks || []).forEach(function (check) {
        container.appendChild(renderQuickCard(check));
      });
    });
  }

  function renderQuickCard(check) {
    var card = el("article", { class: "rec quick-card" });
    card.appendChild(el("div", { class: "quick-head" }, [statusBadge(check.status), el("h4", { text: check.question })]));
    card.appendChild(el("p", { class: "notes", text: check.why }));
    card.appendChild(el("p", { class: "quick-summary", text: check.summary }));
    var detail = el("div", { class: "quick-detail" });
    if (check.status !== "no_data") {
      var extras = [];
      if (check.fix_count) extras.push(check.fix_count + (check.fix_count === 1 ? " fix" : " fixes"));
      if (check.tip_count) extras.push(check.tip_count + (check.tip_count === 1 ? " tip" : " tips"));
      var button = el("button", {
        type: "button",
        text: "Show the evidence" + (extras.length === 2 ? ", " + extras.join(" and ") : extras.length ? " and " + extras[0] : ""),
        "aria-expanded": "false",
      });
      var loaded = false;
      button.addEventListener("click", function () {
        var open = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", open ? "false" : "true");
        detail.hidden = open;
        if (!loaded) {
          loaded = true;
          loadInto(detail, withWindow("/api/quick-actions/" + encodeURIComponent(check.id)), renderQuickDetail);
        }
      });
      card.appendChild(button);
    }
    card.appendChild(detail);
    return card;
  }

  function renderQuickDetail(data, container) {
    if (data.table) container.appendChild(simpleTable(data.table.columns, data.table.rows));
    renderTips(data.tips, container);
    if (data.fixes && data.fixes.length) {
      container.appendChild(
        el("p", {
          class: "notes",
          text: "Each fix below is a prompt for Claude, which shows you the diff before saving, and where it applies a command that previews the change with --dry-run. Nothing here changes Claude Code by itself.",
        })
      );
      renderFixList(data.fixes, container);
    }
  }

  // ======================================================================
  // Context files tab: every CLAUDE.md file and every skill Claude Code
  // lists, with how often each is sent and what it costs
  // ======================================================================

  function renderContextFiles(panel) {
    clear(panel);
    tabHeading(panel, "context");
    panel.appendChild(el("h3", { text: "CLAUDE.md files" }));
    panel.appendChild(
      el("p", {
        class: "notes",
        text: "Read from disk when you open this tab and never stored. Sent to your main session at its start and to most subagents each time one starts.",
      })
    );
    var files = el("div", { id: "context-claude-md" });
    var fileDetail = el("div", { id: "context-claude-md-detail" });
    panel.appendChild(files);
    panel.appendChild(fileDetail);
    loadInto(files, withWindow("/api/claude-md"), function (data, container) {
      renderClaudeMdList(data, container, fileDetail);
    });

    panel.appendChild(el("h3", { text: "Skills" }));
    panel.appendChild(
      el("p", {
        class: "notes",
        text: "Claude Code lists every skill's name and description at the start of each session and subagent, used or not. Descriptions are read from your newest transcript and never stored.",
      })
    );
    var skills = el("div", { id: "context-skills" });
    panel.appendChild(skills);
    loadInto(skills, withWindow("/api/skills"), renderSkills);
  }

  function renderClaudeMdList(data, container, detailContainer) {
    var rows = data.files || [];
    if (!rows.length) {
      container.appendChild(el("p", { class: "notice", text: "No CLAUDE.md files found." }));
      return;
    }
    var cards = el("div", { class: "profile-cards" });
    rows.forEach(function (file) {
      var card = el("article", { class: "profile-card" });
      card.appendChild(el("h4", { text: file.path }));
      card.appendChild(el("p", { class: "profile-card-meta", text: file.who }));
      var facts = [thousands(file.tokens) + " tokens"];
      facts.push(file.seen ? "sent to " + file.reach_text : "not seen in this window's sessions");
      if (file.cost_text) facts.push(file.cost_text);
      card.appendChild(el("p", { class: "profile-card-summary", text: facts.join(" · ") }));
      if (file.findings && file.findings.length) {
        card.appendChild(el("ul", { class: "notes" }, file.findings.map(function (f) {
          return el("li", { text: f });
        })));
      }
      var button = el("button", { type: "button", text: "Review" + (file.fix_count ? " (" + file.fix_count + (file.fix_count === 1 ? " fix" : " fixes") + ")" : "") });
      button.addEventListener("click", function () {
        loadInto(detailContainer, withWindow("/api/claude-md/" + encodeURIComponent(file.id)), renderClaudeMdDetail);
        detailContainer.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      card.appendChild(button);
      cards.appendChild(card);
    });
    container.appendChild(cards);
  }

  function renderClaudeMdDetail(data, container) {
    container.appendChild(el("h3", { text: data.path }));
    container.appendChild(
      el("p", { class: "notes", text: thousands(data.tokens) + " tokens" + (data.reach_text ? ", sent to " + data.reach_text : "") + (data.cost_text ? ", " + data.cost_text : "") + "." })
    );
    var sections = data.section_rows || [];
    if (sections.length) {
      container.appendChild(
        simpleTable(
          [{ label: "Section" }, { label: "Line" }, { label: "Tokens" }, { label: "Share" }, { label: "Cost" }, { label: "Only about" }],
          sections.map(function (s) {
            return [
              (s.level > 1 ? "  ".repeat(s.level - 1) : "") + (s.heading || "(before the first heading)"),
              s.line,
              thousands(s.tokens),
              formatCell(s.share * 100, "pct"),
              s.cost_text || "",
              (s.agents || []).join(", "),
            ];
          }),
          "Sections, largest share of the file first in the prompt"
        )
      );
    }
    if (data.duplicates && data.duplicates.length) {
      container.appendChild(el("h5", { text: "Repeated text" }));
      container.appendChild(el("ul", { class: "notes" }, data.duplicates.map(function (d) {
        var where = (d.also_in || []).map(function (o) {
          return o.file + " line " + o.line;
        });
        return el("li", { text: "Line " + d.line + ", about " + d.tokens + " tokens: “" + d.excerpt + "”" + (where.length ? ", also in " + where.join(", ") : "") });
      })));
    }
    if (data.stale && data.stale.length) {
      container.appendChild(el("h5", { text: "References to things that no longer exist" }));
      container.appendChild(el("ul", { class: "notes" }, data.stale.map(function (d) {
        return el("li", { text: "Line " + d.line + ": " + d.reference + " (" + d.kind + ")" });
      })));
    }
    if (data.fixes && data.fixes.length) {
      container.appendChild(el("h4", { text: "What you could change" }));
      renderFixList(data.fixes, container);
    } else {
      container.appendChild(el("p", { class: "notes", text: "Nothing to change in this file." }));
    }
  }

  var SKILL_STATUS = {
    unused: "Never used",
    used: "Used",
    listed: "Listed",
    "not listed": "Not listed",
  };

  function renderSkills(data, container) {
    var rows = data.skills || [];
    if (!rows.length) {
      container.appendChild(el("p", { class: "notice", text: "No skill listing recorded in this window." }));
      return;
    }
    container.appendChild(
      el("p", {
        class: "quick-summary",
        text:
          rows.length + " skills listed, " + thousands(data.listing_tokens) + " tokens at each start" +
          (data.listing_cost_text ? ", " + data.listing_cost_text : "") + ". " +
          (data.unused ? data.unused + " were never used." : "Every listed skill was used."),
      })
    );
    renderFixList(data.fixes, container);
    var filterRow = el("div", { class: "pager" });
    var unusedOnly = el("input", { type: "checkbox", id: "skills-unused-only", checked: Boolean(data.unused) });
    filterRow.appendChild(unusedOnly);
    filterRow.appendChild(el("label", { for: "skills-unused-only", text: "Show only skills Claude never used" }));
    container.appendChild(filterRow);
    var list = el("div", { class: "skill-list" });
    container.appendChild(list);
    function draw() {
      clear(list);
      rows
        .filter(function (row) {
          return !unusedOnly.checked || row.status === "unused";
        })
        .forEach(function (row) {
          var item = el("details", { class: "skill-row" });
          item.appendChild(
            el("summary", null, [
              el("strong", { text: row.name }),
              el("span", { class: "notes", text: " · " + row.source_label + " · " + (SKILL_STATUS[row.status] || row.status) + " · " + row.listing_cost_text }),
            ])
          );
          item.appendChild(el("p", { text: row.description || "(no description in the listing)" }));
          var facts = [thousands(row.listing_tokens) + " tokens in the listing"];
          if (row.listed_text) facts.push("listed to " + row.listed_text);
          if (row.use_text) facts.push(row.use_text);
          if (row.path) facts.push(row.path);
          item.appendChild(el("p", { class: "notes", text: facts.join(" · ") }));
          renderFixList(row.fixes, item);
          list.appendChild(item);
        });
    }
    unusedOnly.addEventListener("change", draw);
    draw();
  }

  // ======================================================================
  // Profiles: create one from a goal, with a live what-if; and what
  // each change you made did
  // ======================================================================

  function postJson(url, body) {
    return fetchJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function candidateValueText(value) {
    return _diffRowValue(value);
  }

  function renderWhatIf(data, container) {
    clear(container);
    if (!data || !data.rows || !data.rows.length) {
      container.appendChild(el("p", { class: "notes", text: "Tick a change to see its estimated effect." }));
      return;
    }
    if (data.total_text) container.appendChild(el("p", { class: "quick-summary", text: "Estimated effect of these changes: " + data.total_text + "." }));
    if (data.total_note) container.appendChild(el("p", { class: "notes", text: data.total_note }));
    if (data.not_estimated) {
      container.appendChild(
        el("p", { class: "notes", text: data.not_estimated + (data.not_estimated === 1 ? " change isn't" : " changes aren't") + " estimated; see each row." })
      );
    }
  }

  function renderProfileCreator(container, onSaved) {
    clear(container);
    var goalsBox = el("div", { class: "profile-cards" });
    var draftBox = el("div", { class: "goal-draft" });
    container.appendChild(el("p", { class: "notes", text: "1. Pick what you want. 2. Tick the changes. 3. Name it and save. Saving writes only this tool's profile folder; you then apply it with the prompt or command it shows." }));
    container.appendChild(goalsBox);
    container.appendChild(draftBox);
    loadInto(goalsBox, "/api/profile-goals", function (data, target) {
      (data.goals || []).forEach(function (goal) {
        var card = el("article", { class: "profile-card goal-card" });
        card.appendChild(el("h4", { text: goal.title }));
        card.appendChild(el("p", { class: "profile-card-summary", text: goal.what }));
        var pick = el("button", { type: "button", text: "Start here" });
        pick.addEventListener("click", function () {
          if (goal.id === "current") {
            var saveBtn = document.getElementById("profiles-save-current");
            if (saveBtn) {
              saveBtn.click();
              saveBtn.scrollIntoView({ behavior: "smooth", block: "center" });
            }
            return;
          }
          loadInto(draftBox, withWindow("/api/profile-goals?goal=" + encodeURIComponent(goal.id)), function (draft, box) {
            renderGoalDraft(draft, box, onSaved);
          });
          draftBox.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        card.appendChild(pick);
        target.appendChild(card);
      });
    });
  }

  function renderGoalDraft(draft, container, onSaved) {
    container.appendChild(el("h4", { text: draft.goal.title }));
    var candidates = draft.candidates || [];
    if (!candidates.length) {
      container.appendChild(el("p", { class: "notice", text: "Nothing to change for this goal " + (draft.period || "in this window") + ": your settings already match what the data supports, or there isn't enough data yet." }));
      return;
    }
    container.appendChild(el("p", { class: "notes", text: "Ticked changes are the ones your data supports. Unticked ones are a trade-off for you to decide." }));
    var total = el("div", { class: "whatif-total", role: "status" });
    var table = el("table", { class: "data-table goal-table" });
    var head = el("tr");
    ["", "Setting", "Now", "After", "Estimated effect", "Why, and the trade-off"].forEach(function (label) {
      head.appendChild(el("th", { scope: "col", text: label }));
    });
    table.appendChild(el("thead", null, [head]));
    var tbody = el("tbody");
    var boxes = [];
    candidates.forEach(function (c, i) {
      var box = el("input", { type: "checkbox", id: "goal-candidate-" + i, checked: Boolean(c.ticked) });
      boxes.push(box);
      var estimate = c.estimate || {};
      var why = el("td", null, [
        el("p", { text: c.evidence }),
        c.tradeoff ? el("p", { class: "notes", text: "Trade-off: " + c.tradeoff }) : null,
        estimate.basis ? el("p", { class: "notes", text: estimate.fidelity_text + " " + estimate.basis }) : null,
      ]);
      tbody.appendChild(
        el("tr", null, [
          el("td", null, [box]),
          el("td", null, [el("label", { for: box.id, text: c.label + (c.agent ? " (" + c.agent + ")" : "") })]),
          el("td", { text: candidateValueText(c.now) }),
          el("td", { text: candidateValueText(c.value) }),
          el("td", { text: estimate.effect_text || "" }),
          why,
        ])
      );
    });
    table.appendChild(tbody);
    container.appendChild(el("div", { class: "table-wrap" }, [table]));
    container.appendChild(total);

    function chosen() {
      var settings = {};
      var agents = {};
      candidates.forEach(function (c, i) {
        if (!boxes[i].checked) return;
        if (c.agent) {
          agents[c.agent] = agents[c.agent] || {};
          agents[c.agent][c.key] = c.value;
        } else {
          settings[c.key] = c.value;
        }
      });
      return { settings: settings, agents: agents };
    }
    var pending = 0;
    function refreshTotal() {
      var ticket = ++pending;
      total.textContent = "Working out the estimate…";
      postJson(withWindow("/api/whatif"), chosen()).then(function (result) {
        if (ticket !== pending) return;
        var body = result.body;
        if (!body || body.ok !== true) {
          clear(total);
          total.appendChild(errorNotice(body && body.error));
          return;
        }
        renderWhatIf(body.data, total);
      });
    }
    boxes.forEach(function (box) {
      box.addEventListener("change", refreshTotal);
    });
    refreshTotal();

    var form = el("div", { class: "profile-actions" });
    var name = el("input", { type: "text", id: "goal-profile-name", value: draft.goal.title });
    form.appendChild(el("label", { for: "goal-profile-name", text: "Name" }));
    form.appendChild(name);
    var save = el("button", { type: "button", text: "Save as a profile" });
    var status = el("span", { class: "notes", role: "status" });
    form.appendChild(save);
    form.appendChild(status);
    container.appendChild(form);
    save.addEventListener("click", function () {
      var picked = chosen();
      if (!Object.keys(picked.settings).length && !Object.keys(picked.agents).length) {
        status.textContent = "Tick at least one change first.";
        return;
      }
      var id = (name.value || draft.goal.id).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || draft.goal.id;
      save.disabled = true;
      status.textContent = "Saving…";
      postJson("/api/profiles", {
        id: id,
        name: name.value || draft.goal.title,
        settings: picked.settings,
        agents: picked.agents,
        notes: "Made from the goal \"" + draft.goal.title + "\" " + (draft.period || "") + ".",
      }).then(function (result) {
        save.disabled = false;
        var body = result.body;
        if (!body || body.ok !== true) {
          status.textContent = (body && body.error && body.error.message) || "Could not save the profile.";
          return;
        }
        status.textContent = "Saved. It's in the list above: pick \"Show what it changes\" for the prompt and the command that apply it.";
        if (onSaved) onSaved(body.data.id);
      });
    });
  }

  function renderProfileEstimate(profile, container) {
    Promise.all([fetchJson("/api/profiles/" + encodeURIComponent(profile.id)), loadProfileSchema()]).then(function (results) {
      var body = results[0].body;
      if (!body || body.ok !== true) return;
      var p = body.data;
      var labels = {};
      ((results[1] && results[1].settings) || []).concat((results[1] && results[1].agents) || []).forEach(function (lever) {
        labels[lever.key] = lever.label;
      });
      postJson(withWindow("/api/whatif"), { settings: p.settings || {}, agents: p.agents || {} }).then(function (res) {
        var data = res.body && res.body.ok === true ? res.body.data : null;
        if (!data || !data.rows.length) return;
        clear(container);
        container.appendChild(el("h4", { text: "Estimated effect" }));
        renderWhatIf(data, container);
        container.appendChild(
          simpleTable(
            [{ label: "Change" }, { label: "Effect" }, { label: "How it was worked out" }],
            data.rows.map(function (row) {
              return [
                (labels[row.key] || row.key) + (row.agent ? " (" + row.agent + ")" : "") + ": " + _diffRowValue(row.value),
                row.effect_text,
                (row.fidelity_text + " " + row.basis).trim(),
              ];
            })
          )
        );
      });
    });
  }

  function renderImpact(data, container) {
    var changes = data.changes || [];
    if (!changes.length) {
      container.appendChild(
        el("p", { class: "notes", text: "No changes recorded yet. After you apply a profile or a fix, or change a setting, this shows the sessions before it against those after it." })
      );
      return;
    }
    container.appendChild(el("p", { class: "notes", text: data.caveat }));
    changes.forEach(function (item) {
      var change = item.change || {};
      var card = el("article", { class: "rec impact-card" });
      card.appendChild(el("h4", { text: change.label + (change.reverted ? " (since undone)" : "") }));
      card.appendChild(el("p", { class: "profile-card-meta", text: String(change.ts || "").replace("T", " ").replace("Z", " UTC") + (change.keys && change.keys.length ? " · " + change.keys.join(", ") : "") }));
      card.appendChild(el("p", { class: "quick-summary", text: item.verdict }));
      if (item.enough) {
        card.appendChild(
          simpleTable(
            [{ label: "Measure" }, { label: "Before" }, { label: "After" }, { label: "Change" }],
            (item.measures || []).map(function (m) {
              return [m.label, m.before, m.after, m.change_pct === null || m.change_pct === undefined ? "" : (m.change_pct > 0 ? "+" : "") + m.change_pct + "%"];
            })
          )
        );
      }
      var unjudged = (item.quality || []).filter(function (group) {
        return !group.judged;
      });
      (item.quality || []).forEach(function (group) {
        if (!group.judged) return;
        card.appendChild(el("p", { class: "quick-summary" }, [el("strong", { text: "Quality, " + group.label + ": " }), el("span", { text: group.verdict })]));
        var box = el("details", { class: "fix" });
        box.appendChild(el("summary", { text: "Every quality signal (" + group.before_runs + " runs before, " + group.after_runs + " after)" }));
        box.appendChild(
          simpleTable(
            [{ label: "Signal" }, { label: "Before" }, { label: "After" }, { label: "Verdict" }],
            (group.signals || []).map(function (s) {
              return [s.label, s.before_text + " (" + s.before_counts + ")", s.after_text + " (" + s.after_counts + ")", s.verdict];
            })
          )
        );
        card.appendChild(box);
      });
      if (unjudged.length) {
        card.appendChild(
          el("p", { class: "notes" }, [
            el("strong", { text: "Quality: " }),
            el("span", {
              text:
                "too few runs yet to judge " +
                unjudged
                  .map(function (group) {
                    return group.label + " (" + group.before_runs + " before, " + group.after_runs + " after)";
                  })
                  .join(", ") +
                ". Each needs at least " + unjudged[0].min_runs + " runs on each side.",
            }),
          ])
        );
      }
      if (change.source === "apply" && change.backup_ts && !change.reverted) {
        card.appendChild(el("p", { class: "notes", text: "To undo it:" }));
        card.appendChild(codeBlockWithCopy("claude-token-lens apply --revert " + change.backup_ts));
      }
      container.appendChild(card);
    });
  }

  // ======================================================================
  // Data quality: what this tool installed, what to expect, and how to
  // take it back out
  // ======================================================================

  function renderSetup(data, container) {
    container.appendChild(el("h4", { text: "What to expect" }));
    container.appendChild(
      el(
        "ul",
        { class: "notes expectations" },
        (data.expectations || []).map(function (item) {
          return el("li", null, [el("strong", { text: item.title + ". " }), el("span", { text: item.text })]);
        })
      )
    );
    container.appendChild(el("h4", { text: "What it installed and changed" }));
    (data.items || []).forEach(function (item) {
      var box = el("details", { class: "fix" });
      box.appendChild(el("summary", { text: item.title + ": " + item.status }));
      var list = el("dl", { class: "fix-explainer" });
      [["Where", item.where], ["What it does", item.what_it_does], ["Tokens", item.token_cost], ["To undo it", item.undo]].forEach(function (pair) {
        list.appendChild(el("dt", { text: pair[0] }));
        list.appendChild(el("dd", { text: pair[1] }));
      });
      box.appendChild(list);
      container.appendChild(box);
    });
    container.appendChild(el("h4", { text: "Remove everything" }));
    container.appendChild(el("p", { class: "notes", text: "Shows what it would remove, undo and delete. Run it again without --dry-run to do it; it asks before each step and backs up settings.json first." }));
    container.appendChild(codeBlockWithCopy(data.uninstall_command));
  }

  // ======================================================================
  // Tab controller (WAI-ARIA tabs pattern: arrow keys move focus + select,
  // Home/End jump to the ends; last-selected tab remembered per docs/ui.md)
  // ======================================================================

  // ======================================================================
  // Glossary tab (same wording as the README's glossary)
  // ======================================================================

  var GLOSSARY = [
    ["Session", "One conversation with Claude Code, from start to exit. Resuming it continues the same session."],
    ["Main session", "The conversation you type into, as opposed to the subagents it starts."],
    ["Subagent", "A separate Claude that your session starts for one task, such as a search or a review. It has its own context and reports back when done."],
    ["Transcript", "The log file Claude Code writes for a session or a subagent run. Everything here is read from these files on your machine."],
    ["Reply", "One response from Claude, including any tool calls it makes. Every reply is billed for the whole context it reads."],
    ["Token", "The unit models read and write, roughly three quarters of a word. Prices are per million tokens."],
    ["Context", "Everything Claude reads on a reply: system prompt, tools, CLAUDE.md files and the conversation so far."],
    ["Startup context", "What Claude reads before your first message, or before a subagent's task: system prompt, tool list, CLAUDE.md files, skills and more."],
    ["Prompt cache", "A copy of the start of the context kept on Anthropic's side, so the next reply can re-read it cheaply instead of paying full price."],
    ["Cache read", "Re-reading context from the prompt cache. About a tenth of the normal input price."],
    ["Cache write", "Putting context into the prompt cache. Costs more than normal input: 1.25 times for a 5-minute lifetime, 2 times for 1 hour."],
    ["Cache rebuild", "Writing context to the cache again because the cached copy expired or something early in the conversation changed."],
    ["Cache lifetime (TTL)", "How long the prompt cache stays warm after a reply: 5 minutes by default, or 1 hour. A pause longer than this means a rebuild."],
    ["Conversation summary", "When the context gets too large, Claude Code replaces the conversation so far with a summary. Also called compaction."],
    ["List price", "Anthropic's published price per token. On a Pro or Max plan you don't pay this; it is shown to compare costs."],
    ["Usage limits", "On a Pro or Max plan, the share of your five-hour and weekly allowance you have used."],
    ["Billing mode", "Whether amounts are shown for a Pro or Max plan (a share of your usage limits when there are enough readings, otherwise a list-price equivalent) or as money for pay-per-token billing."],
    ["Effort level", "How hard Claude thinks before replying. Thinking is billed as output, the most expensive token type."],
    ["Scorecard", "Five areas rated 1 (very poor) to 5 (excellent), each from one number in your data."],
    ["Recommendation", "A change worth making, with what it changes, the trade-off, a prompt you can give Claude and a command you can run."],
    ["Profile", "A named group of settings you can compare with yours, try for one session, or apply."],
    ["Scope", "Where a change is written: your user settings (every project), this project on your machine only, or this project for everyone."],
    ["Managed setting", "A setting your organisation's policy controls. Only your administrator can change it."],
    ["Snapshot", "A record of your Claude Code settings at one moment, taken so changes can be compared over time."],
    ["Window", "The stretch of time the numbers cover, picked at the top of the dashboard: the last hour, today, the last 24 hours, 7, 30 or 90 days, all time, or since your last change. A session counts, in full, when it was last active in the window."],
    ["Change point", "A moment your settings changed: an apply, its undo, or a change the settings snapshot saw. The dashboard compares the sessions before it with those after it."],
    ["Quick action", "One question about a way to spend less, such as whether a cheaper model would do for an agent, answered from your own sessions with the evidence and a fix you can copy."],
    ["What-if estimate", "What a change would have saved over the window, worked out from your own sessions. It is an estimate: cheaper settings can change how Claude works, which the estimate can't see."],
    ["CLAUDE.md", "Instruction files Claude reads at the start of every session, and of most subagents: yours, each project's, and rule files. Every line is paid for on every reply that re-reads it."],
    ["Skill", "A packaged set of instructions Claude can load when a task needs it. Its name and description are listed to Claude at the start of every session, used or not."],
    ["Quality signal", "A sign of whether the work went well, not just what it cost: tool calls that failed, agent runs that didn't finish, your corrections. Compared across models and efforts, and before and after each change you make."],
  ];

  function renderGlossary(panel) {
    clear(panel);
    tabHeading(panel, "glossary");
    var list = el("dl", { class: "glossary" });
    GLOSSARY.forEach(function (pair) {
      list.appendChild(el("dt", { text: pair[0] }));
      list.appendChild(el("dd", { text: pair[1] }));
    });
    panel.appendChild(list);
  }

  var TAB_RENDERERS = {
    overview: renderOverview,
    quick: renderQuickActions,
    sessions: renderSessions,
    cache: renderCache,
    ttl: renderTtl,
    savings: renderSavings,
    agents: renderAgents,
    context: renderContextFiles,
    config: renderConfig,
    profiles: renderProfiles,
    recommendations: renderRecommendations,
    usage: renderUsage,
    diagnostics: renderDiagnosticsTab,
    glossary: renderGlossary,
  };

  var TAB_ORDER = ["overview", "quick", "sessions", "cache", "ttl", "savings", "agents", "context", "config", "profiles", "recommendations", "usage", "diagnostics", "glossary"];

  var renderedTabs = {};

  function activateTab(tabKey, options) {
    options = options || {};
    TAB_ORDER.forEach(function (key) {
      var tabBtn = document.getElementById("tab-" + key);
      var panel = document.getElementById("panel-" + key);
      if (!tabBtn || !panel) return;
      var selected = key === tabKey;
      tabBtn.setAttribute("aria-selected", selected ? "true" : "false");
      tabBtn.tabIndex = selected ? 0 : -1;
      panel.hidden = !selected;
    });
    storageSet("tls:activeTab", tabKey);
    state.activeTab = tabKey;
    if (!renderedTabs[tabKey] || options.force) {
      renderedTabs[tabKey] = true;
      var panel = document.getElementById("panel-" + tabKey);
      var renderer = TAB_RENDERERS[tabKey];
      if (panel && renderer) renderer(panel);
    }
    if (options.focus) {
      var btn = document.getElementById("tab-" + tabKey);
      if (btn) btn.focus();
    }
  }

  // The header's window picker: every tab reads state.window, so a change
  // clears every rendered tab and redraws the one on screen.
  function initWindowPicker() {
    var saved = storageGet("tls:window");
    if (saved === null) {
      var legacy = storageGet("tls:overviewWindow");
      if (legacy !== null) saved = legacy || "all";
    }
    var known = WINDOW_OPTIONS.some(function (opt) {
      return opt.value === saved;
    });
    if (known) state.window = saved;
    var host = document.getElementById("window-picker");
    if (!host) return;
    host.appendChild(el("label", { for: "window-select", text: "Window" }));
    var select = el("select", { id: "window-select" });
    WINDOW_OPTIONS.forEach(function (opt) {
      select.appendChild(el("option", { value: opt.value, text: opt.label }));
    });
    select.value = state.window;
    host.appendChild(select);
    var hint = el("span", { class: "window-hint" });
    host.appendChild(hint);
    function describe() {
      var short = ["1h", "today", "24h", "change"].indexOf(state.window) !== -1;
      hint.textContent = short
        ? "Counts every session active in this window, in full, so a long session that started earlier counts whole."
        : "";
    }
    describe();
    select.addEventListener("change", function () {
      state.window = select.value;
      storageSet("tls:window", select.value);
      describe();
      Object.keys(renderedTabs).forEach(function (key) {
        delete renderedTabs[key];
      });
      activateTab(state.activeTab || "overview", { force: true });
    });
  }

  function initTabs() {
    initWindowPicker();
    var nav = document.getElementById("tabs");
    if (!nav) return;
    var buttons = Array.prototype.slice.call(nav.querySelectorAll(".tab"));
    buttons.forEach(function (button, index) {
      button.addEventListener("click", function () {
        activateTab(button.getAttribute("data-tab"));
      });
      button.addEventListener("keydown", function (event) {
        var targetIndex = null;
        if (event.key === "ArrowRight") targetIndex = (index + 1) % buttons.length;
        else if (event.key === "ArrowLeft") targetIndex = (index - 1 + buttons.length) % buttons.length;
        else if (event.key === "Home") targetIndex = 0;
        else if (event.key === "End") targetIndex = buttons.length - 1;
        if (targetIndex === null) return;
        event.preventDefault();
        var targetKey = buttons[targetIndex].getAttribute("data-tab");
        activateTab(targetKey, { focus: true });
      });
    });

    var saved = storageGet("tls:activeTab");
    var initial = saved && TAB_RENDERERS[saved] ? saved : "overview";
    activateTab(initial);
  }

  document.addEventListener("DOMContentLoaded", initTabs);
})();
