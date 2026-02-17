"""Skill loading and selection for task execution."""

import hashlib
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("istota.skills_loader")


@dataclass
class SkillMeta:
    """Metadata for a skill."""

    name: str
    description: str
    always_include: bool = False
    admin_only: bool = False
    keywords: list[str] = field(default_factory=list)
    resource_types: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)


def load_skill_index(skills_dir: Path) -> dict[str, SkillMeta]:
    """Load skill metadata from _index.toml."""
    index_path = skills_dir / "_index.toml"
    if not index_path.exists():
        return {}

    with open(index_path, "rb") as f:
        data = tomllib.load(f)

    return {
        name: SkillMeta(
            name=name,
            description=meta.get("description", ""),
            always_include=meta.get("always_include", False),
            admin_only=meta.get("admin_only", False),
            keywords=meta.get("keywords", []),
            resource_types=meta.get("resource_types", []),
            source_types=meta.get("source_types", []),
            file_types=meta.get("file_types", []),
        )
        for name, meta in data.items()
    }


def _get_attachment_extensions(attachments: list[str] | None) -> set[str]:
    """Extract lowercase file extensions from attachment paths."""
    if not attachments:
        return set()
    extensions = set()
    for att in attachments:
        # Handle both full paths and relative paths
        name = att.rsplit("/", 1)[-1] if "/" in att else att
        if "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
            extensions.add(ext)
    return extensions


def select_skills(
    prompt: str,
    source_type: str,
    user_resource_types: set[str],
    skill_index: dict[str, SkillMeta],
    is_admin: bool = True,
    attachments: list[str] | None = None,
) -> list[str]:
    """
    Select relevant skills based on prompt and context.

    Selection criteria (in order):
    1. Always include core skills (always_include=true)
    2. Match by source type (e.g., briefing tasks)
    3. Match by user resource types (e.g., user has calendar access)
    4. Match by file types in attachments (e.g., .mp3 triggers whisper)
    5. Match by keywords in prompt

    Skills with admin_only=true are skipped for non-admin users.
    """
    selected = set()
    prompt_lower = prompt.lower()
    attachment_extensions = _get_attachment_extensions(attachments)

    for name, meta in skill_index.items():
        # Skip admin-only skills for non-admin users
        if meta.admin_only and not is_admin:
            continue

        # Always include core skills
        if meta.always_include:
            selected.add(name)
            continue

        # Match by source type (e.g., briefing)
        if meta.source_types and source_type in meta.source_types:
            selected.add(name)
            continue

        # Match by user resource types
        if meta.resource_types:
            if any(rt in user_resource_types for rt in meta.resource_types):
                selected.add(name)
                continue

        # Match by file types in attachments
        if meta.file_types and attachment_extensions:
            if any(ft in attachment_extensions for ft in meta.file_types):
                selected.add(name)
                continue

        # Match by keywords in prompt
        if meta.keywords:
            if any(kw in prompt_lower for kw in meta.keywords):
                selected.add(name)

    result = sorted(selected)
    if result:
        logger.debug("Selected skills: %s", ", ".join(result))
    return result


def compute_skills_fingerprint(skills_dir: Path) -> str:
    """Compute a content hash of all skill files for change detection.

    Hashes _index.toml + all *.md files (sorted by name for determinism).
    Returns the first 12 chars of the hex digest.
    """
    h = hashlib.sha256()
    index_path = skills_dir / "_index.toml"
    if index_path.exists():
        h.update(index_path.read_bytes())
    for md_file in sorted(skills_dir.glob("*.md")):
        h.update(md_file.name.encode())
        h.update(md_file.read_bytes())
    return h.hexdigest()[:12]


def load_skills_changelog(skills_dir: Path) -> str | None:
    """Load CHANGELOG.md from the skills directory if it exists."""
    changelog_path = skills_dir / "CHANGELOG.md"
    if changelog_path.exists():
        content = changelog_path.read_text().strip()
        return content if content else None
    return None


def load_skills(skills_dir: Path, skill_names: list[str], bot_name: str = "Istota", bot_dir: str = "") -> str:
    """Load and concatenate selected skill files, substituting {BOT_NAME}/{BOT_DIR} placeholders."""
    if not bot_dir:
        bot_dir = bot_name.lower()
    parts = []
    for name in skill_names:
        skill_path = skills_dir / f"{name}.md"
        if skill_path.exists():
            title = name.replace("-", " ").title()
            content = skill_path.read_text().strip()
            content = content.replace("{BOT_NAME}", bot_name).replace("{BOT_DIR}", bot_dir)
            parts.append(f"### {title}\n\n{content}")

    if not parts:
        return ""

    fingerprint = compute_skills_fingerprint(skills_dir)
    return f"## Skills Reference (v: {fingerprint})\n\n" + "\n\n".join(parts)
