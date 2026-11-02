"""The page's writer of CRON.md: one whole job definition, checked and spliced.

Everything here runs through the real parser and, where the order matters, the
real sync: what the page accepts is what the daemon loads.
"""

import pytest

from istota import cron_loader, db
from istota.config import BrainConfig, Config, ModelsConfig, NativeBrainConfig, UserConfig
from istota.cron_loader import (
    CronJob,
    JobFieldError,
    job_revision,
    job_text_digest,
    load_cron_document,
    save_job_definition,
    sync_cron_jobs_to_db,
    validate_job_input,
)
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress
from istota.skills._types import SkillMeta
from istota.storage import get_user_cron_path
from istota.workspace.text_files import MemoryMdLocked

USER = "alice"
ADMIN = "root"
SKILLS = {"browse": SkillMeta(name="browse", description="x", egress="external")}


@pytest.fixture
def mount(tmp_path):
    path = tmp_path / "mount"
    path.mkdir()
    return path


@pytest.fixture
def config(tmp_path, mount):
    cfg = Config(
        db_path=tmp_path / "test.db",
        workspace_path=mount,
        temp_dir=tmp_path / "temp",
        users={USER: UserConfig(), ADMIN: UserConfig()},
        admin_users={ADMIN},
        brain=BrainConfig(room_selectable=["native", "claude_code"]),
    )
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def usable_skills(monkeypatch):
    monkeypatch.setattr("istota.skills._loader.usable_external_skills", lambda *_: SKILLS)


def cron_path(mount, user=USER):
    return mount / get_user_cron_path(user, "istota").lstrip("/")


