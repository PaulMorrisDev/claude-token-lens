/* claude-token-lens service UI.
 *
 * The entry point: the shell (sidebar, page header, window picker,
 * theme), the #/ router and the renderer for each view. The other
 * first-party modules hold everything else, one per concern (core,
 * format, api, ui, grid, links, shell, icons) and one per page
 * (page-*.js).
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
 * never a blank view.
 *
 * No view holds state the server doesn't already have (docs/ui.md's Data
 * flow section): the address (#/page/segment?w=window) says what is on
 * screen, and `localStorage` keeps only per-viewer conveniences -- the
 * last view, the chosen window, the theme, the sidebar's width and a
 * table's sort -- never data the server is the source of truth for.
 * Reads/writes are wrapped in try/catch: a private window or blocked
 * site data must never break rendering.
 */

import { el, paramsChanged, renderedViews, setRouteHandler, state, storageGet, storageRemove, storageSet, WINDOW_OPTIONS } from "./core.js";
import { icon } from "./icons.js";
import { loadRecommendations, resetFiguresAsOf } from "./api.js";
import { pollHealth } from "./shell.js";
import { formatHash, OLD_TAB_VIEWS, PAGES, parseHash, VIEW_KEYS, viewFor } from "./links.js";
import { revealEvidence } from "./evidence.js";
import { setSectionChart } from "./grid.js";
import { sectionChart } from "./charts-types.js";
import { renderOverview } from "./page-overview.js";
import { renderQuickActions, renderRecommendations } from "./page-actions.js";
import { renderSavings, renderSessions, renderUsage } from "./page-spend.js";
import { renderCache, renderTtl } from "./page-cache.js";
import { renderAgentQuality, renderAgents, renderContextFiles } from "./page-agents.js";
import { renderHabits } from "./page-habits.js";
import { renderConfig, renderProfiles } from "./page-setup.js";
import { renderCapture } from "./page-capture.js";
import { renderDataQuality } from "./page-data.js";
import { renderCostCards, renderGlossary } from "./page-glossary.js";

// One renderer per view key (links.js's VIEW_KEYS, in sidebar order).
var VIEW_RENDERERS = {
  overview: renderOverview,
  "actions/recommendations": renderRecommendations,
  "actions/checks": renderQuickActions,
  "spend/usage": renderUsage,
  "spend/savings": renderSavings,
  "spend/sessions": renderSessions,
  "cache/rebuilds": renderCache,
  "cache/lifetime": renderTtl,
  "agents/subagents": renderAgents,
  "agents/quality": renderAgentQuality,
  "agents/context": renderContextFiles,
  habits: renderHabits,
  "setup/settings": renderConfig,
  "setup/profiles": renderProfiles,
  "setup/capture": renderCapture,
  data: renderDataQuality,
  "glossary/terms": renderGlossary,
  "glossary/how-costs-work": renderCostCards,
};

// ======================================================================
// The router: #/<page>[/<segment>][?w=<window>&id=<item>&t=<table>&row=<row>]
// ======================================================================

var router = {
  // The view on screen, each page's last-used segment, and each view's
  // scroll position, restored on Back and Forward.
  current: null,
  lastSegment: {},
  scroll: {},
  // Set by goTo for the hashchange it causes: a new visit starts at the
  // top; a change nobody asked for here (Back, Forward, a typed address)
  // returns to where the view was left.
  pending: null,
};

// How a view relates to the window: "follow" (the default), "fixed" (its
// figures don't change with the window; the header says so) or "none"
// (no figures at all, so nothing is said).
function windowMode(view) {
  var flag = view.segment && view.segment.window !== undefined ? view.segment.window : view.page.window;
  if (flag === false) return view.page.segments ? "fixed" : "none";
  return "follow";
}

function knownWindow(value) {
  return WINDOW_OPTIONS.some(function (opt) {
    return opt.value === value;
  });
}

// The view key for a page and an optional segment: a page with segments
// and none named opens the one last used there, or its first.
function routeKey(page, segment) {
  if (!page.segments) return page.id;
  var segmentId = segment ? segment.id : router.lastSegment[page.id] || page.segments[0].id;
  return page.id + "/" + segmentId;
}

// The view to open when the address names none: the last one shown, or
// the view the old tab bar last had selected (moved over once).
function startingView() {
  var saved = storageGet("tls:view");
  if (saved && viewFor(saved)) return saved;
  var oldTab = storageGet("tls:activeTab");
  storageRemove("tls:activeTab");
  if (oldTab && OLD_TAB_VIEWS[oldTab]) return OLD_TAB_VIEWS[oldTab];
  return "overview";
}

