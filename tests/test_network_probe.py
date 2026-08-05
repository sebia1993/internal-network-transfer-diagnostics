from __future__ import annotations

import csv
import errno
import json
import logging
import os
import socket
import threading
import time
import uuid

import pytest

import measurement_transactions as transaction_module
import network_probe.service as service_module
from app_version import APP_VERSION
from network_measurement import NetworkMeasurementGate
from network_probe.models import PROBE_PROTOCOL_VERSION, ProbeConfig
from network_probe.agent import ProbeClientError, connectivity_error_code, normalize_server_url
from network_probe.protocol import recv_frame, send_frame
from network_probe.self_check import run_probe_self_check
from network_probe.service import ProbeService, ProbeServiceError
from network_probe.tcp_engine import (
    ProbeTransferError,
    aggregate_stream_results,
    run_receiver_stream,
    run_sender_stream,
)
from network_probe.windows_tcp_info import SIO_TCP_INFO, snapshot_tcp_info


def available_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def build_service(
    tmp_path,
    *,
    enabled: bool = True,
    attach_timeout: float = 10.0,
    agent_ttl: float = 10.0,
    terminal_ttl: float = 30 * 60.0,
    max_terminal_sessions: int = 100,
    max_connection_handlers: int = 16,
    clock=time.perf_counter,
    gate_clock=time.monotonic,
    gate_max_hold_seconds=None,
) -> tuple[ProbeService, NetworkMeasurementGate]:
    gate = NetworkMeasurementGate(
        clock=gate_clock,
        max_hold_seconds=gate_max_hold_seconds,
    )
    service = ProbeService(
        config=ProbeConfig(
            enabled=enabled,
            host="127.0.0.1",
            port=available_port(),
            log_path=tmp_path / "data" / "network_probe_log.csv",
            results_root=tmp_path / "data" / "network_probe_results",
            warmup_seconds=0.05,
            long_poll_seconds=0.05,
            agent_ttl_seconds=agent_ttl,
            stream_attach_timeout_seconds=attach_timeout,
            terminal_session_ttl_seconds=terminal_ttl,
            max_terminal_sessions=max_terminal_sessions,
            max_connection_handlers=max_connection_handlers,
        ),
        measurement_gate=gate,
        normalize_ip=lambda value: value or "",
        clock=clock,
    )
    return service, gate


def wait_for_session_persistence(
    service: ProbeService,
    session_id: str,
    *,
    timeout: float = 3.0,
) -> dict:
    with service.condition:
        completed = service.condition.wait_for(
            lambda: (
                session_id in service.sessions
                and service.sessions[session_id].persistence_complete
            ),
            timeout=timeout,
        )
    assert completed, "TCP probe result persistence did not complete"
    return service.session_status(session_id)


def check_connectivity(
    service: ProbeService,
    registration: dict,
    *,
    client_version: str = APP_VERSION,
) -> dict:
    with socket.create_connection(("127.0.0.1", service.config.port), timeout=3) as sock:
        sock.settimeout(3)
        send_frame(
            sock,
            {
                "type": "connectivity_check",
                "protocol_version": PROBE_PROTOCOL_VERSION,
                "agent_id": registration["agent_id"],
                "agent_token": registration["agent_token"],
                "client_version": client_version,
            },
        )
        return recv_frame(sock)


def register(
    service: ProbeService,
    *,
    client_version: str = APP_VERSION,
    preflight: bool = True,
) -> dict:
    registration = service.register_agent(
        {
            "agent_id": uuid.uuid4().hex,
            "hostname": "TEST-PC",
            "server_host": "127.0.0.1",
            "protocol_version": PROBE_PROTOCOL_VERSION,
            "client_version": client_version,
        },
        "127.0.0.1",
    )
    if preflight and service.started:
        response = check_connectivity(service, registration, client_version=client_version)
        assert response["type"] == "connectivity_ready"
    return registration


