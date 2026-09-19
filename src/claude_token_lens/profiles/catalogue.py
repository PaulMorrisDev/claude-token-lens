"""The seven shipped starting-point profiles (plan "Milestone v0.3":
"Catalogue shipped as starting points, each with the metrics that
justify it"), plus :func:`suggest`, the deterministic archetype/purpose
-> catalogue-id mapping the plan's baseline/``init`` (v0.3, out of this
work package's writable paths) will call once it detects a corpus's
archetype and dominant purposes.

Each ``catalogue/<id>.toml`` is a normal profile document (loaded
through ``schema.load_profile``, so it is validated the same way any
other profile is) whose ``notes`` field names the actual report
table/column this project already computes that justifies the profile's
settings -- never an invented number (see each file's own ``notes``).

Deviation from the plan, reported rather than made silently (see
``model.py``'s module docstring for this project's convention): the
plan's `suggest(archetype, purposes)` signature (Milestone v0.3 bullet)
takes no session *mode* (``classify.classify_mode``'s
overnight/long-agentic/interactive/mixed), only archetype and purpose --
but the ``overnight-batch`` catalogue entry is justified entirely by
*mode* evidence (``classify.classify_mode``'s overnight rule: span > 4h
and max human gap > 60 min), which has no archetype or purpose signal of
its own (an overnight session can be any archetype). With the signature
fixed as given, ``suggest`` cannot deterministically reach
``"overnight-batch"`` -- it is reachable only via direct
``catalogue.get("overnight-batch")`` (or a future ``suggest`` revision
that also takes the corpus's mode mix, once that lands as an explicit,
reviewed contract change rather than a guess baked in here). This is
disclosed rather than papered over with a fabricated purpose->id
mapping that would never actually fire for a genuinely overnight corpus.
"""

from __future__ import annotations

from pathlib import Path

from .schema import Profile, load_profile

__all__ = ["CATALOGUE_IDS", "list_profiles", "get", "suggest"]

#: Exactly the seven ids the plan names (Milestone v0.3's catalogue
#: bullet), in the order the plan lists them.
CATALOGUE_IDS: tuple[str, ...] = (
    "interactive-chat",
    "discovery-scrape",
    "planning-requirements",
    "implementation-heavy",
    "overseer-fanout",
    "overnight-batch",
    "workflow-ultracode",
)


def _catalogue_dir() -> Path:
    return Path(__file__).parent / "catalogue"


def list_profiles() -> list[Profile]:
    """Every shipped catalogue profile, in :data:`CATALOGUE_IDS` order."""
    return [load_profile(_catalogue_dir() / f"{profile_id}.toml") for profile_id in CATALOGUE_IDS]


def get(profile_id: str) -> Profile | None:
    """The shipped catalogue profile named ``profile_id``, or ``None`` if
    ``profile_id`` is not one of :data:`CATALOGUE_IDS`."""
    if profile_id not in CATALOGUE_IDS:
        return None
    return load_profile(_catalogue_dir() / f"{profile_id}.toml")


# -- suggest(): deterministic archetype/purpose -> catalogue id ------------
#
# Printed verbatim (as a table) in docs/profiles.md's "suggest() mapping"
# section -- keep the two in sync by hand when either changes, the same
# convention docs/config-layers.md already follows for snapshots.py.

#: Checked first, in ``purposes`` list order (the caller's own dominant-
#: purpose ordering) -- a purpose is a more specific signal than the bare
#: archetype, so it wins when present. Every entry here is one of the
#: nine purposes ``classify.classify_purpose`` can return.
_PURPOSE_OVERRIDE: dict[str, str] = {
    "local-llm-pipeline": "discovery-scrape",
    "workflow-run": "workflow-ultracode",
    "agent-fanout": "overseer-fanout",
    "refactor": "implementation-heavy",
    "test-triage": "implementation-heavy",
    "review": "implementation-heavy",
    "planning": "planning-requirements",
    "docs-or-light-edit": "interactive-chat",
    "general-dev": "planning-requirements",
}

#: Falls back to this when no purpose in the caller's list matches
#: anything in ``_PURPOSE_OVERRIDE`` above. Every key is one of
#: ``schema.ARCHETYPES``.
_ARCHETYPE_DEFAULT: dict[str, str] = {
    "chat-only": "interactive-chat",
    "single-model": "planning-requirements",
    "effort-varied": "planning-requirements",
    "plan-high-implement-low": "implementation-heavy",
    "overseer-fanout": "overseer-fanout",
    "workflow-heavy": "workflow-ultracode",
    "mixed": "interactive-chat",
}

#: The catalogue id neither table above ever reaches (see module
#: docstring's deviation note) -- ``suggest`` never returns this.
UNREACHABLE_BY_SUGGEST: str = "overnight-batch"


def suggest(archetype: str | None, purposes: list[str]) -> str:
    """The catalogue id ``id`` (see :data:`CATALOGUE_IDS`) that best
    starts a corpus with workstyle ``archetype`` (one of
    ``schema.ARCHETYPES``, or ``None``/unclassified) whose dominant
    purposes are ``purposes`` (``classify.classify_purpose``'s values,
    most-dominant first). Deterministic: the same ``(archetype,
    purposes)`` pair always returns the same id. Never returns
    :data:`UNREACHABLE_BY_SUGGEST` -- see the module docstring."""
    for purpose in purposes:
        if purpose in _PURPOSE_OVERRIDE:
            return _PURPOSE_OVERRIDE[purpose]
    return _ARCHETYPE_DEFAULT.get(archetype or "", "interactive-chat")
