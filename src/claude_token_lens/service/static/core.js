/* claude-token-lens service UI: core.js
 *
 * Shared DOM helpers, browser storage, the dashboard's state and the
 * navigation hook (goTo) that lets a module link to another view
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

export function storageRemove(key) {
  try {
    window.localStorage.removeItem(key);
  } catch (err) {
    /* private window or blocked site data -- ignore */
  }
}

// -- report.json cache (shared by every view that reads the report) ---

export var state = {
  // One report promise per window (review finding 21): /api/report.json
  // takes the window and the server memoizes its answer per window and
  // change (docs/api.md). A single shared promise kept every view on the
  // first window's report after the picker changed.
  reportPromises: {},
  currency: "USD",
  // UX-1: report.meta.units {mode, share_per_usd, period_label,
  // basis} (model.py's ReportMeta.units) -- the billing-mode facts
  // format.js's money()/moneyText() need to phrase an amount client-side.
  // null until the first report loads, same as currency defaulting
  // to "USD" until then.
  units: null,
  // The one window every view reads (the picker in the page header): a
  // number of days, or a named window the server resolves ("1h",
  // "today", "24h", "change", "all").
  window: "30",
  // The view on screen, as links.js's view key ("spend/usage").
  view: null,
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

// View keys already drawn for the current window; a view not listed
// draws when next shown.
export var renderedViews = {};

// -- navigation hook -----------------------------------------------------
// app.js owns the router. Every other module that links to a view calls
// goTo, which app.js wires up at start, so no module has to import
// app.js and the module graph has no cycles.

var routeHandler = null;

export function setRouteHandler(handler) {
  routeHandler = handler;
}

// Show a view ("spend/usage", or a page id for its last-used segment).
// options: focus (move focus to the page title), force (draw again).
export function goTo(viewKey, options) {
  if (routeHandler) routeHandler(viewKey, options);
}
