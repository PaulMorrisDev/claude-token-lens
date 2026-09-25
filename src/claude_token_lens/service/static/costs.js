/* claude-token-lens service UI: costs.js
 *
 * The prices behind the dashboard's money: report.meta.rates (from
 * pricing.toml) turned into the sentences the Actions detail
 * (page-actions.js) and Glossary > How costs work (page-glossary.js)
 * both need. Every multiplier goes through format.js's fraction(), never
 * typed in here, so the words always match your pricing.
 */

import { findSection } from "./api.js";
import { modelTier } from "./charts.js";
import { fraction, modelName } from "./format.js";

// The prices behind the sentences, from your pricing (report.meta.rates,
// read from pricing.toml), never typed in here. "main" is the model you
// spent most on in this window.
export function pricingFacts(report) {
  var rates = (report && report.meta && report.meta.rates) || {};
  var overview = report ? findSection(report, "overview") : null;
  var byModel = ((overview && overview.tables) || []).filter(function (t) {
    return t.name === "by_model";
  })[0];
  var used = byModel
    ? byModel.rows
        .map(function (row) {
          return String(row[0]);
        })
        .filter(function (id) {
          return rates[id];
        })
    : [];
  var mainId = used[0] || Object.keys(rates)[0] || null;
  return { rates: rates, used: used, main: mainId ? rates[mainId] : null, mainId: mainId };
}

// A price as a share or multiple of the input price: "a tenth of the
// input price", "1.25 times the input price".
export function priced(ratio) {
  var words = fraction(ratio);
  return words ? words + " the input price" : "";
}

// The model of a family ("opus", "sonnet", "haiku") the sentence names:
// the one you used most, else the newest one priced.
function familyModel(facts, family) {
  var used = facts.used.filter(function (id) {
    return modelTier(id) === family;
  })[0];
  if (used) return used;
  var priced = Object.keys(facts.rates).filter(function (id) {
    return modelTier(id) === family && !/fable/.test(id);
  });
  priced.sort(function (a, b) {
    return versionOf(b) - versionOf(a);
  });
  return priced[0] || null;
}

function versionOf(id) {
  var numbers = String(id).replace(/-\d{8}$/, "").match(/\d+/g) || [];
  return numbers.reduce(function (sum, n, i) {
    return sum + Number(n) / Math.pow(100, i);
  }, 0);
}

// "Each model has its own price per token. Sonnet costs a fifth of
// Opus's price, and Haiku a twentieth of it." -- the models named are
// whichever a recommendation group's changes move to, else Sonnet and
// Haiku.
export function modelSentence(facts, group) {
  var reference = familyModel(facts, "opus");
  var referencePrice = reference && facts.rates[reference] ? facts.rates[reference].input : 0;
  if (!referencePrice) return "";
  var wanted = [];
  group.members.forEach(function (rec) {
    (rec.changes || []).forEach(function (change) {
      var family = modelTier(change.value);
      if (family !== "other" && family !== "opus" && wanted.indexOf(family) === -1) wanted.push(family);
    });
  });
  if (!wanted.length) wanted = ["sonnet", "haiku"];
  var parts = wanted
    .map(function (family) {
      var id = familyModel(facts, family);
      var price = id && facts.rates[id] ? facts.rates[id].input : 0;
      return price ? { name: modelName(id), ratio: price / referencePrice } : null;
    })
    .filter(function (part) {
      return part && part.ratio < 0.995;
    })
    .sort(function (a, b) {
      return b.ratio - a.ratio;
    });
  if (!parts.length) return "";
  var first = fraction(parts[0].ratio);
  var text = "Each model has its own price per token. " + parts[0].name + " costs " + first + (/ of$/.test(first) ? " " : " of ") + modelName(reference) + "'s price";
  if (parts[1]) {
    var second = fraction(parts[1].ratio);
    text += ", and " + parts[1].name + " " + second + (/ of$/.test(second) ? " it" : " of it");
  }
  return text + ".";
}

// ======================================================================
// Glossary > How costs work: the rule sentence for each of links.js's
// COST_CARDS, from your own pricing. The Terms segment shows the same
// sentence as a "Why it matters" line on every term a card explains
// (links.js's COST_CARDS[].terms) -- cardRuleText is the one place each
// is written, so the two segments never drift apart.
// ======================================================================