// Read the address and show what it names. An address that is not a
// route, or leaves out the segment or window, is rewritten in place (no
// extra history entry) to the full form first.
function resolveRoute() {
  var route = parseHash(window.location.hash);
  var key = route ? routeKey(route.page, route.segment) : startingView();
  var params = route ? route.params : {};
  if (params.w && knownWindow(params.w) && params.w !== state.window) applyWindow(params.w);
  params.w = state.window;
  var hash = formatHash(key, params);
  if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  var extra = Object.assign({}, params);
  delete extra.w;
  state.params = extra;

  var pending = router.pending;
  router.pending = null;
  var asked = pending !== null && pending.key === key;
  // An evidence link (t, row) scrolls to its row itself.
  var target = extra.t ? "keep" : null;
  showView(key, {
    force: asked && pending.options.force,
    focus: asked && pending.options.focus,
    scroll: target || (asked ? (key === router.current ? "keep" : "top") : "restore"),
  });
  if (extra.t) revealEvidence(viewPanel(key), extra.t, extra.row);
  paramsChanged(key, extra);
}

// Every in-app navigation comes through here (links.js's pageLink, the
// sidebar, the segments): it adds one history entry, and resolveRoute
// does the rest when the hash changes. A page id alone opens that
// page's last-used segment. options.params go in the address too: an
// item to select (id) or a table row to show (t, row).
function goTo(target, options) {
  options = options || {};
  var route = parseHash("#/" + String(target));
  if (!route) return;
  var key = routeKey(route.page, route.segment);
  if (key === router.current && !options.force && !options.params) {
    if (options.focus) focusTitle();
    return;
  }
  router.pending = { key: key, options: options };
  var hash = formatHash(key, Object.assign({}, options.params || {}, { w: state.window }));
  if (window.location.hash === hash) resolveRoute();
  else window.location.hash = hash;
}

function viewPanel(key) {
  var id = "view-" + key.replace("/", "-");
  var panel = document.getElementById(id);
  if (!panel) {
    panel = el("section", { class: "view", id: id, "data-view": key, "aria-labelledby": "page-title", hidden: true });
    document.getElementById("views").appendChild(panel);
  }
  return panel;
}

function showView(key, options) {
  var view = viewFor(key);
  if (!view) return;
  var previous = router.current;
  if (previous && previous !== key) router.scroll[previous] = window.scrollY;
  router.current = key;
  state.view = key;
  if (view.segment) router.lastSegment[view.page.id] = view.segment.id;
  storageSet("tls:view", key);

  updateSidebar(view);
  updateHeader(view);
  document.title = (view.segment ? view.segment.label + " \u00b7 " : "") + view.page.label + " \u00b7 claude-token-lens";

  var panel = viewPanel(key);
  Array.prototype.forEach.call(document.getElementById("views").children, function (child) {
    child.hidden = child !== panel;
  });
  if (!renderedViews[key] || options.force) {
    renderedViews[key] = true;
    VIEW_RENDERERS[key](panel);
  }
  // A redraw asked for here (Redraw figures, a new window) may change
  // the recommendations too.
  if (options.force) refreshActionsBadge(true);

  // A new view jumps into place: the page's smooth scrolling is for
  // moves within a view, not between them.
  if (options.scroll === "top") window.scrollTo({ top: 0, behavior: "instant" });
  else if (options.scroll === "restore") window.scrollTo({ top: router.scroll[key] || 0, behavior: "instant" });
  if (options.focus) focusTitle();
}

function focusTitle() {
  var title = document.getElementById("page-title");
  if (title) title.focus({ preventScroll: true });
}

// ======================================================================
// Sidebar: the pages, the Actions count and the rail
// ======================================================================

// A plain left click stays in the app; a modified or middle click keeps
// the browser's own meaning (a new tab or window).
function inAppClick(event) {
  return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
}

function buildSidebar() {
  var main = document.getElementById("nav-main");
  var foot = document.getElementById("nav-foot");
  PAGES.forEach(function (page) {
    var link = el("a", { class: "nav-item", href: formatHash(page.id, {}), "data-page": page.id, "data-tip": page.label }, [
      icon(page.icon),
      el("span", { class: "nav-label", text: page.label }),
    ]);
    if (page.id === "actions") {
      link.appendChild(el("span", { class: "nav-badge", id: "actions-badge", hidden: true }));
    }
    link.addEventListener("click", function (event) {
      if (!inAppClick(event)) return;
      event.preventDefault();
      goTo(page.id);
    });
    (page.foot ? foot : main).appendChild(el("li", null, [link]));
  });

  var brand = document.getElementById("brand");
  brand.addEventListener("click", function (event) {
    if (!inAppClick(event)) return;
    event.preventDefault();
    goTo("overview");
  });

  // The rail: icons only, with each page's name as a tooltip. Chosen
  // with the toggle (tls:sidebar), and always on below 1024px wide.
  var app = document.getElementById("app");
  var toggle = document.getElementById("sidebar-toggle");
  var narrow = window.matchMedia("(max-width: 1023px)");
  toggle.appendChild(icon("sidebar"));
  function drawRail() {
    var rail = narrow.matches || storageGet("tls:sidebar") === "rail";
    app.setAttribute("data-sidebar", rail ? "rail" : "full");
    toggle.hidden = narrow.matches;
    toggle.setAttribute("aria-expanded", rail ? "false" : "true");
    toggle.setAttribute("aria-label", rail ? "Show page names" : "Hide page names");
    toggle.setAttribute("data-tip", rail ? "Show page names" : "Hide page names");
  }
  toggle.addEventListener("click", function () {
    storageSet("tls:sidebar", app.getAttribute("data-sidebar") === "rail" ? "full" : "rail");
    drawRail();
  });
  narrow.addEventListener("change", drawRail);
  drawRail();
}

