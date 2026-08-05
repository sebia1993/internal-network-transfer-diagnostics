from __future__ import annotations

import csv
import json
import logging
import math
import secrets
import socket
import statistics
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app_version import APP_VERSION
from measurement_transactions import (
    commit_measurement_result,
    has_pending_measurement_transactions,
    measurement_transaction_root_for_log,
)
from network_measurement import NetworkMeasurementGate
from result_storage import prune_old_json_results
from runtime_stability import CsvIntegrityError, archive_csv_history

from .models import (
    PROBE_CONNECTIVITY_INTERVAL_SECONDS,
    PROBE_CONNECTIVITY_STALE_SECONDS,
    PROBE_DIRECTIONS,
    PROBE_DURATIONS,
    PROBE_PROTOCOL_VERSION,
    PROBE_STREAM_COUNTS,
    AgentRecord,
    ProbeConfig,
    ProbeSession,
)
from .protocol import ProbeProtocolError, recv_frame, send_frame
from .tcp_engine import (
    ProbeCancelled,
    ProbeTransferError,
    aggregate_stream_results,
    run_receiver_stream,
    run_sender_stream,
)


PROBE_LOG_FIELDS = [
    "checked_at",
    "session_id",
    "agent_id",
    "agent_hostname",
    "client_ip",
    "server_host",
    "requested_direction",
    "phase",
    "duration_seconds",
    "warmup_seconds",
    "stream_count",
    "sender_bytes",
    "receiver_bytes",
    "sender_mbps",
    "receiver_mbps",
    "median_rtt_ms",
    "min_rtt_ms",
    "cwnd_bytes",
    "retransmitted_bytes",
    "status",
    "error",
    "result_json",
]
TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
RESULT_SUBMISSION_TIMEOUT_SECONDS = 15.0
PROBE_MAX_REGISTERED_AGENTS = 256
PROBE_MAX_RESULT_BYTES = 4 * 1024**4
PROBE_MAX_RESULT_MBPS = 10_000_000.0
PROBE_STREAM_DURATION_EARLY_TOLERANCE_SECONDS = 1.0
PROBE_MAX_TELEMETRY_VALUE = (1 << 63) - 1
PROBE_MAX_TELEMETRY_ERROR_CHARS = 500
PROBE_RESULT_FIELDS = frozenset(
    {
        "role",
        "bytes",
        "duration_seconds",
        "average_mbps",
        "median_mbps",
        "min_mbps",
        "max_mbps",
        "intervals",
        "streams",
        "telemetry",
    }
)
PROBE_STREAM_RESULT_FIELDS = frozenset(
    {
        "stream_id",
        "role",
        "bytes",
        "duration_seconds",
        "mbps",
        "interval_bytes",
        "telemetry",
    }
)
PROBE_INTERVAL_FIELDS = frozenset({"index", "bytes", "mbps"})
PROBE_TELEMETRY_FIELDS = frozenset(
    {
        "available",
        "error",
        "rtt_us",
        "min_rtt_us",
        "cwnd_bytes",
        "bytes_retrans",
        "fast_retransmits",
        "duplicate_acks",
        "timeout_episodes",
    }
)
PROBE_TELEMETRY_COUNTER_FIELDS = (
    "rtt_us",
    "min_rtt_us",
    "cwnd_bytes",
    "bytes_retrans",
    "fast_retransmits",
    "duplicate_acks",
    "timeout_episodes",
)
CONNECTIVITY_FAILURE_MESSAGES = {
    "connect_timeout": "TCP 측정 포트 연결 시간이 초과되었습니다.",
    "connection_refused": "TCP 측정 포트에서 연결을 거부했습니다.",
    "name_resolution_failed": "서버 PC 이름을 IP 주소로 확인하지 못했습니다.",
    "network_unreachable": "서버 TCP 측정 포트까지 네트워크 경로가 없습니다.",
    "protocol_error": "TCP 연결 점검 응답을 확인하지 못했습니다.",
    "connection_error": "TCP 측정 포트에 연결하지 못했습니다.",
}


class ProbeServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def ensure_probe_storage(log_path: Path, results_root: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 0:
        return
    with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=PROBE_LOG_FIELDS).writeheader()


def _microseconds_to_milliseconds(value: Any) -> str | float:
    try:
        return round(float(value) / 1000, 3)
    except (TypeError, ValueError):
        return ""


def build_probe_log_rows(
    result: dict[str, Any],
) -> list[dict[str, object]]:
    requested = result["requested"]
    direction = requested["direction"]
    phases = (
        ("upload", "download")
        if direction == "full"
        else (direction,)
    )
    agent = result["agent"]
    result_json = (
        "data/network_probe_results/"
        f"{result['session_id']}.json"
    )
    rows: list[dict[str, object]] = []
    for phase in phases:
        combined = result["phases"].get(phase, {})
        sender = combined.get("sender", {})
        receiver = combined.get("receiver", {})
        telemetry = sender.get("telemetry", {})
        rows.append(
            {
                "checked_at": result["completed_at"],
                "session_id": result["session_id"],
                "agent_id": agent["agent_id"],
                "agent_hostname": agent["hostname"],
                "client_ip": agent["client_ip"],
                "server_host": result["server_host"],
                "requested_direction": direction,
                "phase": phase,
                "duration_seconds": requested["duration_seconds"],
                "warmup_seconds": requested["warmup_seconds"],
                "stream_count": requested["stream_count"],
                "sender_bytes": sender.get("bytes", ""),
                "receiver_bytes": receiver.get("bytes", ""),
                "sender_mbps": sender.get("average_mbps", ""),
                "receiver_mbps": receiver.get("average_mbps", ""),
                "median_rtt_ms": _microseconds_to_milliseconds(
                    telemetry.get("rtt_us")
                ),
                "min_rtt_ms": _microseconds_to_milliseconds(
                    telemetry.get("min_rtt_us")
                ),
                "cwnd_bytes": (
                    telemetry.get("cwnd_bytes", "")
                    if telemetry.get("available")
                    else ""
                ),
                "retransmitted_bytes": (
                    telemetry.get("bytes_retrans", "")
                    if telemetry.get("available")
                    else ""
                ),
                "status": result["status"],
                "error": result["error"],
                "result_json": result_json,
            }
        )
    return rows