def write_file(mount, content, user=USER):
    path = cron_path(mount, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def job(name="j", prompt="say hello", **fields):
    return CronJob(name=name, cron="0 9 * * *", prompt=prompt, **fields)


def only_job(config, user=USER):
    jobs = load_cron_document(config, user).jobs
    assert len(jobs) == 1
    return jobs[0]


def sync(config, conn, user=USER):
    doc = load_cron_document(config, user)
    return sync_cron_jobs_to_db(
        conn, user, doc.jobs, is_admin=config.is_admin(user), unread_names=doc.unread_names,
    )


def payload(**fields):
    return {"name": "j", "cron": "0 9 * * *", "kind": "prompt", "prompt": "say hello", **fields}


TWO_JOBS = """# My schedule

Notes **above** the fence stay as typed.

```toml
[[jobs]]
name = "first"
cron = "0 8 * * *"
prompt = "morning"
silent_unless_action = true
effort = "high"

[[jobs]]
name = "second"
cron = "0 9 * * *"
prompt = "later"
```

And notes below it, with trailing spaces
"""


class TestAddAndReplace:
    def test_a_first_job_writes_a_whole_document(self, config, mount):
        assert save_job_definition(
            config, USER, job(), original_name=None, expected_revision=None,
        ).status == "ok"
        assert cron_path(mount).read_text().startswith("# Scheduled Jobs\n\n```toml\n")
        assert only_job(config).prompt == "say hello"

    def test_a_job_lands_in_a_seeded_template(self, config, mount):
        write_file(mount, "# Scheduled Jobs\n\n```toml\n# [[jobs]]\n# name = \"x\"\n```\n")
        outcome = save_job_definition(config, USER, job(), original_name=None, expected_revision=None)
        assert outcome.status == "ok"
        assert only_job(config).name == "j"

    def test_add_keeps_the_prose_and_the_other_jobs(self, config, mount):
        write_file(mount, TWO_JOBS)
        before = load_cron_document(config, USER)
        assert save_job_definition(
            config, USER, job("third"), original_name=None, expected_revision=None,
        ).status == "ok"
        text = cron_path(mount).read_text()
        start, end = before.block_span
        assert text[:start] == TWO_JOBS[:start]
        assert text.endswith(TWO_JOBS[end:])
        after = load_cron_document(config, USER)
        assert [j.name for j in after.jobs] == ["first", "second", "third"]
        assert after.jobs[0] == before.jobs[0]

    def test_replace_changes_one_job_where_it_stands(self, config, mount):
        write_file(mount, TWO_JOBS)
        doc = load_cron_document(config, USER)
        first, second = doc.jobs
        changed = validate_job_input(config, USER, {"prompt": "breakfast"}, first)
        assert save_job_definition(
            config, USER, changed, original_name="first", expected_revision=job_revision(first),
        ).status == "ok"
        after = load_cron_document(config, USER)
        assert [j.name for j in after.jobs] == ["first", "second"]
        assert after.jobs[0].prompt == "breakfast"
        assert after.jobs[0].silent_unless_action is True
        assert after.jobs[0].effort == "high"
        assert after.jobs[1] == second

    def test_a_rename_replaces_the_old_name(self, config, mount):
        write_file(mount, TWO_JOBS)
        first = load_cron_document(config, USER).jobs[0]
        renamed = validate_job_input(config, USER, {"name": "dawn"}, first)
        assert save_job_definition(
            config, USER, renamed, original_name="first", expected_revision=job_revision(first),
        ).status == "ok"
        assert [j.name for j in load_cron_document(config, USER).jobs] == ["dawn", "second"]


class TestRefusals:
    @pytest.mark.parametrize("content", [
        "no fence here\n",
        "```toml\n[[jobs\n```\n",
        "```toml\n[[jobs]]\nname = \"j\"\n",
        '```toml\n[[jobs]]\nname = "ok"\ncron = "* * * * *"\nprompt = "p"\n\n[[jobs]]\nname = "typo"\n```\n',
        '```toml\n[[jobs]]\nname = "gone"\ncron = "* * * * *"\nprompt_file = "/Users/alice/missing.txt"\n```\n',
        '```toml\n[[jobs]]\nname = "other"\ncron = "* * * * *"\nprompt_file = "/Users/bob/x.txt"\n```\n',
        "",
    ])
    def test_a_file_it_cannot_rebuild_is_left_alone(self, config, mount, content):
        path = write_file(mount, content)
        outcome = save_job_definition(config, USER, job(), original_name=None, expected_revision=None)
        assert outcome.status == "unreadable"
        assert path.read_text() == content

    def test_no_workspace_is_unreadable(self, tmp_path):
        cfg = Config(db_path=tmp_path / "t.db", temp_dir=tmp_path / "temp")
        assert not cfg.has_workspace
        assert save_job_definition(
            cfg, USER, job(), original_name=None, expected_revision=None,
        ).status == "unreadable"

    def test_a_replace_of_a_name_not_there(self, config, mount):
        path = write_file(mount, TWO_JOBS)
        outcome = save_job_definition(config, USER, job(), original_name="nope", expected_revision="x")
        assert outcome.status == "not_found"
        assert path.read_text() == TWO_JOBS

    def test_a_replace_with_no_file(self, config):
        outcome = save_job_definition(config, USER, job(), original_name="j", expected_revision="x")
        assert outcome.status == "not_found"

    @pytest.mark.parametrize("revision", ["stale", None])
    def test_a_job_edited_since_the_page_read_it(self, config, mount, revision):
        path = write_file(mount, TWO_JOBS)
        outcome = save_job_definition(
            config, USER, job("first"), original_name="first", expected_revision=revision,
        )
        assert outcome.status == "changed"
        assert path.read_text() == TWO_JOBS

    def test_a_new_name_already_taken(self, config, mount):
        path = write_file(mount, TWO_JOBS)
        outcome = save_job_definition(config, USER, job("second"), original_name=None, expected_revision=None)
        assert outcome.status == "name_taken"
        assert path.read_text() == TWO_JOBS

    def test_a_rename_onto_another_job(self, config, mount):
        path = write_file(mount, TWO_JOBS)
        first = load_cron_document(config, USER).jobs[0]
        outcome = save_job_definition(
            config, USER, job("second"), original_name="first", expected_revision=job_revision(first),
        )
        assert outcome.status == "name_taken"
        assert path.read_text() == TWO_JOBS

    def test_too_large(self, config, mount, monkeypatch):
        path = write_file(mount, TWO_JOBS)
        monkeypatch.setattr(cron_loader, "USER_CONFIG_READ_CAP_BYTES", len(TWO_JOBS))
        outcome = save_job_definition(config, USER, job("third"), original_name=None, expected_revision=None)
        assert outcome.status == "too_large"
        assert path.read_text() == TWO_JOBS

    def test_the_parser_judges_the_candidate(self, config, mount):
        path = write_file(mount, TWO_JOBS)
        outcome = save_job_definition(
            config, USER, CronJob(name="empty", cron="* * * * *"),
            original_name=None, expected_revision=None,
        )
        assert outcome.status == "invalid"
        assert any("needs a prompt" in sentence for sentence in outcome.errors)
        assert path.read_text() == TWO_JOBS

    def test_a_held_lock_is_told_apart(self, config, mount, monkeypatch):
        path = write_file(mount, TWO_JOBS)

        def held(*_args, **_kwargs):
            raise MemoryMdLocked("held")

        monkeypatch.setattr(cron_loader.text_files, "file_lock", held)
        outcome = save_job_definition(config, USER, job("third"), original_name=None, expected_revision=None)
        assert outcome.status == "locked"
        assert path.read_text() == TWO_JOBS

    def test_a_failed_write(self, config, mount, monkeypatch):
        write_file(mount, TWO_JOBS)
        monkeypatch.setattr("istota.storage.write_regular_file", lambda *_: False)
        outcome = save_job_definition(config, USER, job("third"), original_name=None, expected_revision=None)
        assert outcome.status == "failed"


class TestRevision:
    def test_it_follows_the_definition(self):
        assert job_revision(job()) == job_revision(job())
        assert job_revision(job()) != job_revision(job(prompt="other"))
        assert job_revision(job()) != job_revision(job(enabled=False))

    def test_it_sees_a_prompt_file_edited_behind_the_page(self, config, mount):
        prompt = mount / "Users" / USER / "prompt.txt"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("first text\n")
        write_file(mount, f'```toml\n[[jobs]]\nname = "j"\ncron = "* * * * *"\nprompt_file = "/Users/{USER}/prompt.txt"\n```\n')
        before = job_revision(only_job(config))
        prompt.write_text("second text\n")
        assert job_revision(only_job(config)) != before


class TestPromptFiles:
    def _prompt_file_job(self, config, mount):
        prompt = mount / "Users" / USER / "prompt.txt"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("line one\nline two\n")
        write_file(mount, f'```toml\n[[jobs]]\nname = "j"\ncron = "* * * * *"\nprompt_file = "/Users/{USER}/prompt.txt"\n```\n')
        return prompt, only_job(config)

    def test_unchanged_text_keeps_the_reference(self, config, mount):
        prompt, stored = self._prompt_file_job(config, mount)
        edited = validate_job_input(config, USER, {"prompt": stored.prompt, "cron": "0 1 * * *"}, stored)
        assert save_job_definition(
            config, USER, edited, original_name="j", expected_revision=job_revision(stored),
        ).status == "ok"
        after = only_job(config)
        assert after.prompt_file == stored.prompt_file
        assert after.cron == "0 1 * * *"

    def test_changed_text_clears_it_and_the_old_file_stays(self, config, mount):
        prompt, stored = self._prompt_file_job(config, mount)
        edited = validate_job_input(config, USER, {"prompt": "one line now"}, stored)
        assert edited.prompt_file == ""
        assert save_job_definition(
            config, USER, edited, original_name="j", expected_revision=job_revision(stored),
        ).status == "ok"
        after = only_job(config)
        assert after.prompt == "one line now"
        assert after.prompt_file == ""
        assert prompt.read_text() == "line one\nline two\n"

    @pytest.mark.parametrize("text", ["first\nsecond", "first\r\nsecond", "tab\there\n\nend"])
    def test_a_multi_line_prompt_goes_out_to_a_file_with_the_digest_the_sync_computes(
        self, config, mount, text,
    ):
        candidate = validate_job_input(config, USER, payload(prompt=f"  {text}\n"), None)
        digest = job_text_digest(candidate, is_admin=False)
        assert save_job_definition(
            config, USER, candidate, original_name=None, expected_revision=None,
        ).status == "ok"
        stored = only_job(config)
        assert stored.prompt_file
        assert stored.prompt == text.replace("\r\n", "\n")
        with db.get_db(config.db_path) as conn:
            sync(config, conn)
            row = conn.execute("SELECT * FROM scheduled_jobs WHERE name = 'j'").fetchone()
        assert egress.job_digest(row) == digest


class TestEveryFieldRoundTrips:
    def test_through_the_writer_the_parser_and_the_sync(self, config, usable_skills):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, ADMIN, "mine")
        sent = {
            "name": "  everything  ",
            "cron": " 15 7 * * 1 ",
            "kind": "prompt",
            "prompt": "  a prompt with \"quotes\", a \\ and `ticks`  ",
            "target": f"web:{room.token}",
            "enabled": False,
            "once": True,
            "silent_unless_action": True,
            "skip_log_channel": True,
            "reach": "public",
            "model": "sonnet",
            "effort": "low",
            "brain": "native",
            "publish_shared_kv": "news/daily",
            "publish_shared_kv_trusted": True,
            "egress": ["News.Example.COM", "skill:browse"],
        }
        candidate = validate_job_input(config, ADMIN, sent, None)
        assert save_job_definition(
            config, ADMIN, candidate, original_name=None, expected_revision=None,
        ).status == "ok"

        stored = only_job(config, ADMIN)
        assert stored == candidate
        assert stored.name == "everything"
        assert stored.cron == "15 7 * * 1"
        assert stored.prompt == 'a prompt with "quotes", a \\ and `ticks`'
        assert stored.room == room.token
        assert stored.egress == ("news.example.com", "skill:browse")

        with db.get_db(config.db_path) as conn:
            sync(config, conn, ADMIN)
            row = dict(conn.execute("SELECT * FROM scheduled_jobs WHERE name = 'everything'").fetchone())
        assert row["cron_expression"] == "15 7 * * 1"
        assert row["conversation_token"] == room.token
        assert row["output_target"] == f"web:{room.token}"
        assert (row["enabled"], row["once"], row["silent_unless_action"], row["skip_log_channel"]) == (0, 1, 1, 1)
        assert row["reach"] == "public"
        assert row["effort"] == "low"
        assert row["brain"] == "native"
        assert row["publish_shared_kv"] == "news/daily"
        assert row["publish_shared_kv_trusted"] == 1
        assert row["egress_request"] == '["news.example.com", "skill:browse"]'
        assert egress.job_digest(row) == job_text_digest(candidate, is_admin=True)

    def test_a_command_job(self, config):
        candidate = validate_job_input(
            config, ADMIN, payload(kind="command", command="  echo hi | tee /dev/null  "), None,
        )
        assert candidate.prompt == ""
        assert save_job_definition(
            config, ADMIN, candidate, original_name=None, expected_revision=None,
        ).status == "ok"
        assert only_job(config, ADMIN).command == "echo hi | tee /dev/null"
        with db.get_db(config.db_path) as conn:
            sync(config, conn, ADMIN)
            row = conn.execute("SELECT * FROM scheduled_jobs WHERE user_id = ?", (ADMIN,)).fetchone()
        assert row["command"] == "echo hi | tee /dev/null"
        assert egress.job_digest(row) == job_text_digest(candidate, is_admin=True)

    def test_what_the_form_does_not_send_is_kept(self, config, mount):
        stored = job(effort="high", silent_unless_action=True, reach="public", once=True)
        assert validate_job_input(config, USER, {"cron": "1 1 * * *"}, stored) == CronJob(
            name="j", cron="1 1 * * *", prompt="say hello", effort="high",
            silent_unless_action=True, reach="public", once=True,
        )

    def test_a_switch_of_kind_clears_the_other_text(self, config):
        stored = job(prompt_file="/Users/root/x.txt")
        switched = validate_job_input(config, ADMIN, {"kind": "command", "command": "ls"}, stored)
        assert (switched.prompt, switched.prompt_file, switched.command) == ("", "", "ls")
        back = validate_job_input(
            config, ADMIN, {"kind": "prompt", "prompt": "p"},
            CronJob(name="j", cron="* * * * *", command="ls"),
        )
        assert (back.prompt, back.command) == ("p", "")


