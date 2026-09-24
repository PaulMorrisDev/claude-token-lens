/* claude-token-lens service UI: page-data.js
 *
 * The Data quality tab: parse diagnostics and what this tool installed.
 */

import { clear, el, state } from "./core.js";
import { loadInto, loadReport, withWindow } from "./api.js";
import { codeBlockWithCopy, errorNotice, loadingNode } from "./ui.js";
import { renderMappedSections, renderTable } from "./grid.js";
import { tabHeading } from "./links.js";

// ======================================================================
// Diagnostics tab (phases section + the Diagnostics counters block)
// ======================================================================

export function renderDiagnosticsTab(panel) {
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