class ProbeService:
    def __init__(
        self,
        *,
        config: ProbeConfig,
        measurement_gate: NetworkMeasurementGate,
        normalize_ip: Callable[[str | None], str],
        clock: Callable[[], float] = time.perf_counter,
        diagnostic_logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.measurement_gate = measurement_gate
        self.normalize_ip = normalize_ip
        self.clock = clock
        self.diagnostic_logger = diagnostic_logger or logging.getLogger(__name__)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.storage_lock = threading.Lock()
        self.agents: dict[str, AgentRecord] = {}
        self.sessions: dict[str, ProbeSession] = {}
        self.listener: socket.socket | None = None
        self.accept_thread: threading.Thread | None = None
        self.connection_handler_slots = threading.BoundedSemaphore(
            max(int(config.max_connection_handlers), 1)
        )
        self.connection_handlers_lock = threading.Lock()
        self.connection_handlers: dict[threading.Thread, socket.socket] = {}
        self.stop_event = threading.Event()
        self.start_error = ""
        self.started = False
        self.diagnostic_failure_count = 0
        self.last_diagnostic_event = ""
        self.last_diagnostic_error_type = ""
        ensure_probe_storage(config.log_path, config.results_root)

    def set_diagnostic_logger(self, logger: logging.Logger) -> None:
        self.diagnostic_logger = logger

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

    def start(self) -> bool:
        if not self.config.enabled:
            return False
        with self.lock:
            if self.started:
                return True
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((self.config.host, self.config.port))
                listener.listen(16)
                listener.settimeout(1.0)
            except OSError as exc:
                listener.close()
                self._record_diagnostic_failure("tcp_listener_bind_failed", exc)
                self.start_error = (
                    f"TCP {self.config.port} 포트를 열 수 없습니다. 오류 코드: "
                    "TCP_BIND_FAILED. 다른 프로그램의 포트 사용 여부와 방화벽 "
                    "정책을 확인하세요."
                )
                return False
            self.listener = listener
            self.stop_event.clear()
            self.started = True
            self.start_error = ""
            self.accept_thread = threading.Thread(target=self._accept_loop, name="network-probe-accept", daemon=True)
            self.accept_thread.start()
            return True

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            active = [session for session in self.sessions.values() if session.status not in TERMINAL_STATUSES]
        for session in active:
            self.cancel_session(session.session_id, error="서버가 종료되어 TCP 측정이 중단되었습니다.")
        listener = self.listener
        self.listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self.accept_thread is not None:
            self.accept_thread.join(timeout=3)
        with self.connection_handlers_lock:
            handlers = list(self.connection_handlers.items())
        for _, connection in handlers:
            self._close_socket(connection)
        handler_deadline = time.monotonic() + 6.0
        for thread, _ in handlers:
            thread.join(timeout=max(handler_deadline - time.monotonic(), 0.0))
        with self.lock:
            self.started = False

    def status_payload(self) -> dict[str, Any]:
        with self.lock:
            self._cleanup_terminal_sessions_locked()
            self._cleanup_expired_agents_locked()
            return {
                "enabled": self.config.enabled,
                "available": bool(self.config.enabled and self.started and not self.start_error),
                "port": self.config.port,
                "server_version": APP_VERSION,
                "protocol_version": PROBE_PROTOCOL_VERSION,
                "error": self.start_error,
                "active_session_id": next(
                    (session.session_id for session in self.sessions.values() if session.status not in TERMINAL_STATUSES),
                    "",
                ),
            }

    def register_agent(self, payload: dict[str, Any], client_ip: str) -> dict[str, Any]:
        if not self.config.enabled:
            raise ProbeServiceError("TCP 전송 성능 측정이 비활성화되어 있습니다.", 503)
        if not self.started or self.start_error:
            raise ProbeServiceError(self.start_error or "TCP 전송 성능 측정 서버를 사용할 수 없습니다.", 503)
        agent_id = str(payload.get("agent_id", "")).strip().lower()
        hostname = self._clean_hostname(payload.get("hostname"))
        server_host = str(payload.get("server_host", "")).strip()[:255]
        try:
            protocol_version = int(payload.get("protocol_version", 0))
        except (TypeError, ValueError) as exc:
            raise ProbeServiceError("에이전트 프로토콜 버전이 올바르지 않습니다.") from exc
        if len(agent_id) != 32 or any(character not in "0123456789abcdef" for character in agent_id):
            raise ProbeServiceError("에이전트 ID 형식이 올바르지 않습니다.")
        if protocol_version != PROBE_PROTOCOL_VERSION:
            raise ProbeServiceError(
                "서버와 클라이언트의 TCP 측정 프로토콜 버전이 다릅니다. "
                "서버 웹 화면에서 최신 Windows 클라이언트 ZIP을 다시 받으세요.",
                409,
            )
        client_version = self._clean_client_version(payload.get("client_version"))
        if not server_host:
            raise ProbeServiceError("서버 주소가 비어 있습니다.")

        now = self.clock()
        token = secrets.token_urlsafe(32)
        with self.condition:
            self._cleanup_expired_agents_locked()
            previous = self.agents.get(agent_id)
            if previous is None and len(self.agents) >= PROBE_MAX_REGISTERED_AGENTS:
                raise ProbeServiceError(
                    "등록 가능한 TCP 측정 클라이언트 수를 초과했습니다. 잠시 후 다시 시도하세요.",
                    429,
                )
            if previous and previous.busy_session_id:
                raise ProbeServiceError("측정 중인 에이전트는 다시 등록할 수 없습니다.", 409)
            self.agents[agent_id] = AgentRecord(
                agent_id=agent_id,
                token=token,
                hostname=hostname,
                client_ip=client_ip,
                server_host=server_host,
                protocol_version=protocol_version,
                client_version=client_version,
                registered_at=now,
                last_seen_at=now,
            )
            self.condition.notify_all()
        return {
            "agent_id": agent_id,
            "agent_token": token,
            "hostname": hostname,
            "client_ip": client_ip,
            "long_poll_seconds": self.config.long_poll_seconds,
            "agent_ttl_seconds": self.config.agent_ttl_seconds,
            "probe_port": self.config.port,
            "server_version": APP_VERSION,
            "protocol_version": PROBE_PROTOCOL_VERSION,
            "connectivity_interval_seconds": PROBE_CONNECTIVITY_INTERVAL_SECONDS,
            "connectivity_stale_seconds": PROBE_CONNECTIVITY_STALE_SECONDS,
        }

    def list_agents(self) -> list[dict[str, Any]]:
        with self.lock:
            self._cleanup_expired_agents_locked()
            now = self.clock()
            values = []
            for agent in sorted(self.agents.values(), key=lambda item: (item.hostname.lower(), item.client_ip)):
                connectivity = self._connectivity_payload_locked(agent, now=now)
                values.append(
                    {
                        "agent_id": agent.agent_id,
                        "hostname": agent.hostname,
                        "client_ip": agent.client_ip,
                        "status": "busy" if agent.busy_session_id else "online",
                        "last_seen_seconds_ago": round(max(now - agent.last_seen_at, 0.0), 1),
                        "client_version": agent.client_version,
                        "server_version": APP_VERSION,
                        "version_match": agent.client_version == APP_VERSION,
                        "probe_port": self.config.port,
                        **connectivity,
                    }
                )
            return values

    def report_connectivity_failure(
        self,
        agent_id: str,
        token: str,
        client_ip: str,
        error_code: str,
    ) -> dict[str, Any]:
        agent = self.authenticate_agent(agent_id, token, client_ip)
        normalized_code = error_code if error_code in CONNECTIVITY_FAILURE_MESSAGES else "connection_error"
        with self.condition:
            agent.connectivity_status = "failed"
            agent.connectivity_checked_at = self.clock()
            agent.connectivity_error_code = normalized_code
            agent.connectivity_message = CONNECTIVITY_FAILURE_MESSAGES[normalized_code]
            self.condition.notify_all()
            return self._connectivity_payload_locked(agent)

    def authenticate_agent(self, agent_id: str, token: str, client_ip: str) -> AgentRecord:
        with self.lock:
            agent = self.agents.get(agent_id)
            if agent is None or not secrets.compare_digest(agent.token, token):
                raise ProbeServiceError("에이전트 인증 정보가 올바르지 않습니다.", 401)
            if agent.client_ip != client_ip:
                raise ProbeServiceError("에이전트 등록 IP와 요청 IP가 다릅니다.", 403)
            agent.last_seen_at = self.clock()
            return agent

    def next_job(self, agent_id: str, token: str, client_ip: str) -> dict[str, Any]:
        deadline = self.clock() + self.config.long_poll_seconds
        with self.condition:
            while True:
                agent = self.authenticate_agent(agent_id, token, client_ip)
                if agent.pending_job is not None:
                    job = agent.pending_job
                    agent.pending_job = None
                    session = self.sessions.get(str(job["session_id"]))
                    if session is not None:
                        session.job_claimed = True
                        session.status = "attaching"
                        self._start_attach_watchdog(session.session_id, session.next_phase() or "")
                    return {"job": job}
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return {"job": None}
                self.condition.wait(timeout=min(remaining, 1.0))

    def _raise_if_measurement_recovery_pending_locked(self) -> None:
        if has_pending_measurement_transactions(
            measurement_transaction_root_for_log(self.config.log_path),
            "tcp_probe",
        ):
            raise ProbeServiceError(
                "이전 TCP 측정 결과 복구가 필요합니다. 오류 코드: "
                "MEASUREMENT_RECOVERY_PENDING. 서버를 다시 시작한 뒤 "
                "재측정하세요.",
                503,
            )

    def create_session(
        self,
        *,
        agent_id: str,
        direction: str,
        duration_seconds: int,
        stream_count: int,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise ProbeServiceError("TCP 전송 성능 측정이 비활성화되어 있습니다.", 503)
        if not self.started or self.start_error:
            raise ProbeServiceError(self.start_error or "TCP 전송 성능 측정 서버를 사용할 수 없습니다.", 503)
        if direction not in PROBE_DIRECTIONS:
            raise ProbeServiceError("측정 방향은 업로드, 다운로드, 전체 중 하나여야 합니다.")
        if duration_seconds not in PROBE_DURATIONS:
            raise ProbeServiceError("측정 시간은 10초 또는 30초만 선택할 수 있습니다.")
        if stream_count not in PROBE_STREAM_COUNTS:
            raise ProbeServiceError("TCP 스트림 수는 1개 또는 4개만 선택할 수 있습니다.")

        with self.storage_lock:
            self._raise_if_measurement_recovery_pending_locked()
        session_id = uuid.uuid4().hex
        if not self.measurement_gate.acquire(
            "tcp_probe",
            session_id,
            cancel_callback=lambda: self.cancel_session(
                session_id,
                error="최대 실행 시간을 초과하여 TCP 측정이 중단되었습니다.",
            ),
        ):
            raise ProbeServiceError("다른 네트워크 측정이 진행 중입니다.", 409)
        try:
            with self.storage_lock:
                self._raise_if_measurement_recovery_pending_locked()
            with self.condition:
                self._cleanup_terminal_sessions_locked()
                self._cleanup_expired_agents_locked()
                agent = self.agents.get(agent_id)
                if agent is None:
                    raise ProbeServiceError("온라인 상태인 측정 클라이언트를 찾을 수 없습니다.", 404)
                if agent.busy_session_id or agent.pending_job is not None:
                    raise ProbeServiceError("선택한 클라이언트에서 다른 측정이 진행 중입니다.", 409)
                connectivity = self._connectivity_payload_locked(agent)
                if connectivity["connectivity_status"] != "ready":
                    raise ProbeServiceError(
                        self._connectivity_start_error(connectivity),
                        409,
                    )
                session = ProbeSession(
                    session_id=session_id,
                    session_token=secrets.token_urlsafe(32),
                    agent_id=agent.agent_id,
                    agent_hostname=agent.hostname,
                    client_ip=agent.client_ip,
                    server_host=agent.server_host,
                    requested_direction=direction,
                    duration_seconds=duration_seconds,
                    stream_count=stream_count,
                    created_at_monotonic=self.clock(),
                    created_at_text=self._timestamp(),
                )
                self.sessions[session_id] = session
                agent.busy_session_id = session_id
                agent.pending_job = {
                    "session_id": session_id,
                    "session_token": session.session_token,
                    "probe_port": self.config.port,
                    "direction": direction,
                    "phases": session.phases(),
                    "duration_seconds": duration_seconds,
                    "warmup_seconds": self.config.warmup_seconds,
                    "stream_count": stream_count,
                    "protocol_version": PROBE_PROTOCOL_VERSION,
                }
                self.condition.notify_all()
        except Exception:
            self.measurement_gate.release("tcp_probe", session_id)
            raise
        self._start_job_claim_watchdog(session_id)
        return self.session_status(session_id)

    def session_status(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            self._cleanup_terminal_sessions_locked()
            session = self._require_session_locked(session_id)
            phases = session.phases()
            completed_count = len(session.combined_results)
            phase_progress = 0.0
            if session.status in {"warmup", "running", "awaiting_result"} and session.phase_started_at:
                elapsed = max(self.clock() - session.phase_started_at, 0.0)
                total = self.config.warmup_seconds + session.duration_seconds
                phase_progress = min(elapsed / total, 1.0)
            progress = (completed_count + phase_progress) / max(len(phases), 1) * 100
            if session.status == "completed":
                progress = 100.0
            return {
                "session_id": session.session_id,
                "status": session.status,
                "agent": {
                    "agent_id": session.agent_id,
                    "hostname": session.agent_hostname,
                    "client_ip": session.client_ip,
                },
                "requested": {
                    "direction": session.requested_direction,
                    "duration_seconds": session.duration_seconds,
                    "warmup_seconds": self.config.warmup_seconds,
                    "stream_count": session.stream_count,
                },
                "active_phase": session.active_phase,
                "progress_percent": round(min(max(progress, 0.0), 100.0), 1),
                "results": dict(session.combined_results),
                "error": session.error,
                "result_available": session.result_available,
                "persistence_complete": session.persistence_complete,
                "result_url": (
                    f"/api/network-probe/results/{session.session_id}.json"
                    if session.result_available
                    else ""
                ),
                "excel_url": (
                    f"/api/network-probe/results/{session.session_id}.xlsx"
                    if session.result_available
                    else ""
                ),
            }

    def cancel_session(self, session_id: str, error: str = "사용자가 TCP 측정을 취소했습니다.") -> dict[str, Any]:
        with self.lock:
            session = self._require_session_locked(session_id)
            if session.status in TERMINAL_STATUSES:
                return self.session_status(session_id)
            session.cancel_event.set()
            sockets = [sock for values in session.sockets.values() for sock in values.values()]
        for sock in sockets:
            self._close_socket(sock)
        self._finalize_session(session, "cancelled", error)
        return self.session_status(session_id)

    def control_status(self, session_id: str, agent_id: str, token: str, client_ip: str) -> dict[str, Any]:
        self.authenticate_agent(agent_id, token, client_ip)
        with self.lock:
            session = self._require_agent_session_locked(session_id, agent_id)
            return {
                "session_id": session.session_id,
                "cancelled": session.cancel_event.is_set() or session.status in {"cancelled", "failed"},
                "status": session.status,
                "error": session.error,
            }

    def complete_agent_phase(
        self,
        session_id: str,
        agent_id: str,
        token: str,
        client_ip: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.authenticate_agent(agent_id, token, client_ip)
        phase = str(payload.get("phase", ""))
        requested_status = str(payload.get("status", "success"))
        with self.lock:
            session = self._require_agent_session_locked(session_id, agent_id)
        if requested_status != "success":
            self._finalize_session(session, "failed", str(payload.get("error", "TCP 클라이언트 측정에 실패했습니다."))[:500])
            return self.session_status(session_id)
        if phase != session.next_phase():
            raise ProbeServiceError("TCP 측정 단계 순서가 올바르지 않습니다.", 409)

        try:
            result = self._validated_side_result(
                payload.get("result"),
                phase,
                expected_duration_seconds=session.duration_seconds,
                expected_stream_count=session.stream_count,
            )
        except ProbeServiceError as exc:
            self._finalize_session(session, "failed", str(exc))
            return self.session_status(session_id)
        deadline = self.clock() + 15.0
        with self.condition:
            session = self._require_agent_session_locked(session_id, agent_id)
            if session.status in TERMINAL_STATUSES:
                return self.session_status(session_id)
            if phase != session.next_phase():
                raise ProbeServiceError("TCP 측정 단계 순서가 올바르지 않습니다.", 409)
            streams = result.get("streams")
            if not isinstance(streams, list) or len(streams) != session.stream_count:
                raise ProbeServiceError("TCP 클라이언트 스트림 결과 수가 올바르지 않습니다.")
            session.agent_results[phase] = result
            while phase not in session.server_results and session.status not in TERMINAL_STATUSES:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 1.0))
            if session.status in TERMINAL_STATUSES:
                return self.session_status(session_id)
            if phase not in session.server_results:
                self._finalize_session(session, "failed", "서버 측 TCP 측정 결과가 제시간에 완료되지 않았습니다.")
                return self.session_status(session_id)
            server_result = session.server_results[phase]
            sender = result if result.get("role") == "sender" else server_result
            receiver = result if result.get("role") == "receiver" else server_result
            if sender.get("role") != "sender" or receiver.get("role") != "receiver":
                self._finalize_session(session, "failed", "TCP 송신·수신 결과 역할이 올바르지 않습니다.")
                return self.session_status(session_id)
            session.combined_results[phase] = {"sender": sender, "receiver": receiver}
            session.sockets.pop(phase, None)
            next_phase = session.next_phase()
            if next_phase is not None:
                session.active_phase = next_phase
                session.status = "attaching"
                self._start_attach_watchdog(session.session_id, next_phase)
                self.condition.notify_all()
                return self.session_status(session_id)

        self._finalize_session(session, "completed", "")
        return self.session_status(session_id)

    def result_path_for(self, session_id: str) -> Path:
        if len(session_id) != 32 or any(character not in "0123456789abcdef" for character in session_id):
            raise ProbeServiceError("TCP 측정 결과를 찾을 수 없습니다.", 404)
        path = self.config.results_root / f"{session_id}.json"
        if not path.exists() or not path.is_file():
            raise ProbeServiceError("TCP 측정 결과를 찾을 수 없습니다.", 404)
        return path

    def _read_result_payload(self, session_id: str) -> tuple[str, dict[str, Any]]:
        path = self.result_path_for(session_id)
        try:
            result_text = path.read_text(encoding="utf-8")
            saved = json.loads(result_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProbeServiceError(
                "TCP 측정 결과 파일을 읽을 수 없습니다. 오류 코드: RESULT_READ_FAILED.",
                500,
            ) from exc
        if not isinstance(saved, dict):
            raise ProbeServiceError("TCP 측정 결과 파일 형식이 올바르지 않습니다.", 500)
        return result_text, saved

    def saved_result_for(self, session_id: str) -> dict[str, Any]:
        _, saved = self._read_result_payload(session_id)
        return saved

    def result_text_for(self, session_id: str) -> str:
        result_text, _ = self._read_result_payload(session_id)
        return result_text

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            listener = self.listener
            if listener is None:
                break
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    break
                continue
            self._start_connection_handler(connection, self.normalize_ip(address[0]))

    def _start_connection_handler(self, connection: socket.socket, client_ip: str) -> bool:
        if not self.connection_handler_slots.acquire(blocking=False):
            self._close_socket(connection)
            return False

        thread = threading.Thread(
            target=self._run_connection_handler,
            args=(connection, client_ip),
            name="network-probe-connection",
            daemon=True,
        )
        with self.connection_handlers_lock:
            self.connection_handlers[thread] = connection
        try:
            thread.start()
        except Exception:
            with self.connection_handlers_lock:
                self.connection_handlers.pop(thread, None)
            self.connection_handler_slots.release()
            self._close_socket(connection)
            return False
        return True

    def _run_connection_handler(self, connection: socket.socket, client_ip: str) -> None:
        try:
            self._handle_connection(connection, client_ip)
        finally:
            current_thread = threading.current_thread()
            with self.connection_handlers_lock:
                self.connection_handlers.pop(current_thread, None)
            self.connection_handler_slots.release()

    def _handle_connection(self, connection: socket.socket, client_ip: str) -> None:
        handed_off = False
        try:
            connection.settimeout(5.0)
            payload = recv_frame(connection)
            if payload.get("type") == "connectivity_check":
                agent = self._handle_connectivity_check(client_ip, payload)
                send_frame(
                    connection,
                    {
                        "type": "connectivity_ready",
                        "protocol_version": PROBE_PROTOCOL_VERSION,
                        "server_version": APP_VERSION,
                        "client_version": agent.client_version,
                        "probe_port": self.config.port,
                    },
                )
                return
            session, phase, start_group = self._attach_stream(connection, client_ip, payload)
            send_frame(connection, {"type": "ready", "session_id": session.session_id, "stream_id": payload["stream_id"]})
            handed_off = True
            if start_group:
                threading.Thread(
                    target=self._run_server_phase,
                    args=(session.session_id, phase),
                    name=f"probe-{session.session_id[:8]}-{phase}",
                    daemon=True,
                ).start()
        except (OSError, ProbeProtocolError, ProbeServiceError, KeyError, TypeError, ValueError) as exc:
            try:
                send_frame(connection, {"type": "error", "error": str(exc)[:300]})
            except Exception:
                pass
        finally:
            if not handed_off:
                self._close_socket(connection)

    def _handle_connectivity_check(self, client_ip: str, payload: dict[str, Any]) -> AgentRecord:
        if int(payload.get("protocol_version", 0)) != PROBE_PROTOCOL_VERSION:
            raise ProbeServiceError(
                "TCP 측정 프로토콜 버전이 다릅니다. 최신 Windows 클라이언트 ZIP을 다시 받으세요.",
                409,
            )
        agent_id = str(payload.get("agent_id", ""))
        token = str(payload.get("agent_token", ""))
        client_version = self._clean_client_version(payload.get("client_version"))
        agent = self.authenticate_agent(agent_id, token, client_ip)
        if agent.client_version != client_version:
            raise ProbeServiceError("등록된 클라이언트 버전과 TCP 점검 버전이 다릅니다.", 409)
        with self.condition:
            agent.connectivity_status = "ready"
            agent.connectivity_checked_at = self.clock()
            agent.connectivity_error_code = ""
            agent.connectivity_message = "TCP 측정 포트 연결 준비가 완료되었습니다."
            self.condition.notify_all()
        return agent

    def _attach_stream(
        self,
        connection: socket.socket,
        client_ip: str,
        payload: dict[str, Any],
    ) -> tuple[ProbeSession, str, bool]:
        if payload.get("type") != "data_stream":
            raise ProbeServiceError("TCP 데이터 스트림 종류가 올바르지 않습니다.")
        if int(payload.get("protocol_version", 0)) != PROBE_PROTOCOL_VERSION:
            raise ProbeServiceError("TCP 측정 프로토콜 버전이 다릅니다.", 409)
        session_id = str(payload.get("session_id", ""))
        token = str(payload.get("session_token", ""))
        phase = str(payload.get("phase", ""))
        stream_id = int(payload.get("stream_id", -1))
        with self.lock:
            session = self._require_session_locked(session_id)
            if session.status in TERMINAL_STATUSES or session.cancel_event.is_set():
                raise ProbeServiceError("종료된 TCP 측정 세션입니다.", 409)
            if not secrets.compare_digest(session.session_token, token):
                raise ProbeServiceError("TCP 측정 세션 토큰이 올바르지 않습니다.", 403)
            if session.client_ip != client_ip:
                raise ProbeServiceError("에이전트 등록 IP와 TCP 접속 IP가 다릅니다.", 403)
            if phase != session.next_phase():
                raise ProbeServiceError("TCP 측정 단계 순서가 올바르지 않습니다.", 409)
            if stream_id < 0 or stream_id >= session.stream_count:
                raise ProbeServiceError("TCP 스트림 번호가 허용 범위를 벗어났습니다.")
            phase_sockets = session.sockets.setdefault(phase, {})
            if stream_id in phase_sockets:
                raise ProbeServiceError("같은 TCP 스트림 번호가 중복 연결되었습니다.", 409)
            phase_sockets[stream_id] = connection
            session.active_phase = phase
            session.status = "attaching"
            return session, phase, len(phase_sockets) == session.stream_count

    def _run_server_phase(self, session_id: str, phase: str) -> None:
        with self.lock:
            session = self._require_session_locked(session_id)
            sockets = dict(session.sockets.get(phase, {}))
            if len(sockets) != session.stream_count:
                self._finalize_session(session, "failed", "TCP 데이터 스트림 연결 수가 부족합니다.")
                return
            session.status = "warmup"
            session.phase_started_at = self.clock()
            cancel_event = session.cancel_event
        try:
            for stream_id, sock in sorted(sockets.items()):
                send_frame(
                    sock,
                    {
                        "type": "go",
                        "phase": phase,
                        "stream_id": stream_id,
                        "warmup_seconds": self.config.warmup_seconds,
                        "duration_seconds": session.duration_seconds,
                    },
                )
            threading.Thread(
                target=self._mark_measurement_running,
                args=(session_id, phase),
                daemon=True,
            ).start()
            results: list[dict[str, Any]] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()
            role = "receiver" if phase == "upload" else "sender"

            def worker(stream_id: int, sock: socket.socket) -> None:
                try:
                    if role == "sender":
                        result = run_sender_stream(
                            sock,
                            stream_id=stream_id,
                            warmup_seconds=self.config.warmup_seconds,
                            duration_seconds=session.duration_seconds,
                            cancel_event=cancel_event,
                        )
                    else:
                        result = run_receiver_stream(
                            sock,
                            stream_id=stream_id,
                            warmup_seconds=self.config.warmup_seconds,
                            duration_seconds=session.duration_seconds,
                            cancel_event=cancel_event,
                        )
                    with result_lock:
                        results.append(result)
                except BaseException as exc:
                    cancel_event.set()
                    with result_lock:
                        errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(stream_id, sock), daemon=True)
                for stream_id, sock in sorted(sockets.items())
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=self.config.warmup_seconds + session.duration_seconds + 15)
            if any(thread.is_alive() for thread in threads):
                raise ProbeTransferError("TCP 측정 작업이 제한 시간 안에 종료되지 않았습니다.")
            if errors:
                raise errors[0]
            aggregate = aggregate_stream_results(results, role=role, duration_seconds=session.duration_seconds)
            with self.condition:
                if session.status not in TERMINAL_STATUSES:
                    session.server_results[phase] = aggregate
                    session.status = "awaiting_result"
                    self.condition.notify_all()
                    self._start_result_watchdog(session.session_id, phase)
        except ProbeCancelled:
            if session.status not in TERMINAL_STATUSES:
                self._finalize_session(session, "cancelled", "TCP 측정이 취소되었습니다.")
        except BaseException as exc:
            if session.status not in TERMINAL_STATUSES:
                self._finalize_session(session, "failed", str(exc)[:500])
        finally:
            for sock in sockets.values():
                self._close_socket(sock)

    def _mark_measurement_running(self, session_id: str, phase: str) -> None:
        if self.stop_event.wait(self.config.warmup_seconds):
            return
        with self.lock:
            session = self.sessions.get(session_id)
            if session and session.active_phase == phase and session.status == "warmup":
                session.status = "running"

    def _start_attach_watchdog(self, session_id: str, phase: str) -> None:
        if not phase:
            return

        def watch() -> None:
            if self.stop_event.wait(self.config.stream_attach_timeout_seconds):
                return
            with self.lock:
                session = self.sessions.get(session_id)
                if session is None or session.status != "attaching" or session.next_phase() != phase:
                    return
                connected = len(session.sockets.get(phase, {}))
                if connected >= session.stream_count:
                    return
            self._finalize_session(session, "failed", "TCP 데이터 스트림 연결 시간이 초과되었습니다.")

        threading.Thread(target=watch, name=f"probe-watch-{session_id[:8]}-{phase}", daemon=True).start()

    def _start_job_claim_watchdog(self, session_id: str) -> None:
        def watch() -> None:
            if self.stop_event.wait(self.config.agent_ttl_seconds):
                return
            with self.lock:
                session = self.sessions.get(session_id)
            if session is None:
                return
            self._finalize_session(
                session,
                "failed",
                "TCP 측정 클라이언트가 제한 시간 안에 작업을 가져오지 않았습니다.",
                expected_status="queued",
            )

        threading.Thread(
            target=watch,
            name=f"probe-job-watch-{session_id[:8]}",
            daemon=True,
        ).start()

    def _start_result_watchdog(self, session_id: str, phase: str) -> None:
        def watch() -> None:
            if self.stop_event.wait(RESULT_SUBMISSION_TIMEOUT_SECONDS):
                return
            with self.lock:
                session = self.sessions.get(session_id)
            if session is None:
                return
            self._finalize_session(
                session,
                "failed",
                "TCP 측정 클라이언트 결과 수신 시간이 초과되었습니다.",
                expected_status="awaiting_result",
                expected_phase=phase,
            )

        threading.Thread(
            target=watch,
            name=f"probe-result-watch-{session_id[:8]}-{phase}",
            daemon=True,
        ).start()

    def _finalize_session(
        self,
        session: ProbeSession,
        status: str,
        error: str,
        *,
        expected_status: str | None = None,
        expected_phase: str | None = None,
    ) -> None:
        should_persist = False
        gate_release_completed = False
        sockets: list[socket.socket] = []
        with self.condition:
            if expected_status is not None and session.status != expected_status:
                return
            if expected_phase is not None and (
                session.active_phase != expected_phase or expected_phase in session.agent_results
            ):
                return
            if session.status not in TERMINAL_STATUSES:
                session.status = status
                session.error = error.strip()[:500]
                session.completed_at_monotonic = None
                session.result_available = False
                session.persistence_complete = False
                session.cancel_event.set()
                agent = self.agents.get(session.agent_id)
                if agent and agent.busy_session_id == session.session_id:
                    agent.busy_session_id = ""
                    agent.pending_job = None
                sockets = [sock for values in session.sockets.values() for sock in values.values()]
                session.sockets.clear()
                self.condition.notify_all()
                should_persist = True
        try:
            for sock in sockets:
                self._close_socket(sock)
            if should_persist:
                persistence_error: Exception | None = None
                try:
                    self._persist_result(session)
                except Exception as exc:
                    persistence_error = exc
                    self._record_diagnostic_failure(
                        "tcp_result_persistence_failed",
                        exc,
                    )
                with self.condition:
                    self.measurement_gate.release("tcp_probe", session.session_id)
                    gate_release_completed = True
                    if persistence_error is not None:
                        session.status = "failed"
                        session.error = (
                            str(persistence_error)
                            if isinstance(persistence_error, ProbeServiceError)
                            else (
                                "TCP 측정 결과 저장 실패. 오류 코드: "
                                "RESULT_WRITE_FAILED. 서버 진단 로그를 확인한 뒤 다시 "
                                "측정하세요."
                            )
                        )
                        session.result_available = False
                        session.persistence_complete = True
                        session.completed_at_monotonic = self.clock()
                        self._cleanup_terminal_sessions_locked(
                            preserve_session_id=session.session_id
                        )
                        self.condition.notify_all()
                    else:
                        session.result_available = True
                        session.persistence_complete = True
                        session.completed_at_monotonic = self.clock()
                        self._cleanup_terminal_sessions_locked(
                            preserve_session_id=session.session_id
                        )
                        self.condition.notify_all()
        finally:
            if should_persist and not gate_release_completed:
                self.measurement_gate.release("tcp_probe", session.session_id)

    def _persist_result(self, session: ProbeSession) -> None:
        completed_at = self._timestamp()
        result = {
            "schema_version": 1,
            "session_id": session.session_id,
            "started_at": session.created_at_text,
            "completed_at": completed_at,
            "agent": {
                "agent_id": session.agent_id,
                "hostname": session.agent_hostname,
                "client_ip": session.client_ip,
            },
            "server_host": session.server_host,
            "requested": {
                "direction": session.requested_direction,
                "duration_seconds": session.duration_seconds,
                "warmup_seconds": self.config.warmup_seconds,
                "stream_count": session.stream_count,
            },
            "phases": session.combined_results,
            "status": session.status,
            "error": session.error,
        }
        ensure_probe_storage(self.config.log_path, self.config.results_root)
        path = self.config.results_root / f"{session.session_id}.json"
        rows = build_probe_log_rows(result)
        with self.storage_lock:
            self._raise_if_measurement_recovery_pending_locked()
            marker_removed = commit_measurement_result(
                source="tcp_probe",
                result_path=path,
                result=result,
                log_path=self.config.log_path,
                fieldnames=PROBE_LOG_FIELDS,
                key_field="phase",
                rows=rows,
            )
            if not marker_removed:
                self._record_diagnostic_failure(
                    "tcp_measurement_transaction_cleanup_failed",
                    OSError("measurement transaction cleanup failed"),
                    warning=True,
                )
                return
            try:
                archive_csv_history(self.config.log_path, PROBE_LOG_FIELDS)
            except (OSError, CsvIntegrityError) as exc:
                self._record_diagnostic_failure(
                    "tcp_csv_archive_failed",
                    exc,
                    warning=True,
                )
            prune_old_json_results(self.config.results_root)

    def _validated_side_result(
        self,
        value: Any,
        phase: str,
        *,
        expected_duration_seconds: int,
        expected_stream_count: int,
    ) -> dict[str, Any]:
        if phase not in {"upload", "download"} or not isinstance(value, dict):
            raise ProbeServiceError("TCP 클라이언트 결과 형식이 올바르지 않습니다.")
        self._reject_unexpected_result_fields(value, PROBE_RESULT_FIELDS, "결과")
        expected_role = "sender" if phase == "upload" else "receiver"
        if value.get("role") != expected_role:
            raise ProbeServiceError("TCP 클라이언트 결과 역할이 올바르지 않습니다.")
        byte_count = self._bounded_result_int(
            value.get("bytes"),
            "전송량",
            minimum=0,
            maximum=PROBE_MAX_RESULT_BYTES,
        )
        if byte_count <= 0:
            raise ProbeServiceError("TCP 클라이언트 결과에 전송된 데이터가 없습니다.")
        duration = self._bounded_result_float(
            value.get("duration_seconds"),
            "측정 시간",
            minimum=0.0,
            maximum=float(max(PROBE_DURATIONS) + 5),
        )
        if (
            duration <= 0
            or not math.isclose(
                duration,
                float(expected_duration_seconds),
                rel_tol=0.0,
                abs_tol=0.001,
            )
        ):
            raise ProbeServiceError("TCP 클라이언트 측정 시간이 올바르지 않습니다.")

        metric_fields = ("average_mbps", "median_mbps", "min_mbps", "max_mbps")
        for field_name in metric_fields:
            if field_name in value:
                self._bounded_result_float(
                    value[field_name],
                    field_name,
                    minimum=0.0,
                    maximum=PROBE_MAX_RESULT_MBPS,
                )

        raw_intervals = value.get("intervals")
        if (
            not isinstance(raw_intervals, list)
            or len(raw_intervals) != expected_duration_seconds
        ):
            raise ProbeServiceError("TCP 클라이언트 구간 결과 수가 올바르지 않습니다.")
        sanitized_intervals: list[dict[str, Any]] = []
        interval_total_bytes = 0
        for offset, interval in enumerate(raw_intervals, start=1):
            if not isinstance(interval, dict):
                raise ProbeServiceError("TCP 클라이언트 구간 결과 형식이 올바르지 않습니다.")
            self._reject_unexpected_result_fields(interval, PROBE_INTERVAL_FIELDS, "구간 결과")
            interval_index = self._bounded_result_int(
                interval.get("index"),
                "구간 번호",
                minimum=1,
                maximum=expected_duration_seconds,
            )
            if interval_index != offset:
                raise ProbeServiceError("TCP 클라이언트 구간 번호가 올바르지 않습니다.")
            interval_bytes = self._bounded_result_int(
                interval.get("bytes"),
                "구간 전송량",
                minimum=0,
                maximum=PROBE_MAX_RESULT_BYTES,
            )
            if "mbps" in interval:
                self._bounded_result_float(
                    interval["mbps"],
                    "구간 처리량",
                    minimum=0.0,
                    maximum=PROBE_MAX_RESULT_MBPS,
                )
            interval_mbps = round(interval_bytes * 8 / 1_000_000, 2)
            if interval_mbps > PROBE_MAX_RESULT_MBPS:
                raise ProbeServiceError("TCP 클라이언트 구간 처리량이 허용 범위를 벗어났습니다.")
            sanitized_intervals.append(
                {
                    "index": interval_index,
                    "bytes": interval_bytes,
                    "mbps": interval_mbps,
                }
            )
            interval_total_bytes += interval_bytes
        if interval_total_bytes != byte_count:
            raise ProbeServiceError("TCP 클라이언트 구간 전송량 합계가 올바르지 않습니다.")

        streams = value.get("streams")
        if not isinstance(streams, list) or len(streams) != expected_stream_count:
            raise ProbeServiceError("TCP 클라이언트 스트림 결과 수가 올바르지 않습니다.")
        stream_ids: set[int] = set()
        stream_total_bytes = 0
        sanitized_streams: list[dict[str, Any]] = []
        for stream in streams:
            if not isinstance(stream, dict):
                raise ProbeServiceError("TCP 클라이언트 스트림 결과 형식이 올바르지 않습니다.")
            self._reject_unexpected_result_fields(stream, PROBE_STREAM_RESULT_FIELDS, "스트림 결과")
            if stream.get("role") != expected_role:
                raise ProbeServiceError("TCP 클라이언트 스트림 결과 역할이 올바르지 않습니다.")
            stream_id = self._bounded_result_int(
                stream.get("stream_id"),
                "스트림 번호",
                minimum=0,
                maximum=expected_stream_count - 1,
            )
            stream_bytes = self._bounded_result_int(
                stream.get("bytes"),
                "스트림 전송량",
                minimum=0,
                maximum=PROBE_MAX_RESULT_BYTES,
            )
            if stream_bytes <= 0:
                raise ProbeServiceError("TCP 클라이언트 스트림에 전송된 데이터가 없습니다.")
            stream_duration = self._bounded_result_float(
                stream.get("duration_seconds"),
                "스트림 측정 시간",
                minimum=0.0,
                maximum=float(expected_duration_seconds + 5),
            )
            if stream_duration <= 0:
                raise ProbeServiceError("TCP 클라이언트 스트림 측정 시간이 올바르지 않습니다.")
            if stream_duration < max(
                float(expected_duration_seconds)
                - PROBE_STREAM_DURATION_EARLY_TOLERANCE_SECONDS,
                0.001,
            ):
                raise ProbeServiceError(
                    "TCP 클라이언트 스트림 측정 시간이 요청보다 너무 짧습니다."
                )
            if stream_id in stream_ids:
                raise ProbeServiceError("TCP 클라이언트 스트림 번호가 올바르지 않습니다.")
            if "mbps" in stream:
                self._bounded_result_float(
                    stream["mbps"],
                    "스트림 처리량",
                    minimum=0.0,
                    maximum=PROBE_MAX_RESULT_MBPS,
                )
            stream_mbps = round(stream_bytes * 8 / stream_duration / 1_000_000, 2)
            if stream_mbps > PROBE_MAX_RESULT_MBPS:
                raise ProbeServiceError("TCP 클라이언트 스트림 처리량이 허용 범위를 벗어났습니다.")

            raw_interval_bytes = stream.get("interval_bytes")
            if (
                not isinstance(raw_interval_bytes, list)
                or len(raw_interval_bytes) != expected_duration_seconds
            ):
                raise ProbeServiceError("TCP 클라이언트 스트림 구간 결과 수가 올바르지 않습니다.")
            sanitized_interval_bytes = [
                self._bounded_result_int(
                    interval_bytes,
                    "스트림 구간 전송량",
                    minimum=0,
                    maximum=PROBE_MAX_RESULT_BYTES,
                )
                for interval_bytes in raw_interval_bytes
            ]
            if sum(sanitized_interval_bytes) != stream_bytes:
                raise ProbeServiceError("TCP 클라이언트 스트림 구간 전송량 합계가 올바르지 않습니다.")

            stream_telemetry = self._validated_telemetry(stream.get("telemetry"))
            stream_ids.add(stream_id)
            stream_total_bytes += stream_bytes
            sanitized_streams.append(
                {
                    "stream_id": stream_id,
                    "role": expected_role,
                    "bytes": stream_bytes,
                    "duration_seconds": stream_duration,
                    "mbps": stream_mbps,
                    "interval_bytes": sanitized_interval_bytes,
                    "telemetry": stream_telemetry,
                }
            )
        if stream_total_bytes != byte_count:
            raise ProbeServiceError("TCP 클라이언트 스트림 전송량 합계가 올바르지 않습니다.")

        average_mbps = round(byte_count * 8 / duration / 1_000_000, 2)
        if average_mbps > PROBE_MAX_RESULT_MBPS:
            raise ProbeServiceError("TCP 클라이언트 처리량이 허용 범위를 벗어났습니다.")
        interval_speeds = [float(interval["mbps"]) for interval in sanitized_intervals]
        return {
            "role": expected_role,
            "bytes": byte_count,
            "duration_seconds": duration,
            "average_mbps": average_mbps,
            "median_mbps": round(statistics.median(interval_speeds), 2)
            if interval_speeds
            else 0.0,
            "min_mbps": round(min(interval_speeds), 2) if interval_speeds else 0.0,
            "max_mbps": round(max(interval_speeds), 2) if interval_speeds else 0.0,
            "intervals": sanitized_intervals,
            "streams": sorted(sanitized_streams, key=lambda item: item["stream_id"]),
            "telemetry": self._validated_telemetry(value.get("telemetry")),
        }

    @staticmethod
    def _reject_unexpected_result_fields(
        value: dict[str, Any],
        allowed_fields: frozenset[str],
        label: str,
    ) -> None:
        if set(value) - allowed_fields:
            raise ProbeServiceError(f"TCP 클라이언트 {label}에 허용되지 않은 항목이 있습니다.")

    @staticmethod
    def _bounded_result_int(
        value: Any,
        label: str,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProbeServiceError(f"TCP 클라이언트 {label} 값이 올바르지 않습니다.")
        if value < minimum or value > maximum:
            raise ProbeServiceError(f"TCP 클라이언트 {label} 값이 허용 범위를 벗어났습니다.")
        return value

    @staticmethod
    def _bounded_result_float(
        value: Any,
        label: str,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProbeServiceError(f"TCP 클라이언트 {label} 값이 올바르지 않습니다.")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
            raise ProbeServiceError(f"TCP 클라이언트 {label} 값이 허용 범위를 벗어났습니다.")
        return numeric

    def _validated_telemetry(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProbeServiceError("TCP 클라이언트 상세 통계 형식이 올바르지 않습니다.")
        self._reject_unexpected_result_fields(value, PROBE_TELEMETRY_FIELDS, "상세 통계")
        available = value.get("available")
        if not isinstance(available, bool):
            raise ProbeServiceError("TCP 클라이언트 상세 통계 상태가 올바르지 않습니다.")

        if not available:
            if set(value) - {"available", "error"}:
                raise ProbeServiceError("사용할 수 없는 TCP 상세 통계에 수치 항목이 포함되어 있습니다.")
            sanitized: dict[str, Any] = {"available": False}
            if "error" in value:
                error = value["error"]
                if not isinstance(error, str) or len(error) > PROBE_MAX_TELEMETRY_ERROR_CHARS:
                    raise ProbeServiceError("TCP 클라이언트 상세 통계 오류 설명이 너무 깁니다.")
                sanitized["error"] = error
            return sanitized

        if "error" in value:
            raise ProbeServiceError("사용 가능한 TCP 상세 통계에 오류 설명이 포함되어 있습니다.")
        sanitized = {"available": True}
        for field_name in PROBE_TELEMETRY_COUNTER_FIELDS:
            if field_name not in value:
                continue
            counter = value[field_name]
            if counter is None and field_name in {"rtt_us", "min_rtt_us"}:
                sanitized[field_name] = None
                continue
            sanitized[field_name] = self._bounded_result_int(
                counter,
                field_name,
                minimum=0,
                maximum=PROBE_MAX_TELEMETRY_VALUE,
            )
        return sanitized

    def _require_session_locked(self, session_id: str) -> ProbeSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise ProbeServiceError("TCP 측정 세션을 찾을 수 없습니다.", 404)
        return session

    def _cleanup_terminal_sessions_locked(self, *, preserve_session_id: str = "") -> None:
        now = self.clock()
        ttl_seconds = max(float(self.config.terminal_session_ttl_seconds), 0.0)
        expired_ids = [
            session_id
            for session_id, session in self.sessions.items()
            if session_id != preserve_session_id
            and session.status in TERMINAL_STATUSES
            and session.completed_at_monotonic is not None
            and now - session.completed_at_monotonic > ttl_seconds
        ]
        for session_id in expired_ids:
            self.sessions.pop(session_id, None)

        terminal_sessions = sorted(
            (
                session
                for session in self.sessions.values()
                if session.session_id != preserve_session_id
                and session.status in TERMINAL_STATUSES
            ),
            key=lambda session: (
                session.completed_at_monotonic
                if session.completed_at_monotonic is not None
                else float("inf"),
                session.session_id,
            ),
        )
        terminal_count = len(terminal_sessions) + int(bool(preserve_session_id))
        overflow = terminal_count - max(int(self.config.max_terminal_sessions), 1)
        for session in terminal_sessions[: max(overflow, 0)]:
            self.sessions.pop(session.session_id, None)

    def _require_agent_session_locked(self, session_id: str, agent_id: str) -> ProbeSession:
        session = self._require_session_locked(session_id)
        if session.agent_id != agent_id:
            raise ProbeServiceError("이 에이전트의 TCP 측정 세션이 아닙니다.", 403)
        return session

    def _cleanup_expired_agents_locked(self) -> None:
        now = self.clock()
        expired = [agent_id for agent_id, agent in self.agents.items() if now - agent.last_seen_at > self.config.agent_ttl_seconds]
        for agent_id in expired:
            agent = self.agents.get(agent_id)
            if agent and agent.busy_session_id:
                session = self.sessions.get(agent.busy_session_id)
                if session and session.status not in TERMINAL_STATUSES:
                    self._finalize_session(session, "failed", "TCP 측정 클라이언트 연결이 만료되었습니다.")
            self.agents.pop(agent_id, None)

    def _connectivity_payload_locked(
        self,
        agent: AgentRecord,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = self.clock() if now is None else now
        checked_at = agent.connectivity_checked_at
        checked_seconds_ago = (
            round(max(current - checked_at, 0.0), 1)
            if checked_at is not None
            else None
        )
        status = agent.connectivity_status
        message = agent.connectivity_message
        if status == "ready" and (
            checked_at is None or current - checked_at > PROBE_CONNECTIVITY_STALE_SECONDS
        ):
            status = "stale"
            message = "최근 TCP 연결 점검 결과가 오래되었습니다."
        return {
            "connectivity_status": status,
            "connectivity_error_code": agent.connectivity_error_code,
            "connectivity_message": message,
            "connectivity_checked_seconds_ago": checked_seconds_ago,
        }

    def _connectivity_start_error(self, connectivity: dict[str, Any]) -> str:
        status = connectivity.get("connectivity_status")
        if status == "failed":
            detail = str(connectivity.get("connectivity_message") or "TCP 측정 포트 연결에 실패했습니다.")
            return (
                f"{detail} 서버 콘솔과 Windows 방화벽에서 TCP {self.config.port} "
                "인바운드 허용 상태를 확인하세요."
            )
        if status == "stale":
            return "최근 TCP 연결 점검 결과가 없어 측정을 시작할 수 없습니다. 자동 재점검을 기다려 주세요."
        return "선택한 클라이언트의 TCP 연결을 확인 중입니다. 잠시 후 다시 시도하세요."

    @staticmethod
    def _clean_hostname(value: Any) -> str:
        hostname = "".join(character for character in str(value or "") if character.isprintable()).strip()[:64]
        if not hostname:
            raise ProbeServiceError("클라이언트 PC 이름이 비어 있습니다.")
        return hostname

    @staticmethod
    def _clean_client_version(value: Any) -> str:
        version = "".join(character for character in str(value or "") if character.isprintable()).strip()[:32]
        if not version:
            raise ProbeServiceError(
                "클라이언트 버전 정보가 없습니다. 서버 웹 화면에서 최신 Windows 클라이언트 ZIP을 다시 받으세요.",
                409,
            )
        return version

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    @staticmethod
    def _microseconds_to_milliseconds(value: Any) -> str | float:
        return _microseconds_to_milliseconds(value)

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
