"""The model is shown the proxy's schema and the server runs the real tool.

A second copy of a tool schema is how a model ends up offered a parameter the
executor does not implement — `config_mapper.py`'s header enumerates the three
defect classes a duplicated schema carries in this repo, and "a field the
loader never read" is exactly the shape it would take here.

The extraction to module-level constants is what makes the two ends *the same
object* rather than two equal ones, so most of this file is an identity check.
That is deliberately stronger than equality: `ToolSchema` is a frozen
dataclass, so two independently-written copies could compare equal today and
diverge the moment one of them is edited, and identity is the property that
cannot.
"""

import pytest

from istota.session.tools import ToolEnv, build_default_tools, build_remote_tools
from istota.session.tools import files as files_mod


@pytest.fixture
def real(tmp_path):
    return {t.schema.name: t for t in build_default_tools(ToolEnv(cwd=tmp_path))}


@pytest.fixture
def proxy():
    # `build_remote_tools` never touches the server during construction, so
    # None is enough to build the surface the model is shown.
    return {t.schema.name: t for t in build_remote_tools(None)}


def test_the_same_six_tools_are_offered(real, proxy):
    assert set(proxy) == set(real)
    assert set(proxy) == {"Read", "Write", "Edit", "Grep", "Glob", "Bash"}


def test_a_server_env_carries_no_web_fetch_so_the_six_are_all_of_them(tmp_path):
    """`build_default_tools` appends WebFetch when `env.web_fetch` is set, and
    the server's env never sets it: that tool stays in the daemon, where its
    SSRF hardening and its credential-free client already are."""
    env = ToolEnv(cwd=tmp_path)
    assert env.web_fetch is None
    assert len(build_default_tools(env)) == 6


@pytest.mark.parametrize("name", ["Read", "Write", "Edit", "Grep", "Glob", "Bash"])
def test_the_schema_is_the_same_object_on_both_sides(real, proxy, name):
    assert proxy[name].schema is real[name].schema


@pytest.mark.parametrize("name", ["Read", "Write", "Edit", "Grep", "Glob", "Bash"])
def test_the_execution_mode_matches(real, proxy, name):
    """Not cosmetic: the loop serializes any batch containing a `sequential`
    tool, so a proxy that called Read `sequential` would silently undo parallel
    tool execution, and one that called Write `parallel` would let two
    mutations of the same file race."""
    assert proxy[name].execution_mode == real[name].execution_mode


@pytest.mark.parametrize("name", ["Read", "Write", "Edit", "Grep", "Glob", "Bash"])
def test_the_argument_coercion_hook_matches(real, proxy, name):
    """The half a schema constant does not cover. `prepare_arguments` decides
    what the model's arguments *mean* — Edit's turns a legacy
    old_string/new_string call into a one-element `edits` batch — so two
    different shims would let the ends disagree while every schema assertion
    stayed green."""
    assert proxy[name].prepare_arguments is real[name].prepare_arguments


def test_only_edit_has_a_coercion_hook(proxy):
    """States which tools the assertion above is non-trivial for, so adding a
    hook to a second tool and not to its proxy fails here rather than passing
    as `None is None`."""
    assert [n for n, t in proxy.items() if t.prepare_arguments is not None] == ["Edit"]
    assert proxy["Edit"].prepare_arguments is files_mod.prepare_edit_arguments


def test_edits_coercion_runs_against_the_module_level_schema(tmp_path):
    """`prepare_edit_arguments` lifted out of a closure over the factory's
    `schema`, so this is the assertion that the lift kept its meaning: the
    legacy shape still becomes a one-element batch, and `replace_all` still
    stays on the exact single-edit path."""
    legacy = files_mod.prepare_edit_arguments(
        {"file_path": "/w/x", "old_string": "a", "new_string": "b"}
    )
    assert legacy["edits"] == [{"old_string": "a", "new_string": "b"}]

    exact = files_mod.prepare_edit_arguments(
        {"file_path": "/w/x", "old_string": "a", "new_string": "b", "replace_all": True}
    )
    assert exact.get("edits") is None


def test_the_proxy_table_is_the_only_place_a_tool_could_be_dropped(proxy, real):
    """`build_remote_tools` iterates a hand-written table. A seventh core tool
    added to `build_default_tools` and not to it would leave the model unable
    to call something the server implements, silently."""
    from istota.session.tools import remote

    assert [s.name for s, _ in remote._PROXIED] == list(proxy)
    assert set(s.name for s, _ in remote._PROXIED) == set(real)
