/* claude-token-lens service UI: shell.js
 *
 * The parts on every tab: the health banner and footer, and the
 * metrics-capture banner.
 */

import { clear, el, renderedTabs, showTab, state, storageGet, storageSet } from "./core.js";
import { shortTs, thousands } from "./format.js";
import { fetchJson, figures, resetFiguresAsOf } from "./api.js";
import { captureTabLink, tabLink } from "./links.js";

export function renderHealth(health, container) {
  var watcher = health.watcher || {};
  if (health.service_registered === false) {
    container.appendChild(
      el("div", { class: "notice error", role: "alert" }, [
        el("p", {
          text:
            "The service is not registered to start at logon; history older than cleanupPeriodDays will be lost after a reboot. Run: claude-token-lens install-service",
        }),
      ])
    );
  }
  var scan = health.scan || {};
  if (health.message) {
    container.appendChild(el("div", { class: "notice" + (health.status === "starting" ? "" : " error") }, [el("p", { text: health.message })]));
  }
  var lines = [
    "status: " + (health.status || "unknown"),
    "version: " + (health.version || "-"),
    "schema version: " + (health.schema_version === undefined ? "-" : health.schema_version),
    "last scan finished: " + (scan.last_success_at ? shortTs(scan.last_success_at) : watcher.finished_at ? shortTs(watcher.finished_at) : "never"),
    "files scanned / parsed: " + thousands(watcher.files_scanned || 0) + " / " + thousands(watcher.files_parsed || 0),
    "sessions upserted: " + thousands(watcher.sessions_upserted || 0),
    "errors this tick: " + (watcher.errors || 0),
  ];
  var list = el(
    "ul",
    { class: "notes" },
    lines.map(function (line) {
      return el("li", { text: line });
    })
  );
  container.appendChild(list);
  if (watcher.error_messages && watcher.error_messages.length) {
    container.appendChild(el("p", { text: "Recent errors:" }));
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
  updateFooterHealth(health);
}

var HEALTH_LABELS = {
  ok: "Up to date",
  starting: "Scanning your history",
  degraded: "Last scan failed",
  stale: "Not updating",
};

function updateFooterHealth(health) {
  var footer = document.getElementById("footer-health");
  if (!footer) return;
  var watcher = health.watcher || {};
  var scan = health.scan || {};
  var lastScan = scan.last_success_at || watcher.finished_at;
  footer.textContent =
    (health.version ? "claude-token-lens " + health.version + " — " : "") +
    (HEALTH_LABELS[health.status] || "Service " + (health.status || "unknown")) +
    (lastScan ? " — last scan finished " + shortTs(lastScan) : "") +
    (watcher.errors ? " — " + watcher.errors + (watcher.errors === 1 ? " error" : " errors") : "") +
    (figures.asOf ? " — figures as of " + shortTs(figures.asOf) : "") +
    ".";
}

// A newer or reset figures-as-of time (api.js) redraws the footer.
figures.notify = function () {
  if (healthPoll.health) updateFooterHealth(healthPoll.health);
};

// The banner under the header, on every tab: what /api/health's
// status means when it is not "ok" (first scan in progress, a failed
// scan, a scanner that has stopped) and, once a scan that was running
// when the page drew its figures finishes, a way to redraw them.
var healthPoll = { status: null, timer: null, health: null };

function redrawAllTabs() {
  state.reportPromises = {};
  resetFiguresAsOf();
  Object.keys(renderedTabs).forEach(function (key) {
    delete renderedTabs[key];
  });
  showTab(state.activeTab || "overview", { force: true });
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
    banner.classList.add("error");
    banner.appendChild(el("p", { text: "Can't reach the dashboard service, so these figures may be out of date. Is serve still running?" }));
    banner.hidden = false;
    return;
  }
  if (health.status === "ok") {
    if (previous === "starting" || banner.getAttribute("data-scan-finished") === "true") {
      banner.setAttribute("data-scan-finished", "true");
      var refresh = el("button", { type: "button", class: "link-button", text: "Redraw figures" });
      refresh.addEventListener("click", function () {
        banner.removeAttribute("data-scan-finished");
        banner.hidden = true;
        redrawAllTabs();
      });
      banner.appendChild(el("p", {}, [el("span", { text: "The scan has finished. " }), refresh]));
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

export function pollHealth() {
  fetchJson("/api/health").then(function (result) {
    var body = result.body;
    var health = body && body.ok === true ? body.data : null;
    var previous = healthPoll.status;
    healthPoll.status = health ? health.status : "unreachable";
    if (health) healthPoll.health = health;
    renderHealthBanner(health, previous);
    if (health) updateFooterHealth(health);
    if (health) updateCaptureBanner(health.capture);
    // Poll quickly while a scan's progress is worth watching.
    healthPoll.timer = setTimeout(pollHealth, health && health.status === "starting" ? 3000 : 60000);
  });
}

// ======================================================================
// Metrics capture: the banner on every tab, and the Capture tab
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

// UX-6/9: both the capture-invite "Hide" and a notes dismissal used to
// be (or would otherwise have been) permanent, via a bare "1" in
// localStorage -- once hidden, hidden forever, even after the notes
// themselves changed. A snoozed key instead stores the dismissal
// timestamp; snoozed() below treats it as expired past BANNER_SNOOZE_MS,
// so a quiet banner returns on its own after a week rather than needing
// the config wiped to see it again. A legacy bare "1" reads as an
// ancient timestamp and is therefore already-expired -- it naturally
// self-heals to "not hidden" the first time this runs, with no
// migration code needed.
var BANNER_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;

function snoozed(key) {
  var raw = storageGet(key);
  if (!raw) return false;
  var ts = Number(raw);
  return isFinite(ts) && Date.now() - ts < BANNER_SNOOZE_MS;
}

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
  var inviteHidden = !info.on && snoozed("tls:captureInviteHidden");
  var hidden = inviteHidden && !info.feedback_note && !notesVisible;
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
  line.appendChild(captureTabLink(info.on ? "Capture settings" : "See what it captures"));
  if (info.on) {
    line.appendChild(document.createTextNode(" "));
    line.appendChild(tabLink("habits", "Work habits"));
  }
  if (!info.on) {
    var hide = el("button", { type: "button", class: "link-button capture-hide", text: "Hide for a week" });
    hide.addEventListener("click", function () {
      storageSet("tls:captureInviteHidden", String(Date.now()));
      renderCaptureBanner(data);
    });
    line.appendChild(document.createTextNode(" "));
    line.appendChild(hide);
  }
  banner.appendChild(line);
  if (notesVisible) {
    banner.appendChild(
      el(
        "ul",
        { class: "capture-notes" },
        notes.map(function (note) {
          return el("li", { text: note });
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
