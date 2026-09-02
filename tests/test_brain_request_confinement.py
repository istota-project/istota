"""What a daemon-side ``BrainRequest`` may hand the model (ISSUE-395).

Six modules build a ``BrainRequest`` directly rather than going through
``execute_task``, so none of them gets the env and the confinement roots that
path assembles. Three of them — the OCR extractors — also grant a ``Read``
tool. Together that handed the model a host-side read over the whole
filesystem, as the daemon user, with the daemon's whole environment in reach.

Two behavioural properties are asserted here. The environment one covers all
six builders; the roots one covers the three that grant a tool, since the other
three grant none and have nothing to confine.

The AST guards are what keep the *class* closed rather than the six instances.
They walk the source for ``BrainRequest(...)`` construction sites and require,
of each one, that ``env=`` is not the daemon's own environment and that a site
granting a file tool also passes ``fs_read_roots``. A seventh builder that
reproduces either half of ISSUE-395 fails here rather than shipping.

What the guards do **not** catch, stated so nobody reads more into a green run:
they match literal expressions at the call site only. ``e = dict(os.environ)``
on one line and ``env=e`` on the next passes, as does any ``env=`` whose value
is computed in a helper. Chasing indirection needs dataflow the rest of this
file does not justify; the behavioural tests are what cover the shipped sites.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from istota.brain import BrainRequest, BrainResult
from istota.session.tools.env import ToolEnv, ToolPathError

SRC = Path(__file__).resolve().parents[1] / "src" / "istota"

#: Names that must never reach a model subprocess. Each is a real key the
#: daemon carries: the master Fernet key, the Nextcloud app password, a mail
#: password and a forge token.
DAEMON_CREDENTIALS = {
    "ISTOTA_SECRET_KEY": "fernet-key-placeholder",
    "NC_PASS": "nextcloud-app-password-placeholder",
    "EMAIL_PASSWORD": "mail-password-placeholder",
    "GITLAB_TOKEN": "forge-token-placeholder",
}


class _CapturingBrain:
    """Stands in for whatever ``make_brain`` would have returned."""

    def __init__(self, captured: list[BrainRequest]) -> None:
        self._captured = captured

    def resolve_model_name(self, _role: str) -> str:
        return "test-model"

    def execute(self, req: BrainRequest) -> BrainResult:
        self._captured.append(req)
        return BrainResult(success=True, result_text="[]")


@pytest.fixture
def capture_request(monkeypatch):
    """Capture the ``BrainRequest`` an OCR module hands its brain.

    ``_call_brain`` imports ``make_brain`` from ``istota.brain`` at call time
    and ``persist_brain_usage`` from ``istota.executor``, so both are patched at
    their definition sites rather than on the OCR module.
    """
    captured: list[BrainRequest] = []
    import istota.brain as brain_mod
    import istota.executor as executor_mod

    monkeypatch.setattr(
        brain_mod, "make_brain", lambda _cfg: _CapturingBrain(captured)
    )
    monkeypatch.setattr(
        executor_mod, "persist_brain_usage", lambda *a, **k: None
    )
    for name, value in DAEMON_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    return captured


def _ocr_modules():
    from istota.health import encounter_ocr, immunization_ocr, ocr

    return [ocr, encounter_ocr, immunization_ocr]


def _ocr_module_ids():
    return ["panel", "encounter", "immunization"]


class TestTheOcrRequestNamesItsDocument:
    """Direction 1 from the entry: give the request the roots it needs."""

    @pytest.mark.parametrize(
        "module", _ocr_modules(), ids=_ocr_module_ids()
    )
    def test_a_read_granting_request_carries_the_document_as_its_only_root(
        self, module, capture_request, make_config, tmp_path
    ):
        document = tmp_path / "uploads" / "7" / "original.png"
        document.parent.mkdir(parents=True)
        document.write_bytes(b"\x89PNG\r\n\x1a\n")
        config = make_config()

        module._call_brain(
            "extract this", config, read_path=document, user_id="alice"
        )

        req = capture_request[0]
        assert req.allowed_tools == ["Read"]
        assert req.fs_read_roots == [document]

    @pytest.mark.parametrize(
        "module", _ocr_modules(), ids=_ocr_module_ids()
    )
    def test_read_is_not_offered_without_a_document_to_read(
        self, module, capture_request, make_config
    ):
        """The two travel together, so ``Read`` with no root is unreachable."""
        module._call_brain("extract this", make_config(), user_id="alice")

        req = capture_request[0]
        assert req.allowed_tools == []
        assert not req.fs_read_roots

    @pytest.mark.parametrize(
        "module", _ocr_modules(), ids=_ocr_module_ids()
    )
    def test_the_roots_refuse_a_read_outside_the_document(
        self, module, capture_request, make_config, tmp_path
    ):
        """The roots as ``NativeBrain._tool_workspace`` would apply them.

        Asserting on the field alone would pass against a root that confines
        nothing, so this builds the ``ToolEnv`` the request produces and asks
        it the question the model's ``Read`` would ask. The negative control is
        that against the pre-fix code ``fs_read_roots`` is ``None``,
        ``read_roots`` is therefore ``None``, and ``/etc/passwd`` resolves.
        """
        document = tmp_path / "uploads" / "7" / "original.png"
        document.parent.mkdir(parents=True)
        document.write_bytes(b"\x89PNG\r\n\x1a\n")
        sibling = document.parent / "other-patient.png"
        sibling.write_bytes(b"\x89PNG\r\n\x1a\n")
        config = make_config()

        module._call_brain(
            "extract this", config, read_path=document, user_id="alice"
        )

        req = capture_request[0]
        env = ToolEnv(
            cwd=Path(req.cwd),
            read_roots=tuple(req.fs_read_roots) if req.fs_read_roots else None,
        )
        assert env.confined
        assert env.resolve(str(document)) == document.resolve()
        with pytest.raises(ToolPathError):
            env.resolve("/etc/passwd")
        # The document's own directory holds other users' uploads on a shared
        # deployment, so the root is the file rather than its parent.
        with pytest.raises(ToolPathError):
            env.resolve(str(sibling))


class TestTheOcrRequestEnvironment:
    """Direction 3 from the entry: narrow the env to what OCR needs."""

    @pytest.mark.parametrize(
        "module", _ocr_modules(), ids=_ocr_module_ids()
    )
    def test_no_daemon_credential_reaches_the_request(
        self, module, capture_request, make_config
    ):
        module._call_brain("extract this", make_config(), user_id="alice")

        env = capture_request[0].env
        for name in DAEMON_CREDENTIALS:
            assert name not in env, f"{name} reached the model's environment"

    @pytest.mark.parametrize(
        "module", _ocr_modules(), ids=_ocr_module_ids()
    )
    def test_the_request_still_carries_what_a_model_call_needs(
        self, module, capture_request, make_config
    ):
        """A narrowed env must not be an empty one.

        ``ToolEnv.subprocess_env`` and ``create_subprocess_exec`` both read a
        falsy env as "inherit the parent", and the parent is the daemon — so an
        empty dict here would be strictly worse than the bug it replaces.
        """
        module._call_brain("extract this", make_config(), user_id="alice")

        env = capture_request[0].env
        assert env
        assert env.get("PATH")
        assert env.get("HOME")


class TestTheNarrowedEnvCanStillReachTheProvider:
    """The regression the narrowing itself could have introduced.

    None of these calls builds a CONNECT bridge or an ``--unshare-net``, so
    they run in the daemon's own network namespace and used to inherit its
    proxy, CA-bundle and gateway settings from ``dict(os.environ)``. Narrowing
    without carrying those forward strands a proxy-only or gateway deployment
    at the connect, from six sites at once.
    """

    REACHABILITY_ENV = {
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "HTTP_PROXY": "http://proxy.internal:3128",
        "SSL_CERT_FILE": "/etc/ssl/certs/corporate.pem",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/corporate.pem",
        "ANTHROPIC_BASE_URL": "https://gateway.internal/v1",
        "ANTHROPIC_AUTH_TOKEN": "gateway-token-placeholder",
    }

    def test_the_provider_reachability_vars_survive(
        self, monkeypatch, make_config
    ):
        from istota.executor import build_model_cli_env

        for name, value in self.REACHABILITY_ENV.items():
            monkeypatch.setenv(name, value)
        for name in DAEMON_CREDENTIALS:
            monkeypatch.setenv(name, "should-not-survive")

        env = build_model_cli_env(make_config())

        for name, value in self.REACHABILITY_ENV.items():
            assert env.get(name) == value, f"{name} was dropped"
        for name in DAEMON_CREDENTIALS:
            assert name not in env

    def test_an_empty_no_proxy_survives(self, monkeypatch, make_config):
        """``NO_PROXY=`` blanks an inherited exemption list; it is not absence."""
        from istota.executor import build_model_cli_env

        monkeypatch.setenv("NO_PROXY", "")

        assert build_model_cli_env(make_config())["NO_PROXY"] == ""

    def test_an_unset_var_is_not_invented(self, monkeypatch, make_config):
        from istota.executor import build_model_cli_env

        for name in (*self.REACHABILITY_ENV, "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)

        env = build_model_cli_env(make_config())

        for name in (*self.REACHABILITY_ENV, "NO_PROXY"):
            assert name not in env

    def test_an_operator_passthrough_entry_wins(self, monkeypatch, make_config):
        """`build_clean_env` applies passthrough; this fills in, never overrides."""
        from istota.config import SecurityConfig
        from istota.executor import build_model_cli_env

        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        config = make_config(
            security=SecurityConfig(passthrough_env_vars=["HTTPS_PROXY"])
        )

        assert (
            build_model_cli_env(config)["HTTPS_PROXY"]
            == "http://proxy.internal:3128"
        )


class TestTheOtherDirectBuildersNarrowToo:
    """The three builders that grant no tool still handed over the env."""

    def test_the_sleep_cycle_request_drops_the_daemon_credentials(
        self, monkeypatch, make_config
    ):
        from istota.executor import build_model_cli_env

        for name, value in DAEMON_CREDENTIALS.items():
            monkeypatch.setenv(name, value)

        env = build_model_cli_env(make_config())

        assert env.get("PATH")
        for name in DAEMON_CREDENTIALS:
            assert name not in env

    @pytest.mark.parametrize(
        "module_path, func_name",
        [
            ("istota.memory.sleep_cycle", "_run_sleep_cycle_brain"),
            ("istota.briefings.shared_blocks", "_run_section_brain"),
            ("istota.health.explainer", "_call_brain"),
        ],
    )
    def test_the_builder_sources_its_env_from_the_helper(
        self, module_path, func_name
    ):
        """Pins the wiring the AST guard cannot see: the helper is called.

        The guard proves the daemon environment is gone; it does not prove what
        replaced it, and a request built with ``env={}`` would satisfy it while
        being worse than the bug (``None``/falsy means "inherit the parent").
        Asserting the helper is what feeds ``env=`` closes that, without
        standing three subsystems up.

        The check is on the parsed call rather than on the function text: the
        comment above each of these sites quotes the old expression, so a
        substring search matches the explanation as readily as the code.
        """
        import importlib
        import inspect
        import textwrap

        module = importlib.import_module(module_path)
        source = textwrap.dedent(inspect.getsource(getattr(module, func_name)))

        env_values = [
            _keyword(node, "env")
            for _, node in _calls_named(ast.parse(source), "BrainRequest")
        ]
        assert env_values, f"{func_name} builds no BrainRequest"
        for value in env_values:
            assert isinstance(value, ast.Call), "env= is not a call"
            assert getattr(value.func, "id", None) == "build_model_cli_env"


#: Tools whose grant must travel with a read allowlist. `Grep` and `Glob` walk
#: the filesystem the same way `Read` opens it, so all three are file tools for
#: this purpose even though only `Read` is granted in the tree today.
FILE_TOOLS = {"Read", "Grep", "Glob"}


def _calls_named(tree: ast.AST, target: str):
    """Yield ``(node, node)`` for every call to ``target`` in one parsed tree."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name == target:
            yield node, node


