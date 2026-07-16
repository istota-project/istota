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

# referenceId prefix stamped on a web-origin user turn the web process posted
# into Talk *as the user* (post-as-user mirroring). The Talk poller drops any
# message carrying it — the marker travels inside the Talk message itself, so
# the echo check is race-free against the external-id stamp write. Shared by
# transport.talk.inbound (the skip) and web_app (the post).
WEBMIRROR_REF_PREFIX = "istota:webmirror:"

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .. import db


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


@dataclass(frozen=True)
class DeliveryOptions:
    """Optional per-delivery metadata carried alongside ``deliver(target, text)``.

    Push surfaces that don't use these ignore them; ``NtfyTransport.deliver``
    reads ``title`` / ``priority`` / ``tags``. Kept a typed object (rather than
    untyped ``**extra``) so the protocol change is one explicit field set.
    """

    title: str | None = None
    priority: int | None = None
    tags: str | None = None


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
