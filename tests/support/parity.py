"""The security parity matrix, and the tests that witness each row.

The one-deployment-shape spec moves Istota from bare metal onto one OCI image
run in a dedicated VM, and its governing rule is that the new shape is at least
as secure as the old one at every boundary. Each boundary is a numbered row in
the spec's "Security parity matrix", and each row is proven by a witness test
against the built artifact. `ROWS` is the in-tree copy of that row list; the
spec lives outside the repository, so this is what the default suite can hold.

A test names the rows it witnesses with the decorator:

    from tests.support import parity

    @parity.witness(4)
    class TestATaskIsInItsOwnCgroup: ...

The decorator changes nothing about collection or running. What reads it is
`tests/test_parity_registry.py`, which walks `tests/` for the decorator rather
than importing the tier modules, so a witness in an artifact tier is counted
without that tier's fixtures being loaded.

Every row is in exactly one of three states: witnessed (some test carries the
decorator for it), pending (named in `PENDING` with the stage that will close
it), or not witnessed by design (`NOT_WITNESSED`, row 14 only). `PENDING` may
only shrink: it must stay a subset of `PENDING_AT_STAGE_1`, so a row added to
the matrix later has to arrive with its witness, and a witnessed row has to be
taken out of `PENDING`. Witnessed is not closed: a row is closed when its
negative control has been run and seen red, which the stage's result file
records.
"""

from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")

ROWS: dict[int, str] = {
    1: "per-task mount plan: database masks, control dir read-only, no other user's workspace or repos, --disable-userns",
    2: "task network: --unshare-net, CONNECT allowlist, broker interception for bound hosts",
    3: "skill and network proxy peer check (ISSUE-550)",
    4: "per-task resource limits",
    5: "daemon and every exec path unprivileged",
    6: "daemon filesystem write scope",
    7: "daemon cannot see the rest of the machine",
    8: "no Docker API reachable from istota or a task",
    9: "never write into an unmounted external store",
    10: "devbox egress",
    11: "devbox credential socket reaches only its owner",
    12: "secrets not readable outside the daemon",
    13: "release integrity",
    14: "engine privilege",
    15: "host log and audit bounds, swap",
    16: "task syscall surface",
    17: "browser container cannot reach the rest of the stack",
    18: "proxied listener reachable only from the upstream",
    19: "TLS in direct mode",
    20: "container MAC confinement",
}

NOT_WITNESSED: dict[int, str] = {
    14: "rootful dockerd inside a dedicated VM; parity with bare metal plus a container, closed by the rootless-podman spec",
}

PENDING: dict[int, str] = {
    7: "Stage 7",
    9: "Stage 7",
    10: "Stage 7",
    11: "Stage 7",
    15: "Stage 7",
    18: "Stage 7",
    19: "Stage 7",
    # Needs a host with AppArmor; Docker Desktop has none.
    20: "Stage 7",
}

# The pending set as Stage 1 left it. `PENDING` may lose rows, never gain them.
# Editing this to make room for a new pending row defeats the guard; a new row
# lands with its witness instead. Row 20 is the one exception, and it was the
# spec's: the AppArmor row was added after Stage 1 measured docker-default
# refusing bwrap's mounts, and only a host with AppArmor can witness it.
PENDING_AT_STAGE_1: frozenset[int] = frozenset(
    {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20}
)

# A row the spec closes in two halves, witnessed for the half that has landed.
# Each entry names the half still to come; the registry test requires the row
# to be witnessed, so this records what a witness does not yet cover rather
# than standing in for one.
OUTSTANDING_HALVES: dict[int, str] = {
    5: "Stage 6: the VM's `istota` wrapper and `istota-stack setup` exec as 10001",
    # The image tier runs on Docker Desktop, whose file sharing shows every
    # bind-mounted file as uid 0, so the mode is witnessed there and the owner
    # only where the bind keeps the writer's uid: the VM.
    12: "Stage 7: the secret files are owned by uid 10001, read on the VM",
}


def witness(*rows: int) -> Callable[[T], T]:
    """Mark a test class or function as the witness for one or more rows."""
    unknown = [row for row in rows if row not in ROWS]
    if not rows or unknown:
        raise ValueError(f"parity.witness needs known matrix rows, got {rows!r}")

    def mark(target: T) -> T:
        existing = getattr(target, "__parity_rows__", ())
        setattr(target, "__parity_rows__", tuple(existing) + tuple(rows))
        return target

    return mark
