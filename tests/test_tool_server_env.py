"""`hello` → `ToolEnv`, and the proxy merge, with no server and no socket.

Two pure translations that decide what every tool inside the sandbox can see.
The Linux tier measures the consequences; this is where the rules are stated,
because a rule asserted only through a namespace is a rule nobody can read.
"""

from pathlib import Path

from istota.session.tools import ToolEnv, hello_payload
from istota.tool_server import build_env, merge_proxy_env


def _hello(**kw):
    args = dict(
        cwd=Path("/w"),
        subprocess_env=None,
        read_roots=None,
        write_roots=None,
        write_denied_roots=(),
        deferred_dir=None,
        bash_timeout_seconds=120,
        max_output_bytes=30_000,
        max_read_lines=2000,
        max_read_bytes=25_000_000,
        bash_spill_full_output=True,
    )
    args.update(kw)
    return hello_payload(**args)


class TestBuildEnv:
    def test_every_field_crosses(self):
        env = build_env(_hello(
            cwd=Path("/w/alice"),
            subprocess_env={"A": "b"},
            read_roots=(Path("/w/alice"), Path("/mnt/Talk")),
            write_roots=(Path("/w/alice"),),
            write_denied_roots=(Path("/w/alice/.developer"),),
            deferred_dir=Path("/w/alice"),
            bash_timeout_seconds=45,
            max_output_bytes=111,
            max_read_lines=222,
            max_read_bytes=333,
            bash_spill_full_output=False,
        ), process_env={})
        assert env.cwd == Path("/w/alice")
        assert env.subprocess_env == {"A": "b"}
        assert env.read_roots == (Path("/w/alice"), Path("/mnt/Talk"))
        assert env.write_roots == (Path("/w/alice"),)
        assert env.write_denied_roots == (Path("/w/alice/.developer"),)
        assert env.deferred_dir == Path("/w/alice")
        assert env.bash_timeout_seconds == 45
        assert env.max_output_bytes == 111
        assert env.max_read_lines == 222
        assert env.max_read_bytes == 333
        assert env.bash_spill_full_output is False

    def test_none_roots_mean_unconfined_not_an_empty_allowlist(self):
        """JSON has no tuple, so both spellings arrive as a list unless `None`
        is carried as `null`. An unconfined dev machine that started refusing
        every read would look like a broken tool rather than a bad
        translation."""
        env = build_env(_hello(), process_env={})
        assert env.read_roots is None and env.confined is False

    def test_the_server_never_builds_a_web_fetch_tool(self):
        """WebFetch stays in the daemon — it is credential-free, its hardening
        is entirely about the IPs it resolves, and a namespace helps with none
        of that. `build_default_tools` keys on this field, so `None` here is
        what keeps the server at exactly six tools."""
        assert build_env(_hello(), process_env={}).web_fetch is None

    def test_the_caps_the_daemon_sends_are_the_ones_it_would_have_used(self):
        """`hello` is now the only thing that decides these, so a second set of
        numbers written into the frame builder would be a silent second default
        the day one of them changes."""
        defaults = ToolEnv(cwd=Path("/w"))
        env = build_env(_hello(), process_env={})
        assert (env.max_output_bytes, env.max_read_lines, env.max_read_bytes) == (
            defaults.max_output_bytes, defaults.max_read_lines, defaults.max_read_bytes,
        )


class TestMergeProxyEnv:
    def test_the_bridge_variables_are_folded_in(self):
        """The whole reason this function exists: `build_bwrap_cmd`'s network
        wrapper sets these on the *server*, and the `subprocess_env` in the
        frame was built by the daemon, which has no bridge and no port."""
        merged = merge_proxy_env(
            {"PATH": "/usr/bin"},
            {"HTTPS_PROXY": "http://127.0.0.1:8118", "HTTP_PROXY": "http://127.0.0.1:8118"},
        )
        assert merged["PATH"] == "/usr/bin"
        assert merged["HTTPS_PROXY"] == "http://127.0.0.1:8118"

    def test_an_empty_no_proxy_is_carried_rather_than_dropped(self):
        """The wrapper sets `NO_PROXY=` to *blank* an inherited exemption list.
        A truthiness test would drop it and silently re-open whatever the
        daemon's own value exempts — which is the one thing an operator setting
        it could not then see."""
        merged = merge_proxy_env({"PATH": "/usr/bin"}, {"NO_PROXY": ""})
        assert "NO_PROXY" in merged and merged["NO_PROXY"] == ""

    def test_the_daemons_value_wins_a_collision(self):
        """A deployment that put `HTTPS_PROXY` in `passthrough_env_vars` meant
        that one; the bridge's values go *under* the daemon's, not over."""
        merged = merge_proxy_env(
            {"HTTPS_PROXY": "http://corp:3128"},
            {"HTTPS_PROXY": "http://127.0.0.1:8118", "NO_PROXY": ""},
        )
        assert merged["HTTPS_PROXY"] == "http://corp:3128"
        assert merged["NO_PROXY"] == ""

    def test_nothing_to_merge_leaves_the_value_alone(self):
        """`None` means "inherit the parent environment" to `ToolEnv`, and on a
        deployment with no network proxy that is already the right answer.
        Returning a dict here would narrow what a direct caller's Bash children
        inherit, for no gain."""
        assert merge_proxy_env(None, {}) is None
        assert merge_proxy_env({"A": "b"}, {}) == {"A": "b"}

    def test_none_plus_a_bridge_becomes_the_whole_process_environment(self):
        """The one case where `None` cannot survive: the children must get the
        proxy, and the only way to add a key to "inherit everything" is to
        materialize everything."""
        merged = merge_proxy_env(None, {"PATH": "/usr/bin", "HTTPS_PROXY": "http://p:1"})
        assert merged == {"PATH": "/usr/bin", "HTTPS_PROXY": "http://p:1"}

    def test_only_the_three_are_taken(self):
        """Not a general environment copy: the server's own environment is the
        task env plus whatever bwrap's wrapper added, and anything else it
        picked up on the way in is not something to hand the model."""
        merged = merge_proxy_env(
            {"PATH": "/usr/bin"},
            {"HTTPS_PROXY": "http://p:1", "SECRET_TOKEN": "nope"},
        )
        assert "SECRET_TOKEN" not in merged