class TestFieldRules:
    @pytest.mark.parametrize("field,value", [
        ("name", ""),
        ("name", "   "),
        ("name", "x" * 101),
        ("name", "new\x07line"),
        ("name", "_module.money.sync"),
        ("name", 5),
        ("name", "\ud800"),
        ("cron", ""),
        ("cron", "not a cron"),
        ("cron", 5),
        ("kind", "shell"),
        ("kind", None),
        ("prompt", ""),
        ("prompt", "   "),
        ("prompt", "x" * (48 * 1024 + 1)),
        ("prompt", "\ud800"),
        ("enabled", "true"),
        ("once", 1),
        ("silent_unless_action", "false"),
        ("skip_log_channel", None),
        ("reach", "open"),
        ("reach", "private"),
        ("reach", 1),
        ("model", "two words"),
        ("model", 5),
        ("effort", "ultra"),
        ("egress", "example.com"),
        ("egress", ["1.2.3.4"]),
        ("egress", ["open:" + "0" * 64]),
        ("egress", [5]),
        ("publish_shared_kv_trusted", "yes"),
    ])
    def test_each_refusal_names_its_field(self, config, field, value):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(**{field: value}), None)
        assert exc.value.field == field

    def test_the_prompt_limit_counts_bytes(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(prompt="é" * (24 * 1024 + 1)), None)
        assert exc.value.field == "prompt"

    def test_kind_is_required_on_create(self, config):
        body = payload()
        del body["kind"]
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, body, None)
        assert exc.value.field == "kind"

    @pytest.mark.parametrize("body", [None, [], "x"])
    def test_a_body_that_is_not_an_object(self, config, body):
        with pytest.raises(JobFieldError):
            validate_job_input(config, USER, body, None)

    def test_a_value_is_never_echoed(self, config):
        canary = "CANARY-value-CANARY"
        for field, value in [
            ("cron", canary),
            ("kind", canary),
            ("target", f"web:{canary}"),
            ("target", canary),
            ("effort", canary),
            ("model", canary),
            ("egress", [canary]),
            ("egress", [f"skill:{canary.lower()}"]),
            ("reach", canary),
        ]:
            with pytest.raises(JobFieldError) as exc:
                validate_job_input(config, USER, payload(**{field: value}), None)
            assert canary.lower() not in str(exc.value).lower(), field

    def test_a_non_admin_command_is_a_field_error(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(kind="command", command="rm -rf x"), None)
        assert exc.value.field == "command"

    def test_a_pure_skill_command_is_fine_for_anyone(self, config):
        candidate = validate_job_input(
            config, USER, payload(kind="command", command="istota-skill bookmarks sync"), None,
        )
        assert candidate.command == "istota-skill bookmarks sync"

    def test_an_empty_command(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, ADMIN, payload(kind="command", command="  "), None)
        assert exc.value.field == "command"

    @pytest.mark.parametrize("reach", ["limited", "public", "full"])
    def test_reach_is_set_on_create(self, config, reach):
        assert validate_job_input(config, USER, payload(reach=reach), None).reach == reach

    def test_reach_is_untouched_on_edit(self, config):
        stored = job(reach="public")
        assert validate_job_input(config, USER, {"reach": "full"}, stored).reach == "public"

    def test_an_odd_name_in_the_file_does_not_stop_an_edit(self, config):
        stored = job(name="x" * 120)
        assert validate_job_input(config, USER, {"name": "x" * 120, "cron": "1 1 * * *"}, stored).cron == "1 1 * * *"

    def test_an_odd_effort_in_the_file_does_not_stop_an_edit(self, config):
        stored = job(effort="ultra")
        assert validate_job_input(config, USER, {"effort": "ultra"}, stored).effort == "ultra"

    def test_stripped_values_match_the_parser(self, config, mount):
        candidate = validate_job_input(config, USER, payload(prompt=" p ", name=" n "), None)
        save_job_definition(config, USER, candidate, original_name=None, expected_revision=None)
        assert only_job(config) == candidate


