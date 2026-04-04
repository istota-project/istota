"""Skill discovery, manifest loading, and doc loading.

Supports two discovery modes:
1. Directory-based: each skill is a subdirectory with skill.md (YAML frontmatter
   for metadata). Optional skill.toml for backward compat / operator overrides.
2. Legacy: flat _index.toml + *.md files in a single directory

Discovery order (later wins):
1. Bundled skills: src/istota/skills/*/skill.md
2. Operator overrides: config/skills/*/skill.md (or skill.toml)
3. Legacy fallback: config/skills/_index.toml (lowest priority)
"""

import hashlib
import importlib
import json
import logging
import re
import subprocess
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


def _parse_frontmatter(md_path: Path) -> dict | None:
    """Parse YAML frontmatter from a skill.md file.

    Supports a minimal subset: scalar values, booleans, inline YAML lists
    [a, b, c], and JSON-encoded values (for env specs).
    Returns parsed dict or None if no frontmatter found or parse error.
    """
    if not md_path.exists():
        return None
    try:
        text = md_path.read_text()
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    yaml_text = text[3:end].strip()
    try:
        data = {}
        for line in yaml_text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            colon = line.find(":")
            if colon == -1:
                continue
            key = line[:colon].strip()
            value = line[colon + 1:].strip()
            # Parse booleans
            if value.lower() == "true":
                data[key] = True
            elif value.lower() == "false":
                data[key] = False
            # Parse inline list: [a, b, c]
            elif value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                if not inner:
                    data[key] = []
                # Check if it looks like JSON (contains { })
                elif "{" in inner:
                    data[key] = json.loads(value)
                else:
                    data[key] = [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
            elif value.startswith("["):
                # Malformed list (unclosed bracket) — skip this field
                logger.warning("Malformed list in frontmatter key %r: %s", key, value[:50])
            else:
                data[key] = value.strip("'\"")
        return data if data else None
    except Exception as e:
        logger.warning("Failed to parse frontmatter in %s: %s", md_path, e)
        return None


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from markdown text."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:].lstrip("\n")


def _load_skill_meta(skill_dir: Path) -> SkillMeta | None:
    """Load skill metadata from a directory.

    Primary source is YAML frontmatter in skill.md. Falls back to skill.toml
    for any fields not present in frontmatter (backward compat for operator
    overrides). Returns None if neither file exists.
    """
    md_path = skill_dir / "skill.md"
    toml_path = skill_dir / "skill.toml"

    fm = _parse_frontmatter(md_path)
    toml_data: dict = {}

    if toml_path.exists():
        try:
            with open(toml_path, "rb") as f:
                toml_data = tomllib.load(f)
        except Exception as e:
            logger.warning("Failed to parse %s: %s", toml_path, e)

    if not fm and not toml_data:
        return None

    def _get(key: str, default=None):
        """Get from frontmatter first, then toml fallback."""
        if fm and key in fm:
            return fm[key]
        return toml_data.get(key, default)

    def _get_bool(key: str, default: bool = False) -> bool:
        val = _get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() == "true"
        return default

    def _get_list(key: str) -> list:
        val = _get(key, [])
        return val if isinstance(val, list) else []

    # Frontmatter uses "triggers" for keywords
    keywords = _get_list("triggers") if (fm and "triggers" in fm) else _get_list("keywords")

    # Env specs: frontmatter uses JSON array in "env" field, toml uses [[env]]
    env_raw = _get("env", [])
    if isinstance(env_raw, list) and env_raw and isinstance(env_raw[0], dict):
        env_specs = _parse_env_specs(env_raw)
    else:
        env_specs = []

    return SkillMeta(
        name=skill_dir.name,
        description=_get("description", "") or "",
        always_include=_get_bool("always_include"),
        admin_only=_get_bool("admin_only"),
        keywords=keywords,
        resource_types=_get_list("resource_types"),
        source_types=_get_list("source_types"),
        file_types=_get_list("file_types"),
        companion_skills=_get_list("companion_skills"),
        exclude_skills=_get_list("exclude_skills"),
        env_specs=env_specs,
        dependencies=_get_list("dependencies"),
        exclude_memory=_get_bool("exclude_memory"),
        exclude_persona=_get_bool("exclude_persona"),
        exclude_resources=_get_list("exclude_resources"),
        cli=_get_bool("cli"),
        skill_dir=str(skill_dir),
    )


def _discover_directory_skills(base_dir: Path) -> dict[str, SkillMeta]:
    """Scan subdirectories of base_dir for skill metadata (frontmatter or toml)."""
    skills = {}
    if not base_dir.is_dir():
        return skills
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        if child.name == "__pycache__":
            continue
        meta = _load_skill_meta(child)
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
            exclude_memory=meta.get("exclude_memory", False),
            exclude_persona=meta.get("exclude_persona", False),
            exclude_resources=meta.get("exclude_resources", []),
            cli=meta.get("cli", False),
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


def get_skill_availability(meta: SkillMeta) -> tuple[str, str | None]:
    """Check if a skill's dependencies are installed.

    Returns ("available", None) or ("unavailable", "package_name").
    """
    if not meta.dependencies:
        return ("available", None)
    for dep in meta.dependencies:
        pkg_name = dep.split(">=")[0].split("==")[0].split("<")[0].split(">")[0].strip()
        pkg_name = pkg_name.replace("-", "_")
        try:
            importlib.import_module(pkg_name)
        except ImportError:
            return ("unavailable", pkg_name)
    return ("available", None)


def select_skills(
    prompt: str,
    source_type: str,
    user_resource_types: set[str],
    skill_index: dict[str, SkillMeta],
    is_admin: bool = True,
    attachments: list[str] | None = None,
    disabled_skills: set[str] | None = None,
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
    Skills in disabled_skills are skipped entirely (instance-wide + per-user).
    """
    selected = set()
    prompt_lower = prompt.lower()
    attachment_extensions = _get_attachment_extensions(attachments)
    disabled = disabled_skills or set()

    for name, meta in skill_index.items():
        if name in disabled:
            continue

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

        if meta.file_types and attachment_extensions:
            if any(ft in attachment_extensions for ft in meta.file_types):
                if _check_dependencies(meta):
                    selected.add(name)
                continue

        if meta.keywords:
            if any(kw in prompt_lower for kw in meta.keywords):
                # If skill requires a resource type, only include if user has it
                if meta.resource_types:
                    if not any(rt in user_resource_types for rt in meta.resource_types):
                        continue
                if _check_dependencies(meta):
                    selected.add(name)

    # Resolve companion skills (e.g., whisper pulls in reminders, schedules)
    companions = set()
    for name in selected:
        meta = skill_index[name]
        for companion in meta.companion_skills:
            if companion in skill_index and companion not in selected and companion not in disabled:
                cmeta = skill_index[companion]
                if cmeta.admin_only and not is_admin:
                    continue
                if _check_dependencies(cmeta):
                    companions.add(companion)
    selected |= companions

    # Apply exclude_skills: selected skills can exclude others
    excluded = set()
    for name in list(selected):
        meta = skill_index[name]
        for ex in meta.exclude_skills:
            if ex in selected:
                excluded.add(ex)
    selected -= excluded

    result = sorted(selected)
    if result:
        logger.debug("Selected skills: %s", ", ".join(result))
    return result


def format_cli_skills(skill_index: dict[str, SkillMeta]) -> str:
    """Generate a prompt-ready list of skills that have CLI tools.

    Returns a formatted string listing each CLI skill with its command
    and description, suitable for inclusion in the tools section of a prompt.
    Returns empty string if no CLI skills exist.
    """
    lines = []
    for name in sorted(skill_index):
        meta = skill_index[name]
        if meta.cli:
            lines.append(f"  - `istota-skill {name}` — {meta.description}")
    if not lines:
        return ""
    header = (
        "- Skill CLI tools (run `--help` for subcommands). "
        "Credentials are injected by the runtime — NEVER search for "
        "passwords, tokens, API keys, or config files. "
        "If a command fails with an auth error, report it to the user."
    )
    return header + "\n" + "\n".join(lines)


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
            content = _strip_frontmatter(doc_path.read_text()).strip()
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


def build_skill_manifest(
    skill_index: dict[str, SkillMeta],
    exclude: set[str],
    disabled_skills: set[str] | None = None,
    is_admin: bool = True,
) -> str:
    """Build a compact manifest of available skills for LLM classification.

    Excludes already-selected skills, always_include skills (already loaded),
    disabled skills, admin_only skills for non-admins, and skills with
    unmet dependencies.
    """
    disabled = disabled_skills or set()
    lines = []
    for name in sorted(skill_index):
        if name in exclude:
            continue
        meta = skill_index[name]
        if meta.always_include:
            continue
        if name in disabled:
            continue
        if meta.admin_only and not is_admin:
            continue
        if not _check_dependencies(meta):
            continue
        triggers = ", ".join(meta.keywords[:10]) if meta.keywords else "none"
        lines.append(f"- {name}: {meta.description}. Triggers: {triggers}")
    return "Available skills (not yet selected):\n" + "\n".join(lines)


def classify_skills(
    prompt: str,
    skill_index: dict[str, SkillMeta],
    already_selected: set[str],
    disabled_skills: set[str] | None = None,
    is_admin: bool = True,
    model: str = "haiku",
    timeout: float = 3.0,
) -> list[str]:
    """LLM-based skill classification (Pass 2).

    Returns additional skill names to load beyond keyword matches.
    Returns [] on timeout, error, or if no additional skills are needed.
    Respects disabled_skills, admin_only, and dependency checks.
    """
    manifest = build_skill_manifest(
        skill_index, exclude=already_selected,
        disabled_skills=disabled_skills, is_admin=is_admin,
    )

    # Check if there are any skills to classify
    skill_lines = [l for l in manifest.split("\n") if l.startswith("- ")]
    if not skill_lines:
        return []

    classification_prompt = (
        "Given this task, which additional skills (if any) should be loaded?\n"
        "Return a JSON array of skill names, or [] if none are needed.\n"
        "Only include skills that are clearly relevant — not speculative matches.\n"
        "\n"
        f"Task: {prompt}\n"
        "\n"
        f"{manifest}"
    )

    try:
        result = subprocess.run(
            ["claude", "-p", "-", "--model", model],
            input=classification_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            logger.warning(
                "Skill classification failed (returncode=%d): %s",
                result.returncode,
                result.stderr or result.stdout,
            )
            return []

        output = result.stdout.strip()

        # Extract JSON from code blocks or raw output
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", output, re.DOTALL)
        if code_block:
            output = code_block.group(1).strip()

        skill_names = json.loads(output)
        if not isinstance(skill_names, list):
            logger.warning("Skill classification returned non-list: %s", output[:200])
            return []

        # Filter to valid skill names not already selected, respecting all guards
        _disabled = disabled_skills or set()
        valid_names = []
        for name in skill_names:
            if not isinstance(name, str) or name not in skill_index:
                continue
            if name in already_selected or name in _disabled:
                continue
            meta = skill_index[name]
            if meta.admin_only and not is_admin:
                continue
            if not _check_dependencies(meta):
                continue
            valid_names.append(name)

        if valid_names:
            logger.info("Semantic routing added skills: %s", ", ".join(valid_names))

        return valid_names

    except subprocess.TimeoutExpired:
        logger.warning("Skill classification timed out after %.1fs", timeout)
        return []
    except json.JSONDecodeError as e:
        logger.warning("Skill classification JSON parse error: %s", e)
        return []
    except FileNotFoundError:
        logger.error("Claude CLI not found for skill classification")
        return []
    except Exception as e:
        logger.warning("Skill classification error: %s", e)
        return []
