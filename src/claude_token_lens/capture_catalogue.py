"""Metrics capture: the words Claude may write, and the metrics behind them.

Metrics capture is opt-in. While it is on, a small hook adds a short note
to each session and subagent start (see ``hooks/capture-note.py``) asking
Claude to end its replies with a one-line tag, for example
``[tl: task=bugfix brief=partial level=normal]``. Token Lens reads the tags
back out of the transcripts to explain what the work was, not only what it
cost.

This module is the single source of truth for what those tags may say.
Every value is a closed vocabulary: a word outside it is dropped by the
parser (``capture_tags.py``), so no free text Claude writes is ever stored.
It has no imports from the rest of the package, so the parser, the hook
script's generated JSON and the dashboard all read the same lists.
"""

from __future__ import annotations

#: The marker every capture note carries, followed by the note format
#: version and the codes of the metrics it asks for
#: (``tl-cap v1 task,brief,level``). The parser finds capture notes by it.
NOTE_MARKER = "tl-cap v"

#: Current note format version.
NOTE_VERSION = 1

#: ``[tl: ...]`` keys (and the ``[result: ...]`` extras) -> the words each
#: may take. A key whose value is a comma list (``missing=files,goal``) is
#: in :data:`LIST_KEYS`.
TAG_VOCAB: dict[str, tuple[str, ...]] = {
    # Essentials, main session
    "task": (
        "feature",
        "bugfix",
        "refactor",
        "debug",
        "docs",
        "review",
        "test",
        "research",
        "plan",
        "ops",
        "chat",
    ),
    "brief": ("clear", "partial", "vague"),
    "level": ("easy", "normal", "hard"),
    "shift": ("new", "build", "grew", "redo"),
    # Standard, main session
    "size": ("xs", "s", "m", "l", "xl"),
    "missing": ("files", "goal", "constraints", "done", "repro", "scope", "none"),
    "plan": ("none", "made", "following", "deviated"),
    "skill": ("helped", "unneeded", "would-help", "none"),
    "found": ("yes", "partial", "no"),
    # Standard, subagent report (inside [result: ...])
    "fit": ("smaller", "right", "larger"),
    "rules": ("used", "unused"),
    # Deep, main session
    "prior": ("needed", "some", "none"),
    "detour": ("none", "dead-end", "reread", "overbuilt", "env", "flaky"),
    "check": ("targeted", "full", "build", "run", "manual", "none"),
    "out": ("needed", "part", "unneeded"),
    "useful": ("yes", "part", "no"),
}

#: Keys whose value is a comma-separated list of words.
LIST_KEYS = frozenset({"missing"})

#: ``[result: <word> ...]``: the subagent's own account of finishing.
RESULT_WORDS = ("done", "partial", "blocked")

#: ``[retry: <word>]`` at the start of a brief: why an agent is being run
#: again. ``scope`` means the task itself changed or was cut too wide.
RETRY_REASONS = ("model", "brief", "tools", "scope", "other")

#: ``[spawn: <word>]`` at the start of a brief: why the work was handed to
#: an agent at all.
SPAWN_REASONS = ("parallel", "isolate", "cheaper", "specialist", "review")

#: A skill name kept with ``skill=would-help:<name>``: the same shape as a
#: Claude Code skill or plugin skill name. Anything else is cut to a bare
#: ``would-help``, and a name that matches no skill in the transcript is
#: dropped too (see ``capture_tags.parse_tl_tag``).
SKILL_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"
