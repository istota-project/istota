"""`istota migrate import-standalone`: a standalone install's state in the local VM's stack.

The source is what the Mac's `istota-stack import-standalone` stages from a
standalone root (`~/.istota`): a MANIFEST naming that root, the standalone
`config.toml` and `admins`, online backups of the databases in db_backup's
snapshot layout, the session transcripts and the names under `Users/`. The
standalone `istota.env` comes on stdin. Here the root is a fixture path that
exists nowhere: only its name matters to the importer, which never reads the
Mac.

The target is a directory standing in for `/data` (`--data-dir`), the config
file, the stack's `secrets/` and the staging directory `istota-stack` hands
over (`--vm-dir`), in the shape `create --no-setup` leaves them: no config, no
master key, the secrets files empty. Every value is synthetic.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from io import StringIO
from pathlib import Path

import pytest

from istota import db, user_profiles
from istota.config import load_config
from istota.maintenance import standalone_import as si
from istota.setup_wizard import SECRET_NAMES

MAC_HOME = "/Users/alice"
ROOT = f"{MAC_HOME}/.istota"
SECRET_KEY = "k" * 64
OTHER_KEY = "z" * 64
DATE = "2026-10-12"

STANDALONE_CONFIG = f"""\
bot_name = "Istota"
emissaries_enabled = false
db_path = "{ROOT}/istota.db"
workspace_path = "{ROOT}"
temp_dir = "{ROOT}/tmp"

[web]
enabled = true
auth = "none"
port = 8766

[talk]
enabled = false

[email]
enabled = true
imap_host = "imap.example.test"
imap_user = "bot@example.test"
smtp_host = "smtp.example.test"

[location]
enabled = false

[caldav]
url = "https://dav.example.test/"
username = "alice"

[security]
sandbox_enabled = false
skill_proxy_enabled = false
sandbox_ro_paths = ["{MAC_HOME}/Code"]

[scheduler]
db_backup_dir = "{ROOT}/Backups/db/snapshots"

[brain]
kind = "native"

[brain.native]
base_url = "http://localhost:11434/v1"
model = "qwen3"

[briefings]
newsletter_max_links_per_source = 7

[[default_briefings]]
name = "morning"
cron = "0 7 * * *"

[users.alice]
display_name = "Alice"
timezone = "Europe/Berlin"
disabled_modules = ["money"]

[[users.alice.resources]]
type = "folder"
path = "{MAC_HOME}/Projects"
name = "Projects"
"""

#: What `istota setup` writes, with the master key twice: the first line is the
#: one `istota serve` used, the second a dead copy an operator appended.
STANDALONE_ENV = f"""\
# Istota local secrets
ISTOTA_SECRET_KEY={SECRET_KEY}