def run_client_phase(
    service: ProbeService,
    registration: dict,
    job: dict,
    phase: str,
    *,
    complete: bool = True,
) -> dict:
    sockets: dict[int, socket.socket] = {}
    try:
        for stream_id in range(int(job["stream_count"])):
            sock = socket.create_connection(("127.0.0.1", service.config.port), timeout=3)
            sock.settimeout(3)
            send_frame(
                sock,
                {
                    "type": "data_stream",
                    "protocol_version": PROBE_PROTOCOL_VERSION,
                    "session_id": job["session_id"],
                    "session_token": job["session_token"],
                    "phase": phase,
                    "stream_id": stream_id,
                },
            )
            assert recv_frame(sock)["type"] == "ready"
            sockets[stream_id] = sock
        for stream_id, sock in sockets.items():
            go = recv_frame(sock)
            assert go["type"] == "go"
            assert go["stream_id"] == stream_id

        role = "sender" if phase == "upload" else "receiver"
        cancel_event = threading.Event()
        results = []
        errors = []
        lock = threading.Lock()

        def worker(stream_id: int, sock: socket.socket) -> None:
            try:
                if role == "sender":
                    result = run_sender_stream(
                        sock,
                        stream_id=stream_id,
                        warmup_seconds=float(job["warmup_seconds"]),
                        duration_seconds=int(job["duration_seconds"]),
                        cancel_event=cancel_event,
                    )
                else:
                    result = run_receiver_stream(
                        sock,
                        stream_id=stream_id,
                        warmup_seconds=float(job["warmup_seconds"]),
                        duration_seconds=int(job["duration_seconds"]),
                        cancel_event=cancel_event,
                    )
                with lock:
                    results.append(result)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=item) for item in sockets.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        result = aggregate_stream_results(results, role=role, duration_seconds=int(job["duration_seconds"]))
        if not complete:
            return result
        return service.complete_agent_phase(
            str(job["session_id"]),
            str(registration["agent_id"]),
            str(registration["agent_token"]),
            "127.0.0.1",
            {"phase": phase, "status": "success", "result": result},
        )
    finally:
        for sock in sockets.values():
            sock.close()


def build_agent_result(
    *,
    stream_count: int,
    duration_seconds: float = 10.0,
    zero_stream_id: int | None = None,
) -> dict:
    streams = []
    interval_count = int(max(duration_seconds, 1))
    for stream_id in range(stream_count):
        byte_count = 0 if stream_id == zero_stream_id else 1024
        interval_bytes = [0] * interval_count
        interval_bytes[0] = byte_count
        streams.append(
            {
                "stream_id": stream_id,
                "role": "sender",
                "bytes": byte_count,
                "duration_seconds": duration_seconds,
                "interval_bytes": interval_bytes,
                "telemetry": {"available": False},
            }
        )
    total_bytes = sum(stream["bytes"] for stream in streams)
    intervals = [
        {
            "index": index + 1,
            "bytes": total_bytes if index == 0 else 0,
            "mbps": round(
                (total_bytes if index == 0 else 0) * 8 / 1_000_000,
                2,
            ),
        }
        for index in range(interval_count)
    ]
    return {
        "role": "sender",
        "bytes": total_bytes,
        "duration_seconds": duration_seconds,
        "average_mbps": 0.0,
        "median_mbps": 0.0,
        "min_mbps": 0.0,
        "max_mbps": 0.0,
        "intervals": intervals,
        "streams": streams,
        "telemetry": {"available": False},
    }


def test_probe_self_check_transfers_bytes():
    assert run_probe_self_check() == 0


