"""Artifacts an older release would have left behind, rebuilt on demand.

Layer 4 of the deployment-artifact-verification spec asks one question: does the
shipped image still work against the state an *earlier* release wrote? Two of
istota's three upgrade shapes ran new code against an old `config.toml` —

  * the auto-update cron `git reset --hard`s to main every two minutes and runs
    no Ansible at all, so a code-only advance keeps whatever config the last
    full play wrote. Still true;
  * an operator rebuilding the Docker stack over a retained volume kept the
    `config.toml` the entrypoint generated on the volume's *first* boot, which
    may predate the binaries the new image ships. ISSUE-368 made that render
    run on every boot, so this one is now a window rather than a permanent
    state: it is what the container looks like between the image changing and
    the container restarting, and it is still where the drift assertions have
    their subject.

Both are how ISSUE-263 reached production. Neither is observable from a test
that renders a fresh config, because a fresh config is by definition current.

So this module produces the *old* artifacts, and `tests/image/test_upgrade.py`
boots the *new* image over them.

Two artifacts, produced two different ways, and the difference is deliberate:

`config.toml` is captured by running the anchor's own `entrypoint.sh` in a
throwaway container. Nothing in that script executes istota code before it
writes the file — it is bash, curl and python3 — so the container can be built
from the *current* image and the anchor's release image never has to be built.
That matters: a month-old image is a ten-minute build whose failure (a moved
base image, a yanked package) is a supply-chain finding with nothing to say
about upgrades.

The database is built from the anchor's `schema.sql` instead, applied with
plain `sqlite3`. Running the anchor's *Python* under the current image's
installed dependency set would be neither the old environment nor the new one,
and its failure mode — an ImportError from dependency drift — reports as an
upgrade failure when it is nothing of the sort. `db.init_db` is
`_run_migrations` followed by `executescript(schema.sql)`, and on a fresh
database the migration half finds no tables to migrate, so the anchor's
`schema.sql` *is* the anchor's fresh schema, by construction.

Capturing a config costs a container, so the result is cached under
`.devstate/` keyed by both the anchor commit and the render environment. A
cache keyed on the commit alone would serve a developer-less config to the run
whose whole subject is the `[developer]` block.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FLOOR_FILE = REPO / "scripts" / "upgrade-floor"

# Where a captured config is cached. `.devstate/` is already gitignored and is
# already where this repo keeps derived local state.
CACHE_ROOT = REPO / ".devstate" / "upgrade"

# The entrypoint writes its config here, and the image reads it from here.
CONTAINER_CONFIG = "/data/config/config.toml"

# How long the anchor's entrypoint gets to reach the config write. Generous:
# it does the admin-allowlist write, the provisioning-flag source and the OCS
# room calls first, and under an emulated build every one of those is slow.
CAPTURE_TIMEOUT = 300

GIT_TIMEOUT = 120


class UpgradeHarnessError(RuntimeError):
    """The harness could not produce an artifact, for a reason it can name."""


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """A release to upgrade *from*."""

    ref: str
    commit: str

    @property
    def short(self) -> str:
        return self.commit[:12]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def read_floor(path: Path = FLOOR_FILE) -> str:
    """The one ref named by `scripts/upgrade-floor`.

    A single hand-edited line, deliberately: the floor is the *policy* about how
    far back an upgrade is expected to work, and a policy that a script computes
    is one nobody chose. Comments are allowed so the choice can carry its date
    and its reason.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise UpgradeHarnessError(f"cannot read the floor file {path}: {exc}") from exc

    refs = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not refs:
        raise UpgradeHarnessError(
            f"{path} names no ref. It is the supported upgrade span; the far "
            f"anchor has nothing to start from without it."
        )
    if len(refs) > 1:
        raise UpgradeHarnessError(
            f"{path} must name exactly one ref, found {len(refs)}: {refs}. "
            f"Taking the first would pin a floor nobody chose."
        )
    return refs[0]


