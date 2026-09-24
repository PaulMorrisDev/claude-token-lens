/* claude-token-lens service UI: page-capture.js
 *
 * The Capture tab: metrics-capture level, sample and metrics. Its saves
 * are the one config write the dashboard makes, to Token Lens's own
 * config.toml.
 */

import { clear, el } from "./core.js";
import { formatCell, thousands } from "./format.js";
import { loadInto, postJson } from "./api.js";
import { codeBlockWithCopy, errorNotice } from "./ui.js";
import { simpleTable } from "./grid.js";
import { tabHeading } from "./links.js";
import { capturePoll, showCaptureData } from "./shell.js";

// -- the Capture tab ---------------------------------------------------

export function renderCapture(panel) {
  clear(panel);
  tabHeading(panel, "capture");
  var container = el("div", { id: "capture-content" });
  panel.appendChild(container);
  loadInto(container, "/api/capture", function (data, target) {
    showCaptureData(data);
    renderCaptureData(data, target);
  });
}

function levelMetricIds(data) {
  var levelIds = {};
  (data.sections || []).forEach(function (section) {
    section.metrics.forEach(function (row) {
      if (row.kind === "level") levelIds[row.id] = true;
    });
  });
  return (data.config.metrics || []).filter(function (id) {
    return levelIds[id];
  });
}

function allRows(data) {
  var rows = [];
  (data.sections || []).forEach(function (section) {
    rows = rows.concat(section.metrics);
  });
  return rows;
}

function rowById(data, id) {
  return allRows(data).filter(function (row) {
    return row.id === id;
  })[0];
}

// What a change adds that makes Claude use more tokens: the metrics
// it switches on that ask Claude to read or write something.
function costlyAdditions(data, beforeIds, afterIds) {
  return afterIds.filter(function (id) {
    var row = rowById(data, id);
    return beforeIds.indexOf(id) === -1 && row && row.asks_claude;
  });
}

function titlesOf(data, ids) {
  return ids
    .map(function (id) {
      var row = rowById(data, id);
      return row ? row.title.toLowerCase() : id;
    })
    .join(", ");
}

// The cost warning again, before anything that uses more tokens. A
// native <dialog> when the browser has one, else confirm().
function confirmCapture(title, lines, onYes) {
  var text = lines.filter(Boolean);
  if (typeof HTMLDialogElement === "undefined") {
    if (window.confirm(title + "\n\n" + text.join("\n\n"))) onYes();
    return;
  }
  var dialog = el("dialog", { class: "capture-dialog", "aria-labelledby": "capture-dialog-title" });
  dialog.appendChild(el("h4", { id: "capture-dialog-title", text: title }));
  text.forEach(function (line) {
    dialog.appendChild(el("p", { text: line }));
  });
  var yes = el("button", { type: "button", class: "capture-button primary", text: "Go ahead" });
  var no = el("button", { type: "button", class: "capture-button", text: "Cancel" });
  dialog.appendChild(el("div", { class: "dialog-actions" }, [no, yes]));
  function close() {
    dialog.close();
    dialog.remove();
  }
  yes.addEventListener("click", function () {
    close();
    onYes();
  });
  no.addEventListener("click", close);
  dialog.addEventListener("cancel", function () {
    dialog.remove();
  });
  document.body.appendChild(dialog);
  dialog.showModal();
  no.focus();
}

function captureStatus(container) {
  return container.querySelector(".capture-status");
}

// Send a change; redraw the tab and the banner from the answer.
function postCapture(change, container, doneText) {
  var status = captureStatus(container);
  if (status) {
    clear(status);
    status.appendChild(el("p", { class: "loading", text: "Saving…" }));
  }
  postJson("/api/capture", change).then(function (result) {
    var body = result.body;
    if (!body || body.ok !== true) {
      if (!status) return;
      clear(status);
      status.appendChild(errorNotice(body && body.error));
      var commands = (body && body.error && body.error.commands) || [];
      if (commands.length) {
        status.appendChild(el("p", { text: "From a terminal on this machine:" }));
        status.appendChild(codeBlockWithCopy(commands.join("\n")));
      }
      return;
    }
    capturePoll.sig = null;
    showCaptureData(body.data);
    clear(container);
    renderCaptureData(body.data, container);
    var fresh = captureStatus(container);
    if (fresh) {
      fresh.appendChild(
        el("div", { class: "notice" }, [
          el("p", {
            text: body.data.changed
              ? doneText + " It takes effect in sessions and subagents started from now on."
              : "Nothing changed: it was already set that way.",
          }),
        ])
      );
    }
  });
}

