"""Transport abstraction — a uniform seam over Istota's messaging surfaces.

Inbound, a ``Transport`` normalizes a surface's messages into
``IncomingMessage`` via ``poll``; ``ingest_message`` turns those into tasks.
Outbound, ``deliver`` / ``edit`` push a task's result to a resolved channel.
``TransportRegistry`` holds the enabled transports and resolves one per task.

Two concrete transports ship: ``TalkTransport`` and ``EmailTransport``. Matrix
and web chat are the designed-for next consumers — adding one is a new
``Transport`` subclass plus a line in ``make_registry``. See
``.claude/rules/transport.md`` for the "how to add a transport" guide.
"""

from ._types import (
    DeliveryOptions,
    IncomingMessage,
    Transport,
    TransportCapabilities,
)
from .email import EmailTransport
from .ingest import ingest_message
from .registry import TransportRegistry, make_registry
from .routing import (
    Destination,
    parse_output_target,
    plan_has_surface,
    resolve_delivery_plan,
)
from .talk import TalkTransport
from .web import WebTransport

__all__ = [
    "IncomingMessage",
    "Transport",
    "TransportCapabilities",
    "DeliveryOptions",
    "TransportRegistry",
    "make_registry",
    "ingest_message",
    "Destination",
    "parse_output_target",
    "plan_has_surface",
    "resolve_delivery_plan",
    "TalkTransport",
    "EmailTransport",
    "WebTransport",
]
