"""Tests for the kv skill's argv-ceiling and read-cost ergonomics (ISSUE-239).

Four additive surfaces, none of which changes the table shape or the
``(user_id, namespace, key) -> JSON text`` value model:

* batched ``set-contains`` — one spawn and one parse per run, not per member;
* bounded ``list`` output — ``--keys-only`` and per-value truncation;
* ``set --value-file`` — a non-argv write path past ``MAX_ARG_STRLEN``;
* ``set-trim`` — count-bounded collections.
"""

import json
import os

import pytest

from istota import db
from istota.skills.kv import main as kv_main


def _env(monkeypatch, db_path, *, deferred=None, task_id=None):
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    if deferred is None:
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
    else:
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    if task_id is None:
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    else:
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
    monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)


def _seed(db_path, key, value):
    with db.get_db(db_path) as conn:
        db.kv_set(conn, "alice", "ns", key, json.dumps(value))


# ============================================================================
# 1. Batched set-contains
# ============================================================================


class TestSetContainsBatch:
    def test_single_member_keeps_scalar_shape(self, db_path, capsys, monkeypatch):
        """`contains` stays the scalar prompts already read; `batched` is what
        tells a variable-length caller which shape it got."""
        _seed(db_path, "seen", ["a", "b"])
        _env(monkeypatch, db_path)
        kv_main(["set-contains", "ns", "seen", "a"])
        out = json.loads(capsys.readouterr().out)
        assert out["contains"] is True
        assert out["batched"] is False

    def test_batched_flag_discriminates_the_two_shapes(self, db_path, capsys, monkeypatch):
        """The boundary case a variable-length caller hits: a batch of one."""
        _seed(db_path, "seen", ["a"])
        _env(monkeypatch, db_path)
        for members, batched in ([["a"], False], [["a", "b"], True]):
            kv_main(["set-contains", "ns", "seen", *members])
            out = json.loads(capsys.readouterr().out)
            assert out["batched"] is batched
            assert isinstance(out["contains"], dict) is batched

    def test_non_string_members_error_cleanly(self, db_path, capsys, monkeypatch):
        """`kv set` will store any JSON array; an unhashable member must not
        escape as a raw TypeError traceback."""
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "objs", json.dumps([{"a": 1}]))
        _env(monkeypatch, db_path)
        with pytest.raises(SystemExit):
            kv_main(["set-contains", "ns", "objs", "a", "b"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "non-string members" in out["error"]

    def test_multiple_members_return_map(self, db_path, capsys, monkeypatch):
        _seed(db_path, "seen", ["a", "b"])
        _env(monkeypatch, db_path)
        kv_main(["set-contains", "ns", "seen", "a", "z", "b"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["contains"] == {"a": True, "z": False, "b": True}

    def test_map_reports_present_and_missing_counts(self, db_path, capsys, monkeypatch):
        _seed(db_path, "seen", ["a"])
        _env(monkeypatch, db_path)
        kv_main(["set-contains", "ns", "seen", "a", "y", "z"])
        out = json.loads(capsys.readouterr().out)
        assert out["present"] == 1
        assert out["missing"] == 2

    def test_duplicate_members_collapse(self, db_path, capsys, monkeypatch):
        _seed(db_path, "seen", ["a"])
        _env(monkeypatch, db_path)
        kv_main(["set-contains", "ns", "seen", "a", "a", "z"])
        out = json.loads(capsys.readouterr().out)
        assert out["contains"] == {"a": True, "z": False}
        # Counts are over distinct members, matching the map.
        assert out["present"] == 1
        assert out["missing"] == 1

    def test_missing_key_reports_all_false(self, db_path, capsys, monkeypatch):
        _env(monkeypatch, db_path)
        kv_main(["set-contains", "ns", "nope", "a", "b"])
        out = json.loads(capsys.readouterr().out)
        assert out["contains"] == {"a": False, "b": False}

    def test_non_array_value_still_errors(self, db_path, capsys, monkeypatch):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "scalar", '"hello"')
        _env(monkeypatch, db_path)
        with pytest.raises(SystemExit):
            kv_main(["set-contains", "ns", "scalar", "a", "b"])
        assert json.loads(capsys.readouterr().out)["status"] == "error"


# ============================================================================
# 2. Bounded `list` output
# ============================================================================


class TestListBounding:
    def test_long_value_truncated_by_default(self, db_path, capsys, monkeypatch):
        members = [f"id-{i:06d}" for i in range(2000)]
        _seed(db_path, "big", members)
        _env(monkeypatch, db_path)
        kv_main(["list", "ns"])
        out = json.loads(capsys.readouterr().out)
        entry = out["entries"][0]
        assert entry["truncated"] is True
        assert isinstance(entry["value"], str)
        assert len(entry["value"]) <= 2048
        # The full size is still reported, so the caller can see what it cost.
        assert entry["value_chars"] == len(json.dumps(members))
        assert out["truncated_count"] == 1

    def test_short_value_untouched(self, db_path, capsys, monkeypatch):
        _seed(db_path, "small", {"count": 42})
        _env(monkeypatch, db_path)
        kv_main(["list", "ns"])
        out = json.loads(capsys.readouterr().out)
        entry = out["entries"][0]
        assert entry["value"] == {"count": 42}
        assert "truncated" not in entry
        assert out["truncated_count"] == 0

    def test_max_value_chars_zero_disables_truncation(self, db_path, capsys, monkeypatch):
        members = [f"id-{i:06d}" for i in range(2000)]
        _seed(db_path, "big", members)
        _env(monkeypatch, db_path)
        kv_main(["list", "ns", "--max-value-chars", "0"])
        out = json.loads(capsys.readouterr().out)
        assert out["entries"][0]["value"] == members
        assert out["truncated_count"] == 0

    def test_custom_max_value_chars(self, db_path, capsys, monkeypatch):
        _seed(db_path, "mid", ["x" * 200])
        _env(monkeypatch, db_path)
        kv_main(["list", "ns", "--max-value-chars", "50"])
        out = json.loads(capsys.readouterr().out)
        assert out["entries"][0]["truncated"] is True
        assert len(out["entries"][0]["value"]) <= 50

    def test_negative_max_value_chars_rejected(self, db_path, capsys, monkeypatch):
        _env(monkeypatch, db_path)
        with pytest.raises(SystemExit):
            kv_main(["list", "ns", "--max-value-chars", "-1"])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_keys_only_omits_values(self, db_path, capsys, monkeypatch):
        _seed(db_path, "a", ["x"] * 500)
        _seed(db_path, "b", {"y": 1})
        _env(monkeypatch, db_path)
        kv_main(["list", "ns", "--keys-only"])
        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 2
        assert sorted(e["key"] for e in out["entries"]) == ["a", "b"]
        for entry in out["entries"]:
            assert "value" not in entry
            # Size still reported — that's the point of orienting with `list`.
            assert entry["value_chars"] > 0
        assert out["truncated_count"] == 0

    def test_keys_only_wins_over_max_value_chars(self, db_path, capsys, monkeypatch):
        _seed(db_path, "a", ["x"] * 500)
        _env(monkeypatch, db_path)
        kv_main(["list", "ns", "--keys-only", "--max-value-chars", "0"])
        out = json.loads(capsys.readouterr().out)
        assert "value" not in out["entries"][0]

    def test_non_json_value_truncates_as_raw_text(self, db_path, capsys, monkeypatch):
        """A value that isn't valid JSON is passed through today; keep that,
        but bound it like any other."""
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "raw", "not json " * 500)
        _env(monkeypatch, db_path)
        kv_main(["list", "ns"])
        out = json.loads(capsys.readouterr().out)
        entry = out["entries"][0]
        assert entry["truncated"] is True
        assert len(entry["value"]) <= 2048

    def test_get_is_never_truncated(self, db_path, capsys, monkeypatch):
        """`get` is an explicit request for content and stays whole."""
        members = [f"id-{i:06d}" for i in range(2000)]
        _seed(db_path, "big", members)
        _env(monkeypatch, db_path)
        kv_main(["get", "ns", "big"])
        out = json.loads(capsys.readouterr().out)
        assert out["value"] == members


# ============================================================================
# 3. set --value-file
# ============================================================================


class TestSetValueFile:
    def test_direct_write_from_file(self, db_path, db_conn, tmp_path, capsys, monkeypatch):
        payload = tmp_path / "big.json"
        members = [f"id-{i}" for i in range(5000)]
        payload.write_text(json.dumps(members))
        # Deferred dir set (it is the allowed root) but no task id, so the write
        # takes the direct path.
        _env(monkeypatch, db_path, deferred=tmp_path)
        kv_main(["set", "ns", "big", "--value-file", str(payload)])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert json.loads(db.kv_get(db_conn, "alice", "ns", "big")["value"]) == members

    def test_deferred_write_carries_file_contents(self, db_path, tmp_path, capsys, monkeypatch):
        payload = tmp_path / "v.json"
        payload.write_text('{"x": 1}')
        _env(monkeypatch, db_path, deferred=tmp_path, task_id=42)
        kv_main(["set", "ns", "k", "--value-file", str(payload)])
        assert json.loads(capsys.readouterr().out)["deferred"] is True
        ops = json.loads((tmp_path / "task_42_kv_ops.json").read_text())
        assert ops[0]["value"] == '{"x": 1}'

    def test_invalid_json_in_file_rejected(self, db_path, tmp_path, capsys, monkeypatch):
        payload = tmp_path / "bad.json"
        payload.write_text("not json")
        _env(monkeypatch, db_path, deferred=tmp_path)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(payload)])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_path_outside_allowed_roots_refused(self, db_path, tmp_path, capsys, monkeypatch):
        """The CLI runs host-side with the daemon's filesystem view. Without
        this scoping, --value-file is an arbitrary host-file read whose result
        the model can fetch straight back with `kv get`."""
        outside = tmp_path.parent / "outside.json"
        outside.write_text('"secret"')
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        _env(monkeypatch, db_path, deferred=allowed)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(outside)])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "outside allowed roots" in out["error"]

    def test_symlink_refused(self, db_path, tmp_path, capsys, monkeypatch):
        real = tmp_path.parent / "real.json"
        real.write_text('"secret"')
        link = tmp_path / "link.json"
        link.symlink_to(real)
        _env(monkeypatch, db_path, deferred=tmp_path)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(link)])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "symlink" in out["error"]

    def test_reads_the_resolved_path_not_the_argument(
        self, db_path, db_conn, tmp_path, capsys, monkeypatch,
    ):
        """Validating one path and reopening the original re-walks its
        symlinks. Feed it a path that resolves inside the root by a different
        route and confirm the approved target is what was stored."""
        allowed = tmp_path / "allowed"
        (allowed / "sub").mkdir(parents=True)
        target = allowed / "real.json"
        target.write_text('"approved"')
        _env(monkeypatch, db_path, deferred=allowed)
        kv_main([
            "set", "ns", "k",
            "--value-file", str(allowed / "sub" / ".." / "real.json"),
        ])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        assert db.kv_get(db_conn, "alice", "ns", "k")["value"] == '"approved"'

    def test_no_roots_configured_refuses(self, db_path, tmp_path, capsys, monkeypatch):
        payload = tmp_path / "v.json"
        payload.write_text('"x"')
        _env(monkeypatch, db_path)  # neither deferred dir nor mount path
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(payload)])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_own_workspace_under_the_mount_is_allowed(
        self, db_path, db_conn, tmp_path, capsys, monkeypatch,
    ):
        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        payload = mount / "Users" / "alice" / "v.json"
        payload.write_text('"from-workspace"')
        _env(monkeypatch, db_path)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        kv_main(["set", "ns", "k", "--value-file", str(payload)])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        assert db.kv_get(db_conn, "alice", "ns", "k")["value"] == '"from-workspace"'

    def test_another_users_workspace_is_refused(self, db_path, tmp_path, capsys, monkeypatch):
        """NEXTCLOUD_MOUNT_PATH is the shared mount root for every user, and a
        host-side read does not self-scope by ISTOTA_USER_ID the way the SQL
        does — so taking the mount whole would hand back bob's files."""
        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        (mount / "Users" / "bob").mkdir(parents=True)
        victim = mount / "Users" / "bob" / "private.json"
        victim.write_text('{"bobs": "private notes"}')
        _env(monkeypatch, db_path)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "loot", "--value-file", str(victim)])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "outside allowed roots" in out["error"]

    def test_over_cap_file_refused(self, db_path, tmp_path, capsys, monkeypatch):
        from istota.skills.kv import MAX_VALUE_FILE_BYTES
        payload = tmp_path / "huge.json"
        payload.write_text('"' + "x" * (MAX_VALUE_FILE_BYTES + 8) + '"')
        _env(monkeypatch, db_path, deferred=tmp_path)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(payload)])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "cap" in out["error"]

    def test_missing_file_reports_cleanly(self, db_path, tmp_path, capsys, monkeypatch):
        _env(monkeypatch, db_path, deferred=tmp_path)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(tmp_path / "nope.json")])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_non_regular_file_refused(self, db_path, tmp_path, capsys, monkeypatch):
        """A directory or a fifo would otherwise hang or raise deep in read_text."""
        d = tmp_path / "adir"
        d.mkdir()
        _env(monkeypatch, db_path, deferred=tmp_path)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(d)])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_both_value_and_value_file_rejected(self, db_path, tmp_path, capsys, monkeypatch):
        payload = tmp_path / "v.json"
        payload.write_text('"x"')
        _env(monkeypatch, db_path, deferred=tmp_path)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", '"y"', "--value-file", str(payload)])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_neither_value_nor_value_file_rejected(self, db_path, capsys, monkeypatch):
        _env(monkeypatch, db_path)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k"])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_positional_value_still_works(self, db_path, db_conn, capsys, monkeypatch):
        _env(monkeypatch, db_path)
        kv_main(["set", "ns", "k", '"hello"'])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        assert db.kv_get(db_conn, "alice", "ns", "k")["value"] == '"hello"'

    def test_shared_write_from_file_still_gated(self, db_path, tmp_path, capsys, monkeypatch):
        payload = tmp_path / "v.json"
        payload.write_text('"x"')
        _env(monkeypatch, db_path, deferred=tmp_path)
        monkeypatch.delenv("ISTOTA_CONFIG_PATH", raising=False)
        with pytest.raises(SystemExit):
            kv_main(["set", "ns", "k", "--value-file", str(payload), "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "admin" in out["error"]


# ============================================================================
# 4. set-trim
# ============================================================================


class TestSetTrimDirect:
    def test_keeps_newest_dropping_from_the_front(self, db_path, db_conn, capsys, monkeypatch):
        _seed(db_path, "seen", ["a", "b", "c", "d", "e"])
        _env(monkeypatch, db_path)
        kv_main(["set-trim", "ns", "seen", "--keep-newest", "2"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["removed"] == 3
        assert out["size"] == 2
        assert json.loads(db.kv_get(db_conn, "alice", "ns", "seen")["value"]) == ["d", "e"]

    @pytest.mark.parametrize("keep", [3, 4, 5, 6, 10])
    def test_keep_larger_than_size_is_a_noop(self, keep, db_path, db_conn, capsys, monkeypatch):
        """`len < keep < 2*len` is the window a `len(current) - keep` slice gets
        wrong — the start index goes negative and clamps, dropping members. A
        single oversized `keep` (10 against 2) misses it entirely."""
        _seed(db_path, "seen", ["a", "b", "c"])
        _env(monkeypatch, db_path)
        kv_main(["set-trim", "ns", "seen", "--keep-newest", str(keep)])
        out = json.loads(capsys.readouterr().out)
        assert out["removed"] == 0
        assert out["size"] == 3
        assert json.loads(db.kv_get(db_conn, "alice", "ns", "seen")["value"]) == ["a", "b", "c"]

    @pytest.mark.parametrize(
        "keep,expected",
        [(1, ["e"]), (2, ["d", "e"]), (3, ["c", "d", "e"]), (4, ["b", "c", "d", "e"])],
    )
    def test_keeps_exactly_the_tail(self, keep, expected, db_path, db_conn, capsys, monkeypatch):
        _seed(db_path, "seen", ["a", "b", "c", "d", "e"])
        _env(monkeypatch, db_path)
        kv_main(["set-trim", "ns", "seen", "--keep-newest", str(keep)])
        out = json.loads(capsys.readouterr().out)
        assert out["size"] == len(expected)
        assert json.loads(db.kv_get(db_conn, "alice", "ns", "seen")["value"]) == expected

    def test_keep_zero_empties_the_array(self, db_path, db_conn, capsys, monkeypatch):
        _seed(db_path, "seen", ["a", "b"])
        _env(monkeypatch, db_path)
        kv_main(["set-trim", "ns", "seen", "--keep-newest", "0"])
        out = json.loads(capsys.readouterr().out)
        assert out["removed"] == 2
        assert json.loads(db.kv_get(db_conn, "alice", "ns", "seen")["value"]) == []

    def test_negative_keep_rejected(self, db_path, capsys, monkeypatch):
        _seed(db_path, "seen", ["a"])
        _env(monkeypatch, db_path)
        with pytest.raises(SystemExit):
            kv_main(["set-trim", "ns", "seen", "--keep-newest", "-1"])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_missing_key_is_a_noop_and_does_not_create_it(self, db_path, db_conn, capsys, monkeypatch):
        _env(monkeypatch, db_path)
        kv_main(["set-trim", "ns", "nope", "--keep-newest", "5"])
        out = json.loads(capsys.readouterr().out)
        assert out["removed"] == 0
        assert db.kv_get(db_conn, "alice", "ns", "nope") is None

    def test_non_array_errors(self, db_path, capsys, monkeypatch):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "scalar", '"hello"')
        _env(monkeypatch, db_path)
        with pytest.raises(SystemExit):
            kv_main(["set-trim", "ns", "scalar", "--keep-newest", "1"])
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_shared_rejected(self, db_path, capsys, monkeypatch):
        _env(monkeypatch, db_path)
        with pytest.raises(SystemExit):
            kv_main(["set-trim", "ns", "seen", "--keep-newest", "1", "--shared"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "set-ops" in out["error"]


class TestSetTrimDeferred:
    def test_writes_deferred_op(self, db_path, tmp_path, capsys, monkeypatch):
        _seed(db_path, "seen", ["a", "b", "c"])
        _env(monkeypatch, db_path, deferred=tmp_path, task_id=42)
        kv_main(["set-trim", "ns", "seen", "--keep-newest", "1"])
        out = json.loads(capsys.readouterr().out)
        assert out["deferred"] is True
        assert out["removed"] == 2
        ops = json.loads((tmp_path / "task_42_kv_ops.json").read_text())
        assert ops[0] == {
            "op": "set-trim",
            "namespace": "ns",
            "key": "seen",
            "keep_newest": 1,
        }

    def test_queues_even_when_the_key_does_not_exist_yet(
        self, db_path, tmp_path, capsys, monkeypatch,
    ):
        """The create-then-cap run the docs recommend: a set-add queued in the
        same task creates the key at apply time, so the trim must be queued too
        rather than short-circuited against the read-time view."""
        _env(monkeypatch, db_path, deferred=tmp_path, task_id=42)
        kv_main(["set-add", "ns", "seen", "a", "b", "c", "d"])
        capsys.readouterr()
        kv_main(["set-trim", "ns", "seen", "--keep-newest", "2"])
        assert json.loads(capsys.readouterr().out)["deferred"] is True
        ops = json.loads((tmp_path / "task_42_kv_ops.json").read_text())
        assert [o["op"] for o in ops] == ["set-add", "set-trim"]

    def test_create_then_cap_composes_end_to_end(self, db_path, tmp_path, capsys, monkeypatch):
        from istota.config import (
            Config, EmailConfig, NextcloudConfig, SchedulerConfig, TalkConfig,
        )
        from istota.scheduler import _process_deferred_kv_ops

        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id="alice")
            task = db.get_task(conn, task_id)

        _env(monkeypatch, db_path, deferred=user_temp, task_id=task_id)
        kv_main(["set-add", "ns", "seen", "a", "b", "c", "d"])
        capsys.readouterr()
        kv_main(["set-trim", "ns", "seen", "--keep-newest", "2"])
        capsys.readouterr()

        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        config = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com", username="istota", app_password="secret",
            ),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )
        assert _process_deferred_kv_ops(config, task, user_temp) == 2
        with db.get_db(db_path) as conn:
            assert json.loads(
                db.kv_get(conn, "alice", "ns", "seen")["value"],
            ) == ["c", "d"]


class TestProcessDeferredSetTrim:
    def _make_config(self, db_path, tmp_path):
        from istota.config import (
            Config, EmailConfig, NextcloudConfig, SchedulerConfig, TalkConfig,
        )
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com", username="istota", app_password="secret",
            ),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(),
            nextcloud_mount_path=mount,
            temp_dir=tmp_path / "temp",
        )

    def _run(self, db_path, tmp_path, ops):
        from istota.scheduler import _process_deferred_kv_ops
        config = self._make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id="alice")
            task = db.get_task(conn, task_id)
        (user_temp / f"task_{task_id}_kv_ops.json").write_text(json.dumps(ops))
        return _process_deferred_kv_ops(config, task, user_temp)

    @pytest.mark.parametrize("keep", [4, 5, 6])
    def test_keep_larger_than_size_is_a_noop_on_replay(self, keep, db_path, tmp_path):
        """Same clamping window as the direct path, replayed."""
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "seen", json.dumps(["a", "b", "c"]))
        self._run(db_path, tmp_path, [
            {"op": "set-trim", "namespace": "ns", "key": "seen", "keep_newest": keep},
        ])
        with db.get_db(db_path) as conn:
            assert json.loads(
                db.kv_get(conn, "alice", "ns", "seen")["value"],
            ) == ["a", "b", "c"]

    def test_applies_trim_against_the_fresh_value(self, db_path, tmp_path):
        """The re-read is the point: the trim composes with set-adds queued
        earlier in the same task rather than against the read-time view."""
        count = self._run(db_path, tmp_path, [
            {"op": "set-add", "namespace": "ns", "key": "seen",
             "members": ["a", "b", "c", "d"]},
            {"op": "set-trim", "namespace": "ns", "key": "seen", "keep_newest": 2},
        ])
        assert count == 2
        with db.get_db(db_path) as conn:
            assert json.loads(db.kv_get(conn, "alice", "ns", "seen")["value"]) == ["c", "d"]

    def test_skips_missing_key_without_creating_it(self, db_path, tmp_path):
        count = self._run(db_path, tmp_path, [
            {"op": "set-trim", "namespace": "ns", "key": "nope", "keep_newest": 2},
        ])
        assert count == 0
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "nope") is None

    def test_skips_non_array_value(self, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "scalar", '"hello"')
        self._run(db_path, tmp_path, [
            {"op": "set-trim", "namespace": "ns", "key": "scalar", "keep_newest": 1},
        ])
        with db.get_db(db_path) as conn:
            assert db.kv_get(conn, "alice", "ns", "scalar")["value"] == '"hello"'

    def test_skips_bad_keep_newest(self, db_path, tmp_path):
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", "ns", "seen", json.dumps(["a", "b"]))
        for bad in (-1, "two", None, True):
            self._run(db_path, tmp_path, [
                {"op": "set-trim", "namespace": "ns", "key": "seen", "keep_newest": bad},
            ])
        with db.get_db(db_path) as conn:
            assert json.loads(db.kv_get(conn, "alice", "ns", "seen")["value"]) == ["a", "b"]


