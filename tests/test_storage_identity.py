"""Storage-agnostic vocabulary spec — Stage 1 (config) + Stage 5 (helper).

The storage I/O is already backend-agnostic; these cover the config-derived
notion of *what backs storage* (``storage_backend`` / ``storage_label``) that
the prompt + skill layers key on, plus the optional ``workspace_root`` helper.
"""

from pathlib import Path

from istota.config import Config, NextcloudConfig, WebConfig


class TestStorageIdentity:
    def test_nextcloud_backed_when_url_set(self):
        cfg = Config(nextcloud=NextcloudConfig(url="https://cloud.example.com"))
        assert cfg.storage_is_nextcloud is True
        assert cfg.storage_backend == "nextcloud"
        assert cfg.storage_label == "Nextcloud"

    def test_local_when_url_blank(self):
        cfg = Config()
        assert cfg.storage_is_nextcloud is False
        assert cfg.storage_backend == "local"
        assert cfg.storage_label == "your workspace"

    def test_backend_independent_of_web_auth(self):
        # Storage vocabulary must track only what backs the files, never the
        # web-auth axis — this is the "not is_standalone" decision, locked in.
        none_auth = Config(
            web=WebConfig(auth="none"),
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
        )
        assert none_auth.storage_backend == "nextcloud"

        nc_auth_local = Config(web=WebConfig(auth="nextcloud"))
        assert nc_auth_local.storage_backend == "local"

    def test_backend_independent_of_mount_vs_rclone(self):
        # A Nextcloud URL means Nextcloud whether via mount or rclone.
        mounted = Config(
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            nextcloud_mount_path=Path("/srv/mount/nc"),
        )
        rclone = Config(nextcloud=NextcloudConfig(url="https://cloud.example.com"))
        assert mounted.storage_backend == "nextcloud"
        assert rclone.storage_backend == "nextcloud"


class TestWorkspaceRoot:
    def test_scoped_user_root_on_mount(self):
        cfg = Config(nextcloud_mount_path=Path("/srv/mount/nc"))
        assert cfg.workspace_root("alice") == Path("/srv/mount/nc/Users/alice")

    def test_unscoped_root_on_mount(self):
        cfg = Config(nextcloud_mount_path=Path("/srv/mount/nc"))
        assert cfg.workspace_root() == Path("/srv/mount/nc")

    def test_none_under_rclone(self):
        cfg = Config(nextcloud=NextcloudConfig(url="https://cloud.example.com"))
        assert cfg.nextcloud_mount_path is None
        assert cfg.workspace_root("alice") is None

    def test_local_workspace_root(self):
        cfg = Config(nextcloud_mount_path=Path("/home/me/.istota"))
        assert cfg.workspace_root("me") == Path("/home/me/.istota/Users/me")


# ---------------------------------------------------------------------------
# Stage 2: executor prompt framing
# ---------------------------------------------------------------------------

from istota import db  # noqa: E402
from istota.executor import build_prompt  # noqa: E402


def _task(**kw):
    defaults = dict(
        id=1, status="running", source_type="talk", user_id="alice",
        prompt="what's in my Downloads folder", conversation_token="room1",
    )
    defaults.update(kw)
    return db.Task(**defaults)


def _base_config(tmp_path, **kw):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "t.db",
        skills_dir=skills_dir,
        bundled_skills_dir=tmp_path / "_empty",
        temp_dir=tmp_path / "temp",
        **kw,
    )