class TestModel:
    def test_a_non_admin_model_outside_the_set_is_a_field_error(self, config):
        config.models = ModelsConfig(enabled=[])
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(model="claude-made-up-1"), None)
        assert exc.value.field == "model"

    def test_an_admin_model_that_will_not_resolve_stays_a_warning(self, config):
        candidate = validate_job_input(config, ADMIN, payload(model="claude-made-up-1"), None)
        assert candidate.model == "claude-made-up-1"

    def test_a_refused_model_in_the_file_saves_unchanged(self, config, mount):
        write_file(mount, '```toml\n[[jobs]]\nname = "j"\ncron = "* * * * *"\nprompt = "p"\nmodel = "claude-made-up-1"\n```\n')
        doc = load_cron_document(config, USER)
        assert doc.jobs[0].model_refused
        edited = validate_job_input(
            config, USER, {"model": "claude-made-up-1", "cron": "1 1 * * *"}, doc.jobs[0],
        )
        assert save_job_definition(
            config, USER, edited, original_name="j", expected_revision=job_revision(doc.jobs[0]),
        ).status == "ok"
        assert only_job(config).model == "claude-made-up-1"

    def test_changing_a_refused_model_is_judged(self, config, mount):
        stored = job(model="claude-made-up-1", model_refused=True)
        with pytest.raises(JobFieldError):
            validate_job_input(config, USER, {"model": "claude-made-up-2"}, stored)

    def test_a_refused_candidate_is_still_refused_by_the_writer(self, config):
        outcome = save_job_definition(
            config, USER, job(model="claude-made-up-1"), original_name=None, expected_revision=None,
        )
        assert outcome.status == "invalid"


