from __future__ import annotations

import csv
import json
import logging
import math
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context

from measurement_transactions import (
    commit_measurement_result,
    has_pending_measurement_transactions,
    measurement_transaction_root_for_log,
)
from network_measurement import NetworkMeasurementGate
from result_storage import prune_old_json_results
from runtime_stability import CsvIntegrityError, archive_csv_history
from sustained_excel import (
    EXCEL_MIME_TYPE,
    SustainedExcelError,
    build_sustained_excel,
    build_sustained_excel_filename,
)


SUSTAINED_LOG_FIELDS = [
    "checked_at",
    "session_id",
    "client_ip",
    "direction",
    "duration_seconds",
    "warmup_seconds",
    "stream_count",
    "bytes_transferred",
    "actual_duration_seconds",
    "average_mbps",
    "median_mbps",
    "min_mbps",
    "max_mbps",
    "variability_percent",
    "http_latency_median_ms",
    "status",
    "error",
    "result_json",
]
SUCCESS_BYTE_RELATIVE_TOLERANCE = 0.05
SUCCESS_BYTE_MIN_TOLERANCE_BYTES = 64 * 1024


@dataclass(frozen=True)
class SustainedCheckSettings:
    allowed_durations: tuple[int, ...] = (10, 30)
    allowed_stream_counts: tuple[int, ...] = (1,)
    warmup_seconds: float = 3.0
    session_ttl_seconds: float = 5 * 60
    max_upload_chunk_bytes: int = 8 * 1024 * 1024
    upload_read_chunk_bytes: int = 256 * 1024
    download_chunk_bytes: int = 1024 * 1024


@dataclass
class SustainedSession:
    session_id: str
    client_ip: str
    requested_direction: str
    duration_seconds: int
    stream_count: int
    created_at_monotonic: float
    created_at_text: str
    last_activity_at: float
    status: str = "created"
    phase_index: int = -1
    active_direction: str = ""
    active_phase: str = ""
    phase_started_at: float = 0.0
    phase_deadline: float = 0.0
    phase_bytes: int = 0
    measured_bytes: dict[str, int] = field(default_factory=lambda: {"upload": 0, "download": 0})
    measured_intervals: dict[str, list[int]] = field(default_factory=lambda: {"upload": [], "download": []})
    error: str = ""

    def expected_phases(self) -> list[tuple[str, str]]:
        directions = ["upload", "download"] if self.requested_direction == "full" else [self.requested_direction]
        return [(direction, phase) for direction in directions for phase in ("warmup", "measure")]


class SustainedCheckError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def ensure_sustained_storage(log_path: Path, results_root: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 0:
        return
    with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=SUSTAINED_LOG_FIELDS).writeheader()


def build_sustained_log_rows(
    result: dict[str, Any],
) -> list[dict[str, object]]:
    result_json = (
        "data/network_check_results/"
        f"{result['session_id']}.json"
    )
    rows: list[dict[str, object]] = []
    for direction in ("upload", "download"):
        summary = result["directions"].get(direction)
        if summary is None:
            continue
        rows.append(
            {
                "checked_at": result["completed_at"],
                "session_id": result["session_id"],
                "client_ip": result["client_ip"],
                "direction": direction,
                "duration_seconds": result["requested"]["duration_seconds"],
                "warmup_seconds": result["requested"]["warmup_seconds"],
                "stream_count": result["requested"]["stream_count"],
                "bytes_transferred": summary["bytes_transferred"],
                "actual_duration_seconds": summary[
                    "actual_duration_seconds"
                ],
                "average_mbps": summary["average_mbps"],
                "median_mbps": summary["median_mbps"],
                "min_mbps": summary["min_mbps"],
                "max_mbps": summary["max_mbps"],
                "variability_percent": summary["variability_percent"],
                "http_latency_median_ms": (
                    result["http_latency"]["median_ms"] or ""
                ),
                "status": result["status"],
                "error": result["error"],
                "result_json": result_json,
            }
        )
    return rows


def calculate_mbps(byte_count: int, duration_seconds: float) -> float:
    if byte_count <= 0 or duration_seconds <= 0:
        return 0.0
    return byte_count * 8 / duration_seconds / 1_000_000


