from __future__ import annotations

import math
import socket
import statistics
import threading
import time
from typing import Any, Callable

from .windows_tcp_info import snapshot_tcp_info, telemetry_delta


BLOCK_SIZE = 128 * 1024
PAYLOAD = bytes(index % 251 for index in range(BLOCK_SIZE))


class ProbeCancelled(RuntimeError):
    pass


class ProbeTransferError(RuntimeError):
    pass


def _check_cancel(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise ProbeCancelled("TCP 측정이 취소되었습니다.")


def _send_until(sock: socket.socket, deadline_ns: int, cancel_event: threading.Event) -> int:
    sent_total = 0
    pending = memoryview(PAYLOAD)
    last_progress = time.perf_counter()
    while time.perf_counter_ns() < deadline_ns:
        _check_cancel(cancel_event)
        try:
            sent = sock.send(pending)
        except socket.timeout:
            if time.perf_counter() - last_progress > 10:
                raise ProbeTransferError("TCP 송신이 10초 이상 진행되지 않았습니다.")
            continue
        except OSError as exc:
            if cancel_event.is_set():
                raise ProbeCancelled("TCP 측정이 취소되었습니다.") from exc
            raise ProbeTransferError(f"TCP 송신 연결이 종료되었습니다: {exc}") from exc
        if sent <= 0:
            raise ProbeTransferError("TCP 송신 연결이 종료되었습니다.")
        sent_total += sent
        last_progress = time.perf_counter()
        pending = pending[sent:]
        if not pending:
            pending = memoryview(PAYLOAD)
    return sent_total


def run_sender_stream(
    sock: socket.socket,
    *,
    stream_id: int,
    warmup_seconds: float,
    duration_seconds: int,
    cancel_event: threading.Event,
    telemetry_provider: Callable[[socket.socket], dict[str, Any]] = snapshot_tcp_info,
) -> dict[str, Any]:
    sock.settimeout(1.0)
    warmup_end = time.perf_counter_ns() + int(warmup_seconds * 1_000_000_000)
    _send_until(sock, warmup_end, cancel_event)

    telemetry_start = telemetry_provider(sock)
    started_ns = time.perf_counter_ns()
    ended_ns = started_ns + duration_seconds * 1_000_000_000
    intervals = [0] * duration_seconds
    total_bytes = 0
    pending = memoryview(PAYLOAD)
    last_progress = time.perf_counter()
    while time.perf_counter_ns() < ended_ns:
        _check_cancel(cancel_event)
        try:
            sent = sock.send(pending)
        except socket.timeout:
            if time.perf_counter() - last_progress > 10:
                raise ProbeTransferError("TCP 송신이 10초 이상 진행되지 않았습니다.")
            continue
        except OSError as exc:
            if cancel_event.is_set():
                raise ProbeCancelled("TCP 측정이 취소되었습니다.") from exc
            raise ProbeTransferError(f"TCP 송신 연결이 종료되었습니다: {exc}") from exc
        if sent <= 0:
            raise ProbeTransferError("TCP 송신 연결이 종료되었습니다.")
        now_ns = time.perf_counter_ns()
        total_bytes += sent
        interval_index = min(max((now_ns - started_ns) // 1_000_000_000, 0), duration_seconds - 1)
        intervals[int(interval_index)] += sent
        last_progress = time.perf_counter()
        pending = pending[sent:]
        if not pending:
            pending = memoryview(PAYLOAD)

    telemetry_end = telemetry_provider(sock)
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    actual_seconds = max((time.perf_counter_ns() - started_ns) / 1_000_000_000, 0.000001)
    return _stream_result(
        stream_id=stream_id,
        role="sender",
        byte_count=total_bytes,
        duration_seconds=actual_seconds,
        interval_bytes=intervals,
        telemetry=telemetry_delta(telemetry_start, telemetry_end),
    )


def run_receiver_stream(
    sock: socket.socket,
    *,
    stream_id: int,
    warmup_seconds: float,
    duration_seconds: int,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    sock.settimeout(1.0)
    buffer = bytearray(BLOCK_SIZE)
    warmup_end_ns = time.perf_counter_ns() + int(warmup_seconds * 1_000_000_000)
    measured_start_ns = warmup_end_ns
    measured_end_ns = measured_start_ns + duration_seconds * 1_000_000_000
    intervals = [0] * duration_seconds
    total_bytes = 0
    last_progress = time.perf_counter()
    while True:
        _check_cancel(cancel_event)
        try:
            received = sock.recv_into(buffer)
        except socket.timeout:
            if time.perf_counter_ns() >= measured_end_ns and time.perf_counter() - last_progress > 2:
                break
            if time.perf_counter() - last_progress > 10:
                raise ProbeTransferError("TCP 수신이 10초 이상 진행되지 않았습니다.")
            continue
        except OSError as exc:
            if cancel_event.is_set():
                raise ProbeCancelled("TCP 측정이 취소되었습니다.") from exc
            raise ProbeTransferError(f"TCP 수신 연결이 종료되었습니다: {exc}") from exc
        if received == 0:
            break
        now_ns = time.perf_counter_ns()
        last_progress = time.perf_counter()
        if measured_start_ns <= now_ns <= measured_end_ns:
            total_bytes += received
            interval_index = min((now_ns - measured_start_ns) // 1_000_000_000, duration_seconds - 1)
            intervals[int(interval_index)] += received

    return _stream_result(
        stream_id=stream_id,
        role="receiver",
        byte_count=total_bytes,
        duration_seconds=float(duration_seconds),
        interval_bytes=intervals,
        telemetry={"available": False, "error": "수신 소켓에는 송신 TCP 통계를 적용하지 않습니다."},
    )


def _stream_result(
    *,
    stream_id: int,
    role: str,
    byte_count: int,
    duration_seconds: float,
    interval_bytes: list[int],
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stream_id": stream_id,
        "role": role,
        "bytes": int(byte_count),
        "duration_seconds": round(duration_seconds, 6),
        "mbps": round(byte_count * 8 / duration_seconds / 1_000_000, 2) if duration_seconds > 0 else 0.0,
        "interval_bytes": [int(value) for value in interval_bytes],
        "telemetry": telemetry,
    }


def aggregate_stream_results(results: list[dict[str, Any]], *, role: str, duration_seconds: int) -> dict[str, Any]:
    if not results:
        raise ProbeTransferError("TCP 스트림 결과가 없습니다.")
    if duration_seconds <= 0:
        raise ProbeTransferError("TCP 측정 시간이 올바르지 않습니다.")

    validated_results = []
    for result in results:
        try:
            byte_count = int(result.get("bytes", 0))
            actual_duration = float(result.get("duration_seconds", 0))
        except (TypeError, ValueError) as exc:
            raise ProbeTransferError("TCP 스트림 처리량 결과가 올바르지 않습니다.") from exc
        if byte_count <= 0:
            raise ProbeTransferError("TCP 스트림에 전송된 데이터가 없습니다.")
        if not math.isfinite(actual_duration) or actual_duration <= 0:
            raise ProbeTransferError("TCP 스트림 측정 시간이 올바르지 않습니다.")
        validated = dict(result)
        validated["bytes"] = byte_count
        validated["duration_seconds"] = actual_duration
        validated_results.append(validated)

    results = validated_results
    total_bytes = sum(int(item.get("bytes", 0)) for item in results)
    interval_bytes = [0] * duration_seconds
    for item in results:
        for index, value in enumerate(item.get("interval_bytes", [])[:duration_seconds]):
            interval_bytes[index] += int(value)
    intervals = [
        {
            "index": index + 1,
            "bytes": value,
            "mbps": round(value * 8 / 1_000_000, 2),
        }
        for index, value in enumerate(interval_bytes)
    ]
    speeds = [item["mbps"] for item in intervals]
    telemetry_rows = [item.get("telemetry", {}) for item in results if item.get("telemetry", {}).get("available")]
    telemetry: dict[str, Any]
    if telemetry_rows:
        rtts = [int(item["rtt_us"]) for item in telemetry_rows if item.get("rtt_us") is not None]
        min_rtts = [int(item["min_rtt_us"]) for item in telemetry_rows if item.get("min_rtt_us") is not None]
        telemetry = {
            "available": True,
            "rtt_us": round(statistics.median(rtts)) if rtts else None,
            "min_rtt_us": min(min_rtts) if min_rtts else None,
            "cwnd_bytes": sum(int(item.get("cwnd_bytes", 0)) for item in telemetry_rows),
            "bytes_retrans": sum(int(item.get("bytes_retrans", 0)) for item in telemetry_rows),
            "fast_retransmits": sum(int(item.get("fast_retransmits", 0)) for item in telemetry_rows),
            "duplicate_acks": sum(int(item.get("duplicate_acks", 0)) for item in telemetry_rows),
            "timeout_episodes": sum(int(item.get("timeout_episodes", 0)) for item in telemetry_rows),
        }
    else:
        telemetry = {
            "available": False,
            "error": next(
                (str(item.get("telemetry", {}).get("error")) for item in results if item.get("telemetry", {}).get("error")),
                "TCP 상세 통계를 사용할 수 없습니다.",
            ),
        }
    return {
        "role": role,
        "bytes": total_bytes,
        "duration_seconds": duration_seconds,
        "average_mbps": round(total_bytes * 8 / duration_seconds / 1_000_000, 2),
        "median_mbps": round(statistics.median(speeds), 2) if speeds else 0.0,
        "min_mbps": round(min(speeds), 2) if speeds else 0.0,
        "max_mbps": round(max(speeds), 2) if speeds else 0.0,
        "intervals": intervals,
        "streams": sorted(results, key=lambda item: int(item.get("stream_id", 0))),
        "telemetry": telemetry,
    }