class TestRestrictedFields:
    CASES = [
        ("brain", "native"),
        ("provider", "openrouter"),
        ("publish_shared_kv", "news/daily"),
    ]

    @pytest.mark.parametrize("field,value", CASES)
    def test_a_non_admin_cannot_set_one(self, config, field, value):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(**{field: value}), None)
        assert exc.value.field == field

    @pytest.mark.parametrize("field,value", CASES)
    def test_a_non_admin_cannot_change_one(self, config, field, value):
        stored = job(**{field: "was/before"})
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, {field: value}, stored)
        assert exc.value.field == field

    @pytest.mark.parametrize("field,value", CASES)
    def test_an_unchanged_value_saves_and_a_clear_is_allowed(self, config, field, value):
        stored = job(**{field: value})
        assert getattr(validate_job_input(config, USER, {field: value}, stored), field) == value
        assert getattr(validate_job_input(config, USER, {field: ""}, stored), field) == ""
        assert getattr(validate_job_input(config, USER, {field: None}, stored), field) == ""

    def test_a_non_admin_cannot_set_trust(self, config):
        stored = job(publish_shared_kv="news/daily")
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, {"publish_shared_kv_trusted": True}, stored)
        assert exc.value.field == "publish_shared_kv_trusted"

    def test_trust_saved_unchanged_and_removed_with_its_target(self, config):
        stored = job(publish_shared_kv="news/daily", publish_shared_kv_trusted=True)
        assert validate_job_input(config, USER, {"cron": "1 1 * * *"}, stored).publish_shared_kv_trusted
        cleared = validate_job_input(config, USER, {"publish_shared_kv": ""}, stored)
        assert (cleared.publish_shared_kv, cleared.publish_shared_kv_trusted) == ("", False)
        assert not validate_job_input(config, USER, {"publish_shared_kv_trusted": False}, stored).publish_shared_kv_trusted

    def test_trust_needs_a_target(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, ADMIN, payload(publish_shared_kv_trusted=True), None)
        assert exc.value.field == "publish_shared_kv_trusted"

    def test_a_blank_admins_file_grants_no_publishing(self, tmp_path, mount):
        cfg = Config(db_path=tmp_path / "s.db", workspace_path=mount, temp_dir=tmp_path / "temp")
        assert cfg.is_admin(USER)
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(cfg, USER, payload(publish_shared_kv="news/daily"), None)
        assert exc.value.field == "publish_shared_kv"

    @pytest.mark.parametrize("target", ["_secrets/key", "_reserved/x", "ns/", "/key", "  "])
    def test_a_writer_needs_a_key_and_an_open_namespace(self, config, target):
        if not target.strip():
            assert validate_job_input(config, ADMIN, payload(publish_shared_kv=target), None).publish_shared_kv == ""
            return
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, ADMIN, payload(publish_shared_kv=target), None)
        assert exc.value.field == "publish_shared_kv"

    def test_a_bare_key_is_a_shared_block(self, config):
        assert validate_job_input(config, ADMIN, payload(publish_shared_kv="digest"), None).publish_shared_kv == "digest"

    def test_an_admin_brain_off_the_allowlist(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, ADMIN, payload(brain="tmux"), None)
        assert exc.value.field == "brain"

    def test_a_pin_taken_off_the_allowlist_saves_unchanged(self, config):
        config.brain.room_selectable = []
        stored = job(brain="native")
        assert validate_job_input(config, ADMIN, {"brain": "native"}, stored).brain == "native"

    def _native_config(self, config):
        config.brain = BrainConfig(
            kind="native",
            native=NativeBrainConfig(provider="openrouter", providers=["openrouter", "anthropic", "unbuilt"]),
        )

    def test_an_admin_provider_for_a_native_job(self, config):
        self._native_config(config)
        assert validate_job_input(config, ADMIN, payload(provider="anthropic"), None).provider == "anthropic"

    @pytest.mark.parametrize("provider", ["nowhere", "unbuilt"])
    def test_a_provider_native_may_not_send_to(self, config, provider):
        self._native_config(config)
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, ADMIN, payload(provider=provider), None)
        assert exc.value.field == "provider"

    def test_a_provider_on_a_job_that_is_not_native(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, ADMIN, payload(provider="openrouter"), None)
        assert exc.value.field == "provider"

    def test_a_file_the_sync_drops_it_from_still_saves(self, config, mount):
        write_file(mount, '```toml\n[[jobs]]\nname = "j"\ncron = "* * * * *"\nprompt = "p"\nbrain = "native"\npublish_shared_kv = "news/daily"\n```\n')
        stored = only_job(config)
        edited = validate_job_input(config, USER, {"cron": "1 1 * * *"}, stored)
        assert save_job_definition(
            config, USER, edited, original_name="j", expected_revision=job_revision(stored),
        ).status == "ok"
        after = only_job(config)
        assert (after.brain, after.publish_shared_kv, after.cron) == ("native", "news/daily", "1 1 * * *")
        with db.get_db(config.db_path) as conn:
            sync(config, conn)
            assert conn.execute("SELECT brain FROM scheduled_jobs").fetchone()[0] is None


