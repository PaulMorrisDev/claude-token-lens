/* claude-token-lens service UI: shell.js
 *
 * The parts on every page: the sidebar's status line (service health,
 * figures, capture level, version), the health banner and the
 * metrics-capture banner.
 */

import { clear, el, goTo, renderedViews, state, storageGet, storageSet } from "./core.js";
import { shortTs, thousands } from "./format.js";
import { connection, fetchJson, figures, resetFiguresAsOf, runReconnectRetries } from "./api.js";
import { captureLink, pageLink } from "./links.js";
import { button, callout, prose, toast } from "./ui.js";

// A service that doesn't start at logon loses history to Claude Code's
// cleanup: said on the Overview, where it will be seen, and again with
// the rest of the health detail on Data quality.
export function renderLogonNotice(health, container) {
  if (!health || health.service_registered !== false) return;
  container.appendChild(
    callout({
      tone: "critical",
      title: "The service doesn't start when you log on.",
      text: "After a restart, history older than Claude Code's cleanup period (cleanupPeriodDays) is lost. To fix it, run: claude-token-lens install-service",
    })
  );
}

export function renderHealth(health, container) {
  var watcher = health.watcher || {};
  renderLogonNotice(health, container);
  var scan = health.scan || {};
  if (health.message) {
    container.appendChild(callout({ tone: health.status === "starting" ? "info" : "critical", text: health.message }));
  }
  var facts = [
    ["Status", HEALTH_LABELS[health.status] || health.status || "Unknown"],
    ["Version", health.version || "-"],
    ["Database version", health.schema_version === undefined ? "-" : String(health.schema_version)],
    ["Last scan finished", scan.last_success_at ? shortTs(scan.last_success_at) : watcher.finished_at ? shortTs(watcher.finished_at) : "Not yet"],
    ["Transcript files checked", thousands(watcher.files_scanned || 0)],
    ["Changed files read", thousands(watcher.files_parsed || 0)],
    ["Sessions updated", thousands(watcher.sessions_upserted || 0)],
    ["Errors in the last scan", thousands(watcher.errors || 0)],
  ];
  var list = el("dl", { class: "fact-list" });
  facts.forEach(function (pair) {
    list.appendChild(el("dt", { text: pair[0] }));
    list.appendChild(el("dd", { text: pair[1] }));
  });
  container.appendChild(list);
  if (watcher.error_messages && watcher.error_messages.length) {
    container.appendChild(el("p", { text: "Recent errors" }));
    container.appendChild(
      el(
        "ul",
        { class: "notes" },
        watcher.error_messages.map(function (msg) {
          return el("li", { text: msg });
        })
      )
    );
  }
}

// ======================================================================
// The sidebar's status line
// ======================================================================

var HEALTH_LABELS = {
  ok: "Up to date",
  starting: "Scanning your history",
  degraded: "Last scan failed",
  stale: "Not updating",
  unreachable: "Can't reach the service",
};

// The dot beside the label, by status token. The label always says the
// same thing in words.
var HEALTH_TONES = { ok: "good", starting: "accent", degraded: "serious", stale: "warn", unreachable: "critical" };

function captureStatusText(block) {
  if (!block) return "";
  if (!block.on) return "Capture: off";
  var title = block.title || block.level;
  if (block.expired) return "Capture: " + title + ", ended";
  if (block.hooks_ok === false) return "Capture: " + title + ", hooks not connected";
  return "Capture: " + title;
}

