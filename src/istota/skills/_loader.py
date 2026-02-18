"""Skill discovery, manifest loading, and doc loading.

Supports two discovery modes:
1. Directory-based: each skill is a subdirectory with skill.toml + skill.md
2. Legacy: flat _index.toml + *.md files in a single directory

Discovery order (later wins):
1. Bundled skills: src/istota/skills/*/skill.toml
2. Operator overrides: config/skills/*/skill.toml (or skill.md only)
3. Legacy fallback: config/skills/_index.toml (lowest priority)
"""

import hashlib
import importlib
import logging
import tomllib
from pathlib import Path

from ._types import EnvSpec, SkillMeta

logger = logging.getLogger("istota.skills_loader")

# Path to bundled skills (sibling directories of this file)
_BUNDLED_SKILLS_DIR = Path(__file__).parent


def _parse_env_specs(data: list[dict]) -> list[EnvSpec]:
    """Parse [[env]] entries from a skill.toml into EnvSpec objects."""
    specs = []
    for entry in data:
        specs.append(EnvSpec(
            var=entry.get("var", ""),
            source=entry.get("from", ""),
            config_path=entry.get("config_path", ""),
            when=entry.get("when", ""),
            resource_type=entry.get("resource_type", ""),
            field=entry.get("field", ""),
            template=entry.get("template", ""),
            user_path_fn=entry.get("user_path_fn", ""),
        ))
    return specs


def _load_skill_toml(skill_dir: Path) -> SkillMeta | None:
    """Load a single skill.toml from a directory."""
    toml_path = skill_dir / "skill.toml"
    if not toml_path.exists():
        return None
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        logger.warning("Failed to parse %s: %s", toml_path, e)
        return None

    return SkillMeta(
        name=skill_dir.name,
        description=data.get("description", ""),
        always_include=data.get("always_include", False),
        admin_only=data.get("admin_only", False),
        keywords=data.get("keywords", []),
        resource_types=data.get("resource_types", []),
        source_types=data.get("source_types", []),
        file_types=data.get("file_types", []),
        companion_skills=data.get("companion_skills", []),
        env_specs=_parse_env_specs(data.get("env", [])),
        dependencies=data.get("dependencies", []),
        skill_dir=str(skill_dir),
    )


def _discover_directory_skills(base_dir: Path) -> dict[str, SkillMeta]:
    """Scan subdirectories of base_dir for skill.toml manifests."""
    skills = {}
    if not base_dir.is_dir():
        return skills
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        if child.name == "__pycache__" or child.name == "whisper":
            # whisper is a special case — has __init__.py but also needs skill.toml
            # Once migrated it will be picked up normally; skip only __pycache__
            if child.name == "__pycache__":
                continue
        meta = _load_skill_toml(child)
        if meta is not None:
            skills[meta.name] = meta
    return skills


def _load_legacy_index(skills_dir: Path) -> dict[str, SkillMeta]:
    """Load skill metadata from legacy _index.toml format."""
    index_path = skills_dir / "_index.toml"
    if not index_path.exists():
        return {}

    try:
        with open(index_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        logger.warning("Failed to parse %s: %s", index_path, e)
        return {}

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
            companion_skills=meta.get("companion_skills", []),
        )
        for name, meta in data.items()
        if isinstance(meta, dict)
    }


def load_skill_index(
    skills_dir: Path,
    bundled_dir: Path | None = None,
) -> dict[str, SkillMeta]:
    """Load all skill metadata with layered discovery.

    Discovery priority (later wins):
    1. Legacy _index.toml in skills_dir (lowest priority)
    2. Bundled skill.toml directories (in src/istota/skills/)
    3. Operator skill.toml directories in skills_dir (highest priority)

    Args:
        skills_dir: Operator config skills directory (e.g. config/skills/).
        bundled_dir: Override for bundled skills directory (for testing).
    """
    if bundled_dir is None:
        bundled_dir = _BUNDLED_SKILLS_DIR

    # Layer 1: Legacy _index.toml (lowest priority)
    skills = _load_legacy_index(skills_dir)

    # Layer 2: Bundled directory-based skills
    bundled = _discover_directory_skills(bundled_dir)
    skills.update(bundled)

    # Layer 3: Operator overrides from config/skills/*/skill.toml
    overrides = _discover_directory_skills(skills_dir)
    skills.update(overrides)

    return skills


