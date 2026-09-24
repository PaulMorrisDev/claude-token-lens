/* claude-token-lens service UI: format.js
 *
 * Number, money and time formatting: the one place a value becomes
 * text.
 */

import { state } from "./core.js";

// -- formatting (mirrors render/tables.py::format_cell exactly) ------

export function thousands(n) {
  return Math.round(Number(n)).toLocaleString("en-US");
}

function formatSecs(value) {
  var total = Math.round(value);
  var sign = total < 0 ? "-" : "";
  total = Math.abs(total);
  var hours = Math.floor(total / 3600);
  var remainder = total % 3600;
  var minutes = Math.floor(remainder / 60);
  var seconds = remainder % 60;
  var parts = [];
  if (hours) parts.push(hours + "h");
  if (hours || minutes) parts.push(minutes + "m");
  parts.push(seconds + "s");
  return sign + parts.join(" ");
}

var COLUMN_KINDS = ["str", "int", "float", "pct", "money", "tokens", "secs"];

export var NUMERIC_KINDS = { int: true, float: true, pct: true, money: true, tokens: true, secs: true };

// Mirrors render/tables.py::format_cell's kind switch exactly.
// ``toLocaleString("en-US", ...)`` is used (fixed locale, not the
// browser's own) for thousands separators so output stays
// deterministic regardless of the viewer's system locale.
// UX-1: unitsAware requests units.Units.money's billing-mode phrasing
// (moneyText() below) for a "money" cell instead of the plain
// currency-suffixed number -- opt-in per call site (every existing
// sortable data-grid column keeps the plain, always-parseable number;
// this mirrors render/tables.py::format_cell, whose own `units`
// parameter the report's own table renderers likewise never pass --
// only prose call sites, like page-habits.js's playbook card, do).
export function formatCell(value, kind, currency, unitsAware) {
  currency = currency || "USD";
  if (value === null || value === undefined) return "-";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (COLUMN_KINDS.indexOf(kind) === -1) kind = "str";
  switch (kind) {
    case "str":
      // A mixed "metric / value" table: numbers read with separators.
      if (typeof value === "number" && isFinite(value)) {
        return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
      }
      return String(value);
    case "int":
    case "tokens":
      return Math.round(Number(value)).toLocaleString("en-US");
    case "float":
      return Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    case "pct":
      return Number(value).toFixed(1) + "%";
    case "money":
      if (unitsAware) return moneyText(Number(value));
      return Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " " + currency;
    case "secs":
      return formatSecs(Number(value));
    default:
      return String(value);
  }
}

// Mirrors units.Units.money (src/claude_token_lens/units.py): usd (a
// list-price amount over opts.period, e.g. "a week") phrased for the
// billing mode from state.units (UX-1's report.meta.units -- {mode,
// share_per_usd, period_label, basis}, set when a report loads).
// Returns null for a non-positive or non-finite amount, same contract
// as the Python original. share_per_usd is already the window-%-per-
// USD slope (elasticity.express_in_window(1.0, ...)), so a share for
// an arbitrary usd is just usd * share_per_usd -- linear, no curve
// fit needed client-side.
export function money(usd, opts) {
  opts = opts || {};
  var period = opts.period || "";
  if (typeof usd !== "number" || !isFinite(usd) || usd <= 0) return null;
  var suffix = period ? " " + period : "";
  var dollars = formatCell(usd, "money", state.currency);
  var unitsInfo = state.units || {};
  if (unitsInfo.mode !== "subscription") {
    return { primary: dollars + suffix, secondary: "", basis: "at list price" };
  }
  var sharePerUsd = unitsInfo.share_per_usd;
  if (sharePerUsd === null || sharePerUsd === undefined) {
    return { primary: dollars + " list-price equivalent" + suffix, secondary: "", basis: unitsInfo.basis || "" };
  }
  var share = usd * sharePerUsd;
  var shareText = share < 1 ? share.toFixed(2) + "%" : formatCell(share, "pct");
  return {
    primary: "about " + shareText + " of your " + (unitsInfo.period_label || "weekly usage limit") + suffix,
    secondary: dollars + " list-price equivalent",
    basis: unitsInfo.basis || "",
  };
}

// Mirrors units.Units.money_text/Amount.phrase: a one-line amount
// that is never empty, for a spot that used to interpolate a raw
// "$" + value.toFixed(2). opts.prefix (e.g. "about ") is joined
// without doubling "about" when money()'s own primary text already
// opens with it (a subscription's "about X% of your weekly usage
// limit" -- finding F3's "about about" bug, mirrored client-side).
export function moneyText(usd, opts) {
  opts = opts || {};
  var prefix = opts.prefix || "";
  var amount = money(usd, opts);
  var text = amount ? (amount.secondary ? amount.primary + " (" + amount.secondary + ")" : amount.primary) : null;
  if (text === null) {
    var value = typeof usd === "number" && isFinite(usd) ? usd : 0;
    return formatCell(value, "money", state.currency);
  }
  if (!prefix) return text;
  var strippedPrefix = prefix.replace(/\.$/, "").trim().toLowerCase();
  if (strippedPrefix === "about" && text.toLowerCase().indexOf("about ") === 0) return text;
  var joiner = /[ \-‑]$/.test(prefix) ? "" : " ";
  return prefix + joiner + text;
}

export function cellSortValue(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

// A change as a signed percentage: "+12%", "−3%" with a true minus
// sign (U+2212, the width of the plus), "0%". Blank when there is none.
export function signedPercent(value) {
  if (value === null || value === undefined || value === "") return "";
  var n = Number(value);
  if (n > 0) return "+" + value + "%";
  if (n < 0) return "−" + String(value).replace(/^-/, "") + "%";
  return value + "%";
}

// "2026-09-23T10:44:22.705Z" -> "2026-09-23 10:44 UTC", for a table cell.
export function shortTs(ts) {
  var text = String(ts || "");
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(text)) return text || "-";
  return text.slice(0, 16).replace("T", " ") + (/Z$/.test(text) ? " UTC" : "");
}