ISTOTA_WEB_INSECURE_COOKIES=1
ISTOTA_WEB_SESSION_SECRET_KEY={"s" * 64}
ISTOTA_ADMINS_FILE={MAC_HOME}/.config/istota/admins
ISTOTA_BRAIN_NATIVE_API_KEY=native-key
ISTOTA_EMAIL_IMAP_PASSWORD=imap-password
ISTOTA_EMAIL_SMTP_PASSWORD=imap-password
export ISTOTA_CALDAV_PASSWORD="caldav-password"
ISTOTA_SECRET_KEY={OTHER_KEY}
"""


def _framework_db(path: Path, *, profiles: tuple[str, ...] = ("alice",)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    for user_id in profiles:
        conn = sqlite3.connect(path)
        try:
            # Straight into the table: a standalone install made before
            # case-insensitive ids could hold any spelling.
            conn.execute("INSERT INTO user_profiles (user_id, display_name) VALUES (?, ?)", (user_id, user_id))
            conn.commit()
        finally:
            conn.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, status, source_type, user_id, prompt, attachments) "
            "VALUES (1, 'completed', 'web', 'alice', 'look', ?)",
            (json.dumps([f"{ROOT}/Users/alice/inbox/a.pdf", f"{MAC_HOME}/Desktop/b.pdf"]),),
        )
        conn.execute(
            "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, "
            "content, content_hash, metadata_json) VALUES (?, ?, ?, 0, ?, ?, ?)",
            ("alice", "memory_file", f"{ROOT}/Users/alice/memories/2026-10-01.md", "one", "h1",
             json.dumps({"file_path": f"{ROOT}/Users/alice/memories/2026-10-01.md"})),
        )
        conn.execute(
            "INSERT INTO user_resources (user_id, resource_type, resource_path, display_name) "
            "VALUES ('alice', 'folder', ?, 'Projects')", (f"{MAC_HOME}/Projects",),
        )
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()


def _module_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT)")
    conn.execute("INSERT INTO feeds (url) VALUES ('https://example.test/feed.xml')")
    conn.commit()
    conn.close()


def _stage(tmp_path: Path, *, config: str = STANDALONE_CONFIG, profiles: tuple[str, ...] = ("alice",),
           workspace_users: tuple[str, ...] = ("alice",)) -> Path:
    """What the Mac CLI stages from a standalone root."""
    source = tmp_path / "standalone"
    source.mkdir()
    (source / "MANIFEST").write_text(
        f"format={si.MANIFEST_FORMAT}\nsource_root={ROOT}\nmac_home={MAC_HOME}\n"
        "exported_at=2026-10-12T10:00:00Z\n"
    )
    (source / "config.toml").write_text(config)
    (source / "admins").write_text("# admins\nalice\n")
    snapshot = source / "db" / DATE
    _framework_db(snapshot / "framework" / "istota.db", profiles=profiles)
    _module_db(snapshot / "alice" / "feeds.db")
    (source / "logs" / "alice").mkdir(parents=True)
    (source / "logs" / "alice" / "20261001_task-1-1.jsonl").write_text('{"type": "session"}\n')
    (source / "workspace-users").write_text("".join(f"{name}\n" for name in workspace_users))
    return source


#: An older standalone install's istota.env: every credential, no master key.
ENV_WITHOUT_KEY = "\n".join(
    line for line in STANDALONE_ENV.splitlines() if not line.startswith("ISTOTA_SECRET_KEY=")
) + "\n"

#: A row in each table the master key encrypts into, as its writer leaves one.
ENCRYPTED_ROWS = {
    "secrets": (
        "INSERT INTO secrets (user_id, service, key, encrypted_value) VALUES ('alice', 'karakeep', 'api_key', :ct)"
    ),
    "secrets_history": (
        "INSERT INTO secrets_history (user_id, service, key, encrypted_value, op, actor) "
        "VALUES ('alice', 'karakeep', 'api_key', :ct, 'update', 'alice')"
    ),
    "google_oauth_tokens": (
        "INSERT INTO google_oauth_tokens (user_id, access_token, refresh_token, token_expiry) "
        "VALUES ('alice', :ct, :ct, '2026-10-12T00:00:00Z')"
    ),
}


def _framework_conn(source: Path) -> sqlite3.Connection:
    return sqlite3.connect(source / "db" / DATE / "framework" / "istota.db")


def _encrypted_row(source: Path, table: str) -> None:
    from cryptography.fernet import Fernet

    conn = _framework_conn(source)
    try:
        conn.execute(ENCRYPTED_ROWS[table], {"ct": Fernet(Fernet.generate_key()).encrypt(b"value")})
        conn.commit()
    finally:
        conn.close()


def _plaintext_google_row(source: Path) -> None:
    conn = _framework_conn(source)
    try:
        conn.execute(
            "INSERT INTO google_oauth_tokens (user_id, access_token, refresh_token, token_expiry) "
            "VALUES ('alice', 'ya29.plain', '1//plain', '2026-10-12T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()


class Target:
    """`/data`, the config file, `secrets/` and the staging directory, as
    `create --no-setup` and `istota-stack import-standalone` leave them."""

    def __init__(self, tmp_path: Path):
        self.data = tmp_path / "data"
        self.config = self.data / "config" / "config.toml"
        self.vm = tmp_path / "vm"
        self.secrets = self.vm / "secrets"
        self.config.parent.mkdir(parents=True)
        self.secrets.mkdir(parents=True)
        for name in SECRET_NAMES:
            (self.secrets / name).write_text("")
        (self.vm / ".env").write_text("ISTOTA_TAG=v1.2.0\n")
        (self.vm / "host.env").write_text(
            "STORAGE=host-folder\nINGRESS=local\nHOST_LOOPBACK_BLOCK=1\nHOST_GATEWAY_ADDR=192.168.5.2\n"
        )

    def args(self, source: Path, *extra: str) -> list[str]:
        return [
            "--source", str(source),
            "--hostname", "localhost:8080",
            "--config", str(self.config),
            "--vm-dir", str(self.vm),
            "--secrets-dir", str(self.secrets),
            "--data-dir", str(self.data),
            *extra,
        ]

    @property
    def db_path(self) -> Path:
        return self.data / "db" / "istota.db"

    def rows(self, sql: str, *params) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def snapshot(self) -> dict[str, bytes]:
        out = {}
        for root in (self.data, self.vm):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    out[str(path)] = path.read_bytes()
        return out


@pytest.fixture
def target(tmp_path):
    return Target(tmp_path)


@pytest.fixture
def source(tmp_path):
    return _stage(tmp_path)


def _run(target: Target, source: Path, *extra: str, env: str = STANDALONE_ENV):
    out, err = StringIO(), StringIO()
    code = si.main(target.args(source, *extra), stdin=StringIO(env), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class TestAnImport:
    @pytest.fixture
    def imported(self, target, source):
        code, out, err = _run(target, source)
        assert code == 0, out + err
        return target, out

    def test_the_config_is_the_stacks_with_the_users_choices(self, imported, monkeypatch):
        target, _ = imported
        doc = tomllib.loads(target.config.read_text())
        assert doc["db_path"] == "/data/db/istota.db"
        assert doc["temp_dir"] == "/data/tmp"
        assert doc["workspace_path"] == "/mnt/shared"
        assert doc["workspace_mount_path"] == "/mnt/shared"
        assert doc["security"] == {"sandbox_enabled": True, "skill_proxy_enabled": True}
        assert doc["site"]["hostname"] == "localhost:8080"
        assert doc["web"]["auth"] == ["email"]
        # Carried: the brain, mail, CalDAV and briefing settings, and the user.
        assert doc["brain"]["native"]["model"] == "qwen3"
        assert doc["email"]["imap_host"] == "imap.example.test"
        assert doc["caldav"]["url"] == "https://dav.example.test/"
        assert doc["briefings"]["newsletter_max_links_per_source"] == 7
        assert doc["default_briefings"][0]["name"] == "morning"
        assert doc["users"]["alice"]["timezone"] == "Europe/Berlin"
        assert doc["users"]["alice"]["disabled_modules"] == ["money"]
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(target.config))
        config = load_config(target.config)
        assert config.web.auth == ["email"]
        assert "alice" in config.users

    def test_no_mac_path_is_left_in_the_config(self, imported):
        target, _ = imported
        assert MAC_HOME not in target.config.read_text()

    def test_a_setting_pointing_outside_the_root_is_reported_and_dropped(self, imported):
        target, out = imported
        doc = tomllib.loads(target.config.read_text())
        assert "resources" not in doc["users"]["alice"]
        assert f"users.alice.resources[0].path: {MAC_HOME}/Projects" in out
        assert f"security.sandbox_ro_paths[0]: {MAC_HOME}/Code" in out

    def test_a_model_on_the_macs_loopback_is_named(self, imported):
        _, out = imported
        assert "http://localhost:11434/v1" in out
        assert "allow-host-port 11434" in out

    def test_the_first_master_key_line_wins(self, imported):
        target, _ = imported
        assert (target.data / ".secret_key").read_text() == SECRET_KEY
        assert ((target.data / ".secret_key").stat().st_mode & 0o777) == 0o600

    def test_the_credentials_go_to_the_secrets_files(self, imported):
        target, out = imported
        assert (target.secrets / "istota_brain_native_api_key").read_text() == "native-key"
        assert (target.secrets / "istota_email_imap_password").read_text() == "imap-password"
        assert (target.secrets / "istota_caldav_password").read_text() == "caldav-password"
        assert (target.secrets / "istota_web_session_secret_key").read_text() == "s" * 64
        for value in ("native-key", "imap-password", "caldav-password", SECRET_KEY):
            assert value not in out
            assert value not in target.config.read_text()

    def test_the_stack_files_are_staged_for_the_local_vm(self, imported):
        target, _ = imported
        env = (target.vm / ".env").read_text()
        assert "ISTOTA_TAG=v1.2.0\n" in env
        assert "NGINX_PUBLISH=127.0.0.1:8080:80\n" in env
        assert "DOMAIN=localhost:8080\n" in env
        host = (target.vm / "host.env").read_text()
        assert "STORAGE=host-folder\n" in host
        assert "HOST_LOOPBACK_BLOCK=1\n" in host

    def test_the_admins_file_comes_across(self, imported):
        target, _ = imported
        assert "alice" in (target.config.parent / "admins").read_text()

    def test_the_databases_are_restored(self, imported):
        target, _ = imported
        assert target.rows("SELECT user_id FROM user_profiles") == [("alice",)]
        module = target.data / "db" / "modules" / "alice" / "feeds.db"
        conn = sqlite3.connect(module)
        try:
            assert conn.execute("SELECT url FROM feeds").fetchall() == [("https://example.test/feed.xml",)]
        finally:
            conn.close()

    def test_the_transcripts_come_across(self, imported):
        target, _ = imported
        assert (target.data / "db" / "logs" / "alice" / "20261001_task-1-1.jsonl").is_file()

    def test_stored_paths_under_the_root_are_rebased(self, imported):
        target, _ = imported
        (attachments,) = target.rows("SELECT attachments FROM tasks WHERE id = 1")[0]
        assert json.loads(attachments) == [
            "/mnt/shared/Users/alice/inbox/a.pdf", f"{MAC_HOME}/Desktop/b.pdf",
        ]
        (source_id,) = target.rows("SELECT source_id FROM memory_chunks")[0]
        assert source_id == "/mnt/shared/Users/alice/memories/2026-10-01.md"

    def test_a_resource_outside_the_root_is_reported_and_left(self, imported):
        target, out = imported
        assert target.rows("SELECT resource_path FROM user_resources") == [(f"{MAC_HOME}/Projects",)]
        assert "user_resources.resource_path" in out
        assert f"{MAC_HOME}/Desktop/b.pdf" in out

    def test_the_import_is_recorded(self, imported):
        target, _ = imported
        (value,) = target.rows(
            "SELECT value FROM shared_kv WHERE namespace = '_migration' AND key = ?", si.RECORD_KEY,
        )[0]
        record = json.loads(value)
        assert record["source_root"] == ROOT
        assert [ROOT, "/mnt/shared"] in record["prefixes"]
        assert not (target.data / si.PROGRESS_NAME).exists()

    def test_a_second_run_does_nothing(self, imported, source):
        target, _ = imported
        before = target.snapshot()
        code, out, err = _run(target, source)
        assert code == 0, out + err
        assert "already imported" in out
        assert target.snapshot() == before

    def test_the_users_own_files_are_named_for_the_path_rewrite(self, imported):
        _, out = imported
        assert f"istota migrate workspace-paths --map {ROOT}=/mnt/shared" in out


class TestRefusals:
    """A refusal writes nothing: no config, no key, no database."""

    def _refused(self, target: Target, source: Path, *extra: str, env: str = STANDALONE_ENV) -> str:
        before = target.snapshot()
        code, out, err = _run(target, source, *extra, env=env)
        assert code == 1, out + err
        assert target.snapshot() == before
        assert not target.db_path.exists()
        return err

    def test_user_ids_that_differ_only_in_case_are_refused(self, tmp_path, target):
        """On the Mac's folder `Users/Alice` and `Users/alice` are one directory."""
        source = _stage(tmp_path, profiles=("alice", "Alice"))
        err = self._refused(target, source)
        assert "Alice" in err and "alice" in err
        assert "case" in err

    def test_a_folder_under_users_colliding_with_a_user_is_refused(self, tmp_path, target):
        source = _stage(tmp_path, workspace_users=("alice", "ALICE"))
        err = self._refused(target, source)
        assert "ALICE" in err

    def test_a_master_key_line_too_short_to_use_is_refused(self, target, source):
        err = self._refused(target, source, env="ISTOTA_SECRET_KEY=short\nISTOTA_BRAIN_NATIVE_API_KEY=native-key\n")
        assert "ISTOTA_SECRET_KEY" in err

    @pytest.mark.parametrize("table", sorted(ENCRYPTED_ROWS))
    def test_no_master_key_with_rows_encrypted_under_one_is_refused(self, tmp_path, target, table):
        source = _stage(tmp_path)
        _encrypted_row(source, table)
        err = self._refused(target, source, env=ENV_WITHOUT_KEY)
        assert "ISTOTA_SECRET_KEY" in err
        assert f"{table} (1 row)" in err

    def test_an_unknown_variable_is_refused_by_name_only(self, target, source):
        err = self._refused(target, source, env=STANDALONE_ENV + "MY_OTHER_TOKEN=hunter2\n")
        assert "MY_OTHER_TOKEN" in err
        assert "hunter2" not in err

    def test_a_config_naming_another_workspace_is_refused(self, tmp_path, target):
        config = STANDALONE_CONFIG.replace(f'workspace_path = "{ROOT}"', 'workspace_path = "/Volumes/x"')
        source = _stage(tmp_path, config=config)
        err = self._refused(target, source)
        assert "workspace_path" in err

    def test_a_different_master_key_already_there_is_refused(self, target, source):
        (target.data / ".secret_key").write_text(OTHER_KEY)
        err = self._refused(target, source)
        assert "master key" in err

    def test_a_stack_that_has_done_work_is_refused(self, tmp_path, target, source):
        target.db_path.parent.mkdir(parents=True)
        db.init_db(target.db_path)
        conn = sqlite3.connect(target.db_path)
        conn.execute("INSERT INTO tasks (status, source_type, user_id, prompt) VALUES ('completed', 'web', 'bob', 'x')")
        conn.commit()
        conn.close()
        code, out, err = _run(target, source)
        assert code == 1, out + err
        assert "tasks" in err
        assert not target.config.exists()

    def test_a_manifest_of_another_format_is_refused(self, target, source):
        (source / "MANIFEST").write_text("format=1\n")
        self._refused(target, source)