def _brain_request_calls():
    """Every ``BrainRequest(...)`` construction site under ``src/istota``."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for _, node in _calls_named(tree, "BrainRequest"):
            yield path, node


def _keyword(node: ast.Call, arg: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == arg:
            return kw.value
    return None


class TestNoBuilderPassesTheDaemonEnvironment:
    """The guard that holds the env half of the class."""

    def test_every_brainrequest_in_the_tree_narrows_its_env(self):
        offenders = []
        for path, node in _brain_request_calls():
            value = _keyword(node, "env")
            if value is not None and _is_os_environ_copy(value):
                rel = path.relative_to(SRC.parent.parent)
                offenders.append(f"{rel}:{value.lineno}")
        assert not offenders, (
            "These BrainRequest builders hand the model the daemon's whole "
            "environment. Use executor.build_model_cli_env(config) instead "
            "(ISSUE-395): " + ", ".join(offenders)
        )


class TestNoBuilderGrantsAFileToolWithoutRoots:
    """The guard that holds the confinement half — the one ISSUE-395 is about.

    The env guard alone would let a seventh builder grant ``Read`` with no
    ``fs_read_roots`` and ship green, which reproduces the original defect
    exactly.
    """

    def test_a_file_tool_grant_carries_read_roots(self):
        offenders = []
        for path, node in _brain_request_calls():
            tools = _keyword(node, "allowed_tools")
            if not _grants_file_tool(tools):
                continue
            if _keyword(node, "fs_read_roots") is None:
                rel = path.relative_to(SRC.parent.parent)
                offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, (
            "These BrainRequest builders grant a file tool without passing "
            "fs_read_roots. Absent roots means NO confinement, not none "
            "allowed — see session/tools/env.py (ISSUE-395): "
            + ", ".join(offenders)
        )


def _grants_file_tool(node: ast.expr | None) -> bool:
    """True when a literal ``allowed_tools`` list can contain a file tool.

    Only a literal list is inspected. A computed one (``build_allowed_tools``
    on the task path) is not a direct builder's concern and is confined by the
    executor instead.
    """
    if isinstance(node, ast.IfExp):
        return _grants_file_tool(node.body) or _grants_file_tool(node.orelse)
    if not isinstance(node, (ast.List, ast.Tuple)):
        return False
    return any(
        isinstance(el, ast.Constant) and el.value in FILE_TOOLS
        for el in node.elts
    )


def _is_os_environ_copy(node: ast.expr) -> bool:
    """True for the literal ways of spelling "the whole environment".

    Covers ``os.environ``, a bare ``environ`` from ``from os import environ``,
    ``dict(os.environ)``, ``os.environ.copy()`` and ``{**os.environ}``. It does
    not resolve a name bound to one of those on an earlier line; see the module
    docstring.
    """
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    if isinstance(node, ast.Name) and node.id == "environ":
        return True
    if isinstance(node, ast.Dict):
        # ``{**os.environ}`` — an unpacked mapping has a ``None`` key.
        return any(
            key is None and _is_os_environ_copy(value)
            for key, value in zip(node.keys, node.values)
        )
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "dict":
            if any(_is_os_environ_copy(a) for a in node.args):
                return True
            return any(
                kw.arg is None and _is_os_environ_copy(kw.value)
                for kw in node.keywords
            )
        if isinstance(func, ast.Attribute) and func.attr == "copy":
            return _is_os_environ_copy(func.value)
    return False
