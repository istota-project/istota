"""Read-only snapshots and CLI round trips over real temporary stores."""

import json
import stat
import tomllib

import pytest

from istota import cli, db, user_briefings, user_profiles
from istota.briefings import db as bdb
from istota.config import load_config
from istota.lib import toml_write
from istota.storage import resolve_user_config_dir


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
    (tmp_path / "admins").write_text("alice\n")
    database = tmp_path / "framework.db"
    db.init_db(database)
    path = tmp_path / "config.toml"
    path.write_text(toml_write.dumps({
        "db_path": str(database), "workspace_path": str(tmp_path / "workspace"),
        "temp_dir": str(tmp_path / "temp"), "default_briefings": [], "briefing_shared_blocks": [],
    }))
    user_profiles.provision_user(database, "alice", {}, mode="managed")
    config = load_config(path, apply_db_overlays=False)
    return path, config


def invoke(monkeypatch, capsys, config, *arguments):
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config), *map(str, arguments)])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    output = capsys.readouterr()
    return code, output.out, output.err


def content(install, *, options=None, source=None):
    _, config = install
    user_briefings.ensure_briefing(config.db_path, user_id="alice", name="morning",
                                  cron="0 7 * * *", output="email")
    path = config.module_db_path("alice", "briefings")
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        block = bdb.add_block(conn, briefing_name="morning", title="News", options=options or {})
        bdb.add_source(conn, block_id=block, kind="browse", config=source or {})
        conn.commit()
    return path


@pytest.mark.parametrize("where", ["options", "source", "shared"])
@pytest.mark.parametrize("to_file", [False, True])
def test_embedded_credentials_refuse_without_output_or_writes(install, tmp_path, monkeypatch, capsys, where, to_file):
    path, config = install
    embedded = {"requests": [{"headers": {"Authorization": "private-placeholder"}}]}
    if where == "shared":
        with db.get_db(config.db_path) as conn:
            db.upsert_shared_block_config(conn, name="news", cron="0 7 * * *",
                                          sources=[{"kind": "browse", "config": embedded}])
        module = None
    else:
        module = content(install, **{where: embedded})
    output = tmp_path / "snapshot.toml"
    output.write_text("existing output")
    before = config.db_path.read_bytes(), module.read_bytes() if module else None
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", *(["-o", output] if to_file else []))
    assert code == 1
    assert stdout == "" and "private-placeholder" not in stderr
    assert "embedded_briefing_credential" in stderr
    assert "Authorization" in stderr and ("options" if where == "options" else "config") in stderr
    assert output.read_text() == "existing output"
    assert before == (config.db_path.read_bytes(), module.read_bytes() if module else None)


