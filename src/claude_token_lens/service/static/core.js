/* claude-token-lens service UI: core.js
 *
 * Shared DOM helpers, browser storage, the dashboard's state and the
 * tab-navigation hook (showTab) that lets a module link to another tab
 * without importing app.js.
 */

// -- tiny DOM helpers ------------------------------------------------

export function el(tag, attrs, children) {
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
        // Only ever used with strings the calling module built itself from
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

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Used only where a value must go through innerHTML (the inline-SVG
// timeline in page-spend.js) rather than textContent/setAttribute.
export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function storageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (err) {
    return null;
  }
}

export function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (err) {
    /* private window, blocked site data, or a full quota -- ignore */
  }
}

// -- report.json cache (shared across Overview/Cache/TTL/Agents/
//    Config/Usage/Diagnostics/Recommendations) ------------------------

export var state = {
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
  // by the header picker's window now (WINDOW_OPTIONS), so each window
  // gets its own cache entry.
  reportPromises: {},
  currency: "USD",
  // UX-1: report.meta.units {mode, share_per_usd, period_label,
  // basis} (model.py's ReportMeta.units) -- the billing-mode facts
  // format.js's money()/moneyText() need to phrase an amount client-side.
  // null until the first report loads, same as currency defaulting
  // to "USD" until then.
  units: null,
  // The one window every tab reads (the picker in the header): a
  // number of days, or a named window the server resolves ("1h",
  // "today", "24h", "change", "all").
  window: "30",
};

// Short windows show a change's effect within the hour; "Since my
// last change" starts at the newest apply, undo or settings change.
// A window counts every session active in it, whole.
export var WINDOW_OPTIONS = [
  { label: "Last hour", value: "1h" },
  { label: "Today", value: "today" },
  { label: "Last 24 hours", value: "24h" },
  { label: "Last 7 days", value: "7" },
  { label: "Last 30 days", value: "30" },
  { label: "Last 90 days", value: "90" },
  { label: "All time", value: "all" },
  { label: "Since my last change", value: "change" },
];

export var renderedTabs = {};

// -- tab navigation hook -----------------------------------------------
// app.js owns the tab controller. Every other module that links to a
// tab calls showTab, which app.js wires up at start, so no module has
// to import app.js and the module graph has no cycles.

var tabHandler = null;

export function setTabHandler(handler) {
  tabHandler = handler;
}

export function showTab(tabKey, options) {
  if (tabHandler) tabHandler(tabKey, options);
}
