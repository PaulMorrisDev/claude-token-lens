/* claude-token-lens service UI: page-habits.js
 *
 * The Work habits tab.
 */

import { clear, el, escapeHtml, state } from "./core.js";
import { formatCell, money, moneyText } from "./format.js";
import { findSection, loadReport } from "./api.js";
import { codeBlockWithCopy, errorNotice, loadingNode } from "./ui.js";
import { helpBlock, renderPlacedTables } from "./grid.js";
import { tabHeading } from "./links.js";

// ======================================================================
// Work habits tab: the habits section's "This week" digest as cards,
// the playbook as cards with a by-week sparkline and the example to
// copy, brief templates with Copy buttons, then its other tables.
// ======================================================================

export function renderHabits(panel) {
  clear(panel);
  tabHeading(panel, "habits");
  var container = el("div", { id: "habits-sections" });
  panel.appendChild(container);
  container.appendChild(loadingNode());
  loadReport().then(function (result) {
    clear(container);
    if (result.error) {
      container.appendChild(errorNotice(result.error));
      return;
    }
    var section = findSection(result.report, "habits");
    if (!section) {
      container.appendChild(el("p", { class: "notice", text: "No work-habit figures for this window." }));
      return;
    }
    renderHabitsSection(section, container);
  });
}

function tableRowsAsObjects(table) {
  return (table.rows || []).map(function (row) {
    var out = {};
    (table.columns || []).forEach(function (column, i) {
      out[column.key] = row[i];
    });
    return out;
  });
}

function labelFor(table, value) {
  var labels = table.value_labels || {};
  return typeof value === "string" && labels[value] ? labels[value] : value;
}

