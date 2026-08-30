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
returns early on it, before the fast model), and no case resolves a full CalDAV
triple, so ``discover_calendars_for_task`` returns ``[]`` before opening a
socket. That second one is subtler than "configures no CalDAV", which is what
this docstring first claimed and is wrong: ``Config.caldav_url`` falls back to
``{nextcloud.url}/remote.php/dav`` and ``caldav_username`` to
``nextcloud.username``, so the only thing missing is ``caldav_password``, which
resolves to the unset ``nextcloud.app_password``. ``assemble`` asserts the
triple is incomplete rather than trusting the fallback chain, because setting a
realistic ``app_password`` in the fixture is an obvious future edit and would
silently turn the whole matrix into live CalDAV discovery. The autouse
``_no_sockets`` guard is the backstop under both.

**Synthetic skills, not the bundled catalogue.** ``bundled_skills_dir`` points at
the handful of skills this module writes. The goldens are then about prompt
*structure* — which layers appear, in what order, with what vocabulary — and not
about the wording of any shipped skill, so an edit to
``src/istota/skills/*/skill.md`` regenerates nothing here. They are shaped to
cover the gates that decide eager-vs-menu-vs-absent and the two that suppress
whole layers: ``always_include``, ``source_types``, ``companion_skills``,
``admin_only``, ``requires_capability``, ``exclude_persona`` and
``exclude_memory``.

**The storage backend is a dimension here** because two of its three
differences are prompt content: ``storage_backend`` selects the file-tool
vocabulary and the ``{storage}`` / ``{workspace}`` substitution in a skill body,
and ``available_capabilities()`` drops ``nextcloud`` when the URL is empty,
which drops the capability-gated skill from the menu. ``base_nextcloud`` and
``base_local`` are the pair; they differ in nothing else. The third difference
is ``runtime.mount_liveness``, which is a ``doctor`` check and lives in
``tests/test_doctor.py``.

The spec's Behaviour section says to build the ``Config`` and ``Task`` from the
shared ``make_config`` / ``make_task`` fixtures. This module builds its own
instead, for two reasons neither factory can serve: a case needs its own temp
root (several tests assemble twice and compare), and every case needs
``bundled_skills_dir`` pointed at the synthetic set, which ``make_config`` does
not take.

What is deliberately *not* covered, so nobody reads the goldens as exhaustive,
and with the real reason for each rather than one reason stretched over all of
them. Recalled memories and playbooks are off by default in the product
(``memory_search.auto_recall``, ``playbooks.enabled``). The knowledge-graph
block has no config gate at all — it is absent only because the fixture's DB
holds no facts, so seeding one is all a case would take. Dated memories *are*
switched off here, structurally: ``sleep_cycle.auto_load_dated_days`` defaults
to 3 and the reader runs on every case, returning ``None`` only because no
``memories/YYYY-MM-DD.md`` exists — one seeded fixture file away from putting a
wall-clock filename in a golden — so ``_build_config`` pins it to 0 rather than
relying on the absence. The skills changelog needs a fingerprint mismatch
against a stored one. A case for any of these is a case someone can add.
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from istota import db, executor
from istota.config import (
    Config,
    ConversationConfig,
    NextcloudConfig,
    SecurityConfig,
    SleepCycleConfig,
    UserConfig,
)
from istota.executor import custom_system_prompt_path, execute_task

GOLDEN_DIR = Path(__file__).parent / "golden" / "prompts"
UPDATE_ENV = "ISTOTA_UPDATE_GOLDEN"
UPDATE_CMD = f"{UPDATE_ENV}=1 uv run pytest tests/test_prompt_golden.py -n0"
DRY_RUN_PREFIX = "[DRY RUN] Would execute with prompt:\n\n"


def updating() -> bool:
    """Whether this run rewrites the goldens instead of comparing.

    Deliberately not `bool(os.environ.get(...))`. The variable comes from the
    ambient environment and disarms the whole file, so `ISTOTA_UPDATE_GOLDEN=0`
    left exported in a shell would silently turn every golden into a rubber
    stamp forever. The affirmative/negative sets mirror
    `PRECOMMIT_SCANS_REQUIRED`, which the repo already parses this way; anything
    else raises rather than being guessed at.
    """
    raw = os.environ.get(UPDATE_ENV)
    if raw is None or raw == "":
        return False
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(
        f"{UPDATE_ENV}={raw!r} is neither affirmative nor negative. Use "
        f"`{UPDATE_ENV}=1` to rewrite the goldens, or unset it to compare."
    )

USER = "alice"
OTHER_USER = "bob"

# ------------------------------------------------------------- the network guard


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    """"Runs against nothing" is an assertion here, not a claim in a docstring.

    The first draft of this module reached the network on nine of eleven cases
    and said in its own header that it did not. `dry_run` returns *after* full
    assembly, so anything assembly calls is live: the path was
    `read_user_memory_v2` returning None -> `ensure_user_directories_v2` ->
    `ocs_share_folder`, two sockets per Nextcloud-backed case at a ten-second
    timeout, which on a resolver with a search domain is three minutes added to
    the default suite and a POST to whoever owns the name.

    Recording rather than only blocking, because every caller in that path
    swallows exceptions for graceful degradation — a guard that merely raised
    would be caught, the golden would still match, and the property would go
    back to being a claim. The attempt is recorded, then refused, then asserted
    on at teardown where nothing can swallow it.
    """
    import socket

    attempts: list[str] = []

    def _refuse(target: str):
        def _fn(*args, **kwargs):
            attempts.append(f"{target}{args[1:2] or args[:1]}")
            raise OSError(f"network blocked in the prompt goldens: {target}")

        return _fn

    monkeypatch.setattr(socket.socket, "connect", _refuse("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", _refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _refuse("getaddrinfo"))

    yield

    assert not attempts, (
        "prompt assembly opened the network: "
        + "; ".join(sorted(set(attempts)))
        + ". These goldens run against nothing — find what assembly called and "
        "turn it off through configuration, not through a mock."
    )


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
    # The per-run temp root, longest form first. On macOS these are the same
    # string — pytest already hands back the resolved `/private/var/...` form —
    # so the sort is inert there. It matters where `TMPDIR` is a symlink, which
    # is the ordinary Linux-runner case: `str(tmp_path)` is then the short form
    # and `resolve()` the long one, and replacing the short one first would
    # leave the prefix of the long one stranded in front of `<TMP>`.
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
    # The two suppression flags, which live on the skill and not on the config:
    # an eager skill carrying them makes `build_prompt` drop the emissaries and
    # persona layers and `execute_task` skip every memory read. The real
    # `briefing` skill is what carries them in production, and without a
    # synthetic stand-in the briefing golden would be a `cli` prompt wearing
    # briefing guidelines.
    "digest": (
        ["source_types: [briefing]", "exclude_persona: true", "exclude_memory: true"],
        "DIGEST BODY",
    ),
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
    #: Writes a per-skill user overlay for the eager `notes` skill into
    #: `config/skills/notes.md` before assembly.
    overlay: bool = False
    #: Seeds one completed task in the same conversation, so `_build_db_context`
    #: has history to render. Fixed `created_at`, so the rendered `[timestamp]`
    #: is a constant rather than a clock reading.
    history: bool = False
    #: The re-executed half of the untrusted-sender confirmation gate.
    confirmed: bool = False
    #: Two settings, not one: it pins `security.sandbox_enabled` *and* the
    #: `_bwrap_available()` host probe (a real kernel with bubblewrap answers
    #: differently from a laptop), because `executor` selects the rule-3
    #: paragraph on the conjunction of the two. Defaults True so the matrix's
    #: norm is the shipped server norm — `sandbox_enabled` defaults True in
    #: `SecurityConfig` and the deployed daemon has bubblewrap. `sandbox_off`
    #: is the standalone shape, which ships with the sandbox disabled. There
    #: are four rule-3 strings (admin/user x masked/unmasked); the matrix
    #: covers three, leaving non-admin-unmasked to whoever needs it.
    sandboxed: bool = True


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
    # Memory is seeded here on purpose and must NOT appear: the eager `digest`
    # skill carries `exclude_memory`, which is the other half of the pair the
    # briefing case exists to witness. Without the seed, its absence would
    # prove nothing.
    Case("source_briefing", source_type="briefing", memory=True),
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
    # Conversation context present, against every other case's absent. Email
    # rather than talk because the DB path is the one email always takes;
    # `_build_talk_api_context` falls back to it anyway with an empty cache.
    Case(
        "conversation_context",
        source_type="email",
        conversation_token="thread-token",
        history=True,
    ),
    # The standalone shape: `istota setup` ships `sandbox_enabled = false`, and
    # rule 3 then says so rather than claiming a boundary that is not there.
    Case("sandbox_off", sandboxed=False),
    # A per-skill user overlay on the eager `notes` skill. Against
    # `base_nextcloud`, which it differs from in nothing else, so the diff
    # between the two goldens is exactly the injected block — where it lands
    # relative to the body, what the label and precedence line say, and the
    # `## ` demotion.
    Case("skill_overlay", overlay=True),
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
        nextcloud=NextcloudConfig(
            url=url,
            username="istota",
            # Not a mock, and not decoration: this is the shipped knob for the
            # deployment shape where the bot dir reaches the user by a
            # `files_external` mount instead of a share. Left on, assembly's
            # `ensure_user_directories_v2` call reaches
            # `nextcloud/_http.ocs_share_folder`, which is gated on
            # `nc_configured` (url *and* username) and would open two real
            # sockets to `cloud.example.test` per Nextcloud-backed case at a
            # 10 s timeout each. Measured: it moves no golden.
            auto_share_bot_dir=False,
        ),
        # No LLM triage of history: `select_relevant_context` returns before the
        # fast model on this flag.
        conversation=ConversationConfig(use_selection=False),
        # Structural, not incidental: the reader runs on every case and is one
        # seeded `memories/YYYY-MM-DD.md` away from a wall-clock filename in a
        # golden. See the module docstring.
        sleep_cycle=SleepCycleConfig(auto_load_dated_days=0),
        security=SecurityConfig(sandbox_enabled=case.sandboxed),
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

    if not case.conversation_token:
        return
    channel_memory = _get_mount_path(
        config, get_channel_memory_path(case.conversation_token)
    )
    channel_memory.parent.mkdir(parents=True, exist_ok=True)
    channel_memory.write_text("CHANNEL MEMORY: this room is for release notes.\n")


#: Deliberately carries a `## ` heading. The loader demotes it to `####` rather
#: than dropping it, because at level 2 it would close the skill's own `### `
#: section and leave the rest of the overlay reading as a sibling of the whole
#: skills reference. The golden is where that is visible as prompt text.
OVERLAY = (
    "## Notes rules\n\n"
    "- Never write a new file to the base folder.\n"
    "- Frontmatter carries `agents:` on anything the bot wrote.\n"
)


def _seed_overlay(config: Config, case: Case) -> None:
    if not case.overlay:
        return
    from istota.storage import _get_mount_path, get_user_skill_overlays_path

    overlay_dir = _get_mount_path(
        config, get_user_skill_overlays_path(USER, config.bot_dir_name)
    )
    overlay_dir.mkdir(parents=True, exist_ok=True)
    (overlay_dir / "notes.md").write_text(OVERLAY)


def _seed_history(config: Config, case: Case) -> None:
    """One completed prior turn in the same conversation.

    Inserted with SQL rather than through `db.create_task` because
    `created_at` defaults to `datetime('now')` and the context formatter renders
    it into the prompt — a golden would then carry a clock reading. A fixed
    value well outside any plausible recency window is safe here only because
    `conversation.context_recency_hours` defaults to 0, which disables the
    window; if that default ever changes this case goes quiet rather than red,
    so the golden asserts the rendered timestamp by value.
    """
    if not case.history:
        return
    with db.get_db(config.db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, created_at, updated_at, status, source_type,
                conversation_token, user_id, prompt, result
            ) VALUES (?, ?, ?, 'completed', 'email', ?, ?, ?, ?)
            """,
            (
                900,
                "2026-01-05 09:15:00",
                "2026-01-05 09:16:00",
                case.conversation_token,
                USER,
                "What did the release notes say about the migration?",
                "The migration runs on first boot and is idempotent.",
            ),
        )
        conn.commit()


def assemble(case: Case, tmp_path: Path, monkeypatch) -> str:
    """Run one case to a normalized prompt."""
    # A host-capability probe, not a collaborator: `_bwrap_available` shells out
    # to bwrap, so it answers False on a laptop and True on the Linux runner,
    # and it picks one of two rule-3 paragraphs. Pinning it is what makes the
    # golden mean the same thing on both.
    monkeypatch.setattr(executor, "_bwrap_available", lambda: case.sandboxed)

    config = _build_config(case, tmp_path)
    assert not all(
        (config.caldav_url, config.caldav_username, config.caldav_password)
    ), (
        "the fixture resolves a full CalDAV triple, so assembly will attempt "
        "discovery against a real server; see the module docstring"
    )
    _seed_memory(config, case)
    _seed_overlay(config, case)
    _seed_history(config, case)
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

    if updating():
        golden.parent.mkdir(parents=True, exist_ok=True)
        changed = not golden.exists() or golden.read_text(encoding="utf-8") != prompt
        golden.write_text(prompt, encoding="utf-8")
        # Loud, because the alternative is a green run that silently accepted a
        # prompt change nobody looked at. The `git diff` is the review; this is
        # what tells you there is one.
        if changed:
            warnings.warn(
                f"{UPDATE_ENV}: rewrote {golden.name} — review the diff",
                stacklevel=1,
            )
        return

    assert golden.exists(), (
        f"no golden for case {case.name!r}. Generate it with "
        f"`{UPDATE_CMD}` and review the result like any other change."
    )
    expected = golden.read_text(encoding="utf-8")
    assert prompt == expected, (
        f"the assembled prompt for {case.name!r} differs from "
        f"{golden.relative_to(GOLDEN_DIR.parent.parent)}. If the change is "
        f"intended, regenerate with `{UPDATE_CMD}` and review the diff."
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_normalization_is_total(case, tmp_path, monkeypatch):
    """Two assemblies of one case, under different temp roots, must agree.

    The obvious version of this test — assemble once, then assert the temp path
    and the header lines are absent — is four-fifths tautology: it asserts back
    exactly the substitutions `normalize` has just made, and `.*` on a header
    line matches the empty string, so no surviving value is even reachable. It
    therefore cannot catch the thing it is named for, which is a *new*
    run-specific value that `normalize` does not know about.

    Assembling twice is the property stated directly. A second temp root moves
    every path, and the wall clock moves on its own between the two calls, so
    anything unnormalized carrying either shows up as an inequality. The
    wall-clock arms below are kept alongside it because two assemblies a
    millisecond apart usually agree on the minute, and a golden that only fails
    across a minute boundary is worse than one that fails always.
    """
    before = datetime.now(timezone.utc)
    first = assemble(case, tmp_path / "first", monkeypatch)
    second = assemble(case, tmp_path / "second", monkeypatch)

    assert first == second, (
        "the same case assembled twice under different temp roots does not "
        "normalize to the same text, so something run-specific is reaching the "
        "golden that `normalize` does not cover"
    )

    # Platform-neutral backstop for a temp path in a form neither `str(tmp_path)`
    # nor its resolved form matches. `/var/folders/` alone is macOS-only, and
    # the Linux runner is where the goldens are most likely to be regenerated.
    assert "pytest-of-" not in first
    assert "/var/folders/" not in first

    # Nothing rendered off the wall clock survived, in any of the forms the
    # prompt uses. A bare `20\d\d` sweep would be simpler and wrong: the
    # conversation-context case renders a *fixed* seeded date on purpose, and
    # an allowlist of placeholders decays. Both ends of the assembly window are
    # checked so a run straddling midnight cannot slip a day through.
    for now in (before, datetime.now(timezone.utc)):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H", "%A, %B %-d, %Y", "%Y-%m-%d %H:%M"):
            rendered = now.strftime(fmt)
            assert rendered not in first, (
                f"a wall-clock value survived normalization: {rendered!r} "
                f"(format {fmt!r})"
            )


def test_a_skill_overlay_adds_a_block_and_changes_nothing_else(tmp_path, monkeypatch):
    """The eager load path's witness, stated as a difference rather than as a
    substring.

    `skill_overlay` and `base_nextcloud` differ in one seeded file, so removing
    the injected block from the first must yield the second exactly. A substring
    assertion would pass just as happily on a prompt that had also lost a layer
    somewhere else, and the fingerprint arm is the one that would go quiet if
    `compute_skills_fingerprint` ever started scanning the user tree — which
    would fire the skills changelog on an overlay edit and then say nothing
    about it.
    """
    base = assemble(CASES_BY_NAME["base_nextcloud"], tmp_path / "base", monkeypatch)
    overlaid = assemble(CASES_BY_NAME["skill_overlay"], tmp_path / "overlaid", monkeypatch)

    block = (
        "\n\n#### alice's configuration for this skill\n\n"
        "These instructions come from the user and take precedence over this "
        "skill's instructions above, wherever the two conflict.\n\n"
        "#### Notes rules\n\n"
        "- Never write a new file to the base folder.\n"
        "- Frontmatter carries `agents:` on anything the bot wrote."
    )
    assert block in overlaid, "the overlay block is not in the assembled prompt"
    assert overlaid.replace(block, "") == base

    # The `## ` the fixture wrote was demoted rather than dropped: at level 2 it
    # would have closed `### Notes` and left the rules reading as a sibling of
    # the whole skills reference.
    assert "\n## Notes rules" not in overlaid


def test_the_failure_rule_is_identical_in_both_rules_blocks(tmp_path, monkeypatch):
    """`## Important rules` is assembled twice, and the goldens cannot see the gap.

    `executor` builds one rules block for an admin and a separate one for a
    standard user, and the two lists renumber independently — the admin block
    carries a rule the other does not. A rule added to one copy and not the
    other therefore reaches half the deployment with nothing to say so: both
    goldens still match, each having been regenerated from whatever its own
    block holds.

    The failure-visibility rule (ISSUE-345) is the witness rather than the
    subject. It is asserted by identity, not by substring, because the way this
    drifts is a reworded copy rather than a missing one.
    """
    prompts = {
        "admin": assemble(CASES_BY_NAME["base_nextcloud"], tmp_path / "admin", monkeypatch),
        "standard user": assemble(CASES_BY_NAME["nonadmin"], tmp_path / "standard", monkeypatch),
    }

    rules = {}
    for who, prompt in prompts.items():
        found = [line for line in prompt.splitlines() if line.startswith("3c. ")]
        assert len(found) == 1, f"{who}: expected one rule 3c, found {len(found)}"
        rules[who] = found[0]

    assert rules["admin"] == rules["standard user"]
    assert "goes in the deliverable" in rules["admin"]


def test_every_golden_file_belongs_to_a_case():
    """A renamed case must not leave a stale file behind.

    A golden nothing reads is worse than no golden: it looks like coverage in a
    directory listing and asserts nothing.
    """
    if updating():
        pytest.skip(
            "mid-regeneration: under xdist this test has no ordering "
            "relationship with the writers, so it would race them"
        )
    if not GOLDEN_DIR.exists():
        pytest.skip("no goldens generated yet")
    on_disk = {p.stem for p in GOLDEN_DIR.glob("*.txt")}
    assert on_disk == set(CASES_BY_NAME), (
        f"orphaned goldens: {sorted(on_disk - set(CASES_BY_NAME))}; "
        f"missing goldens: {sorted(set(CASES_BY_NAME) - on_disk)}"
    )


def test_the_regeneration_switch_survives_the_env_scrub():
    """The documented regeneration command has to be able to arm this file.

    `tests/conftest.py` scrubs `ISTOTA_*` from the environment per test
    (ISSUE-301), and `ISTOTA_UPDATE_GOLDEN` matches neither keep-prefix
    (`ISTOTA_TEST`, `ISTOTA_UPGRADE_`), so it was deleted before `updating()`
    ever read it. The failure mode is the bad one: the documented command
    wrote nothing and reported all twelve goldens as *failing*, which reads as
    a prompt regression rather than as a broken switch — and the way out looks
    like editing goldens by hand.

    Asserting on the policy rather than on `os.environ`, because the scrub has
    already run by the time a test body executes: reading the environment here
    would pass for the wrong reason on a machine that never exported it.
    """
    from tests.support.env_isolation import scrubbed_env_names

    assert scrubbed_env_names({UPDATE_ENV: "1"}) == set(), (
        f"{UPDATE_ENV} is scrubbed before {UPDATE_CMD!r} can read it"
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

    monkeypatch.setattr(executor, "_bwrap_available", lambda: case.sandboxed)
    config = _build_config(case, tmp_path / "custom")
    config.custom_system_prompt = True
    (config.skills_dir.parent / "system-prompt.md").write_text("CUSTOM SYSTEM PROMPT\n")
    # The filename and directory above duplicate a derivation that lives in the
    # product. Without this, a move of either leaves the test asserting that
    # two unconfigured runs match — green, and covering nothing.
    resolved = custom_system_prompt_path(config)
    assert resolved is not None and resolved.exists(), (
        "the fixture did not actually configure a custom system prompt"
    )
    success, result, _a, _t = execute_task(_build_task(case), config, [], dry_run=True)
    assert success
    custom = normalize(
        result[len(DRY_RUN_PREFIX):], tmp_path=tmp_path / "custom"
    )

    assert custom == plain
    assert "CUSTOM SYSTEM PROMPT" not in custom
