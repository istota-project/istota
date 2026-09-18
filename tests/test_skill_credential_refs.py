"""The credential stamp: the box, the pre-dispatch refusal, and the coverage walk.

Three layers, and the file is arranged around what each can be wrong about.

`SecretValue` owns one property and it is a rendering one: the plaintext must
not come out of `repr`, `str`, an f-string or a container holding the box. Those
tests need no proxy and no parser.

`_credref.resolve_parsed` owns the resolution: every stamped argument on the
verbs an invocation actually took, one request per element, in argv order, and
the first refusal refuses the whole call. Those tests drive a **real**
`SkillProxy` over a real `AF_UNIX` socket, because the authority being consulted
is the proxy — a fake would be a model of the answer rather than the answer, and
the log level and the `mode` field are properties of the wire.

`_cli.parse_and_resolve` owns the refusal: the facade's envelope, exit 1, and
**no dispatch**. That last one is asserted by giving the command table a handler
that records being called, rather than by reading the message — a control that
cannot tell a refused call from a differently-worded failure is not a control.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import tempfile
from pathlib import Path

import pytest

from istota.skill_proxy import SkillProxy
from istota.skills import _cli
from istota.skills._credref import (
    MODE,
    NAME,
    PAIR,
    STAMP,
    CredentialPair,
    SecretValue,
    credential_ref,
    stamped,
)

#: Values distinct enough that a sweep over a whole rendered blob means
#: something; the names beside them are deliberately ordinary.
VAULT = {
    "acme_password": "credvalue-acme-zzzzzzzzzzzz",
    "acme_username": "credvalue-user-yyyyyyyyyyyy",
    "github_pat": "credvalue-ghp-xxxxxxxxxxxx",
}


@pytest.fixture
def sock_path():
    """Short socket path that fits the AF_UNIX limit (~104 chars on macOS)."""
    directory = tempfile.mkdtemp(prefix="cr_", dir="/tmp")
    path = Path(directory) / "s.sock"
    yield path
    path.unlink(missing_ok=True)
    Path(directory).rmdir()


@pytest.fixture
def proxy(sock_path, monkeypatch):
    """A live proxy, with its socket where the resolver will look for it."""
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock_path))

    def start(**kwargs):
        kwargs.setdefault("vault_credentials", dict(VAULT))
        server = SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin"}, task_id=11, **kwargs,
        )
        server.__enter__()
        started.append(server)
        return server

    started: list[SkillProxy] = []
    yield start
    for server in started:
        server.__exit__(None, None, None)


def build(*, form=NAME, **kwargs):
    """A two-level parser with one stamped argument under `go`."""
    parser = argparse.ArgumentParser(prog="fake-skill")
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("go")
    go.add_argument("--plain")
    credential_ref(go, "--secret", form=form, **kwargs)
    # A sibling verb with the same dest and no stamp: the walk must not resolve
    # a stamp declared under a verb this invocation did not take.
    other = sub.add_parser("other")
    other.add_argument("--secret")
    return parser


class Recorder:
    """A handler that records having run, which is the discriminating fact.

    The pre-dispatch refusal's whole claim is that no handler runs. An
    assertion on the envelope's wording cannot tell a refusal from a handler
    that ran and failed differently, so every test about that claim reads
    `calls` and not the message.
    """

    def __init__(self, result=None):
        self.calls: list[argparse.Namespace] = []
        self.result = result if result is not None else {"status": "ok"}

    def __call__(self, args):
        self.calls.append(args)
        return self.result


def drive(parser, argv, handler):
    """One skill `main`: parse, resolve, dispatch. Returns the printed envelope."""
    args = _cli.parse_and_resolve(parser, argv)
    _cli.run_skill_cli({"go": handler, "other": handler}, args)
    return args


# ---------------------------------------------------------------------------
# The box
# ---------------------------------------------------------------------------


class TestSecretValue:
    def test_reveal_returns_the_plaintext(self):
        assert SecretValue("acme_password", "hunter2").reveal() == "hunter2"

    @pytest.mark.parametrize(
        "render",
        [
            repr,
            str,
            lambda v: f"{v}",
            lambda v: f"{v!s}",
            lambda v: f"{v!r}",
            lambda v: f"{v:>40}",
            lambda v: "{}".format(v),  # noqa: UP032 — the point is the call
            lambda v: json.dumps({"value": v}, default=repr),
            lambda v: repr([v]),
            lambda v: repr({"password": v}),
            lambda v: repr(CredentialPair("#password", v)),
            lambda v: str(ValueError(f"could not fill with {v}")),
        ],
    )
    def test_no_rendering_carries_the_plaintext(self, render):
        value = SecretValue("acme_password", VAULT["acme_password"])
        rendered = render(value)
        assert VAULT["acme_password"] not in rendered
        # Not merely absent as a whole: no run of it either, which is what
        # catches a renderer that truncates rather than redacts.
        assert "credvalue" not in rendered
        assert "acme_password" in rendered

    def test_the_name_is_what_it_renders_as(self):
        assert repr(SecretValue("github_pat", "x")) == "SecretValue(github_pat)"
        assert str(SecretValue("github_pat", "x")) == "SecretValue(github_pat)"

    def test_it_carries_no_instance_dict_to_dump(self):
        """`vars()` on a `__dict__`-carrying box hands back the plaintext."""
        with pytest.raises(TypeError):
            vars(SecretValue("github_pat", VAULT["github_pat"]))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_a_name_form_reaches_the_handler_boxed(self, proxy):
        proxy()
        handler = Recorder()
        drive(build(), ["go", "--secret", "github_pat"], handler)
        value = handler.calls[0].secret
        assert isinstance(value, SecretValue)
        assert value.reveal() == VAULT["github_pat"]
        assert value.name == "github_pat"

    def test_a_pair_form_keeps_the_label(self, proxy):
        proxy()
        handler = Recorder()
        drive(
            build(form=PAIR),
            ["go", "--secret", "#password=acme_password"],
            handler,
        )
        pair = handler.calls[0].secret
        assert isinstance(pair, CredentialPair)
        assert pair.label == "#password"
        assert pair.value.reveal() == VAULT["acme_password"]

    def test_an_appended_list_resolves_in_order(self, proxy):
        proxy()
        handler = Recorder()
        drive(
            build(form=PAIR, action="append"),
            [
                "go",
                "--secret", "#user=acme_username",
                "--secret", "#password=acme_password",
            ],
            handler,
        )
        pairs = handler.calls[0].secret
        assert [p.label for p in pairs] == ["#user", "#password"]
        assert [p.value.reveal() for p in pairs] == [
            VAULT["acme_username"], VAULT["acme_password"],
        ]

    def test_the_resolved_namespace_renders_no_plaintext(self, proxy):
        """The box is on the namespace, which is what handlers log.

        This is the boxing claim made about the *wiring* rather than about the
        class: `repr(args)` walks every value on the namespace, which is what a
        handler's debug line, an interpolated exception and pytest's own
        assertion rewriting all do.
        """
        proxy()
        handler = Recorder()
        drive(
            build(form=PAIR, action="append"),
            [
                "go",
                "--secret", "#user=acme_username",
                "--secret", "#password=acme_password",
            ],
            handler,
        )
        args = handler.calls[0]
        blob = f"{args!r} {args.secret} {args.secret!r} {args.secret[0]}"
        for value in VAULT.values():
            assert value not in blob
        assert "credvalue" not in blob
        assert "acme_password" in blob

    def test_an_absent_argument_is_left_alone(self, proxy):
        proxy()
        handler = Recorder()
        drive(build(), ["go", "--plain", "x"], handler)
        assert handler.calls[0].secret is None

    def test_a_stamp_under_a_verb_that_was_not_taken_is_not_resolved(self, proxy):
        """The sibling `other --secret` carries no stamp and must stay a string."""
        proxy()
        handler = Recorder()
        drive(build(), ["other", "--secret", "github_pat"], handler)
        assert handler.calls[0].secret == "github_pat"

    def test_a_parser_with_no_stamp_behaves_as_parse_args_does(self, proxy):
        proxy()
        parser = argparse.ArgumentParser(prog="unstamped")
        sub = parser.add_subparsers(dest="command", required=True)
        go = sub.add_parser("go")
        go.add_argument("--anything")
        handler = Recorder()
        args = drive(parser, ["go", "--anything", "github_pat"], handler)
        assert args.anything == "github_pat"
        assert handler.calls

    def test_the_request_declares_mode_skill_and_logs_at_info(self, proxy, caplog):
        proxy()
        with caplog.at_level(logging.INFO, logger="istota.skill_proxy"):
            drive(build(), ["go", "--secret", "github_pat"], Recorder())
        records = [
            r for r in caplog.records if "vault_credential task_id" in r.getMessage()
        ]
        assert len(records) == 1
        assert f"mode={MODE}" in records[0].getMessage()
        assert records[0].levelno == logging.INFO
        assert VAULT["github_pat"] not in records[0].getMessage()


# ---------------------------------------------------------------------------
# The refusal, before dispatch
# ---------------------------------------------------------------------------


def refusal_of(capsys):
    """The envelope the facade printed."""
    return json.loads(capsys.readouterr().out.strip())


class TestTheRefusal:
    def test_an_unknown_name_refuses_and_the_handler_never_runs(
        self, proxy, capsys,
    ):
        proxy()
        handler = Recorder()
        code = None
        try:
            drive(build(), ["go", "--secret", "no_such_name"], handler)
        except SystemExit as exc:  # noqa: PERF203 — one call, not a loop
            code = exc.code
        # The discriminating assertion, made first and independently of how
        # the call ended: no handler ran. Asserting the envelope or the exit
        # status alone passes against a handler that ran and failed, which is
        # exactly the state removing the refusal produces.
        assert handler.calls == []
        assert code == 1
        envelope = refusal_of(capsys)
        assert envelope["status"] == "error"
        assert envelope["reason"] == "vault_credential_refused"

    def test_the_refusal_names_the_verb_and_the_flag(self, proxy, capsys):
        proxy()
        with pytest.raises(SystemExit):
            drive(build(), ["go", "--secret", "no_such_name"], Recorder())
        message = refusal_of(capsys)["error"]
        assert "go --secret" in message

    def test_no_value_reaches_the_refusal(self, proxy, capsys):
        """A refusal for one name must not carry another's value."""
        proxy()
        with pytest.raises(SystemExit):
            drive(
                build(form=PAIR, action="append"),
                [
                    "go",
                    "--secret", "#user=acme_username",
                    "--secret", "#password=no_such_name",
                ],
                Recorder(),
            )
        blob = capsys.readouterr().out
        for value in VAULT.values():
            assert value not in blob

    def test_a_later_refusal_still_refuses_the_whole_call(self, proxy):
        proxy()
        handler = Recorder()
        with pytest.raises(SystemExit):
            drive(
                build(form=PAIR, action="append"),
                [
                    "go",
                    "--secret", "#user=acme_username",
                    "--secret", "#password=no_such_name",
                ],
                handler,
            )
        assert handler.calls == []

    def test_a_malformed_pair_refuses_without_asking_the_proxy(
        self, proxy, capsys, caplog,
    ):
        proxy()
        handler = Recorder()
        with caplog.at_level(logging.DEBUG, logger="istota.skill_proxy"):
            with pytest.raises(SystemExit):
                drive(build(form=PAIR), ["go", "--secret", "acme_password"], handler)
        assert refusal_of(capsys)["reason"] == "vault_credential_refused"
        assert handler.calls == []
        # Nothing was fetched, so nothing was charged to the attempt's budget.
        assert not [
            r for r in caplog.records if "vault_credential" in r.getMessage()
        ]

    def test_an_empty_name_refuses(self, proxy, capsys):
        proxy()
        with pytest.raises(SystemExit):
            drive(build(), ["go", "--secret", "   "], Recorder())
        assert refusal_of(capsys)["reason"] == "vault_credential_refused"

    def test_the_cap_refusal_reaches_the_facade(self, proxy, capsys):
        """The per-attempt budget is the proxy's, and its refusal is one of ours."""
        proxy(vault_fetch_limit=1)
        handler = Recorder()
        with pytest.raises(SystemExit):
            drive(
                build(form=PAIR, action="append"),
                [
                    "go",
                    "--secret", "#user=acme_username",
                    "--secret", "#password=acme_password",
                ],
                handler,
            )
        envelope = refusal_of(capsys)
        assert envelope["reason"] == "vault_credential_refused"
        # The cap's own refusal names no credential; ours must not add one.
        assert "acme_password" not in envelope["error"]
        assert handler.calls == []

    def test_no_socket_refuses_rather_than_raising(self, monkeypatch, capsys):
        monkeypatch.delenv("ISTOTA_SKILL_PROXY_SOCK", raising=False)
        handler = Recorder()
        with pytest.raises(SystemExit) as exc:
            drive(build(), ["go", "--secret", "github_pat"], handler)
        assert exc.value.code == 1
        assert refusal_of(capsys)["reason"] == "vault_credential_refused"
        assert handler.calls == []

    def test_an_unanswering_socket_refuses_rather_than_raising(
        self, sock_path, monkeypatch, capsys,
    ):
        """A listener that accepts and says nothing is a refusal, not a traceback."""
        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock_path))
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        try:
            handler = Recorder()
            with pytest.raises(SystemExit):
                drive(build(), ["go", "--secret", "github_pat"], handler)
            assert refusal_of(capsys)["reason"] == "vault_credential_refused"
            assert handler.calls == []
        finally:
            server.close()


