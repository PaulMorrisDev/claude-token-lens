/* claude-token-lens service UI: api.js
 *
 * Fetching from the service: the {ok, data} envelope, the per-window
 * report cache and the figures-as-of stamp.
 */

import { clear, state } from "./core.js";
import { readableAmounts, setKnownProjects } from "./format.js";
import { errorNotice, loadingNode } from "./ui.js";

// -- fetch / envelope handling ----------------------------------------

export function fetchJson(url, options) {
  return fetch(url, options)
    .then(function (res) {
      return res
        .json()
        .catch(function () {
          return { ok: false, error: { code: "bad_response", message: "response was not valid JSON (HTTP " + res.status + ")" } };
        })
        .then(function (body) {
          // A report-backed route says when its figures are from
          // (docs/api.md, "Caching").
          var asOf = res.headers.get("X-Figures-As-Of");
          if (asOf) noteFiguresAsOf(asOf);
          noteConnection(true);
          return { httpStatus: res.status, body: readableAmounts(body), asOf: asOf };
        });
    })
    .catch(function (err) {
      noteConnection(false);
      return { httpStatus: 0, body: { ok: false, error: { code: "network_error", message: String(err && err.message ? err.message : err) } } };
    });
}

// Whether the local service answers (docs/ui.md, "Service unreachable").
// shell.js sets notify: it says so under the page header, retries, and
// on reconnecting runs the loads that failed meanwhile (retryOnReconnect).
// reportFailed: a report load failed while it was gone, so the views
// drawn from it need drawing again, not just their own loads.
export var connection = { up: true, notify: null, retries: [], reportFailed: false };

function noteConnection(up) {
  if (connection.up === up) return;
  connection.up = up;
  if (connection.notify) connection.notify(up);
}

// A load that failed because the service was gone runs again once it's
// back, if what it draws into is still on the page.
export function retryOnReconnect(retry) {
  connection.retries.push(retry);
}

export function runReconnectRetries() {
  var retries = connection.retries;
  connection.retries = [];
  connection.reportFailed = false;
  retries.forEach(function (retry) {
    retry();
  });
}

/**
 * Fetch `url` and draw it into `container`: `render(data, container)` on
 * `ok: true`, or an error callout with a Try again button on `ok: false`
 * or a network failure. Never throws -- this is the one place most
 * views' data flow funnels through, per docs/ui.md's "or shows the
 * error.message inline... never a raw stack trace" contract.
 *
 * Empty, the container shows a skeleton while the answer is on its way
 * (options.skeleton picks its shape: "lines", "rows", "tiles"). Already
 * drawn, it keeps what it shows, dimmed, until the answer lands; and if
 * the service has gone, it keeps it, marked stale, and tries again once
 * the service is back. options also passes through to fetch.
 */
export function loadInto(container, url, render, options) {
  var drawn = container.firstChild !== null && !container.querySelector(".loading, .callout-critical");
  if (drawn) {
    container.classList.add("is-refreshing");
    container.setAttribute("aria-busy", "true");
  } else {
    clear(container);
    container.appendChild(loadingNode(null, options && options.skeleton));
  }
  function retry() {
    if (container.isConnected) loadInto(container, url, render, options);
  }
  return fetchJson(url, options).then(function (result) {
    container.classList.remove("is-refreshing");
    container.removeAttribute("aria-busy");
    var body = result.body;
    if (!body || body.ok !== true) {
      var error = body && body.error;
      var offline = result.httpStatus === 0;
      if (offline) retryOnReconnect(retry);
      if (offline && drawn) {
        container.classList.add("is-stale");
        return null;
      }
      clear(container);
      container.appendChild(errorNotice(error, retry));
      return null;
    }
    clear(container);
    container.classList.remove("is-stale");
    try {
      render(body.data, container);
    } catch (err) {
      container.appendChild(errorNotice({ code: "render_error", message: String(err && err.message ? err.message : err) }));
    }
    return body.data;
  });
}

// The window as a query parameter, for every window-aware route.
function windowParam() {
  var value = state.window || "all";
  return /^[0-9]+$/.test(value) ? "window_days=" + value : "window=" + encodeURIComponent(value);
}

export function withWindow(url) {
  return url + (url.indexOf("?") === -1 ? "?" : "&") + windowParam();
}

export function loadReport() {
  var key = state.window;
  if (state.reportPromises[key]) {
    // A report fetched earlier is drawn again: its figures' time counts.
    state.reportPromises[key].then(function (loaded) {
      if (loaded && loaded.asOf) noteFiguresAsOf(loaded.asOf);
    });
  } else {
    var url = withWindow("/api/report.json");
    state.reportPromises[key] = fetchJson(url).then(function (result) {
      var body = result.body;
      if (!body || body.ok === false) {
        // Not kept: the next view that asks fetches it again (the
        // service may be back, or the failure passing).
        delete state.reportPromises[key];
        if (result.httpStatus === 0) connection.reportFailed = true;
        return { error: (body && body.error) || { code: "error", message: "failed to load report" } };
      }
      var asOf = result.asOf;
      // docs/api.md: unlike every other route, /api/report.json is the
      // raw rendered document ({"schema_version": ..., "report": {...}}),
      // not the {"ok": true, "data": ...} envelope -- kept unwrapped for
      // byte parity with the CLI's own `report --json` output. Accept
      // both shapes here: `body.ok === true` is an enveloped response
      // (a possible future/alternate deployment), whose report lives at
      // `body.data.report`; anything else that reached this point (no
      // `ok` key, or `ok` truthy-but-not-boolean) is the real unwrapped
      // shape, whose report is `body.report` directly.
      var report = body.ok === true ? body.data && body.data.report : body.report;
      if (report && report.meta && report.meta.pricing && report.meta.pricing.currency) {
        state.currency = report.meta.pricing.currency;
      }
      if (report && report.meta && report.meta.units) {
        state.units = report.meta.units;
      }
      if (report && report.meta && report.meta.projects) setKnownProjects(report.meta.projects);
      return { report: report, asOf: asOf };
    });
  }
  return state.reportPromises[key];
}

export function findSection(report, key) {
  if (!report || !Array.isArray(report.sections)) return null;
  for (var i = 0; i < report.sections.length; i++) {
    if (report.sections[i].key === key) return report.sections[i];
  }
  return null;
}

// The time the oldest figures drawn since the views were last dropped
// are from: the oldest X-Figures-As-Of a report-backed response has
// carried. Views keep what they drew and don't refetch on their own, so
// X-Figures-Refreshing isn't shown: "Redraw figures", a new window or
// a reload picks up the newer report.
// notify is set by shell.js, which redraws the sidebar's status line.
export var figures = { asOf: null, notify: null };

function noteFiguresAsOf(asOf) {
  if (figures.asOf && asOf >= figures.asOf) return;
  figures.asOf = asOf;
  if (figures.notify) figures.notify();
}

export function resetFiguresAsOf() {
  figures.asOf = null;
  if (figures.notify) figures.notify();
}

// A JSON POST to one of the service's write routes (docs/api.md),
// through the same envelope handling as every GET.
export function postJson(url, body) {
  return fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