class TestAnInstallWithNoMasterKey:
    """An older standalone install's istota.env has no ISTOTA_SECRET_KEY. With
    nothing in its database encrypted under one, the stack gets a new key, as
    `istota setup` makes one, and every other credential still comes across."""

    def test_a_new_key_is_made_and_the_credentials_come_across(self, target, source):
        code, out, err = _run(target, source, env=ENV_WITHOUT_KEY)
        assert code == 0, out + err
        key = (target.data / ".secret_key").read_text()
        assert len(key) == 64 and set(key) <= set("0123456789abcdef")
        assert key not in out + err
        assert ((target.data / ".secret_key").stat().st_mode & 0o777) == 0o600
        assert (target.secrets / "istota_brain_native_api_key").read_text() == "native-key"
        assert (target.secrets / "istota_caldav_password").read_text() == "caldav-password"
        assert "new master key" in out
        assert target.rows("SELECT prompt FROM tasks") == [("look",)]

    def test_a_plaintext_google_token_is_not_counted(self, tmp_path, target):
        """Without a key the standalone never encrypted its Google tokens; the
        stack's migration encrypts them under the new key at its first start."""
        source = _stage(tmp_path)
        _plaintext_google_row(source)
        code, out, err = _run(target, source, env=ENV_WITHOUT_KEY)
        assert code == 0, out + err
        assert (target.data / ".secret_key").is_file()

    def test_a_key_already_on_the_vm_is_kept(self, target, source):
        """A partial run made one; the re-run keeps it rather than refusing a
        key that differs from a freshly made one."""
        (target.data / ".secret_key").write_text(OTHER_KEY)
        code, out, err = _run(target, source, env=ENV_WITHOUT_KEY)
        assert code == 0, out + err
        assert (target.data / ".secret_key").read_text() == OTHER_KEY

    def test_a_dry_run_says_so_and_writes_nothing(self, target, source):
        before = target.snapshot()
        code, out, err = _run(target, source, "--dry-run", env=ENV_WITHOUT_KEY)
        assert code == 0, out + err
        assert "new master key" in out
        assert target.snapshot() == before