def summarize_intervals(
    *,
    byte_count: int,
    duration_seconds: float,
    interval_bytes: list[int],
    interval_durations: list[float] | None = None,
) -> dict[str, Any]:
    interval_rows = []
    for index, bytes_in_interval in enumerate(interval_bytes):
        interval_duration = 1.0
        if interval_durations is not None and index < len(interval_durations):
            try:
                candidate_duration = float(interval_durations[index])
            except (TypeError, ValueError):
                candidate_duration = 1.0
            if math.isfinite(candidate_duration) and candidate_duration > 0:
                interval_duration = candidate_duration
        interval_rows.append(
            {
                "index": index + 1,
                "duration_seconds": round(interval_duration, 3),
                "bytes_transferred": int(bytes_in_interval),
                "mbps": round(calculate_mbps(int(bytes_in_interval), interval_duration), 2),
            }
        )

    speeds = [row["mbps"] for row in interval_rows]
    average_mbps = calculate_mbps(byte_count, duration_seconds)
    median_mbps = statistics.median(speeds) if speeds else 0.0
    minimum_mbps = min(speeds) if speeds else 0.0
    maximum_mbps = max(speeds) if speeds else 0.0
    mean_speed = statistics.fmean(speeds) if speeds else 0.0
    variability = statistics.pstdev(speeds) / mean_speed * 100 if mean_speed else 0.0
    return {
        "bytes_transferred": int(byte_count),
        "actual_duration_seconds": round(float(duration_seconds), 3),
        "average_mbps": round(average_mbps, 2),
        "median_mbps": round(median_mbps, 2),
        "min_mbps": round(minimum_mbps, 2),
        "max_mbps": round(maximum_mbps, 2),
        "variability_percent": round(variability, 2),
        "intervals": interval_rows,
    }


