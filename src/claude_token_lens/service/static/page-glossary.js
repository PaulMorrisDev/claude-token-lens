/* claude-token-lens service UI: page-glossary.js
 *
 * The Glossary page (same wording as the README's glossary).
 */

import { clear, el } from "./core.js";
import { viewIntro } from "./links.js";

// ======================================================================
// Glossary (same wording as the README's glossary)
// ======================================================================

var GLOSSARY = [
  ["Session", "One conversation with Claude Code, from start to exit. Resuming it continues the same session."],
  ["Main session", "The conversation you type into, as opposed to the subagents it starts."],
  ["Subagent", "A separate Claude that your session starts for one task, such as a search or a review. It has its own context and reports back when done."],
  ["Transcript", "The log file Claude Code writes for a session or a subagent run. Everything here is read from these files on your machine."],
  ["Reply", "One response from Claude, including any tool calls it makes. Every reply is billed for the whole context it reads."],
  ["Token", "The unit models read and write, roughly three quarters of a word. Prices are per million tokens."],
  ["Context", "Everything Claude reads on a reply: system prompt, tools, CLAUDE.md files and the conversation so far."],
  ["Startup context", "What Claude reads before your first message, or before a subagent's task: system prompt, tool list, CLAUDE.md files, skills and more."],
  ["Prompt cache", "A copy of the start of the context kept on Anthropic's side, so the next reply can re-read it cheaply instead of paying full price."],
  ["Cache read", "Re-reading context from the prompt cache. About a tenth of the normal input price."],
  ["Cache write", "Putting context into the prompt cache. Costs more than normal input: 1.25 times for a 5-minute lifetime, 2 times for 1 hour."],
  ["Cache rebuild", "Writing context to the cache again because the cached copy expired or something early in the conversation changed."],
  ["Cache lifetime (TTL)", "How long the prompt cache stays warm after a reply: 5 minutes by default, or 1 hour. A pause longer than this means a rebuild."],
  ["Conversation summary", "When the context gets too large, Claude Code replaces the conversation so far with a summary. Also called compaction."],
  ["List price", "Anthropic's published price per token. On a Pro or Max plan you don't pay this; it is shown to compare costs."],
  ["Usage limits", "On a Pro or Max plan, the share of your five-hour and weekly allowance you have used."],
  ["Billing mode", "How amounts are shown. On a Pro or Max plan, as a share of your usage limits when there are enough readings, otherwise as a list-price equivalent. On pay-per-token billing, as money."],
  ["Effort level", "How hard Claude thinks before replying. Thinking is billed as output, the most expensive token type."],
  ["Scorecard", "Five areas rated 1 (very poor) to 5 (excellent), each from one number in your data."],
  ["Recommendation", "A change worth making, with what it changes, the trade-off, a prompt you can give Claude and a command you can run."],
  ["Profile", "A named group of settings you can compare with yours, try for one session, or apply."],
  ["Scope", "Where a change is written: your user settings (every project), this project on your machine only, or this project for everyone."],
  ["Managed setting", "A setting your organisation's policy controls. Only your administrator can change it."],
  ["Snapshot", "A record of your Claude Code settings at one moment, taken so changes can be compared over time."],
  ["Window", "The stretch of time the numbers cover, picked at the top of the dashboard. It can be the last hour, today, the last 24 hours, 7, 30 or 90 days, all time, or since your last change. A session counts, in full, when it was last active in the window."],
  ["Change point", "A moment your settings changed: an apply, its undo, or a change the settings snapshot saw. The dashboard compares the sessions before it with those after it."],
  ["Quick action", "One question about a way to spend less, answered from your own sessions with the evidence and a fix you can copy. The dashboard lists them on the Actions page, under Checks."],
  ["What-if estimate", "What a change would have saved over the window, worked out from your own sessions. It is an estimate: cheaper settings can change how Claude works, which the estimate can't see."],
  ["CLAUDE.md", "Instruction files Claude reads at the start of every session, and of most subagents: yours, each project's, and rule files. Every line is paid for on every reply that re-reads it."],
  ["Skill", "A packaged set of instructions Claude can load when a task needs it. Its name and description are listed to Claude at the start of every session, used or not."],
  ["Quality signal", "A sign of whether the work went well, not just what it cost: tool calls that failed, agent runs that didn't finish, your corrections. Compared across models and efforts, and before and after each change you make."],
  ["Metrics capture", "An opt-in feature, off by default: Claude adds a one-line tag saying what a piece of work was and how it went. It costs tokens while it's on. init's last questions and claude-token-lens capture turn it on, change what it asks for, or turn it off."],
  ["Capture level", "How much metrics capture asks for: off, free, essentials, standard or deep, each adding more of it. Set at init or with claude-token-lens capture level."],
  ["Tag", "The one-line, closed-vocabulary note metrics capture has Claude add to a reply, such as [tl: task=bugfix brief=clear] or [result: done fit=right]. Only words from a fixed list are kept; nothing Claude writes in its own words is."],
  ["Prompt cycle", "One message of yours and everything Claude did to answer it, subagents at any depth included. The unit metrics capture and the Work habits page measure by."],
  ["Work habits", "The page (and report section) that turns prompt cycles into habits worth trying, with a rough saving for each. Each shows where its evidence came from: reported by Claude, inferred from the transcript, or your own feedback."],
  ["Feedback skill", "/tl-feedback, a skill you can add and run after a piece of work. It asks whether the work delivered, what slowed it, whether it was worth the tokens, and what would have helped. Works at any capture level, even off; picking deep turns it on, with its reminders."],
  ["Brief templates", "Checklists per kind of task on the Work habits page, built from what your own requests tend to lack. Turned on, it also adds a /tl-brief skill that checks a request against its checklist and asks once for anything missing before Claude starts."],
  ["Sampling", "Running metrics capture in only a share of sessions (100, 50, 25 or 10 percent, [capture] sample) to spend fewer tokens on it. Picked at random, per session."],
  ["Time-box", "The date metrics capture switches itself back off. By default it's 14 days after you turn a level on, whether at init, with capture on or level, or on the Capture page. So turning it on never means it runs unattended forever. --for or --capture-for sets another length, and --no-limit or --capture-no-limit turns the limit off. You can also say so when asked."],
];

export function renderGlossary(panel) {
  clear(panel);
  viewIntro(panel, "glossary");
  var list = el("dl", { class: "glossary" });
  GLOSSARY.forEach(function (pair) {
    list.appendChild(el("dt", { text: pair[0] }));
    list.appendChild(el("dd", { text: pair[1] }));
  });
  panel.appendChild(list);
}
