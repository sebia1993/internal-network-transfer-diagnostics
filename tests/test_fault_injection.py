from __future__ import annotations

import csv
import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import app as app_module
from app_version import APP_VERSION
from network_measurement import NetworkMeasurementGate
from network_probe.models import PROBE_PROTOCOL_VERSION, ProbeConfig
from network_probe.service import ProbeService
from runtime_stability import DataDirectoryLock, InstanceLockError, ensure_csv_integrity


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def wait_for_path(path: Path, process: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"fault worker exited early: stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(0.02)
    raise AssertionError(f"fault worker did not create ready marker: {path}")


def kill_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def start_worker(code: str, *args: str | Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *(str(arg) for arg in args)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_forced_process_exit_mid_upload_removes_dead_reservation_on_restart(tmp_path):
    storage_root = tmp_path / "uploads"
    ready_path = tmp_path / "upload-ready"
    code = """
import sys, time
from pathlib import Path
from app import reserve_upload_target
root, ready = map(Path, sys.argv[1:3])
reservation = reserve_upload_target(root, "fault.txt", confirm_duplicate=False)
reservation.temporary_path.write_bytes(b"partial-upload")
ready.write_text("ready", encoding="ascii")
time.sleep(60)
"""
    process = start_worker(code, storage_root, ready_path)
    try:
        wait_for_path(ready_path, process)
        assert len(list(storage_root.glob(f"{app_module.UPLOAD_ARTIFACT_PREFIX}*"))) == 2
        kill_process(process)

        removed = app_module.cleanup_stale_upload_artifacts(storage_root)

        assert removed == 2
        assert list(storage_root.glob(f"{app_module.UPLOAD_ARTIFACT_PREFIX}*")) == []
        assert not (storage_root / "fault.txt").exists()
    finally:
        kill_process(process)


def test_forced_process_exit_mid_csv_append_recovers_only_incomplete_tail(tmp_path):
    csv_path = tmp_path / "upload_log.csv"
    ready_path = tmp_path / "csv-ready"
    fieldnames = ["id", "memo"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fieldnames)
        writer.writerow(["complete", "preserve"])
    code = """
import os, sys, time
from pathlib import Path
path, ready = map(Path, sys.argv[1:3])
with path.open("ab") as handle:
    handle.write(b'broken,"unfinished')
    handle.flush()
    os.fsync(handle.fileno())
ready.write_text("ready", encoding="ascii")
time.sleep(60)
"""
    process = start_worker(code, csv_path, ready_path)
    try:
        wait_for_path(ready_path, process)
        kill_process(process)

        result = ensure_csv_integrity(csv_path, fieldnames)

        assert result.repaired is True
        assert result.backup_path is not None
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            assert list(csv.reader(handle)) == [fieldnames, ["complete", "preserve"]]
    finally:
        kill_process(process)


def test_forced_exit_after_file_commit_recovers_upload_log_on_restart(tmp_path):
    config_path = tmp_path / "config.ini"
    ready_path = tmp_path / "upload-committed"
    code = """
import sys, time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from app import (
    _storage_relative_path,
    build_download_url,
    commit_uploaded_file,
    ensure_directories,
    load_config,
    reserve_upload_target,
)
from upload_transactions import begin_upload_transaction, transaction_root_for_log
config_path, ready = map(Path, sys.argv[1:3])
config = load_config(config_path)
ensure_directories(config)
reservation = reserve_upload_target(
    config.storage_root,
    "committed.txt",
    confirm_duplicate=False,
)
row = {
    "upload_id": reservation.upload_id,
    "uploaded_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
    "original_filename": "committed.txt",
    "stored_filename": reservation.stored_filename,
    "storage_subdir": "",
    "storage_path": str(reservation.target_path.resolve()),
    "memo": "forced-exit",
    "download_url": build_download_url(reservation.upload_id, config),
}
begin_upload_transaction(
    transaction_root_for_log(config.log_path),
    operation="upload",
    phase="prepared",
    upload_id=reservation.upload_id,
    target_relative_path=_storage_relative_path(reservation.target_path, config),
    row=row,
)
commit_uploaded_file(BytesIO(b"committed-before-log"), reservation)
ready.write_text(reservation.upload_id, encoding="ascii")
time.sleep(60)
"""
    process = start_worker(code, config_path, ready_path)
    try:
        wait_for_path(ready_path, process)
        upload_id = ready_path.read_text(encoding="ascii")
        kill_process(process)

        config = app_module.load_config(config_path)
        app_module.ensure_directories(config)

        recovered = app_module.find_upload(upload_id, config)
        assert recovered is not None
        assert Path(recovered["storage_path"]).read_bytes() == b"committed-before-log"
        assert list((config.log_path.parent / "upload_transactions").glob("*.json")) == []
    finally:
        kill_process(process)


def test_forced_exit_after_delete_log_rewrite_removes_file_on_restart(tmp_path):
    config_path = tmp_path / "config.ini"
    config = app_module.load_config(config_path)
    app_module.ensure_directories(config)
    upload_id = "20260730-120000-delete12"
    target_path = config.storage_root / "delete-after-log.txt"
    target_path.write_bytes(b"delete-after-log")
    row = {
        "upload_id": upload_id,
        "uploaded_at": "2026-07-30 12:00:00 +0900",
        "original_filename": target_path.name,
        "stored_filename": target_path.name,
        "storage_subdir": "",
        "storage_path": str(target_path.resolve()),
        "memo": "forced-delete",
        "download_url": f"http://127.0.0.1:8000/download/{upload_id}",
    }
    app_module.append_upload_log(row, config)
    ready_path = tmp_path / "delete-log-removed"
    code = """
import sys, time
from pathlib import Path
from app import (
    _storage_relative_path,
    find_upload,
    load_config,
    record_file_path,
    remove_upload_log_row,
)
from upload_transactions import begin_upload_transaction, transaction_root_for_log
config_path, ready = map(Path, sys.argv[1:3])
config = load_config(config_path)
row = find_upload("20260730-120000-delete12", config)
target_path = record_file_path(row, config)
begin_upload_transaction(
    transaction_root_for_log(config.log_path),
    operation="delete",
    phase="prepared",
    upload_id=row["upload_id"],
    target_relative_path=_storage_relative_path(target_path, config),
    row=row,
)
assert remove_upload_log_row(row["upload_id"], config)
ready.write_text("ready", encoding="ascii")
time.sleep(60)
"""
    process = start_worker(code, config_path, ready_path)
    try:
        wait_for_path(ready_path, process)
        kill_process(process)
        assert target_path.exists()
        assert app_module.find_upload(upload_id, config) is None

        app_module.ensure_directories(config)

        assert not target_path.exists()
        assert app_module.find_upload(upload_id, config) is None
        assert list((config.log_path.parent / "upload_transactions").glob("*.json")) == []
    finally:
        kill_process(process)


@pytest.mark.parametrize(
    ("case", "expected_keys_before", "expected_keys_after"),
    [
        ("sustained-after-json", [], ["upload"]),
        (
            "sustained-full-after-first-row",
            ["upload"],
            ["upload", "download"],
        ),
        ("probe-after-json", [], ["upload"]),
        (
            "probe-full-after-first-row",
            ["upload"],
            ["upload", "download"],
        ),
    ],
)
def test_forced_exit_during_measurement_commit_recovers_exactly_once(
    tmp_path,
    case,
    expected_keys_before,
    expected_keys_after,
):
    config_path = tmp_path / "config.ini"
    ready_path = tmp_path / f"{case}.ready"
    session_id = uuid.uuid4().hex
    code = r"""
import sys
import time
from pathlib import Path

import measurement_transactions as transactions
from app import load_config
from network_measurement import NetworkMeasurementGate
from network_probe.models import ProbeConfig, ProbeSession
from network_probe.service import ProbeService
from network_sustained import SustainedCheckManager

case, config_value, ready_value, session_id = sys.argv[1:5]
config_path = Path(config_value)
ready_path = Path(ready_value)
config = load_config(config_path)

def checkpoint(source, checkpoint_name, row_index):
    after_json = case.endswith("after-json") and checkpoint_name == "json_committed"
    after_first_row = (
        case.endswith("after-first-row")
        and checkpoint_name == "csv_row_committed"
        and row_index == 1
    )
    if after_json or after_first_row:
        ready_path.write_text(
            f"{source}:{checkpoint_name}:{row_index}",
            encoding="ascii",
        )
        time.sleep(60)

transactions._checkpoint = checkpoint

if case.startswith("sustained"):
    manager = SustainedCheckManager(
        log_path=config.network_check_session_log_path,
        results_root=config.network_check_results_root,
    )
    directions = ["upload", "download"] if "full" in case else ["upload"]
    summary = {
        "bytes_transferred": 10_000_000,
        "actual_duration_seconds": 10.0,
        "average_mbps": 8.0,
        "median_mbps": 8.0,
        "min_mbps": 7.5,
        "max_mbps": 8.5,
        "variability_percent": 6.25,
        "intervals": [],
    }
    result = {
        "schema_version": 1,
        "session_id": session_id,
        "client_ip": "127.0.0.1",
        "started_at": "2026-07-30 13:00:00 +0900",
        "completed_at": "2026-07-30 13:00:10 +0900",
        "requested": {
            "direction": "full" if len(directions) == 2 else "upload",
            "duration_seconds": 10,
            "warmup_seconds": 3.0,
            "stream_count": 1,
        },
        "directions": {
            direction: dict(summary)
            for direction in directions
        },
        "http_latency": {
            "median_ms": 1.0,
            "min_ms": 1.0,
            "max_ms": 1.0,
            "samples_ms": [1.0],
        },
        "status": "success",
        "error": "",
    }
    manager._persist_result(result)
else:
    service = ProbeService(
        config=ProbeConfig(
            enabled=True,
            host="127.0.0.1",
            port=5201,
            log_path=config.network_probe_log_path,
            results_root=config.network_probe_results_root,
        ),
        measurement_gate=NetworkMeasurementGate(),
        normalize_ip=lambda value: value or "",
    )
    session = ProbeSession(
        session_id=session_id,
        session_token="fault-token",
        agent_id="b" * 32,
        agent_hostname="FAULT-PC",
        client_ip="127.0.0.1",
        server_host="127.0.0.1",
        requested_direction="full" if "full" in case else "upload",
        duration_seconds=10,
        stream_count=1,
        created_at_monotonic=0.0,
        created_at_text="2026-07-30 13:00:00 +0900",
        status="cancelled",
        error="fault injection",
    )
    service._persist_result(session)

raise AssertionError("fault checkpoint was not reached")
"""
    process = start_worker(
        code,
        case,
        config_path,
        ready_path,
        session_id,
    )
    try:
        wait_for_path(ready_path, process)
        kill_process(process)
        config = app_module.load_config(config_path)
        if case.startswith("sustained"):
            log_path = config.network_check_session_log_path
            result_path = (
                config.network_check_results_root / f"{session_id}.json"
            )
            key_field = "direction"
        else:
            log_path = config.network_probe_log_path
            result_path = (
                config.network_probe_results_root / f"{session_id}.json"
            )
            key_field = "phase"

        transaction_root = (
            config.log_path.parent / "measurement_transactions"
        )
        markers = list(transaction_root.rglob("*.json"))
        assert len(markers) == 1
        assert result_path.exists()
        result_before = result_path.read_bytes()
        assert json.loads(result_before.decode("utf-8"))["session_id"] == session_id
        with log_path.open("r", encoding="utf-8-sig", newline="") as handle:
            before_rows = [
                row
                for row in csv.DictReader(handle)
                if row["session_id"] == session_id
            ]
        assert [row[key_field] for row in before_rows] == expected_keys_before

        app_module.ensure_directories(config)

        with log_path.open("r", encoding="utf-8-sig", newline="") as handle:
            recovered_rows = [
                row
                for row in csv.DictReader(handle)
                if row["session_id"] == session_id
            ]
        assert [row[key_field] for row in recovered_rows] == expected_keys_after
        assert len({row[key_field] for row in recovered_rows}) == len(
            expected_keys_after
        )
        assert all(
            row["result_json"].endswith(f"/{session_id}.json")
            for row in recovered_rows
        )
        assert result_path.read_bytes() == result_before
        assert list(transaction_root.rglob("*.json")) == []

        csv_after_recovery = log_path.read_bytes()
        app_module.ensure_directories(config)
        assert log_path.read_bytes() == csv_after_recovery
        assert result_path.read_bytes() == result_before
        assert list(transaction_root.rglob("*.json")) == []
    finally:
        kill_process(process)


def test_data_directory_lock_recovers_after_owner_process_is_killed(tmp_path):
    lock_path = tmp_path / "data" / ".internal-upload.instance.lock"
    ready_path = tmp_path / "lock-ready"
    code = """
import sys, time
from pathlib import Path
from runtime_stability import DataDirectoryLock
lock_path, ready = map(Path, sys.argv[1:3])
lock = DataDirectoryLock(lock_path)
lock.acquire()
ready.write_text("ready", encoding="ascii")
time.sleep(60)
"""
    process = start_worker(code, lock_path, ready_path)
    try:
        wait_for_path(ready_path, process)
        with pytest.raises(InstanceLockError, match="이미 실행 중"):
            DataDirectoryLock(lock_path).acquire()

        kill_process(process)
        recovered = DataDirectoryLock(lock_path)
        recovered.acquire()
        recovered.release()
    finally:
        kill_process(process)


def test_active_tcp_session_shutdown_releases_port_and_measurement_gate(tmp_path):
    port = available_port()
    log_path = tmp_path / "data" / "network_probe_log.csv"
    results_root = tmp_path / "data" / "network_probe_results"
    config = ProbeConfig(
        enabled=True,
        host="127.0.0.1",
        port=port,
        log_path=log_path,
        results_root=results_root,
        long_poll_seconds=0.05,
    )
    gate = NetworkMeasurementGate()
    service = ProbeService(
        config=config,
        measurement_gate=gate,
        normalize_ip=lambda value: value or "",
    )
    assert service.start() is True
    registration = service.register_agent(
        {
            "agent_id": uuid.uuid4().hex,
            "hostname": "FAULT-CLIENT",
            "server_host": "127.0.0.1",
            "protocol_version": PROBE_PROTOCOL_VERSION,
            "client_version": APP_VERSION,
        },
        "127.0.0.1",
    )
    with service.condition:
        agent = service.agents[registration["agent_id"]]
        agent.connectivity_status = "ready"
        agent.connectivity_checked_at = service.clock()
    session = service.create_session(
        agent_id=registration["agent_id"],
        direction="full",
        duration_seconds=30,
        stream_count=4,
    )

    service.stop()

    assert gate.is_available() is True
    assert service.session_status(session["session_id"])["status"] == "cancelled"
    replacement = ProbeService(
        config=config,
        measurement_gate=NetworkMeasurementGate(),
        normalize_ip=lambda value: value or "",
    )
    try:
        assert replacement.start() is True
    finally:
        replacement.stop()