def test_same_install_cli_round_trip_is_deterministic_and_read_only(install, tmp_path, monkeypatch, capsys):
    from istota import cron_loader
    from istota.credentials import store

    path, config = install
    raw = tomllib.loads(path.read_text())
    raw["brain"] = {"native": {"extra_headers": {"x.tenant": "inline-placeholder", "empty": ""}}}
    path.write_text(toml_write.dumps(raw))
    marker = tmp_path / "managed.json"
    marker.write_text('{"config": {"managed": true, "source": "ansible"}}')
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-snapshot-key-" * 4)
    store.set_secret(config.db_path, "alice", "karakeep", "api_key", "stored-placeholder")
    store.set_secret(config.db_path, "alice", "vault", "example", "excluded-placeholder")
    monkeypatch.delenv("ISTOTA_SECRET_KEY")
    module = content(install, options={"api_key": ""})
    with bdb.connect(module) as conn:
        block = bdb.add_block(conn, briefing_name="morning", title="Shared")
        bdb.add_source(conn, block_id=block, kind="shared_block", config={"name": "news"})
        conn.commit()
    with db.get_db(config.db_path) as conn:
        db.upsert_shared_block_config(conn, name="news", cron="0 7 * * *", sources=[{"kind": "browse"}])
    cron = resolve_user_config_dir(config, "alice") / "CRON.md"
    cron.parent.mkdir(parents=True)
    cron.write_text('```toml\n[[jobs]]\nname="daily"\ncron="0 9 * * *"\nprompt="Review"\n```\n')
    heartbeat = cron.with_name("HEARTBEAT.md")
    heartbeat.write_text('```toml\n[[checks]]\nname="inbox"\ntype="file-watch"\npath="/Users/alice/inbox"\n```\n')
    with db.get_db(config.db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(conn, "alice", cron_loader.load_cron_jobs(config, "alice"))
        conn.execute("UPDATE scheduled_jobs SET auto_disabled_at='2026-01-01', consecutive_failures=5")
        locks = user_profiles.managed_fields(conn, "alice")
    watched = [path, config.db_path, module, marker, cron, heartbeat]
    before = [item.read_bytes() for item in watched]
    first = invoke(monkeypatch, capsys, path, "snapshot")
    second = invoke(monkeypatch, capsys, path, "snapshot")
    assert first == second and first[0] == 0
    assert "excluded" in first[2]
    document = tomllib.loads(first[1])
    assert document["meta"] == {"version": 1, "generated_by": "snapshot", "source": "snapshot"}
    assert document["config"]["brain"]["native"]["extra_headers"] == {"x.tenant": "<unchanged>", "empty": ""}
    alice = document["users"]["alice"]
    assert alice["profile_mode"] == "update" and "enrollment" not in alice
    assert alice["scheduled_jobs"][0]["enabled"] is True
    assert alice["heartbeat"]["checks"][0]["reset"] == ["cooldown_minutes", "interval_minutes", "max_age_hours"]
    assert alice["briefings"][0]["blocks"][0]["options"] == {"api_key": ""}
    assert alice["secrets"] == [{"service": "karakeep", "keys": ["api_key"]}]
    assert all(value not in first[1] + first[2] for value in ("inline-placeholder", "stored-placeholder", "excluded-placeholder"))
    plan = tmp_path / "settings.toml"
    plan.write_text(first[1])
    for flags in (["--dry-run"], []):
        code, stdout, stderr = invoke(monkeypatch, capsys, path, "apply", "-f", plan, "--format", "json", *flags)
        assert code == 0, stdout + stderr
        assert json.loads(stdout)["operations"] == []
        assert [item.read_bytes() for item in watched] == before
    with db.get_db(config.db_path) as conn:
        assert user_profiles.managed_fields(conn, "alice") == locks
        assert conn.execute("SELECT last_accessed_at FROM secrets").fetchall()[0][0] is None
        assert conn.execute("SELECT auto_disabled_at FROM scheduled_jobs").fetchone()[0] == "2026-01-01"


def test_destination_cli_round_trip_resets_defaults_and_preserves_runtime(install, tmp_path, monkeypatch, capsys):
    from istota.settings_plan.paths import patch_document

    source, config = install
    content(install)
    code, output, error = invoke(monkeypatch, capsys, source, "snapshot", "--format", "json")
    assert code == 0, error
    document = json.loads(output)
    destination = tmp_path / "destination"
    destination.mkdir()
    target = destination / "config.toml"
    database = destination / "framework.db"
    db.init_db(database)
    rebased, _ = patch_document(document, [str(tmp_path) + "=" + str(destination)])
    target.write_text(toml_write.dumps(rebased["config"]))
    user_profiles.provision_user(database, "alice", {"display_name": "Other", "disabled_modules": ["health"],
                                "briefing_email_html": False}, mode="managed")
    user_profiles.update_profile(database, "alice", google_scopes={"gmail": "readonly"})
    user_profiles.provision_user(database, "bob", {"display_name": "Retained"})
    with db.get_db(database) as conn:
        db.upsert_user_resource(conn, "alice", "folder", "/data/extra")
        locks = user_profiles.managed_fields(conn, "alice")
    user_briefings.ensure_briefing(database, user_id="alice", name="old", cron="0 8 * * *", output="email")
    target_config = load_config(target, apply_db_overlays=False)
    module = target_config.module_db_path("alice", "briefings")
    bdb.init_db(module)
    with bdb.connect(module) as conn:
        bdb.insert_archive(conn, briefing_name="old", subject="Retained", body_md="History")
        conn.commit()
    plan = tmp_path / "settings.json"
    plan.write_text(output)
    flags = ["--patch", str(tmp_path) + "=" + str(destination), "--prune", "--format", "json"]
    for expected in (2, 0):
        code, stdout, stderr = invoke(monkeypatch, capsys, target, "apply", "-f", plan, *flags)
        assert code == expected, stdout + stderr
    profile = user_profiles.get_profile(database, "alice")
    assert profile.display_name == "alice" and profile.disabled_modules == [] and profile.briefing_email_html
    assert profile.google_scopes == {"gmail": "readonly"}
    assert user_profiles.get_profile(database, "bob").display_name == "Retained"
    with db.get_db(database) as conn:
        assert user_profiles.managed_fields(conn, "alice") == locks
        assert db.get_user_resources(conn, "alice", "folder") == []
    with bdb.connect(module) as conn:
        assert conn.execute("SELECT COUNT(*) FROM briefing_archive").fetchone()[0] == 1
    code, stdout, stderr = invoke(monkeypatch, capsys, target, "snapshot", "--format", "json", "--user", "alice")
    assert code == 0, stderr
    assert json.loads(stdout)["users"] == rebased["users"]


def test_scoped_output_patch_metadata_and_private_replacement(install, tmp_path, monkeypatch, capsys):
    path, config = install
    content(install, source={"path": "/unselected/freeform"})
    with bdb.connect(config.module_db_path("alice", "briefings")) as conn:
        block = bdb.add_block(conn, briefing_name="morning", title="Shared")
        bdb.add_source(conn, block_id=block, kind="shared_block", config={"name": "news"})
        conn.commit()
    with db.get_db(config.db_path) as conn:
        db.upsert_user_resource(conn, "alice", "folder", "/data/documents")
    output = tmp_path / "output.json"
    output.write_text("old")
    output.chmod(0o644)
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "--user", "alice", "--no-meta",
                                 "--format", "json", "--patch", "/data=${DATA}", "-o", output)
    assert code == 0 and stdout == "" and "shared references" in stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    document = json.loads(output.read_text())
    assert set(document) == {"users"}
    assert document["users"]["alice"]["resources"][0]["path"] == "${DATA}/documents"
    assert document["users"]["alice"]["briefings"][0]["blocks"][0]["sources"][0]["config"]["path"] == "/unselected/freeform"
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "--user", "absent")
    assert code == 1 and stdout == "" and "unknown_user" in stderr


