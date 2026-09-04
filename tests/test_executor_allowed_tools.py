"""`build_allowed_tools` scopes `WebFetch` by admin status.

A `claude_code` or `tmux_claude` task runs behind `--unshare-net` plus the
CONNECT allowlist, so its only egress is what the operator listed. The native
brain's `WebFetch` runs in the daemon's own network namespace, outside that
allowlist, and `NativeBrain._build_tools` filters its in-process tool surface by
exactly the list this function returns. So the list is where a non-admin's
egress is decided on the native path, and it is scoped here rather than at the
brain: the same rule then covers a native-default deployment as well as a room
that pinned native for itself.

The CLI brains are unaffected either way — they run with
`--dangerously-skip-permissions` and never receive this list as an allowlist.
"""

from pathlib import Path

from istota.executor import build_allowed_tools


class TestWebFetchIsAdminScoped:
    def test_non_admin_has_no_webfetch(self):
        assert "WebFetch" not in build_allowed_tools(is_admin=False, skill_names=[])

    def test_admin_keeps_webfetch(self):
        assert "WebFetch" in build_allowed_tools(is_admin=True, skill_names=[])

    def test_websearch_is_not_scoped(self):
        """The two web tools are not the same boundary.

        `WebSearch` runs server-side at the provider and returns titles and URLs
        rather than page bodies, so it grants no egress from this host. Asserted
        so a later reader does not "tidy" the two into one branch.
        """
        for is_admin in (True, False):
            assert "WebSearch" in build_allowed_tools(
                is_admin=is_admin, skill_names=[],
            )

    def test_everything_else_is_unchanged(self):
        """Only `WebFetch` differs between the two lists."""
        admin = build_allowed_tools(is_admin=True, skill_names=[])
        non_admin = build_allowed_tools(is_admin=False, skill_names=[])
        assert set(admin) - set(non_admin) == {"WebFetch"}
        assert set(non_admin) - set(admin) == set()

    def test_skill_names_still_do_not_change_the_list(self):
        base = build_allowed_tools(is_admin=False, skill_names=[])
        assert base == build_allowed_tools(
            is_admin=False, skill_names=["developer", "browse"],
        )


class TestTheNativeToolSurfaceFollows:
    """The scoping is only worth anything because the native brain reads it.

    `build_allowed_tools` returning a shorter list is a fact about a list; what
    makes it a boundary is `_build_tools` filtering on it. Both directions, so
    the assertion cannot pass against a brain that never builds the tool.
    """

    def _brain_and_request(self, is_admin: bool):
        from istota.brain._types import BrainRequest
        from istota.brain.native import NativeBrain
        from istota.config import NativeBrainConfig

        brain = NativeBrain(NativeBrainConfig(model="m"), provider=object())
        req = BrainRequest(
            prompt="hi",
            allowed_tools=build_allowed_tools(is_admin=is_admin, skill_names=[]),
            cwd=Path("/tmp"),
            env={},
            timeout_seconds=30,
        )
        return brain, req

    def test_non_admin_native_request_has_no_webfetch_tool(self):
        brain, req = self._brain_and_request(is_admin=False)
        assert "WebFetch" not in [t.schema.name for t in brain._build_tools(req)]

    def test_admin_native_request_has_the_webfetch_tool(self):
        brain, req = self._brain_and_request(is_admin=True)
        assert "WebFetch" in [t.schema.name for t in brain._build_tools(req)]