class TestDelivery:
    def _shared_room(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, USER, "ours")
            db.add_web_room_member(conn, room.token, "bob")
        return room.token

    def _own_room(self, config):
        with db.get_db(config.db_path) as conn:
            return db.create_web_chat_room(conn, USER, "mine").token

    @pytest.mark.parametrize("spelling", ["web", "room"])
    def test_a_room_target_sets_the_room(self, config, spelling):
        token = self._own_room(config)
        candidate = validate_job_input(config, USER, payload(target=f"{spelling}:{token}", room="ignored"), None)
        assert candidate.room == token

    def test_a_room_target_saves_without_a_delivery_warning(self, config, caplog):
        token = self._own_room(config)
        candidate = validate_job_input(config, USER, payload(target=f"room:{token}"), None)
        assert save_job_definition(
            config, USER, candidate, original_name=None, expected_revision=None,
        ).status == "ok"
        assert only_job(config).room == token

    @pytest.mark.parametrize("spelling", ["web", "room"])
    def test_somebody_elses_room_is_refused(self, config, spelling):
        with db.get_db(config.db_path) as conn:
            token = db.create_web_chat_room(conn, "bob", "bobs").token
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(target=f"{spelling}:{token}"), None)
        assert exc.value.field == "target"

    @pytest.mark.parametrize("spelling", ["web", "room"])
    def test_a_shared_room_is_refused(self, config, spelling):
        token = self._shared_room(config)
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(target=f"{spelling}:{token}"), None)
        assert exc.value.field == "target"

    def test_a_pin_on_a_room_since_shared_is_not_judged_again(self, config):
        token = self._shared_room(config)
        stored = job(target=f"web:{token}", room=token)
        edited = validate_job_input(config, USER, {"target": f"web:{token}", "cron": "1 1 * * *"}, stored)
        assert (edited.target, edited.room) == (f"web:{token}", token)

    def test_an_unknown_surface_is_refused(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(target="matrix"), None)
        assert exc.value.field == "target"

    def test_a_room_target_names_its_room(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(target="room"), None)
        assert exc.value.field == "target"

    def test_a_talk_target_takes_the_token_sent(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "conv-mine", USER, origin="talk", name="conv-mine")
            db.add_room_binding(conn, "conv-mine", "talk", "conv-mine")
            db.add_room_member(conn, "conv-mine", USER)
        candidate = validate_job_input(config, USER, payload(target="talk", room="conv-mine"), None)
        assert candidate.room == "conv-mine"

    def test_a_talk_token_not_yours_is_refused_on_the_room_field(self, config):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(target="talk", room="conv-other"), None)
        assert exc.value.field == "room"

    def test_a_target_with_no_room_clears_a_room_sent(self, config):
        assert validate_job_input(config, USER, payload(target="email", room="x"), None).room == ""

    def test_a_phone_pairing_in_the_file_is_kept(self, config):
        stored = job(target="sms", room="rm_abc")
        edited = validate_job_input(config, USER, {"cron": "1 1 * * *"}, stored)
        assert (edited.target, edited.room) == ("sms", "rm_abc")


