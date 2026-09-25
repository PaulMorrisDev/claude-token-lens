"""Profiles (V3-profiles): the schema, catalogue and diff/apply-command
helpers behind the plan's Milestone v0.3 "Profile schema" and Appendix
A7. See ``schema.py``, ``catalogue.py`` and ``diff.py`` for the three
pieces; this package intentionally has no dependency on ``cli.py`` or
``service/`` — a later work package wires ``apply``, ``init``,
``baseline`` and the two ``/api/profiles*`` routes against this module
(see ``docs/api.md``'s S1-api note that both routes are ``501`` stubs
until this package exists).
"""

from __future__ import annotations

from .schema import (
    AGENT_ALLOWLIST,
    ARCHETYPES,
    ENV_ALLOWLIST,
    SETTINGS_ALLOWLIST,
    LeverSpec,
    Profile,
    ProfileError,
    dump_profile,
    load_dict,
    load_profile,
    loads_profile,
    recommend_lever_key,
    validate,
)
from .catalogue import get, list_profiles, suggest
from .diff import DiffRow, ProfileDiff, apply_command, diff_against_effective, render_unified_diff

__all__ = [
    "AGENT_ALLOWLIST",
    "ARCHETYPES",
    "ENV_ALLOWLIST",
    "SETTINGS_ALLOWLIST",
    "LeverSpec",
    "Profile",
    "ProfileError",
    "dump_profile",
    "load_dict",
    "load_profile",
    "loads_profile",
    "recommend_lever_key",
    "validate",
    "get",
    "list_profiles",
    "suggest",
    "DiffRow",
    "ProfileDiff",
    "apply_command",
    "diff_against_effective",
    "render_unified_diff",
]
