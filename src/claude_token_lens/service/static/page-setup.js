/* claude-token-lens service UI: page-setup.js
 *
 * The Config and Profiles tabs, with what each change did.
 */

import { clear, el, state } from "./core.js";
import { fetchJson, findSection, loadInto, loadReport, postJson, withWindow } from "./api.js";
import { codeBlockWithCopy, emptyState, errorNotice, loadingNode, restartNote } from "./ui.js";
import { renderMappedSections, renderPlacedTables, renderSectionGeneric, simpleTable } from "./grid.js";
import { tabHeading, tabLink } from "./links.js";

// ======================================================================
// Config tab (config/scorecard sections + config-diff + baseline)
// ======================================================================

export function renderConfig(panel) {
  clear(panel);
  tabHeading(panel, "config");

  var driftContainer = el("div", { id: "config-drift" });
  panel.appendChild(el("h3", { text: "Your settings and how they changed" }));
  panel.appendChild(driftContainer);
  loadInto(driftContainer, withWindow("/api/config-diff?auto_keys=1"), renderConfigDiff);

  var baselineContainer = el("div", { id: "config-baseline" });
  panel.appendChild(el("h3", { text: "Latest baseline" }));
  panel.appendChild(baselineContainer);
  loadInto(baselineContainer, "/api/baseline", renderBaseline);

  var sectionContainer = el("div", { id: "config-sections" });
  panel.appendChild(sectionContainer);
  sectionContainer.appendChild(loadingNode());
  loadReport().then(function (result) {
    clear(sectionContainer);
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    // The config section's own tables came from /api/config-diff above.
    renderMappedSections(result.report, "config", sectionContainer, ["config"]);
  });
}

function renderConfigDiff(data, container) {
  if (data && Array.isArray(data.tables)) {
    renderSectionGeneric(container, data, state.currency, "config-diff");
  } else if (Array.isArray(data) && data.length && data[0] && Array.isArray(data[0].tables)) {
    data.forEach(function (section, i) {
      renderSectionGeneric(container, section, state.currency, "config-diff-" + i);
    });
  } else if (Array.isArray(data) && data.length) {
    renderPlacedTables(container, data, state.currency, "config-diff");
  } else {
    container.appendChild(el("p", { class: "notice", text: "No config drift observed across the current snapshot window." }));
  }
}

// v0.3: GET /api/baseline now returns {"baseline", "history",
// "capture_status"} (docs/api.md) rather than a bare list -- the
// shape-defensive fallbacks docs/ui.md's own "Shape-defensive
// rendering" note flagged for trimming once api.py landed are gone;
// this reads that shape directly.
function renderBaseline(data, container) {
  var status = data && data.capture_status;
  if (status) {
    container.appendChild(el("p", { class: "notice", text: status.summary || "" }));
  }

  var latest = data && data.baseline;
  if (!latest) {
    container.appendChild(el("p", { class: "notice", text: "No baseline captured yet (see `claude-token-lens baseline`)." }));
    return;
  }
  if (status && status.started && !status.complete) {
    container.appendChild(
      el("p", { class: "notice", text: "Capture window open: provisional -- this baseline may change once capture completes." })
    );
  }

  var rows = data.history && data.history.length ? data.history : [latest];
  var table = el("table");
  var head = el("thead", null, [
    el("tr", null, ["Project", "Window start", "Window end", "Archetype", "Captured"].map(function (h) {
      return el("th", { text: h });
    })),
  ]);
  var body = el(
    "tbody",
    null,
    rows.map(function (row) {
      return el("tr", null, [
        // Nit 27: Store.baselines() joins in the owning project's
        // (redacted) slug specifically so this table doesn't have to
        // show the meaningless projects.id primary key -- render that
        // instead of the raw project_id the route used to be the only
        // thing available here.
        el("td", { text: row.project_slug || "-" }),
        el("td", { text: row.window_start || "-" }),
        el("td", { text: row.window_end || "-" }),
        el("td", { text: row.archetype || "-" }),
        el("td", { text: row.created_at || "-" }),
      ]);
    })
  );
  table.appendChild(head);
  table.appendChild(body);
  container.appendChild(table);
}

// ======================================================================
// Profiles tab
// ======================================================================
//
// v0.3: GET /api/profiles now returns {"profiles": [...each tagged
// source: "catalogue"|"user"...], "suggested_profile_id"} (docs/api.md)
// rather than a bare list of indexed (user-only) profiles, and GET
// /api/profiles/<id>/diff is a real computation (profiles/diff.py)
// rather than a 501 stub -- see that route's own docstring in api.py.

// Filled from GET /api/profile-schema on first use: every key a
// profile may set, with its label, type and plain-English text.
var profileSchemaPromise = null;

function loadProfileSchema() {
  if (!profileSchemaPromise) {
    profileSchemaPromise = fetchJson("/api/profile-schema").then(function (result) {
      return result.body && result.body.ok === true ? result.body.data : null;
    });
  }
  return profileSchemaPromise;
}

var PROFILE_SCOPE_LABELS = {
  user: "Your user settings, every project",
  "project-local": "This project, on your machine only",
  repo: "This project, shared with everyone who works in it",
};

