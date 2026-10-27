"""Resilience primitives shared by the provider adapters."""

import asyncio
from collections import deque
from datetime import timezone
from email.utils import parsedate_to_datetime
import logging
import math
import time
from typing import Deque, Optional


logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class CircuitOpenError(ProviderError):
    """The dependency is failing; the call was not attempted."""


class CircuitBreaker:
    """Consecutive-failure breaker with a single half-open probe.

    State changes contain no awaits, so they are atomic on the event loop.
    """

    def __init__(self, name: str, threshold: int, cooldown_seconds: float) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            return "half-open"
        return "open"

    def before_call(self) -> None:
        state = self.state
        if state == "open" or (state == "half-open" and self._probing):
            raise CircuitOpenError(f"{self.name} circuit is open")
        if state == "half-open":
            self._probing = True

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def on_failure(self) -> None:
        self._probing = False
        self._failures += 1
        if self._opened_at is not None or self._failures >= self.threshold:
            if self._opened_at is None:
                logger.error("%s circuit opened after %d consecutive failures", self.name, self._failures)
            self._opened_at = time.monotonic()

    def on_abort(self) -> None:
        """A cancelled call proves nothing; let another probe run."""
        self._probing = False


class SlidingWindowLimiter:
    """Reserve no more than `limit` outbound attempts per rolling window."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._timestamps: Deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def defer(self, seconds: float) -> None:
        """Share an explicit provider cooldown with all workers."""
        async with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    async def acquire(self, deadline: Optional[float] = None) -> None:
        """Reserve an attempt; fail before sending if the wait would pass `deadline`."""
        while True:
            async with self._lock:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise ProviderError("Job deadline expired before a send slot")
                while self._timestamps and self._timestamps[0] <= now - self.window_seconds:
                    self._timestamps.popleft()
                wait = max(0.0, self._blocked_until - now)
                if len(self._timestamps) >= self.limit:
                    wait = max(wait, self._timestamps[0] + self.window_seconds - now)
                if wait <= 0:
                    self._timestamps.append(now)
                    return
                if deadline is not None and now + wait > deadline:
                    raise ProviderError("Job deadline would expire before a send slot")
            await asyncio.sleep(wait + 0.01)


def retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = max(0.0, date.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    if seconds > 60:
        raise ProviderError("Provider retry delay exceeds the 60-second retry budget")
    return seconds
