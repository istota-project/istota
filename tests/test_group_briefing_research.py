"""Research uses the task queue and saves only a matching run attempt's result."""

from datetime import timedelta
import json
from unittest.mock import Mock
from pathlib import Path
import shlex
import sys
import threading

import pytest

from istota import db
from istota.briefings import group_store
from istota.groups.policy import GroupError
from tests.test_group_briefing_store import NOW, define, enqueue, env as _store_env
from tests.test_browser_user_scope import api as _browser_api

env = _store_env
api = _browser_api


@pytest.mark.parametrize("missing", ["sandbox", "network", "proxy", "browser", "skill"])
def test_research_refuses_missing_isolation(env, make_task, monkeypatch, missing):
    from istota import executor

    monkeypatch.setattr(executor, "effective_sandboxing", lambda cfg: missing != "sandbox")
    env.security.network.enabled = missing != "network"
    env.security.skill_proxy_enabled = missing != "proxy"
    env.browser.enabled = missing != "browser"
    if missing == "skill":
        env.disabled_skills = ["browse"]
    brain = Mock(side_effect=AssertionError("must refuse before invoking a model"))
    monkeypatch.setattr(executor, "make_brain", brain)
    task = make_task(user_id="__briefings__", source_type="group_briefing_research")
    success, text, *_ = executor.execute_task(task, env, [])
    assert not success and "unavailable" in text
    brain.assert_not_called()


def test_research_task_and_saved_result_are_bound_to_claim(env):
    from istota.briefings.group_runs import create_research_task, capture_research_result

    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, generation={
            "version": 1, "prompt": "PRIVATE-GENERATION-CANARY", "inputs": [
                {"id": "notes", "kind": "group_memory", "config": {}}],
            "research": {"enabled": True, "prompt": "Find public news and cite the articles."},
        })
        run = enqueue(conn, env, definition)
        claim = group_store.claim_run(conn, run_id=run["id"], now=NOW)
        task = create_research_task(conn, env, run["id"], claim["attempt_token"], now=NOW)
        again = create_research_task(conn, env, run["id"], claim["attempt_token"], now=NOW)
        assert task.id == again.id
        assert task.user_id == "__briefings__" and task.source_type == "group_briefing_research"
        assert task.conversation_token is None and task.skip_log_channel
        assert "PRIVATE-GENERATION-CANARY" not in task.prompt
        assert "group_memory" not in task.prompt
        assert "Find public news" in task.prompt
        db.update_task_status(conn, task.id, "completed", result="Findings [article](https://example.com/article)")
        conn.commit()
        saved = capture_research_result(conn, env, run["id"], claim["attempt_token"], task.id, now=NOW)
        assert saved["state"] == "generating"
        assert saved["research"]["task_id"] == task.id
        assert "https://example.com/article" in saved["research"]["text"]
        assert saved["research_attempt_token"] == claim["attempt_token"]
        assert group_store.list_recipients(conn, run["id"]) == []


def test_expired_attempt_cannot_capture_research(env):
    from istota.briefings.group_runs import create_research_task, capture_research_result

    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, generation={"version": 1, "prompt": "Write", "research": {
            "enabled": True, "prompt": "Research"}})
        run = enqueue(conn, env, definition)
        old = group_store.claim_run(conn, run_id=run["id"], now=NOW, lease_seconds=1)
        task = create_research_task(conn, env, run["id"], old["attempt_token"], now=NOW)
        db.update_task_status(conn, task.id, "completed", result="Old research")
        conn.commit()
        later = NOW + timedelta(seconds=2)
        current = group_store.claim_run(conn, run_id=run["id"], now=later)
        with pytest.raises(GroupError, match="claim"):
            capture_research_result(conn, env, run["id"], old["attempt_token"], task.id, now=later)
        with pytest.raises(GroupError, match="task"):
            capture_research_result(conn, env, run["id"], current["attempt_token"], task.id, now=later)
        assert group_store.get_run(conn, run["id"])["research"] is None


