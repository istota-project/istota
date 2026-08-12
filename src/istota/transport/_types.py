"""Core transport types: the normalized inbound message, capability
descriptors, and the bidirectional ``Transport`` protocol.

A transport is the seam between Istota's surface-agnostic core (the scheduler,
the event-stream consumers, the notification dispatcher) and one concrete
messaging surface (Nextcloud Talk, email, and — designed-for but not built
here — Matrix and web chat). Inbound, a transport's ``poll`` normalizes the
surface's messages into ``IncomingMessage``; outbound, ``deliver`` / ``edit``
push a task's result back to a resolved channel.

``conversation_token`` keeps its name and stays opaque at every consumer — it
is the per-surface channel id. ``source_type`` stays the routing key. See the
transport-abstraction spec for why neither is renamed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .. import db

# referenceId prefix stamped on a web-origin user turn the web process posted
# into Talk *as the user* (post-as-user mirroring). The Talk poller drops any
# message carrying it — the marker travels inside the Talk message itself, so
# the echo check is race-free against the external-id stamp write. Shared by
# transport.talk.inbound (the skip) and web_app (the post).
WEBMIRROR_REF_PREFIX = "istota:webmirror:"


@dataclass
class IncomingMessage:
    """A surface-normalized inbound message, ready to become a task.

    A transport's ``poll()`` yields one of these per message that SHOULD create
    a task. Messages the transport handles internally (commands, confirmation
    replies, mentions it chose to ignore) are not emitted.

    The field-to-column mapping is the contract ``ingest_message`` relies on:
    ``channel_token`` → ``Task.conversation_token``, ``delivery_token`` →
    ``Task.talk_delivery_token``, ``platform_message_id`` →
    ``Task.talk_message_id``, ``reply_to_message_id`` → ``Task.reply_to_talk_id``.
    """

    user_id: str                      # resolved Istota user id
    text: str                         # cleaned prompt text
    source_type: str                  # "talk" | "email" | ... -> Task.source_type
    surface: str                      # transport.name; the delivery surface
    channel_token: str | None         # -> Task.conversation_token (opaque)
    channel_name: str | None = None   # room display name (for lazy room registration)
    delivery_token: str | None = None  # -> Task.talk_delivery_token (resolved room)
    platform_message_id: int | None = None      # -> Task.talk_message_id
    reply_to_message_id: int | None = None       # -> Task.reply_to_talk_id
    reply_to_content: str | None = None
    attachments: list[str] = field(default_factory=list)
    is_group_chat: bool = False
    output_target: str | None = None  # "talk"|"email"|"ntfy"|comma list|None
    model: str | None = None          # !model override (canonical id)
    effort: str | None = None
    # True when the message carried an explicit `!model` prefix (any alias,
    # including `!model default` which resolves to no override). Suppresses the
    # per-room model default in record_inbound so an explicit per-message choice
    # — including an explicit "use the instance default" — always wins.
    model_prefix_used: bool = False
    # Withhold this turn from the canonical transcript even when its token
    # resolves to a real room. Set by the email poller for a message facing the
    # untrusted-sender confirmation gate: publishing attacker-supplied text into
    # the user's room before they approve it — and leaving it there when they
    # decline — is exactly what the gate exists to prevent.
    suppress_transcript_mirror: bool = False
    # Whether this turn belongs in the resolved transcript room *at all*. The
    # sibling above is a hold — the turn belongs there and is published once
    # approved (``confirmations.approve``); this is a permanent answer with no
    # restore path, so the two must not be collapsed into one flag. Set False by
    # the email poller for a thread reply the user sent from their own address:
    # they are on the email surface by demonstration, so a room copy only
    # duplicates the exchange and re-bills it as context on every later task in
    # that room (ISSUE-254). Applies only to a non-room surface — a Talk or web
    # message is a turn in its own room by construction.
    mirror_to_room: bool = True
    # The message's own sender, as the surface reported it, when that is not
    # simply ``user_id`` — today only email's envelope sender, which names the
    # person who *wrote* the mail rather than the istota user it was routed to.
    # Raw and untrusted: ``record_inbound`` sanitizes it through
    # ``db.external_email_sender`` before it reaches ``messages.author_label``.
    sender_address: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # original payload


@dataclass(frozen=True)
class TransportCapabilities:
    """What a surface can do — drives capability-gated wiring in the scheduler.

    The scheduler subscribes the progress-ack consumer only when
    ``supports_progress_ack`` is True, and splits long messages only when
    ``max_message_length`` is set. A new surface declares its capabilities once
    and the core does the right thing without a ``source_type ==`` check.

    ``surface_class`` is the routing dimension the delivery planner reads:

    - ``"push"`` — the daemon actively delivers via ``Transport.deliver()``
      (Talk, email, ntfy, istota_file, future Matrix). Requires a durable
      channel target resolved before delivery.
    - ``"stream"`` — outbound is the ``task_events`` log; the client tails it.
      ``deliver()`` is a no-op. REPL and web chat. A ``stream`` destination
      contributes no push work; the ``result``/``error``/``done`` events satisfy
      it.

    ``room_view`` is a *second, orthogonal* routing dimension: whether the
    surface is a view of a shared room, and if so where that view's transcript
    is stored. It answers "does a message written into this room still need to
    be pushed here?", which ``surface_class`` cannot — that one describes how
    outbound works for a task's *own* result.

    - ``"external"`` — the surface is a room view whose transcript lives in a
      store Istota does not own (Talk's is in Nextcloud). Writing the canonical
      ``messages`` row does not put the message in front of this surface's
      users, so a room fan-out must push to it.
    - ``"canonical"`` — the surface is a room view rendered *from* the canonical
      ``messages`` store (web chat). Writing the row **is** the delivery; a push
      would render the same message twice.
    - ``None`` — not a room view at all (email, ntfy, istota_file, repl). These
      are delivery targets; a room fan-out never mirrors to them.

    The two axes are independent and must not be conflated. Web is
    ``surface_class="stream"`` *and* ``room_view="canonical"`` today, which is a
    coincidence of having exactly one room-view stream surface — a surface that
    is one but not the other is what the split exists to keep correct.

    ``user_routable`` marks a surface as one a user can deliberately point
    traffic at (a briefing output, a default destination, an alert route). The
    self-routing surfaces are False: ``istota_file`` only ever delivers back to
    the TASKS.md line a task came from (no row → dropped), and ``repl`` is the
    inline terminal the daemon never delivers to. Both still validate on the
    wire and work as programmatic destinations — ``user_routable`` only governs
    what the UI *offers*, never what the grammar permits.
    """

    supports_edit: bool = False          # can edit a previously sent message
    supports_threading: bool = False     # reply-to + @mention semantics
    supports_progress_ack: bool = False  # an editable ack message during run
    supports_typing: bool = False
    max_message_length: int | None = None  # None = unlimited; drives splitting
    surface_class: str = "push"          # "push" | "stream"
    user_routable: bool = True           # can a user select it as a destination
    # Room view + where that view's transcript lives; None = not a room view.
    room_view: Literal["external", "canonical"] | None = None


@dataclass(frozen=True)
class DeliveryOptions:
    """Optional per-delivery metadata carried alongside ``deliver(target, text)``.

    Push surfaces that don't use these ignore them; ``NtfyTransport.deliver``
    reads ``title`` / ``priority`` / ``tags`` / ``markdown``. Kept a typed
    object (rather than untyped ``**extra``) so the protocol change is one
    explicit field set.

    ``markdown`` asks the surface to render the body as markdown rather than
    literal text. Opt-in, because a plain-text body routinely contains ``*``,
    ``_`` and ``#`` that a renderer would eat — a default-on flag would silently
    rewrite every existing notification.
    """

    title: str | None = None
    priority: int | None = None
    tags: str | None = None
    markdown: bool = False


@runtime_checkable
class Transport(Protocol):
    """Bidirectional surface adapter. See module docstring for the model."""

    name: str                       # "talk", "email", "matrix", "web"
    capabilities: TransportCapabilities

    async def poll(self) -> list[IncomingMessage]:
        """Fetch new messages. The transport keeps its own cadence/driver;
        this is called by that driver (Talk's long-poll thread, email's
        interval poll). Returns only task-producing messages."""
        ...

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
        options: "DeliveryOptions | None" = None,
    ) -> int | None:
        """Send a result/message to a target channel. Handles splitting,
        formatting, and threading per the surface. Returns the last posted
        platform message id (None if the surface has no id concept).

        ``task`` is optional and ignored by surfaces that don't need it; email
        uses it for the deferred-output / ``ProcessedEmail`` lookup and Talk
        uses it for group-chat reply-threading + @mention (the "task-aware
        deliver" decision). ``options`` carries surface-specific metadata
        (ntfy title/priority/tags); surfaces that don't use it ignore it."""
        ...

    async def edit(self, target: str, message_id: int, text: str) -> None:
        """Edit a previously sent message. No-op if not supports_edit."""
        ...

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        """Download an inbound attachment to local_path. No-op if the surface
        has no attachment-download concept."""
        ...

    def resolve_target(self, task: "db.Task") -> str | None:
        """Resolve the channel to deliver a task's result to. Returns None if
        the surface can't deliver this task."""
        ...
