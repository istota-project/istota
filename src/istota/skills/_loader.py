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

import errno
import hashlib
import importlib
import json
import logging
import os
import re
import stat
import tomllib
from dataclasses import dataclass
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
            template=entry.get("template", ""),
            user_path_fn=entry.get("user_path_fn", ""),
            service=entry.get("service", ""),
            key=entry.get("key", ""),
            sensitive=bool(entry.get("sensitive", False)),
            proxy_only=bool(entry.get("proxy_only", False)),
            fallback_var=entry.get("fallback_var", ""),
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
        lines = yaml_text.split("\n")
        idx = 0
        while idx < len(lines):
            line = lines[idx].strip()
            idx += 1
            if not line or line.startswith("#"):
                continue
            colon = line.find(":")
            if colon == -1:
                continue
            key = line[:colon].strip()
            value = line[colon + 1:].strip()
            # Block-style list (key: on its own line, then `- item` lines).
            # The minimal parser otherwise only understands inline [a, b] lists,
            # so a block list would silently parse to "" and any gate keyed on
            # it (e.g. requires_capability) would fail OPEN. Consume the block.
            if value == "":
                items = []
                while idx < len(lines) and lines[idx].strip().startswith("- "):
                    items.append(lines[idx].strip()[2:].strip().strip("'\""))
                    idx += 1
                if items:
                    data[key] = items
                else:
                    data[key] = ""  # genuinely empty scalar (prior behavior)
                continue
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
        requires_capability=_get_list("requires_capability"),
        exclude_memory=_get_bool("exclude_memory"),
        exclude_persona=_get_bool("exclude_persona"),
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
            requires_capability=meta.get("requires_capability", []),
            exclude_memory=meta.get("exclude_memory", False),
            exclude_persona=meta.get("exclude_persona", False),
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


def capability_disabled_skills(
    skill_index: dict[str, SkillMeta],
    available_capabilities: "set[str] | frozenset[str]",
) -> set[str]:
    """Skills whose declared ``requires_capability`` isn't satisfied.

    A skill is dropped when any capability it declares is absent from
    ``available_capabilities`` (all declared capabilities must be present).
    Skills that declare none are never dropped by this gate.
    """
    disabled: set[str] = set()
    for name, meta in skill_index.items():
        if meta.requires_capability and not set(meta.requires_capability).issubset(
            available_capabilities
        ):
            disabled.add(name)
    return disabled


def effective_disabled_skills(config, user_id: str, skill_index: dict[str, SkillMeta]) -> set[str]:
    """The full disabled set for a task: instance-wide + per-user + capability gate.

    Unions ``config.disabled_skills``, the user's per-user ``disabled_skills``,
    and any skill whose ``requires_capability`` isn't in
    ``config.available_capabilities()``. This is the single place the executor
    and the ``skills`` CLI both call so their view of "disabled" can't drift.
    ``config`` is duck-typed (no import) to avoid a config→loader cycle.
    """
    disabled = set(config.disabled_skills)
    user_config = config.get_user(user_id)
    if user_config:
        disabled |= set(user_config.disabled_skills)
    disabled |= capability_disabled_skills(skill_index, config.available_capabilities())
    return disabled


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
    # An exclude must never strip a safety companion (e.g. untrusted_input) that a
    # skill surviving the exclude pass pulled in — that guardrail is not the
    # excluder's to drop. Recompute companions over the post-exclude selection and
    # protect them. Today nothing excludes a companion, so this is dormant; it
    # keeps a future skill's exclude_skills from silently disarming an ingest
    # skill's companion (unlike `skills show`, the select path has no loud marker).
    survivors = sorted(selected - excluded)
    protected = set(expand_companions(
        survivors, skill_index,
        is_admin=is_admin,
        disabled_skills=disabled,
        enabled_experimental_features=enabled_experimental_features,
    ))
    for ex in excluded - protected:
        selected.discard(ex)
        reasons.pop(ex, None)

    result = sorted(selected)
    if result:
        trace = ", ".join(f"{n}({reasons.get(n, '?')})" for n in result)
        logger.info("pass1_selection count=%d: %s", len(result), trace)
    return result


