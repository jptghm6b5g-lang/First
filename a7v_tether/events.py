"""In-process publish/subscribe used for the live dashboard (Server-Sent Events)."""

from __future__ import annotations

import collections
import itertools
import logging
import queue
import threading
import time
from typing import Any, Deque, Dict, List


class EventBus:
    def __init__(self) -> None:
        self._subs: List["queue.Queue[Dict[str, Any]]"] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, data: Any = None) -> None:
        event = {"id": next(self._ids), "type": kind, "data": data, "ts": time.time()}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:  # slow client – drop rather than block the app
                pass


class LogBuffer(logging.Handler):
    """Keeps recent log lines and forwards them to the event bus."""

    def __init__(self, bus: EventBus, capacity: int = 1000) -> None:
        super().__init__(logging.DEBUG)
        self.bus = bus
        self.records: Deque[Dict[str, Any]] = collections.deque(maxlen=capacity)
        self._ids = itertools.count(1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {
                "id": next(self._ids),
                "ts": record.created,
                "level": record.levelname,
                "source": record.name.split(".")[-1],
                "message": record.getMessage(),
            }
        except Exception:  # pragma: no cover - malformed log call
            return
        self.records.append(item)
        self.bus.publish("log", item)
