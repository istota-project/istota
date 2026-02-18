"""Skill loading and selection for task execution.

This module is a backward-compatible wrapper. All logic has moved to
istota.skills._loader and istota.skills._types.
"""

# Re-export the SkillMeta from _types for backward compat
from .skills._types import SkillMeta  # noqa: F401

# Re-export all public functions from _loader
from .skills._loader import (  # noqa: F401
    _get_attachment_extensions,
    compute_skills_fingerprint,
    load_skill_index,
    load_skills,
    load_skills_changelog,
    select_skills,
)