@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("surface", ["profile", "module", "file"])
def test_fresh_transaction_recheck_and_bounded_retry(install, monkeypatch, capsys, continuous, surface):
    from istota.settings_plan import snapshot

    path, config = install
    module = content(install)
    cron = resolve_user_config_dir(config, "alice") / "CRON.md"
    cron.parent.mkdir(parents=True)
    original = snapshot._gather
    calls = []
    def interleave(*args):
        gathered = original(*args)
        calls.append(1)
        if len(calls) == 1 or continuous:
            if surface == "profile":
                user_profiles.update_profile(config.db_path, "alice", display_name=f"Changed {len(calls)}")
            elif surface == "module":
                with bdb.connect(module) as conn:
                    conn.execute("UPDATE briefing_blocks SET title=?", (f"Changed {len(calls)}",))
                    conn.commit()
            else:
                cron.write_text(f"Concurrent notes {len(calls)}\n")
        return gathered
    monkeypatch.setattr(snapshot, "_gather", interleave)
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "--format", "json")
    assert len(calls) == 4
    if continuous:
        assert code == 1 and not stdout and "state_changed" in stderr
    else:
        assert code == 0, stderr
        if surface == "profile":
            assert json.loads(stdout)["users"]["alice"]["profile"]["display_name"] == "Changed 1"


def test_runtime_updates_do_not_cause_retry(install, monkeypatch, capsys):
    from istota.settings_plan import snapshot

    path, config = install
    content(install)
    original = snapshot._gather
    calls = []
    def interleave(*args):
        gathered = original(*args)
        calls.append(1)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE briefing_configs SET last_run_at=?", (str(len(calls)),))
        return gathered
    monkeypatch.setattr(snapshot, "_gather", interleave)
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot")
    assert code == 0 and stdout and len(calls) == 2, stderr


@pytest.mark.parametrize("invalid", ["framework", "module", "legacy_user", "cron", "heartbeat", "pending_seed"])
def test_invalid_or_uninitialized_state_refuses_without_replacement(install, tmp_path, monkeypatch, capsys, invalid):
    path, config = install
    if invalid == "framework":
        config.db_path.unlink()
    elif invalid == "module":
        module = config.module_db_path("alice", "briefings")
        module.parent.mkdir(parents=True)
        module.write_text("broken database")
    elif invalid in {"legacy_user", "pending_seed"}:
        raw = tomllib.loads(path.read_text())
        if invalid == "legacy_user":
            raw["users"] = {"bob": {"display_name": "Legacy"}}
        else:
            raw["default_briefings"] = [{"name": "morning", "cron": "0 7 * * *", "output": "email",
                                        "blocks": [{"title": "Calendar", "sources": [{"kind": "calendar"}]}]}]
        path.write_text(toml_write.dumps(raw))
    else:
        file = resolve_user_config_dir(config, "alice") / ("CRON.md" if invalid == "cron" else "HEARTBEAT.md")
        file.parent.mkdir(parents=True)
        file.write_text("```toml\nbroken = [\n```\n")
    output = tmp_path / "output.toml"
    output.write_text("existing")
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "-o", output)
    assert code == 1 and not stdout and stderr
    assert output.read_text() == "existing"
    if invalid == "framework":
        assert not config.db_path.exists() and "istota init" in stderr