def _get_attachment_extensions(attachments: list[str] | None) -> set[str]:
    """Extract lowercase file extensions from attachment paths."""
    if not attachments:
        return set()
    extensions = set()
    for att in attachments:
        name = att.rsplit("/", 1)[-1] if "/" in att else att
        if "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
            extensions.add(ext)
    return extensions


def _check_dependencies(meta: SkillMeta) -> bool:
    """Check if a skill's Python dependencies are importable."""
    if not meta.dependencies:
        return True
    for dep in meta.dependencies:
        # Extract package name from requirement string (e.g. "faster-whisper>=1.1.0" -> "faster_whisper")
        pkg_name = dep.split(">=")[0].split("==")[0].split("<")[0].split(">")[0].strip()
        pkg_name = pkg_name.replace("-", "_")
        try:
            importlib.import_module(pkg_name)
        except ImportError:
            logger.debug("Skill %s skipped: dependency %s not installed", meta.name, dep)
            return False
    return True


def select_skills(
    prompt: str,
    source_type: str,
    user_resource_types: set[str],
    skill_index: dict[str, SkillMeta],
    is_admin: bool = True,
    attachments: list[str] | None = None,
) -> list[str]:
    """Select relevant skills based on prompt and context.

    Selection criteria (in order):
    1. Always include core skills (always_include=true)
    2. Match by source type (e.g., briefing tasks)
    3. Match by user resource types (e.g., user has calendar access)
    4. Match by file types in attachments (e.g., .mp3 triggers whisper)
    5. Match by keywords in prompt

    Skills with admin_only=true are skipped for non-admin users.
    Skills with unmet dependencies are skipped with a debug log.
    """
    selected = set()
    prompt_lower = prompt.lower()
    attachment_extensions = _get_attachment_extensions(attachments)

    for name, meta in skill_index.items():
        if meta.admin_only and not is_admin:
            continue

        if meta.always_include:
            if _check_dependencies(meta):
                selected.add(name)
            continue

        if meta.source_types and source_type in meta.source_types:
            if _check_dependencies(meta):
                selected.add(name)
            continue

        if meta.resource_types:
            if any(rt in user_resource_types for rt in meta.resource_types):
                if _check_dependencies(meta):
                    selected.add(name)
                continue

        if meta.file_types and attachment_extensions:
            if any(ft in attachment_extensions for ft in meta.file_types):
                if _check_dependencies(meta):
                    selected.add(name)
                continue

        if meta.keywords:
            if any(kw in prompt_lower for kw in meta.keywords):
                if _check_dependencies(meta):
                    selected.add(name)

    # Resolve companion skills (e.g., whisper pulls in reminders, schedules)
    companions = set()
    for name in selected:
        meta = skill_index[name]
        for companion in meta.companion_skills:
            if companion in skill_index and companion not in selected:
                cmeta = skill_index[companion]
                if cmeta.admin_only and not is_admin:
                    continue
                if _check_dependencies(cmeta):
                    companions.add(companion)
    selected |= companions

    result = sorted(selected)
    if result:
        logger.debug("Selected skills: %s", ", ".join(result))
    return result