def format_cli_skills(skill_index: dict[str, SkillMeta], *, is_admin: bool) -> str:
    """Generate a prompt-ready list of skills that have CLI tools.

    Returns a formatted string listing each CLI skill with its command
    and description, suitable for inclusion in the tools section of a prompt.
    Returns empty string if no CLI skills exist.

    ``is_admin`` is keyword-only and has no default on purpose. This list is
    built straight off ``meta.cli``, and the skill proxy's ``allowed_skills``
    is likewise every ``cli: true`` skill — so an ``admin_only`` CLI omitted
    here is simply never mentioned to a non-admin, while one left in would be
    both advertised and executable. The other ``admin_only`` gates (eager
    selection, companion expansion, the on-demand menu) don't cover this path.
    """
    lines = []
    for name in sorted(skill_index):
        meta = skill_index[name]
        if meta.cli and (is_admin or not meta.admin_only):
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


# --------------------------------------------------------- per-skill overlays

#: Skills that accept no user overlay. Not a security boundary — the user can
#: already fork either doc through the operator override — but a guard against
#: a casual preference line landing in the safety layer.
OVERLAY_DENYLIST = frozenset({"sensitive_actions", "untrusted_input"})

#: Above this the file is reported but still loaded. The number is an early
#: warning for ``OVERLAY_MAX_BYTES`` and nothing else: past that one the overlay
#: stops loading at all, and this is the band where there is still room to
#: shrink the file before that happens.
#:
#: It was 8 KB, on a different theory — that an overlay past a handful of
#: preference lines was probably a forked skill doc, dropped in by someone
#: half-remembering the *operator* override, which is replace semantics.
#: ISSUE-337 ended that theory: it cut the developer skill's workflow out of the
#: bundled body and named ``config/skills/developer.md`` as its home, so a
#: 20-something KB overlay on that one skill is now the designed outcome. At
#: 8 KB the threshold reported that configuration as a suspected fork on every
#: load and held ``doctor``'s ``config.skill_overlays`` at WARN for as long as
#: the file existed — a signal that fires on what the design asks for is
#: training to ignore the surface it fires on.
#:
#: It is not a prompt-budget control and never was. Nothing measures the
#: combined size, and the bundled body an overlay is appended to (38 KB for
#: ``developer``) is unmeasured.
OVERLAY_WARN_BYTES = 24 * 1024

#: Above this the file is not loaded at all. The failure mode the cap guards
#: against is someone — quite possibly the agent, half-remembering the
#: *operator* override, which is replace semantics — dropping a forked 47 KB
#: skill doc in here and getting two contradictory bodies in one prompt with no
#: error anywhere.
OVERLAY_MAX_BYTES = 32 * 1024

#: ``{user_id}`` is substituted by the call sites, alongside ``{scripts_dir}``
#: and the rest.
#:
#: Level 4 for a narrower reason than "it stays inside the skill's ``### ``",
#: which is not true and was claimed here: the bundled bodies carry their own
#: undemoted ``## `` headings (16 in ``developer``, 3 in ``notes``), and
#: ``load_skills`` inserts them verbatim, so a skill's ``### `` section is
#: already closed by its own first ``## `` long before the overlay is appended.
#: Demoting the *bundled* bodies would be a behaviour change across every skill
#: and every golden, and those headings are deliberate upstream authoring.
#: What the level does buy is real and is the reason the overlay is demoted to
#: match: the user's rules read as a subsection of their own label rather than
#: as a new section peer to ``## Skills Reference``, so they stay attached to
#: the skill they configure instead of floating up as general prompt text.
OVERLAY_LABEL = "#### {user_id}'s configuration for this skill"

#: Scoped to *this skill*, deliberately, and not to "anything above".
#: ``load_skills`` renders the eager set in sorted order, so a skill body that
#: sorts earlier is literally above this line — and ``sensitive_actions`` sorts
#: before ``skills``, ``todos`` and every ``t``-``z`` name, while ``skills`` is
#: ``always_include`` and therefore eager on every task. An unscoped claim of
#: precedence would have the daemon writing "supersede anything above" around
#: user text that sits below the safety body, which is the outcome
#: ``OVERLAY_DENYLIST`` exists to prevent and which the denylist cannot reach,
#: since it filters by *filename*. The Goals this implements say the overlay
#: wins against the bundled body; that is what it now says.
OVERLAY_PREAMBLE = (
    "These instructions come from the user and take precedence over this "
    "skill's instructions above, wherever the two conflict."
)

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

