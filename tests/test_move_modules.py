"""`scripts/move_modules.py`: the mover the package reorganisation runs once per stage.

Every later stage of the reorganisation is this script plus a few hand edits,
so a defect here lands in every stage at once. Two halves:

- A miniature repository in a temp directory, with a small move table of its
  own, drives each rewrite rule through a real `git mv`. The real table cannot
  be used there: its other rows name modules the fixture does not have, which
  the script rightly refuses as an inconsistent pair.
- The real table, checked against the real `src/`: every row names a module
  that exists, and no row can be misread by the rewrite rules.
"""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "move_modules.py"


def _load():
    spec = importlib.util.spec_from_file_location("move_modules", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["move_modules"] = module
    spec.loader.exec_module(module)
    return module


mm = _load()

MINI = mm.Table(
    moves=[
        ("rooms", "istota.room_policy", "istota.rooms.policy"),
        ("rooms", "istota.room_scopes", "istota.rooms.scopes"),
        ("notes", "istota.notifications", "istota.notifications.delivery"),
        ("notes", "istota.notification_store", "istota.notifications.store"),
        ("web", "istota.web_app", "istota.webui.app"),
        ("pkg", "istota.old_pkg", "istota.newpkg.sub"),
    ],
    packages=frozenset({"istota.old_pkg"}),
    collisions=frozenset({"istota.notifications"}),
    stubs=frozenset({"istota.web_app"}),
)

CONSUMER = """\
from istota import db, room_policy, room_scopes as rs  # noqa: F401
from istota import notifications
from . import notification_store


def go():
    from istota import (
        db,
        room_policy,
    )
    return room_policy, rs, notifications, notification_store, db
"""

FILES = {
    "src/istota/__init__.py": "",
    "src/istota/db.py": "def connect():\n    pass\n",
    "src/istota/room_policy.py": (
        "from . import db\n"
        "from .room_scopes import scope_for\n"
        "\n"
        "\n"
        "def readers():\n"
        "    return db, scope_for\n"
    ),
    "src/istota/room_scopes.py": "def scope_for():\n    pass\n",
    "src/istota/notifications.py": "def send_notification():\n    pass\n",
    "src/istota/notification_store.py": "def write():\n    pass\n",
    "src/istota/web_app.py": "app = object()\n",
    "src/istota/old_pkg/__init__.py": "from .inner import VALUE  # noqa: F401\n",
    "src/istota/old_pkg/inner.py": "from .. import db  # noqa: F401\n\nVALUE = 1\n",
    "src/istota/brain/__init__.py": "",
    "src/istota/brain/_types.py": "T = int\n",
    "src/istota/brain/core.py": "from ._types import T  # noqa: F401\nfrom .. import room_policy  # noqa: F401\n",
    "src/istota/consumer.py": CONSUMER,
    "tests/test_things.py": (
        'PATCH = "istota.notifications.send_notification"\n'
        'ALREADY = "istota.notifications.store.write"\n'
        'LOGGER = "istota.room_policy"\n'
        'OWNER = "room_policy.py"\n'
        'KEEP = "istota.web_app"  # move-modules: keep\n'
    ),
    "deploy/istota-web.service.j2": "ExecStart=uvicorn {{ istota_package }}.web_app:app\n",
    "docs/rooms.md": (
        "See `room_policy.py`, and src/istota/room_policy.py.\n"
        "Prose names room_policy.py without backticks.\n"
        "The package `old_pkg/` and src/istota/old_pkg/inner.py.\n"
    ),
    "CHANGELOG.md": "istota.room_policy and `room_policy.py`\n",
    "tests/golden/prompt.txt": "istota.room_policy\n",
    "docker/devbox/lib/istota_copy.py": "import istota.room_policy  # noqa: F401\n",
}

EXCLUDED = ("CHANGELOG.md", "tests/golden/prompt.txt", "docker/devbox/lib/istota_copy.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _commit(repo: Path, message: str = "step") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "mover@example.invalid")
    _git(root, "config", "user.name", "Mover Test")
    _commit(root, "fixture")
    return root


def run(repo: Path, *args: str) -> int:
    return mm.main([*args, "--root", str(repo)], table=MINI)


def read(repo: Path, rel: str) -> str:
    return (repo / rel).read_text()


def snapshot(repo: Path) -> dict[str, str]:
    return {
        str(p.relative_to(repo)): p.read_text()
        for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


class TestMoves:
    def test_a_move_is_a_git_rename_and_creates_an_empty_package(self, repo):
        assert run(repo, "--only", "rooms") == 0
        status = _git(repo, "status", "--porcelain")
        assert any(
            line.startswith("R") and "src/istota/room_policy.py -> src/istota/rooms/policy.py" in line
            for line in status.splitlines()
        ), status
        assert read(repo, "src/istota/rooms/__init__.py") == ""
        assert "A  src/istota/rooms/__init__.py" in status
        assert not (repo / "src/istota/room_policy.py").exists()

    def test_a_package_move_takes_the_whole_tree(self, repo):
        assert run(repo, "--only", "pkg") == 0
        assert not (repo / "src/istota/old_pkg").exists()
        assert read(repo, "src/istota/newpkg/sub/__init__.py") == "from .inner import VALUE  # noqa: F401\n"
        assert read(repo, "src/istota/newpkg/__init__.py") == ""

    def test_check_is_1_before_0_after_and_a_second_run_changes_nothing(self, repo):
        stages = ["rooms", "notes", "web", "pkg"]
        assert run(repo, "--check", "--only", *stages) == 1
        assert snapshot(repo) == {k: v for k, v in FILES.items()}, "--check must change nothing"
        assert run(repo, "--only", *stages) == 0
        assert run(repo, "--check", "--only", *stages) == 0
        _commit(repo)
        before = snapshot(repo)
        assert run(repo, "--only", *stages) == 0
        assert snapshot(repo) == before
        assert run(repo) == 0
        assert snapshot(repo) == before


class TestFromIstota:
    def test_moved_names_split_out_under_their_old_local_names(self, repo):
        assert run(repo, "--only", "rooms", "notes") == 0
        assert read(repo, "src/istota/consumer.py") == (
            "from istota import db  # noqa: F401\n"
            "from istota.rooms import policy as room_policy  # noqa: F401\n"
            "from istota.rooms import scopes as rs  # noqa: F401\n"
            "from istota.notifications import delivery as notifications\n"
            "from istota.notifications import store as notification_store\n"
            "\n"
            "\n"
            "def go():\n"
            "    from istota import (\n"
            "        db,\n"
            "    )\n"
            "    from istota.rooms import policy as room_policy\n"
            "    return room_policy, rs, notifications, notification_store, db\n"
        )

    def test_a_line_separator_ast_does_not_count_shifts_nothing(self, repo):
        """`str.splitlines` breaks on U+2028 and friends; `ast` line numbers do not."""
        path = repo / "src/istota/sep.py"
        path.write_text('X = "a b"\ndef f():\n    y = 1\n    from istota import room_policy\n    return room_policy, y\n')
        _commit(repo)
        assert run(repo, "--only", "rooms") == 0
        text = path.read_text()
        assert "    y = 1\n" in text
        assert "    from istota.rooms import policy as room_policy\n" in text

    def test_the_rewritten_file_still_parses(self, repo):
        assert run(repo, "--only", "rooms", "notes", "web", "pkg") == 0
        for path in (repo / "src").rglob("*.py"):
            ast.parse(path.read_text(), filename=str(path))


class TestRelativeImports:
    def test_a_moved_files_relative_imports_become_absolute(self, repo):
        assert run(repo, "--only", "rooms") == 0
        assert read(repo, "src/istota/rooms/policy.py").startswith(
            "from istota import db\nfrom istota.rooms.scopes import scope_for\n"
        )

    def test_an_import_into_a_moved_module_becomes_absolute(self, repo):
        assert run(repo, "--only", "rooms") == 0
        assert read(repo, "src/istota/brain/core.py") == (
            "from ._types import T  # noqa: F401\n"
            "from istota.rooms import policy as room_policy  # noqa: F401\n"
        )

    def test_imports_inside_a_moved_package_stay_relative_unless_they_leave_it(self, repo):
        assert run(repo, "--only", "pkg") == 0
        assert read(repo, "src/istota/newpkg/sub/__init__.py") == "from .inner import VALUE  # noqa: F401\n"
        assert read(repo, "src/istota/newpkg/sub/inner.py").startswith("from istota import db  # noqa: F401\n")

    def test_a_relative_import_carried_into_a_moved_file_by_a_merge_is_fixed(self, repo):
        """After the move a branch's merge can bring `from .db import` into the new file."""
        assert run(repo, "--only", "rooms") == 0
        _commit(repo)
        policy = repo / "src/istota/rooms/policy.py"
        policy.write_text(policy.read_text() + "from .db import connect  # noqa: F401\n"
                          "from .scopes import scope_for as again  # noqa: F401\n")
        _commit(repo)
        assert run(repo) == 0
        text = policy.read_text()
        assert "from istota.db import connect  # noqa: F401\n" in text
        # Written against the new home: it already resolves, so it is left alone.
        assert "from .scopes import scope_for as again  # noqa: F401\n" in text

    def test_a_name_at_both_homes_is_reported(self, repo, capsys):
        assert run(repo, "--only", "rooms") == 0
        (repo / "src/istota/rooms/db.py").write_text("")
        policy = repo / "src/istota/rooms/policy.py"
        policy.write_text(policy.read_text() + "from . import db as again  # noqa: F401\n")
        _commit(repo)
        capsys.readouterr()
        assert run(repo) == 0
        assert "from . import db as again" in policy.read_text()
        assert "resolves at both the old and the new home" in capsys.readouterr().out


class TestDottedReferences:
    def test_the_collision_rule(self, repo):
        assert run(repo, "--only", "notes") == 0
        text = read(repo, "tests/test_things.py")
        assert 'PATCH = "istota.notifications.delivery.send_notification"' in text
        assert 'ALREADY = "istota.notifications.store.write"' in text
        _commit(repo)
        assert run(repo, "--only", "notes") == 0
        assert read(repo, "tests/test_things.py") == text

    def test_a_module_added_to_a_landed_collision_package_is_left_alone(self, repo):
        assert run(repo, "--only", "notes") == 0
        _commit(repo)
        (repo / "src/istota/notifications/digest.py").write_text("X = 1\n")
        (repo / "src/istota/uses.py").write_text(
            "from istota.notifications import digest  # noqa: F401\n"
            'PATCH = "istota.notifications.digest.X"\n'
        )
        _commit(repo)
        before = snapshot(repo)
        assert run(repo, "--check") == 0
        assert run(repo) == 0
        assert snapshot(repo) == before

    def test_a_bare_collision_token_after_landing_is_reported_not_rewritten(self, repo, capsys):
        assert run(repo, "--only", "notes") == 0
        _commit(repo)
        (repo / "docs/pkg.md").write_text("The `istota.notifications` package.\n")
        (repo / "src/istota/branch.py").write_text(
            "from istota.notifications import send_notification  # noqa: F401\n"
        )
        _commit(repo)
        capsys.readouterr()
        assert run(repo) == 0
        assert read(repo, "docs/pkg.md") == "The `istota.notifications` package.\n"
        # Importing a name the package does not have is unambiguously the old module.
        assert read(repo, "src/istota/branch.py") == (
            "from istota.notifications.delivery import send_notification  # noqa: F401\n"
        )
        assert "hand-fix docs/pkg.md:1: istota.notifications:" in capsys.readouterr().out

    def test_a_logger_name_and_the_keep_marker(self, repo):
        assert run(repo, "--only", "rooms", "web") == 0
        text = read(repo, "tests/test_things.py")
        assert 'LOGGER = "istota.rooms.policy"' in text
        assert 'KEEP = "istota.web_app"  # move-modules: keep' in text

    def test_the_templated_unit_path(self, repo):
        assert run(repo, "--only", "web") == 0
        assert read(repo, "deploy/istota-web.service.j2") == (
            "ExecStart=uvicorn {{ istota_package }}.webui.app:app\n"
        )

    def test_an_entry_point_stub_at_the_old_path_counts_as_done(self, repo):
        assert run(repo, "--only", "web") == 0
        stub = "from istota.webui.app import app  # noqa: F401  (stub for istota.web_app)\n"
        (repo / "src/istota/web_app.py").write_text(stub)
        _commit(repo)
        assert run(repo, "--check", "--only", "web") == 0
        assert run(repo) == 0
        assert read(repo, "src/istota/web_app.py") == stub


class TestPathsAndMarkdown:
    def test_paths_and_backticked_names_are_rewritten_and_bare_names_reported(self, repo, capsys):
        assert run(repo, "--only", "rooms", "pkg") == 0
        assert read(repo, "docs/rooms.md") == (
            "See `rooms/policy.py`, and src/istota/rooms/policy.py.\n"
            "Prose names room_policy.py without backticks.\n"
            "The package `newpkg/sub/` and src/istota/newpkg/sub/inner.py.\n"
        )
        assert 'OWNER = "room_policy.py"' in read(repo, "tests/test_things.py")
        out = capsys.readouterr().out
        assert "hand-fix docs/rooms.md:2: room_policy.py:" in out
        assert "hand-fix tests/test_things.py:4: room_policy.py:" in out

    def test_excluded_paths_are_untouched(self, repo):
        assert run(repo, "--only", "rooms", "notes", "web", "pkg") == 0
        for rel in EXCLUDED:
            assert read(repo, rel) == FILES[rel], rel


class TestRefusals:
    def test_a_dirty_path_to_move_refuses_with_nothing_changed(self, repo):
        (repo / "src/istota/room_policy.py").write_text(FILES["src/istota/room_policy.py"] + "# edit\n")
        before = snapshot(repo)
        assert run(repo, "--only", "rooms") == 2
        assert snapshot(repo) == before

    def test_an_unknown_stage(self, repo):
        assert run(repo, "--only", "nope") == 2

    def test_an_inconsistent_pair(self, repo):
        (repo / "src/istota/rooms").mkdir()
        (repo / "src/istota/rooms/policy.py").write_text("")
        _commit(repo)
        assert run(repo, "--only", "rooms") == 2

    def test_a_partly_moved_stage_needs_naming(self, repo):
        _git(repo, "mv", "src/istota/room_scopes.py", "src/istota/scopes_tmp.py")
        (repo / "src/istota/rooms").mkdir()
        _git(repo, "mv", "src/istota/scopes_tmp.py", "src/istota/rooms/scopes.py")
        _commit(repo)
        assert run(repo) == 2
        assert run(repo, "--only", "rooms") == 0

    def test_a_leftover_package_destination_refuses(self, repo):
        """`git mv` into an existing directory would nest the package inside it."""
        (repo / "src/istota/newpkg/sub/__pycache__").mkdir(parents=True)
        (repo / "src/istota/newpkg/sub/__pycache__/x.pyc").write_bytes(b"")
        assert run(repo, "--only", "pkg") == 2
        assert (repo / "src/istota/old_pkg/inner.py").exists()

    def test_a_gitignored_destination(self, repo):
        (repo / ".gitignore").write_text("rooms/\n")
        _commit(repo)
        before = snapshot(repo)
        assert run(repo, "--only", "rooms") == 2
        assert snapshot(repo) == before


class TestCatchUp:
    def test_no_flags_rewrites_landed_stages_and_moves_nothing_else(self, repo):
        assert run(repo, "--only", "rooms") == 0
        _commit(repo)
        (repo / "src/istota/branch_new.py").write_text("from istota.room_policy import readers  # noqa: F401\n")
        _commit(repo)
        assert run(repo) == 0
        assert read(repo, "src/istota/branch_new.py") == "from istota.rooms.policy import readers  # noqa: F401\n"
        assert (repo / "src/istota/notifications.py").exists()
        assert 'PATCH = "istota.notifications.send_notification"' in read(repo, "tests/test_things.py")


# ---------------------------------------------------------------------------
# The real table, against the real tree.


def _old_names():
    return [old for _, old, _ in mm.MOVES]


def _new_names():
    return [new for _, _, new in mm.MOVES]


class TestTheRealTable:
    def test_every_row_is_in_a_consistent_state(self):
        bad = [
            (old, new) for _, old, new in mm.MOVES
            if mm.pair_state(REPO, mm.DEFAULT_TABLE, old, new) == "inconsistent"
        ]
        assert bad == []

    def test_stages_are_the_declared_ones_in_order(self):
        assert mm.DEFAULT_TABLE.stages == list(mm.STAGES)

    def test_no_duplicates_and_no_chains(self):
        olds, news = _old_names(), _new_names()
        assert len(set(olds)) == len(olds)
        assert len(set(news)) == len(news)
        assert set(olds).isdisjoint(news)

    def test_no_new_path_sits_under_an_old_one_except_a_collision(self):
        olds = set(_old_names())
        offenders = []
        for new in _new_names():
            parts = new.split(".")
            for k in range(2, len(parts)):
                prefix = ".".join(parts[:k])
                if prefix in olds and prefix not in mm.COLLISIONS:
                    offenders.append(new)
        assert offenders == []

    def test_no_old_path_reads_as_a_filename(self):
        filenames = {"istota.db", "istota.env", "istota.log", "istota.toml", "istota.lock", "istota.json"}
        assert filenames.isdisjoint(_old_names())

    def test_the_declared_sets_name_table_rows(self):
        olds = set(_old_names())
        assert mm.PACKAGES <= olds
        assert mm.COLLISIONS <= olds
        assert mm.ENTRY_POINT_STUBS <= olds

    def test_no_collision_child_is_a_name_in_the_old_module(self):
        """`istota.notifications.store` must not have meant an attribute of the old module."""
        for old in mm.COLLISIONS:
            new = dict((o, n) for _, o, n in mm.MOVES)[old]
            path = mm.module_file(REPO, old)
            if not path.is_file():
                path = mm.module_file(REPO, new)
            names = mm._top_level_names(ast.parse(path.read_text()))
            assert names.isdisjoint(mm.DEFAULT_TABLE.children(old)), old

    def test_the_packages_this_creates(self):
        existing = {"istota", "istota.nextcloud", "istota.location", "istota.briefings"}
        created = {new.rsplit(".", 1)[0] for new in _new_names()} - existing - set(_new_names())
        assert created == {
            f"istota.{name}" for name in (
                "lib", "rooms", "relay", "sandbox", "credentials", "devbox",
                "notifications", "usage", "webui", "maintenance", "mail", "browser",
            )
        }

    def test_the_cli_refuses_an_unknown_stage(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check", "--only", "no-such-stage"],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 2, result.stderr
        assert "unknown stage" in result.stderr
