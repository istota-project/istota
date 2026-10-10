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
MAIL_WEB_OVERLAY = MAIL_OVERLAY.with_name("web.yml")


@dataclass(frozen=True)
class Profile:
    """A stack shape plus the services pointed at it."""

    name: str
    """The pool key. Two profiles sharing a name would share a stack."""

    shape: Literal["lean", "full"] = "lean"
    """Which compose file the stack boots from.

    `lean` is one container, no Nextcloud, entrypoint bypassed: about thirty
    seconds to healthy. `full` is the deployment as shipped — postgres, redis,
    nextcloud, istota, web, nginx — booted through `entrypoint.sh`, and a minute
    or so to healthy on a cold volume set. Both boot from a `config.toml` the
    testbed writes, since the config is an input on every shape.
    """

    services: tuple[str, ...] = ("model",)
    """Registry names, resolved through `services.REGISTRY`.

    Every profile carries `model`: a stack with no scripted endpoint has no
    deterministic task, and every scenario in the tier runs one.
    """

    config: dict = field(default_factory=dict)
    """A `config.toml` fragment, merged after every service's `config()`.

    For an axis that is a config value rather than a service — a poll interval
    shortened so a mail scenario does not wait a minute, say. It may override a
    service's key; two services may not set the same one.
    """

    image: str = ""
    """A prebuilt image tag; empty means build from the checkout.

    Non-empty selects the prebuilt compose overlay. The image is a compose-level
    property, so a scenario needing a *different* image is a different profile
    rather than a flag on an existing one.
    """

    compose_overlays: tuple[Path, ...] = ()
    """Extra `-f` files, merged over the shape's base in order."""

    compose_profiles: tuple[str, ...] = ()
    """Compose profiles to activate, as `--profile` arguments.

    Distinct from this class's own `name`, which is a *testbed* profile and
    keys the pool. A compose profile is the shipped file's mechanism for a
    service that is declared but not started by default — `browser`,
    `location`, and now `signaling` — and activating one is what makes the tier
    boot the file an operator boots rather than substituting a harness file for
    it. It rides in the argument list beside `--project-name`, so `ps`, `logs`
    and `down` see the same service set `up` did; a profile passed only to `up`
    leaves a running container that `down` does not know to remove.
    """


#: Both mail profiles poll every five seconds rather than every sixty, which
#: would otherwise put a minute of dead wait into every mail scenario on a
#: session-scoped stack.
#:
#: And they give the stack's one user an address of their own. Without it
#: `config.users["testuser"]` has no address and neither the sender-match rung
#: nor the plus-address rung can resolve — `extract_user_from_recipient`
#: requires the tag to name a user that exists.
#:
#: `@ext.test` rather than `@bot.test`: the mail server collapses every
#: recipient at the bot's own domain into the bot mailbox, so a user address
#: inside it would make the bot the recipient of its own replies.
MAIL_CONFIG: dict = {
    "scheduler": {"email_poll_interval": 5},
    "users": {"testuser": {"email_addresses": ["testuser@ext.test"]}},
}

BASE = Profile("base")
FORGE = Profile("forge", services=("model", "gitlab"))

# ntfy needs no config at all: it is a per-user connected service in the
# encrypted secrets store rather than a config block, so the scenario points
# the daemon at the stub with `istota secret ensure` inside the container. See
# `services/ntfy.py::NtfyService.config` for why that is not a gap.
NOTIFY = Profile("notify", services=("model", "ntfy"))

#: No config: the feeds module is on by default (modules are opt-out), and the
#: stub is pointed at by seeded DB rows rather than by anything in config.toml.
FEEDS = Profile("feeds", services=("model", "feeds"))