class SustainedCheckManager:
    def __init__(
        self,
        *,
        log_path: Path,
        results_root: Path,
        settings: SustainedCheckSettings | None = None,
        measurement_gate: NetworkMeasurementGate | None = None,
        clock: Callable[[], float] = time.perf_counter,
        diagnostic_logger: logging.Logger | None = None,
    ) -> None:
        self.log_path = log_path
        self.results_root = results_root
        self.settings = settings or SustainedCheckSettings()
        self.measurement_gate = measurement_gate
        self.clock = clock
        self.diagnostic_logger = diagnostic_logger or logging.getLogger(__name__)
        self.lock = threading.RLock()
        self.storage_lock = threading.Lock()
        self.diagnostic_failure_count = 0
        self.last_diagnostic_event = ""
        self.last_diagnostic_error_type = ""
        self.active_session: SustainedSession | None = None
        ensure_sustained_storage(log_path, results_root)
        self.download_chunk = bytes(index % 251 for index in range(self.settings.download_chunk_bytes))

    def _record_diagnostic_failure(
        self,
        event: str,
        exc: BaseException,
        *,
        warning: bool = False,
    ) -> None:
        error_type = type(exc).__name__
        with self.lock:
            self.diagnostic_failure_count += 1
            self.last_diagnostic_event = event
            self.last_diagnostic_error_type = error_type
        log_method = (
            self.diagnostic_logger.warning
            if warning
            else self.diagnostic_logger.error
        )
        log_method("%s error_type=%s", event, error_type)

    def diagnostic_status(self) -> dict[str, str | int]:
        with self.lock:
            return {
                "failure_count": self.diagnostic_failure_count,
                "last_event": self.last_diagnostic_event,
                "last_error_type": self.last_diagnostic_error_type,
            }

    def _raise_if_measurement_recovery_pending_locked(self) -> None:
        if has_pending_measurement_transactions(
            measurement_transaction_root_for_log(self.log_path),
            "http_sustained",
        ):
            raise SustainedCheckError(
                "이전 HTTP 측정 결과 복구가 필요합니다. 오류 코드: "
                "MEASUREMENT_RECOVERY_PENDING. 서버를 다시 시작한 뒤 "
                "재측정하세요.",
                503,
            )

    def start_session(
        self,
        *,
        client_ip: str,
        direction: str,
        duration_seconds: int,
        stream_count: int,
    ) -> SustainedSession:
        self.cleanup_expired()
        if direction not in {"upload", "download", "full"}:
            raise SustainedCheckError("측정 방향은 업로드, 다운로드, 전체 중 하나여야 합니다.")
        if duration_seconds not in self.settings.allowed_durations:
            raise SustainedCheckError("측정 시간은 10초 또는 30초만 선택할 수 있습니다.")
        if stream_count not in self.settings.allowed_stream_counts:
            raise SustainedCheckError("HTTP 시간 기준 측정은 1개 연결만 지원합니다.")

        now = self.clock()
        with self.lock:
            if self.active_session is not None:
                raise SustainedCheckError("다른 HTTP 시간 기준 측정이 진행 중입니다.", 409)
            with self.storage_lock:
                self._raise_if_measurement_recovery_pending_locked()
            session_id = uuid.uuid4().hex
            if self.measurement_gate is not None and not self.measurement_gate.acquire(
                "http_sustained",
                session_id,
                cancel_callback=lambda expected_session_id=session_id: self.close(
                    "최대 실행 시간을 초과하여 HTTP 시간 기준 측정이 중단되었습니다.",
                    expected_session_id=expected_session_id,
                ),
            ):
                raise SustainedCheckError("다른 네트워크 측정이 진행 중입니다.", 409)
            session = SustainedSession(
                session_id=session_id,
                client_ip=client_ip,
                requested_direction=direction,
                duration_seconds=duration_seconds,
                stream_count=stream_count,
                created_at_monotonic=now,
                created_at_text=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                last_activity_at=now,
            )
            self.active_session = session
        self._start_expiry_watchdog(session.session_id)
        return session

    def _start_expiry_watchdog(self, session_id: str) -> None:
        def watch() -> None:
            while True:
                with self.lock:
                    session = self.active_session
                    if session is None or session.session_id != session_id:
                        return
                    remaining = self.settings.session_ttl_seconds - (
                        self.clock() - session.last_activity_at
                    )
                if remaining > 0:
                    time.sleep(min(max(remaining, 0.01), 1.0))
                    continue
                try:
                    self.cleanup_expired()
                except SustainedCheckError:
                    return
                except Exception as exc:
                    self._record_diagnostic_failure(
                        "http_sustained_expiry_cleanup_failed",
                        exc,
                    )
                    return
                with self.lock:
                    session = self.active_session
                    if session is None or session.session_id != session_id:
                        return
                time.sleep(0.01)

        threading.Thread(
            target=watch,
            name=f"http-sustained-expiry-{session_id[:8]}",
            daemon=True,
        ).start()

    def _require_session(self, session_id: str, client_ip: str) -> SustainedSession:
        session = self.active_session
        if session is None or session.session_id != session_id:
            raise SustainedCheckError("HTTP 시간 기준 측정 세션을 찾을 수 없습니다.", 404)
        if session.client_ip != client_ip:
            raise SustainedCheckError("이 측정 세션은 다른 IP에서 사용할 수 없습니다.", 403)
        session.last_activity_at = self.clock()
        return session

    def begin_phase(self, session_id: str, client_ip: str, direction: str, phase: str) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            session = self._require_session(session_id, client_ip)
            expected = session.expected_phases()
            next_index = session.phase_index + 1
            if next_index >= len(expected) or expected[next_index] != (direction, phase):
                raise SustainedCheckError("측정 단계 순서가 올바르지 않습니다.", 409)
            if session.phase_index >= 0 and now + 0.05 < session.phase_deadline:
                raise SustainedCheckError("이전 측정 단계가 아직 진행 중입니다.", 409)

            phase_duration = self.settings.warmup_seconds if phase == "warmup" else float(session.duration_seconds)
            session.phase_index = next_index
            session.active_direction = direction
            session.active_phase = phase
            session.phase_started_at = now
            session.phase_deadline = now + phase_duration
            session.phase_bytes = 0
            session.status = "running"
            if phase == "measure":
                session.measured_bytes[direction] = 0
                session.measured_intervals[direction] = [0] * session.duration_seconds
            return {
                "session_id": session.session_id,
                "direction": direction,
                "phase": phase,
                "duration_seconds": phase_duration,
                "stream_count": session.stream_count,
            }

    def validate_data_stream(self, session_id: str, client_ip: str, direction: str, stream_id: int) -> SustainedSession:
        with self.lock:
            session = self._require_session(session_id, client_ip)
            if stream_id < 0 or stream_id >= session.stream_count:
                raise SustainedCheckError("허용되지 않는 HTTP 연결 번호입니다.")
            if session.active_direction != direction or session.active_phase not in {"warmup", "measure"}:
                raise SustainedCheckError("현재 측정 단계와 요청 방향이 다릅니다.", 409)
            return session

    def record_bytes(self, session_id: str, client_ip: str, direction: str, byte_count: int) -> None:
        if byte_count <= 0:
            return
        now = self.clock()
        with self.lock:
            session = self._require_session(session_id, client_ip)
            if session.active_direction != direction or now > session.phase_deadline:
                return
            session.phase_bytes += byte_count
            if session.active_phase != "measure":
                return
            session.measured_bytes[direction] += byte_count
            interval_index = min(int(now - session.phase_started_at), session.duration_seconds - 1)
            if interval_index >= 0:
                session.measured_intervals[direction][interval_index] += byte_count

    def phase_active(self, session_id: str, client_ip: str, direction: str) -> bool:
        with self.lock:
            session = self._require_session(session_id, client_ip)
            return session.active_direction == direction and self.clock() < session.phase_deadline

    def status(self, session_id: str, client_ip: str) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            session = self._require_session(session_id, client_ip)
            phase_duration = max(session.phase_deadline - session.phase_started_at, 0.0)
            elapsed = max(min(now - session.phase_started_at, phase_duration), 0.0) if phase_duration else 0.0
            return {
                "session_id": session.session_id,
                "status": session.status,
                "direction": session.active_direction,
                "phase": session.active_phase,
                "phase_bytes": session.phase_bytes,
                "elapsed_seconds": round(elapsed, 3),
                "duration_seconds": round(phase_duration, 3),
                "phase_complete": bool(phase_duration and now >= session.phase_deadline),
            }

    def complete(self, session_id: str, client_ip: str, payload: dict[str, Any], *, status: str = "success") -> dict[str, Any]:
        with self.lock:
            session = self._require_session(session_id, client_ip)
            if status == "success" and session.phase_index != len(session.expected_phases()) - 1:
                raise SustainedCheckError("모든 측정 단계가 완료되지 않았습니다.", 409)
            if status == "success" and self.clock() + 0.05 < session.phase_deadline:
                raise SustainedCheckError("현재 측정 단계가 아직 진행 중입니다.", 409)
            result_payload = payload
            result_status = status
            if status == "success":
                try:
                    self._validate_success_client_results(session, payload)
                except SustainedCheckError:
                    result_status = "failure"
                    result_payload = {
                        "latency_samples_ms": payload.get("latency_samples_ms", []),
                        "results": {},
                        "error": (
                            "브라우저 측 측정 결과 검증에 실패했습니다. "
                            "전송량·시간·구간 합계를 확인한 뒤 다시 측정하세요."
                        ),
                    }
            result = self._build_result(session, result_payload, status=result_status)
            try:
                try:
                    self._persist_result(result)
                except SustainedCheckError:
                    raise
                except Exception as exc:
                    self._record_diagnostic_failure(
                        "http_sustained_result_persistence_failed",
                        exc,
                    )
                    raise SustainedCheckError(
                        "측정 결과를 저장하지 못했습니다. 오류 코드: "
                        "RESULT_WRITE_FAILED. 서버 진단 로그를 확인한 뒤 다시 "
                        "측정하세요.",
                        500,
                    ) from None
                else:
                    session.status = result_status
                    session.error = result["error"]
            finally:
                self.active_session = None
                if self.measurement_gate is not None:
                    self.measurement_gate.release("http_sustained", session.session_id)
            return result

    def cancel(self, session_id: str, client_ip: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.complete(session_id, client_ip, payload or {}, status="cancelled")

    def close(
        self,
        reason: str = "서버 종료로 HTTP 시간 기준 측정이 중단되었습니다.",
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.lock:
            session = self.active_session
            if session is None or (
                expected_session_id is not None
                and session.session_id != expected_session_id
            ):
                return None
            result = self._build_result(
                session,
                {"error": reason},
                status="failure",
            )
            try:
                try:
                    self._persist_result(result)
                except SustainedCheckError:
                    raise
                except Exception as exc:
                    self._record_diagnostic_failure(
                        "http_sustained_shutdown_persistence_failed",
                        exc,
                    )
                    raise SustainedCheckError(
                        "종료 중 측정 결과를 저장하지 못했습니다. 오류 코드: "
                        "RESULT_WRITE_FAILED.",
                        500,
                    ) from None
                else:
                    session.status = "failure"
                    session.error = result["error"]
            finally:
                self.active_session = None
                if self.measurement_gate is not None:
                    self.measurement_gate.release(
                        "http_sustained",
                        session.session_id,
                    )
            return result

    def cleanup_expired(self) -> None:
        with self.lock:
            session = self.active_session
            if session is None or self.clock() - session.last_activity_at <= self.settings.session_ttl_seconds:
                return
            result = self._build_result(
                session,
                {"error": "브라우저 연결이 끊어져 HTTP 시간 기준 측정 세션이 만료되었습니다."},
                status="failure",
            )
            try:
                try:
                    self._persist_result(result)
                except SustainedCheckError:
                    raise
                except Exception as exc:
                    self._record_diagnostic_failure(
                        "http_sustained_expiry_persistence_failed",
                        exc,
                    )
                    raise SustainedCheckError(
                        "만료된 측정 결과를 저장하지 못했습니다. 오류 코드: "
                        "RESULT_WRITE_FAILED.",
                        500,
                    ) from None
            finally:
                self.active_session = None
                if self.measurement_gate is not None:
                    self.measurement_gate.release("http_sustained", session.session_id)

    def _result_path(self, session_id: str) -> Path:
        if not session_id or any(character not in "0123456789abcdef" for character in session_id) or len(session_id) != 32:
            raise SustainedCheckError("측정 결과를 찾을 수 없습니다.", 404)
        result_path = self.results_root / f"{session_id}.json"
        if not result_path.exists() or not result_path.is_file():
            raise SustainedCheckError("측정 결과를 찾을 수 없습니다.", 404)
        return result_path

    def _read_result_payload(self, session_id: str) -> tuple[str, dict[str, Any]]:
        result_path = self._result_path(session_id)
        try:
            result_text = result_path.read_text(encoding="utf-8")
            saved = json.loads(result_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SustainedCheckError(
                "측정 결과 파일을 읽을 수 없습니다. 오류 코드: RESULT_READ_FAILED.",
                500,
            ) from exc
        if not isinstance(saved, dict):
            raise SustainedCheckError("측정 결과 파일 형식이 올바르지 않습니다.", 500)
        return result_text, saved

    @staticmethod
    def _ensure_result_owner(saved: dict[str, Any], client_ip: str) -> None:
        if saved.get("client_ip") != client_ip:
            raise SustainedCheckError("이 측정 결과는 다른 IP에서 받을 수 없습니다.", 403)

    def saved_result_for(self, session_id: str, client_ip: str) -> dict[str, Any]:
        _, saved = self._read_result_payload(session_id)
        self._ensure_result_owner(saved, client_ip)
        return saved

    def result_text_for(self, session_id: str, client_ip: str) -> str:
        result_text, saved = self._read_result_payload(session_id)
        self._ensure_result_owner(saved, client_ip)
        return result_text

    def result_path_for(self, session_id: str, client_ip: str) -> Path:
        result_path = self._result_path(session_id)
        self.saved_result_for(session_id, client_ip)
        return result_path

    def _build_result(self, session: SustainedSession, payload: dict[str, Any], *, status: str) -> dict[str, Any]:
        latency_samples = self._validated_latency_samples(payload.get("latency_samples_ms", []))
        latency = {
            "samples_ms": latency_samples,
            "min_ms": round(min(latency_samples), 2) if latency_samples else None,
            "median_ms": round(statistics.median(latency_samples), 2) if latency_samples else None,
            "max_ms": round(max(latency_samples), 2) if latency_samples else None,
        }
        client_results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
        directions = {}
        expected_directions = ["upload", "download"] if session.requested_direction == "full" else [session.requested_direction]
        for direction in expected_directions:
            if direction == "upload":
                directions[direction] = summarize_intervals(
                    byte_count=session.measured_bytes[direction],
                    duration_seconds=float(session.duration_seconds),
                    interval_bytes=session.measured_intervals[direction],
                )
            else:
                client_result = client_results.get(direction)
                if client_result is None and status != "success" and session.measured_bytes[direction] > 0:
                    directions[direction] = summarize_intervals(
                        byte_count=session.measured_bytes[direction],
                        duration_seconds=float(session.duration_seconds),
                        interval_bytes=session.measured_intervals[direction],
                    )
                else:
                    try:
                        directions[direction] = self._validated_client_result(
                            client_result,
                            session.duration_seconds,
                        )
                    except SustainedCheckError:
                        if status == "success":
                            raise
                        directions[direction] = summarize_intervals(
                            byte_count=session.measured_bytes[direction],
                            duration_seconds=float(session.duration_seconds),
                            interval_bytes=session.measured_intervals[direction],
                        )

        error = str(payload.get("error", "")).strip()[:500]
        return {
            "schema_version": 1,
            "session_id": session.session_id,
            "client_ip": session.client_ip,
            "started_at": session.created_at_text,
            "completed_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
            "requested": {
                "direction": session.requested_direction,
                "duration_seconds": session.duration_seconds,
                "warmup_seconds": self.settings.warmup_seconds,
                "stream_count": session.stream_count,
            },
            "http_latency": latency,
            "directions": directions,
            "status": status,
            "error": error,
            "result_url": f"/network-check/sustained/results/{session.session_id}.json",
            "excel_url": f"/network-check/sustained/results/{session.session_id}.xlsx",
        }

    @staticmethod
    def _validated_latency_samples(value: Any) -> list[float]:
        if not isinstance(value, list):
            return []
        samples = []
        for item in value[:10]:
            try:
                number = float(item)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and 0 <= number <= 60_000:
                samples.append(round(number, 3))
        return samples

    def _validate_success_client_results(
        self,
        session: SustainedSession,
        payload: dict[str, Any],
    ) -> None:
        client_results = payload.get("results")
        if not isinstance(client_results, dict):
            raise SustainedCheckError("성공 결과에 브라우저 측 전송 결과가 없습니다.")

        expected_directions = (
            ["upload", "download"]
            if session.requested_direction == "full"
            else [session.requested_direction]
        )
        for direction in expected_directions:
            label = "업로드" if direction == "upload" else "다운로드"
            value = client_results.get(direction)
            if not isinstance(value, dict):
                raise SustainedCheckError(
                    f"성공 결과에 {label} 브라우저 측 전송 결과가 없습니다."
                )
            try:
                declared_bytes = int(value.get("bytes_transferred", 0))
                duration = float(value.get("actual_duration_seconds", 0))
            except (TypeError, ValueError) as exc:
                raise SustainedCheckError(
                    f"{label} 브라우저 측 전송 결과 형식이 올바르지 않습니다."
                ) from exc
            if declared_bytes <= 0:
                raise SustainedCheckError(
                    f"{label} 브라우저 측 전송량이 없어 성공으로 저장할 수 없습니다."
                )
            if (
                not math.isfinite(duration)
                or duration <= 0
                or duration > session.duration_seconds + 5
            ):
                raise SustainedCheckError(
                    f"{label} 브라우저 측 측정 시간이 올바르지 않습니다."
                )

            raw_intervals = value.get("intervals")
            if (
                not isinstance(raw_intervals, list)
                or not raw_intervals
                or len(raw_intervals) > session.duration_seconds + 2
            ):
                raise SustainedCheckError(
                    f"{label} 브라우저 측 구간 결과가 올바르지 않습니다."
                )

            interval_bytes = 0
            for item in raw_intervals:
                if not isinstance(item, dict):
                    raise SustainedCheckError(
                        f"{label} 브라우저 측 구간 결과가 올바르지 않습니다."
                    )
                try:
                    bytes_in_interval = int(item.get("bytes_transferred", -1))
                    interval_duration = float(item.get("duration_seconds", 0))
                except (TypeError, ValueError) as exc:
                    raise SustainedCheckError(
                        f"{label} 브라우저 측 구간 결과가 올바르지 않습니다."
                    ) from exc
                if (
                    bytes_in_interval < 0
                    or not math.isfinite(interval_duration)
                    or interval_duration <= 0
                    or interval_duration > 5
                ):
                    raise SustainedCheckError(
                        f"{label} 브라우저 측 구간 결과가 올바르지 않습니다."
                    )
                interval_bytes += bytes_in_interval

            if interval_bytes <= 0:
                raise SustainedCheckError(
                    f"{label} 브라우저 측 구간 전송량이 없어 성공으로 저장할 수 없습니다."
                )

            chunk_bytes = (
                self.settings.max_upload_chunk_bytes
                if direction == "upload"
                else self.settings.download_chunk_bytes
            )
            if not self._byte_counts_match(
                declared_bytes,
                interval_bytes,
                chunk_bytes=chunk_bytes,
                stream_count=session.stream_count,
            ):
                raise SustainedCheckError(
                    f"{label} 브라우저 측 전체 전송량과 구간 전송량 합계가 일치하지 않습니다."
                )

            server_bytes = int(session.measured_bytes.get(direction, 0))
            if server_bytes <= 0:
                raise SustainedCheckError(
                    f"{label} 서버 측 전송량이 없어 성공으로 저장할 수 없습니다."
                )
            if not self._byte_counts_match(
                declared_bytes,
                server_bytes,
                chunk_bytes=chunk_bytes,
                stream_count=session.stream_count,
            ):
                raise SustainedCheckError(
                    f"{label} 브라우저 측 전송량과 서버 측 전송량 차이가 허용 범위를 벗어났습니다."
                )

    @staticmethod
    def _byte_counts_match(
        first: int,
        second: int,
        *,
        chunk_bytes: int,
        stream_count: int,
    ) -> bool:
        reference = max(abs(int(first)), abs(int(second)), 1)
        streams = max(int(stream_count), 1)
        minimum = SUCCESS_BYTE_MIN_TOLERANCE_BYTES * streams
        chunk_cap = max(int(chunk_bytes) * streams, minimum)
        relative = math.ceil(reference * SUCCESS_BYTE_RELATIVE_TOLERANCE)
        tolerance = min(max(relative, minimum), chunk_cap)
        return abs(int(first) - int(second)) <= tolerance

    @staticmethod
    def _validated_client_result(value: Any, requested_duration: int) -> dict[str, Any]:
        if not isinstance(value, dict):
            return summarize_intervals(byte_count=0, duration_seconds=float(requested_duration), interval_bytes=[])
        try:
            byte_count = max(0, int(value.get("bytes_transferred", 0)))
            duration = float(value.get("actual_duration_seconds", requested_duration))
        except (TypeError, ValueError):
            raise SustainedCheckError("다운로드 측정 결과 형식이 올바르지 않습니다.")
        if not math.isfinite(duration) or duration <= 0 or duration > requested_duration + 5:
            raise SustainedCheckError("다운로드 측정 시간이 올바르지 않습니다.")
        interval_bytes = []
        interval_durations = []
        raw_intervals = value.get("intervals", [])
        if isinstance(raw_intervals, list):
            for item in raw_intervals[: requested_duration + 2]:
                if not isinstance(item, dict):
                    continue
                try:
                    bytes_in_interval = max(0, int(item.get("bytes_transferred", 0)))
                    interval_duration = float(item.get("duration_seconds", 1.0))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(interval_duration) or interval_duration <= 0 or interval_duration > 5:
                    continue
                interval_bytes.append(bytes_in_interval)
                interval_durations.append(interval_duration)
        return summarize_intervals(
            byte_count=byte_count,
            duration_seconds=duration,
            interval_bytes=interval_bytes,
            interval_durations=interval_durations,
        )

    def _persist_result(self, result: dict[str, Any]) -> None:
        ensure_sustained_storage(self.log_path, self.results_root)
        result_path = self.results_root / f"{result['session_id']}.json"
        rows = build_sustained_log_rows(result)
        with self.storage_lock:
            self._raise_if_measurement_recovery_pending_locked()
            marker_removed = commit_measurement_result(
                source="http_sustained",
                result_path=result_path,
                result=result,
                log_path=self.log_path,
                fieldnames=SUSTAINED_LOG_FIELDS,
                key_field="direction",
                rows=rows,
            )
            if not marker_removed:
                self._record_diagnostic_failure(
                    "http_sustained_transaction_cleanup_failed",
                    OSError("measurement transaction cleanup failed"),
                    warning=True,
                )
                return
            try:
                archive_csv_history(self.log_path, SUSTAINED_LOG_FIELDS)
            except (OSError, CsvIntegrityError) as exc:
                self._record_diagnostic_failure(
                    "http_sustained_csv_archive_failed",
                    exc,
                    warning=True,
                )
            prune_old_json_results(self.results_root)


def create_sustained_blueprint(
    *,
    log_path: Path,
    results_root: Path,
    normalize_ip: Callable[[str | None], str],
    settings: SustainedCheckSettings | None = None,
    measurement_gate: NetworkMeasurementGate | None = None,
    clock: Callable[[], float] = time.perf_counter,
    diagnostic_logger: logging.Logger | None = None,
) -> tuple[Blueprint, SustainedCheckManager]:
    manager = SustainedCheckManager(
        log_path=log_path,
        results_root=results_root,
        settings=settings,
        measurement_gate=measurement_gate,
        clock=clock,
        diagnostic_logger=diagnostic_logger,
    )
    blueprint = Blueprint("sustained_network", __name__)

    @blueprint.after_request
    def disable_sensitive_response_caching(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def client_ip() -> str:
        return normalize_ip(request.remote_addr)

    def error_response(exc: SustainedCheckError):
        messages = {
            400: "HTTP 측정 요청이 올바르지 않습니다. 1개 연결과 지원 시간·방향을 확인하세요.",
            403: "이 HTTP 측정 요청을 현재 접속 주소에서 처리할 수 없습니다.",
            404: "HTTP 측정 세션 또는 결과를 찾을 수 없습니다.",
            409: "HTTP 측정 요청이 현재 상태와 충돌했습니다.",
            413: "HTTP 업로드 측정 조각이 허용 크기를 초과했습니다.",
            500: "RESULT_READ_FAILED: HTTP 측정 결과를 안전하게 읽거나 생성할 수 없습니다.",
            503: "HTTP 측정 기능을 사용할 수 없습니다.",
        }
        return jsonify({"error": messages.get(exc.status_code, "HTTP 측정 요청을 처리할 수 없습니다.")}), exc.status_code

    @blueprint.get("/network-check/latency")
    def latency():
        response = jsonify({"ok": True})
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0"
        return response

    @blueprint.post("/network-check/sustained/sessions")
    def create_session():
        payload = request.get_json(silent=True) or {}
        try:
            session = manager.start_session(
                client_ip=client_ip(),
                direction=str(payload.get("direction", "")),
                duration_seconds=int(payload.get("duration_seconds", 0)),
                stream_count=int(payload.get("stream_count", 0)),
            )
        except (TypeError, ValueError):
            return jsonify({"error": "측정 조건 형식이 올바르지 않습니다."}), 400
        except SustainedCheckError as exc:
            return error_response(exc)
        return jsonify(
            {
                "session_id": session.session_id,
                "direction": session.requested_direction,
                "duration_seconds": session.duration_seconds,
                "warmup_seconds": manager.settings.warmup_seconds,
                "stream_count": session.stream_count,
                "max_upload_chunk_bytes": manager.settings.max_upload_chunk_bytes,
            }
        )

    @blueprint.post("/network-check/sustained/sessions/<session_id>/phase")
    def begin_phase(session_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(
                manager.begin_phase(
                    session_id,
                    client_ip(),
                    str(payload.get("direction", "")),
                    str(payload.get("phase", "")),
                )
            )
        except SustainedCheckError as exc:
            return error_response(exc)

    @blueprint.post("/network-check/sustained/sessions/<session_id>/upload/<int:stream_id>")
    def upload(session_id: str, stream_id: int):
        try:
            manager.validate_data_stream(session_id, client_ip(), "upload", stream_id)
            content_length = request.content_length
            if content_length is not None and content_length > manager.settings.max_upload_chunk_bytes:
                raise SustainedCheckError("업로드 측정 조각이 허용 크기보다 큽니다.", 413)
            received = 0
            while True:
                chunk = request.stream.read(manager.settings.upload_read_chunk_bytes)
                if not chunk:
                    break
                received += len(chunk)
                if received > manager.settings.max_upload_chunk_bytes:
                    raise SustainedCheckError("업로드 측정 조각이 허용 크기보다 큽니다.", 413)
                manager.record_bytes(session_id, client_ip(), "upload", len(chunk))
            return jsonify({"bytes_received": received})
        except SustainedCheckError as exc:
            return error_response(exc)

    @blueprint.get("/network-check/sustained/sessions/<session_id>/download/<int:stream_id>")
    def download(session_id: str, stream_id: int):
        try:
            manager.validate_data_stream(session_id, client_ip(), "download", stream_id)
        except SustainedCheckError as exc:
            return error_response(exc)

        request_ip = client_ip()

        def generate():
            while True:
                try:
                    if not manager.phase_active(session_id, request_ip, "download"):
                        break
                    manager.record_bytes(session_id, request_ip, "download", len(manager.download_chunk))
                    yield manager.download_chunk
                except SustainedCheckError:
                    break

        return Response(
            stream_with_context(generate()),
            mimetype="application/octet-stream",
            headers={
                "Cache-Control": "no-store, no-cache, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @blueprint.get("/network-check/sustained/sessions/<session_id>/status")
    def session_status(session_id: str):
        try:
            return jsonify(manager.status(session_id, client_ip()))
        except SustainedCheckError as exc:
            return error_response(exc)

    @blueprint.post("/network-check/sustained/sessions/<session_id>/complete")
    def complete(session_id: str):
        payload = request.get_json(silent=True) or {}
        requested_status = str(payload.get("status", "success"))
        status = requested_status if requested_status in {"success", "failure"} else "failure"
        try:
            return jsonify(manager.complete(session_id, client_ip(), payload, status=status))
        except SustainedCheckError as exc:
            return error_response(exc)

    @blueprint.post("/network-check/sustained/sessions/<session_id>/cancel")
    def cancel(session_id: str):
        try:
            return jsonify(manager.cancel(session_id, client_ip(), request.get_json(silent=True) or {}))
        except SustainedCheckError as exc:
            return error_response(exc)

    @blueprint.get("/network-check/sustained/results/<session_id>.json")
    def result_json(session_id: str):
        try:
            result_text = manager.result_text_for(session_id, client_ip())
        except SustainedCheckError as exc:
            return error_response(exc)
        return Response(
            result_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="network-check-{session_id}.json"'},
        )

    @blueprint.get("/network-check/sustained/results/<session_id>.xlsx")
    def result_excel(session_id: str):
        try:
            saved_result = manager.saved_result_for(session_id, client_ip())
            excel_payload = build_sustained_excel(saved_result)
        except SustainedCheckError as exc:
            return error_response(exc)
        except SustainedExcelError:
            return jsonify({"error": "HTTP 측정 Excel 결과 생성에 실패했습니다."}), 500

        response = send_file(
            BytesIO(excel_payload),
            mimetype=EXCEL_MIME_TYPE,
            as_attachment=True,
            download_name=build_sustained_excel_filename(saved_result),
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return blueprint, manager
