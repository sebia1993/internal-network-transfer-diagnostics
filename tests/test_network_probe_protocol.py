import json
import socket
import struct

import pytest

from network_probe.protocol import (
    MAX_FRAME_BYTES,
    ProbeProtocolError,
    ReplayGuard,
    recv_frame,
    send_frame,
    sign_frame,
    verify_frame_signature,
)


def test_probe_frame_round_trip():
    left, right = socket.socketpair()
    try:
        send_frame(left, {"type": "ready", "stream_id": 3})
        assert recv_frame(right) == {"type": "ready", "stream_id": 3}
    finally:
        left.close()
        right.close()


def test_probe_frame_rejects_oversized_payload():
    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("!I", MAX_FRAME_BYTES + 1))
        with pytest.raises(ProbeProtocolError, match="크기"):
            recv_frame(right)
    finally:
        left.close()
        right.close()


def test_probe_frame_rejects_non_object_json():
    left, right = socket.socketpair()
    encoded = json.dumps(["not", "an", "object"]).encode("utf-8")
    try:
        left.sendall(struct.pack("!I", len(encoded)) + encoded)
        with pytest.raises(ProbeProtocolError, match="JSON 객체"):
            recv_frame(right)
    finally:
        left.close()
        right.close()


def test_hmac_frame_round_trip_and_replay_rejection():
    now = [1000.0]
    guard = ReplayGuard(clock=lambda: now[0])
    signed = sign_frame(
        {"type": "connectivity_check", "agent_id": "a" * 32},
        "secret-key",
        timestamp=1000,
        nonce="1" * 32,
    )
    assert verify_frame_signature(signed, "secret-key", guard) == {
        "type": "connectivity_check",
        "agent_id": "a" * 32,
    }
    with pytest.raises(ProbeProtocolError, match="재사용"):
        verify_frame_signature(signed, "secret-key", guard)


def test_hmac_frame_rejects_tampering_and_expired_timestamp():
    guard = ReplayGuard(clock=lambda: 1000.0)
    signed = sign_frame(
        {"type": "data_stream", "stream_id": 0},
        "secret-key",
        timestamp=1000,
        nonce="2" * 32,
    )
    tampered = {**signed, "stream_id": 1}
    with pytest.raises(ProbeProtocolError, match="서명"):
        verify_frame_signature(tampered, "secret-key", guard)

    expired = sign_frame(
        {"type": "data_stream", "stream_id": 0},
        "secret-key",
        timestamp=900,
        nonce="3" * 32,
    )
    with pytest.raises(ProbeProtocolError, match="시간"):
        verify_frame_signature(expired, "secret-key", guard)
