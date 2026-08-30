"""Which KV namespaces the model may not touch.

A reserved namespace holds framework state that happens to live in the KV
store: today the USER.md curation audit trail and the fingerprints the
bypass detector compares against (`memory/curation/audit.py`),
`_provisioned_rooms` — the Talk token `provision_rooms.py` provisioned for each
default room name, which is what lets a deploy recognise a room the user has
since renamed instead of minting a second one (ISSUE-342) — and
`_avatar_import`, what the scheduler's Nextcloud profile-picture import tick
wrote down for `doctor`'s socket-free `web.avatar_import` check to read. Those
rows are written by the daemon, by the host-side `memory` skill CLI and by the
`provision-rooms` CLI, and read by neither the model nor the `kv` skill.

Both KV tables, not only the per-user one: `skills/kv` applies this in `main`
before it dispatches a verb, so `--shared` — which reads and writes the
deployment-wide `shared_kv` — is covered by the same line. `_avatar_import` is
a `shared_kv` namespace and would otherwise be reachable.

The rule is a name prefix rather than a list, so a fifth reserved namespace
costs nothing here or at either enforcement point. Both of those are needed
and neither substitutes for the other:

- `skills/kv` refuses the namespace before it does anything, which covers a
  CLI call made host-side through the skill proxy.
- `scheduler_deferred` refuses it again when it applies a deferred op, which
  covers a **sandboxed** task: the sandbox has no database, so `kv set` there
  writes a JSON op file that the scheduler replays afterwards. Guarding only
  the CLI would leave that path open, and it is the path most tasks take.

This is defence in depth rather than the boundary. The boundary is that the
framework database is bound into no sandbox at any path; what this stops is a
task reaching the same rows through the one tool that legitimately spans the
whole store.

stdlib-only leaf: it imports nothing, so the `kv` skill subprocess can use it
without pulling in `istota.db`.
"""

from __future__ import annotations

# Every reserved namespace starts with this. Chosen because the `kv` skill has
# always documented namespaces as caller-chosen labels and nothing in the
# store used a leading underscore, so reserving the prefix orphaned no rows.
RESERVED_NAMESPACE_PREFIX = "_"


def is_reserved_namespace(namespace: object) -> bool:
    """True when `namespace` names framework state the model may not reach.

    Takes `object` rather than `str` on purpose: both call sites pass a value
    that came off an argparse namespace or out of model-written JSON, so a
    non-string is a case to answer rather than to crash on. Anything that is
    not a string is not a reserved namespace — the caller's own validation
    rejects it moments later for being unusable.
    """
    return isinstance(namespace, str) and namespace.startswith(
        RESERVED_NAMESPACE_PREFIX
    )