function renderStatusLine() {
  var line = document.getElementById("status-line");
  if (!line) return;
  var health = healthPoll.health;
  var status = healthPoll.status || "starting";
  var watcher = (health && health.watcher) || {};
  var scan = (health && health.scan) || {};
  var lastScan = scan.last_success_at || watcher.finished_at;
  var label = HEALTH_LABELS[status] || "Service " + status;
  var parts = [
    status,
    lastScan || "",
    watcher.errors || 0,
    figures.asOf || "",
    healthPoll.redrawDue ? "1" : "0",
    captureStatusText(health && health.capture),
    (health && health.version) || "",
  ];
  // Rebuilt only when something shown changed: the poll runs every few
  // seconds during a scan.
  var sig = parts.join("|");
  if (line.getAttribute("data-render-sig") === sig) return;
  line.setAttribute("data-render-sig", sig);
  clear(line);

  line.appendChild(
    el("div", { class: "status-row status-health", "data-tip": label }, [
      el("span", { class: "status-dot tone-" + (HEALTH_TONES[status] || "muted"), "aria-hidden": "true" }),
      el("span", { class: "status-text", text: label }),
    ])
  );
  var detail = [];
  if (lastScan) detail.push("Last scan " + shortTs(lastScan));
  if (watcher.errors) detail.push(watcher.errors + (watcher.errors === 1 ? " error" : " errors"));
  if (detail.length) line.appendChild(el("div", { class: "status-row status-detail", text: detail.join(", ") }));
  if (figures.asOf || healthPoll.redrawDue) {
    var row = el("div", { class: "status-row status-detail" });
    if (figures.asOf) row.appendChild(el("span", { text: "Figures as of " + shortTs(figures.asOf) }));
    if (healthPoll.redrawDue) row.appendChild(redrawButton());
    line.appendChild(row);
  }
  var capture = captureStatusText(health && health.capture);
  if (capture) line.appendChild(el("div", { class: "status-row status-detail" }, [captureLink(capture)]));
  line.appendChild(
    el("div", {
      class: "status-row status-detail",
      text: (health && health.version ? "claude-token-lens " + health.version + ". " : "") + "Your data stays on this machine.",
    })
  );
}

// A newer or reset figures-as-of time (api.js) redraws the status line.
figures.notify = renderStatusLine;

// ======================================================================
// The health banner, under the page header on every page
// ======================================================================

// What /api/health's status means when it is not "ok" (first scan in
// progress, a failed scan, a scanner that has stopped) and, once a scan
// that was running when the page drew its figures finishes, a way to
// redraw them (also offered in the status line).
var healthPoll = { status: null, timer: null, health: null, redrawDue: false, inflight: false, misses: 0 };

// options.focus moves focus to the page title: the button pressed is
// redrawn away. A redraw after reconnecting leaves focus where it is.
function redrawEverything(options) {
  healthPoll.redrawDue = false;
  var banner = document.getElementById("health-banner");
  if (banner) {
    banner.removeAttribute("data-scan-finished");
    banner.hidden = true;
  }
  state.reportPromises = {};
  state.recommendationPromises = {};
  resetFiguresAsOf();
  Object.keys(renderedViews).forEach(function (key) {
    delete renderedViews[key];
  });
  // The button pressed is redrawn away, so focus moves to the page
  // title rather than dropping to the document.
  goTo(state.view || "overview", { force: true, focus: !!(options && options.focus) });
  renderStatusLine();
}

function redrawButton() {
  var redraw = el("button", { type: "button", class: "link-button", text: "Redraw figures" });
  redraw.addEventListener("click", function () {
    redrawEverything({ focus: true });
  });
  return redraw;
}

