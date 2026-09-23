# Writing help text

Every table on the dashboard carries a short explanation, and every
recommendation explains the change it suggests. This page is the house
style for both. The copy lives in
[`src/claude_token_lens/helptext.py`](../src/claude_token_lens/helptext.py)
and is checked by `tests/test_help_coverage.py`.

## Rules

- **Short sentences.** One idea per sentence. Aim for under 20 words.
- **Plain words.** Say what happens, not what the code calls it.
- **Second person.** "Your subagents", "you pay", not "the user".
- **Numbers carry a unit.** Tokens, %, minutes, or the billing-mode
  amount (see below).
- **No internal names.** No snake_case keys, enum names, or field names
  (`per_turn_effort`, `CACHE_SIGNAL`, `cache_creation_tokens`). The test
  rejects them.

## Words to use

| Say | Not |
|---|---|
| cache rebuild | re-cache, recache |
| cache write | cache_creation |
| cache read | cache_read |
| startup context | spawn write, baseline write |
| conversation summary (compaction) | compaction, on first use |
| main session | top-level |
| subagent | sub, agent transcript |
| task prompt | briefing |
| Claude Code's notes | attachments, reminders |
| cache lifetime (TTL) | TTL, on first use |
| thinking | thinking tokens, on first use |

## The "how to read this" block

Each kept or advanced table has three parts (`model.Help`):

1. **What it shows.** One or two sentences, starting with a noun phrase.
   "Each row is one agent type."
2. **How to read it.** What a big or small number means, and what to
   compare it with.
3. **When to act.** The threshold or pattern worth acting on, and what to
   do. Leave it empty when the table is context only.

Column help is one sentence: what the number is, per what.

## Amounts

Amounts follow the billing mode in `config.toml`:

- **Subscription:** share of your usage limits first ("about 2% of a
  weekly limit"), with the list-price equivalent second.
- **API:** dollars first.

Never write "$" for a subscription user without "list-price equivalent".

## Explaining a change

Every suggested change answers six questions, in this order:

1. **What the setting controls.**
2. **Now and after.** Current value and new value, in plain words.
3. **Where and who.** The file, and who it affects ("every session in
   this project", "only the Explore agent").
4. **Expected effect.** In billing-mode units, with what it is based on.
5. **Trade-off.** What you give up.
6. **How to undo it.**

Then offer two ways to make the change: a command
(`claude-token-lens apply --set ... --dry-run` first) and a prompt for
Claude, and end with `fixes.RESTART_NOTE`: Claude Code reads settings
when it starts, so the change needs a restart.

## Prompts for Claude

A prompt must stand on its own:

- Name the file, the key, and the new value.
- Say why, in one sentence.
- Say that Claude Code will ask permission before editing files in
  `.claude`.
- Ask Claude to restate the change and show the diff before saving.
- End with `fixes.PROMPT_RESTART`, so Claude reminds you to restart
  Claude Code once the change is saved.