def default_branch(repo: Path) -> str:
    """`origin/HEAD` where it is set, else a local `main`/`master`.

    Resolved rather than hardcoded because the merge-base anchor is only
    meaningful against the branch the work actually lands on.
    """
    head = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    for candidate in ("origin/main", "main", "origin/master", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").returncode == 0:
            return candidate
    raise UpgradeHarnessError(
        "cannot determine the default branch: no origin/HEAD, and neither main "
        "nor master resolves"
    )


def resolve_anchor(repo: Path, *, ref: str = "", floor: bool = False) -> Anchor:
    """Which release this run upgrades from.

    Three ways in, and they are not interchangeable. `--from <ref>` is for
    reproducing a specific report. `--from-floor` is the far anchor, the one
    that spans a month and is worth running before a release. The default is
    the merge-base with the default branch — the near anchor, which is the
    auto-update cron's span and is close to a no-op as a regression detector
    on its own, which is exactly why the far anchor exists.
    """
    if ref and floor:
        raise UpgradeHarnessError(
            f"both an explicit ref ({ref!r}) and --from-floor were given; "
            f"they name different anchors and there is no sensible order of "
            f"precedence between them"
        )

    if floor:
        wanted = read_floor()
    elif ref:
        wanted = ref
    else:
        base = _git(repo, "merge-base", "HEAD", default_branch(repo))
        if base.returncode != 0 or not base.stdout.strip():
            raise UpgradeHarnessError(
                f"cannot find a merge-base with {default_branch(repo)}: "
                f"{base.stderr.strip()}"
            )
        wanted = base.stdout.strip()

    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{wanted}^{{commit}}")
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise UpgradeHarnessError(
            f"{wanted!r} does not resolve to a commit in {repo}. A tag from "
            f"before a shallow clone's cutoff is the usual cause; "
            f"`git fetch --tags --unshallow` fixes that one."
        )
    return Anchor(ref=wanted, commit=resolved.stdout.strip())


# ---------------------------------------------------------------------------
# The capture cache
# ---------------------------------------------------------------------------


def capture_digest(env: dict[str, str]) -> str:
    """A stable fingerprint of the render environment.

    Sorted, because a dict's order is not part of what it means and a cache that
    thought otherwise would miss on every second call.
    """
    payload = json.dumps(env, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def capture_dir(root: Path, commit: str, digest: str) -> Path:
    return root / f"{commit[:12]}-{digest}"


# ---------------------------------------------------------------------------
# The Nextcloud stub
# ---------------------------------------------------------------------------

_ROOM_TOKEN = "stubroom"

# The exact byte sequence the entrypoint's readiness probe greps for:
#
#     curl -sf "${NC_URL}/status.php" | grep -q '"installed":true'
#
# A grep for a literal, against JSON — so the *serializer's* whitespace is part
# of the contract. `json.dumps` puts a space after the colon by default, the
# grep misses, and the probe spins for its full 120 seconds before rendering a
# config with empty room tokens. Measured: a capture that should take eight
# seconds took 121.9. Written out as bytes rather than built by `json.dumps`
# with separators, because the thing under contract is the text, and a literal
# is the only form of it that cannot be reformatted by accident.
STATUS_PHP_BODY = b'{"installed":true,"maintenance":false,"productname":"Nextcloud"}'

_OCS_ROOM = {
    "ocs": {
        "meta": {"status": "ok", "statuscode": 200, "message": "OK"},
        "data": {"token": _ROOM_TOKEN, "name": "stub", "type": 2},
    }
}


class _NextcloudHandler(BaseHTTPRequestHandler):
    """Exactly the two endpoints the entrypoint's readiness probe requires.

    Not a Nextcloud emulator. The entrypoint greps `status.php` for
    `"installed":true` *and* requires HTTP 200 from the OCS room endpoint;
    without both it spins for its full 120 seconds and then renders a config
    with empty room tokens. That config would still be usable, but the wait is
    paid on every cache miss and the log reads like a hang.

    Anything else is a 501 naming the path, as `testbed/services/gitlab.py`
    does — a newer release wanting more provisioning than this should say so
    rather than stall.
    """

    server_version = "IstotaNextcloudStub/1.0"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> None:
        # The body is read before *any* response goes out. The forge stub was
        # bitten by the other order: a reply sent before the body is consumed
        # leaves it in the connection buffer to be parsed as the next request
        # line, and the following request is answered out of the first one's
        # bytes.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        path = self.path.split("?", 1)[0]
        if path == "/status.php":
            self._send(200, STATUS_PHP_BODY, "application/json")
            return
        if path == "/ocs/v2.php/apps/spreed/api/v4/room":
            self._send(200, json.dumps(_OCS_ROOM).encode(), "application/json")
            return
        if path.startswith("/ocs/v2.php/apps/spreed/api/v4/room/"):
            # Participant adds, which follow a create.
            #
            # Not message posts: those go to `v1/chat/{token}`, which this stub
            # 501s on purpose — the entrypoint sends them with `-o /dev/null …
            # || true`, so a refusal there is invisible to it and irrelevant to
            # the captured config.
            #
            # Every answer here carries the same `data` dict, which has two
            # consequences worth knowing rather than discovering. The
            # entrypoint's `find_room_by_name` and `room_has_participant` both
            # test `isinstance(rooms, list)`, so a dict never matches and every
            # room is *created* rather than found; and since each create returns
            # the same token, the captured config has `log_channel ==
            # alerts_channel`. Neither matters to what this tier asserts — no
            # check reads a room token — and fixing it would mean modelling
            # Spreed's room list, which is a Nextcloud emulator by another name.
            self._send(200, json.dumps(_OCS_ROOM).encode(), "application/json")
            return

        self._send(
            501,
            f"the Nextcloud stub does not implement {path}\n".encode(),
            "text/plain",
        )

    do_GET = _route
    do_POST = _route
    do_PUT = _route
    do_DELETE = _route

    def log_message(self, *args) -> None:  # noqa: A003 - BaseHTTPRequestHandler's name
        """Silent. A capture writes a hundred of these into the pytest report."""


@dataclass
class StubServer:
    url: str
    port: int
    _server: ThreadingHTTPServer

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def serve_nextcloud_stub(host: str = "127.0.0.1") -> StubServer:
    """Start the stub on an ephemeral port.

    Loopback by default, so an ordinary `uv run pytest` of the unit tests below
    opens nothing on the network — the opt-in `testbed/services/model_endpoint.py`
    settled on after binding `0.0.0.0` on every run.

    `capture_config` does pass `0.0.0.0`, and has to: the container reaches the
    stub through `host.docker.internal`, which resolves to the host's bridge
    address rather than to loopback. So for the seconds a capture takes, this
    serves `status.php` and one canned OCS room token to anything that finds
    the ephemeral port. It holds no credential and answers 501 to everything
    else, which is why that is acceptable rather than merely tolerated.
    """
    server = ThreadingHTTPServer((host, 0), _NextcloudHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return StubServer(url=f"http://{host}:{port}", port=port, _server=server)


# ---------------------------------------------------------------------------
# The render environment
# ---------------------------------------------------------------------------


def render_env(*, nextcloud_url: str) -> dict[str, str]:
    """What the anchor's entrypoint is given, and nothing else.

    An explicit dict, never `**os.environ`. The entrypoint reads dozens of
    `ISTOTA_*` variables, so inheriting the developer's shell would make the
    captured config — and therefore the whole tier's verdict — depend on what
    happens to be exported in the terminal that started pytest.

    The `[developer]` block is switched on with a token because otherwise
    `doctor`'s `_dev_gate` and `_forge_token_gate` SKIP every `developer.*`
    check, and "no FAIL" over a set of SKIPs is the vacuous assertion this
    spec has found at every layer it built.

    What is deliberately *not* set is `ISTOTA_DEVELOPER_GH_BIN_PATH` and its
    glab twin. The volume shape's subject is that an old release rendered no
    such key and the dataclass default stood; pinning one here would
    manufacture the same WARN out of a value the harness chose, which says
    nothing about the release.
    """
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "NC_INTERNAL_URL": nextcloud_url,
        "USER_NAME": "upgradeuser",
        "USER_DISPLAY_NAME": "Upgrade User",
        "USER_TIMEZONE": "UTC",
        "USER_EMAIL": "upgradeuser@example.invalid",
        "BOT_USER": "istota",
        "BOT_PASSWORD": "bot-password-value",
        "USER_PASSWORD": "user-password-value",
        "ISTOTA_DEVELOPER_ENABLED": "true",
        "ISTOTA_DEVELOPER_REPOS_DIR": "/data/repos",
        "ISTOTA_DEVELOPER_GITLAB_URL": "https://gitlab.example.invalid",
        "ISTOTA_DEVELOPER_GITLAB_TOKEN": "upgrade-harness-forge-token",
        "ISTOTA_DEVELOPER_GITLAB_USERNAME": "istota-upgrade",
        "ISTOTA_DEVELOPER_GITLAB_DEFAULT_NAMESPACE": "istota-upgrade",
    }


# ---------------------------------------------------------------------------
# Capturing the anchor's config.toml
# ---------------------------------------------------------------------------


def export_tree(repo: Path, commit: str, subdir: str, dest: Path) -> Path:
    """`git archive` one directory of one commit into `dest`.

    An archive rather than a worktree or a checkout: the anchor is read-only
    here, a second worktree would need cleanup that a failed run would skip,
    and `git archive` cannot touch the caller's index.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit, "--", subdir],
        capture_output=True,
        timeout=GIT_TIMEOUT,
    )
    if archive.returncode != 0:
        raise UpgradeHarnessError(
            f"git archive {commit}:{subdir} failed: "
            f"{archive.stderr.decode(errors='replace').strip()}"
        )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        input=archive.stdout,
        capture_output=True,
        timeout=GIT_TIMEOUT,
    )
    if extract.returncode != 0:
        raise UpgradeHarnessError(
            f"extracting {commit}:{subdir} failed: "
            f"{extract.stderr.decode(errors='replace').strip()}"
        )
    return dest / subdir


# The line the entrypoint prints once the config file is complete. Watched for
# rather than polling the file, because before the Stage 4 extraction the
# render appended to `config.toml` incrementally — the file exists, and is a
# truncated fragment, from the first `cat >` onwards.
CONFIG_WRITTEN_MARKER = "Config written to"

# The wrapper the capture container runs. The anchor's entrypoint goes to
# completion or until the marker appears, whichever comes first; everything
# after the config write is Nextcloud workspace seeding and an `exec` into a
# scheduler that would never exit.
_CAPTURE_SCRIPT = f"""
set -u
mkdir -p /mnt/shared /data/config /data/repos

# The flag is written on the host and copied in, not composed here. A heredoc
# would have to choose between quoted — in which case `USER_NAME=${{USER_NAME}}`
# lands in the file literally and is expanded later by the entrypoint's own
# `source`, which happens to work and defers shell evaluation of every value to
# a point where a value carrying metacharacters is evaluated rather than read —
# and unquoted, which expands `$` inside the values themselves. `shlex.quote`
# on the host settles it once.
cp /flag/.istota-provisioned /mnt/shared/.istota-provisioned

"$1" > /tmp/entrypoint.log 2>&1 &
pid=$!

found=0
i=0
while [ "$i" -lt {CAPTURE_TIMEOUT * 5} ]; do
    if grep -q '{CONFIG_WRITTEN_MARKER}' /tmp/entrypoint.log 2>/dev/null; then
        found=1
        break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        break
    fi
    sleep 0.2
    i=$((i + 1))
done

kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true

# Re-check after the loop. The loop tests the marker *before* `kill -0`, so an
# entrypoint that writes the config and then exits between the two checks
# leaves `found=0` over a log that contains the marker — reported as "never
# reported 'Config written to'" with the line right there in the tail.
if [ "$found" -ne 1 ] && grep -q '{CONFIG_WRITTEN_MARKER}' /tmp/entrypoint.log 2>/dev/null; then
    found=1
fi

if [ "$found" -ne 1 ]; then
    echo "=== the anchor's entrypoint never reported '{CONFIG_WRITTEN_MARKER}' ===" >&2
    tail -n 60 /tmp/entrypoint.log >&2
    exit 1
fi

cp {CONTAINER_CONFIG} /out/config.toml
"""


def capture_config(
    *,
    repo: Path,
    anchor: Anchor,
    image: str,
    env: dict[str, str],
    cache_root: Path = CACHE_ROOT,
    platform: str = "",
    refresh: bool = False,
) -> Path:
    """The `config.toml` the anchor's release would have generated.

    Runs the anchor's own `entrypoint.sh` — not a reimplementation of it, and
    not the current one — in a container built from `image`. That the container
    is the *new* image is deliberate and load-bearing: nothing in the entrypoint
    executes istota code before the config write, so the anchor's own release
    image would produce a byte-identical file for a ten-minute build.

    Cached, because the container costs about ten seconds and the answer is a
    pure function of the commit and the environment.
    """
    # The image and the platform are part of the key, not just the environment.
    # The capture runs the anchor's *scripts* — those come from `git archive`
    # and are pinned by the commit — but the interpreter running them (bash,
    # python3, curl) is the container's, and an emulated amd64 run and a native
    # one would otherwise share one cache entry.
    digest = capture_digest({**env, "__image": image, "__platform": platform})
    destination = capture_dir(cache_root, anchor.commit, digest)
    cached = destination / "config.toml"
    if cached.exists() and not refresh:
        return cached

    stub = serve_nextcloud_stub(host="0.0.0.0")
    work = Path(tempfile.mkdtemp(prefix="istota-upgrade-"))
    try:
        entrypoint_dir = export_tree(repo, anchor.commit, "docker/istota", work / "tree")
        entrypoint = entrypoint_dir / "entrypoint.sh"
        if not entrypoint.exists():
            raise UpgradeHarnessError(
                f"{anchor.ref} has no docker/istota/entrypoint.sh, so there is "
                f"no release config to capture from it"
            )
        entrypoint.chmod(0o755)

        out = work / "out"
        out.mkdir()

        container_env = dict(env)
        container_env["NC_INTERNAL_URL"] = f"http://host.docker.internal:{stub.port}"

        flag_dir = work / "flag"
        flag_dir.mkdir()
        (flag_dir / ".istota-provisioned").write_text(
            _provisioning_flag(container_env)
        )

        argv = [
            "docker", "run", "--rm",
            "--add-host", "host.docker.internal:host-gateway",
            "--entrypoint", "/bin/bash",
            "-v", f"{entrypoint_dir}:/anchor:ro",
            "-v", f"{flag_dir}:/flag:ro",
            "-v", f"{out}:/out",
        ]
        if platform:
            argv += ["--platform", platform]
        # Credential-shaped values go as a bare `-e NAME` with the value handed
        # to docker through our own environment. `-e NAME=value` puts it in
        # argv, where any other user on the host reads it out of `ps`. Same rule
        # as `tests/image/conftest.py:run_in`.
        child_env = dict(os.environ)
        for name, value in container_env.items():
            if _looks_like_a_credential(name):
                child_env[name] = value
                argv += ["-e", name]
            else:
                argv += ["-e", f"{name}={value}"]
        argv += [image, "-c", _CAPTURE_SCRIPT, "capture", "/anchor/entrypoint.sh"]

        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=CAPTURE_TIMEOUT + 120,
            env=child_env,
        )
        produced = out / "config.toml"
        if result.returncode != 0 or not produced.exists():
            raise UpgradeHarnessError(
                f"capturing {anchor.ref}'s config.toml failed (exit "
                f"{result.returncode}).\n--- stdout ---\n"
                f"{_scrub(result.stdout, container_env)}\n--- stderr ---\n"
                f"{_scrub(result.stderr, container_env)}"
            )

        # Written through a `.partial` sibling and `os.replace`d into place, for
        # the same reason `render-config.sh` does it with the real config: the
        # only cache-validity test here is `cached.exists()`, so a Ctrl-C or a
        # full disk part-way through the copy leaves a truncated file that is
        # indistinguishable from a good one and gets served to every later run.
        # The symptom would be a doctor FAIL that reads as a code regression,
        # and the only escape is `--refresh`, which nobody reaches for.
        #
        # A sibling rather than a `mktemp`, because `os.replace` is only atomic
        # within a filesystem. `anchor.json` is written last and is the
        # completion marker for a human reading the cache directory.
        destination.mkdir(parents=True, exist_ok=True)
        partial = cached.with_name(cached.name + ".partial")
        shutil.copy2(produced, partial)
        os.replace(partial, cached)
        (destination / "anchor.json").write_text(
            json.dumps(
                {
                    "ref": anchor.ref,
                    "commit": anchor.commit,
                    "digest": digest,
                    "image": image,
                    "platform": platform or "native",
                },
                indent=2,
            )
            + "\n"
        )
        return cached
    finally:
        stub.close()
        # `ISTOTA_TEST_KEEP` is the tier-wide "leave the evidence" switch. There
        # are no containers to keep here — every run is `--rm` — but the export
        # of the anchor's entrypoint and the captured output are exactly what a
        # failed capture needs, and they are gone by the time the assertion is
        # read otherwise.
        if os.environ.get("ISTOTA_TEST_KEEP"):
            print(f"[upgrade] ISTOTA_TEST_KEEP: left the capture work dir at {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


# What `docker/nextcloud`'s provisioning writes and the entrypoint `source`s.
# The entrypoint reads `BOT_PASSWORD` from the container environment rather than
# from here, which is why it is not in this list.
_FLAG_KEYS = (
    "USER_NAME",
    "BOT_USER",
    "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET",
    "OAUTH_REDIRECT_URI",
)


def _provisioning_flag(env: dict[str, str]) -> str:
    """`/mnt/shared/.istota-provisioned`, as provisioning would have left it.

    The entrypoint `source`s this file, so every value is shell input. Quoting
    it here with `shlex.quote` is the only place that can be done once and
    correctly — a heredoc in the capture script has to pick between expanding
    `$` inside the values and not expanding the file's own placeholders, and
    both readings are wrong for at least one value.

    The OAuth keys are written empty on purpose: the capture has no Nextcloud
    to register a client against, and the entrypoint's `[web]` block is gated on
    them being non-empty, so an empty pair is the honest answer rather than a
    fabricated client id.
    """
    lines = []
    for key in _FLAG_KEYS:
        lines.append(f"{key}={shlex.quote(env.get(key, ''))}")
    return "\n".join(lines) + "\n"


# The same set `tests/image/conftest.py:_CREDENTIAL_NAME` uses. Kept identical
# on purpose: two credential filters that disagree mean the narrower one is a
# hole, and the difference shows up only on the run where a value leaks.
_CREDENTIAL_NAME = re.compile(
    r"(TOKEN|PASSWORD|SECRET|KEY|CREDENTIAL|PASSWD|API)", re.IGNORECASE
)


def _looks_like_a_credential(name: str) -> bool:
    return bool(_CREDENTIAL_NAME.search(name))


def _scrub(text: str, env: dict[str, str]) -> str:
    """Replace every credential-shaped value we supplied with its name.

    A failed capture renders the container's output into the pytest report, and
    the render environment carries four password-shaped values.
    """
    for name, value in env.items():
        if value and _looks_like_a_credential(name):
            text = text.replace(value, f"<{name}>")
    return text


RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"


def render_current_config(destination: Path) -> Path:
    """Today's `config.toml`, from the shipped render script on the host.

    The drift check's control needs a config that *should not* drift, and it
    must not come from an anchor: tying it to the code shape made it skip on
    exactly the run the spec names for verification (`--from-floor --shape
    volume`), and made `--from-floor --shape both` red by construction, because
    the override then anchors the code shape at the floor too.

    Rendered by the same script the container would run, as
    `tests/smoke/conftest.py` does, so the control is a real current config
    rather than a fixture's idea of one. The `[developer]` inputs mirror
    `render_env`'s so the two configs differ in the release that produced them
    and nothing else.
    """
    destination.mkdir(parents=True, exist_ok=True)
    config_file = destination / "config.toml"
    base = render_env(nextcloud_url="http://nextcloud")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "CONFIG_FILE": str(config_file),
        "USER_NAME": base["USER_NAME"],
        "NC_URL": "http://nextcloud",
        "APP_PASSWORD": base["BOT_PASSWORD"],
        "BOT_USER": base["BOT_USER"],
        "USER_TIMEZONE": "UTC",
    }
    for key, value in base.items():
        if key.startswith("ISTOTA_DEVELOPER_"):
            environment[key] = value

    result = subprocess.run(
        ["bash", str(RENDER_CONFIG)],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    if result.returncode != 0 or not config_file.exists():
        raise UpgradeHarnessError(
            f"render-config.sh exited {result.returncode} and the control has no "
            f"current config to compare against\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{_scrub(result.stderr, environment)}"
        )
    return config_file


# ---------------------------------------------------------------------------
# The anchor's database
# ---------------------------------------------------------------------------


def schema_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def anchor_schema_digest(repo: Path, commit: str) -> str:
    return schema_digest(read_anchor_schema(repo, commit))


_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def schema_tables(text: str) -> set[str]:
    return set(_CREATE_TABLE.findall(text))


def tables_added_since(repo: Path, commit: str) -> set[str]:
    """Tables HEAD's schema declares and the anchor's does not.

    This is the migration tier's *witness*. Comparing two `schema.sql` texts
    proves only that the files differ — both are git blobs, and neither has been
    anywhere near the container. What makes "migrations applied over the old
    database" an assertion rather than a hope is naming something that must
    exist in the upgraded file and could not have come from the seed.

    Derived rather than hardcoded, so it keeps working as the floor moves. It
    can legitimately come back empty — if the floor is bumped to a commit whose
    schema matches HEAD's — and the caller has to treat that as "nothing to
    assert" rather than as a pass.
    """
    return schema_tables((repo / "schema.sql").read_text()) - schema_tables(
        read_anchor_schema(repo, commit)
    )


def read_anchor_schema(repo: Path, commit: str) -> str:
    shown = _git(repo, "show", f"{commit}:schema.sql")
    if shown.returncode != 0:
        raise UpgradeHarnessError(
            f"{commit[:12]} has no schema.sql: {shown.stderr.strip()}"
        )
    return shown.stdout


# Rows for the migrations to have something to rewrite. An empty database
# exercises the DDL half of a migration and nothing else, which is the half
# least likely to be wrong.
#
# Deliberately small and deliberately introspected: hardcoding a column list
# would break the moment the anchor moves, and it would break as "the upgrade
# tier is red" rather than as "the seed needs updating".
_SEED_TASKS = 3


def build_anchor_db(repo: Path, commit: str, destination: Path) -> Path:
    """A database as the anchor's release would have created it.

    `db.init_db` is `_run_migrations(conn)` followed by
    `executescript(schema.sql)`. On a database with no tables the migration half
    has nothing to find, so applying the anchor's `schema.sql` alone *is* the
    anchor's fresh schema — not an approximation of it.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    schema = read_anchor_schema(repo, commit)
    with sqlite3.connect(destination) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(schema)
        _seed(conn)
    return destination


def _seed(conn: sqlite3.Connection) -> None:
    """Insert a few tasks, supplying whatever the anchor's schema demands.

    Column-driven rather than column-listed. The point of the seed is "rows
    exist", and a hardcoded insert would turn every schema change between here
    and the floor into a red upgrade tier with a misleading message.
    """
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(tasks)")}
    if not columns:
        raise UpgradeHarnessError(
            "the anchor's schema.sql declares no `tasks` table, so this is not "
            "an istota database and the seed has nothing to write to"
        )

    supplied = {
        "user_id": "upgradeuser",
        "prompt": "a task the previous release completed",
        "status": "completed",
        "source_type": "scheduled",
        "conversation_token": "upgradetoken",
        "result": "done",
    }

    names: list[str] = []
    for name, info in columns.items():
        _cid, _name, ctype, notnull, default, primary = info

        # Skip only the rowid alias — `INTEGER PRIMARY KEY`, which SQLite fills
        # in. `primary` is the column's position in the primary key, so a bare
        # `if primary: continue` also skips every member of a *composite* key,
        # and those do have to be supplied. Today's `tasks` has neither shape
        # beyond the alias, so this branch is about not being wrong later.
        if primary == 1 and "INTEGER" in (ctype or "").upper():
            continue

        if name in supplied:
            names.append(name)
            continue

        if primary or (notnull and default is None):
            # A column the anchor requires and this seed has no opinion about.
            # Filled with something type-appropriate rather than skipped, so a
            # new NOT NULL column does not break the seed.
            #
            # Best-effort, and worth saying so: the value is guessed from type
            # affinity alone, so a `NOT NULL ... CHECK (x IN (...))` column
            # would get an empty string the check rejects. If that happens the
            # insert raises here, in the harness, naming the column — which is
            # the right place for it, rather than a confusing red upgrade tier.
            supplied[name] = 0 if "INT" in (ctype or "").upper() else ""
            names.append(name)

    placeholders = ", ".join("?" for _ in names)
    statement = f"INSERT INTO tasks ({', '.join(names)}) VALUES ({placeholders})"
    for index in range(_SEED_TASKS):
        values = []
        for name in names:
            value = supplied[name]
            if name == "prompt":
                value = f"{value} #{index}"
            values.append(value)
        conn.execute(statement, values)