def test_probe_connection_handlers_are_bounded_and_release_capacity(tmp_path, monkeypatch):
    service, _ = build_service(tmp_path, max_connection_handlers=2)
    release_handlers = threading.Event()
    both_started = threading.Event()
    started_count = 0
    started_lock = threading.Lock()

    class DummyConnection:
        def __init__(self):
            self.closed = False

        def shutdown(self, _how):
            return None

        def close(self):
            self.closed = True

    def blocking_handler(_connection, _client_ip):
        nonlocal started_count
        with started_lock:
            started_count += 1
            if started_count == 2:
                both_started.set()
        release_handlers.wait(timeout=3)

    monkeypatch.setattr(service, "_handle_connection", blocking_handler)
    first = DummyConnection()
    second = DummyConnection()
    rejected = DummyConnection()

    assert service._start_connection_handler(first, "127.0.0.1") is True
    assert service._start_connection_handler(second, "127.0.0.1") is True
    assert both_started.wait(timeout=2)
    assert service._start_connection_handler(rejected, "127.0.0.1") is False
    assert rejected.closed is True

    release_handlers.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with service.connection_handlers_lock:
            if not service.connection_handlers:
                break
        time.sleep(0.01)
    with service.connection_handlers_lock:
        assert service.connection_handlers == {}

    release_handlers.clear()
    replacement = DummyConnection()
    assert service._start_connection_handler(replacement, "127.0.0.1") is True
    service.stop()
    assert replacement.closed is True


def test_probe_client_rejects_invalid_server_port():
    with pytest.raises(ProbeClientError, match="포트"):
        normalize_server_url("127.0.0.1:not-a-port")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (socket.gaierror("name lookup failed"), "name_resolution_failed"),
        (ConnectionRefusedError("refused"), "connection_refused"),
        (socket.timeout("timed out"), "connect_timeout"),
        (OSError(errno.ENETUNREACH, "unreachable"), "network_unreachable"),
        (ProbeClientError("bad response"), "protocol_error"),
        (OSError("other"), "connection_error"),
    ],
)
def test_probe_connectivity_error_codes_are_stable(error, expected):
    assert connectivity_error_code(error) == expected


def test_windows_tcp_info_uses_winsock_vendor_ioctl_code():
    assert SIO_TCP_INFO == 0xD8000027


def test_disabled_probe_rejects_registration(tmp_path):
    service, _ = build_service(tmp_path, enabled=False)

    with pytest.raises(ProbeServiceError) as exc_info:
        register(service)

    assert exc_info.value.status_code == 503


def test_probe_registration_requires_current_protocol_and_client_version(tmp_path):
    service, _ = build_service(tmp_path)
    assert service.start() is True
    try:
        base = {
            "agent_id": uuid.uuid4().hex,
            "hostname": "TEST-PC",
            "server_host": "127.0.0.1",
        }
        with pytest.raises(ProbeServiceError, match="최신 Windows 클라이언트 ZIP") as protocol_error:
            service.register_agent({**base, "protocol_version": 1, "client_version": "v0.4.2"}, "127.0.0.1")
        assert protocol_error.value.status_code == 409

        with pytest.raises(ProbeServiceError, match="클라이언트 버전 정보") as version_error:
            service.register_agent(
                {**base, "agent_id": uuid.uuid4().hex, "protocol_version": PROBE_PROTOCOL_VERSION},
                "127.0.0.1",
            )
        assert version_error.value.status_code == 409
    finally:
        service.stop()


def test_probe_measurement_waits_for_successful_tcp_preflight(tmp_path):
    service, _ = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service, preflight=False)
        agent = service.list_agents()[0]
        assert agent["connectivity_status"] == "checking"
        assert agent["client_version"] == APP_VERSION
        assert agent["server_version"] == APP_VERSION

        with pytest.raises(ProbeServiceError, match="확인 중") as blocked:
            service.create_session(
                agent_id=registration["agent_id"],
                direction="upload",
                duration_seconds=10,
                stream_count=1,
            )
        assert blocked.value.status_code == 409

        response = check_connectivity(service, registration)
        assert response["type"] == "connectivity_ready"
        assert response["server_version"] == APP_VERSION
        assert response["protocol_version"] == PROBE_PROTOCOL_VERSION
        assert service.list_agents()[0]["connectivity_status"] == "ready"

        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )
        service.cancel_session(created["session_id"])
    finally:
        service.stop()


