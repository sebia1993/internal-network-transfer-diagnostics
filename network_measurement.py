from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping


DEFAULT_MAX_HOLD_SECONDS = {
    "http_quick": 30 * 60.0,
    "http_sustained": 5 * 60.0,
    "tcp_probe": 5 * 60.0,
}
DEFAULT_FALLBACK_MAX_HOLD_SECONDS = 15 * 60.0
LONG_RUNNING_RATIO = 0.8


@dataclass(frozen=True)
class MeasurementOwner:
    kind: str
    owner_id: str
    acquired_at: float
    max_hold_seconds: float


class NetworkMeasurementGate:
    """Allows only one network measurement to consume the server at a time."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_hold_seconds: Mapping[str, float] | None = None,
        fallback_max_hold_seconds: float = DEFAULT_FALLBACK_MAX_HOLD_SECONDS,
    ) -> None:
        if fallback_max_hold_seconds <= 0:
            raise ValueError("fallback_max_hold_seconds must be positive")
        self._lock = threading.Lock()
        self._owner: MeasurementOwner | None = None
        self._clock = clock
        self._max_hold_seconds = dict(DEFAULT_MAX_HOLD_SECONDS)
        if max_hold_seconds is not None:
            for kind, seconds in max_hold_seconds.items():
                if seconds <= 0:
                    raise ValueError(f"max hold time for {kind} must be positive")
                self._max_hold_seconds[kind] = float(seconds)
        self._fallback_max_hold_seconds = float(fallback_max_hold_seconds)
        self._expired_count = 0
        self._cancel_callback: Callable[[], None] | None = None
        self._cancel_requested = False
        self._cancel_callback_failure_count = 0
        self._diagnostic_logger: logging.Logger | None = None

    def set_diagnostic_logger(self, logger: logging.Logger) -> None:
        self._diagnostic_logger = logger

    def _owner_matches(self, kind: str, owner_id: str) -> bool:
        return bool(
            self._owner is not None
            and self._owner.kind == kind
            and self._owner.owner_id == owner_id
        )

    def _request_expired_owner_cancel_locked(
        self,
        now: float,
    ) -> Callable[[], None] | None:
        if self._owner is None:
            return None
        if now - self._owner.acquired_at < self._owner.max_hold_seconds:
            return None
        if self._cancel_requested:
            return None
        self._cancel_requested = True
        self._expired_count += 1
        return self._cancel_callback

    def _invoke_cancel_callback(self, callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            # The callback owns its cleanup sequence. If any part of that
            # sequence fails, expose the failure even when it already released
            # the active owner before raising.
            with self._lock:
                self._cancel_callback_failure_count += 1
            logger = self._diagnostic_logger or logging.getLogger(__name__)
            logger.error(
                "measurement_cancel_callback_failed error_type=%s",
                type(exc).__name__,
            )

    def acquire(
        self,
        kind: str,
        owner_id: str,
        *,
        cancel_callback: Callable[[], None] | None = None,
    ) -> bool:
        if cancel_callback is not None and not callable(cancel_callback):
            raise TypeError("cancel_callback must be callable")
        callback = None
        acquired = False
        with self._lock:
            now = self._clock()
            callback = self._request_expired_owner_cancel_locked(now)
            if self._owner is None:
                self._owner = MeasurementOwner(
                    kind=kind,
                    owner_id=owner_id,
                    acquired_at=now,
                    max_hold_seconds=self._max_hold_seconds.get(
                        kind,
                        self._fallback_max_hold_seconds,
                    ),
                )
                self._cancel_callback = cancel_callback
                self._cancel_requested = False
                acquired = True
            elif self._owner_matches(kind, owner_id) and not self._cancel_requested:
                if self._cancel_callback is None and cancel_callback is not None:
                    self._cancel_callback = cancel_callback
                acquired = True
        self._invoke_cancel_callback(callback)
        return acquired

    def release(self, kind: str, owner_id: str) -> bool:
        with self._lock:
            if not self._owner_matches(kind, owner_id):
                return False
            now = self._clock()
            if (
                not self._cancel_requested
                and self._owner is not None
                and now - self._owner.acquired_at >= self._owner.max_hold_seconds
            ):
                self._expired_count += 1
            self._owner = None
            self._cancel_callback = None
            self._cancel_requested = False
            return True

    def current_owner(self) -> MeasurementOwner | None:
        with self._lock:
            callback = self._request_expired_owner_cancel_locked(self._clock())
        self._invoke_cancel_callback(callback)
        with self._lock:
            return self._owner

    def is_available(self) -> bool:
        return self.current_owner() is None

    def status(self) -> dict[str, object]:
        with self._lock:
            callback = self._request_expired_owner_cancel_locked(self._clock())
        self._invoke_cancel_callback(callback)
        with self._lock:
            now = self._clock()
            owner = self._owner
            if owner is None:
                return {
                    "active": False,
                    "kind": "",
                    "age_seconds": 0.0,
                    "max_hold_seconds": 0.0,
                    "long_running": False,
                    "expired_count": self._expired_count,
                    "cancel_callback_failure_count": (
                        self._cancel_callback_failure_count
                    ),
                }
            age_seconds = max(now - owner.acquired_at, 0.0)
            return {
                "active": True,
                "kind": owner.kind,
                "age_seconds": round(age_seconds, 3),
                "max_hold_seconds": owner.max_hold_seconds,
                "long_running": age_seconds >= owner.max_hold_seconds * LONG_RUNNING_RATIO,
                "expired_count": self._expired_count,
                "cancel_callback_failure_count": (
                    self._cancel_callback_failure_count
                ),
            }
