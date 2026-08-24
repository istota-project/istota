"""What a scenario declares it needs, and what a stack is keyed by.

A **profile** is a named shape plus the set of services it runs plus any extra
config. Profiles rather than one stack with everything enabled: a stack with
every subsystem on would have the daemon polling mail, feeds and Talk during
every unrelated test, which makes the quiesce wait the dominant cost and couples
every test to every background loop. Profiles rather than today's per-test
stack, for the arithmetic — a per-test `up`/`down --volumes` is about twelve
seconds on the lean shape and minutes on the full one, and six subsystems on
that model produces a tier nobody runs.

There is no `backend` field and no `LOCAL` profile. The two storage backends
differ in exactly three things — the prompt's storage vocabulary, the skill
menu, and whether `runtime.mount_liveness` runs — and all three are pure
functions of a `Config`, so they are witnessed by the prompt goldens and two
unit tests rather than by any stack. Naming the axis here anyway is what stops
someone adding a Nextcloud stub to the lean shape for an unrelated reason and
silently deleting that coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: The compose overlay that runs the mail container, on either shape.
#:
#: A literal path rather than an import from `services.mail`, which would make
#: this module import a service module and close a cycle: `services/__init__`
#: is what resolves a profile's service names.
MAIL_OVERLAY = Path(__file__).resolve().parent / "compose" / "mail" / "mail.yml"


@dataclass(frozen=True)
class Profile:
    """A stack shape plus the services pointed at it."""

    name: str
    """The pool key. Two profiles sharing a name would share a stack."""

    shape: Literal["lean", "full"] = "lean"
    """Which compose file the stack boots from.

    `lean` is one container, no Nextcloud, entrypoint bypassed, config rendered
    on the host: about thirty seconds to healthy. `full` is the deployment as
    shipped — postgres, redis, nextcloud, istota, web, nginx — booted through
    `entrypoint.sh` with the generator running inside the container where it
    does in production, and around ten minutes to healthy on a cold volume set.
    """

    services: tuple[str, ...] = ("model",)
    """Registry names, resolved through `services.REGISTRY`.

    Every profile carries `model`: a stack with no scripted endpoint has no
    deterministic task, and every scenario in the tier runs one.
    """

    config: dict[str, str] = field(default_factory=dict)
    """Extra `ISTOTA_*` variables, merged after every service's `config_env()`.

    For an axis that is a config value rather than a service — a rate limit
    lowered so a suite of twenty messages from one sender is not throttled, say.
    Held to the same rule as `config_env`: only variables the shipped generator
    reads and `docker-compose.yml` passes through.
    """

    image: str = ""
    """A prebuilt image tag; empty means build from the checkout.

    Non-empty selects the prebuilt compose overlay. The image is a compose-level
    property, so a scenario needing a *different* image is a different profile
    rather than a flag on an existing one.
    """

    compose_overlays: tuple[Path, ...] = ()
    """Extra `-f` files, merged over the shape's base in order."""


#: Both mail profiles poll every five seconds rather than every sixty.
#:
#: `ISTOTA_SCHEDULER_EMAIL_POLL_INTERVAL` is read by `render-config.sh` and
#: passed through by `docker-compose.yml`, so this is a legitimate wiring rather
#: than a fixture reaching past the generator. Sixty would put a minute of dead
#: wait into every mail scenario, which on a session-scoped stack is a minute
#: per test rather than one per session.
#: And they give the stack's one user an address of their own.
#:
#: `USER_EMAIL` is read by `render-config.sh` and passed through by
#: `docker-compose.yml`, so it satisfies the two-file rule like everything else
#: here; it is not `ISTOTA_`-prefixed because it belongs to the identity block
#: rather than to a module. Without it `config.users["testuser"]` has no address
#: and neither the sender-match rung nor the plus-address rung can resolve —
#: `extract_user_from_recipient` requires the tag to name a user that exists.
#:
#: `@ext.test` rather than `@bot.test`: the mail server collapses every
#: recipient at the bot's own domain into the bot mailbox, so a user address
#: inside it would make the bot the recipient of its own replies.
MAIL_CONFIG = {
    "ISTOTA_SCHEDULER_EMAIL_POLL_INTERVAL": "5",
    "USER_EMAIL": "testuser@ext.test",
}

BASE = Profile("base")
FORGE = Profile("forge", services=("model", "gitlab"))

# ntfy needs no config at all: it is a per-user connected service in the
# encrypted secrets store rather than a config block, so the scenario points
# the daemon at the stub with `istota secret ensure` inside the container. See
# `services/ntfy.py::NtfyService.config_env` for why that is not a gap.
NOTIFY = Profile("notify", services=("model", "ntfy"))