# ============================================================================
# Operator CLI (`istota kv list`)
# ============================================================================


class TestOperatorCliList:
    def _args(self, db_path, **kw):
        import argparse
        ns = argparse.Namespace(
            namespace="ns", user="alice", shared=False,
            keys_only=False, max_value_chars=0, config=None,
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        ns.db_path = db_path
        return ns

    def test_full_values_by_default(self, db_path, capsys, monkeypatch):
        from istota import cli
        members = [f"id-{i:06d}" for i in range(2000)]
        _seed(db_path, "big", members)
        monkeypatch.setattr(cli, "_get_kv_conn", lambda args: db.get_db(db_path))
        cli.cmd_kv_list(self._args(db_path))
        out = json.loads(capsys.readouterr().out)
        assert out["entries"][0]["value"] == members

    def test_keys_only(self, db_path, capsys, monkeypatch):
        from istota import cli
        _seed(db_path, "big", ["x"] * 500)
        monkeypatch.setattr(cli, "_get_kv_conn", lambda args: db.get_db(db_path))
        cli.cmd_kv_list(self._args(db_path, keys_only=True))
        out = json.loads(capsys.readouterr().out)
        assert "value" not in out["entries"][0]
        assert out["entries"][0]["value_chars"] > 0

    def test_opt_in_truncation(self, db_path, capsys, monkeypatch):
        from istota import cli
        _seed(db_path, "big", ["x"] * 500)
        monkeypatch.setattr(cli, "_get_kv_conn", lambda args: db.get_db(db_path))
        cli.cmd_kv_list(self._args(db_path, max_value_chars=100))
        out = json.loads(capsys.readouterr().out)
        assert out["entries"][0]["truncated"] is True
        assert len(out["entries"][0]["value"]) <= 100

    def test_operator_set_from_file_needs_no_allowlist(
        self, db_path, db_conn, tmp_path, capsys, monkeypatch,
    ):
        """The operator runs in their own shell with no task context, so the
        skill CLI's host-root allowlist would refuse every path. This surface
        reads as the operator instead."""
        from istota import cli
        payload = tmp_path / "big.json"
        payload.write_text(json.dumps([f"id-{i}" for i in range(3)]))
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.setattr(cli, "_get_kv_conn", lambda args: db.get_db(db_path))
        import argparse
        args = argparse.Namespace(
            namespace="ns", key="big", value=None, value_file=str(payload),
            user="alice", shared=False, config=None,
        )
        cli.cmd_kv_set(args)
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        assert json.loads(
            db.kv_get(db_conn, "alice", "ns", "big")["value"],
        ) == ["id-0", "id-1", "id-2"]

    def test_operator_set_rejects_both_value_and_file(self, db_path, tmp_path, capsys):
        from istota import cli
        import argparse
        payload = tmp_path / "v.json"
        payload.write_text('"x"')
        args = argparse.Namespace(
            namespace="ns", key="k", value='"y"', value_file=str(payload),
            user="alice", shared=False, config=None,
        )
        with pytest.raises(SystemExit):
            cli.cmd_kv_set(args)
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_parser_wires_the_new_flags(self, capsys, monkeypatch):
        """The operator parser is built inside main(); --help exits before any
        config is loaded, so it's the cheap way to prove the wiring."""
        import sys as _sys
        from istota import cli
        monkeypatch.setattr(_sys, "argv", ["istota", "kv", "list", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        help_text = capsys.readouterr().out
        assert "--keys-only" in help_text
        assert "--max-value-chars" in help_text


# ============================================================================
# The documented ceiling
# ============================================================================


class TestDocumentedCeiling:
    def test_skill_doc_names_the_real_mechanism(self):
        from pathlib import Path
        doc = Path(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ) / "src" / "istota" / "skills" / "kv" / "skill.md"
        text = doc.read_text(encoding="utf-8")
        # The 40 KB anecdote had the wrong number and no mechanism.
        assert "40 KB" not in text
        assert "128 KiB" in text
        assert "--value-file" in text
        assert "set-trim" in text