function updateSidebar(view) {
  Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (link) {
    if (link.getAttribute("data-page") === view.page.id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

// The number of "Do this" recommendations for the window, beside
// Actions. Hidden when there are none or they can't be counted.
var badgeFor = null;

function refreshActionsBadge(again) {
  var windowAsked = state.window;
  if (badgeFor === windowAsked && !again) return;
  badgeFor = windowAsked;
  loadRecommendations().then(function (result) {
    if (state.window !== windowAsked) return;
    var badge = document.getElementById("actions-badge");
    if (!badge) return;
    var body = result.body;
    var recs = body && body.ok === true && Array.isArray(body.data) ? body.data : [];
    var count = recs.filter(function (rec) {
      return rec.severity === "action";
    }).length;
    badge.textContent = "";
    badge.hidden = !count;
    if (!count) return;
    badge.appendChild(el("span", { "aria-hidden": "true", text: String(count) }));
    badge.appendChild(el("span", { class: "visually-hidden", text: ", " + count + (count === 1 ? " change" : " changes") + " to make" }));
  });
}

// ======================================================================
// Page header: title, segments, window, theme
// ======================================================================

function updateHeader(view) {
  document.getElementById("page-title").textContent = view.page.label;

  var nav = document.getElementById("segments");
  nav.textContent = "";
  nav.hidden = !view.page.segments;
  if (view.page.segments) {
    nav.setAttribute("aria-label", view.page.label + " sections");
    view.page.segments.forEach(function (segment) {
      var key = view.page.id + "/" + segment.id;
      var link = el("a", { class: "segment", href: formatHash(key, { w: state.window }), text: segment.label });
      if (segment === view.segment) link.setAttribute("aria-current", "page");
      link.addEventListener("click", function (event) {
        if (!inAppClick(event)) return;
        event.preventDefault();
        goTo(key);
      });
      nav.appendChild(link);
    });
  }
  updateWindowControl(view);
}

var windowControl = { button: null, label: null, menu: null, fixed: null };

function windowLabel(value) {
  for (var i = 0; i < WINDOW_OPTIONS.length; i++) {
    if (WINDOW_OPTIONS[i].value === value) return WINDOW_OPTIONS[i].label;
  }
  return value;
}

function updateWindowControl(view) {
  var mode = windowMode(view);
  windowControl.button.hidden = mode !== "follow";
  windowControl.fixed.hidden = mode !== "fixed";
  windowControl.label.textContent = windowLabel(state.window);
  Array.prototype.forEach.call(windowControl.menu.querySelectorAll("[role=menuitemradio]"), function (item) {
    item.setAttribute("aria-checked", item.getAttribute("data-value") === state.window ? "true" : "false");
  });
}

// A new window: every view that follows the window draws again when next
// shown (setWindow redraws the one on screen).
function applyWindow(value) {
  state.window = value;
  storageSet("tls:window", value);
  delete state.reportPromises[value];
  delete state.recommendationPromises[value];
  resetFiguresAsOf();
  Object.keys(renderedViews).forEach(function (key) {
    var view = viewFor(key);
    if (!view || windowMode(view) === "follow") delete renderedViews[key];
  });
  refreshActionsBadge();
}

function setWindow(value) {
  if (value === state.window || !knownWindow(value)) return;
  applyWindow(value);
  var view = viewFor(router.current);
  // What the view had open stays open if the new window has it; a table
  // row shown from an evidence link was a one-off.
  var keep = {};
  if (state.params.id) keep.id = state.params.id;
  state.params = keep;
  window.history.replaceState(null, "", formatHash(router.current, Object.assign({ w: value }, keep)));
  updateHeader(view);
  if (windowMode(view) === "follow") showView(router.current, { force: true, scroll: "keep" });
}

function initWindowPicker() {
  var saved = storageGet("tls:window");
  if (saved === null) {
    var legacy = storageGet("tls:overviewWindow");
    if (legacy !== null) saved = legacy || "all";
  }
  if (knownWindow(saved)) state.window = saved;

  var host = document.getElementById("window-picker");
  var label = el("span", { class: "control-label" });
  var button = el(
    "button",
    { type: "button", class: "control-button", id: "window-button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "window-menu" },
    [icon("clock"), el("span", { class: "visually-hidden", text: "Window: " }), label, icon("chevron-down")]
  );
  var menu = el("div", { class: "menu", id: "window-menu", role: "menu", "aria-label": "Window", hidden: true });
  WINDOW_OPTIONS.forEach(function (opt) {
    var item = el("button", { type: "button", class: "menu-item", role: "menuitemradio", "data-value": opt.value, tabIndex: -1 }, [
      icon("check"),
      el("span", { text: opt.label }),
    ]);
    item.addEventListener("click", function () {
      closeMenu(true);
      setWindow(opt.value);
    });
    menu.appendChild(item);
  });
  menu.appendChild(
    el("p", {
      class: "menu-note",
      text: "A window counts every session with a reply in it, in full. So a long session that started earlier counts whole.",
    })
  );
  var fixed = el("span", { class: "window-fixed", hidden: true }, [icon("clock"), el("span", { text: "The window doesn't apply here" })]);
  host.appendChild(button);
  host.appendChild(menu);
  host.appendChild(fixed);
  windowControl = { button: button, label: label, menu: menu, fixed: fixed };

  function items() {
    return Array.prototype.slice.call(menu.querySelectorAll(".menu-item"));
  }
  function openMenu() {
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    (menu.querySelector('[aria-checked="true"]') || items()[0]).focus();
  }
  function closeMenu(returnFocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (returnFocus) button.focus();
  }
  button.addEventListener("click", function () {
    if (menu.hidden) openMenu();
    else closeMenu(false);
  });
  button.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      openMenu();
    }
  });
  menu.addEventListener("keydown", function (event) {
    var list = items();
    var index = list.indexOf(document.activeElement);
    var next = null;
    if (event.key === "ArrowDown") next = (index + 1) % list.length;
    else if (event.key === "ArrowUp") next = (index - 1 + list.length) % list.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = list.length - 1;
    else if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
      return;
    } else if (event.key === "Tab") {
      closeMenu(false);
      return;
    }
    if (next === null) return;
    event.preventDefault();
    list[next].focus();
  });
  document.addEventListener("pointerdown", function (event) {
    if (!host.contains(event.target)) closeMenu(false);
  });
}

