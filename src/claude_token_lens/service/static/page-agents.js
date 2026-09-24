/* claude-token-lens service UI: page-agents.js
 *
 * The Agents & context page: Agents, Quality and Context.
 */

import { clear, el } from "./core.js";
import { compactNumber, formatCell, thousands } from "./format.js";
import { loadInto, loadReport, withWindow } from "./api.js";
import { button, drawer, emptyState, errorNotice, loadingNode, renderFixList } from "./ui.js";
import { renderMappedSections, simpleTable } from "./grid.js";
import { viewIntro } from "./links.js";

// ======================================================================
// Agents & context, Agents (agent_startup, agents) and Quality (quality,
// workflows, workstyle): report sections only
// ======================================================================

function renderReportSections(container, viewKey) {
  container.appendChild(loadingNode("Loading the report", "rows"));
  loadReport().then(function (result) {
    clear(container);
    if (result.error) {
      container.appendChild(errorNotice(result.error));
      return;
    }
    renderMappedSections(result.report, viewKey, container);
  });
}

export function renderAgents(panel) {
  clear(panel);
  viewIntro(panel, "agents/subagents");
  var container = el("div", { id: "agents-sections" });
  panel.appendChild(container);
  renderReportSections(container, "agents/subagents");
}

export function renderAgentQuality(panel) {
  clear(panel);
  viewIntro(panel, "agents/quality");
  var container = el("div", { id: "agent-quality-sections" });
  panel.appendChild(container);
  renderReportSections(container, "agents/quality");
}

// ======================================================================
// Agents & context, Context: every CLAUDE.md file and every skill Claude
// Code lists, with how often each is sent and what it costs, then the
// context budget section
// ======================================================================

export function renderContextFiles(panel) {
  clear(panel);
  viewIntro(panel, "agents/context");
  panel.appendChild(el("h2", { text: "CLAUDE.md files" }));
  panel.appendChild(
    el("p", {
      class: "notes",
      text: "Read from disk when you open this page and never stored. Sent to your main session at its start and to most subagents each time one starts.",
    })
  );
  var files = el("div", { id: "context-claude-md" });
  panel.appendChild(files);
  loadInto(files, withWindow("/api/claude-md"), renderClaudeMdList, { skeleton: "tiles" });

  panel.appendChild(el("h2", { text: "Skills" }));
  panel.appendChild(
    el("p", {
      class: "notes",
      text: "Claude Code lists every skill's name and description at the start of each session and subagent, used or not. Descriptions are read from your newest transcript and never stored.",
    })
  );
  var skills = el("div", { id: "context-skills" });
  panel.appendChild(skills);
  loadInto(skills, withWindow("/api/skills"), renderSkills, { skeleton: "rows" });

  var budget = el("div", { id: "context-budget" });
  panel.appendChild(budget);
  renderReportSections(budget, "agents/context");
}

function renderClaudeMdList(data, container) {
  var rows = data.files || [];
  if (!rows.length) {
    container.appendChild(
      emptyState(
        "No CLAUDE.md files found in your projects or your home folder.",
        null,
        "Claude Code reads one at the start of every session once you add it."
      )
    );
    return;
  }
  var cards = el("div", { class: "profile-cards" });
  rows.forEach(function (file) {
    var card = el("article", { class: "profile-card" });
    card.appendChild(el("h3", { text: file.path }));
    card.appendChild(el("p", { class: "profile-card-meta", text: file.who }));
    var facts = [compactNumber(file.tokens) + " tokens"];
    facts.push(file.seen ? "sent to " + file.reach_text : "not seen in this window's sessions");
    if (file.cost_text) facts.push(file.cost_text);
    card.appendChild(el("p", { class: "profile-card-summary", text: facts.join(" · ") }));
    if (file.findings && file.findings.length) {
      card.appendChild(el("ul", { class: "notes" }, file.findings.map(function (f) {
        return el("li", { text: f });
      })));
    }
    card.appendChild(
      button("Review" + (file.fix_count ? " (" + file.fix_count + (file.fix_count === 1 ? " fix" : " fixes") + ")" : ""), {
        action: function () {
          drawer({
            title: file.path,
            wide: true,
            fill: function (body) {
              loadInto(body, withWindow("/api/claude-md/" + encodeURIComponent(file.id)), renderClaudeMdDetail);
            },
          });
        },
      })
    );
    cards.appendChild(card);
  });
  container.appendChild(cards);
}

function renderClaudeMdDetail(data, container) {
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
    container.appendChild(el("h4", { text: "Repeated text" }));
    container.appendChild(el("ul", { class: "notes" }, data.duplicates.map(function (d) {
      var where = (d.also_in || []).map(function (o) {
        return o.file + " line " + o.line;
      });
      return el("li", { text: "Line " + d.line + ", about " + d.tokens + " tokens: “" + d.excerpt + "”" + (where.length ? ", also in " + where.join(", ") : "") });
    })));
  }
  if (data.stale && data.stale.length) {
    container.appendChild(el("h4", { text: "References to things that no longer exist" }));
    container.appendChild(el("ul", { class: "notes" }, data.stale.map(function (d) {
      return el("li", { text: "Line " + d.line + ": " + d.reference + " (" + d.kind + ")" });
    })));
  }
  if (data.fixes && data.fixes.length) {
    container.appendChild(el("h3", { text: "What you could change" }));
    renderFixList(data.fixes, container);
  } else {
    container.appendChild(emptyState("Nothing to change in this file: it has no repeated text, and nothing in it points at something that's gone."));
  }
}

var SKILL_STATUS = {
  unused: "Never used",
  used: "Used",
  listed: "Listed",
  "not listed": "Not listed",
  hidden: "Already hidden",
  "needed by a tool": "Needed by a Claude Code tool",
  "no longer listed": "No longer listed",
};

function renderSkills(data, container) {
  var rows = data.skills || [];
  if (!rows.length) {
    container.appendChild(
      emptyState(
        "No skill listing in this window: none of its sessions listed any skills.",
        null,
        "Pick a longer window to include more sessions."
      )
    );
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
  var filterRow = el("div", { class: "filter-row" });
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