def test_the_encrypted_tables_are_every_table_the_master_key_writes():
    """`credentials.store.MASTER_KEY_COLUMNS` is what the import counts. It is
    held to the code: every table the store inserts into, and every table a
    function elsewhere inserts into after taking the store's Fernet. A new
    module taking it fails the first assertion, so its tables get looked at."""
    import ast
    import importlib
    import re

    from istota.credentials import store
    from tests.support.drift import source_of

    src = Path(store.__file__).resolve().parents[1]
    callers = set()
    for path in src.rglob("*.py"):
        if path != Path(store.__file__).resolve() and re.search(
            r"\._get_fernet\(|import[^\n]*\b_get_fernet\b", path.read_text(encoding="utf-8")
        ):
            callers.add(str(path.relative_to(src)))
    # doctor decrypts wallet rows of `secrets` to check them and writes nothing.
    assert callers == {"db.py", "doctor.py"}

    def inserts(node) -> set[str]:
        found = set()
        for leaf in ast.walk(node):
            if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                found.update(re.findall(r"INSERT INTO\s+(\w+)", leaf.value))
        return found

    tables = inserts(ast.parse(source_of(store)))
    for name in sorted(callers):
        module = importlib.import_module("istota." + name.removesuffix(".py").replace("/", "."))
        for fn in ast.walk(ast.parse(source_of(module))):
            if isinstance(fn, ast.FunctionDef) and any(
                isinstance(n, ast.Attribute) and n.attr == "_get_fernet" for n in ast.walk(fn)
            ):
                tables |= inserts(fn)
    assert tables == set(store.MASTER_KEY_COLUMNS)


class TestADryRun:
    def test_it_prints_the_plan_and_writes_nothing(self, target, source):
        before = target.snapshot()
        code, out, err = _run(target, source, "--dry-run")
        assert code == 0, out + err
        assert "workspace_path" in out
        assert "istota_brain_native_api_key" in out
        assert "tasks.attachments" in out
        assert target.snapshot() == before
        assert not target.config.exists()


def test_the_profile_case_rule_is_the_one_the_config_applies():
    """The refusal reads `lib.user_ids`, the rule `load_config` and the profile
    writers apply: a pair the stack would refuse at every start is refused here."""
    with pytest.raises(si.Refusal):
        si.check_user_ids(["alice"], ["Alice"], [])
    si.check_user_ids(["alice"], ["alice"], ["alice"])


def test_user_profiles_is_what_the_check_reads(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    user_profiles.ensure_profile(path, "alice")
    assert si.profile_user_ids(path) == ["alice"]
