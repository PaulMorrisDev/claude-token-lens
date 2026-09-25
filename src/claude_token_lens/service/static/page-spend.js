/* claude-token-lens service UI: page-spend.js
 *
 * The Spend page: Usage, Savings and Sessions.
 */

import { clear, el } from "./core.js";
import { compactNumber, formatDuration, fullValue, moneyParts, projectName, shortTs, thousands } from "./format.js";
import { fetchJson, loadInto, loadReport, postJson, withWindow } from "./api.js";
import { button, drawer, errorNotice, loadingNode, tile, tileRow, toast } from "./ui.js";
import { dataGrid, renderMappedSections, renderReportBackedSection } from "./grid.js";
import { viewIntro } from "./links.js";
import { sessionContextChart } from "./charts-types.js";

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

  // -- chart 5: context size over turns with event markers ------------
  wrap.appendChild(sessionContextChart(session));

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
