/* claude-token-lens service UI: page-spend.js
 *
 * The Spend page: Usage, Savings and Sessions.
 */

import { clear, el, escapeHtml, state } from "./core.js";
import { formatCell, NUMERIC_KINDS, shortTs, thousands } from "./format.js";
import { fetchJson, loadInto, loadReport, postJson, withWindow } from "./api.js";
import { errorNotice, loadingNode } from "./ui.js";
import { renderMappedSections, renderReportBackedSection } from "./grid.js";
import { viewIntro } from "./links.js";

// ======================================================================
// Spend, Sessions
// ======================================================================

export var sessionsState = { limit: 50, offset: 0 };

export function renderSessions(panel) {
  clear(panel);
  viewIntro(panel, "spend/sessions");

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
    renderMappedSections(result.report, "spend/sessions", sectionContainer);
  });

  function load() {
    rangeLabel.textContent = "Rows " + (sessionsState.offset + 1) + "–" + (sessionsState.offset + sessionsState.limit);
    prevBtn.disabled = sessionsState.offset === 0;
    var url = withWindow("/api/sessions?limit=" + sessionsState.limit + "&offset=" + sessionsState.offset);
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

function sessionCellText(value, col) {
  if (col.key === "id") return String(value || "").slice(0, 8);
  if (col.key === "first_ts" || col.key === "last_ts") return shortTs(value);
  return formatCell(value, col.kind, state.currency);
}

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
      tr.appendChild(
        el("td", {
          class: (NUMERIC_KINDS[col.kind] ? "num " : "") + "col-" + col.key,
          text: sessionCellText(value, col),
          // The short session id shows in full on hover.
          title: col.key === "id" ? String(value || "") : null,
        })
      );
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
  container.appendChild(el("div", { class: "table-wrap" }, [table]));
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

// -- your rating (Setup, Capture: dashboard rating) -> POST /api/sessions/<id>/feedback --
function buildSessionRating(container, session) {
  var saved = session.feedback || {};
  var form = el("fieldset", { class: "session-rating" });
  form.appendChild(el("legend", { text: "Rate this session" }));
  form.appendChild(el("p", { class: "notes", text: "The /tl-feedback questions as checkboxes. Kept in Token Lens's own store, so it costs no tokens." }));
  var inputs = {};
  session.feedback_questions.forEach(function (q) {
    var group = el("div", { class: "rating-question", role: "group", "aria-label": q.question });
    group.appendChild(el("p", { class: "rating-label", text: q.question + (q.multi ? " (tick any)" : "") }));
    var chosen = q.multi ? saved[q.key] || [] : saved[q.key] ? [saved[q.key]] : [];
    inputs[q.key] = [];
    q.options.forEach(function (opt) {
      var id = "rate-" + q.key + "-" + opt.word;
      var box = el("input", { type: q.multi ? "checkbox" : "radio", id: id, name: "rate-" + q.key, value: opt.word, checked: chosen.indexOf(opt.word) !== -1 });
      inputs[q.key].push(box);
      group.appendChild(el("span", { class: "rating-option" }, [box, el("label", { for: id, text: opt.label })]));
    });
    form.appendChild(group);
  });
  var save = el("button", { type: "button", text: "Save rating" });
  var reset = el("button", { type: "button", text: "Clear" });
  var status = el("span", { class: "notes", role: "status" });
  if (saved.set_at) status.textContent = "Rated " + saved.set_at.slice(0, 10) + ".";
  form.appendChild(el("div", { class: "rating-actions" }, [save, reset, status]));

  function send(clearAll) {
    var payload = {};
    session.feedback_questions.forEach(function (q) {
      var ticked = clearAll ? [] : inputs[q.key].filter(function (box) {
        return box.checked;
      }).map(function (box) {
        return box.value;
      });
      payload[q.key] = q.multi ? ticked : ticked[0] || null;
    });
    save.disabled = reset.disabled = true;
    status.textContent = "Saving…";
    postJson("/api/sessions/" + encodeURIComponent(session.id) + "/feedback", payload).then(function (res) {
      save.disabled = reset.disabled = false;
      if (!res.body || res.body.ok !== true) {
        status.textContent = "";
        status.appendChild(errorNotice(res.body && res.body.error));
        return;
      }
      renderSessionDetail(container, session.id);
    });
  }
  save.addEventListener("click", function () {
    send(false);
  });
  reset.addEventListener("click", function () {
    send(true);
  });
  return form;
}

function buildSessionDetail(container, session) {
  var wrap = el("div", { class: "session-detail" });
  wrap.appendChild(el("h2", { text: "Session " + session.id }));

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
  wrap.appendChild(el("h2", { text: "Why was this session expensive?" }));
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

  var saveTagsBtn = el("button", { type: "button", text: "Save tags" });
  var tagStatus = el("span", { class: "notes" });
  tagControls.appendChild(modeLabel);
  tagControls.appendChild(purposeLabel);
  tagControls.appendChild(saveTagsBtn);
  tagControls.appendChild(tagStatus);
  wrap.appendChild(tagControls);

  saveTagsBtn.addEventListener("click", function () {
    saveTagsBtn.disabled = true;
    tagStatus.textContent = "Saving…";
    var updates = [];
    if (modeSelect.value) updates.push(["mode", modeSelect.value]);
    if (purposeSelect.value) updates.push(["purpose", purposeSelect.value]);
    if (!updates.length) {
      tagStatus.textContent = "Choose a mode or purpose first.";
      saveTagsBtn.disabled = false;
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
      saveTagsBtn.disabled = false;
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

  if (session.feedback_questions) wrap.appendChild(buildSessionRating(container, session));

  // -- transcripts table (no path -- see docs/api.md's privacy rule) --
  wrap.appendChild(el("h2", { text: "Transcripts" }));
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
  wrap.appendChild(el("h2", { text: "Timeline" }));
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

// UX-6/9: every timeline marker used to be an identical circle,
// distinguished only by fill colour -- color alone (WCAG 1.4.1), so a
// colorblind viewer or a low-color display can't tell recache from
// compaction from spawn, etc. Each kind now also gets its own shape;
// colour stays as a second, redundant cue rather than the only one.
// ``titleText`` is optional (the legend's own tiny icons pass none).
function markerGlyph(shape, cx, cy, r, fill, titleText) {
  var title = titleText ? "<title>" + titleText + "</title>" : "";
  var pts;
  switch (shape) {
    case "square":
      return (
        '<rect x="' + (cx - r * 0.9).toFixed(1) + '" y="' + (cy - r * 0.9).toFixed(1) +
        '" width="' + (r * 1.8).toFixed(1) + '" height="' + (r * 1.8).toFixed(1) +
        '" fill="' + fill + '">' + title + "</rect>"
      );
    case "triangle-up":
    case "triangle-down":
      var flip = shape === "triangle-down" ? -1 : 1;
      pts = [
        [cx, cy - flip * r * 1.3],
        [cx - r * 1.2, cy + flip * r * 0.9],
        [cx + r * 1.2, cy + flip * r * 0.9],
      ];
      return (
        '<polygon points="' +
        pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ") +
        '" fill="' + fill + '">' + title + "</polygon>"
      );
    case "diamond":
      pts = [
        [cx, cy - r * 1.3],
        [cx + r * 1.3, cy],
        [cx, cy + r * 1.3],
        [cx - r * 1.3, cy],
      ];
      return (
        '<polygon points="' +
        pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ") +
        '" fill="' + fill + '">' + title + "</polygon>"
      );
    case "plus":
      return (
        '<rect x="' + (cx - r * 0.35).toFixed(1) + '" y="' + (cy - r * 1.2).toFixed(1) +
        '" width="' + (r * 0.7).toFixed(1) + '" height="' + (r * 2.4).toFixed(1) + '" fill="' + fill + '"></rect>' +
        '<rect x="' + (cx - r * 1.2).toFixed(1) + '" y="' + (cy - r * 0.35).toFixed(1) +
        '" width="' + (r * 2.4).toFixed(1) + '" height="' + (r * 0.7).toFixed(1) + '" fill="' + fill + '">' + title + "</rect>"
      );
    case "x":
      return (
        '<line x1="' + (cx - r * 1.1).toFixed(1) + '" y1="' + (cy - r * 1.1).toFixed(1) +
        '" x2="' + (cx + r * 1.1).toFixed(1) + '" y2="' + (cy + r * 1.1).toFixed(1) +
        '" stroke="' + fill + '" stroke-width="1.6"></line>' +
        '<line x1="' + (cx - r * 1.1).toFixed(1) + '" y1="' + (cy + r * 1.1).toFixed(1) +
        '" x2="' + (cx + r * 1.1).toFixed(1) + '" y2="' + (cy - r * 1.1).toFixed(1) +
        '" stroke="' + fill + '" stroke-width="1.6">' + title + "</line>"
      );
    case "circle":
    default:
      return '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="' + r + '" fill="' + fill + '">' + title + "</circle>";
  }
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
  // Markers are drawn in ink, not chart colours: the shape tells kinds
  // apart, and ink keeps every glyph at 3:1 or better against the panel
  // in both themes (WCAG 1.4.11), where a light yellow or pink would not.
  var markerColors = { recache: "var(--ink-2)", compaction: "var(--ink-2)", spawn: "var(--ink-2)", human: "var(--ink-2)" };
  // UX-6/9: one shape per kind (see markerGlyph above), never reused
  // across the two marker sets below -- 7 kinds, 7 distinct shapes.
  var markerShapes = { recache: "circle", compaction: "square", spawn: "triangle-up", human: "diamond" };
  // v3-limits wiring: drawn in the blank strip above the context-size
  // line (y well below
  // `padding`) rather than pinned to a turn's own point -- a
  // usage-limit event's `ts` falls *inside* the pause gap between two
  // turns, not at a turn_index of its own, so unlike recache/
  // compaction/spawn/human it cannot share the index-based x position
  // those markers use. Positioned instead by interpolating `ts`
  // between the session's own `first_ts`/`last_ts` (docs/limits.md's
  // "Session-timeline marker contract" / docs/ui.md).
  var limitMarkerColors = { limit_hit: "var(--ink-2)", limit_resume: "var(--ink-2)", agent_terminated: "var(--ink-2)" };
  var limitMarkerShapes = { limit_hit: "triangle-down", limit_resume: "plus", agent_terminated: "x" };
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
        '" fill="none" stroke="var(--chart-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline>'
    );
  } else if (points.length === 1) {
    // Finding 11: a single-turn session has exactly one point, and a
    // <polyline> needs at least two to draw anything -- it silently
    // rendered nothing at all. Draw the one point as a dot instead.
    svgParts.push(
      '<circle cx="' + points[0][0].toFixed(1) + '" cy="' + points[0][1].toFixed(1) +
        '" r="4" fill="var(--chart-1)"></circle>'
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
        markerGlyph(markerShapes[kind] || "circle", points[i][0], points[i][1], 3, markerColors[kind] || "var(--ink-3)", label)
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
        markerGlyph(limitMarkerShapes[marker.kind] || "circle", mx, my, 3, limitMarkerColors[marker.kind] || "var(--ink-3)", label)
      );
    });
  }
  svgParts.push("</svg>");

  var wrap = el("div", { html: svgParts.join("") });
  var legend = el("div", { class: "timeline-legend" });
  // UX-6/9: the legend's own swatch mirrors the marker's real shape
  // (not just a colour dot), via the same markerGlyph a viewer just
  // saw drawn on the chart -- so the legend stays a genuine key
  // rather than a second color-only cue.
  function swatchIcon(shape, fill) {
    return el("span", { class: "swatch", html: '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">' + markerGlyph(shape, 7, 7, 3, fill) + "</svg>" });
  }
  Object.keys(markerColors).forEach(function (kind) {
    legend.appendChild(el("span", null, [swatchIcon(markerShapes[kind] || "circle", markerColors[kind]), document.createTextNode(kind)]));
  });
  Object.keys(limitMarkerColors).forEach(function (kind) {
    if (!limitKindsSeen[kind]) return;
    legend.appendChild(
      el("span", null, [swatchIcon(limitMarkerShapes[kind] || "circle", limitMarkerColors[kind]), document.createTextNode(kind.replace(/_/g, " "))])
    );
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
// Spend, Savings (v4 wiring round) -- carry, compaction_sim, model_swap
// and waste each have their own dedicated report-backed route, same
// as ttl in page-cache.js, fetched directly rather than waiting on the full
// report.json. The recommendation cards these sections' rules feed
// stay on Actions, Recommendations, same as every other section --
// this view is the tables only.
// ======================================================================

var SAVINGS_SECTIONS = [
  { url: "/api/carry", id: "savings-carry", empty: "No context-carry data for this window." },
  { url: "/api/compaction-sim", id: "savings-compaction-sim", empty: "No compaction-window sweep data for this window." },
  { url: "/api/model-swap", id: "savings-model-swap", empty: "No model-swap data for this window." },
  { url: "/api/waste", id: "savings-waste", empty: "No wasted-turn data for this window." },
];

export function renderSavings(panel) {
  clear(panel);
  viewIntro(panel, "spend/savings");
  SAVINGS_SECTIONS.forEach(function (spec) {
    var container = el("div", { id: spec.id });
    panel.appendChild(container);
    loadInto(container, withWindow(spec.url), function (data, target) {
      renderReportBackedSection(data, target, spec.id, spec.empty);
    });
  });
}

// ======================================================================
// Spend, Usage (usage/elasticity/compactions/phases sections, cost by
// model + a raw /api/compactions list)
// ======================================================================

export function renderUsage(panel) {
  clear(panel);
  viewIntro(panel, "spend/usage");

  var sectionContainer = el("div", { id: "usage-sections" });
  panel.appendChild(sectionContainer);
  sectionContainer.appendChild(loadingNode());
  loadReport().then(function (result) {
    clear(sectionContainer);
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    renderMappedSections(result.report, "spend/usage", sectionContainer);
  });

  var compactionsContainer = el("div", { id: "usage-compactions" });
  panel.appendChild(el("h2", { text: "Conversation summaries (compactions) in this window" }));
  panel.appendChild(compactionsContainer);
  loadInto(compactionsContainer, withWindow("/api/compactions"), renderCompactionsRaw);
}

var COMPACTION_COLUMNS = [
  { key: "transcript_id", label: "Transcript", kind: "str" },
  { key: "ts", label: "Time", kind: "str" },
  { key: "pre_tokens", label: "Tokens before", kind: "tokens" },
  { key: "post_tokens", label: "Tokens after", kind: "tokens" },
  { key: "dropped_tokens", label: "Tokens dropped", kind: "tokens" },
  { key: "trigger", label: "Trigger", kind: "str" },
  { key: "join_delta_s", label: "Next reply after", kind: "secs" },
];

function compactionCellText(row, col) {
  // transcript_id is the store's own opaque number, not a quantity.
  if (col.key === "transcript_id") return row.transcript_id === null || row.transcript_id === undefined ? "-" : "#" + row.transcript_id;
  if (col.key === "ts") return shortTs(row.ts);
  return formatCell(row[col.key], col.kind, state.currency);
}

function renderCompactionsRaw(rows, container) {
  if (!rows.length) {
    container.appendChild(el("p", { class: "notice", text: "No conversation summaries in this window." }));
    return;
  }
  // The API lists them oldest first; this table shows the newest.
  rows = rows.slice().sort(function (a, b) {
    return String(b.ts || "").localeCompare(String(a.ts || ""));
  });
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
          return el("td", { class: NUMERIC_KINDS[col.kind] ? "num" : null, text: compactionCellText(row, col) });
        })
      );
    })
  );
  table.appendChild(head);
  table.appendChild(body);
  container.appendChild(el("div", { class: "table-wrap" }, [table]));
  if (rows.length > 50) container.appendChild(el("p", { class: "notes", text: "Showing the newest 50 of " + thousands(rows.length) + "." }));
}