// The theme: follow the system, or the viewer's pick (tls:theme, which
// theme-boot.js applies before the first paint on the next load).
var THEMES = [
  { value: "system", label: "Theme: same as the system", icon: "system" },
  { value: "light", label: "Theme: light", icon: "sun" },
  { value: "dark", label: "Theme: dark", icon: "moon" },
];

function initThemeToggle() {
  var button = document.getElementById("theme-toggle");
  function currentIndex() {
    var value = document.documentElement.getAttribute("data-theme");
    return value === "light" ? 1 : value === "dark" ? 2 : 0;
  }
  function draw() {
    var theme = THEMES[currentIndex()];
    var next = THEMES[(currentIndex() + 1) % THEMES.length];
    button.textContent = "";
    button.appendChild(icon(theme.icon));
    button.setAttribute("aria-label", theme.label + ". Switch to " + next.value);
    button.setAttribute("data-tip", theme.label);
  }
  button.addEventListener("click", function () {
    var next = THEMES[(currentIndex() + 1) % THEMES.length].value;
    document.documentElement.setAttribute("data-theme", next);
    if (next === "system") storageRemove("tls:theme");
    else storageSet("tls:theme", next);
    draw();
  });
  draw();
}

// A hairline under the page header once content scrolls beneath it.
function initStickyHeader() {
  var header = document.getElementById("page-header");
  var sentinel = document.getElementById("header-sentinel");
  if (!("IntersectionObserver" in window)) return;
  new IntersectionObserver(function (entries) {
    header.classList.toggle("is-stuck", !entries[0].isIntersecting);
  }).observe(sentinel);
}

function init() {
  if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
  setRouteHandler(goTo);
  setSectionChart(sectionChart);
  buildSidebar();
  initWindowPicker();
  initThemeToggle();
  initStickyHeader();
  document.getElementById("skip-link").addEventListener("click", focusTitle);
  pollHealth();
  window.addEventListener("hashchange", resolveRoute);
  resolveRoute();
  refreshActionsBadge();
}

// VIEW_KEYS and VIEW_RENDERERS must name the same views: a page added to
// links.js without a renderer would otherwise show an empty view.
VIEW_KEYS.forEach(function (key) {
  if (!VIEW_RENDERERS[key]) throw new Error("no renderer for view " + key);
});

document.addEventListener("DOMContentLoaded", init);
