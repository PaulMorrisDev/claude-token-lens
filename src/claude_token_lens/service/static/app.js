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
    renderTableBody(table.rows, table.columns, tbody, currency, maxima, table.value_labels);
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
  function renderTableBody(rows, columns, tbody, currency, maxima, valueLabels) {
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
    renderTableBody(rows, table.columns, tableEl.querySelector("tbody"), currency, maxima, table.value_labels);
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
    sessions: "Sessions",
    cache: "Cache",
    ttl: "Cache lifetime (TTL)",
    savings: "Savings",
    agents: "Agents",
    config: "Config",
    profiles: "Profiles",
    recommendations: "Recommendations",
    usage: "Usage",
    diagnostics: "Data quality",
  };

  var TAB_INTROS = {
    overview: "Your totals for the window, and a scorecard of where your tokens go.",
    sessions: "Every session, newest first. Pick one to see its replies on a timeline.",
    cache:
      "When Claude Code had to rebuild the prompt cache, and why. A rebuild writes the whole conversation to the cache again, at the cache-write price.",
    ttl: "How long the prompt cache stays warm, and whether a longer cache lifetime would have paid for itself.",
    savings:
      "Estimates of what you could save: shorter tool output, earlier conversation summaries, cheaper models, and replies that did no useful work.",
    agents: "What your subagents cost, what they are given when they start, and what they hand back.",
    config: "Your Claude Code settings, how they changed, and how much of the context window is used before you type.",
    profiles: "Ready-made groups of settings you can compare with yours.",
    recommendations: "Changes worth making, most important first.",
    usage: "Usage over time, by project, and in five-hour blocks.",
    diagnostics: "How much of your data could be read, and anything the parser had to skip.",
  };

  function tabHeading(panel, tabKey) {
    panel.appendChild(el("h2", { text: TAB_TITLES[tabKey] || tabKey }));
    if (TAB_INTROS[tabKey]) panel.appendChild(el("p", { class: "tab-intro", text: TAB_INTROS[tabKey] }));
  }

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
    tabHeading(panel, "overview");

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

    // Which billing mode the amounts follow, and why (config.toml's
    // billing, or the automatic choice from usage-limit readings).
    var billingLine = el("p", { class: "notes", id: "overview-billing" });
    panel.appendChild(billingLine);

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
        var meta = report.meta || {};
        billingLine.textContent =
          (meta.billing_mode === "subscription"
            ? "Billing: Pro or Max plan. Amounts are list-price equivalents, not what you are charged"
            : "Billing: pay per token (API). Amounts are what the tokens cost at list price") +
          (meta.billing_source ? " (" + meta.billing_source + ")." : ".");
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
    loadInto(container, "/api/ttl", function (data, target) {
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
      loadInto(container, spec.url, function (data, target) {
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

  function renderProfiles(panel) {
    clear(panel);
    tabHeading(panel, "profiles");

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
  function renderFix(fix) {
    var box = el("div", { class: "fix" });
    if (fix.explainer && fix.explainer.length) {
      box.appendChild(el("h5", { text: "What you're changing" + (fix.key ? ": " + fix.key + (fix.agent ? " for " + fix.agent : "") : "") }));
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
    card.appendChild(el("div", { class: "rec-meta", text: "category: " + rec.category + (rec.agent_type ? " · agent type: " + rec.agent_type : "") }));
    if (rec.why) card.appendChild(el("p", { class: "rec-why", text: rec.why }));
    card.appendChild(el("p", { text: "Action: " + rec.action }));
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
      fixes.forEach(function (fix) {
        card.appendChild(renderFix(fix));
      });
    }

    if (rec.evidence && rec.evidence.length) {
      card.appendChild(el("p", { text: "Evidence:" }));
      var list = el("ul", { class: "evidence-list" });
      rec.evidence.forEach(function (tuple) {
        var label = tuple[0], value = tuple[1], sourceTable = tuple[2], rowKey = tuple[3];
        var formatted = report ? formatEvidenceValue(report, value, sourceTable, rowKey, state.currency) : String(value);
        list.appendChild(el("li", { text: label + ": " + formatted + " (" + evidenceSource(report, sourceTable, rowKey) + ")" }));
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
    loadInto(countersContainer, "/api/diagnostics", function (table, target) {
      target.appendChild(renderTable(table, "diagnostics-counters-table", state.currency));
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
    savings: renderSavings,
    agents: renderAgents,
    config: renderConfig,
    profiles: renderProfiles,
    recommendations: renderRecommendations,
    usage: renderUsage,
    diagnostics: renderDiagnosticsTab,
  };

  var TAB_ORDER = ["overview", "sessions", "cache", "ttl", "savings", "agents", "config", "profiles", "recommendations", "usage", "diagnostics"];

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