function renderHabitsSection(section, container) {
  var sectionHelp = helpBlock(section.help);
  if (sectionHelp) container.appendChild(sectionHelp);
  var tables = section.tables || [];
  var byName = {};
  tables.forEach(function (table) {
    byName[table.name] = table;
  });
  if (byName.habits_digest) renderHabitsDigest(byName.habits_digest, container);
  if (byName.habits_playbook) renderHabitsPlaybook(byName.habits_playbook, container);
  if (byName.habits_brief_templates) renderBriefTemplates(byName.habits_brief_templates, container);
  var rest = tables.filter(function (table) {
    return ["habits_digest", "habits_playbook", "habits_brief_templates", "habits_setups"].indexOf(table.name) === -1;
  });
  renderPlacedTables(container, rest, state.currency, "habits");
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

function renderHabitsDigest(table, container) {
  container.appendChild(el("h3", { text: table.title }));
  var help = helpBlock(table.help);
  if (help) container.appendChild(help);
  var rows = tableRowsAsObjects(table);
  if (!rows.length) {
    container.appendChild(el("p", { class: "notice", text: "Nothing to show for this window yet." }));
    return;
  }
  var cards = el("div", { class: "stat-cards habits-digest" });
  rows.forEach(function (row) {
    var kind = (table.row_kinds || {})[row.item] || "str";
    var card = el("div", { class: "stat-card" });
    card.appendChild(el("div", { class: "stat-label", text: labelFor(table, row.item) }));
    // UX-1: a money card follows the billing mode (money() mirrors
    // Units.money); the list-price figure goes underneath when the
    // headline is a share of the weekly limit, and "list-price
    // equivalent" does when there's no share to show.
    var amount = kind === "money" ? money(Number(row.value)) : null;
    var headline = amount ? amount.primary : formatCell(row.value, kind, state.currency);
    var underneath = amount ? amount.secondary : "";
    if (amount && !underneath && / list-price equivalent$/.test(headline)) {
      headline = headline.replace(/ list-price equivalent$/, "");
      underneath = "list-price equivalent";
    }
    card.appendChild(el("div", { class: "stat-value", text: headline }));
    if (underneath) card.appendChild(el("div", { class: "stat-hint", text: underneath }));
    card.appendChild(el("div", { text: row.what || "" }));
    if (row.detail) card.appendChild(el("div", { class: "stat-hint", text: row.detail }));
    cards.appendChild(card);
  });
  container.appendChild(cards);
}

// The playbook's `weeks` column: 0-100 per week, "-" for a week with
// too few messages, drawn as a small bar chart.
function habitSparkline(weeks, label) {
  var values = String(weeks || "").split(" ").filter(function (part) {
    return part !== "";
  });
  if (!values.length) return null;
  var width = 8 * values.length;
  var height = 24;
  var parts = ['<svg viewBox="0 0 ' + width + " " + height + '" class="habit-spark" role="img" aria-label="' + escapeHtml(label) + '">'];
  values.forEach(function (value, i) {
    if (value === "-") {
      parts.push('<rect x="' + (i * 8 + 1) + '" y="' + (height - 1) + '" width="6" height="1" fill="var(--axis-line)"></rect>');
      return;
    }
    var h = Math.max(1, Math.round((Number(value) / 100) * (height - 2)));
    parts.push('<rect x="' + (i * 8 + 1) + '" y="' + (height - h) + '" width="6" height="' + h + '" fill="var(--chart-1)"></rect>');
  });
  parts.push("</svg>");
  var wrap = el("span", { class: "habit-spark-wrap" });
  wrap.innerHTML = parts.join("");
  return wrap;
}

//: UX-4/7: habits shown as cards before the rest collapse into <details>
//: (F3: "uncapped playbook" -- every habit got a card, largest and
//: smallest saving alike, crowding out the ones worth trying first).
var PLAYBOOK_CARD_LIMIT = 5;

function renderHabitsPlaybook(table, container) {
  container.appendChild(el("h3", { text: table.title }));
  var help = helpBlock(table.help);
  if (help) container.appendChild(help);
  var rows = tableRowsAsObjects(table);
  if (!rows.length) {
    container.appendChild(el("p", { class: "notice", text: "No habit stood out in this window." }));
    return;
  }
  var featured = rows.slice(0, PLAYBOOK_CARD_LIMIT);
  var rest = rows.slice(PLAYBOOK_CARD_LIMIT);
  var cards = el("div", { class: "habit-cards" });
  appendHabitCards(table, featured, cards);
  container.appendChild(cards);
  if (rest.length) {
    var more = el("details", { class: "help" });
    more.appendChild(el("summary", { text: rest.length + " more habit" + (rest.length === 1 ? "" : "s") + " worth trying" }));
    var restCards = el("div", { class: "habit-cards" });
    appendHabitCards(table, rest, restCards);
    more.appendChild(restCards);
    container.appendChild(more);
  }
}

function appendHabitCards(table, rows, cards) {
  rows.forEach(function (row) {
    var card = el("article", { class: "habit-card" });
    var head = el("div", { class: "profile-card-head" });
    head.appendChild(el("h4", { text: labelFor(table, row.habit) }));
    head.appendChild(el("span", { class: "badge", text: labelFor(table, row.theme) }));
    card.appendChild(head);
    // UX-1/UX-2: routed through moneyText so a subscription reads "about
    // X% of your weekly usage limit" instead of a bare "$" figure; "a
    // week" is dropped under a subscription since the primary text
    // already says "...weekly usage limit" (finding F3's "weekly ...
    // a week" doubling, mirrored client-side -- see capture_view.py's
    // _roi for the same call).
    // UX-3: a habit apply_covered_by (habits.py) matched to a rule that
    // fired shows no saving of its own -- it would double-count the
    // rule's -- and names the rule instead.
    if (row.covered_by) {
      card.appendChild(el("p", { class: "habit-saving", text: "Already covered by “" + row.covered_by + "” in Recommendations." }));
    } else {
      var savingPeriod = (state.units || {}).mode === "subscription" ? "" : "a week";
      var saving = row.saving === null || row.saving === undefined
        ? "Saving not priced"
        : moneyText(row.saving, { period: savingPeriod, prefix: "About " });
      card.appendChild(el("p", { class: "habit-saving", text: saving }));
    }
    if (row.evidence) card.appendChild(el("p", { text: row.evidence }));
    if (row.example) {
      card.appendChild(el("p", { class: "habit-try", text: "Try:" }));
      card.appendChild(codeBlockWithCopy(row.example));
    }
    var meta = [
      "Seen " + formatCell(row.n, "int", state.currency),
      String(labelFor(table, row.source) || ""),
      "confidence " + String(labelFor(table, row.confidence) || "").toLowerCase(),
      "trend " + String(labelFor(table, row.trend) || "").toLowerCase(),
    ].filter(function (part) {
      return part && part.trim();
    });
    var metaLine = el("p", { class: "profile-card-meta", text: meta.join(" | ") });
    var spark = habitSparkline(row.weeks, "By week, " + labelFor(table, row.trend) + ": " + row.weeks);
    if (spark) metaLine.appendChild(spark);
    card.appendChild(metaLine);
    // UX-3: basis explains a saving figure that isn't shown once covered.
    if (row.basis && !row.covered_by) card.appendChild(el("p", { class: "cell-hint", text: "How the saving is worked out: " + row.basis + "." }));
    // UX-8: same where/trade-off/undo shape as a recommendation's fix
    // explainer (page-actions.js's renderFix), collapsed by default so it doesn't
    // crowd out the habit itself.
    if (row.where || row.trade_off || row.how_to_undo) {
      var explainer = el("details", { class: "help" });
      explainer.appendChild(el("summary", { text: "Where, trade-off and how to undo it" }));
      var list = el("dl", { class: "fix-explainer" });
      [["Where", row.where], ["Trade-off", row.trade_off], ["How to undo it", row.how_to_undo]].forEach(function (pair) {
        if (!pair[1]) return;
        list.appendChild(el("dt", { text: pair[0] }));
        list.appendChild(el("dd", { text: pair[1] }));
      });
      explainer.appendChild(list);
      card.appendChild(explainer);
    }
    cards.appendChild(card);
  });
}

function renderBriefTemplates(table, container) {
  container.appendChild(el("h3", { text: table.title }));
  var help = helpBlock(table.help);
  if (help) container.appendChild(help);
  var cards = el("div", { class: "habit-cards" });
  tableRowsAsObjects(table).forEach(function (row) {
    var card = el("article", { class: "habit-card" });
    card.appendChild(el("h4", { text: labelFor(table, row.task) }));
    if (row.why) card.appendChild(el("p", { class: "profile-card-meta", text: row.why }));
    card.appendChild(codeBlockWithCopy(row.template || ""));
    cards.appendChild(card);
  });
  container.appendChild(cards);
}
