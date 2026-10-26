"""A sliding-window hit counter per key, in bounded memory.

For the public routes that need an in-memory limit and have no
authoritative table behind them: the calendar feed and the OCM endpoints.
The login throttle in `webui/app.py` is the same shape but is not this: it
is keyed on email and address, tied to `web_auth.Policy`, gives a reservation
back on success, and sits in front of the `web_auth_attempts` table.

Keys are kept in least-recently-used order and the oldest is dropped past
``max_keys``, so a caller rotating keys cannot grow memory. That also means
it can push another key's history out; a key's own limit still holds while
it is in use. A refusal records nothing, so hammering a refused key does
not extend its window. Callers pass ``now`` (a monotonic time), which keeps
tests off the clock. Thread-safe: the web app's sync handlers run in a
thread pool.

stdlib-only leaf, imports nothing from istota.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque


class SlidingWindow:
    def __init__(self, window_seconds: float, max_hits: int, *, max_keys: int = 4096) -> None:
        if window_seconds <= 0 or max_hits < 0 or max_keys <= 0:
            raise ValueError("window_seconds and max_keys must be positive, max_hits >= 0")
        self.window_seconds = float(window_seconds)
        self.max_hits = int(max_hits)
        self.max_keys = int(max_keys)
        self._hits: OrderedDict[object, deque] = OrderedDict()
        self._lock = threading.Lock()

    def admit(self, key: object, now: float) -> bool:
        """Record a hit for ``key`` at ``now`` and say yes, or refuse and record nothing."""
        with self._lock:
            times = self._hits.get(key)
            if times is not None:
                while times and times[0] <= now - self.window_seconds:
                    times.popleft()
                self._hits.move_to_end(key)
            if self.max_hits == 0 or (times is not None and len(times) >= self.max_hits):
                return False
            if times is None:
                if len(self._hits) >= self.max_keys:
                    self._hits.popitem(last=False)
                times = self._hits[key] = deque()
            times.append(now)
            return True

    def __len__(self) -> int:
        return len(self._hits)

    def __contains__(self, key: object) -> bool:
        return key in self._hits
