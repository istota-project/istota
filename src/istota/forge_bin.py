"""Where the real ``gh`` / ``glab`` binaries are, and how to find them.

A stdlib-only leaf, deliberately. This rule is needed by two callers with
opposite cost profiles: the developer skill, which is already paying for the
whole ``istota.skills`` package when it runs, and ``istota.doctor``, which is
called from ``_validate_forge_clis`` on the ``load_config`` path — the daemon,
the web app, the webhook receiver, every CLI invocation, and every host-side
skill CLI subprocess the skill proxy spawns *per call*.

Importing ``istota.skills.developer`` to reach it cost ~190 ms of import time on
every one of those, because ``istota.skills.__init__`` star-imports the whole
skill set. That is about what five ``--version`` spawns cost, which is exactly
the expense ``probe=False`` exists to avoid — so the resolution rule lives here
instead, importable on its own.

``skills.developer`` re-exports these names, so its own call sites and the tests
that reach for them keep working.
"""

from __future__ import annotations

import os
import shutil

# Last-resort defaults, used only when nothing is configured and the binary is
# not on the daemon's PATH either. They are the conventional manual-install
# location, and since the role stopped taking these two from the Debian archive
# they are also where the Ansible role installs them and what it renders into
# config.toml — so the configured path and this default now normally agree.
FALLBACK_BIN = {"gh": "/usr/local/bin/gh", "glab": "/usr/local/bin/glab"}

# Where the docker image puts them (`docker/istota/Dockerfile`). Deliberately
# not a PATH directory, so `shutil.which` below cannot find them and the probe
# has to name the location. Checked ahead of PATH because it is the binary this
# image shipped and verified at build time.
IMAGE_BIN = {
    "gh": "/usr/local/lib/istota_forge/gh",
    "glab": "/usr/local/lib/istota_forge/glab",
}


def resolve_real_bin(configured: str, name: str) -> str:
    """Absolute path to the real forge binary the wrapper should exec.

    An operator's explicit path is returned as given, existing or not: exec'ing
    something *else* because the chosen one is missing would be the wrong
    surprise, and ``_validate_forge_clis`` already warns at start-up. Only the
    unchosen case falls back — an unset key, or the code default still standing
    — and then to what the *daemon's own* PATH resolves. That lookup runs
    host-side and never sees the model's environment, and the result is still
    an absolute path baked into the policy file, so the wrapper's exec stays as
    pinned as before.

    The fallback exists because the two halves of this setting ship
    separately. The Ansible role installs the binaries and renders the path in
    the same play, but only a *full* play run rewrites ``config.toml``, and the
    auto-update cron pulls code without running Ansible at all. In that window
    ``gh_bin_path`` is absent from the file, the dataclass default stands, and
    every forge command would otherwise exec a path that does not exist.

    A host deployed before the role moved off the Debian archive is the same
    case from the other side: its ``config.toml`` names ``/usr/bin/gh``, which
    is an explicit choice and returned as given, so it keeps exec'ing the stale
    apt binary until a full play rewrites the file. That is the intended
    behaviour rather than an oversight — the play that installs the new binary
    is the play that repoints the config, so the two never disagree for longer
    than one run.

    The docker image has the same gap and cannot close it from its side: the
    entrypoint writes ``config.toml`` only on a first boot with a fresh volume,
    so a container upgraded into a version whose image ships the binaries still
    has a ``[developer]`` block that predates them. ``IMAGE_BIN`` is probed for
    that case — an install shape whose binaries are off PATH by design has no
    other way to be found, and unlike the Ansible one it cannot be repaired by
    rerunning anything.
    """
    default = FALLBACK_BIN[name]
    if configured and configured != default:
        return configured
    if os.path.exists(default):
        return default
    shipped = IMAGE_BIN[name]
    if os.path.exists(shipped):
        return shipped
    return shutil.which(name) or default