class TestPromptStorageFramingLocal:
    def _prompt(self, tmp_path):
        config = _base_config(
            tmp_path,
            nextcloud_mount_path=tmp_path / "workspace",
        )
        assert config.storage_backend == "local"
        return build_prompt(_task(), [], config)

    def test_no_nextcloud_vocabulary(self, tmp_path):
        # Strip environment paths (pytest's tmp_path embeds the method name,
        # which can contain "nextcloud") so we test prose, not artifacts.
        prompt = self._prompt(tmp_path).replace(str(tmp_path), "<WS>")
        assert "Nextcloud" not in prompt
        assert "nextcloud" not in prompt
        assert "rclone" not in prompt
        assert "/srv/mount" not in prompt

    def test_names_the_workspace_root(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert str(tmp_path / "workspace") in prompt
        assert "workspace" in prompt

    def test_has_wider_fs_note(self, tmp_path):
        # Fixes the transcript's second failure: the bot must know the
        # workspace is its managed area, not the limit of what it can read.
        prompt = self._prompt(tmp_path).lower()
        assert "managed area" in prompt


class TestPromptStorageFramingNextcloud:
    def _prompt(self, tmp_path):
        config = _base_config(
            tmp_path,
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            nextcloud_mount_path=tmp_path / "mnt",
        )
        assert config.storage_backend == "nextcloud"
        return build_prompt(_task(), [], config)

    def test_keeps_nextcloud_vocabulary(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert "Nextcloud" in prompt
        assert str(tmp_path / "mnt") in prompt

    def test_no_wider_fs_note_when_sandboxed_server(self, tmp_path):
        prompt = self._prompt(tmp_path).lower()
        assert "managed area" not in prompt


class TestPromptStorageFramingRclone:
    def test_rclone_branch_still_nextcloud(self, tmp_path):
        config = _base_config(
            tmp_path,
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
        )
        assert config.storage_backend == "nextcloud"
        assert config.use_mount is False
        prompt = build_prompt(_task(), [], config)
        assert "rclone" in prompt
        assert "Nextcloud" in prompt


# ---------------------------------------------------------------------------
# Stage 3: skill prose (placeholders + neutral vocabulary)
# ---------------------------------------------------------------------------

from istota.skills._loader import (  # noqa: E402
    _BUNDLED_SKILLS_DIR,
    load_skill_index,
    load_skills,
)
from istota.skills.skills import _workspace_dir  # noqa: E402

# The only bundled skill bodies allowed to keep the literal "Nextcloud": the
# gated deployment note in `files`, and the Nextcloud-specific sharing skill.
_NEXTCLOUD_LITERAL_ALLOWLIST = {"files", "nextcloud"}


class TestSkillBodiesStatic:
    def _skill_md_files(self):
        return sorted(_BUNDLED_SKILLS_DIR.glob("*/skill.md"))

    def test_no_hardcoded_mount_path(self):
        offenders = [p for p in self._skill_md_files() if "/srv/mount" in p.read_text()]
        assert offenders == [], f"skill bodies still hardcode a mount path: {offenders}"

    def test_nextcloud_literal_confined_to_allowlist(self):
        offenders = [
            p.parent.name
            for p in self._skill_md_files()
            if "Nextcloud" in p.read_text()
            and p.parent.name not in _NEXTCLOUD_LITERAL_ALLOWLIST
        ]
        assert offenders == [], f"unexpected Nextcloud vocabulary in: {offenders}"


class TestSkillPlaceholderSubstitution:
    def _render_eager(self, tmp_path, names, config, user_id="alice"):
        """Mimic the executor: load_skills then substitute {workspace}/{storage}."""
        empty_ops = tmp_path / "ops"
        empty_ops.mkdir(exist_ok=True)
        index = load_skill_index(empty_ops, bundled_dir=_BUNDLED_SKILLS_DIR)
        doc = load_skills(
            empty_ops, names, config.bot_name, config.bot_dir_name,
            skill_index=index, bundled_dir=_BUNDLED_SKILLS_DIR,
        )
        ws = config.workspace_root(user_id)
        ws_str = str(ws) if ws is not None else f"{config.rclone_remote}:/Users/{user_id}"
        return (
            doc.replace("{workspace}", ws_str).replace("{storage}", config.storage_label)
        )

    def test_workspace_placeholder_resolves_local(self, tmp_path):
        config = _base_config(tmp_path, nextcloud_mount_path=tmp_path / "ws")
        rendered = self._render_eager(tmp_path, ["files"], config)
        assert "{workspace}" not in rendered
        assert "/srv/mount" not in rendered
        assert str(tmp_path / "ws" / "Users" / "alice") in rendered

    def test_files_skill_no_nextcloud_examples_local(self, tmp_path):
        config = _base_config(tmp_path, nextcloud_mount_path=tmp_path / "ws")
        rendered = self._render_eager(tmp_path, ["files"], config)
        # The command examples are storage-neutral now; only the gated
        # deployment note may mention Nextcloud.
        assert "files are mounted at" not in rendered

    def test_workspace_dir_helper_scoped(self, tmp_path):
        config = _base_config(tmp_path, nextcloud_mount_path=tmp_path / "ws")
        assert _workspace_dir(config, "alice") == str(tmp_path / "ws" / "Users" / "alice")

    def test_workspace_dir_helper_rclone(self, tmp_path):
        config = _base_config(tmp_path, nextcloud=NextcloudConfig(url="https://c.example.com"))
        assert _workspace_dir(config, "alice") == "nextcloud:/Users/alice"