def test_probe_connectivity_failure_and_stale_result_block_measurement(tmp_path):
    current_time = [100.0]
    service, _ = build_service(tmp_path, agent_ttl=100.0, clock=lambda: current_time[0])
    assert service.start() is True
    try:
        registration = register(service)
        failed = service.report_connectivity_failure(
            registration["agent_id"],
            registration["agent_token"],
            "127.0.0.1",
            "connection_refused",
        )
        assert failed["connectivity_status"] == "failed"
        assert failed["connectivity_error_code"] == "connection_refused"
        with pytest.raises(ProbeServiceError, match="Windows 방화벽"):
            service.create_session(
                agent_id=registration["agent_id"],
                direction="upload",
                duration_seconds=10,
                stream_count=1,
            )

        assert check_connectivity(service, registration)["type"] == "connectivity_ready"
        current_time[0] += 46.0
        agent = service.list_agents()[0]
        assert agent["connectivity_status"] == "stale"
        assert agent["connectivity_checked_seconds_ago"] == 46.0
        with pytest.raises(ProbeServiceError, match="자동 재점검"):
            service.create_session(
                agent_id=registration["agent_id"],
                direction="upload",
                duration_seconds=10,
                stream_count=1,
            )
    finally:
        service.stop()


def test_probe_release_version_mismatch_warns_but_does_not_block(tmp_path):
    service, _ = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service, client_version="v0.4.3-rc.9")
        agent = service.list_agents()[0]
        assert agent["connectivity_status"] == "ready"
        assert agent["version_match"] is False

        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="download",
            duration_seconds=10,
            stream_count=1,
        )
        service.cancel_session(created["session_id"])
    finally:
        service.stop()


def test_gate_max_hold_cancels_and_records_stuck_tcp_session(tmp_path):
    gate_time = [0.0]
    service, gate = build_service(
        tmp_path,
        gate_clock=lambda: gate_time[0],
        gate_max_hold_seconds={"tcp_probe": 5.0},
    )
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )

        gate_time[0] = 6.0
        gate_status = gate.status()

        assert gate_status["active"] is False
        assert gate_status["expired_count"] == 1
        status = service.session_status(created["session_id"])
        assert status["status"] == "cancelled"
        assert "최대 실행 시간" in status["error"]
        saved = service.saved_result_for(created["session_id"])
        assert saved["status"] == "cancelled"
    finally:
        service.stop()


@pytest.mark.parametrize("stream_count", [1, 4])
def test_full_probe_session_runs_both_directions_and_persists(tmp_path, monkeypatch, stream_count):
    monkeypatch.setattr(service_module, "PROBE_DURATIONS", (1,))
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="full",
            duration_seconds=1,
            stream_count=stream_count,
        )
        job_response = service.next_job(
            registration["agent_id"], registration["agent_token"], "127.0.0.1"
        )
        job = job_response["job"]
        assert job["session_id"] == created["session_id"]

        first = run_client_phase(service, registration, job, "upload")
        assert first["status"] == "attaching"
        completed = run_client_phase(service, registration, job, "download")
        assert completed["status"] == "completed"
        assert completed["excel_url"] == f"/api/network-probe/results/{created['session_id']}.xlsx"
        assert completed["results"]["upload"]["receiver"]["bytes"] > 0
        assert completed["results"]["download"]["receiver"]["bytes"] > 0
        assert gate.is_available() is True

        result_path = service.result_path_for(created["session_id"])
        saved = json.loads(result_path.read_text(encoding="utf-8"))
        assert saved["status"] == "completed"
        assert service.saved_result_for(created["session_id"])["session_id"] == created["session_id"]
        assert "session_token" not in result_path.read_text(encoding="utf-8")
        rows = service.config.log_path.read_text(encoding="utf-8-sig").splitlines()
        assert len(rows) == 3
    finally:
        service.stop()


