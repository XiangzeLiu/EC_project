"""Thread-safe action rate limiting used by mouse and keyboard trade paths."""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

from .hotkey_config import RateLimitPolicy


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    reason: str = ""
    token: str = ""


class ActionRateLimiter:
    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._last_scope: dict[tuple[str, str], float] = {}
        self._last_signature: dict[tuple[str, str], float] = {}
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._in_flight: dict[str, int] = defaultdict(int)
        self._tokens: dict[str, str] = {}

    def acquire(
        self,
        action: str,
        scope: str,
        policy: RateLimitPolicy,
        *,
        signature: str = "",
        identical_cooldown_ms: int = 0,
    ) -> RateLimitDecision:
        now = self._clock()
        with self._lock:
            if len(self._last_scope) > 512:
                cutoff = now - 60
                self._last_scope = {key: value for key, value in self._last_scope.items() if value >= cutoff}
            if len(self._last_signature) > 512:
                cutoff = now - 60
                self._last_signature = {key: value for key, value in self._last_signature.items() if value >= cutoff}

            scope_key = (action, scope)
            last_scope = self._last_scope.get(scope_key)
            if policy.cooldown_ms > 0 and last_scope is not None:
                if (now - last_scope) * 1000 < policy.cooldown_ms:
                    return RateLimitDecision(False, "cooldown")

            if signature and identical_cooldown_ms > 0:
                signature_key = (action, signature)
                last_signature = self._last_signature.get(signature_key)
                if last_signature is not None and (now - last_signature) * 1000 < identical_cooldown_ms:
                    return RateLimitDecision(False, "duplicate")

            events = self._events[action]
            if policy.burst_limit > 0 and policy.burst_window_ms > 0:
                cutoff = now - policy.burst_window_ms / 1000
                while events and events[0] <= cutoff:
                    events.popleft()
                if len(events) >= policy.burst_limit:
                    return RateLimitDecision(False, "burst")

            if policy.max_in_flight > 0 and self._in_flight[action] >= policy.max_in_flight:
                return RateLimitDecision(False, "in_flight")

            self._last_scope[scope_key] = now
            if signature:
                self._last_signature[(action, signature)] = now
            if policy.burst_limit > 0 and policy.burst_window_ms > 0:
                events.append(now)

            token = ""
            if policy.max_in_flight > 0:
                token = uuid.uuid4().hex
                self._tokens[token] = action
                self._in_flight[action] += 1
            return RateLimitDecision(True, token=token)

    def release(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            action = self._tokens.pop(token, "")
            if action:
                self._in_flight[action] = max(0, self._in_flight[action] - 1)

    def reset(self) -> None:
        with self._lock:
            self._last_scope.clear()
            self._last_signature.clear()
            self._events.clear()
            self._in_flight.clear()
            self._tokens.clear()
