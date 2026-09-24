/* claude-token-lens service UI: page-spend.js
 *
 * The Spend page: Usage, Savings and Sessions.
 */

import { clear, el, escapeHtml } from "./core.js";
import { compactNumber, formatDuration, fullValue, moneyParts, projectName, shortTs, thousands } from "./format.js";
import { fetchJson, loadInto, loadReport, postJson, withWindow } from "./api.js";
import { button, drawer, emptyState, errorNotice, loadingNode, tile, tileRow, toast } from "./ui.js";
import { dataGrid, renderMappedSections, renderReportBackedSection } from "./grid.js";
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
  var prevBtn = button("Previous", { icon: "chevron-left", variant: "quiet" });
  var nextBtn = button("Next", { icon: "chevron-right", variant: "quiet", class: "button-trailing-icon" });
  var rangeLabel = el("span", { class: "pager-range", text: "" });
  pager.appendChild(prevBtn);
  pager.appendChild(rangeLabel);
  pager.appendChild(nextBtn);

  panel.appendChild(pager);
  panel.appendChild(tableContainer);

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
    rangeLabel.textContent = "Sessions " + (sessionsState.offset + 1) + " to " + (sessionsState.offset + sessionsState.limit);
    prevBtn.disabled = sessionsState.offset === 0;
    var url = withWindow("/api/sessions?limit=" + sessionsState.limit + "&offset=" + sessionsState.offset);
    loadInto(
      tableContainer,
      url,
      function (rows, container) {
        renderSessionsTable(rows, container);
        nextBtn.disabled = rows.length < sessionsState.limit;
        if (rows.length < sessionsState.limit) {
          rangeLabel.textContent = rows.length
            ? "Sessions " + (sessionsState.offset + 1) + " to " + (sessionsState.offset + rows.length)
            : "No more sessions";
        }
      },
      { skeleton: "rows" }
    );
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

function timeCell(row, value) {
  return el("span", { class: "nowrap", text: shortTs(value) });
}

var SESSION_COLUMNS = [
  {
    key: "id",
    label: "Session",
    kind: "str",
    // The short id; the whole one on hover.
    render: function (row) {
      return el("span", { class: "mono-id", title: String(row.id || ""), text: String(row.id || "").slice(0, 8) });
    },
  },
  { key: "slug", label: "Project", kind: "str" },
  { key: "first_ts", label: "Started", kind: "str", render: timeCell },
  { key: "last_ts", label: "Last reply", kind: "str", render: timeCell },
  { key: "span_s", label: "Span", kind: "secs" },
  { key: "mode", label: "Mode", kind: "str" },
  { key: "purpose", label: "Purpose", kind: "str" },
  { key: "entrypoint", label: "Started from", kind: "str" },
  { key: "total_cost", label: "Cost", kind: "money" },
  { key: "total_tokens", label: "Tokens", kind: "tokens" },
];

// Shown only when some sessions ran somewhere else, such as WSL.
var SOURCE_COLUMN = { key: "source", label: "Where", kind: "str" };

function renderSessionsTable(rows, container) {
  var columns = SESSION_COLUMNS.slice();
  var elsewhere = rows.some(function (row) {
    return row.source && row.source !== "This computer";
  });
  if (elsewhere) columns.splice(2, 0, SOURCE_COLUMN);
  container.appendChild(
    dataGrid({
      id: "sessions-list-table",
      caption: "Sessions",
      columns: columns,
      rows: rows,
      lead: ["slug", "source", "last_ts", "span_s", "mode", "total_cost", "total_tokens"],
      rowKey: function (row) {
        return row.id;
      },
      rowAction: {
        label: function (row) {
          return "Open session " + String(row.id || "").slice(0, 8);
        },
        run: function (row) {
          openSessionDrawer(row.id);
        },
      },
      empty: "No sessions in this window.",
      emptyNext: "Pick a longer window to see older ones.",
    })
  );
}