@pytest.mark.parametrize("stream_count", [1, 4])
def test_probe_rejects_zero_byte_agent_stream_and_fails_session(tmp_path, stream_count):
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=stream_count,
        )
        service.next_job(
            registration["agent_id"],
            registration["agent_token"],
            "127.0.0.1",
        )

        status = service.complete_agent_phase(
            created["session_id"],
            registration["agent_id"],
            registration["agent_token"],
            "127.0.0.1",
            {
                "phase": "upload",
                "status": "success",
                "result": build_agent_result(
                    stream_count=stream_count,
                    zero_stream_id=0,
                ),
            },
        )

        assert status["status"] == "failed"
        assert "전송된 데이터가 없습니다" in status["error"]
        assert status["persistence_complete"] is True
        assert gate.is_available() is True
    finally:
        service.stop()


@pytest.mark.parametrize(
    ("result", "expected_error"),
    [
        (None, "결과 형식이 올바르지 않습니다"),
        (
            build_agent_result(stream_count=1, duration_seconds=0),
            "측정 시간이 올바르지 않습니다",
        ),
    ],
)
def test_probe_missing_or_invalid_duration_agent_result_fails_session(
    tmp_path,
    result,
    expected_error,
):
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )
        service.next_job(
            registration["agent_id"],
            registration["agent_token"],
            "127.0.0.1",
        )

        status = service.complete_agent_phase(
            created["session_id"],
            registration["agent_id"],
            registration["agent_token"],
            "127.0.0.1",
            {
                "phase": "upload",
                "status": "success",
                "result": result,
            },
        )

        assert status["status"] == "failed"
        assert expected_error in status["error"]
        assert status["persistence_complete"] is True
        assert gate.is_available() is True
    finally:
        service.stop()


def test_probe_clean_close_zero_byte_stream_is_rejected():
    sender, receiver = socket.socketpair()
    try:
        sender.close()
        result = run_receiver_stream(
            receiver,
            stream_id=0,
            warmup_seconds=0,
            duration_seconds=1,
            cancel_event=threading.Event(),
        )

        assert result["bytes"] == 0
        with pytest.raises(ProbeTransferError, match="전송된 데이터가 없습니다"):
            aggregate_stream_results(
                [result],
                role="receiver",
                duration_seconds=1,
            )
    finally:
        receiver.close()


def test_probe_cancel_releases_global_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "PROBE_DURATIONS", (1,))
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=1,
            stream_count=1,
        )

        cancelled = service.cancel_session(created["session_id"])

        assert cancelled["status"] == "cancelled"
        assert gate.is_available() is True
        assert service.result_path_for(created["session_id"]).exists()
    finally:
        service.stop()


def test_probe_stream_attach_timeout_fails_session_and_releases_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "PROBE_DURATIONS", (1,))
    service, gate = build_service(tmp_path, attach_timeout=0.05)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=1,
            stream_count=1,
        )
        service.next_job(registration["agent_id"], registration["agent_token"], "127.0.0.1")

        status = wait_for_session_persistence(service, created["session_id"])

        assert status["status"] == "failed"
        assert "연결 시간이 초과" in status["error"]
        assert gate.is_available() is True
    finally:
        service.stop()


def test_probe_unclaimed_job_timeout_fails_session_and_releases_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "PROBE_DURATIONS", (1,))
    service, gate = build_service(tmp_path, agent_ttl=0.05)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=1,
            stream_count=1,
        )

        status = wait_for_session_persistence(service, created["session_id"])

        assert status["status"] == "failed"
        assert "작업을 가져오지 않았습니다" in status["error"]
        assert gate.is_available() is True
    finally:
        service.stop()