class TestEgressField:
    def test_entries_go_to_normal_form(self, config, usable_skills):
        candidate = validate_job_input(config, USER, payload(egress=["A.Example.com", "skill:browse", "a.example.com"]), None)
        assert candidate.egress == ("a.example.com", "skill:browse")

    def test_a_skill_it_could_not_approve_is_a_field_error(self, config, usable_skills):
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(egress=["skill:nope"]), None)
        assert exc.value.field == "egress"

    def test_a_list_past_the_cap(self, config):
        hosts = [f"h{n}.example.com" for n in range(egress.MAX_DECLARED + 1)]
        with pytest.raises(JobFieldError) as exc:
            validate_job_input(config, USER, payload(egress=hosts), None)
        assert exc.value.field == "egress"

    def test_a_kept_entry_stays_and_asks_for_nothing(self, config, mount, usable_skills):
        write_file(mount, '```toml\n[[jobs]]\nname = "j"\ncron = "* * * * *"\nprompt = "p"\negress = ["a.example.com", "skill:nope"]\n```\n')
        stored = only_job(config)
        assert stored.egress_kept == ("skill:nope",)
        for sent in (["a.example.com", "skill:nope"], ["b.example.com"]):
            edited = validate_job_input(config, USER, {"egress": sent}, stored)
            assert edited.egress_kept == ("skill:nope",)
            assert "skill:nope" not in edited.egress
        assert save_job_definition(
            config, USER, edited, original_name="j", expected_revision=job_revision(stored),
        ).status == "ok"
        after = only_job(config)
        assert after.egress == ("b.example.com",)
        assert after.egress_kept == ("skill:nope",)