// A session's detail slides in from the right, over the list.
function openSessionDrawer(sessionId) {
  drawer({
    title: "Session " + String(sessionId).slice(0, 8),
    wide: true,
    fill: function (body) {
      renderSessionDetail(body, sessionId);
    },
  });
}

function renderSessionDetail(container, sessionId) {
  clear(container);
  container.appendChild(loadingNode("Loading the session", "lines"));
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
  var save = button("Save rating", { variant: "primary" });
  var reset = button("Clear", { variant: "quiet" });
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
        form.appendChild(errorNotice(res.body && res.body.error));
        return;
      }
      toast(clearAll ? "Rating cleared." : "Rating saved.");
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
  wrap.appendChild(el("p", { class: "mono-id session-full-id", text: session.id }));

  var cost = moneyParts(session.total_cost);
  wrap.appendChild(
    tileRow(
      [
        tile({ label: "Cost", value: cost.value, unit: cost.unit, hint: cost.secondary || null }),
        tile({
          label: "Tokens",
          value: el("span", { text: compactNumber(session.total_tokens || 0), title: fullValue(session.total_tokens, "tokens") || null }),
        }),
        tile({ label: "Span", value: formatDuration(session.span_s) }),
      ],
      { class: "metric-tiles-compact" }
    )
  );
  var facts = el("dl", { class: "fact-list" });
  [
    ["Project", el("span", { title: session.slug || "", text: projectName(session.slug) })],
    ["Where it ran", session.source || "This computer"],
    ["Kind of session", session.archetype || "-"],
    ["Billing", session.billing_mode === "subscription" ? "Pro or Max plan" : session.billing_mode === "api" ? "Pay per token (API)" : session.billing_mode || "-"],
    ["Profile", session.profile_id || "None"],
  ].forEach(function (pair) {
    facts.appendChild(el("dt", { text: pair[0] }));
    facts.appendChild(typeof pair[1] === "string" ? el("dd", { text: pair[1] }) : el("dd", null, [pair[1]]));
  });
  wrap.appendChild(facts);

  // -- "why was this session expensive?" (template sentences, no model) --
  var explainBox = el("div", { class: "session-explain" });
  wrap.appendChild(el("h3", { text: "Why did this session cost what it did?" }));
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

  var saveTagsBtn = button("Save tags");
  var tagStatus = el("span", { class: "notes", role: "status" });
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
        tagControls.appendChild(errorNotice(failed[0].body && failed[0].body.error));
      } else {
        toast("Tags saved.");
        renderSessionDetail(container, session.id);
      }
    });
  });

  if (session.feedback_questions) wrap.appendChild(buildSessionRating(container, session));

  // -- timeline: context size over turns with event markers ----------
  wrap.appendChild(el("h3", { text: "Where did the context grow or reset?" }));
  wrap.appendChild(buildSessionTimeline(session));

  // -- transcripts table (no path -- see docs/api.md's privacy rule) --
  wrap.appendChild(el("h3", { text: "Transcripts" }));
  wrap.appendChild(
    dataGrid({
      id: "session-transcripts",
      caption: "Transcripts in this session",
      columns: [
        { key: "kind", label: "Kind", kind: "str" },
        {
          key: "agent_type",
          label: "Agent type",
          kind: "str",
          value: function (t) {
            return t.agent_type || "Main session";
          },
        },
        {
          key: "spawn_depth",
          label: "Spawn depth",
          kind: "int",
          value: function (t) {
            return t.spawn_depth === undefined ? null : t.spawn_depth;
          },
        },
        {
          key: "parent_agent_id",
          label: "Started by",
          kind: "str",
          value: function (t) {
            return t.parent_agent_id || "-";
          },
        },
      ],
      rows: session.transcripts || [],
      empty: "No transcripts recorded for this session.",
    })
  );

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
  container.appendChild(
    dataGrid({
      id: "session-cost-split",
      class: "explain-split",
      caption: "What the cost went on",
      sortable: false,
      bar: 1,
      columns: [
        { key: "label", label: "What the cost went on", kind: "str" },
        { key: "share_pct", label: "Share", kind: "pct" },
      ],
      rows: split,
    })
  );
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
    return emptyState(
      "No turn-by-turn record for this session: Token Lens hasn't stored its main transcript yet.",
      null,
      "It appears once the service has read the session. Open it again in a minute."
    );
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
  { url: "/api/carry", id: "savings-carry", empty: "No tool output to weigh in this window: its sessions kept none worth trimming." },
  { url: "/api/compaction-sim", id: "savings-compaction-sim", empty: "No conversation summaries to replay in this window." },
  { url: "/api/model-swap", id: "savings-model-swap", empty: "No agent in this window could move to a cheaper model." },
  { url: "/api/waste", id: "savings-waste", empty: "No wasted replies in this window." },
];