# ---------------------------------------------------------------------------
# The coverage walk
# ---------------------------------------------------------------------------


def _skill_parsers():
    """Every argparse skill CLI in the tree, by name, built."""
    import importlib

    root = Path(__file__).resolve().parents[1] / "src" / "istota" / "skills"
    out = []
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if skill_dir.name.startswith("_"):
            continue
        module_name = f"istota.skills.{skill_dir.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception:  # pragma: no cover — a skill whose deps are absent
            continue
        builder = getattr(module, "build_parser", None)
        if builder is None:
            continue
        out.append((skill_dir.name, builder()))
    return out


#: Arguments whose *name* says they carry a credential reference. The walk's
#: heuristic, kept deliberately narrow: a wide one (`token`, `password`,
#: `secret`) matches arguments that take a literal value on purpose, which is a
#: registry of exemptions rather than a guard. This asks only that an argument
#: spelled like a credential *reference* carries the stamp that resolves one.
def _looks_like_a_credential_ref(action) -> bool:
    return action.dest.endswith("_credential") or action.dest.startswith(
        "credential_",
    )


class TestTheCoverageWalk:
    """Modelled on `tests/test_skill_host_paths_coverage.py`.

    A hand-maintained list of where a stamp belongs goes stale in silence, so
    the tree is walked instead. The claim here is narrower than the host-path
    walk's, and deliberately: any argument in a skill CLI *named* like a
    credential reference has to carry the stamp, and every stamp that exists
    has to be one the resolver understands and sits on a parser the facade
    resolves.
    """

    def test_every_credential_shaped_argument_is_stamped(self):
        missing = []
        for skill, parser in _skill_parsers():
            for dotted, dest, _form in _all_arguments(parser):
                action = _action_for(parser, dotted, dest)
                if _looks_like_a_credential_ref(action) and not getattr(
                    action, STAMP, None,
                ):
                    missing.append(f"{skill} {dotted} {dest}")
        assert missing == [], (
            "these arguments are named like a credential reference and carry "
            f"no stamp: {missing}"
        )

    def test_every_stamp_declares_a_form_the_resolver_knows(self):
        for skill, parser in _skill_parsers():
            for dotted, dest, form in stamped(parser):
                assert form in (NAME, PAIR), f"{skill} {dotted} {dest}: {form!r}"

    def test_the_walk_finds_the_argument_it_is_meant_to_guard(self):
        """The walk's own control: it sees the one stamp in the tree today."""
        found = {
            (skill, dotted, dest, form)
            for skill, parser in _skill_parsers()
            for dotted, dest, form in stamped(parser)
        }
        assert ("browse", "interact", "fill_credential", PAIR) in found

    def test_no_argument_carries_both_stamps(self):
        """A host path and a credential are different authorities on one value.

        Both resolvers write the resolved value back onto the dest, so an
        argument carrying both would have one overwrite the other in an order
        nothing declares.
        """
        from istota.skills._hostpath import STAMP as HOSTPATH_STAMP

        for skill, parser in _skill_parsers():
            for dotted, dest, _form in stamped(parser):
                action = _action_for(parser, dotted, dest)
                assert getattr(action, HOSTPATH_STAMP, None) is None, (
                    f"{skill} {dotted} {dest} carries both stamps"
                )

    def test_a_stamped_skill_resolves_through_the_facade(self):
        """A stamp on a parser whose `main` never calls `parse_and_resolve`
        is a declaration nothing enforces."""
        import importlib

        for skill, parser in _skill_parsers():
            if not stamped(parser):
                continue
            module = importlib.import_module(f"istota.skills.{skill}")
            source = Path(module.__file__).read_text()
            assert "parse_and_resolve" in source, skill


def _all_arguments(parser, trail=()):
    """`(dotted, dest, form)` for every argument in the tree, stamped or not."""
    out = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                out.extend(_all_arguments(child, (*trail, name)))
        else:
            out.append((".".join(trail), action.dest, getattr(action, STAMP, None)))
    return out


def _action_for(parser, dotted, dest):
    current = parser
    for part in [p for p in dotted.split(".") if p]:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                current = action.choices[part]
                break
    for action in current._actions:
        if action.dest == dest:
            return action
    raise AssertionError(f"no action {dest!r} under {dotted!r}")