function cacheReadRule(facts) {
  var main = facts.main;
  var words = main ? priced(main.cache_read_ratio) : "";
  if (!words) return "";
  return "Reading from the cache costs " + words + ", so most of what's already in the conversation goes at the cheapest rate on every later turn.";
}

function cacheWriteRule(facts) {
  var main = facts.main;
  var write5m = main ? priced(main.cache_write_5m_ratio) : "";
  if (!write5m) return "";
  var write1h = priced(main.cache_write_1h_ratio);
  if (!write1h) {
    return "Writing to the cache costs " + write5m + " for a 5-minute lifetime.";
  }
  return (
    "Writing to the cache costs " +
    write5m +
    " for a 5-minute lifetime, or " +
    write1h +
    " for 1 hour. The longer lifetime is worth it when it avoids enough rebuilds after an idle gap."
  );
}

function cacheRebuildRule(facts) {
  var main = facts.main;
  var read = main ? priced(main.cache_read_ratio) : "";
  var write = main ? priced(main.cache_write_5m_ratio) : "";
  if (!read || !write) return "";
  return "Reading from the cache costs " + read + "; a rebuild writes the whole conversation to the cache again at " + write + ". Every rebuild you avoid saves the difference.";
}

// Compares the model you spent most on with the cheaper tiers your
// pricing has, the same way modelSentence does for a recommendation
// group, but against every tier instead of only the ones a change
// touches.
function modelChoiceRule(facts) {
  var mainId = facts.mainId;
  var main = facts.main;
  if (!mainId || !main || !main.input) return "";
  var mainTier = modelTier(mainId);
  var order = mainTier === "opus" ? ["sonnet", "haiku"] : mainTier === "sonnet" ? ["haiku"] : [];
  var parts = order
    .map(function (family) {
      var id = familyModel(facts, family);
      var price = id && facts.rates[id] ? facts.rates[id].input : 0;
      return price ? { name: modelName(id), ratio: price / main.input } : null;
    })
    .filter(function (part) {
      return part && part.ratio < 0.995;
    });
  if (!parts.length) {
    return "Each model has its own price per token. " + modelName(mainId) + " is already the least expensive tier priced in your setup.";
  }
  var first = fraction(parts[0].ratio);
  var text = "Each model has its own price per token. " + parts[0].name + " costs " + first + (/ of$/.test(first) ? " " : " of ") + modelName(mainId) + "'s price";
  if (parts[1]) {
    var second = fraction(parts[1].ratio);
    text += ", and " + parts[1].name + " " + second + (/ of$/.test(second) ? " it" : " of it");
  }
  return text + ".";
}

function startupContextRule(facts) {
  var main = facts.main;
  var write = main ? priced(main.cache_write_5m_ratio) : "";
  if (!write) return "";
  return "Each subagent starts fresh, and its startup context is written to the cache at " + write + ", once per spawn.";
}

function toolOutputRule(facts) {
  var main = facts.main;
  var read = main ? priced(main.cache_read_ratio) : "";
  if (!read) return "";
  return "Tool output that stays in the conversation is read again on every later turn: from the cache at " + read + ", or more after a rebuild.";
}

function conversationSummaryRule() {
  return "Every turn reads the whole conversation again. A conversation summary replaces it with a shorter one, so each later turn reads less.";
}

function billingModeRule() {
  return "Amounts here follow your billing: a share of your weekly usage limit on a Pro or Max plan, or money on pay-per-token billing.";
}

var CARD_RULES = {
  "cache-reads": cacheReadRule,
  "cache-writes": cacheWriteRule,
  "cache-rebuilds": cacheRebuildRule,
  "model-choice": modelChoiceRule,
  "startup-context": startupContextRule,
  "tool-output": toolOutputRule,
  "conversation-summaries": conversationSummaryRule,
  "billing-mode": billingModeRule,
};

// The rule sentence for one of links.js's COST_CARDS, from your pricing
// (facts, from pricingFacts). Empty when the rates it needs aren't
// priced yet.
export function cardRuleText(slug, facts) {
  var rule = CARD_RULES[slug];
  return rule ? rule(facts || { rates: {}, used: [], main: null, mainId: null }) : "";
}