function estimateLine(estimate) {
  if (!estimate) return "";
  var share = estimate.share_text ? ", " + estimate.share_text + " of what you spent" : "";
  return "About " + estimate.tokens_text + " tokens and " + (estimate.text || "") + share + ".";
}

function roughLine(rough) {
  var parts = [];
  if (rough.session_note) parts.push("about " + rough.session_note + " tokens of note when a session starts, is cleared or compacts");
  if (rough.subagent_note) parts.push("about " + rough.subagent_note + " when a subagent starts");
  if (rough.reply_tag) parts.push("about " + rough.reply_tag + " tokens of tag per reply");
  if (rough.report_tag) parts.push("about " + rough.report_tag + " per agent report");
  if (rough.tool_note) parts.push("about " + rough.tool_note + " after each large or web tool result");
  return parts.length ? "Roughly " + parts.join("; ") + "." : "";
}

function renderCaptureData(data, container) {
  var config = data.config || {};
  container.appendChild(el("div", { class: "notice capture-warning" }, [el("p", { text: data.warning })]));
  container.appendChild(el("div", { class: "capture-status", role: "status", "aria-live": "polite" }));

  // Where it stands now.
  container.appendChild(el("h3", { text: "Now" }));
  var now = [el("strong", { text: "Metrics capture: " + config.describe + ". " })];
  if (config.expired) now.push(el("span", { text: "Its end time has passed, so nothing is captured now. " }));
  if (config.projects_limited) now.push(el("span", { text: "Only some projects are captured ([capture] projects in config.toml). " }));
  container.appendChild(el("p", {}, now));
  var measured = data.measured;
  if (measured) {
    if (measured.sessions || measured.subagents) {
      var lines = [
        "Measured since " + (measured.since || "").slice(0, 10) + ": " + measured.sessions + " sessions and " + measured.subagents + " subagents captured.",
        "About " + thousands(measured.note_tokens) + " tokens of note and " + thousands(measured.tag_tokens) + " tokens of tag: " + (measured.text || "") + (measured.share_text ? ", " + measured.share_text + " of what those sessions cost." : "."),
      ];
      if (measured.coverage_text) {
        lines.push("Claude tagged " + measured.coverage_text + " of your messages" + (measured.report_coverage_pct !== null ? " and " + formatCell(measured.report_coverage_pct, "pct") + " of agent reports." : "."));
      }
      container.appendChild(el("ul", { class: "notes" }, lines.map(function (line) {
        return el("li", { text: line });
      })));
      var scopeNames = { main: "Main session", subagent: "Subagents", tool: "After tool results", brief: "Agent briefs" };
      var scopeRows = Object.keys(measured.scopes || {}).map(function (key) {
        var scope = measured.scopes[key];
        return [scopeNames[key] || key, thousands(scope.note_tokens), thousands(scope.tag_tokens), scope.text];
      });
      if (scopeRows.length) {
        container.appendChild(
          simpleTable(
            [{ label: "Where" }, { label: "Note tokens" }, { label: "Tag tokens" }, { label: "Cost" }],
            scopeRows,
            "Where the tokens went"
          )
        );
      }
    } else {
      container.appendChild(el("p", { text: "No captured sessions yet: the note is added to sessions and subagents started after capture was turned on." }));
    }
  }
  if (data.roi && data.roi.cost && data.roi.cost.usd > 0) {
    // UX-2: roi.cost.text/roi.value.text already carry their own
    // "about" (capture_view.py's _roi) -- not repeated here, or a
    // subscription's would double into "about about X%...".
    var roiText = data.roi.measured
      ? "Capture cost " + data.roi.cost.text + "; suggestions that rely on it are worth " + data.roi.value.text + "."
      : "Capture cost " + data.roi.cost.text + "; nothing measured yet relies on it.";
    container.appendChild(el("p", { class: "notes", text: roiText }));
  }
  if (data.history && data.history.sessions) {
    container.appendChild(
      el("p", {
        class: "notes",
        text: "Estimates replay your last " + data.history.days + " days: " + data.history.sessions + " sessions and " + data.history.subagents + " subagents, as if capture had been on.",
      })
    );
  }
  if (data.billing && data.billing.basis) container.appendChild(el("p", { class: "notes", text: data.billing.basis }));

  // Hook entries Claude Code needs to run for the chosen metrics.
  var hooks = data.hooks || {};
  if (hooks.ok === false && hooks.blocked_by) {
    // A settings policy stops Claude Code running these hooks at all;
    // 'capture connect' can't change that, so it isn't offered.
    container.appendChild(el("div", { class: "notice error" }, [el("p", { text: hooks.summary })]));
  } else if (hooks.ok === false) {
    container.appendChild(
      el("div", { class: "notice error" }, [
        el("p", { text: hooks.summary }),
        (hooks.missing || []).length > 1
          ? el("ul", {}, hooks.missing.map(function (entry) {
              return el("li", { text: "Missing: " + entry });
            }))
          : null,
        el("p", { text: "This page never changes Claude Code's settings.json. This command shows the change and asks before making it; it backs the file up first, and 'claude-token-lens capture remove' takes the entries out again." }),
        codeBlockWithCopy(hooks.connect_command),
      ])
    );
  }

  renderCaptureLevels(data, container);
  renderCaptureControls(data, container);
  renderCaptureMetrics(data, container);
}