export function renderSavings(panel) {
  clear(panel);
  viewIntro(panel, "spend/savings");
  SAVINGS_SECTIONS.forEach(function (spec) {
    var container = el("div", { id: spec.id });
    panel.appendChild(container);
    loadInto(
      container,
      withWindow(spec.url),
      function (data, target) {
        renderReportBackedSection(data, target, spec.id, spec.empty, "Pick a longer window to include more sessions.");
      },
      { skeleton: "rows" }
    );
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
  sectionContainer.appendChild(loadingNode("Loading usage", "rows"));
  loadReport().then(function (result) {
    clear(sectionContainer);
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    renderMappedSections(result.report, "spend/usage", sectionContainer);
  });

  var compactions = el("section", { class: "report-section", id: "usage-compactions-section" });
  compactions.appendChild(el("h2", { class: "section-title", text: "Every conversation summary in this window" }));
  compactions.appendChild(
    el("p", {
      class: "section-intro",
      text: "Each time Claude Code summarised a conversation to make room (a compaction), newest first.",
    })
  );
  var compactionsContainer = el("div", { id: "usage-compactions" });
  compactions.appendChild(compactionsContainer);
  panel.appendChild(compactions);
  loadInto(compactionsContainer, withWindow("/api/compactions"), renderCompactionsRaw, { skeleton: "rows" });
}

var COMPACTION_COLUMNS = [
  {
    key: "ts",
    label: "Time",
    kind: "str",
    render: function (row) {
      return el("span", { class: "nowrap", text: shortTs(row.ts) });
    },
  },
  {
    key: "transcript_id",
    label: "Transcript",
    kind: "str",
    // The store's own opaque number, not a quantity.
    render: function (row) {
      return row.transcript_id === null || row.transcript_id === undefined ? "-" : "#" + row.transcript_id;
    },
  },
  { key: "pre_tokens", label: "Tokens before", kind: "tokens" },
  { key: "post_tokens", label: "Tokens after", kind: "tokens" },
  { key: "dropped_tokens", label: "Tokens dropped", kind: "tokens" },
  { key: "trigger", label: "Trigger", kind: "str" },
  { key: "join_delta_s", label: "Next reply after", kind: "secs" },
];

function renderCompactionsRaw(rows, container) {
  // The API lists them oldest first; the grid starts with the newest.
  rows = rows.slice().sort(function (a, b) {
    return String(b.ts || "").localeCompare(String(a.ts || ""));
  });
  if (rows.length) {
    container.appendChild(el("p", { class: "notes", text: thousands(rows.length) + (rows.length === 1 ? " summary." : " summaries.") }));
  }
  container.appendChild(
    dataGrid({
      id: "usage-compactions-grid",
      caption: "Conversation summaries",
      columns: COMPACTION_COLUMNS,
      rows: rows,
      bar: 4,
      empty: "No conversation summaries in this window: no session grew big enough to need one.",
      emptyNext: "Pick a longer window to see older ones.",
    })
  );
}