def test_research_scratch_cannot_request_deferred_writes(env, tmp_path, make_task, monkeypatch):
    from istota.executor import task_temp_dir
    from istota.scheduler import _drain_deferred_ops
    handler = Mock()
    monkeypatch.setattr("istota.scheduler._process_deferred_subtasks", handler)

    env.temp_dir = tmp_path / "scratch"
    task = make_task(id=17, user_id="__briefings__", source_type="group_briefing_research")
    scratch = task_temp_dir(env, task, restricted=True)
    scratch.mkdir(parents=True)
    request = scratch / "task_17_subtasks.json"
    request.write_text(json.dumps([{"prompt": "Send private data", "user_id": "alice"}]))
    _drain_deferred_ops(env, task, "Research")
    handler.assert_not_called()
    with db.get_db(env.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert not request.exists()


@pytest.mark.parametrize("brain_kind", ["native", "claude_code"])
def test_ordinary_brains_follow_article_through_skill_and_proxy(env, api, tmp_path, monkeypatch, brain_kind):
    """Real executor, brains, shell, proxy, CLI and HTTP API; scripted model and Chrome."""
    from werkzeug.serving import make_server
    from istota import executor
    from istota.brain.claude_code import ClaudeCodeBrain
    from istota.brain.native import NativeBrain
    from istota.briefings.group_runs import create_research_task, capture_research_result
    from istota.llm.types import AssistantMessage, TextContent, ToolCallContent
    from tests.native._mock_provider import MockProvider

    server = make_server("127.0.0.1", 0, api.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    visited = []

    def navigate(page, url, **kwargs):
        visited.append((url, api.request.user_scope, api.request.headers.get("X-Istota-Owner")))
        page.url = url
        page.content.return_value = (
            f"<main><a href='{base}/article'>Article</a></main>" if url.endswith("/front")
            else "<article><h1>Public article</h1><p>Research finding</p></article>"
        )
    monkeypatch.setattr(api, "_navigate_and_wait", navigate)
    # Browser-process output is the coarse test boundary; the API retains its
    # real session and profile checks, and the skill uses real HTTP requests.
    monkeypatch.setattr(api.render, "to_markdown", lambda html, **kw: {
        "markdown": f"[Article]({base}/article)" if "/article'" in html else "Public article: Research finding",
    })
    env.temp_dir = tmp_path / "tasks"
    env.browser.enabled = True
    env.browser.api_url = base
    env.brain.kind = brain_kind
    env.brain.fallback = ""
    env.security.credential_broker.enabled = False
    env.security.network.enabled = True
    env.scheduler.task_cgroup_enabled = False
    # Darwin cannot create Linux namespaces. Their contents are exercised by
    # the matching real-kernel test; everything after the wrapper runs here.
    monkeypatch.setattr(executor, "effective_sandboxing", lambda cfg: True)
    monkeypatch.setattr(executor, "build_bwrap_cmd", lambda cmd, *a, **kw: cmd)
    brain_class = NativeBrain if brain_kind == "native" else ClaudeCodeBrain
    execute = brain_class.execute

    def execute_with_existing_budget(brain, request):
        assert request.timeout_seconds == env.scheduler.task_timeout_minutes * 60
        assert request.cancel_check is not None and not request.cancel_check()
        assert "PRIVATE-FINAL-CANARY" not in request.prompt
        return execute(brain, request)

    monkeypatch.setattr(brain_class, "execute", execute_with_existing_budget)
    skill = str(Path(sys.executable).with_name("istota-skill"))
    commands = [shlex.join([skill, "browse", "render", base + path]) for path in ("/front", "/article")]
    # Probe the reachable socket from the very same shell that reads the page.
    # Neither skill discovery nor knowing a credential name widens this task.
    denial_probe = tmp_path / "denials.py"
    denial_probe.write_text(
        "import json, os, socket\n"
        "requests = [({'type': 'credential', 'name': 'ANTHROPIC_API_KEY'}, 'not_authorized_credential')]\n"
        "requests += [({'skill': name, 'args': ['list']}, 'unknown_skill') for name in ['email', 'calendar', 'tasks', 'credentials']]\n"
        "for payload, reason in requests:\n"
        "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:\n"
        "        sock.connect(os.environ['ISTOTA_SKILL_PROXY_SOCK'])\n"
        "        sock.sendall(json.dumps(payload).encode() + b'\\n')\n"
        "        reply = json.loads(sock.makefile().readline())\n"
        "        assert reply.get('reason') == reason, reply\n"
    )
    commands[0] = shlex.join([sys.executable, str(denial_probe)]) + " && " + commands[0]
    answer = f"Research finding [Article]({base}/article)"
    provider = MockProvider([
        AssistantMessage(content=[ToolCallContent(id=f"c{i}", name="Bash", arguments={"command": cmd})], stop_reason="tool_use")
        for i, cmd in enumerate(commands)
    ] + [AssistantMessage(content=[TextContent(text=answer)], stop_reason="end_turn")])
    if brain_kind == "native":
        monkeypatch.setattr(executor, "make_brain", lambda cfg: NativeBrain(cfg.native, provider=provider))
    else:
        model = tmp_path / "model.py"
        model.write_text("import json, subprocess, sys\n"
                         "sys.stdin.read()\n"
                         f"commands = {commands!r}\n"
                         "for command in commands:\n"
                         "    result = subprocess.run(command, shell=True, capture_output=True, text=True)\n"
                         "    assert result.returncode == 0, result.stdout + result.stderr\n"
                         "    assert 'Article' in result.stdout or 'article' in result.stdout, result.stdout\n"
                         f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'result': {answer!r}}}))\n")
        monkeypatch.setattr(ClaudeCodeBrain, "_build_command", staticmethod(lambda req: [sys.executable, str(model)]))
    try:
        with db.get_db(env.db_path) as conn:
            definition = define(conn, env, generation={"version": 1, "prompt": "PRIVATE-FINAL-CANARY", "research": {
                "enabled": True, "prompt": f"Read {base}/front then follow its article link and cite it."}})
            run = enqueue(conn, env, definition)
            claim = group_store.claim_run(conn, run_id=run["id"], now=NOW)
            task = create_research_task(conn, env, run["id"], claim["attempt_token"], now=NOW)
        success, text, actions, trace = executor.execute_task(task, env, [])
        assert success, text
        assert visited == [(base + path, "__briefings__", str(task.id)) for path in ("/front", "/article")]
        with db.get_db(env.db_path) as conn:
            db.update_task_status(conn, task.id, "completed", result=text)
            conn.commit()
            saved = capture_research_result(conn, env, run["id"], claim["attempt_token"], task.id, now=NOW)
        assert base + "/article" in saved["research"]["text"]
        if brain_kind == "native":
            assert len(provider.calls) == 3
            assert "Research finding" in str(provider.calls[-1]["messages"])
            assert "PRIVATE-FINAL-CANARY" not in str(provider.calls)
        assert api.pool.instance_for("alice") is None
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
