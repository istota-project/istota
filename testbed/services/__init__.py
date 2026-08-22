"""What a stack fixture needs from anything the daemon talks to.

A **service** is anything the daemon talks to that is not the daemon. Real
(Nextcloud, a mail server) or written by us (the forge, the model endpoint).
One protocol covers both, because the fixture's job — start it, point the
rendered config at it, reset it between tests, stop it — is the same either way.

A **stub** is a service we wrote, and it is a liability to be minimized rather
than a design goal: where a real implementation can be run in a container for
comparable cost, run the real one. `HttpStub` is the shared base for the ones
that stay.

Call recording is deliberately **not** on the protocol. It is on `HttpStub`,
because it does not generalize — a mail server speaks IMAP and a real Nextcloud
is asserted through its own API, so a `calls` list on the protocol would mean
something different for two of six members. A protocol that admits the
difference beats one that pretends there is none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import parse_qs


@dataclass(frozen=True)
class ServiceCall:
    """One request a service was asked to serve.

    `auth` is a *shape* string rather than the credential itself — scheme and
    length, never the value. The forge stub established that rule and it is
    right for every service: an assertion needs to know a credential of the
    right kind arrived, and a fixture that stores real-looking tokens in a list
    that gets printed into failure output is a liability on a public repo.

    `auth` is not the only field that can carry a secret, though, and the other
    three are kept whole because assertions need them: GitLab REST accepts
    `?private_token=`, a live write verb can put one in a body, and `headers` is
    the raw header block. So the guarantee is about *rendering* rather than
    about storage — see `__repr__`.
    """

    method: str
    path: str
    """Query-stripped. The query string is parsed into `query`."""
    auth: str = ""
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    query: dict = field(default_factory=dict)

    def payload(self) -> dict:
        """`body` as a dict, whichever encoding the client happened to pick.

        JSON first, then form-encoding, because a REST client uses both — glab
        posts form bodies for some verbs and JSON for others, and an assertion
        should not have to know which. A body that is neither comes back under
        `_body` rather than raising, so a stub handler cannot die on a
        malformed request in a thread whose `handle_error` is silent.
        """
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body)
        except ValueError:
            decoded = self.body.decode("utf-8", "replace")
            return {key: value[0] for key, value in parse_qs(decoded).items()}
        return parsed if isinstance(parsed, dict) else {"_body": parsed}

    def __str__(self) -> str:  # pragma: no cover - diagnostic
        return f"{self.method} {self.path} auth={self.auth}"

    # `repr`, not just `str`. pytest's assertion rewriting renders the *repr* of
    # whatever a failing comparison touched, so a dataclass's generated one is
    # what reaches the report and the terminal — and it prints `body`, `query`
    # and `headers` in full. Under a `--live` run that is a real token in a repo
    # whose pre-commit hook exists because pasted terminal output is where
    # credentials land. `assert not stub.calls_matching(...)` fails on a *list*,
    # and a list's repr calls `repr` on each element, so a `__str__`-only
    # override would be bypassed by every real failure this protects.
    __repr__ = __str__


@runtime_checkable
class Service(Protocol):
    """What the stack fixture needs from any external service."""

    name: str
    """Registry key: "gitlab", "ntfy", "feeds", "mail", "model", "nextcloud"."""

    @property
    def container_url(self) -> str:
        """The address a process inside the istota container reaches this on.

        For a host-side stub, `host.docker.internal` and the bound port. For a
        service running as a compose service in the same project, its service
        name. The caller never learns which.
        """

    def config_env(self) -> dict[str, str]:
        """The `ISTOTA_*` variables that point `render-config.sh` at this service.

        Merged into the render environment (lean shape) or the compose env-file
        (full shape) by the stack fixture. **Must name only variables the
        shipped generator already reads _and_ `docker-compose.yml` passes
        through.** If a subsystem has no such variable, the fix is to add one to
        both, as a reviewed product change — never to side-load config from the
        fixture, which is the property that makes this whole tier honest.
        """

    def reset(self) -> None:
        """Return to the state a fresh test expects.

        Clear recorded calls, restore seeded state. Called between tests against
        a session-scoped stack, so it must be cheap and total: a `reset` that
        leaves one mutation behind is a cross-test dependency that will be
        diagnosed as flake.
        """

    def close(self) -> None:
        """Stop serving and release resources. Idempotent."""


def _model_service(*args, **kwargs) -> Service:
    """Imported late, because `httpstub` imports this module.

    `services/__init__` holds `ServiceCall`, which `httpstub` needs, which every
    stub subclasses — so a top-level `from . import model_endpoint` here would
    close the cycle at import time.
    """
    from . import model_endpoint

    return model_endpoint.serve_script(*args, **kwargs)


def _gitlab_service(*args, **kwargs) -> Service:
    from . import gitlab

    return gitlab.serve(*args, **kwargs)


#: Profile service names to the factory that produces one. A profile names
#: services by string, so a typo is a `KeyError` at fixture setup rather than an
#: import error; `tests/test_testbed_services.py` closes that by checking every
#: profile's names against these keys in the default suite.
REGISTRY: dict[str, Callable[..., Service]] = {
    "model": _model_service,
    "gitlab": _gitlab_service,
}


def build(name: str, *, scratch: Path, host: str) -> Service:
    """Construct one registered service the way a *stack* needs it.

    A separate function rather than a uniform `REGISTRY` signature, because the
    factories deliberately keep their own. `serve_script(turns, ...)` and
    `gitlab.serve(repo_root, ...)` are the API a unit test and an external
    driver use, and — the part that would be lost — they are what
    `tests/test_testbed_services.py` drives when it proves every registered
    service refuses a public bind with no credential. Flattening them into one
    "give me a service" signature that always supplies a credential would make
    that guard unwritable.

    So this is the adapter: the two things a pool has (a scratch directory and
    the interface to bind) turned into what each service's own factory takes,
    including the credential each one publishes. It is an explicit branch per
    service rather than a table of partials, because each branch has something
    to say — the forge needs its repository seeded before a scenario can clone
    it, and a table would have hidden that in a lambda.
    """
    if name not in REGISTRY:
        raise KeyError(
            f"no service named {name!r}; the registry holds {sorted(REGISTRY)}"
        )

    if name == "model":
        from . import model_endpoint

        # No turns: `Stack.reset` installs the real script before each test,
        # and a service constructed with one would let a poller's task consume
        # it in the window before the first reset.
        return model_endpoint.serve_script(
            [], host=host, credential=model_endpoint.ENDPOINT_CREDENTIAL
        )

    if name == "gitlab":
        from . import gitlab

        stub = gitlab.serve(
            scratch / "gitlab", host=host, token=gitlab.FORGE_TOKEN
        )
        # Seeded here rather than by the caller: a forge with no repository is
        # not a thing any scenario in this tier can use, and `reset()` rebuilds
        # exactly what `seed_repo` registered — so a stack whose repo was
        # seeded by the test would lose it at the first reset.
        stub.seed_repo(stub.project)
        return stub

    raise KeyError(  # pragma: no cover - unreachable while the two agree
        f"{name!r} is registered but `build` does not know how to construct it"
    )
