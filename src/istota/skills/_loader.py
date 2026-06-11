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
            resource_types=list(entry.get("resource_types") or []),
            field=entry.get("field", ""),
            template=entry.get("template", ""),
            user_path_fn=entry.get("user_path_fn", ""),
            service=entry.get("service", ""),
            key=entry.get("key", ""),
            sensitive=bool(entry.get("sensitive", False)),
            fallback_var=entry.get("fallback_var", ""),
            gate_user_has_resource=entry.get("gate_user_has_resource", ""),
            gate_has_discovered_calendars=bool(
                entry.get("gate_has_discovered_calendars", False)
            ),
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
        experimental=_get_bool("experimental"),
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
            experimental=meta.get("experimental", False),
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


def expand_companions(
    names: list[str],
    skill_index: dict[str, SkillMeta],
    *,
    is_admin: bool = True,
    disabled_skills: set[str] | None = None,
    enabled_experimental_features: frozenset[str] = frozenset(),
) -> list[str]:
    """Gate-filtered companion resolution, one level, deduped.

    Returns the companion skills declared by ``names`` (via
    ``companion_skills``) that pass the standard gates — not disabled, not
    admin-gated for a non-admin, not an unenabled experimental skill, and with
    importable dependencies — excluding any name already in ``names``.

    One level only: companions-of-companions are not expanded (so a companion
    cycle is inert). A declared companion missing from the index is logged at
    WARNING and skipped — it may be a safety skill, so the gap is never silent.

    Shared by ``select_skills`` (eager-companion expansion) and the
    ``skills show`` CLI (pull-time companion expansion) so the two paths apply
    the identical gate.
    """
    disabled = disabled_skills or set()
    result: list[str] = []
    seen = set(names)
    for name in names:
        meta = skill_index.get(name)
        if meta is None:
            continue
        for companion in meta.companion_skills:
            if companion in seen:
                continue
            cmeta = skill_index.get(companion)
            if cmeta is None:
                logger.warning(
                    "companion %r declared by skill %r not found in index — skipped",
                    companion, name,
                )
                continue
            if companion in disabled:
                continue
            if cmeta.admin_only and not is_admin:
                continue
            if cmeta.experimental and f"skill_{companion}" not in enabled_experimental_features:
                continue
            if not _check_dependencies(cmeta):
                continue
            seen.add(companion)
            result.append(companion)
    return result


def select_skills(
    prompt: str,
    source_type: str,
    user_resource_types: set[str],
    skill_index: dict[str, SkillMeta],
    is_admin: bool = True,
    attachments: list[str] | None = None,
    disabled_skills: set[str] | None = None,
    sticky_skills: set[str] | None = None,
    enabled_experimental_features: frozenset[str] = frozenset(),
) -> list[str]:
    """Select the eager skill set for a task (the single-axis model, Part A).

    "Selected by a deterministic rule ⇒ eager." Selection criteria:
    1. Always include core skills (always_include=true)
    2. Match by source type (e.g., briefing tasks pre-load calendar/markets)
    3. Match by file types in attachments (e.g., .mp3 triggers whisper)

    Keyword (``triggers``) and ``resource_types`` matching are intentionally
    NOT eager selectors — every non-eager eligible skill is surfaced to the
    model as a one-line menu entry (the widened on-demand catalogue), so a
    keyword guess is redundant. ``resource_types`` survives only as a
    menu-membership gate in ``eligible_skill_names``. (``prompt`` /
    ``user_resource_types`` are retained in the signature for call-site
    compatibility; they no longer drive selection.)

    Sticky skills (recent-conversation follow-up) still select eager so a
    multi-turn task keeps its skills inline. Companions of any eager skill are
    pulled in (gate-filtered, one level) via ``expand_companions`` so e.g. the
    ``untrusted_input`` safety skill rides along with a source/file-selected
    ingest skill.

    Skills with admin_only=true are skipped for non-admin users; unmet
    dependencies skipped; ``disabled_skills`` skipped (instance + per-user);
    ``experimental=true`` skipped unless ``skill_<name>`` is in
    ``enabled_experimental_features``.
    """
    selected = set()
    reasons: dict[str, str] = {}
    attachment_extensions = _get_attachment_extensions(attachments)
    disabled = disabled_skills or set()

    def _experimental_blocked(meta: SkillMeta) -> bool:
        if not meta.experimental:
            return False
        return f"skill_{meta.name}" not in enabled_experimental_features

    def _add(name: str, reason: str) -> None:
        selected.add(name)
        reasons.setdefault(name, reason)

    for name, meta in skill_index.items():
        if name in disabled:
            continue

        if meta.admin_only and not is_admin:
            continue

        if _experimental_blocked(meta):
            logger.debug("Skill %s skipped: experimental flag skill_%s not enabled", name, name)
            continue

        if meta.always_include:
            if _check_dependencies(meta):
                _add(name, "always_include")
            continue

        if meta.source_types and source_type in meta.source_types:
            if _check_dependencies(meta):
                _add(name, f"source_type={source_type}")
            continue

        if meta.file_types and attachment_extensions:
            matched_ft = next((ft for ft in meta.file_types if ft in attachment_extensions), None)
            if matched_ft is not None:
                if _check_dependencies(meta):
                    _add(name, f"file_type={matched_ft}")
                continue

    # Inject sticky skills from recent conversation (follow-up context)
    if sticky_skills:
        for name in sticky_skills:
            if name in disabled or name not in skill_index:
                continue
            meta = skill_index[name]
            if meta.admin_only and not is_admin:
                continue
            if _experimental_blocked(meta):
                continue
            if meta.always_include:
                continue  # already selected
            if _check_dependencies(meta):
                _add(name, "sticky")

    # Resolve companion skills (e.g., an ingest skill pulls in untrusted_input).
    # Shared with the `skills show` pull-time path via expand_companions so the
    # gate filter can't drift between the two.
    for cname in expand_companions(
        list(selected), skill_index,
        is_admin=is_admin,
        disabled_skills=disabled,
        enabled_experimental_features=enabled_experimental_features,
    ):
        _add(cname, "companion")

    # Apply exclude_skills: selected skills can exclude others
    excluded = set()
    for name in list(selected):
        meta = skill_index[name]
        for ex in meta.exclude_skills:
            if ex in selected:
                excluded.add(ex)
    for ex in excluded:
        selected.discard(ex)
        reasons.pop(ex, None)

    result = sorted(selected)
    if result:
        trace = ", ".join(f"{n}({reasons.get(n, '?')})" for n in result)
        logger.info("pass1_selection count=%d: %s", len(result), trace)
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


