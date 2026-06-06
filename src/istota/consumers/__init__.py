"""Task-event consumers.

Each consumer implements ``EventSubscriber`` (``on_event`` / ``on_finish``) and
formats the shared ``TaskEvent`` stream for one output surface. In-process
subscribers (Talk, log channel, push) are registered on the ``EventWriter`` by
the scheduler. The web SSE and admin consumers are NOT in-process subscribers —
they poll the ``task_events`` table directly (see ``web_app``).
"""

from .log_channel import LogChannelSubscriber
from .push import PushNotificationSubscriber
from .talk import TalkEventSubscriber

__all__ = [
    "TalkEventSubscriber",
    "LogChannelSubscriber",
    "PushNotificationSubscriber",
]
