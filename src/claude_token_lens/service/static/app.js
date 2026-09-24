/* claude-token-lens service UI.
 *
 * The entry point: the tab controller and the window picker. The
 * other first-party modules hold everything else, one per concern
 * (core, format, api, ui, grid, links, shell) and one per group of
 * tabs (page-*.js).
 *
 * Vanilla ES2020 in native ES modules, no framework, no build step, no
 * external reference of any kind (see docs/ui.md's Constraints
 * section). index.html loads this file with
 * <script type="module" src="/static/app.js"> under the service's CSP
 * (`script-src 'self'`, so no inline handlers/scripts anywhere).
 *
 * Every route the dashboard talks to is one of docs/api.md's `/api/*`
 * routes, always same-origin, always the `{"ok": true, "data": ...}` /
 * `{"ok": false, "error": {...}}` envelope. `fetchJson` is the one
 * place that envelope is unwrapped; every renderer either gets
 * `data` or a rendered inline error notice -- never a raw stack trace,
 * never a blank panel.
 *
 * No tab holds state the server doesn't already have (docs/ui.md's Data
 * flow section): `localStorage` is used only for purely cosmetic,
 * per-viewer conveniences -- the last-selected tab, the chosen window
 * and a table's last sort column/direction -- never data the server is
 * the source of truth for. Reads/writes are wrapped in try/catch per
 * the artifact browser-storage guidance: a private window or blocked
 * site data must never break rendering.
 */

import { el, renderedTabs, setTabHandler, state, storageGet, storageSet, WINDOW_OPTIONS } from "./core.js";
import { resetFiguresAsOf } from "./api.js";
import { pollHealth } from "./shell.js";
import { renderOverview } from "./page-overview.js";
import { renderQuickActions, renderRecommendations } from "./page-actions.js";
import { renderSavings, renderSessions, renderUsage, sessionsState } from "./page-spend.js";
import { renderCache, renderTtl } from "./page-cache.js";
import { renderAgents, renderContextFiles } from "./page-agents.js";
import { renderHabits } from "./page-habits.js";
import { renderConfig, renderProfiles } from "./page-setup.js";
import { renderCapture } from "./page-capture.js";
import { renderDiagnosticsTab } from "./page-data.js";
import { renderGlossary } from "./page-glossary.js";

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
  habits: renderHabits,
  capture: renderCapture,
  diagnostics: renderDiagnosticsTab,
  glossary: renderGlossary,
};

// ======================================================================
// Tab controller (WAI-ARIA tabs pattern: arrow keys move focus + select,
// Home/End jump to the ends; last-selected tab remembered per docs/ui.md)
// ======================================================================

var TAB_ORDER = ["overview", "quick", "sessions", "cache", "ttl", "savings", "agents", "context", "config", "profiles", "recommendations", "usage", "habits", "capture", "diagnostics", "glossary"];

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
      ? "Counts every session with a reply in this window, in full, so a long session that started earlier counts whole."
      : "";
  }
  describe();
  select.addEventListener("change", function () {
    state.window = select.value;
    storageSet("tls:window", select.value);
    sessionsState.offset = 0;
    describe();
    delete state.reportPromises[state.window];
    resetFiguresAsOf();
    Object.keys(renderedTabs).forEach(function (key) {
      delete renderedTabs[key];
    });
    activateTab(state.activeTab || "overview", { force: true });
  });
}

function initTabs() {
  setTabHandler(activateTab);
  initWindowPicker();
  pollHealth();
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
