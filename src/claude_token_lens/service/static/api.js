/* claude-token-lens service UI: api.js
 *
 * Fetching from the service: the {ok, data} envelope, the per-window
 * report cache and the figures-as-of stamp.
 */

import { clear, state } from "./core.js";
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
          return { httpStatus: res.status, body: body, asOf: asOf };
        });
    })
    .catch(function (err) {
      return { httpStatus: 0, body: { ok: false, error: { code: "network_error", message: String(err && err.message ? err.message : err) } } };
    });
}

/**
 * Fetch `url`, replacing `container`'s contents with `loading()` while
 * in flight, then either `render(data, container)` on `ok: true`, or
 * an inline error notice on `ok: false` / a network failure. Never
 * throws -- this is the one place every view's data flow funnels
 * through, per docs/ui.md's "or shows the error.message inline...
 * never a raw stack trace" contract.
 */
export function loadInto(container, url, render, options) {
  clear(container);
  container.appendChild(loadingNode());
  return fetchJson(url, options).then(function (result) {
    clear(container);
    var body = result.body;
    if (!body || body.ok !== true) {
      container.appendChild(errorNotice(body && body.error));
      return null;
    }
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
