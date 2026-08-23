"""Goldens over the fully assembled task prompt.

``execute_task(..., dry_run=True)`` returns the prompt the brain would have been
handed, as the second element of its four-tuple, with every layer in place —
emissaries, persona, channel guidelines, the workspace and file-tool vocabulary,
the eager skill bodies, the on-demand menu, the CLI-tool list, conversation
context, memory, and the rules block. Snapshotting that across a matrix catches
the failure mode substring assertions decay away from: a layer that silently
stops being included. An intentional change is a reviewed golden update via
``ISTOTA_UPDATE_GOLDEN=1``.

**No container, and no model.** ``dry_run`` returns *after* assembly rather than
instead of it, so the path below really does run sticky-skill lookup, memory
reads through ``storage.py``, calendar discovery and conversation-context
selection. Two things keep it brain-free by construction rather than by accident
of how much history a case happens to have: every case pins
``conversation.use_selection = false`` (``context.select_relevant_context``
returns early on it, before the fast model), and no case configures CalDAV
(``discover_calendars_for_task`` returns ``[]`` without opening a socket). If a
case ever reaches a model, that is a defect in the fixture, not something to
paper over with a mock.

**Synthetic skills, not the bundled catalogue.** ``bundled_skills_dir`` points at
six skills this module writes. The goldens are then about prompt *structure* —
which layers appear, in what order, with what vocabulary — and not about the
wording of any shipped skill, so an edit to ``src/istota/skills/*/skill.md``
does not regenerate twelve files. The six are shaped to cover the gates that
change the menu: ``always_include``, ``source_types``, ``companion_skills``,
``admin_only`` and ``requires_capability``.

**The storage backend is a dimension here** because two of its three
differences are prompt content: ``storage_backend`` selects the file-tool
vocabulary and the ``{storage}`` / ``{workspace}`` substitution in a skill body,
and ``available_capabilities()`` drops ``nextcloud`` when the URL is empty,
which drops the capability-gated skill from the menu. ``base_nextcloud`` and
``base_local`` are the pair; they differ in nothing else. The third difference
is ``runtime.mount_liveness``, which is a ``doctor`` check and lives in
``tests/test_doctor.py``.

What is deliberately *not* covered, so nobody reads the goldens as exhaustive:
the recalled-memory, knowledge-graph and playbook layers, all three of which are
off by default in the product (``memory_search.auto_recall``,
``playbooks.enabled``); dated memories, whose filenames are wall-clock coupled;
and the skills changelog, which needs a fingerprint mismatch against a stored
one. A case for any of those is a case someone can add.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from istota import db, executor
from istota.config import (
    Config,
    ConversationConfig,
    NextcloudConfig,
    SecurityConfig,
    UserConfig,
)
from istota.executor import execute_task

GOLDEN_DIR = Path(__file__).parent / "golden" / "prompts"
UPDATE_ENV = "ISTOTA_UPDATE_GOLDEN"
DRY_RUN_PREFIX = "[DRY RUN] Would execute with prompt:\n\n"

USER = "alice"
OTHER_USER = "bob"

# ---------------------------------------------------------------- normalization


def normalize(text: str, *, tmp_path: Path) -> str:
    """Strip everything that would make a committed golden fail tomorrow.

    Three classes, all of which a first draft embeds without noticing: the
    wall-clock header lines, the task id, and the per-run temp directory. The
    replacements are anchored on the line they belong to rather than applied as
    a bare date regex, so a golden that legitimately contained a date would keep
    it — and ``test_a_golden_carries_no_run_specific_value`` fails loudly if any
    of the three survives.
    """
    # The per-run temp root, longest form first: on macOS `tmp_path` is
    # `/private/var/...` while a path built through `tempfile` resolves to
    # `/var/...`, and replacing the short one first would leave `/private<TMP>`.
    roots = {str(tmp_path.resolve()), str(tmp_path)}
    for root in sorted(roots, key=len, reverse=True):
        text = text.replace(root, "<TMP>")

    text = re.sub(r"^Current time: .*$", "Current time: <TIME>", text, flags=re.M)
    text = re.sub(r"^Today's date: .*$", "Today's date: <DATE>", text, flags=re.M)
    text = re.sub(r"^Current UTC: .*$", "Current UTC: <UTC>", text, flags=re.M)
    text = re.sub(
        r"^Current task ID: \d+$", "Current task ID: <TASK_ID>", text, flags=re.M
    )
    # Task ids referenced inline, e.g. the scheduled-notification context header.
    text = re.sub(r"\(task \d+\)", "(task <TASK_ID>)", text)
    return text


# ------------------------------------------------------------------- the skills

# name -> (frontmatter lines, body). Six skills, each present to exercise one
# gate that decides eager-vs-menu-vs-absent.
SKILLS: dict[str, tuple[list[str], str]] = {
    # Eager on every case, and the one body carrying the storage placeholders.
    "notes": (
        ["always_include: true"],
        "NOTES BODY.\nworkspace={workspace}\nstorage={storage}\n"
        "scripts={scripts_dir}\nuser={user_id}",
    ),
    # Never selected by a rule -> menu entry only, under both backends.
    "developer": ([], "DEVELOPER BODY"),
    # The capability gate: absent from menu and CLI list on the local backend.
    "nextcloud": (["requires_capability: [nextcloud]"], "NEXTCLOUD BODY"),
    # The admin gate: absent for a non-admin.
    "operator": (["admin_only: true"], "OPERATOR BODY"),
    # Source-selected eager on `web`, and it drags a companion in with it.
    "triage": (
        ["source_types: [web]", "companion_skills: [untrusted_input]"],
        "TRIAGE BODY",
    ),
    # cli: false -> never in the CLI-tool list; eager only as triage's companion.
    "untrusted_input": (["cli: false"], "UNTRUSTED INPUT BODY"),
}


def _write_skills(bundled: Path) -> None:
    for name, (extra, body) in SKILLS.items():
        d = bundled / name
        d.mkdir(parents=True, exist_ok=True)
        front = [
            "---",
            f"name: {name}",
            f"description: the {name} skill",
        ]
        if not any(line.startswith("cli:") for line in extra):
            front.append("cli: true")
        front.extend(extra)
        front.append("---")
        (d / "skill.md").write_text("\n".join(front) + "\n" + body + "\n")


EMISSARIES = "EMISSARIES TEXT\n\nThe constitutional layer.\n"
PERSONA = "PERSONA TEXT for {BOT_NAME}.\n"
GUIDELINES = {
    "talk": "TALK GUIDELINES for {user_id}.",
    "email": "EMAIL GUIDELINES for {user_id}.",
    "web": "WEB GUIDELINES for {user_id}.",
    "briefing": "BRIEFING GUIDELINES for {user_id}.",
}


# --------------------------------------------------------------------- the cases


@dataclass(frozen=True)
class Case:
    """One golden. Everything not named here comes from the shared base."""

    name: str
    #: "nextcloud" (a URL is configured) or "local" (the URL is the empty
    #: string, which is what `render-config.sh` writes for the Nextcloud-free
    #: install).
    backend: str = "nextcloud"
    admin: bool = True
    emissaries: bool = True
    source_type: str = "cli"
    conversation_token: str | None = None
    #: Written into the mount before assembly: user memory and channel memory.
    memory: bool = False
    #: The re-executed half of the untrusted-sender confirmation gate.
    confirmed: bool = False
    #: `_bwrap_available()` is a host probe — a real kernel with bubblewrap
    #: answers differently from a laptop — and it selects one of two rule-3
    #: paragraphs. Pinned per case so a golden means the same thing on both.
    sandbox_masked: bool = False


CASES: tuple[Case, ...] = (
    # The base, and its storage-backend counterpart. These two differ in
    # nothing but `nextcloud.url`.
    Case("base_nextcloud"),
    Case("base_local", backend="local"),
    # Admin vs not, on the same task. `operator` leaves the menu with the
    # privilege, and the rules block changes wording.
    Case("nonadmin", admin=False),
    # The constitutional layer, present and absent.
    Case("emissaries_off", emissaries=False),
    # source_type: cli is the base; the other four each pull their own
    # channel guidelines and their own output target.
    Case("source_talk", source_type="talk", conversation_token="room-token"),
    Case("source_email", source_type="email"),
    Case("source_briefing", source_type="briefing"),
    # `web` is also the eager-plus-companion case: `triage` is source-selected
    # and drags `untrusted_input` in, leaving `developer` and `nextcloud` in
    # the menu.
    Case("source_web", source_type="web", conversation_token="web-room"),
    # The other half of the confirmation gate. An untrusted sender parks the
    # task; what reaches assembly on the re-execution is the confirmation
    # context. `source_email` is the trusted counterpart.
    Case("email_confirmed", source_type="email", confirmed=True),
    # Memory present, against every other case's absent.
    Case(
        "memory_present",
        source_type="talk",
        conversation_token="room-token",
        memory=True,
    ),
    # The masked-database rule, which the sandbox probe selects.
    Case("sandbox_masked", sandbox_masked=True),
)

CASES_BY_NAME = {c.name: c for c in CASES}


def _build_config(case: Case, tmp_path: Path) -> Config:
    config_dir = tmp_path / "config"
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    bundled = tmp_path / "bundled"
    _write_skills(bundled)

    if case.emissaries:
        (config_dir / "emissaries.md").write_text(EMISSARIES)
    (config_dir / "persona.md").write_text(PERSONA)
    guidelines = config_dir / "guidelines"
    guidelines.mkdir(exist_ok=True)
    for source_type, text in GUIDELINES.items():
        (guidelines / f"{source_type}.md").write_text(text + "\n")

    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)

    db.init_db(tmp_path / "framework.db")

    # The empty string, not a missing key: `Config.storage_is_nextcloud` is
    # `bool(self.nextcloud.url)`, and `render-config.sh` renders `url = ""` for
    # the Nextcloud-free install (an *unset* NC_URL fails its preflight).
    url = "https://cloud.example.test" if case.backend == "nextcloud" else ""

    return Config(
        db_path=tmp_path / "framework.db",
        temp_dir=tmp_path / "temp",
        skills_dir=skills_dir,
        bundled_skills_dir=bundled,
        nextcloud_mount_path=mount,
        nextcloud=NextcloudConfig(url=url, username="istota"),
        # No LLM triage of history: `select_relevant_context` returns before the
        # fast model on this flag.
        conversation=ConversationConfig(use_selection=False),
        security=SecurityConfig(sandbox_enabled=case.sandbox_masked),
        emissaries_enabled=case.emissaries,
        admin_users=set() if case.admin else {OTHER_USER},
        users={
            USER: UserConfig(
                display_name="Alice Example",
                email_addresses=[f"{USER}@example.test"],
                timezone="UTC",
            )
        },
    )


def _build_task(case: Case) -> db.Task:
    fields = dict(
        id=1,
        status="running",
        user_id=USER,
        source_type=case.source_type,
        prompt="Summarize what changed in my notes this week.",
        conversation_token=case.conversation_token,
    )
    if case.confirmed:
        fields["confirmed_at"] = "2026-01-01T00:00:00Z"
        fields["confirmation_prompt"] = (
            "I drafted a reply to the sender and paused for your approval."
        )
    return db.Task(**fields)


def _seed_memory(config: Config, case: Case) -> None:
    if not case.memory:
        return
    from istota.storage import (
        _get_mount_path,
        get_channel_memory_path,
        get_user_memory_path,
    )

    user_memory = _get_mount_path(
        config, get_user_memory_path(USER, config.bot_dir_name)
    )
    user_memory.parent.mkdir(parents=True, exist_ok=True)
    user_memory.write_text("USER MEMORY: alice prefers terse answers.\n")

    assert case.conversation_token
    channel_memory = _get_mount_path(
        config, get_channel_memory_path(case.conversation_token)
    )
    channel_memory.parent.mkdir(parents=True, exist_ok=True)
    channel_memory.write_text("CHANNEL MEMORY: this room is for release notes.\n")


def assemble(case: Case, tmp_path: Path, monkeypatch) -> str:
    """Run one case to a normalized prompt."""
    # A host-capability probe, not a collaborator: `_bwrap_available` shells out
    # to bwrap, so it answers False on a laptop and True on the Linux runner,
    # and it picks one of two rule-3 paragraphs. Pinning it is what makes the
    # golden mean the same thing on both.
    monkeypatch.setattr(executor, "_bwrap_available", lambda: case.sandbox_masked)

    config = _build_config(case, tmp_path)
    _seed_memory(config, case)
    task = _build_task(case)

    success, result, _actions, _trace = execute_task(task, config, [], dry_run=True)
    assert success, result
    assert result.startswith(DRY_RUN_PREFIX), result[:200]
    return normalize(result[len(DRY_RUN_PREFIX):], tmp_path=tmp_path)


# ---------------------------------------------------------------------- the test


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_prompt_golden(case, tmp_path, monkeypatch):
    prompt = assemble(case, tmp_path, monkeypatch)
    golden = GOLDEN_DIR / f"{case.name}.txt"

    if os.environ.get(UPDATE_ENV):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(prompt, encoding="utf-8")
        return

    assert golden.exists(), (
        f"no golden for case {case.name!r}. Generate it with "
        f"`{UPDATE_ENV}=1 uv run pytest tests/test_prompt_golden.py` and review "
        f"the result like any other change."
    )
    expected = golden.read_text(encoding="utf-8")
    assert prompt == expected, (
        f"the assembled prompt for {case.name!r} differs from "
        f"{golden.relative_to(GOLDEN_DIR.parent.parent)}. If the change is "
        f"intended, regenerate with `{UPDATE_ENV}=1 uv run pytest "
        f"tests/test_prompt_golden.py` and review the diff."
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_golden_carries_no_run_specific_value(case, tmp_path, monkeypatch):
    """The normalization helper is total.

    Without this, the first golden to embed a `tmp_path` or a wall-clock stamp
    passes on the day it is written and fails on every day after, and the fix
    someone reaches for is a regeneration rather than a normalization.
    """
    prompt = assemble(case, tmp_path, monkeypatch)

    assert str(tmp_path) not in prompt
    assert str(tmp_path.resolve()) not in prompt
    assert "/var/folders/" not in prompt
    assert not re.search(r"\b20\d\d-\d\d-\d\dT", prompt), "an ISO timestamp survived"
    assert not re.search(
        r"^(Current time|Today's date|Current UTC|Current task ID):(?!\s<)",
        prompt,
        flags=re.M,
    ), "a header line was not normalized"


def test_every_golden_file_belongs_to_a_case():
    """A renamed case must not leave a stale file behind.

    A golden nothing reads is worse than no golden: it looks like coverage in a
    directory listing and asserts nothing.
    """
    if not GOLDEN_DIR.exists():
        pytest.skip("no goldens generated yet")
    on_disk = {p.stem for p in GOLDEN_DIR.glob("*.txt")}
    assert on_disk == set(CASES_BY_NAME), (
        f"orphaned goldens: {sorted(on_disk - set(CASES_BY_NAME))}; "
        f"missing goldens: {sorted(set(CASES_BY_NAME) - on_disk)}"
    )


class TestTheStorageBackendDimension:
    """The two prompt-visible differences between the backends, named.

    The goldens already carry them, but a golden diff says "these 70 lines
    changed" and not "the capability gate stopped working". These name the two
    rows of the design's three-row table that live in the prompt, so the narrow
    controls for them fail by name.
    """

    def _pair(self, tmp_path, monkeypatch):
        nextcloud = assemble(
            CASES_BY_NAME["base_nextcloud"], tmp_path / "nc", monkeypatch
        )
        local = assemble(CASES_BY_NAME["base_local"], tmp_path / "local", monkeypatch)
        return nextcloud, local

    def test_the_file_tool_vocabulary_follows_the_backend(self, tmp_path, monkeypatch):
        nextcloud, local = self._pair(tmp_path, monkeypatch)

        assert "Nextcloud files are mounted at" in nextcloud
        assert "Nextcloud files are mounted at" not in local
        assert "Your files live in your workspace at" in local

        # `{storage}` in an eager skill body, via `Config.storage_label`.
        assert "storage=Nextcloud" in nextcloud
        assert "storage=your workspace" in local

    def test_the_capability_gate_drops_the_nextcloud_skill_from_the_menu(
        self, tmp_path, monkeypatch
    ):
        nextcloud, local = self._pair(tmp_path, monkeypatch)

        # No URL -> `available_capabilities()` omits "nextcloud" ->
        # `effective_disabled_skills` folds the skill in -> it leaves both eager
        # selection and the on-demand menu.
        assert "  - nextcloud: the nextcloud skill" in nextcloud
        assert "  - nextcloud: the nextcloud skill" not in local
        assert "NEXTCLOUD BODY" not in nextcloud + local

    def test_the_cli_tool_list_does_not_apply_the_capability_gate(
        self, tmp_path, monkeypatch
    ):
        """Recorded, not endorsed.

        `format_cli_skills` is built straight off `meta.cli` plus the
        `admin_only` flag; it consults neither `effective_disabled_skills` nor
        `available_capabilities()`. So on the Nextcloud-free install the model
        is still told `istota-skill nextcloud` exists, having been told it is
        not a skill it may load. The same holds for an operator-disabled skill.

        Found while writing the backend dimension of these goldens, left alone
        as a product change this stage did not scope. `base_local.txt` carries
        the line, so a fix shows up as a reviewed golden diff and turns this
        test red rather than passing silently.
        """
        _nextcloud, local = self._pair(tmp_path, monkeypatch)

        assert "`istota-skill nextcloud` — the nextcloud skill" in local


def test_a_custom_system_prompt_does_not_change_the_assembled_prompt(
    tmp_path, monkeypatch
):
    """Recorded because the spec's matrix asks for the pair and the code has none.

    `config.custom_system_prompt` selects `config/system-prompt.md` as the
    brain's *system* prompt — `custom_system_prompt_path` is read at
    `executor.py`'s brain-request assembly, which is several hundred lines past
    the `dry_run` return. So the two settings produce byte-identical task
    prompts, and two goldens would be two copies of one file. Asserting the
    identity keeps the claim checkable: if a future change routes the custom
    system prompt into the task prompt, this goes red and the matrix gains a
    real pair.
    """
    case = CASES_BY_NAME["base_nextcloud"]

    plain = assemble(case, tmp_path / "plain", monkeypatch)

    monkeypatch.setattr(executor, "_bwrap_available", lambda: case.sandbox_masked)
    config = _build_config(case, tmp_path / "custom")
    config.custom_system_prompt = True
    (config.skills_dir.parent / "system-prompt.md").write_text("CUSTOM SYSTEM PROMPT\n")
    success, result, _a, _t = execute_task(_build_task(case), config, [], dry_run=True)
    assert success
    custom = normalize(
        result[len(DRY_RUN_PREFIX):], tmp_path=tmp_path / "custom"
    )

    assert custom == plain
    assert "CUSTOM SYSTEM PROMPT" not in custom