function renderCaptureLevels(data, container) {
  var config = data.config || {};
  container.appendChild(el("h3", { text: "Level" }));
  container.appendChild(el("p", { class: "notes", text: "Each level adds to the one before. Estimates are per week, from your own sessions" + (config.sample < 100 ? ", with " + config.sample + "% of sessions captured" : "") + "." }));
  var grid = el("div", { class: "capture-levels", role: "list" });
  (data.levels || []).forEach(function (level) {
    var card = el("div", { class: "capture-level" + (level.current ? " current" : ""), role: "listitem" });
    var head = el("div", { class: "capture-level-head" }, [el("strong", { text: level.title })]);
    if (level.current) head.appendChild(el("span", { class: "badge badge-suggested", text: "Current" }));
    card.appendChild(head);
    card.appendChild(el("p", { text: level.summary }));
    var cost;
    if (level.id === "off") cost = "No tokens.";
    else if (!level.asks_claude && level.metrics.length) cost = "No Claude tokens.";
    else cost = estimateLine(level.estimate) || roughLine(level.rough);
    if (cost) card.appendChild(el("p", { class: "capture-cost", text: cost }));
    if (level.adds && level.adds.length) card.appendChild(el("p", { class: "notes", text: "Adds: " + level.adds.join(", ") + "." }));
    if (level.id !== "custom" && !level.current) {
      var button = el("button", { type: "button", class: "capture-button", text: level.id === "off" ? "Switch off" : "Switch to " + level.title });
      button.addEventListener("click", function () {
        var before = config.metrics || [];
        var added = costlyAdditions(data, before, level.metrics);
        var send = function () {
          postCapture({ level: level.id }, container, level.id === "off" ? "Switched off." : "Saved: " + level.title + ".");
        };
        if (!added.length) {
          send();
          return;
        }
        confirmCapture("Use more tokens for metrics capture?", [
          data.warning,
          "Switching to " + level.title + " adds " + titlesOf(data, added) + ".",
          level.estimate ? "At the pace of your last two weeks: " + estimateLine(level.estimate) : roughLine(level.rough),
        ], send);
      });
      card.appendChild(button);
    }
    grid.appendChild(card);
  });
  container.appendChild(grid);
}