def test_probe_result_submission_timeout_fails_session_and_releases_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "PROBE_DURATIONS", (1,))
    monkeypatch.setattr(service_module, "RESULT_SUBMISSION_TIMEOUT_SECONDS", 0.05, raising=False)
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=1,
            stream_count=1,
        )
        job = service.next_job(
            registration["agent_id"], registration["agent_token"], "127.0.0.1"
        )["job"]

        run_client_phase(service, registration, job, "upload", complete=False)
        status = wait_for_session_persistence(service, created["session_id"])

        assert status["status"] == "failed"
        assert "결과 수신 시간이 초과" in status["error"]
        assert gate.is_available() is True
    finally:
        service.stop()


def test_probe_storage_failure_does_not_leave_measurement_gate_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "PROBE_DURATIONS", (1,))
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=1,
            stream_count=1,
        )

        def fail_persist(_session):
            raise OSError("disk full")

        monkeypatch.setattr(service, "_persist_result", fail_persist)

        result = service.cancel_session(created["session_id"])

        assert result["status"] == "failed"
        assert "결과 저장 실패" in result["error"]
        assert result["persistence_complete"] is True
        assert result["result_available"] is False
        assert result["result_url"] == ""
        assert result["excel_url"] == ""
        assert gate.is_available() is True
        assert service.diagnostic_status()["failure_count"] == 1
        assert service.diagnostic_status()["last_error_type"] == "OSError"
    finally:
        service.stop()


def test_probe_archive_failure_keeps_primary_result_and_records_safe_event(
    tmp_path,
    monkeypatch,
    caplog,
):
    logger = logging.getLogger("test.probe.archive")
    caplog.set_level(logging.INFO, logger=logger.name)
    service, gate = build_service(tmp_path)
    service.set_diagnostic_logger(logger)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )

        def fail_archive(*_args, **_kwargs):
            raise OSError("sensitive archive path")

        monkeypatch.setattr(
            service_module,
            "archive_csv_history",
            fail_archive,
        )

        result = service.cancel_session(created["session_id"])

        assert result["status"] == "cancelled"
        assert result["result_available"] is True
        assert service.saved_result_for(created["session_id"])["status"] == (
            "cancelled"
        )
        assert gate.is_available()
        assert service.diagnostic_status() == {
            "failure_count": 1,
            "last_event": "tcp_csv_archive_failed",
            "last_error_type": "OSError",
        }
        assert "sensitive archive path" not in caplog.text
    finally:
        service.stop()


def test_probe_marker_cleanup_failure_keeps_result_and_skips_archive(
    tmp_path,
    monkeypatch,
):
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )
        monkeypatch.setattr(
            transaction_module,
            "_finish_measurement_transaction",
            lambda _transaction: False,
        )
        monkeypatch.setattr(
            service_module,
            "archive_csv_history",
            lambda *_args, **_kwargs: pytest.fail("archive must be skipped"),
        )

        result = service.cancel_session(created["session_id"])

        assert result["status"] == "cancelled"
        assert result["result_available"] is True
        assert gate.is_available() is True
        assert service.diagnostic_status() == {
            "failure_count": 1,
            "last_event": "tcp_measurement_transaction_cleanup_failed",
            "last_error_type": "OSError",
        }

        session_ids_before = set(service.sessions)
        result_paths_before = set(service.config.results_root.glob("*.json"))
        monkeypatch.setattr(
            gate,
            "acquire",
            lambda *_args, **_kwargs: pytest.fail("gate must not be acquired"),
        )
        monkeypatch.setattr(
            service,
            "_start_job_claim_watchdog",
            lambda *_args, **_kwargs: pytest.fail("watchdog must not start"),
        )
        with pytest.raises(ProbeServiceError) as raised:
            service.create_session(
                agent_id=registration["agent_id"],
                direction="upload",
                duration_seconds=10,
                stream_count=1,
            )
        assert raised.value.status_code == 503
        assert "MEASUREMENT_RECOVERY_PENDING" in str(raised.value)
        assert set(service.sessions) == session_ids_before
        assert set(service.config.results_root.glob("*.json")) == result_paths_before
        agent = service.agents[registration["agent_id"]]
        assert agent.busy_session_id == ""
        assert agent.pending_job is None
        assert gate.status()["active"] is False
        assert len(
            transaction_module.load_measurement_transactions(
                transaction_module.measurement_transaction_root_for_log(
                    service.config.log_path
                )
            )
        ) == 1
    finally:
        service.stop()


