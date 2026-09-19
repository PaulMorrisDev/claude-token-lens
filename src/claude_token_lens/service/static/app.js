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
    if (COLUMN_KINDS.indexOf(kind) === -1) kind = "str";
    switch (kind) {
      case "str":
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
    // by the `window_days` value now (`""` for "All time", matching
    // WINDOW_OPTIONS), so each window gets its own cache entry.
    reportPromises: {},
    currency: "USD",
  };

  function loadReport(windowDays) {
    var key = windowDays || "";
    if (!state.reportPromises[key]) {
      var url = "/api/report.json" + (key ? "?window_days=" + encodeURIComponent(key) : "");
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

  function renderTable(table, tableId, currency) {
    var maxima = tableMaxima(table);
    var wrap = el("div", { class: "table-wrap" });
    wrap.appendChild(el("h3", { text: table.title || table.name }));

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
    renderTableBody(table.rows, table.columns, tbody, currency, maxima);
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

  function renderTableBody(rows, columns, tbody, currency, maxima) {
    clear(tbody);
    rows.forEach(function (row) {
      var tr = el("tr");
      row.forEach(function (value, i) {
        var column = columns[i] || { kind: "str" };
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
    renderTableBody(rows, table.columns, tableEl.querySelector("tbody"), currency, maxima);
  }

  function renderSectionGeneric(container, section, currency, idPrefix) {
    if (!section) return;
    container.appendChild(el("h2", { text: section.title || section.key }));
    (section.tables || []).forEach(function (table, i) {
      container.appendChild(renderTable(table, (idPrefix || section.key) + "-" + table.name + "-" + i, currency));
    });
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
    agents: "agents",
    workflows: "agents",
    workstyle: "agents",
    config: "config",
    scorecard: "config",
    usage: "usage",
    compactions: "usage",
    phases: "diagnostics",
  };

  function renderMappedSections(report, tabKey, container) {
    if (!report || !Array.isArray(report.sections)) return;
    report.sections.forEach(function (section) {
      if (section.key === "overview") return; // handled by the Overview tab directly
      var target = SECTION_TAB_MAP[section.key] || "diagnostics";
      if (target !== tabKey) return;
      renderSectionGeneric(container, section, state.currency, "report");
    });
  }

  // -- diagnostics dataclass block (mirrors render/html.py's
  //    _diagnostics_lines -- not a Section/Table, a plain counters
  //    block) --------------------------------------------------------

  function renderDiagnosticsBlock(container, diagnostics) {
    if (!diagnostics) return;
    container.appendChild(el("h2", { text: "Diagnostics" }));
    var list = el("ul", { class: "diagnostics-list" });
    Object.keys(diagnostics)
      .sort()
      .forEach(function (key) {
        var value = diagnostics[key];
        var display;
        if (value && typeof value === "object" && !Array.isArray(value)) {
          var parts = Object.keys(value).map(function (k) {
            return k + "=" + value[k];
          });
          display = parts.length ? parts.join(", ") : "-";
        } else {
          display = String(value);
        }
        list.appendChild(el("li", { text: key + ": " + display }));
      });
    container.appendChild(list);
  }

  // ======================================================================
  // Overview tab
  // ======================================================================

  var LEVEL_LABELS = { 5: "excellent", 4: "good", 3: "fair", 2: "poor", 1: "very poor" };
  var WINDOW_OPTIONS = [
    { label: "7 days", value: "7" },
    { label: "30 days", value: "30" },
    { label: "90 days", value: "90" },
    { label: "All time", value: "" },
  ];

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

    (dimTable ? dimTable.rows : []).forEach(function (row) {
      // [dimension, level, label, metric, value, threshold]
      var dimension = row[0], level = row[1], label = row[2], metric = row[3], value = row[4], threshold = row[5];
      var tile = el("div", { class: "tile" });
      tile.appendChild(el("div", { class: "tile-dimension", text: String(dimension).replace(/_/g, " ") }));
      tile.appendChild(
        el("div", { class: "tile-level level-" + level }, [
          document.createTextNode(String(level)),
          el("span", { class: "tile-max", text: " / 5" }),
        ])
      );
      tile.appendChild(el("div", { class: "tile-label", text: label + " (" + metric + ": " + formatCell(value, "float") + ")" }));
      if (threshold) tile.appendChild(el("div", { class: "tile-threshold", text: threshold }));
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
      overallTile.appendChild(el("div", { class: "tile-label", text: overallRow[2] || LEVEL_LABELS[overallLevel] || "unmeasured" }));
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

  function renderSummaryCards(summary, container) {
    var cards = el("div", { class: "stat-cards" });
    var items = [
      ["Sessions", thousands(summary.sessions || 0)],
      ["Transcripts", thousands(summary.transcripts || 0)],
      ["Total cost", formatCell(summary.total_cost, "money", state.currency)],
      ["Total tokens", formatCell(summary.total_tokens, "tokens")],
    ];
    items.forEach(function (pair) {
      cards.appendChild(
        el("div", { class: "stat-card" }, [
          el("div", { class: "stat-label", text: pair[0] }),
          el("div", { class: "stat-value", text: pair[1] }),
        ])
      );
    });
    container.appendChild(cards);
  }

  function renderOverviewSummary(container, windowDays) {
    var url = "/api/summary" + (windowDays ? "?window_days=" + encodeURIComponent(windowDays) : "");
    return loadInto(container, url, renderSummaryCards);
  }

  function renderOverview(panel) {
    clear(panel);
    panel.appendChild(el("h2", { text: "Overview" }));

    var windowRow = el("div", { class: "pager" });
    windowRow.appendChild(el("label", { for: "overview-window", text: "Window:" }));
    var select = el("select", { id: "overview-window" });
    WINDOW_OPTIONS.forEach(function (opt) {
      select.appendChild(el("option", { value: opt.value, text: opt.label }));
    });
    var savedWindow = storageGet("tls:overviewWindow");
    if (savedWindow !== null) select.value = savedWindow;
    windowRow.appendChild(select);
    panel.appendChild(windowRow);

    var summaryContainer = el("div", { id: "overview-summary" });
    panel.appendChild(summaryContainer);
    renderOverviewSummary(summaryContainer, select.value);

    var scorecardContainer = el("div", { id: "overview-scorecard" });
    panel.appendChild(el("h3", { text: "Scorecard" }));
    panel.appendChild(scorecardContainer);
    scorecardContainer.appendChild(loadingNode());

    var totalsContainer = el("div", { id: "overview-totals" });
    panel.appendChild(totalsContainer);
    totalsContainer.appendChild(loadingNode());

    function renderOverviewReportSections(windowDays) {
      clear(scorecardContainer);
      clear(totalsContainer);
      scorecardContainer.appendChild(loadingNode());
      totalsContainer.appendChild(loadingNode());
      loadReport(windowDays).then(function (result) {
        clear(scorecardContainer);
        clear(totalsContainer);
        if (result.error) {
          scorecardContainer.appendChild(errorNotice(result.error));
          totalsContainer.appendChild(errorNotice(result.error));
          return;
        }
        var report = result.report;
        renderScorecardTiles(scorecardContainer, findSection(report, "scorecard"));
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

    renderOverviewReportSections(select.value);

    select.addEventListener("change", function () {
      storageSet("tls:overviewWindow", select.value);
      renderOverviewSummary(summaryContainer, select.value);
      renderOverviewReportSections(select.value);
    });

    var healthContainer = el("div", { id: "overview-health" });
    panel.appendChild(el("h3", { text: "Service health" }));
    panel.appendChild(healthContainer);
    loadInto(healthContainer, "/api/health", renderHealth);
  }

  function renderHealth(health, container) {
    var watcher = health.watcher || {};
    var lines = [
      "status: " + (health.status || "unknown"),
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
      "Service " + (health.status || "unknown") + " — last watcher tick: " + (watcher.finished_at || "never") + " — " + (watcher.files_parsed || 0) + " files parsed, " + (watcher.errors || 0) + " errors.";
  }

  // ======================================================================
  // Sessions tab
  // ======================================================================

  var sessionsState = { limit: 50, offset: 0 };

  function renderSessions(panel) {
    clear(panel);
    panel.appendChild(el("h2", { text: "Sessions" }));

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

  function renderSessionsTable(rows, container, detailContainer) {
    if (!rows.length) {
      container.appendChild(el("p", { class: "notice", text: "No sessions in this window." }));
      return;
    }
    var table = el("table", { id: "sessions-list-table" });
    var thead = el("thead");
    var headRow = el("tr");
    SESSION_COLUMNS.forEach(function (col) {
      headRow.appendChild(el("th", { class: NUMERIC_KINDS[col.kind] ? "num" : null, text: col.label }));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    rows.forEach(function (row) {
      var tr = el("tr", { class: "clickable", tabIndex: 0, "data-session-id": row.id });
      SESSION_COLUMNS.forEach(function (col) {
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
      el("li", { text: "Archetype: " + (session.archetype || "-") }),
      el("li", { text: "Span: " + formatCell(session.span_s, "secs") }),
      el("li", { text: "Cost: " + formatCell(session.total_cost, "money", state.currency) }),
      el("li", { text: "Tokens: " + formatCell(session.total_tokens, "tokens") }),
      el("li", { text: "Billing mode: " + (session.billing_mode || "-") }),
      el("li", { text: "Profile: " + (session.profile_id || "none") }),
    ]);
    wrap.appendChild(summaryList);

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
    panel.appendChild(el("h2", { text: "Cache" }));

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

  function renderRecacheQuickStats(data, container) {
    var bySignature = data.by_signature || {};
    var cards = el("div", { class: "stat-cards" });
    ["full-expiry", "prefix-invalidated"].forEach(function (sig) {
      var entry = bySignature[sig] || { turns: 0, cache_creation_tokens: 0 };
      cards.appendChild(
        el("div", { class: "stat-card" }, [
          el("div", { class: "stat-label", text: sig }),
          el("div", { class: "stat-value", text: thousands(entry.turns || 0) + " turns" }),
          el("div", { class: "notes", text: formatCell(entry.cache_creation_tokens, "tokens") + " cache-creation tokens" }),
        ])
      );
    });
    container.appendChild(el("h3", { text: "Re-cache quick stats (corpus-wide)" }));
    container.appendChild(cards);
  }

  // ======================================================================
  // TTL tab -- /api/ttl is itself Section/Table-shaped (docs/api.md), so
  // it is rendered directly with the same generic table renderer used
  // for report.json sections, rather than waiting on the full report.
  // ======================================================================

  function renderTtl(panel) {
    clear(panel);
    panel.appendChild(el("h2", { text: "TTL" }));
    var container = el("div", { id: "ttl-section" });
    panel.appendChild(container);
    loadInto(container, "/api/ttl", function (data, target) {
      renderTtlData(data, target);
    });
  }

  function renderTtlData(data, container) {
    // Defensive: docs/api.md pins this to "the same shape as the CLI's
    // ttl section tables" but not byte-exactly to Section (a bare
    // `{tables: [...]}` or a list of Table dicts are both plausible
    // until service/api.py lands) -- handle each shape rather than
    // assuming one and rendering nothing on a mismatch.
    if (data && Array.isArray(data.tables)) {
      renderSectionGeneric(container, data, state.currency, "ttl");
    } else if (Array.isArray(data)) {
      data.forEach(function (table, i) {
        container.appendChild(renderTable(table, "ttl-" + i, state.currency));
      });
    } else {
      container.appendChild(el("p", { class: "notice", text: "No TTL simulation data for this window." }));
    }
  }

  // ======================================================================
  // Agents tab (agents/workflows/workstyle sections)
  // ======================================================================

  function renderAgents(panel) {
    clear(panel);
    panel.appendChild(el("h2", { text: "Agents" }));
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
    panel.appendChild(el("h2", { text: "Config" }));

    var driftContainer = el("div", { id: "config-drift" });
    panel.appendChild(el("h3", { text: "Config drift (managed keys)" }));
    panel.appendChild(driftContainer);
    loadInto(driftContainer, "/api/config-diff?auto_keys=1", renderConfigDiff);

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
      renderMappedSections(result.report, "config", sectionContainer);
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
      data.forEach(function (table, i) {
        container.appendChild(renderTable(table, "config-diff-" + i, state.currency));
      });
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

  function renderProfiles(panel) {
    clear(panel);
    panel.appendChild(el("h2", { text: "Profiles" }));

    var listContainer = el("div", { id: "profiles-list" });
    var diffContainer = el("div", { id: "profiles-diff" });
    var formContainer = el("div", { id: "profiles-save-form" });

    panel.appendChild(listContainer);
    panel.appendChild(diffContainer);
    panel.appendChild(el("h3", { text: "Save as a new user profile" }));
    panel.appendChild(formContainer);

    function refreshList() {
      loadInto(listContainer, "/api/profiles", function (data, container) {
        renderProfilesList(data, container, diffContainer);
      });
    }

    refreshList();
    renderSaveProfileForm(formContainer, refreshList);
  }

  function renderProfilesList(data, container, diffContainer) {
    var profiles = (data && data.profiles) || [];
    var suggestedId = data && data.suggested_profile_id;
    if (!profiles.length) {
      container.appendChild(el("p", { class: "notice", text: "No profiles available." }));
      return;
    }
    var list = el("ul", { class: "profile-list" });
    profiles.forEach(function (profile) {
      var isSuggested = Boolean(suggestedId) && profile.id === suggestedId;
      var button = el("button", { type: "button", text: profile.name || profile.id });
      button.addEventListener("click", function () {
        loadInto(diffContainer, "/api/profiles/" + encodeURIComponent(profile.id) + "/diff", renderProfileDiff);
      });

      var metaParts = ["source: " + profile.source];
      if (profile.archetype) metaParts.push("archetype: " + profile.archetype);
      if (profile.updated_at) metaParts.push("updated " + profile.updated_at);

      var children = [button, el("span", { class: "notes", text: " — " + metaParts.join(", ") })];
      if (isSuggested) {
        children.push(el("span", { class: "profile-suggested", text: " (suggested by your latest baseline)" }));
      }
      list.appendChild(el("li", null, children));
    });
    container.appendChild(list);
  }

  function _diffRowValue(value) {
    if (value === null || value === undefined) return "(unset)";
    if (Array.isArray(value)) return "[" + value.join(", ") + "]";
    return String(value);
  }

  function renderDiffRowsTable(rows) {
    var table = el("table");
    var head = el(
      "thead",
      null,
      [
        el(
          "tr",
          null,
          ["Key", "Current", "Proposed", "Where", "Managed"].map(function (h) {
            return el("th", { text: h });
          })
        ),
      ]
    );
    var body = el(
      "tbody",
      null,
      rows.map(function (row) {
        return el("tr", null, [
          el("td", { text: row.key }),
          el("td", { text: _diffRowValue(row.current_value) }),
          el("td", { text: _diffRowValue(row.proposed_value) }),
          el("td", { text: row.target_file || "-" }),
          el("td", { text: row.managed ? "managed by policy" : "-" }),
        ]);
      })
    );
    table.appendChild(head);
    table.appendChild(body);
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

  function renderProfileDiff(data, container) {
    clear(container);
    container.appendChild(el("h3", { text: "Diff: " + data.profile_id + " (scope: " + data.scope + ")" }));

    (data.notes || []).forEach(function (note) {
      container.appendChild(el("p", { class: "notice", text: note }));
    });

    if (data.settings && data.settings.length) {
      container.appendChild(el("h4", { text: "Settings" }));
      container.appendChild(renderDiffRowsTable(data.settings));
    }
    if (data.agents && data.agents.length) {
      container.appendChild(el("h4", { text: "Agents" }));
      container.appendChild(renderDiffRowsTable(data.agents));
    }
    if (data.env && data.env.length) {
      container.appendChild(el("h4", { text: "Environment" }));
      container.appendChild(renderDiffRowsTable(data.env));
    }

    container.appendChild(el("h4", { text: "Unified diff" }));
    container.appendChild(el("pre", { text: data.diff || "(no changes against the current effective config)" }));

    container.appendChild(el("h4", { text: "Apply (run yourself — the service never runs it)" }));
    container.appendChild(codeBlockWithCopy(data.apply_command));

    container.appendChild(el("h4", { text: "Launch (one-session overlay, nothing written)" }));
    container.appendChild(codeBlockWithCopy(data.launch_command));
  }

  function renderSaveProfileForm(container, onSaved) {
    clear(container);

    var idInput = el("input", { type: "text", id: "profile-form-id", name: "id", required: true, placeholder: "my-profile" });
    var nameInput = el("input", { type: "text", id: "profile-form-name", name: "name", placeholder: "My profile" });
    var settingsInput = el("textarea", { id: "profile-form-settings", name: "settings", rows: 4 });
    settingsInput.value = "{}";
    var errorNode = el("div", { class: "notice error", role: "alert", hidden: true });
    var statusNode = el("p", { class: "notes" });

    var form = el("form", { class: "profile-form" }, [
      el("label", null, [el("span", { text: "Profile ID (lowercase, digits, hyphens)" }), idInput]),
      el("label", null, [el("span", { text: "Name" }), nameInput]),
      el(
        "label",
        null,
        [el("span", { text: "Settings overlay (JSON object, e.g. {\"promptCacheTtl\": \"1h\"})" }), settingsInput]
      ),
      el("button", { type: "submit", text: "Save as user profile" }),
      errorNode,
      statusNode,
    ]);

    function showError(message) {
      errorNode.hidden = false;
      errorNode.textContent = message;
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      errorNode.hidden = true;
      errorNode.textContent = "";
      statusNode.textContent = "";

      var id = idInput.value.trim();
      if (!id) {
        showError("Profile ID is required.");
        return;
      }

      var settings;
      try {
        settings = settingsInput.value.trim() ? JSON.parse(settingsInput.value) : {};
      } catch (err) {
        showError("Settings must be valid JSON: " + (err && err.message ? err.message : String(err)));
        return;
      }
      if (typeof settings !== "object" || settings === null || Array.isArray(settings)) {
        showError("Settings must be a JSON object.");
        return;
      }

      var body = { id: id, settings: settings };
      var name = nameInput.value.trim();
      if (name) body.name = name;

      fetchJson("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (result) {
        var respBody = result.body;
        if (!respBody || respBody.ok !== true) {
          showError((respBody && respBody.error && respBody.error.message) || "Could not save profile.");
          return;
        }
        statusNode.textContent = 'Saved profile "' + respBody.data.id + '".';
        idInput.value = "";
        nameInput.value = "";
        settingsInput.value = "{}";
        if (onSaved) onSaved();
      });
    });

    container.appendChild(form);
  }

  // ======================================================================
  // Recommendations tab
  // ======================================================================

  var SEVERITY_ORDER = ["action", "advice", "info"];

  function renderRecommendations(panel) {
    clear(panel);
    panel.appendChild(el("h2", { text: "Recommendations" }));
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

    Promise.all([fetchJson("/api/recommendations"), loadReport()]).then(function (results) {
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
      groupEl.appendChild(el("h3", { text: severity + " (" + group.length + ")" }));
      group.forEach(function (rec) {
        groupEl.appendChild(renderRecommendationCard(rec, report));
      });
      container.appendChild(groupEl);
    });
  }

  function renderRecommendationCard(rec, report) {
    var card = el("article", { class: "rec rec-severity-" + rec.severity });
    card.appendChild(el("h4", { text: rec.title }));
    card.appendChild(el("div", { class: "rec-meta", text: "category: " + rec.category + (rec.agent_type ? " · agent type: " + rec.agent_type : "") }));
    card.appendChild(el("p", { text: "Action: " + rec.action }));

    if (rec.scope === "managed") {
      card.appendChild(el("p", { class: "notice", text: "Managed by policy — raise with your administrator." }));
    } else if (rec.lever) {
      card.appendChild(el("p", { text: "Lever (scope: " + rec.scope + "):" }));
      card.appendChild(el("pre", { text: rec.lever }));
      card.appendChild(
        el("p", { class: "notes", text: "Illustrative host command — `apply` ships in v0.3; nothing here is executed by the service:" })
      );
      card.appendChild(el("pre", { text: "claude-token-lens apply " + rec.lever + " --dry-run" }));
    }

    if (rec.evidence && rec.evidence.length) {
      card.appendChild(el("p", { text: "Evidence:" }));
      var list = el("ul", { class: "evidence-list" });
      rec.evidence.forEach(function (tuple) {
        var label = tuple[0], value = tuple[1], sourceTable = tuple[2], rowKey = tuple[3];
        var formatted = report ? formatEvidenceValue(report, value, sourceTable, rowKey, state.currency) : String(value);
        list.appendChild(el("li", { text: label + ": " + formatted + " (table " + sourceTable + ", row " + rowKey + ")" }));
      });
      card.appendChild(list);
    }
    return card;
  }

  // ======================================================================
  // Usage tab (usage/compactions sections + a raw /api/compactions list)
  // ======================================================================

  function renderUsage(panel) {
    clear(panel);
    panel.appendChild(el("h2", { text: "Usage" }));

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
    panel.appendChild(el("h3", { text: "Recent compactions (raw)" }));
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
    var sectionContainer = el("div", { id: "diagnostics-sections" });
    panel.appendChild(sectionContainer);
    sectionContainer.appendChild(loadingNode());
    loadReport().then(function (result) {
      clear(sectionContainer);
      if (result.error) {
        sectionContainer.appendChild(errorNotice(result.error));
        return;
      }
      var report = result.report;
      renderMappedSections(report, "diagnostics", sectionContainer);
      renderDiagnosticsBlock(sectionContainer, report && report.diagnostics);
    });
  }

  // ======================================================================
  // Tab controller (WAI-ARIA tabs pattern: arrow keys move focus + select,
  // Home/End jump to the ends; last-selected tab remembered per docs/ui.md)
  // ======================================================================

  var TAB_RENDERERS = {
    overview: renderOverview,
    sessions: renderSessions,
    cache: renderCache,
    ttl: renderTtl,
    agents: renderAgents,
    config: renderConfig,
    profiles: renderProfiles,
    recommendations: renderRecommendations,
    usage: renderUsage,
    diagnostics: renderDiagnosticsTab,
  };

  var TAB_ORDER = ["overview", "sessions", "cache", "ttl", "agents", "config", "profiles", "recommendations", "usage", "diagnostics"];

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

  function initTabs() {
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