def test_symlink_output_is_refused(install, tmp_path, monkeypatch, capsys):
    output = tmp_path / "output.toml"
    output.symlink_to(install[0])
    before = install[0].read_bytes()
    code, stdout, stderr = invoke(monkeypatch, capsys, install[0], "snapshot", "-o", output)
    assert code == 1 and not stdout and "output_requires_regular_file" in stderr
    assert output.is_symlink() and install[0].read_bytes() == before


@pytest.mark.parametrize("column,value", [("routing", "broken"), ("disabled_skills", "{}"), ("default_briefings", 2)])
def test_invalid_stored_profile_is_not_exported_as_defaults(install, monkeypatch, capsys, column, value):
    path, config = install
    with db.get_db(config.db_path) as conn:
        conn.execute(f"UPDATE user_profiles SET {column}=?", (value,))
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot")
    assert code == 1 and not stdout and "profile_store_invalid" in stderr


def test_scoped_snapshot_does_not_decode_another_users_profile(install, monkeypatch, capsys):
    path, config = install
    user_profiles.provision_user(config.db_path, "bob", {})
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE user_profiles SET max_background_workers='broken' WHERE user_id='bob'")
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "--user", "alice")
    assert code == 0 and set(tomllib.loads(stdout)["users"]) == {"alice"}, stderr


@pytest.mark.parametrize("where", ["source", "shared"])
def test_empty_embedded_credentials_remain_empty(install, monkeypatch, capsys, where):
    path, config = install
    empty = {"headers": [{"Authorization": ""}]}
    if where == "source":
        content(install, source=empty)
    else:
        with db.get_db(config.db_path) as conn:
            db.upsert_shared_block_config(conn, name="news", cron="0 7 * * *",
                                          sources=[{"kind": "browse", "config": empty}])
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "--format", "json")
    assert code == 0, stderr
    document = json.loads(stdout)
    sources = (document["users"]["alice"]["briefings"][0]["blocks"][0]["sources"] if where == "source"
               else document["shared_briefing_blocks"][0]["sources"])
    assert sources[0]["config"] == empty


def test_raw_config_nested_seed_credentials_keep_sentinel_contract(install, monkeypatch, capsys):
    path, _ = install
    raw = tomllib.loads(path.read_text())
    raw["extensions"] = {"models": {"model.v1": [{"api_key": "private-config-placeholder"}]}}
    path.write_text(toml_write.dumps(raw))
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "--format", "json")
    assert code == 0 and "private-config-placeholder" not in stdout + stderr
    assert json.loads(stdout)["config"]["extensions"]["models"]["model.v1"][0]["api_key"] == "<unchanged>"


def test_snapshot_destination_requires_private_inline_replacement(install, tmp_path, monkeypatch, capsys):
    from istota.settings_plan.paths import patch_document

    path, _ = install
    raw = tomllib.loads(path.read_text())
    raw["brain"] = {"native": {"extra_headers": {"x.tenant": "source-placeholder"}}}
    path.write_text(toml_write.dumps(raw))
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot", "--format", "json")
    assert code == 0, stderr
    document = json.loads(stdout)
    destination = tmp_path / "destination"
    destination.mkdir()
    rebased, _ = patch_document(document, [str(tmp_path) + "=" + str(destination)])
    target = destination / "config.toml"
    target_document = dict(rebased["config"])
    target_document.pop("brain")
    target.write_text(toml_write.dumps(target_document))
    database = destination / "framework.db"
    db.init_db(database)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(rebased))
    before = target.read_bytes(), database.read_bytes()
    code, stdout, stderr = invoke(monkeypatch, capsys, target, "apply", "-f", plan, "--dry-run", "--format", "json")
    assert code == 1
    assert json.loads(stdout)["errors"] == [{"code": "validation_error",
                                            "path": ["config", "brain", "native", "extra_headers", "x.tenant"]}]
    assert (target.read_bytes(), database.read_bytes()) == before
    sidecar = tmp_path / "private.json"
    sidecar.write_text(json.dumps({"config": [{"path": ["brain", "native", "extra_headers", "x.tenant"],
                                              "value": "replacement-placeholder"}]}))
    sidecar.chmod(0o600)
    for flags, expected in [(["--dry-run"], 2), ([], 2), ([], 0)]:
        code, stdout, stderr = invoke(monkeypatch, capsys, target, "apply", "-f", plan, "--format", "json",
                                     "--secrets-file", sidecar, *flags)
        assert code == expected, stdout + stderr
        assert "replacement-placeholder" not in stdout + stderr
        if flags:
            assert (target.read_bytes(), database.read_bytes()) == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert tomllib.loads(target.read_text())["brain"]["native"]["extra_headers"]["x.tenant"] == "replacement-placeholder"