class TestKeepingAnApprovalAcrossAnEdit:
    """`keep_access`, in both orders against the real sync."""

    SUBJECT = "cron:j"

    @pytest.fixture
    def approved(self, config, mount):
        """A job with an approved `open`, as the table and the file hold it."""
        candidate = validate_job_input(config, USER, payload(reach="full"), None)
        save_job_definition(config, USER, candidate, original_name=None, expected_revision=None)
        with db.get_db(config.db_path) as conn:
            sync(config, conn)
            row = conn.execute("SELECT * FROM scheduled_jobs WHERE name = 'j'").fetchone()
            egress.approve_job(conn, USER, self.SUBJECT, "open", egress.job_digest(row), decided_via="web")
            conn.commit()
            assert egress.job_state(egress.grants_for(conn, USER, self.SUBJECT), self._row(conn)) == "open"
        return only_job(config)

    def _row(self, conn):
        return egress.job_row_by_subject(conn, USER, self.SUBJECT)

    def _edit(self, config, stored):
        edited = validate_job_input(config, USER, {"prompt": "the new text"}, stored)
        outcome = save_job_definition(
            config, USER, edited, original_name="j", expected_revision=job_revision(stored),
        )
        assert outcome.status == "ok"
        return job_text_digest(edited, is_admin=False)

    def _grant(self, conn, digest):
        egress.approve_saved_text(conn, USER, self.SUBJECT, "open", digest, decided_via="web")
        egress_refused.resolve_if_settled(conn, USER, self.SUBJECT, by="web")
        conn.commit()

    def _assert_kept(self, conn, digest):
        row = self._row(conn)
        assert egress.job_digest(row) == digest
        grants = egress.grants_for(conn, USER, self.SUBJECT)
        assert grants == {f"open:{digest}"}
        assert egress.job_state(grants, row) == "open"
        assert egress.list_refused(conn, USER, self.SUBJECT) == []

    def test_grant_then_sync(self, config, approved):
        digest = self._edit(config, approved)
        with db.get_db(config.db_path) as conn:
            self._grant(conn, digest)
            assert sync(config, conn) == []
            self._assert_kept(conn, digest)

    def test_sync_then_grant(self, config, approved):
        digest = self._edit(config, approved)
        with db.get_db(config.db_path) as conn:
            sync(config, conn)
            assert [r["entry"] for r in egress.list_refused(conn, USER, self.SUBJECT)] == ["open"]
            self._grant(conn, digest)
            self._assert_kept(conn, digest)

    def test_the_grant_names_text_no_row_holds_until_the_sync(self, config, approved):
        digest = self._edit(config, approved)
        with db.get_db(config.db_path) as conn:
            old_digest = egress.job_digest(self._row(conn))
            assert old_digest != digest
            self._grant(conn, digest)
            # The old grant still answers for the old row; the new one for nothing yet.
            assert egress.job_state(egress.grants_for(conn, USER, self.SUBJECT), self._row(conn)) == "open"
            conn.execute("DELETE FROM egress_grants WHERE entry = ?", (f"open:{old_digest}",))
            assert egress.job_state(egress.grants_for(conn, USER, self.SUBJECT), self._row(conn)) == "limited"

    def test_without_it_the_job_runs_limited_and_asks(self, config, approved):
        self._edit(config, approved)
        with db.get_db(config.db_path) as conn:
            assert sync(config, conn) == [self.SUBJECT]
            grants = egress.grants_for(conn, USER, self.SUBJECT)
            assert grants == frozenset()
            assert egress.job_state(grants, self._row(conn)) == "limited"
            assert [r["entry"] for r in egress.list_refused(conn, USER, self.SUBJECT)] == ["open"]

    def test_a_grant_on_other_text_does_not_stop_the_asking(self, config, approved):
        self._edit(config, approved)
        with db.get_db(config.db_path) as conn:
            self._grant(conn, "a" * 64)
            sync(config, conn)
            assert [r["entry"] for r in egress.list_refused(conn, USER, self.SUBJECT)] == ["open"]

    def test_a_schedule_edit_leaves_the_digest_alone(self, config, approved):
        before = job_text_digest(approved, is_admin=False)
        edited = validate_job_input(config, USER, {"cron": "1 1 * * *"}, approved)
        assert job_text_digest(edited, is_admin=False) == before

    def test_the_digest_covers_the_publish_target(self, config):
        plain = job()
        assert job_text_digest(job(publish_shared_kv="a/b"), is_admin=True) != job_text_digest(plain, is_admin=True)


class TestApproveSavedText:
    SUBJECT = "cron:j"
    DIGEST = "b" * 64

    @pytest.fixture
    def conn(self, config):
        with db.get_db(config.db_path) as c:
            sync_cron_jobs_to_db(c, USER, [job()], is_admin=False)
            yield c

    @pytest.mark.parametrize("kind", ["open", "public"])
    def test_it_stores_the_grant_and_clears_the_request(self, conn, kind):
        egress.note_refused(conn, USER, self.SUBJECT, kind)
        egress.approve_saved_text(conn, USER, self.SUBJECT, kind, self.DIGEST, decided_via="web")
        assert egress.grants_for(conn, USER, self.SUBJECT) == {f"{kind}:{self.DIGEST}"}
        assert egress.list_refused(conn, USER, self.SUBJECT) == []

    def test_only_a_job_request_can_be_approved(self, conn):
        with pytest.raises(egress.EntryRefused):
            egress.approve_saved_text(conn, USER, self.SUBJECT, "skill", self.DIGEST, decided_via="web")

    def test_the_digest_must_be_one(self, conn):
        with pytest.raises(egress.EntryRefused):
            egress.approve_saved_text(conn, USER, self.SUBJECT, "open", "not-a-digest", decided_via="web")

    def test_a_job_that_does_not_exist(self, conn):
        with pytest.raises(egress.EntryRefused):
            egress.approve_saved_text(conn, USER, "cron:gone", "open", self.DIGEST, decided_via="web")

    def test_somebody_elses_job(self, conn):
        with pytest.raises(egress.EntryRefused):
            egress.approve_saved_text(conn, "bob", self.SUBJECT, "open", self.DIGEST, decided_via="web")

    def test_only_a_person_decides(self, conn):
        with pytest.raises(egress.EntryRefused):
            egress.approve_saved_text(conn, USER, self.SUBJECT, "open", self.DIGEST, decided_via="task")