var CAPTURE_ENDS = [
  { value: "", label: "No end" },
  { value: "1", label: "1 day from now" },
  { value: "3", label: "3 days from now" },
  { value: "7", label: "7 days from now" },
  { value: "14", label: "14 days from now" },
  { value: "30", label: "30 days from now" },
];

function renderCaptureControls(data, container) {
  var config = data.config || {};
  container.appendChild(el("h3", { text: "How much and for how long" }));
  if (!config.on) {
    container.appendChild(el("p", { class: "notes", text: "Sampling and an end time apply once capture is on." }));
    return;
  }
  var form = el("div", { class: "capture-controls" });
  var sampleId = "capture-sample";
  var sample = el("select", { id: sampleId });
  (data.samples || []).forEach(function (value) {
    sample.appendChild(el("option", { value: String(value), text: value === 100 ? "Every session" : value + "% of sessions" }));
  });
  sample.value = String(config.sample);
  sample.addEventListener("change", function () {
    var value = parseInt(sample.value, 10);
    var send = function () {
      postCapture({ sample: value }, container, "Saved: " + (value === 100 ? "every session" : value + "% of sessions") + ".");
    };
    if (value > config.sample) {
      confirmCapture("Capture more sessions?", [data.warning, "Capturing " + value + "% of sessions instead of " + config.sample + "% uses about " + (value / config.sample).toFixed(1) + " times the tokens."], send);
      sample.value = String(config.sample);
    } else {
      send();
    }
  });
  form.appendChild(el("label", { for: sampleId, text: "Sessions captured" }));
  form.appendChild(sample);
  form.appendChild(el("p", { class: "notes", text: "A session is in or out for its whole life, and its subagents with it. Fewer sessions cost less and still give a fair picture over time." }));

  var endId = "capture-end";
  var end = el("select", { id: endId });
  // The end already set, as the first choice (the others count from now).
  if (config.until) end.appendChild(el("option", { value: "keep", text: "On " + config.until.slice(0, 16).replace("T", " ") + " (UTC)" }));
  CAPTURE_ENDS.forEach(function (opt) {
    end.appendChild(el("option", { value: opt.value, text: opt.label }));
  });
  end.value = config.until ? "keep" : "";
  end.addEventListener("change", function () {
    if (end.value === "keep") return;
    var until = "";
    if (end.value) until = new Date(Date.now() + parseInt(end.value, 10) * 86400000).toISOString().slice(0, 19) + "+00:00";
    postCapture({ until: until }, container, until ? "Saved: capture switches itself off on " + until.slice(0, 10) + "." : "Saved: no end time.");
  });
  form.appendChild(el("label", { for: endId, text: "Switch itself off" }));
  form.appendChild(end);
  form.appendChild(el("p", { class: "notes", text: "A time-box keeps the cost bounded: capture switches itself off and the banner says so." }));
  container.appendChild(form);
}

function renderCaptureMetrics(data, container) {
  var config = data.config || {};
  container.appendChild(el("h3", { text: "Metrics" }));
  container.appendChild(el("p", { class: "notes", text: "What each one captures, what Claude writes for it, why it helps, and what it costs. Ticking one here picks your own set (Custom)." }));
  (data.sections || []).forEach(function (section) {
    container.appendChild(el("h4", { text: section.title }));
    var list = el("div", { class: "metric-list" });
    section.metrics.forEach(function (row) {
      list.appendChild(renderMetricRow(row, data, container));
    });
    container.appendChild(list);
  });
}