export function renderProfiles(panel) {
  clear(panel);
  tabHeading(panel, "profiles");

  // -- save what you have now, so you can compare or go back later --
  var saveCurrentRow = el("div", { class: "profile-actions" });
  var saveCurrentBtn = el("button", { type: "button", id: "profiles-save-current", text: "Save my current settings as a profile" });
  var saveCurrentStatus = el("span", { class: "notes", role: "status" });
  saveCurrentRow.appendChild(saveCurrentBtn);
  saveCurrentRow.appendChild(saveCurrentStatus);
  panel.appendChild(saveCurrentRow);
  panel.appendChild(
    el("p", {
      class: "notes",
      text: "This saves a copy in this tool's own profile folder. It never changes your Claude Code settings.",
    })
  );

  var listContainer = el("div", { id: "profiles-list" });
  var detailContainer = el("div", { id: "profiles-diff" });
  var formContainer = el("div", { id: "profiles-save-form" });

  var creatorContainer = el("div", { id: "profiles-create" });
  var impactContainer = el("div", { id: "profiles-impact" });
  var setupsContainer = el("div", { id: "profiles-setups" });

  panel.appendChild(el("h3", { text: "Create a profile" }));
  panel.appendChild(creatorContainer);
  panel.appendChild(el("h3", { text: "Best setup for each kind of task" }));
  panel.appendChild(setupsContainer);
  renderTaskSetups(setupsContainer);
  panel.appendChild(el("h3", { text: "Your profiles and the built-in ones" }));
  panel.appendChild(listContainer);
  panel.appendChild(detailContainer);
  panel.appendChild(el("h3", { text: "Your changes and what they did" }));
  panel.appendChild(impactContainer);
  loadInto(impactContainer, "/api/impact", renderImpact);
  var backtestContainer = el("div", { id: "profiles-backtest" });
  panel.appendChild(el("h3", { text: "Did your estimates come true?" }));
  panel.appendChild(backtestContainer);
  loadInto(backtestContainer, "/api/backtest", renderBacktest);
  var editorDetails = el("details", { class: "advanced-detail" });
  editorDetails.appendChild(el("summary", { text: "Edit settings directly" }));
  editorDetails.appendChild(formContainer);
  panel.appendChild(editorDetails);

  var editorShown = false;

  function refreshList() {
    loadInto(listContainer, "/api/profiles", function (data, container) {
      renderProfilesList(data, container, detailContainer);
      // Built once, so a save's status line stays on screen.
      if (!editorShown) {
        editorShown = true;
        renderProfileEditor(formContainer, (data && data.profiles) || [], refreshList);
      }
    });
  }

  var replaceBtn = el("button", { type: "button", text: "Replace the saved copy", hidden: true });
  saveCurrentRow.appendChild(replaceBtn);

  function saveCurrent(replace) {
    saveCurrentBtn.disabled = true;
    replaceBtn.hidden = true;
    saveCurrentStatus.textContent = "Saving…";
    fetchJson("/api/profiles/from-current" + (replace ? "?replace=1" : ""), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(function (result) {
      saveCurrentBtn.disabled = false;
      var body = result.body;
      if (result.httpStatus === 409 && /already exists/.test((body && body.error && body.error.message) || "")) {
        // Saved before: ask before overwriting that copy.
        saveCurrentStatus.textContent = "You saved your settings before. Replace that copy with today's settings?";
        replaceBtn.hidden = false;
        return;
      }
      if (!body || body.ok !== true) {
        saveCurrentStatus.textContent = (body && body.error && body.error.message) || "Could not save your settings.";
        return;
      }
      var skipped = body.data.skipped_managed || [];
      saveCurrentStatus.textContent =
        'Saved as "' + (body.data.name || body.data.id) + '".' +
        (skipped.length ? " Left out, because your organisation's policy sets them: " + skipped.join(", ") + "." : "");
      refreshList();
    });
  }
  saveCurrentBtn.addEventListener("click", function () {
    saveCurrent(false);
  });
  replaceBtn.addEventListener("click", function () {
    saveCurrent(true);
  });

  renderProfileCreator(creatorContainer, refreshList);
  refreshList();
}

function renderProfilesList(data, container, detailContainer) {
  var profiles = (data && data.profiles) || [];
  var suggestedId = data && data.suggested_profile_id;
  if (!profiles.length) {
    container.appendChild(el("p", { class: "notice", text: "No profiles available." }));
    return;
  }
  var cards = el("div", { class: "profile-cards" });
  profiles.forEach(function (profile) {
    var isSuggested = Boolean(suggestedId) && profile.id === suggestedId;
    var card = el("article", { class: "profile-card" + (isSuggested ? " profile-card-suggested" : "") });
    var head = el("div", { class: "profile-card-head" }, [el("h4", { text: profile.name || profile.id })]);
    if (isSuggested) head.appendChild(el("span", { class: "badge badge-suggested", text: "Suggested for you" }));
    card.appendChild(head);

    var meta = [profile.source === "catalogue" ? "Built in" : "Yours"];
    if (profile.for && profile.for.length) meta.push("for " + profile.for.join(", ").replace(/-/g, " "));
    if (profile.updated_at) meta.push("saved " + String(profile.updated_at).slice(0, 10));
    card.appendChild(el("p", { class: "profile-card-meta", text: meta.join(" · ") }));

    var summary = el("p", { class: "profile-card-summary" });
    card.appendChild(summary);
    // "Changes 3 settings: Model, Effort level, ..." from the profile
    // and the schema's labels (catalogue notes are for maintainers).
    Promise.all([fetchJson("/api/profiles/" + encodeURIComponent(profile.id)), loadProfileSchema()]).then(function (results) {
      var body = results[0].body;
      if (!body || body.ok !== true) return;
      var p = body.data;
      var labels = {};
      ((results[1] && results[1].settings) || []).concat((results[1] && results[1].agents) || []).forEach(function (lever) {
        labels[lever.key] = lever.label;
      });
      var names = Object.keys(p.settings || {}).map(function (key) {
        return labels[key] || key;
      });
      Object.keys(p.agents || {}).forEach(function (agent) {
        Object.keys(p.agents[agent]).forEach(function (key) {
          names.push((labels[key] || key) + " (" + agent + ")");
        });
      });
      names = names.concat(Object.keys(p.env || {}));
      var count = p.setting_count || names.length;
      summary.textContent =
        "Changes " + count + (count === 1 ? " setting" : " settings") + (names.length ? ": " + names.join(", ") + "." : ".");
    });

    var button = el("button", { type: "button", text: "Show what it changes" });
    button.addEventListener("click", function () {
      renderProfileDetail(profile, detailContainer);
      detailContainer.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    card.appendChild(button);
    cards.appendChild(card);
  });
  container.appendChild(cards);
  if (suggestedId) {
    container.appendChild(el("p", { class: "notes", text: "Suggested for you: the profile your latest baseline matches best." }));
  }
}

function _diffRowValue(value) {
  if (value === null || value === undefined) return "not set";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "(empty list)";
  if (typeof value === "boolean") return value ? "on" : "off";
  return String(value);
}

// One table for every key the profile sets: plain label, now, after,
// and the file it would be written to under the chosen scope.
function renderDiffRowsTable(rows) {
  var table = el("table", { class: "profile-diff-table" });
  table.appendChild(
    el("thead", null, [
      el(
        "tr",
        null,
        ["Setting", "Now", "After", "Set in"].map(function (h) {
          return el("th", { text: h });
        })
      ),
    ])
  );
  table.appendChild(
    el(
      "tbody",
      null,
      rows.map(function (row) {
        var same = JSON.stringify(row.current_value) === JSON.stringify(row.proposed_value);
        var label = row.label || row.setting || row.key;
        if (row.agent) label += " (" + row.agent + " agent)";
        var setting = el("td", null, [el("span", { text: label })]);
        if (row.description) setting.appendChild(el("div", { class: "cell-hint", text: row.description }));
        var after = el("td", { text: _diffRowValue(row.proposed_value) });
        var where = el("td", { text: row.where || row.target_file || "-" });
        if (row.managed) {
          after.textContent = _diffRowValue(row.current_value);
          where.textContent = "Locked by your organisation's policy; not changed";
        } else if (same) {
          where.textContent = "Already set; no change";
        }
        return el("tr", { class: same || row.managed ? "row-unchanged" : "" }, [
          setting,
          el("td", { text: _diffRowValue(row.current_value) }),
          after,
          where,
        ]);
      })
    )
  );
  return table;
}

function renderProfileDetail(profile, container) {
  clear(container);
  container.appendChild(el("h3", { text: "What " + (profile.name || profile.id) + " changes" }));
  var scopeRow = el("div", { class: "pager" });
  scopeRow.appendChild(el("label", { for: "profile-scope", text: "Target file:" }));
  var scopeSelect = el("select", { id: "profile-scope" });
  Object.keys(PROFILE_SCOPE_LABELS).forEach(function (scope) {
    scopeSelect.appendChild(el("option", { value: scope, text: PROFILE_SCOPE_LABELS[scope] }));
  });
  scopeRow.appendChild(scopeSelect);
  container.appendChild(scopeRow);
  var estimate = el("div", { class: "profile-estimate" });
  container.appendChild(estimate);
  renderProfileEstimate(profile, estimate);
  var body = el("div");
  container.appendChild(body);
  function load() {
    loadInto(
      body,
      "/api/profiles/" + encodeURIComponent(profile.id) + "/diff?scope=" + encodeURIComponent(scopeSelect.value),
      renderProfileDiff
    );
  }
  scopeSelect.addEventListener("change", load);
  load();
}

function renderProfileDiff(data, container) {
  (data.notes || []).forEach(function (note) {
    container.appendChild(el("p", { class: "notice", text: note }));
  });

  var rows = (data.settings || []).concat(data.agents || [], data.env || []);
  if (rows.length) {
    container.appendChild(renderDiffRowsTable(rows));
  } else {
    container.appendChild(el("p", { class: "notice", text: "This profile sets nothing." }));
  }

  var box = el("div", { class: "fix" });
  box.appendChild(el("h5", { text: "Ask Claude to do it" }));
  box.appendChild(el("p", { class: "notes", text: "Paste this into Claude Code. It shows you the diff before saving anything." }));
  box.appendChild(codeBlockWithCopy(data.prompt));
  box.appendChild(el("h5", { text: "Or run this command" }));
  box.appendChild(
    el("p", {
      class: "notes",
      text: "It shows the change without writing anything. Run it again without --dry-run to make the change; the output tells you how to undo it.",
    })
  );
  box.appendChild(codeBlockWithCopy(data.dry_run_command));
  box.appendChild(restartNote());
  box.appendChild(el("h5", { text: "Or try it for one session" }));
  box.appendChild(
    el("p", {
      class: "notes",
      text:
        "It saves these settings to a file in this tool's own folder and prints the command that starts " +
        "Claude Code with them on top of yours. Your settings files aren't changed." +
        ((data.agents || []).length || (data.env || []).length
          ? " A one-session trial carries the settings only, not the agent or environment changes."
          : ""),
    })
  );
  box.appendChild(codeBlockWithCopy(data.launch_command));
  container.appendChild(box);

  var raw = el("details", { class: "advanced-detail" });
  raw.appendChild(el("summary", { text: "Show the file changes" }));
  raw.appendChild(el("pre", { text: data.diff || "(no changes against your current settings)" }));
  container.appendChild(raw);
}

// -- profile editor: a form built from /api/profile-schema -----------

function leverInput(lever, idPrefix) {
  var id = idPrefix + lever.key.replace(/[^A-Za-z0-9]/g, "-");
  var input;
  if (lever.kind === "enum" || lever.kind === "bool") {
    input = el("select", { id: id });
    input.appendChild(el("option", { value: "", text: "Leave as it is" }));
    var values = lever.kind === "bool" ? ["true", "false"] : lever.values || [];
    values.forEach(function (v) {
      var text = lever.kind === "bool" ? (v === "true" ? "On" : "Off") : v;
      input.appendChild(el("option", { value: v, text: text }));
    });
  } else if (lever.kind === "int") {
    input = el("input", { type: "number", id: id, placeholder: "Leave as it is" });
    if (lever.min !== null && lever.min !== undefined) input.min = String(lever.min);
    if (lever.max !== null && lever.max !== undefined) input.max = String(lever.max);
  } else {
    input = el("input", {
      type: "text",
      id: id,
      placeholder: lever.kind === "list[str]" ? "Comma-separated; leave empty to keep" : "Leave empty to keep",
    });
  }
  var label = el("label", { class: "lever", for: id }, [el("span", { class: "lever-label", text: lever.label })]);
  var hint = [lever.description, lever.tradeoff].filter(Boolean).join(" ");
  var field = el("div", { class: "lever-field" }, [label, input]);
  if (hint) field.appendChild(el("div", { class: "cell-hint", text: hint }));
  return { lever: lever, input: input, node: field };
}

function leverValue(field) {
  var raw = String(field.input.value || "").trim();
  if (!raw) return undefined;
  var kind = field.lever.kind;
  if (kind === "bool") return raw === "true";
  if (kind === "int") return parseInt(raw, 10);
  if (kind === "list[str]") {
    return raw
      .split(",")
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean);
  }
  return raw;
}

function setLeverValue(field, value) {
  if (value === undefined || value === null) field.input.value = "";
  else if (Array.isArray(value)) field.input.value = value.join(", ");
  else field.input.value = String(value);
}

function renderProfileEditor(container, profiles, onSaved) {
  clear(container);
  container.appendChild(loadingNode());
  loadProfileSchema().then(function (schema) {
    clear(container);
    if (!schema) {
      container.appendChild(errorNotice({ code: "unavailable", message: "Could not load the list of settings a profile may change." }));
      return;
    }
    buildProfileEditor(container, schema, profiles, onSaved);
  });
}

function buildProfileEditor(container, schema, profiles, onSaved) {
  var form = el("form", { class: "profile-form" });
  container.appendChild(
    el("p", {
      class: "notes",
      text: "Pick only the settings you want to change; anything left empty stays as it is. Saving writes a profile file for this tool. Nothing changes in Claude Code until you use the prompt or command it gives you.",
    })
  );

  var startSelect = el("select", { id: "profile-form-start" });
  startSelect.appendChild(el("option", { value: "", text: "An empty profile" }));
  profiles.forEach(function (p) {
    startSelect.appendChild(el("option", { value: p.id, text: p.name || p.id }));
  });
  var idInput = el("input", { type: "text", id: "profile-form-id", required: true, placeholder: "my-profile" });
  var nameInput = el("input", { type: "text", id: "profile-form-name", placeholder: "My profile" });
  form.appendChild(el("div", { class: "lever-field" }, [el("label", { for: "profile-form-start", text: "Start from" }), startSelect]));
  form.appendChild(
    el("div", { class: "lever-field" }, [el("label", { for: "profile-form-id", text: "Short name (lowercase letters, digits and hyphens)" }), idInput])
  );
  form.appendChild(el("div", { class: "lever-field" }, [el("label", { for: "profile-form-name", text: "Display name" }), nameInput]));

  form.appendChild(el("h4", { text: "Settings for every session" }));
  var settingFields = schema.settings.map(function (lever) {
    return leverInput(lever, "profile-setting-");
  });
  var settingsGrid = el("div", { class: "lever-grid" });
  settingFields.forEach(function (f) {
    settingsGrid.appendChild(f.node);
  });
  form.appendChild(settingsGrid);

  form.appendChild(el("h4", { text: "Settings for one agent" }));
  form.appendChild(el("p", { class: "notes", text: "Written to that agent's file. Use the agent's name as it appears on the Agents tab." }));
  var agentBlocks = [];
  var agentsWrap = el("div", { class: "agent-blocks" });
  form.appendChild(agentsWrap);
  var addAgentBtn = el("button", { type: "button", class: "link-button", text: "Add an agent" });
  form.appendChild(addAgentBtn);

  function addAgentBlock(name, values) {
    var index = agentBlocks.length;
    var nameId = "profile-agent-name-" + index;
    var nameField = el("input", { type: "text", id: nameId, placeholder: "e.g. code-reviewer" });
    nameField.value = name || "";
    var block = el("fieldset", { class: "agent-block" }, [el("legend", { text: "Agent" })]);
    block.appendChild(el("div", { class: "lever-field" }, [el("label", { for: nameId, text: "Agent name" }), nameField]));
    var grid = el("div", { class: "lever-grid" });
    var fields = schema.agents.map(function (lever) {
      var f = leverInput(lever, "profile-agent-" + index + "-");
      setLeverValue(f, (values || {})[lever.key]);
      grid.appendChild(f.node);
      return f;
    });
    block.appendChild(grid);
    var removeBtn = el("button", { type: "button", class: "link-button", text: "Remove this agent" });
    var entry = { name: nameField, fields: fields, node: block };
    removeBtn.addEventListener("click", function () {
      agentBlocks.splice(agentBlocks.indexOf(entry), 1);
      agentsWrap.removeChild(block);
    });
    block.appendChild(removeBtn);
    agentBlocks.push(entry);
    agentsWrap.appendChild(block);
  }
  addAgentBtn.addEventListener("click", function () {
    addAgentBlock("", {});
  });

  var jsonBox = el("details", { class: "advanced-detail" });
  jsonBox.appendChild(el("summary", { text: "Edit as JSON instead" }));
  var jsonInput = el("textarea", { id: "profile-form-json", rows: 8 });
  jsonBox.appendChild(
    el("p", { class: "notes", text: "While this is open, saving uses the JSON below and ignores the form. It starts as a copy of the form." })
  );
  jsonBox.appendChild(jsonInput);
  jsonBox.addEventListener("toggle", function () {
    if (jsonBox.open) jsonInput.value = JSON.stringify(collect(), null, 2);
  });
  form.appendChild(jsonBox);

  var errorNode = el("div", { class: "notice error", role: "alert", hidden: true });
  var statusNode = el("p", { class: "notes", role: "status" });
  form.appendChild(el("button", { type: "submit", text: "Save profile" }));
  form.appendChild(errorNode);
  form.appendChild(statusNode);
  container.appendChild(form);

  function collect() {
    var doc = { id: idInput.value.trim(), settings: {}, agents: {} };
    var name = nameInput.value.trim();
    if (name) doc.name = name;
    settingFields.forEach(function (f) {
      var v = leverValue(f);
      if (v !== undefined) doc.settings[f.lever.key] = v;
    });
    agentBlocks.forEach(function (block) {
      var agentName = block.name.value.trim();
      if (!agentName) return;
      var values = {};
      block.fields.forEach(function (f) {
        var v = leverValue(f);
        if (v !== undefined) values[f.lever.key] = v;
      });
      if (Object.keys(values).length) doc.agents[agentName] = values;
    });
    return doc;
  }

  startSelect.addEventListener("change", function () {
    settingFields.forEach(function (f) {
      setLeverValue(f, undefined);
    });
    agentBlocks.slice().forEach(function (block) {
      agentsWrap.removeChild(block.node);
    });
    agentBlocks.length = 0;
    if (!startSelect.value) return;
    fetchJson("/api/profiles/" + encodeURIComponent(startSelect.value)).then(function (result) {
      var body = result.body;
      if (!body || body.ok !== true) return;
      var p = body.data;
      settingFields.forEach(function (f) {
        setLeverValue(f, p.settings[f.lever.key]);
      });
      Object.keys(p.agents || {}).forEach(function (agentName) {
        addAgentBlock(agentName, p.agents[agentName]);
      });
      if (!nameInput.value) nameInput.value = (p.name || p.id) + " (my copy)";
    });
  });

  function showError(message) {
    errorNode.hidden = false;
    errorNode.textContent = message;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    errorNode.hidden = true;
    errorNode.textContent = "";
    statusNode.textContent = "";

    var doc;
    if (jsonBox.open) {
      try {
        doc = JSON.parse(jsonInput.value);
      } catch (err) {
        showError("The JSON isn't valid: " + (err && err.message ? err.message : String(err)));
        return;
      }
      if (typeof doc !== "object" || doc === null || Array.isArray(doc)) {
        showError("The JSON must be an object, like {\"id\": \"my-profile\", \"settings\": {}}.");
        return;
      }
    } else {
      doc = collect();
    }
    if (!doc.id) {
      showError("Give the profile a short name.");
      return;
    }

    fetchJson("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    }).then(function (result) {
      var respBody = result.body;
      if (!respBody || respBody.ok !== true) {
        showError((respBody && respBody.error && respBody.error.message) || "Could not save the profile.");
        return;
      }
      statusNode.textContent = 'Saved "' + (respBody.data.name || respBody.data.id) + '". It is in the list above.';
      if (onSaved) onSaved();
    });
  });
}

