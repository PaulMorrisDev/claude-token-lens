/* claude-token-lens service UI: ui.js
 *
 * Small shared pieces: notices, empty states, badges, copyable commands
 * and fix blocks.
 */

import { el } from "./core.js";
import { icon } from "./icons.js";

export function errorNotice(error) {
  var code = (error && error.code) || "error";
  var message = (error && error.message) || "Something went wrong.";
  return el("div", { class: "notice error", role: "alert" }, [
    el("strong", { text: "[" + code + "] " }),
    el("span", { text: message }),
  ]);
}

export function loadingNode(label) {
  return el("p", { class: "loading", text: label || "Loading…" });
}

// UX-6/9: used to fire-and-forget navigator.clipboard.writeText and
// always flip the button to "Copied" regardless of what happened --
// an insecure context, a denied permission or any other rejection of
// the Promise it returns (not just a missing API, which the old
// try/catch did cover) left the button falsely claiming success.
// Returns a Promise<boolean> so the caller can tell the two apart.
function copyToClipboard(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return false; }
      );
    }
  } catch (err) {
    /* clipboard unavailable (insecure context, permissions) -- fall through */
  }
  return Promise.resolve(false);
}

export function codeBlockWithCopy(text) {
  var wrap = el("div", { class: "code-block" });
  var pre = el("pre", { text: text || "" });
  var button = el("button", { type: "button", class: "copy-button", text: "Copy" });
  button.addEventListener("click", function () {
    copyToClipboard(text || "").then(function (ok) {
      button.textContent = ok ? "Copied" : "Couldn't copy - select the text above";
      setTimeout(function () {
        button.textContent = "Copy";
      }, 1500);
    });
  });
  wrap.appendChild(pre);
  wrap.appendChild(button);
  return wrap;
}

// Recommendation.agent_type values that are not agent names.
export var AGENT_LABELS = {
  "top-level": "Your main session",
  unknown: "Subagents with no recorded type",
  "workflow-subagent": "Workflow subagents",
};

// Recommendation.scope, in plain words.
export var SCOPE_LABELS = {
  user: "your user settings, every project",
  repo: "this project's settings or agent files",
  managed: "set by your organisation's policy",
};

// Shown after every way of making a change (fixes.RESTART_NOTE; a test
// keeps the two the same).
var RESTART_NOTE =
  "Restart Claude Code to pick up the change. It reads settings and agent files when it starts, so a " +
  "session that is already open keeps the old ones (claude --continue picks your last conversation back up).";

export function restartNote() {
  return el("p", { class: "notice restart-note", text: RESTART_NOTE });
}

// One fixes.build_fix entry: the plain explainer, then the prompt
// for Claude, (for a plain setting) the dry-run command and the
// reminder to restart Claude Code.
function fixTitle(fix) {
  if (fix.title) return fix.title;
  // Same rule as render/tables.py's fix_subject.
  if (!fix.key) return "What you're changing";
  var who = fix.agent ? " for " + fix.agent : fix.key === "model" ? " for your main session" : "";
  return "What you're changing: " + fix.key + who;
}

export function renderFix(fix, collapsed) {
  var box = el(collapsed ? "details" : "div", { class: "fix" });
  if (collapsed) box.appendChild(el("summary", { text: fixTitle(fix) }));
  if (fix.explainer && fix.explainer.length) {
    if (!collapsed) box.appendChild(el("h4", { text: fixTitle(fix) }));
    var list = el("dl", { class: "fix-explainer" });
    fix.explainer.forEach(function (pair) {
      list.appendChild(el("dt", { text: pair[0] }));
      list.appendChild(el("dd", { text: pair[1] }));
    });
    box.appendChild(list);
  }
  // UX-8: a purely informational workflow card (fixes.build_fixes) has
  // an explainer but no prompt -- nothing to ask Claude to do.
  if (fix.prompt) {
    box.appendChild(el("h4", { text: "Ask Claude to do it" }));
    box.appendChild(codeBlockWithCopy(fix.prompt));
  }
  if (fix.command) {
    box.appendChild(el("h4", { text: "Or run this command" }));
    box.appendChild(
      el("p", { class: "notes", text: "It shows the change without writing anything. Run it again without --dry-run to make the change; the output tells you how to undo it." })
    );
    if (fix.command_warning) {
      box.appendChild(el("p", { class: "fix-warning", text: fix.command_warning }));
    }
    box.appendChild(codeBlockWithCopy(fix.command));
  }
  box.appendChild(restartNote());
  return box;
}

// ======================================================================
// Shared pieces for the Quick actions, Context files and Profiles
// additions: a plain table of display strings, and a status badge.
// ======================================================================

// P4 leftover / UX-6/9: one consistent "not enough data yet" box,
// instead of each view building its own ad hoc paragraph (renderImpact,
// renderBacktest and the quick actions' no_data cards used to each
// have a slightly different one). `gate` is the structured
// {reason, have, need} object some routes now carry (see api.py's
// _min_sessions_gate, currently /api/impact) -- when given, its
// numbers are appended so the box reads "2 of 3 sessions so far"
// rather than only the prose message repeating what "not enough" means.
export function emptyState(message, gate) {
  var box = el("div", { class: "placeholder-box empty-state" });
  var text = message || "Not enough data yet.";
  if (gate && typeof gate.have === "number" && typeof gate.need === "number") {
    text += " (" + gate.have + " of " + gate.need + " so far.)";
  }
  box.appendChild(el("p", { text: text }));
  return box;
}

var CHECK_STATUS = {
  act: { label: "Worth a look", cls: "severity-action", icon: "critical" },
  ok: { label: "Nothing to do", cls: "severity-good", icon: "success" },
  no_data: { label: "Not enough data", cls: "severity-info", icon: "info" },
};

export function statusBadge(status) {
  var info = CHECK_STATUS[status] || { label: status, cls: "severity-info", icon: "info" };
  var badge = el("span", { class: "severity-badge " + info.cls });
  badge.appendChild(icon(info.icon, { size: 14 }));
  badge.appendChild(el("span", { text: info.label }));
  return badge;
}

export function renderTips(tips, container) {
  if (!tips || !tips.length) return;
  container.appendChild(el("h4", { text: "Habits that help" }));
  container.appendChild(
    el(
      "ul",
      { class: "notes" },
      tips.map(function (tip) {
        return el("li", null, [el("strong", { text: tip.title + ". " }), el("span", { text: tip.text })]);
      })
    )
  );
}

export function renderFixList(fixes, container) {
  (fixes || []).forEach(function (fix, i) {
    container.appendChild(renderFix(fix, i > 0));
  });
}