def _resolve_skill_doc_path(
    skill_name: str,
    skill_meta: SkillMeta | None,
    skills_dir: Path,
    bundled_dir: Path | None = None,
) -> Path | None:
    """Find the skill.md doc file, checking override path first.

    Resolution order:
    1. Operator override: skills_dir/<name>/skill.md
    2. Operator override (legacy): skills_dir/<name>.md
    3. Bundled: skill_meta.skill_dir/skill.md (from directory discovery)
    4. Bundled fallback (legacy): skills_dir/<name>.md
    """
    if bundled_dir is None:
        bundled_dir = _BUNDLED_SKILLS_DIR

    # 1. Operator directory override
    override_dir = skills_dir / skill_name / "skill.md"
    if override_dir.exists():
        return override_dir

    # 2. Operator legacy flat file
    legacy_path = skills_dir / f"{skill_name}.md"
    if legacy_path.exists():
        return legacy_path

    # 3. Bundled skill directory
    if skill_meta and skill_meta.skill_dir:
        bundled_doc = Path(skill_meta.skill_dir) / "skill.md"
        if bundled_doc.exists():
            return bundled_doc

    # 4. Bundled directory (explicit path)
    bundled_fallback = bundled_dir / skill_name / "skill.md"
    if bundled_fallback.exists():
        return bundled_fallback

    return None


def load_skills(
    skills_dir: Path,
    skill_names: list[str],
    bot_name: str = "Istota",
    bot_dir: str = "",
    skill_index: dict[str, SkillMeta] | None = None,
    bundled_dir: Path | None = None,
) -> str:
    """Load and concatenate selected skill docs, substituting placeholders."""
    if not bot_dir:
        bot_dir = bot_name.lower()

    if bundled_dir is None:
        bundled_dir = _BUNDLED_SKILLS_DIR

    parts = []
    for name in skill_names:
        meta = skill_index.get(name) if skill_index else None
        doc_path = _resolve_skill_doc_path(name, meta, skills_dir, bundled_dir)
        if doc_path is not None:
            title = name.replace("-", " ").replace("_", " ").title()
            content = doc_path.read_text().strip()
            content = content.replace("{BOT_NAME}", bot_name).replace("{BOT_DIR}", bot_dir)
            parts.append(f"### {title}\n\n{content}")

    if not parts:
        return ""

    fingerprint = compute_skills_fingerprint(skills_dir, bundled_dir)
    return f"## Skills Reference (v: {fingerprint})\n\n" + "\n\n".join(parts)


def compute_skills_fingerprint(
    skills_dir: Path,
    bundled_dir: Path | None = None,
) -> str:
    """Compute a content hash of all skill files for change detection.

    Hashes all skill.toml + skill.md files from both bundled and operator dirs,
    plus legacy _index.toml and *.md files. Sorted by name for determinism.
    Returns the first 12 chars of the hex digest.
    """
    if bundled_dir is None:
        bundled_dir = _BUNDLED_SKILLS_DIR

    h = hashlib.sha256()

    # Legacy index
    index_path = skills_dir / "_index.toml"
    if index_path.exists():
        h.update(index_path.read_bytes())

    # Legacy flat md files
    for md_file in sorted(skills_dir.glob("*.md")):
        h.update(md_file.name.encode())
        h.update(md_file.read_bytes())

    # Bundled skill directories
    if bundled_dir.is_dir():
        for child in sorted(bundled_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_") or child.name == "__pycache__":
                continue
            for f in sorted(child.glob("skill.*")):
                h.update(f"{child.name}/{f.name}".encode())
                h.update(f.read_bytes())

    # Operator skill directories
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            for f in sorted(child.glob("skill.*")):
                h.update(f"override/{child.name}/{f.name}".encode())
                h.update(f.read_bytes())

    return h.hexdigest()[:12]


def load_skills_changelog(
    skills_dir: Path,
    bundled_dir: Path | None = None,
) -> str | None:
    """Load CHANGELOG.md — check bundled dir first, then operator dir."""
    if bundled_dir is None:
        bundled_dir = _BUNDLED_SKILLS_DIR

    # Check bundled skills directory first
    bundled_changelog = bundled_dir / "CHANGELOG.md"
    if bundled_changelog.exists():
        content = bundled_changelog.read_text().strip()
        if content:
            return content

    # Fall back to operator skills directory
    changelog_path = skills_dir / "CHANGELOG.md"
    if changelog_path.exists():
        content = changelog_path.read_text().strip()
        return content if content else None

    return None