function candidateValueText(value) {
  return _diffRowValue(value);
}

function renderWhatIf(data, container) {
  clear(container);
  if (!data || !data.rows || !data.rows.length) {
    container.appendChild(el("p", { class: "notes", text: "Tick a change to see its estimated effect." }));
    return;
  }
  if (data.total_text) container.appendChild(el("p", { class: "quick-summary", text: "Estimated effect of these changes: " + data.total_text + "." }));
  if (data.total_note) container.appendChild(el("p", { class: "notes", text: data.total_note }));
  if (data.not_estimated) {
    container.appendChild(
      el("p", { class: "notes", text: data.not_estimated + (data.not_estimated === 1 ? " change isn't" : " changes aren't") + " estimated; see each row." })
    );
  }
}

function renderProfileCreator(container, onSaved) {
  clear(container);
  var goalsBox = el("div", { class: "profile-cards" });
  var draftBox = el("div", { class: "goal-draft" });
  container.appendChild(el("p", { class: "notes", text: "1. Pick what you want. 2. Tick the changes. 3. Name it and save. Saving writes only this tool's profile folder; you then apply it with the prompt or command it shows." }));
  container.appendChild(goalsBox);
  container.appendChild(draftBox);
  loadInto(goalsBox, "/api/profile-goals", function (data, target) {
    (data.goals || []).forEach(function (goal) {
      var card = el("article", { class: "profile-card goal-card" });
      card.appendChild(el("h4", { text: goal.title }));
      card.appendChild(el("p", { class: "profile-card-summary", text: goal.what }));
      var pick = el("button", { type: "button", text: "Start here" });
      pick.addEventListener("click", function () {
        if (goal.id === "current") {
          var saveBtn = document.getElementById("profiles-save-current");
          if (saveBtn) {
            saveBtn.click();
            saveBtn.scrollIntoView({ behavior: "smooth", block: "center" });
          }
          return;
        }
        loadInto(draftBox, withWindow("/api/profile-goals?goal=" + encodeURIComponent(goal.id)), function (draft, box) {
          renderGoalDraft(draft, box, onSaved);
        });
        draftBox.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      card.appendChild(pick);
      target.appendChild(card);
    });
  });
}

function renderTaskSetups(container) {
  container.appendChild(loadingNode());
  loadReport().then(function (result) {
    clear(container);
    if (result.error) {
      container.appendChild(errorNotice(result.error));
      return;
    }
    var section = findSection(result.report, "habits");
    var table = section && (section.tables || []).filter(function (t) {
      return t.name === "habits_setups";
    })[0];
    if (!table || !(table.rows || []).length) {
      container.appendChild(
        el("p", { class: "notes" }, [
          el("span", { text: "Nothing yet: this needs the kind of task Claude reports with metrics capture at Essentials or above. " }),
          tabLink("capture", "Open the Capture tab"),
        ])
      );
      return;
    }
    container.appendChild(el("p", { class: "notes", text: "To make a profile from a cheaper setup, pick \"A profile for one kind of task\" above." }));
    renderPlacedTables(container, [table], state.currency, "profiles");
  });
}

function renderGoalDraft(draft, container, onSaved) {
  container.appendChild(el("h4", { text: draft.goal.title }));
  if (draft.tasks && draft.tasks.length > 1) {
    var taskPick = el("select", { id: "goal-task-pick" });
    draft.tasks.forEach(function (task) {
      taskPick.appendChild(el("option", { value: task, text: task, selected: task === draft.task }));
    });
    taskPick.addEventListener("change", function () {
      var url = "/api/profile-goals?goal=" + encodeURIComponent(draft.goal.id) + "&task=" + encodeURIComponent(taskPick.value);
      loadInto(container, withWindow(url), function (next, box) {
        renderGoalDraft(next, box, onSaved);
      });
    });
    container.appendChild(el("div", { class: "profile-actions" }, [el("label", { for: "goal-task-pick", text: "Kind of task" }), taskPick]));
  }
  if (draft.note) container.appendChild(el("p", { class: "notes", text: draft.note }));
  var candidates = draft.candidates || [];
  if (!candidates.length) {
    if (!draft.note) {
      container.appendChild(el("p", { class: "notice", text: "Nothing to change for this goal " + (draft.period || "in this window") + ": your settings already match what the data supports, or there isn't enough data yet." }));
    }
    return;
  }
  container.appendChild(el("p", { class: "notes", text: "Ticked changes are the ones your data supports. Unticked ones are a trade-off for you to decide." }));
  var total = el("div", { class: "whatif-total", role: "status" });
  var table = el("table", { class: "data-table goal-table" });
  var head = el("tr");
  ["", "Setting", "Now", "After", "Estimated effect", "Why, and the trade-off"].forEach(function (label) {
    head.appendChild(el("th", { scope: "col", text: label }));
  });
  table.appendChild(el("thead", null, [head]));
  var tbody = el("tbody");
  var boxes = [];
  candidates.forEach(function (c, i) {
    var box = el("input", { type: "checkbox", id: "goal-candidate-" + i, checked: Boolean(c.ticked) });
    boxes.push(box);
    var estimate = c.estimate || {};
    var why = el("td", null, [
      el("p", { text: c.evidence }),
      c.tradeoff ? el("p", { class: "notes", text: "Trade-off: " + c.tradeoff }) : null,
      estimate.basis ? el("p", { class: "notes", text: estimate.fidelity_text + " " + estimate.basis }) : null,
    ]);
    tbody.appendChild(
      el("tr", null, [
        el("td", null, [box]),
        el("td", null, [el("label", { for: box.id, text: c.label + (c.agent ? " (" + c.agent + ")" : "") })]),
        el("td", { text: candidateValueText(c.now) }),
        el("td", { text: candidateValueText(c.value) }),
        el("td", { text: estimate.effect_text || "" }),
        why,
      ])
    );
  });
  table.appendChild(tbody);
  container.appendChild(el("div", { class: "table-wrap" }, [table]));
  container.appendChild(total);

  function chosen() {
    var settings = {};
    var agents = {};
    candidates.forEach(function (c, i) {
      if (!boxes[i].checked) return;
      if (c.agent) {
        agents[c.agent] = agents[c.agent] || {};
        agents[c.agent][c.key] = c.value;
      } else {
        settings[c.key] = c.value;
      }
    });
    return { settings: settings, agents: agents };
  }
  var pending = 0;
  function refreshTotal() {
    var ticket = ++pending;
    total.textContent = "Working out the estimate…";
    var url = withWindow("/api/whatif") + (draft.task ? "&task=" + encodeURIComponent(draft.task) : "");
    postJson(url, chosen()).then(function (result) {
      if (ticket !== pending) return;
      var body = result.body;
      if (!body || body.ok !== true) {
        clear(total);
        total.appendChild(errorNotice(body && body.error));
        return;
      }
      renderWhatIf(body.data, total);
    });
  }
  boxes.forEach(function (box) {
    box.addEventListener("change", refreshTotal);
  });
  refreshTotal();

  var form = el("div", { class: "profile-actions" });
  var name = el("input", { type: "text", id: "goal-profile-name", value: draft.task ? draft.task + " tasks" : draft.goal.title });
  form.appendChild(el("label", { for: "goal-profile-name", text: "Name" }));
  form.appendChild(name);
  var save = el("button", { type: "button", text: "Save as a profile" });
  var status = el("span", { class: "notes", role: "status" });
  form.appendChild(save);
  form.appendChild(status);
  container.appendChild(form);
  save.addEventListener("click", function () {
    var picked = chosen();
    if (!Object.keys(picked.settings).length && !Object.keys(picked.agents).length) {
      status.textContent = "Tick at least one change first.";
      return;
    }
    var id = (name.value || draft.goal.id).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || draft.goal.id;
    save.disabled = true;
    status.textContent = "Saving…";
    postJson("/api/profiles", {
      id: id,
      name: name.value || draft.goal.title,
      for: draft.task ? [draft.task] : [],
      settings: picked.settings,
      agents: picked.agents,
      notes: "Made from the goal \"" + draft.goal.title + "\" " + (draft.period || "") + ".",
    }).then(function (result) {
      save.disabled = false;
      var body = result.body;
      if (!body || body.ok !== true) {
        status.textContent = (body && body.error && body.error.message) || "Could not save the profile.";
        return;
      }
      status.textContent = "Saved. It's in the list above: pick \"Show what it changes\" for the prompt and the command that apply it.";
      if (onSaved) onSaved(body.data.id);
    });
  });
}

function renderProfileEstimate(profile, container) {
  Promise.all([fetchJson("/api/profiles/" + encodeURIComponent(profile.id)), loadProfileSchema()]).then(function (results) {
    var body = results[0].body;
    if (!body || body.ok !== true) return;
    var p = body.data;
    var labels = {};
    ((results[1] && results[1].settings) || []).concat((results[1] && results[1].agents) || []).forEach(function (lever) {
      labels[lever.key] = lever.label;
    });
    // F11: `tasks` is the profile's `for` words normalised to the task
    // vocabulary (a catalogue word like "implementation" isn't one, and
    // /api/whatif rejects it); several scale by their combined share.
    var tasks = p.tasks && p.tasks.length ? p.tasks.join(",") : "";
    var url = withWindow("/api/whatif") + (tasks ? "&task=" + encodeURIComponent(tasks) : "");
    postJson(url, { settings: p.settings || {}, agents: p.agents || {} }).then(function (res) {
      var data = res.body && res.body.ok === true ? res.body.data : null;
      if (!data || !data.rows.length) return;
      clear(container);
      container.appendChild(el("h4", { text: "Estimated effect" }));
      renderWhatIf(data, container);
      container.appendChild(
        simpleTable(
          [{ label: "Change" }, { label: "Effect" }, { label: "How it was worked out" }],
          data.rows.map(function (row) {
            return [
              (labels[row.key] || row.key) + (row.agent ? " (" + row.agent + ")" : "") + ": " + _diffRowValue(row.value),
              row.effect_text,
              (row.fidelity_text + " " + row.basis).trim(),
            ];
          })
        )
      );
    });
  });
}

function renderImpact(data, container) {
  var changes = data.changes || [];
  if (!changes.length) {
    container.appendChild(
      emptyState("No changes recorded yet. After you apply a profile or a fix, or change a setting, this shows the sessions before it against those after it.")
    );
    return;
  }
  container.appendChild(el("p", { class: "notes", text: data.caveat }));
  changes.forEach(function (item) {
    var change = item.change || {};
    var card = el("article", { class: "rec impact-card" });
    card.appendChild(el("h4", { text: change.label + (change.reverted ? " (since undone)" : "") }));
    card.appendChild(el("p", { class: "profile-card-meta", text: String(change.ts || "").replace("T", " ").replace("Z", " UTC") + (change.keys && change.keys.length ? " · " + change.keys.join(", ") : "") }));
    if (item.gate) {
      card.appendChild(emptyState(item.verdict, item.gate));
    } else {
      card.appendChild(el("p", { class: "quick-summary", text: item.verdict }));
    }
    if (item.enough) {
      card.appendChild(
        simpleTable(
          [{ label: "Measure" }, { label: "Before" }, { label: "After" }, { label: "Change" }],
          (item.measures || []).map(function (m) {
            return [m.label, m.before, m.after, m.change_pct === null || m.change_pct === undefined ? "" : (m.change_pct > 0 ? "+" : "") + m.change_pct + "%"];
          })
        )
      );
    }
    var unjudged = (item.quality || []).filter(function (group) {
      return !group.judged;
    });
    (item.quality || []).forEach(function (group) {
      if (!group.judged) return;
      card.appendChild(el("p", { class: "quick-summary" }, [el("strong", { text: "Quality, " + group.label + ": " }), el("span", { text: group.verdict })]));
      var box = el("details", { class: "fix" });
      box.appendChild(el("summary", { text: "Every quality signal (" + group.before_runs + " runs before, " + group.after_runs + " after)" }));
      box.appendChild(
        simpleTable(
          [{ label: "Signal" }, { label: "Before" }, { label: "After" }, { label: "Verdict" }],
          (group.signals || []).map(function (s) {
            return [s.label, s.before_text + " (" + s.before_counts + ")", s.after_text + " (" + s.after_counts + ")", s.verdict];
          })
        )
      );
      card.appendChild(box);
    });
    if (unjudged.length) {
      card.appendChild(
        el("p", { class: "notes" }, [
          el("strong", { text: "Quality: " }),
          el("span", {
            text:
              "too few runs yet to judge " +
              unjudged
                .map(function (group) {
                  return group.label + " (" + group.before_runs + " before, " + group.after_runs + " after)";
                })
                .join(", ") +
              ". Each needs at least " + unjudged[0].min_runs + " runs on each side.",
          }),
        ])
      );
    }
    if (change.source === "apply" && change.backup_ts && !change.reverted) {
      card.appendChild(el("p", { class: "notes", text: "To undo it:" }));
      card.appendChild(codeBlockWithCopy("claude-token-lens apply --revert " + change.backup_ts));
    }
    var levelChange = change.source === "capture" && (change.changes || []).filter(function (c) {
      return c.key === "capture.level" && c.old;
    })[0];
    if (levelChange) {
      card.appendChild(el("p", { class: "notes", text: "To change it back, use the Capture tab or:" }));
      card.appendChild(codeBlockWithCopy(levelChange.old === "off" ? "claude-token-lens capture off" : "claude-token-lens capture level " + levelChange.old));
    }
    container.appendChild(card);
  });
}

// EST-P4/P8: did a saving estimate come true? Rows are
// backtest.present()'s own display-ready shape (predicted_text,
// measured_text and verdict_text are already server-formatted
// sentences) -- this just lays them out in a table, no client-side
// money or verdict logic, per docs/ui.md's "server formats, dashboard
// shows" rule.
function renderBacktest(data, container) {
  var predictions = (data && data.predictions) || [];
  if (!predictions.length) {
    container.appendChild(
      emptyState("No estimates logged yet. Estimates shown in “What if?” are logged automatically, then checked here once the sessions to judge them arrive.")
    );
    return;
  }
  container.appendChild(
    el("p", {
      class: "notes",
      text: "Estimates from “What if?”, checked against what actually happened after a matching change.",
    })
  );
  container.appendChild(
    simpleTable(
      [{ label: "Change" }, { label: "When" }, { label: "Estimated" }, { label: "Measured" }, { label: "Verdict" }],
      predictions.map(function (row) {
        return [
          row.agent ? row.agent + ": " + row.measure_key : row.measure_key,
          String(row.ts || "").replace("T", " ").replace("Z", " UTC"),
          row.predicted_text,
          row.measured_text || "—",
          row.verdict_text,
        ];
      })
    )
  );
}