#: The module switch lives here rather than in `feeds.config_env()`.
#:
#: `ISTOTA_FEEDS_ENABLED` says the *module* is on. That is a property of the
#: profile — it is what `FULL_MODULE_SWITCHES` derives from a profile's service
#: list on the other shape — and not something the document server knows or
#: could answer for. Keeping it out of `config_env()` also keeps that method's
#: promise true: the feeds stub is pointed at by seeded DB rows and by nothing
#: the generator reads.
#:
#: The two-file rule still applies and still holds: `render-config.sh:534`
#: reads it and `docker-compose.yml:333` passes it through, which the
#: `Profile.config` guard in `tests/test_testbed_services.py` checks.
FEEDS = Profile(
    "feeds",
    services=("model", "feeds"),
    config={"ISTOTA_FEEDS_ENABLED": "true"},
)

# The negative control: the same profile on an image with the forge binaries
# removed, reproducing ISSUE-263. The tag is empty here and filled in by the
# fixture that builds the control, because it is derived from whatever the
# session's real image turned out to be — there is no constant to write down.
NO_FORGE = Profile("no-forge", services=("model", "gitlab"))

# There is no `cache` profile, and the reason it went is the reason it existed.
# It was `forge` plus `ISTOTA_SECURITY_SANDBOX_CACHE_DIR`, kept separate so the
# cache bind stayed out of every other forge scenario. The daemon now derives
# the cache from `developer.repos_dir` instead — `{repos_dir}/{user_id}/`
# `.package-caches`, per user, inside the subtree the sandbox binds — and does
# not read that key at all while `repos_dir` is set. So the profile's one
# variable was inert, and every forge stack carries the cache bind anyway, which
# leaves nothing for a second name to key on. `tests/smoke/`
# `test_sandbox_repos_isolation.py` runs on `forge`.

# Exactly one full profile, and the asymmetry with the lean shape above is
# deliberate. The argument for fine-grained profiles — that a stack with every
# subsystem enabled has the daemon polling mail, feeds and Talk during every
# unrelated test — is an argument about a thirty-second boot. It inverts at ten
# minutes: `StackPool` keys by profile name, so `full` and `full-mail` would be
# two cold boots of the same six containers to run four scenarios. One profile
# is where the tier spends its cold boot, and the extra poller is what the
# watermark discipline absorbs.
#
# And it carries mail, because a second full profile would be a second cold
# boot of the same six containers to run one attachment scenario.
#: And it runs the self-claim gate in `verify`, which the lean profile does not.
#:
#: Two reasons, and the first is about being able to fail. `render-config.sh`
#: defaults `confirm_sender_match` to `off`, so a profile that also asked for
#: `off` would render the same line whether or not `docker-compose.yml` passed
#: the variable through — and the assertion that the passthrough works would
#: hold against the reverted compose file. `verify` is a value the shell default
#: is not.
#:
#: The second is that `verify` is the mode worth exercising on the shape that
#: has a real boot behind it: it needs `authserv_id` set, refuses to start
#: without it, and decides between a message that runs and one that is held on
#: the strength of a header. That is two of this stage's settings interacting,
#: which is exactly what neither could do before compose passed them.
FULL_CONFIG = {**MAIL_CONFIG, "ISTOTA_EMAIL_CONFIRM_SENDER_MATCH": "verify"}

FULL = Profile(
    "full",
    shape="full",
    services=("model", "nextcloud", "mail"),
    config=FULL_CONFIG,
    compose_overlays=(MAIL_OVERLAY,),
)

# The lean deployed mail path: a real mail server, a real daemon polling it, and
# no Nextcloud. `poll_emails` needs none for attachment-free mail, so everything
# except the attachment upload is reachable thirty seconds after `up` rather
# than a minute.
MAIL = Profile(
    "mail",
    services=("model", "mail"),
    config=MAIL_CONFIG,
    compose_overlays=(MAIL_OVERLAY,),
)

#: Every profile this package defines, for the guard that checks each one names
#: services that exist. A profile absent from here is invisible to that check,
#: so add to it when adding a profile.
ALL: tuple[Profile, ...] = (BASE, FORGE, NO_FORGE, NOTIFY, FEEDS, MAIL, FULL)


def by_name(name: str) -> Profile:
    """The profile a test declared, or a message naming the ones that exist.

    A test declares its profile as a *string* (`@pytest.mark.profile("forge")`)
    so a scenario file needs no import from this package. The cost is that a
    typo is only caught here, so it is caught loudly: the alternative is a
    `KeyError` raised inside a session-scoped fixture, which pytest reports as
    an error on every test in the profile.
    """
    for profile in ALL:
        if profile.name == name:
            return profile
    raise KeyError(
        f"no profile named {name!r}; testbed.profiles defines "
        f"{[profile.name for profile in ALL]}"
    )
