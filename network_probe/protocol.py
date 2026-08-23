from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import socket
import struct
import threading
import time
from typing import Any


MAX_FRAME_BYTES = 16 * 1024
FRAME_AUTH_FIELD = "auth"
FRAME_SIGNATURE_ALGORITHM = "hmac-sha256"
FRAME_MAX_CLOCK_SKEW_SECONDS = 60
FRAME_NONCE_MIN_BYTES = 16


class ProbeProtocolError(RuntimeError):
    pass


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != FRAME_AUTH_FIELD}
    return json.dumps(
        unsigned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_frame(
    payload: dict[str, Any],
    key: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    if not key:
        raise ProbeProtocolError("TCP 제어 메시지 서명 키가 없습니다.")
    signed = {name: value for name, value in payload.items() if name != FRAME_AUTH_FIELD}
    auth_timestamp = int(time.time() if timestamp is None else timestamp)
    auth_nonce = nonce or secrets.token_hex(FRAME_NONCE_MIN_BYTES)
    if len(auth_nonce) < FRAME_NONCE_MIN_BYTES * 2:
        raise ProbeProtocolError("TCP 제어 메시지 nonce가 너무 짧습니다.")
    signature_input = (
        f"{FRAME_SIGNATURE_ALGORITHM}\n{auth_timestamp}\n{auth_nonce}\n".encode("ascii")
        + _canonical_payload(signed)
    )
    signature = hmac.new(key.encode("utf-8"), signature_input, hashlib.sha256).hexdigest()
    signed[FRAME_AUTH_FIELD] = {
        "algorithm": FRAME_SIGNATURE_ALGORITHM,
        "timestamp": auth_timestamp,
        "nonce": auth_nonce,
        "signature": signature,
    }
    return signed


class ReplayGuard:
    def __init__(
        self,
        *,
        max_clock_skew_seconds: int = FRAME_MAX_CLOCK_SKEW_SECONDS,
        max_entries: int = 8192,
        clock=time.time,
    ) -> None:
        self.max_clock_skew_seconds = max(1, int(max_clock_skew_seconds))
        self.max_entries = max(16, int(max_entries))
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def check_and_mark(self, key: str, nonce: str, timestamp: int) -> None:
        now = float(self.clock())
        if abs(now - timestamp) > self.max_clock_skew_seconds:
            raise ProbeProtocolError("TCP 제어 메시지 시간이 허용 범위를 벗어났습니다.")
        identity = hashlib.sha256(
            key.encode("utf-8") + b"\0" + nonce.encode("ascii", errors="ignore")
        ).hexdigest()
        with self._lock:
            expired_before = now - self.max_clock_skew_seconds
            for digest, seen_at in list(self._seen.items()):
                if seen_at < expired_before:
                    self._seen.pop(digest, None)
            if identity in self._seen:
                raise ProbeProtocolError("재사용된 TCP 제어 메시지가 거부되었습니다.")
            if len(self._seen) >= self.max_entries:
                oldest = min(self._seen, key=self._seen.get)  # type: ignore[arg-type]
                self._seen.pop(oldest, None)
            self._seen[identity] = now


def verify_frame_signature(
    payload: dict[str, Any],
    key: str,
    replay_guard: ReplayGuard,
) -> dict[str, Any]:
    auth = payload.get(FRAME_AUTH_FIELD)
    if not isinstance(auth, dict) or set(auth) != {
        "algorithm",
        "timestamp",
        "nonce",
        "signature",
    }:
        raise ProbeProtocolError("TCP 제어 메시지 인증 정보가 없습니다.")
    if auth.get("algorithm") != FRAME_SIGNATURE_ALGORITHM:
        raise ProbeProtocolError("TCP 제어 메시지 서명 방식이 올바르지 않습니다.")
    timestamp = auth.get("timestamp")
    nonce = auth.get("nonce")
    signature = auth.get("signature")
    if (
        not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or not isinstance(nonce, str)
        or len(nonce) < FRAME_NONCE_MIN_BYTES * 2
        or len(nonce) > 128
        or not nonce.isascii()
        or not isinstance(signature, str)
        or len(signature) != 64
        or any(character not in "0123456789abcdef" for character in signature)
    ):
        raise ProbeProtocolError("TCP 제어 메시지 인증 형식이 올바르지 않습니다.")
    unsigned = {name: value for name, value in payload.items() if name != FRAME_AUTH_FIELD}
    signature_input = (
        f"{FRAME_SIGNATURE_ALGORITHM}\n{timestamp}\n{nonce}\n".encode("ascii")
        + _canonical_payload(unsigned)
    )
    expected = hmac.new(key.encode("utf-8"), signature_input, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ProbeProtocolError("TCP 제어 메시지 서명이 올바르지 않습니다.")
    replay_guard.check_and_mark(key, nonce, timestamp)
    return unsigned


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray(size)
    view = memoryview(data)
    received = 0
    while received < size:
        try:
            count = sock.recv_into(view[received:])
        except socket.timeout as exc:
            raise ProbeProtocolError("TCP 제어 메시지 수신 시간이 초과되었습니다.") from exc
        if count == 0:
            raise ProbeProtocolError("TCP 연결이 제어 메시지 수신 중 종료되었습니다.")
        received += count
    return bytes(data)


def send_frame(sock: socket.socket, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProbeProtocolError("TCP 제어 메시지가 허용 크기를 초과했습니다.")
    sock.sendall(struct.pack("!I", len(encoded)) + encoded)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    (size,) = struct.unpack("!I", recv_exact(sock, 4))
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ProbeProtocolError("TCP 제어 메시지 크기가 올바르지 않습니다.")
    try:
        payload = json.loads(recv_exact(sock, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeProtocolError("TCP 제어 메시지 형식이 올바르지 않습니다.") from exc
    if not isinstance(payload, dict):
        raise ProbeProtocolError("TCP 제어 메시지는 JSON 객체여야 합니다.")
    return payload