#: The signaling server, run for real, driven from the harness.
#:
#: The honest limit of this profile is that it cannot exercise istota's own
#: authentication at all: hello-v2 needs a Nextcloud to mint and sign a JWT and
#: to publish the public key the server verifies it against, and there is no
#: Nextcloud on the lean shape. `[talk.signaling] enabled` is therefore
#: *not* set here — the daemon's `require_hpb` refusal would stop the container
#: booting — so the daemon in this stack is a bystander and the scenario drives
#: istota's protocol module against the container itself.
#:
#: What that buys is everything the full tier is too slow and too coarse for:
#: the `welcome` feature negotiation against a real server, the `chat-relay`
#: gate and its refresh-only fallback, real relayed frames through
#: `parse_event`, a server restart mid-session, resume versus a fresh hello, and
#: an idle connection outliving the server's 60-second read deadline. Six to
#: nine seconds a boot against fifty to eighty-four.
SIGNALING = Profile(
    "signaling",
    services=("model", "signaling"),
    compose_profiles=("signaling",),
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
#: `verify` is the mode worth exercising on the shape that has a real boot
#: behind it: it needs `authserv_id` set, refuses to start without it, and
#: decides between a message that runs and one that is held on the strength of
#: a header. And it is not the default, so an assertion that it reached the
#: daemon can fail.
#: And it reconciles rooms every 30 seconds rather than every 300.
#:
#: That interval is two things at once and the scenarios read it both ways. It
#: is the worst-case delay before a room created mid-session gets a watcher, so
#: 300 would mean five minutes of dead wait in any test that makes a room. And
#: it is the safety net — the reconciler compares each room's `lastMessage.id`
#: against the stored cursor and fetches the rooms that are behind — so it is
#: also the *slow* path a delivery assertion has to out-run to mean anything.
#: 30 keeps both usable: a watcher inside half a minute, and a delivery
#: assertion with a window well inside it that only the event stream can meet.
#:
#: Lower would be worse, not better. At 5 or 10 seconds the safety net would
#: deliver almost as fast as the stream and no latency assertion could tell the
#: two apart, which is the failure this tier has documented eight times.
FULL_CONFIG: dict = {
    **MAIL_CONFIG,
    "web": {"auth": ["nextcloud", "email"]},
    "email": {"confirm_sender_match": "verify"},
    "talk": {"signaling": {"room_sync_interval": 30}},
}

#: And it carries signaling, on the same arithmetic and for a reason of its own:
#: this is the *only* shape that can answer anything about hello-v2,
#: `participants/active` or Nextcloud's authorization, because all three need a
#: real Talk behind them. A second full profile would be a second cold boot of
#: the same six containers to run one chain.
#:
#: And ntfy, for `tests/full/test_email_rooms.py`: a push the product confines
#: to ntfy and email (`notifications.store.ROOM_FREE_SURFACES`) can only be told
#: apart from one on the whole alert route where that route also names a Talk
#: room, which is the one thing the lean `email` profile has no way to offer.
#: The stub runs in the pytest process and the daemon reaches it only through
#: the per-user secret the email seeding writes. Until a test seeds it, nothing
#: pushes there; after, testuser's alerts reach it for the rest of the session.
FULL = Profile(
    "full",
    shape="full",
    services=("model", "nextcloud", "mail", "signaling", "ntfy"),
    config=FULL_CONFIG,
    compose_overlays=(MAIL_OVERLAY, MAIL_WEB_OVERLAY),
    compose_profiles=("signaling",),
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

#: The email suite's lean profiles: mail, the model and an ntfy stub, with the
#: self-claim gate in `verify`.
#:
#: `verify` rather than `off`, because it is the mode in which a header decides
#: between running and holding, and `off` is already `mail`'s. So mail the
#: stack's own user sends from their own address must carry a passing
#: `Authentication-Results` stamp (`tests/support/email_flow.py`), or it is
#: held. ntfy because the suite asserts what every push carries, and a profile
#: without the stub could only count them.
EMAIL_CONFIG: dict = {
    **MAIL_CONFIG,
    "email": {"confirm_sender_match": "verify"},
}
EMAIL = Profile(
    "email",
    services=("model", "mail", "ntfy"),
    config=EMAIL_CONFIG,
    compose_overlays=(MAIL_OVERLAY,),
)
#: The same, with every outbound mail held for approval. A profile rather than
#: a runtime flip, because a lean boot is seconds and a config change reaching
#: a running daemon is not something a test can observe.
EMAIL_HOLD_ALL = Profile(
    "email-hold-all",
    services=("model", "mail", "ntfy"),
    config={
        **EMAIL_CONFIG,
        "email": {**EMAIL_CONFIG["email"], "outbound_approval_floor": "all"},
    },
    compose_overlays=(MAIL_OVERLAY,),
)

#: Every profile this package defines, for the guard that checks each one names
#: services that exist. A profile absent from here is invisible to that check,
#: so add to it when adding a profile.
ALL: tuple[Profile, ...] = (
    BASE,
    FORGE,
    NO_FORGE,
    NOTIFY,
    FEEDS,
    MAIL,
    EMAIL,
    EMAIL_HOLD_ALL,
    SIGNALING,
    FULL,
)


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
