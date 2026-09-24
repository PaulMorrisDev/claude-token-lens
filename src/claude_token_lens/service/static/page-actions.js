/* claude-token-lens service UI: page-actions.js
 *
 * The Actions page: Recommendations and Checks.
 */

import { clear, el, state } from "./core.js";
import { icon } from "./icons.js";
import { fetchJson, findSection, loadInto, loadReport, withWindow } from "./api.js";
import {
  AGENT_LABELS,
  emptyState,
  errorNotice,
  loadingNode,
  renderFix,
  renderFixList,
  renderTips,
  SCOPE_LABELS,
  statusBadge,
} from "./ui.js";
import { formatEvidenceValue, simpleTable } from "./grid.js";
import { viewIntro } from "./links.js";

// ======================================================================
// Actions, Recommendations
// ======================================================================

export var SEVERITY_ORDER = ["action", "advice", "info"];

export var SEVERITY_LABELS = { action: "Do this", advice: "Worth considering", info: "For your information" };
var SEVERITY_ICONS = { action: "critical", advice: "warning", info: "info" };

// A recommendation's severity as a chip: the icon and the label carry
// it, the tint only repeats them (WCAG 1.4.1).
export function severityChip(severity) {
  var chip = el("span", { class: "severity-badge severity-" + severity });
  chip.appendChild(icon(SEVERITY_ICONS[severity] || "info", { size: 14 }));
  chip.appendChild(el("span", { text: SEVERITY_LABELS[severity] || severity }));
  return chip;
}

export function renderRecommendations(panel) {
  clear(panel);
  viewIntro(panel, "actions/recommendations");
  var noticeContainer = el("div", { id: "recommendations-notice" });
  var container = el("div", { id: "recommendations-list" });
  panel.appendChild(noticeContainer);
  panel.appendChild(container);
  container.appendChild(loadingNode());

  // v0.3: same "capture window open: provisional" notice the Settings
  // baseline panel shows (docs/api.md's /api/baseline
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
    groupEl.appendChild(el("h2", { text: (SEVERITY_LABELS[severity] || severity) + " (" + group.length + ")" }));
    group.forEach(function (rec) {
      groupEl.appendChild(renderRecommendationCard(rec, report));
    });
    container.appendChild(groupEl);
  });
}

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

function renderRecommendationCard(rec, report) {
  var card = el("article", { class: "rec rec-severity-" + rec.severity });
  // The chip sits inside the heading, so a screen reader moving by
  // headings hears the severity ("Do this") before the title.
  card.appendChild(
    el("h3", { class: "rec-head" }, [
      severityChip(rec.severity),
      el("span", { class: "visually-hidden", text: ": " }),
      el("span", { text: rec.title }),
    ])
  );
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
// Actions, Checks: one question per lever, answered for the window
// ======================================================================

export function renderQuickActions(panel) {
  clear(panel);
  viewIntro(panel, "actions/checks");
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
  card.appendChild(el("div", { class: "quick-head" }, [statusBadge(check.status), el("h3", { text: check.question })]));
  card.appendChild(el("p", { class: "notes", text: check.why }));
  if (check.status === "no_data") {
    card.appendChild(emptyState(check.summary));
  } else {
    card.appendChild(el("p", { class: "quick-summary", text: check.summary }));
  }
  var detail = el("div", { class: "quick-detail" });
  if (check.status !== "no_data") {
    var extras = [];
    if (check.fix_count) extras.push(check.fix_count + (check.fix_count === 1 ? " fix" : " fixes"));
    if (check.tip_count) extras.push(check.tip_count + (check.tip_count === 1 ? " tip" : " tips"));
    var what = "the evidence" + (extras.length === 2 ? ", " + extras.join(" and ") : extras.length ? " and " + extras[0] : "");
    var button = el("button", {
      type: "button",
      text: "Show " + what,
      "aria-expanded": "false",
    });
    var loaded = false;
    button.addEventListener("click", function () {
      var open = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", open ? "false" : "true");
      button.textContent = (open ? "Show " : "Hide ") + what;
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
        text: "Each fix below is a prompt for Claude, which shows you the diff before saving. Where it applies, there is also a command that previews the change with --dry-run. Nothing here changes Claude Code by itself.",
      })
    );
    renderFixList(data.fixes, container);
  }
}