function renderHealthBanner(health, previous) {
  var banner = document.getElementById("health-banner");
  if (!banner) return;
  // UX-6/9: this is an aria-live="polite" region polled every 3-60s
  // (see pollHealth) -- rebuilding its children on every poll, even
  // when nothing about to be shown actually changed, used to tear
  // down and recreate the same <p>/<progress> each time, and a
  // screen reader has no way to tell that apart from genuinely new
  // content, so it re-announced an unchanged "Scanning... 4 of 12"
  // every few seconds. Skip the rebuild entirely when what would be
  // shown is identical to what is already on screen.
  var scanNow = (health && health.scan) || {};
  var sig = !health
    ? "unreachable"
    : health.status === "ok"
      ? previous === "starting" || banner.getAttribute("data-scan-finished") === "true"
        ? "scan-finished"
        : "hidden"
      : ["active", health.status, health.message || "", scanNow.total || 0, scanNow.done || 0].join("|");
  if (banner.getAttribute("data-render-sig") === sig) return;
  banner.setAttribute("data-render-sig", sig);
  clear(banner);
  banner.className = "health-banner";
  if (!health) {
    // No countdown: this region is read aloud when it changes, so it
    // says once that it retries by itself, with a way to try now.
    banner.classList.add("error");
    banner.appendChild(
      el("p", {}, [
        el("span", { text: "Can't reach Token Lens's local service, so the figures on screen may be out of date. It keeps trying by itself. " }),
        button("Try now", { variant: "link", action: pollHealth }),
      ])
    );
    banner.hidden = false;
    return;
  }
  if (health.status === "ok") {
    if (previous === "starting" || banner.getAttribute("data-scan-finished") === "true") {
      banner.setAttribute("data-scan-finished", "true");
      healthPoll.redrawDue = true;
      banner.appendChild(el("p", {}, [el("span", { text: "The scan has finished. " }), redrawButton()]));
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
    return;
  }
  banner.removeAttribute("data-scan-finished");
  if (health.status !== "starting") banner.classList.add("error");
  banner.appendChild(el("p", { text: health.message || HEALTH_LABELS[health.status] || health.status }));
  var scan = health.scan || {};
  if (health.status === "starting" && scan.total) {
    banner.appendChild(el("progress", { max: String(scan.total), value: String(scan.done || 0) }));
  }
  banner.hidden = false;
}

// While the service can't be reached: try again after 2, 4, 8, 16, then
// every 30 seconds.
var RETRY_SECONDS = [2, 4, 8, 16, 30];

export function pollHealth() {
  if (healthPoll.inflight) return;
  healthPoll.inflight = true;
  clearTimeout(healthPoll.timer);
  fetchJson("/api/health").then(function (result) {
    healthPoll.inflight = false;
    var body = result.body;
    var health = body && body.ok === true ? body.data : null;
    var previous = healthPoll.status;
    healthPoll.status = health ? health.status : result.httpStatus === 0 ? "unreachable" : previous || "starting";
    if (health) healthPoll.health = health;
    renderHealthBanner(result.httpStatus === 0 ? null : health, previous);
    renderStatusLine();
    if (health) updateCaptureBanner(health.capture);
    var delay;
    if (result.httpStatus === 0) {
      delay = RETRY_SECONDS[Math.min(healthPoll.misses, RETRY_SECONDS.length - 1)] * 1000;
      healthPoll.misses += 1;
    } else {
      healthPoll.misses = 0;
      // Poll quickly while a scan's progress is worth watching.
      delay = health && health.status === "starting" ? 3000 : 60000;
    }
    healthPoll.timer = setTimeout(pollHealth, delay);
  });
}

// Any request that finds the service gone (or back) says so here: the
// views are marked stale and the health poll starts retrying at once;
// back again, what failed meanwhile loads again.
connection.notify = function (up) {
  var views = document.getElementById("views");
  if (views) views.toggleAttribute("data-stale", !up);
  if (!up) {
    if (healthPoll.status !== "unreachable") pollHealth();
    return;
  }
  toast("Connected to the service again.", { tone: "info" });
  if (connection.reportFailed) {
    connection.retries = [];
    connection.reportFailed = false;
    redrawEverything();
  } else {
    runReconnectRetries();
  }
};

// ======================================================================
// Metrics capture: the banner, shown only when there is something to act
// on (the status line always says the level)
// ======================================================================

// /api/capture's answer, kept for the banner: fetched again when the
// capture block /api/health carries changes, or every few minutes for
// fresh figures (the server keeps its own copy, so this is cheap).
export var capturePoll = { data: null, sig: null, fetchedAt: 0, pending: false };

var CAPTURE_REFRESH_MS = 5 * 60 * 1000;

function captureSignature(block) {
  if (!block) return "none";
  return [block.level, block.enabled_at, block.sample, block.until, block.expired, block.hooks_ok, (block.metrics || []).join(","), (block.feedback || []).join(",")].join("|");
}

function updateCaptureBanner(block) {
  var banner = document.getElementById("capture-banner");
  if (!banner) return;
  if (!block) {
    banner.hidden = true;
    return;
  }
  var sig = captureSignature(block);
  var stale = Date.now() - capturePoll.fetchedAt > CAPTURE_REFRESH_MS;
  if (capturePoll.data && sig === capturePoll.sig && !stale) return;
  if (capturePoll.pending) return;
  capturePoll.pending = true;
  fetchJson("/api/capture").then(function (result) {
    capturePoll.pending = false;
    var body = result.body;
    if (!body || body.ok !== true) {
      banner.hidden = true;
      return;
    }
    capturePoll.sig = sig;
    showCaptureData(body.data);
  });
}

// A fresh /api/capture answer: keep it and redraw the banner.
export function showCaptureData(data) {
  capturePoll.data = data;
  capturePoll.fetchedAt = Date.now();
  renderCaptureBanner(data);
}

// UX-6/9: a notes dismissal stores its timestamp, not a bare "1", and
// lapses after BANNER_SNOOZE_MS, so a quiet banner returns on its own
// after a week rather than staying hidden for good.
var BANNER_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;

function notesSignature(notes) {
  return (notes || []).join("\n");
}

// The notes list keys its own snooze to its exact current content
// (timestamp + signature, "|"-joined) so notes that changed since the
// dismissal -- a new warning, say -- show again immediately rather
// than staying suppressed for the rest of the week.
function notesSnoozed(notes) {
  var raw = storageGet("tls:captureNotesHidden");
  if (!raw) return false;
  var sep = raw.indexOf("|");
  if (sep === -1) return false;
  var ts = Number(raw.slice(0, sep));
  return isFinite(ts) && raw.slice(sep + 1) === notesSignature(notes) && Date.now() - ts < BANNER_SNOOZE_MS;
}

function renderCaptureBanner(data) {
  var banner = document.getElementById("capture-banner");
  if (!banner) return;
  var info = data.banner || {};
  var notes = info.notes || [];
  var notesVisible = notes.length > 0 && !notesSnoozed(notes);
  var hidden = !info.feedback_note && !notesVisible;
  // Same "skip the rebuild when nothing shown would change" guard as
  // renderHealthBanner -- this is an aria-live="polite" region too,
  // and gets re-rendered on every capture poll (see updateCaptureBanner),
  // not only on an actual content change.
  var sig = hidden ? "hidden" : ["shown", info.on ? "1" : "0", info.headline || "", info.feedback_note || "", notesVisible ? notesSignature(notes) : ""].join("~");
  if (banner.getAttribute("data-render-sig") === sig) return;
  banner.setAttribute("data-render-sig", sig);
  clear(banner);
  banner.className = "capture-banner " + (info.on ? "capture-on" : "capture-off");
  if (hidden) {
    banner.hidden = true;
    return;
  }
  var line = el("p", { class: "capture-headline" }, [el("span", { text: info.headline + " " })]);
  line.appendChild(captureLink(info.on ? "Capture settings" : "See what it captures"));
  if (info.on) {
    line.appendChild(document.createTextNode(" "));
    line.appendChild(pageLink("habits", "Work habits"));
  }
  banner.appendChild(line);
  if (notesVisible) {
    banner.appendChild(
      el(
        "ul",
        { class: "capture-notes" },
        notes.map(function (note) {
          return el("li", null, prose(note));
        })
      )
    );
    var dismissNotes = el("button", { type: "button", class: "link-button capture-hide", text: "Dismiss for a week" });
    dismissNotes.addEventListener("click", function () {
      storageSet("tls:captureNotesHidden", Date.now() + "|" + notesSignature(notes));
      renderCaptureBanner(data);
    });
    banner.appendChild(dismissNotes);
  }
  if (info.feedback_note) banner.appendChild(el("p", { class: "capture-feedback-note", text: info.feedback_note }));
  banner.hidden = false;
}