#: A level-1 or level-2 ATX heading, in every form CommonMark accepts one: up to
#: three leading spaces, and a space, a tab or the end of the line after the
#: hashes. Matching the bare ``"## "`` prefix instead let five spellings through
#: — ``" ## x"``, ``"  ## x"``, ``"   ## x"``, ``"##\tx"`` and a bare ``"##"`` —
#: each of which is a real heading and so a real way out of the section. Level 1
#: is included because it escapes strictly harder than level 2: it closes the
#: whole ``## Skills Reference`` block rather than one skill's ``### ``.
_SHALLOW_HEADING_RE = re.compile(r"^ {0,3}#{1,2}(?=[ \t]|$)")


#: A setext underline: ``Text`` on one line and ``===`` or ``---`` under it is a
#: level-1 or level-2 heading, and the most compact way to write one.
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")

#: First characters that mean the line above an underline is not a paragraph,
#: so the pair is a list item plus a thematic break rather than a heading.
_NOT_PARAGRAPH_START = ("#", "-", "*", "+", ">", "|", "=", "`", "~")


def _is_setext_paragraph(line: str) -> bool:
    """Whether ``line`` could be the text half of a setext heading."""
    if not line.strip():
        return False
    if line[:1] in (" ", "\t"):
        return False
    return line[:1] not in _NOT_PARAGRAPH_START


def _closed_fence_lines(lines: list[str]) -> set[int]:
    """Indices of lines inside a code fence that is actually *closed*.

    An unterminated fence is not treated as one, and that is the whole reason
    this is a separate pass. Exempting it would hand a hand-edited overlay a way
    to switch the demotion off for everything after a single stray ``` line —
    and since the fence runs on past the end of the overlay, the level-2 heading
    it protects then sits as a sibling of ``## Skills Reference`` and swallows
    every skill rendered after it. The exemption exists to protect a sample, and
    there is no sample without a closing fence.

    Fence matching follows CommonMark on the three points that decide the
    pairing: the closer uses the same character as the opener, it is at least as
    long, and it carries no info string. That last one matters here — treating
    `````python`` as a closer would end the block early and rewrite a
    ``## `` that really is inside the user's sample, which is the one thing the
    exemption exists to avoid.
    """
    fenced: set[int] = set()
    open_at: int | None = None
    opener = ""
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if m is None:
            continue
        run = m.group(1)
        if open_at is None:
            open_at, opener = i, run
        elif (
            run[0] == opener[0]
            and len(run) >= len(opener)
            and not line[m.end():].strip()
        ):
            fenced.update(range(open_at, i + 1))
            open_at = None
    return fenced


def _demote_overlay_headings(text: str) -> str:
    """Push a level-1 or level-2 heading in an overlay down to level 4.

    A shallow heading in an overlay detaches everything after it from the
    ``#### <user>'s configuration for this skill`` label it was written under,
    leaving the user's rules as a section peer to ``## Skills Reference`` — read
    as general prompt text rather than as this skill's configuration. Demoting
    rather than dropping is deliberate: a hand-edited file then misbehaves
    visibly instead of losing content silently.

    Closed fenced blocks are skipped. A ``## `` inside one is a sample — an
    overlay for the notes skill quite plausibly carries a markdown template —
    and rewriting it would corrupt the user's own text. An *unclosed* fence
    earns no such exemption; see ``_closed_fence_lines``.

    Setext headings are demoted too — ``My Rules`` with ``---`` under it is a
    level-2 heading and the most compact way to write one, which makes it the
    obvious hole to leave in a function whose whole job is this. The text line
    becomes an ATX heading and the underline is left where it is, so nothing the
    user wrote is deleted; what was a heading is then a heading plus a thematic
    break.

    Leading indentation is dropped along with the demotion, since the point is
    that what comes out cannot be read as a heading above level 4.
    """
    lines = text.split("\n")
    fenced = _closed_fence_lines(lines)
    out: list[str] = []
    for i, line in enumerate(lines):
        if i not in fenced:
            m = _SHALLOW_HEADING_RE.match(line)
            if m is not None:
                out.append("####" + line[m.end():])
                continue
            if (
                _SETEXT_UNDERLINE_RE.match(line)
                and out
                and (i - 1) not in fenced
                and _is_setext_paragraph(out[-1])
            ):
                out[-1] = "#### " + out[-1].strip()
        out.append(line)
    return "\n".join(out)