def test_probe_pending_marker_after_gate_acquire_releases_gate_without_session(
    tmp_path,
    monkeypatch,
):
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        pending_checks = iter((False, True))
        monkeypatch.setattr(
            service_module,
            "has_pending_measurement_transactions",
            lambda *_args, **_kwargs: next(pending_checks),
        )
        monkeypatch.setattr(
            service,
            "_start_job_claim_watchdog",
            lambda *_args, **_kwargs: pytest.fail("watchdog must not start"),
        )
        session_ids_before = set(service.sessions)

        with pytest.raises(ProbeServiceError) as raised:
            service.create_session(
                agent_id=registration["agent_id"],
                direction="upload",
                duration_seconds=10,
                stream_count=1,
            )

        assert raised.value.status_code == 503
        assert "MEASUREMENT_RECOVERY_PENDING" in str(raised.value)
        assert set(service.sessions) == session_ids_before
        agent = service.agents[registration["agent_id"]]
        assert agent.busy_session_id == ""
        assert agent.pending_job is None
        assert gate.status()["active"] is False
    finally:
        service.stop()


def test_probe_preserves_pending_recovery_error_if_marker_appears_before_save(
    tmp_path,
    monkeypatch,
):
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        pending_checks = iter((False, False, True))
        monkeypatch.setattr(
            service_module,
            "has_pending_measurement_transactions",
            lambda *_args, **_kwargs: next(pending_checks),
        )
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )

        result = service.cancel_session(created["session_id"])

        assert result["status"] == "failed"
        assert result["result_available"] is False
        assert "MEASUREMENT_RECOVERY_PENDING" in result["error"]
        assert "RESULT_WRITE_FAILED" not in result["error"]
        assert gate.status()["active"] is False
    finally:
        service.stop()


def test_probe_result_urls_wait_until_persistence_succeeds(tmp_path, monkeypatch):
    service, gate = build_service(tmp_path)
    assert service.start() is True
    persistence_started = threading.Event()
    allow_persistence = threading.Event()
    gate_release_observations = []
    cancellation_result = []
    errors = []
    original_persist = service._persist_result
    original_gate_release = gate.release

    def blocking_persist(session):
        persistence_started.set()
        if not allow_persistence.wait(timeout=3):
            raise TimeoutError("test persistence was not released")
        original_persist(session)

    def blocking_gate_release(kind, owner_id):
        gate_release_observations.append(
            service.sessions[owner_id].persistence_complete
        )
        return original_gate_release(kind, owner_id)

    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )
        session_id = created["session_id"]
        monkeypatch.setattr(service, "_persist_result", blocking_persist)
        monkeypatch.setattr(gate, "release", blocking_gate_release)

        def cancel_session():
            try:
                cancellation_result.append(service.cancel_session(session_id))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=cancel_session)
        thread.start()
        assert persistence_started.wait(timeout=2)

        pending = service.session_status(session_id)
        assert pending["status"] == "cancelled"
        assert pending["persistence_complete"] is False
        assert pending["result_available"] is False
        assert pending["result_url"] == ""
        assert pending["excel_url"] == ""
        assert gate.is_available() is False

        allow_persistence.set()
        thread.join(timeout=3)

        assert not thread.is_alive()
        assert errors == []
        assert len(cancellation_result) == 1
        assert gate_release_observations == [False]
        completed = cancellation_result[0]
        assert completed["persistence_complete"] is True
        assert completed["result_available"] is True
        assert completed["result_url"].endswith(f"/{session_id}.json")
        assert completed["excel_url"].endswith(f"/{session_id}.xlsx")
        assert gate.is_available() is True
    finally:
        allow_persistence.set()
        service.stop()