function renderMetricRow(row, data, container) {
  var config = data.config || {};
  var box = el("div", { class: "metric-row" + (row.on ? " on" : "") });
  var id = "metric-" + row.id;
  var head = el("div", { class: "metric-head" });
  var toggle = el("input", { type: "checkbox", id: id, checked: row.on, disabled: !row.toggle });
  head.appendChild(toggle);
  head.appendChild(el("label", { for: id, class: "metric-title", text: row.title }));
  if (!row.toggle) head.appendChild(el("span", { class: "badge", text: "Always measured" }));
  if (row.needs_hook) head.appendChild(el("span", { class: "badge severity-action", text: "Needs a hook entry" }));
  if (row.needs_install) head.appendChild(el("span", { class: "badge severity-action", text: "Needs installing" }));
  if (row.enough) head.appendChild(el("span", { class: "badge badge-suggested", text: "Enough collected" }));
  box.appendChild(head);
  box.appendChild(el("p", { class: "metric-what", text: row.what }));
  var facts = el("dl", { class: "metric-facts" });
  function fact(label, value, cls) {
    if (!value) return;
    facts.appendChild(el("dt", { text: label }));
    facts.appendChild(el("dd", { class: cls || null, text: value }));
  }
  fact("Why", row.why);
  if (row.tag) fact("Claude writes", row.tag, "metric-tag");
  if (row.powers && row.powers.length) fact("Helps with", row.powers.join(", "));
  var cost;
  if (!row.asks_claude) cost = row.kind === "free" ? "No Claude tokens: a hook logs it to a local file." : "No tokens.";
  else if (row.estimate) cost = (row.on ? "Saves about " : "Adds about ") + row.estimate.text + (row.on ? " if switched off." : ".");
  if (row.actual) cost = (cost ? cost + " " : "") + (row.actual_label || "Since it was turned on") + ": " + row.actual.text + ".";
  fact("Cost", cost);
  if (row.target) fact("Collected", row.answers + " of " + row.target + " answers" + (row.enough ? (row.asks_claude ? ": enough for firm suggestions, so switching it off would save its cost." : ": enough for firm suggestions.") : "."));
  box.appendChild(facts);
  if (row.needs_install) {
    // The dashboard never writes Claude Code's folder: the CLI adds the
    // skill after showing it and asking.
    box.appendChild(el("p", { class: "notes", text: row.install_note + ". The dashboard doesn't write Claude Code's folder, so add it from a terminal:" }));
    box.appendChild(codeBlockWithCopy(row.install_command));
  }
  if (row.statusline_note) box.appendChild(el("p", { class: "notes", text: row.statusline_note }));

  toggle.addEventListener("change", function () {
    var turningOn = toggle.checked;
    toggle.checked = row.on; // redrawn from the server's answer
    var change;
    var dropped = [];
    if (row.kind === "level") {
      var current = levelMetricIds(data);
      var next;
      if (turningOn) {
        next = current.concat([row.id]);
      } else {
        dropped = allRows(data)
          .filter(function (other) {
            return other.on && other.id !== row.id && (other.requires || []).indexOf(row.id) !== -1;
          })
          .map(function (other) {
            return other.id;
          });
        next = current.filter(function (id) {
          return id !== row.id && dropped.indexOf(id) === -1;
        });
      }
      change = { metrics: next };
    } else {
      var key = row.kind === "coaching" ? "coaching" : "feedback";
      var list = (config[key] || []).slice();
      change = {};
      change[key] = turningOn ? list.concat([row.id]) : list.filter(function (id) {
        return id !== row.id;
      });
    }
    var done = (turningOn ? "Switched on: " : "Switched off: ") + row.title.toLowerCase() + (dropped.length ? ", and " + titlesOf(data, dropped) + ", which need it" : "") + ".";
    var send = function () {
      postCapture(change, container, done);
    };
    if (turningOn && row.asks_claude) {
      var needs = (row.requires || []).filter(function (id) {
        return (config.metrics || []).indexOf(id) === -1;
      });
      confirmCapture("Use more tokens for metrics capture?", [
        data.warning,
        "Switching on " + row.title.toLowerCase() + (needs.length ? " (with " + titlesOf(data, needs) + ", which it needs)" : "") + (row.estimate ? " adds about " + row.estimate.text + "." : "."),
      ], send);
    } else {
      send();
    }
  });
  return box;
}