def _denylist_key(skill_name: str) -> str:
    """Normalize a skill name for the denylist test.

    ``load_skills`` already treats ``-`` and ``_`` as interchangeable when it
    builds a title, and ``_resolve_skill_doc_path`` accepts a legacy flat
    ``<name>.md`` — so a legacy ``_index.toml`` keyed ``sensitive-actions``
    would otherwise take an overlay while ``sensitive_actions`` would not. Not
    reachable on the shipped tree, whose bundled directories are all
    underscored, but the exact-string test made the guard depend on a spelling
    rather than on the skill.
    """
    return skill_name.replace("-", "_")


#: A YAML mapping key, which is what a real frontmatter block is made of.
_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\s*:")


def _looks_like_frontmatter(text: str) -> bool:
    """Whether a leading ``---`` block is frontmatter rather than a rule.

    ``_strip_frontmatter`` keys on the delimiter alone and returns everything
    past the next ``---`` line. In a bundled skill doc that is safe: those are
    written by people who know the convention. In an overlay it is silent data
    loss — a hand-written file that opens with a ``---`` divider loses every
    rule above the next one, and the only signal is the absence of text nobody
    is looking for. So the block has to actually look like a mapping before any
    of it is dropped.
    """
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    body = [ln for ln in text[3:end].split("\n") if ln.strip()]
    if not body:
        return False
    return all(
        _FRONTMATTER_KEY_RE.match(ln) or ln[:1] in (" ", "\t") for ln in body
    )


def overlay_effective_body(text: str) -> str:
    """What the loader will actually use from an overlay file, or ``""``.

    Frontmatter stripped and whitespace trimmed — everything between decoding
    the bytes and demoting the headings. An empty return means the loader
    treats the file as though it were not there.

    Module API because two other places have to agree with it and cannot
    re-derive it: ``memory skills`` reports whether an overlay *binds*, and
    the memory CLI deletes an overlay whose last bullet has gone so the
    directory stays an honest inventory. Both used to test the raw text for
    emptiness, which parts company with the loader on exactly one input — a
    file holding nothing but frontmatter. That file has bytes, has lines, and
    loads as nothing, so `ls` said configured, `binds` said true, and the
    prompt had none of it. Deriving the answer twice is what made that
    possible; there is one derivation now.
    """
    if _looks_like_frontmatter(text):
        text = _strip_frontmatter(text)
    return text.strip()


#: Ceiling on what any overlay reader will pull into memory, whatever its own
#: policy cap is. `OVERLAY_MAX_BYTES` is the *loading* rule; this is the bound
#: on the read itself, and it is much larger deliberately — a file over the
#: loading cap does not load, and `memory remove --skill` is the only way to
#: bring it back under, so refusing to read it at all would leave the user with
#: a file they can neither use nor shrink. This one exists only so a
#: multi-gigabyte file planted at the path cannot be pulled into the daemon.
OVERLAY_READ_CAP_BYTES = 1024 * 1024

#: Why an overlay file will not reach a prompt. Stable ids: the `memory skills`
#: inventory prints them and `doctor` maps them to statuses, so both surfaces
#: say the same word about the same file.
OVERLAY_UNKNOWN_SKILL = "unknown_skill"
OVERLAY_DENYLISTED = "denylisted"
OVERLAY_SKILL_DISABLED = "skill_disabled"
OVERLAY_EMPTY = "empty"
OVERLAY_OVER_CAP = "over_cap"
OVERLAY_NOT_UTF8 = "overlay_not_utf8"
OVERLAY_IS_A_SYMLINK = "overlay_is_a_symlink"
OVERLAY_NOT_A_REGULAR_FILE = "overlay_not_a_regular_file"
OVERLAY_UNREADABLY_LARGE = "overlay_unreadably_large"
OVERLAY_UNREADABLE = "overlay_unreadable"

#: Said about an overlay that *does* bind. Not reasons — a warning never makes
#: `binds` false, because the file is loaded either way and a surface that
#: conflated the two would tell the user their live customization is inert.
OVERLAY_WARN_LARGE = "over_warn_bytes"
OVERLAY_WARN_SHALLOW_HEADING = "shallow_heading"