def test_probe_terminal_sessions_are_bounded_and_release_socket_references(tmp_path):
    service, _ = build_service(tmp_path, max_terminal_sessions=2)
    assert service.start() is True

    class DummySocket:
        def __init__(self):
            self.closed = False

        def shutdown(self, _how):
            return None

        def close(self):
            self.closed = True

    try:
        registration = register(service)
        session_ids = []
        session_records = []
        sockets = []
        for _ in range(3):
            created = service.create_session(
                agent_id=registration["agent_id"],
                direction="upload",
                duration_seconds=10,
                stream_count=1,
            )
            session = service.sessions[created["session_id"]]
            dummy_socket = DummySocket()
            session.sockets["upload"] = {0: dummy_socket}
            service.cancel_session(created["session_id"])
            session_ids.append(created["session_id"])
            session_records.append(session)
            sockets.append(dummy_socket)

        assert session_ids[0] not in service.sessions
        assert set(service.sessions) == set(session_ids[1:])
        assert all(session.sockets == {} for session in session_records)
        assert all(sock.closed for sock in sockets)
    finally:
        service.stop()


def test_probe_terminal_session_ttl_prunes_memory_but_keeps_saved_result(tmp_path):
    current_time = [100.0]
    service, _ = build_service(
        tmp_path,
        terminal_ttl=5.0,
        clock=lambda: current_time[0],
    )
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="upload",
            duration_seconds=10,
            stream_count=1,
        )
        session_id = created["session_id"]
        service.cancel_session(session_id)
        result_path = service.result_path_for(session_id)

        current_time[0] += 6.0
        service.status_payload()

        assert session_id not in service.sessions
        assert result_path.exists()
        assert service.result_path_for(session_id) == result_path
        with pytest.raises(ProbeServiceError) as exc_info:
            service.session_status(session_id)
        assert exc_info.value.status_code == 404
    finally:
        service.stop()


def test_probe_csv_partial_write_failure_rolls_back_log_and_json(tmp_path, monkeypatch):
    service, gate = build_service(tmp_path)
    assert service.start() is True
    try:
        registration = register(service)
        created = service.create_session(
            agent_id=registration["agent_id"],
            direction="full",
            duration_seconds=10,
            stream_count=1,
        )
        original_log = service.config.log_path.read_bytes()
        original_writerow = csv.DictWriter.writerow
        call_count = 0

        def fail_second_writerow(writer, row):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("disk full")
            return original_writerow(writer, row)

        monkeypatch.setattr(csv.DictWriter, "writerow", fail_second_writerow)

        result = service.cancel_session(created["session_id"])

        assert result["status"] == "failed"
        assert "결과 저장 실패" in result["error"]
        assert service.config.log_path.read_bytes() == original_log
        with pytest.raises(ProbeServiceError) as exc_info:
            service.result_path_for(created["session_id"])
        assert exc_info.value.status_code == 404
        assert gate.is_available() is True
    finally:
        service.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows TCP_INFO 전용 검증")
def test_windows_tcp_info_returns_live_socket_statistics():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname(), timeout=3)
    server, _ = listener.accept()
    try:
        client.sendall(b"probe")
        assert server.recv(5) == b"probe"

        telemetry = snapshot_tcp_info(client)

        assert telemetry["available"] is True
        assert telemetry["rtt_us"] >= 0
        assert telemetry["cwnd_bytes"] > 0
        assert telemetry["bytes_out"] >= 5
    finally:
        client.close()
        server.close()
        listener.close()