def build_disclosure_index(
    lazy_names: list[str],
    skill_index: dict[str, SkillMeta],
) -> str:
    """Build the "Available skills (load on demand)" prompt section.

    One ``- <name>: <description>`` line per lazy skill, under a header that
    tells the model to run ``istota-skill skills show <name>`` to load the full
    instructions before using the skill. Returns ``""`` when there are no lazy
    skills (so the section is omitted and the prompt stays byte-identical to the
    all-eager path).
    """
    if not lazy_names:
        return ""
    lines = []
    for name in sorted(lazy_names):
        meta = skill_index.get(name)
        desc = (meta.description if meta else "") or ""
        lines.append(f"  - {name}: {desc}")
    header = (
        "- Available skills (load on demand). These skills are relevant to this "
        "task but their full instructions are NOT included below. Before using "
        "one, run `istota-skill skills show <name>` to load its documentation:"
    )
    return header + "\n" + "\n".join(lines)


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


def eligible_skill_names(
    skill_index: dict[str, SkillMeta],
    exclude: set[str],
    disabled_skills: set[str] | None = None,
    is_admin: bool = True,
    enabled_experimental_features: frozenset[str] = frozenset(),
) -> list[str]:
    """Sorted names of skills eligible to be surfaced to the model.

    Excludes ``exclude`` (already-selected), ``always_include`` skills (already
    loaded eager), disabled skills, ``admin_only`` skills for non-admins,
    experimental skills whose ``skill_<name>`` flag isn't enabled, and skills
    with unmet dependencies. The progressive-disclosure catalogue index uses
    this so the model self-selects from the full eligible menu.

    Note: no resource gate (unlike ``select_skills``' Pass-1 keyword path). The
    catalogue surfaces the full eligible menu so the model self-selects;
    re-narrowing it to Pass-1's resource match would defeat that. No bundled
    skill currently declares ``resource_types`` anyway — the former holdouts
    (``notes`` / ``spec`` / ``todos``) are doc-only convention skills with
    sensible defaults (``notes`` falls back to ``{BOT_DIR}/notes/``) and dropped
    the field. The gate mechanism stays for any future resource-backed skill.
    """
    disabled = disabled_skills or set()
    names = []
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
        if meta.experimental and f"skill_{name}" not in enabled_experimental_features:
            continue
        if not _check_dependencies(meta):
            continue
        names.append(name)
    return names