def contained_overlay_dir(overlay_dir: Path, user_root: Path) -> Path | None:
    """``overlay_dir`` resolved, or None if it leads outside ``user_root``.

    ``read_overlay_bytes``' ``O_NOFOLLOW`` covers the **last** path component
    and nothing above it, and every component above it is model-writable:
    ``{mount}/Users/{user_id}`` is bound read-write into that user's own
    sandbox, so ``mv config config.real && ln -s /anywhere config`` is two
    commands from inside it. The leaf files at the far end of such a link are
    ordinary regular files, so every leaf-level guard passes and the caller
    reads a directory of the daemon's choosing — measured putting the contents
    of a file outside the mount into the memory-search index, from which
    ``!search`` reads it straight back.

    So containment is the same equality-under-a-known-root rule
    ``sandbox_cache_sweeper`` and ``repos_relocate`` use, and it is stated once
    here because four surfaces face this directory — the loader, the memory
    CLI, the search reindex and ``doctor`` — and a copy that is right in three
    of them is the shape of the hole above.

    **The resolved path is what comes back, and callers must use it.** The
    check and the reads that follow are separated by a directory listing and
    one ``open(2)`` per file, so re-walking by the unresolved name reopens a
    narrower version of the same window.

    Both sides are resolved, because the mount itself is reached through a
    symlink on some hosts and comparing a resolved path to an unresolved root
    reads every path as outside. A missing directory resolves fine and is the
    caller's own ``is_dir`` to check; an unreadable one returns None.
    """
    try:
        resolved = Path(os.path.realpath(overlay_dir))
        root = Path(os.path.realpath(user_root))
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def read_overlay_bytes(
    path: Path, *, max_bytes: int = OVERLAY_READ_CAP_BYTES
) -> tuple[bytes | None, str | None, int | None]:
    """Read an overlay file, refusing anything that is not a plain file.

    Returns ``(data, refusal_reason, size)``. Exactly one of the first two is
    set; ``size`` is the ``fstat`` size where there was an fd to take it from
    and None otherwise. A *missing* file is ``(b"", None, None)`` — absence is
    how ``memory append --skill`` learns to create one, so it must not read as
    a refusal.

    The overlay directory sits under ``{mount}/Users/{user_id}``, which
    ``build_bwrap_cmd`` binds **read-write** into that user's own sandbox. Every
    entry in it is therefore model-writable, and the filename being fixed by the
    skill is not containment — it is the mirror image of what
    ``skill_host_paths`` guards, where the path is free and the caller scopes
    it. Three consequences, none of them theoretical:

    - ``O_NOFOLLOW``, because a symlink planted at ``files.md`` otherwise puts
      up to the cap in bytes of any daemon-readable file into the next prompt —
      or, from ``doctor``, into a check detail that names another user's file.
    - ``S_ISREG``, because a FIFO left at that name blocks ``open(2)`` until
      someone writes to it, and both of the callers that matter run somewhere no
      timeout covers: prompt assembly happens *before* the brain request exists,
      and ``doctor`` runs on the daemon's start-up path. One such file wedges
      every later task for that user, silently. ``O_NONBLOCK`` keeps the open
      itself from blocking while the ``fstat`` decides.
    - the size is checked on the fd *before* the read. Reading the whole file
      and refusing afterwards bounds nothing, and ``MemoryError`` is not an
      ``OSError``, so it would escape the never-raises contract every caller
      depends on rather than degrading to "no overlay".

    One reader for three callers — the loader, the memory CLI and ``doctor`` —
    because the three answers above are the whole of the hardening, and a
    fourth copy of them is a fourth chance for one to be left out. What the
    callers do with a refusal still differs, and that stays theirs: the loader
    degrades it to "no overlay" because its worst case is an inert
    customization, while the CLI must not write through one (that would replace
    whatever was planted with a file the user did not ask for) and ``doctor``
    reports it.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return b"", None, None
    except OSError as e:
        if e.errno == errno.ELOOP:
            return None, OVERLAY_IS_A_SYMLINK, None
        return None, OVERLAY_UNREADABLE, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, OVERLAY_NOT_A_REGULAR_FILE, None
        if st.st_size > max_bytes:
            return None, OVERLAY_UNREADABLY_LARGE, st.st_size
        chunks: list[bytes] = []
        remaining = max_bytes
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), None, st.st_size
    except OSError:
        return None, OVERLAY_UNREADABLE, None
    finally:
        os.close(fd)


@dataclass(frozen=True)
class OverlayInspection:
    """One overlay file, and whether it will actually reach a prompt.

    ``reason`` is None exactly when the file binds. ``lines`` and ``first_line``
    are None when there was no body to count — a refused read, or bytes that are
    not UTF-8 — which is deliberately distinct from a file whose body counted to
    zero, since a surface that printed ``lines: 0`` for a planted symlink would
    be describing content it never saw.
    """

    skill: str
    path: Path
    size: int | None
    lines: int | None
    first_line: str | None
    reason: str | None
    warnings: tuple[str, ...]

    @property
    def binds(self) -> bool:
        return self.reason is None


def inspect_overlay(
    path: Path,
    *,
    known_skills,
    disabled_skills=frozenset(),
    max_read_bytes: int = OVERLAY_READ_CAP_BYTES,
) -> OverlayInspection:
    """Everything a reporting surface needs to say about one overlay file.

    The gates are the loader's own, in the loader's own order, because the
    question two surfaces ask about an overlay is the same question and the
    failure mode is that they drift: ``memory skills`` says a file binds, the
    prompt does not contain it, and nothing anywhere reconciles the two. So
    ``binds`` is derived here, once, and the emptiness test is
    ``overlay_effective_body`` rather than ``.strip()`` — a file holding
    nothing but frontmatter has bytes and has lines and loads as nothing.

    ``disabled_skills`` defaults to empty because not every caller cares. The
    CLI passes ``effective_disabled_skills`` so its inventory can say a file is
    filed correctly and still inert; ``doctor`` deliberately passes nothing,
    since an overlay for a skill the operator switched off is a file that will
    bind again the moment the skill comes back and is not a defect to report.

    Never raises: every read failure is a ``reason``.
    """
    skill = path.name[: -len(".md")] if path.name.endswith(".md") else path.name
    reason: str | None = None
    if skill not in known_skills:
        reason = OVERLAY_UNKNOWN_SKILL
    elif _denylist_key(skill) in OVERLAY_DENYLIST:
        reason = OVERLAY_DENYLISTED
    elif skill in disabled_skills:
        reason = OVERLAY_SKILL_DISABLED

    raw, refusal, size = read_overlay_bytes(path, max_bytes=max_read_bytes)
    lines: int | None = None
    first_line: str | None = None
    warnings: list[str] = []

    if refusal is not None:
        reason = reason or refusal
    else:
        try:
            text = (raw or b"").decode("utf-8")
        except UnicodeDecodeError:
            reason = reason or OVERLAY_NOT_UTF8
        else:
            effective = overlay_effective_body(text)
            body = [ln for ln in effective.split("\n") if ln.strip()]
            lines = len(body)
            first_line = body[0].strip() if body else ""
            if not body:
                reason = reason or OVERLAY_EMPTY
            elif size is not None and size > OVERLAY_MAX_BYTES:
                reason = reason or OVERLAY_OVER_CAP
            else:
                if size is not None and size > OVERLAY_WARN_BYTES:
                    warnings.append(OVERLAY_WARN_LARGE)
                # The predicate is the demotion itself rather than a second
                # regex over the text: `_demote_overlay_headings` already knows
                # about closed fences, indented hashes and setext underlines,
                # and a warning that disagreed with the rewrite would report a
                # sample inside a code block or miss an underlined heading.
                if _demote_overlay_headings(effective) != effective:
                    warnings.append(OVERLAY_WARN_SHALLOW_HEADING)

    return OverlayInspection(
        skill=skill,
        path=path,
        size=size,
        lines=lines,
        first_line=first_line,
        reason=reason,
        warnings=tuple(warnings),
    )


def _load_user_overlay(
    user_overlay_dir: Path | None,
    skill_name: str,
    bot_name: str,
    bot_dir: str,
) -> str | None:
    """Read and render one skill's per-user overlay body, or None.

    Every failure path returns None, so a missing directory, an unreadable file,
    an over-cap one and a denylisted skill all degrade to exactly the prompt
    this skill would have had with no overlay at all. The worst case here is
    that a customization is inert.
    """
    if user_overlay_dir is None or _denylist_key(skill_name) in OVERLAY_DENYLIST:
        return None

    path = user_overlay_dir / f"{skill_name}.md"
    # `max_bytes` is the loading cap here, not the absolute read ceiling: past
    # it the file is not loaded at all, so there is nothing to gain by reading
    # it. The CLI passes the larger bound instead, because it has to be able to
    # read a file back in order to shrink it.
    raw, refusal, size = read_overlay_bytes(path, max_bytes=OVERLAY_MAX_BYTES)
    if refusal == OVERLAY_UNREADABLY_LARGE:
        logger.warning(
            "skill overlay %s is %s bytes (cap %d) — not loaded. An overlay "
            "is appended to the bundled body, not a replacement for it.",
            path, size, OVERLAY_MAX_BYTES,
        )
        return None
    if refusal is not None:
        # `debug`, not `warning`, and the level is the whole point. This runs
        # once per eager skill per task, and every condition it reports is one
        # a task can create for itself — a symlink or a FIFO planted at the
        # path, or a `config` entry replaced with a regular file. A `warning`
        # reaches three places: the rotating app log, the admin Logs pane, and
        # — from a `skills show` subprocess, which configures no logging at all,
        # so Python's `lastResort` handler takes it — stderr, where it lands in
        # the model's own tool output. One planted file would repeat in all
        # three on every task for that user. The surface that is supposed to
        # report this is `doctor`'s `config.skill_overlays`, once, on a stated
        # cadence. (It does *not* reach the operator's Talk log channel, which
        # this comment claimed for a while: `LogChannelSubscriber` consumes task
        # events, and `logging_setup` installs a console and a file handler and
        # nothing else.)
        logger.debug(
            "skill overlay %s could not be read (%s) — not loaded", path, refusal
        )
        return None
    assert raw is not None

    if len(raw) > OVERLAY_WARN_BYTES:
        # `debug`, for the reason the refusals above are, and it applies harder
        # here: this condition persists, so at `warning` it repeats on every
        # load of this skill for as long as the file is that size — including
        # into the model's own tool output, since a `skills show` subprocess
        # prints a warning bare to stderr. `doctor` reports it once instead.
        logger.debug(
            "skill overlay %s is %d bytes, approaching the %d-byte cap",
            path, len(raw), OVERLAY_MAX_BYTES,
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("skill overlay %s is not valid UTF-8 — not loaded", path)
        return None

    body = overlay_effective_body(text)
    if not body:
        return None
    body = _demote_overlay_headings(body)
    return body.replace("{BOT_NAME}", bot_name).replace("{BOT_DIR}", bot_dir)


def load_skills(
    skills_dir: Path,
    skill_names: list[str],
    bot_name: str = "Istota",
    bot_dir: str = "",
    skill_index: dict[str, SkillMeta] | None = None,
    bundled_dir: Path | None = None,
    user_overlay_dir: Path | None = None,
) -> str:
    """Load and concatenate selected skill docs, substituting placeholders.

    ``user_overlay_dir`` is the user's ``config/skills/`` directory (None when
    the deployment has no mount, or for a caller with no user in hand). Applying
    the overlay here rather than at the call sites is the point: there are two
    load paths — the eager one in ``executor`` and ``skills show`` — and this
    codebase keeps getting bitten by the two drifting.
    """
    if not bot_dir:
        bot_dir = bot_name.lower()

    if bundled_dir is None:
        bundled_dir = _BUNDLED_SKILLS_DIR

    # One stat instead of one open per eager skill. Nothing creates this
    # directory — `ensure_user_directories_v2` does not — so on almost every
    # deployment it is absent, and the target is the network mount this repo has
    # already moved every database off.
    if user_overlay_dir is not None and not user_overlay_dir.is_dir():
        user_overlay_dir = None

    parts = []
    for name in skill_names:
        meta = skill_index.get(name) if skill_index else None
        doc_path = _resolve_skill_doc_path(name, meta, skills_dir, bundled_dir)
        if doc_path is not None:
            title = name.replace("-", " ").replace("_", " ").title()
            content = _strip_frontmatter(doc_path.read_text()).strip()
            content = content.replace("{BOT_NAME}", bot_name).replace("{BOT_DIR}", bot_dir)
            block = f"### {title}\n\n{content}"
            overlay = _load_user_overlay(user_overlay_dir, name, bot_name, bot_dir)
            if overlay:
                block += f"\n\n{OVERLAY_LABEL}\n\n{OVERLAY_PREAMBLE}\n\n{overlay}"
            parts.append(block)

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

    The *user* tree is deliberately not scanned, so editing a per-skill overlay
    does not move the fingerprint and does not fire the "skills changed, here's
    the changelog" notice in ``executor``. That is required behaviour rather
    than an oversight: a changelog that fires because the user edited their own
    overlay, and then says nothing about that edit, is pure noise.
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
    with unmet dependencies. The on-demand menu catalogue uses this so the model
    self-selects from the full eligible menu.

    Note: no resource gate. The catalogue surfaces the full eligible menu so the
    model self-selects; re-narrowing it to a resource match would defeat that.
    No bundled
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
