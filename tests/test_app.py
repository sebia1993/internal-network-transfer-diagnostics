import csv
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

import app as app_module
from app_version import APP_VERSION
from app import (
    CSV_FIELDS,
    NETWORK_CHECK_FIELDS,
    build_download_url,
    create_app,
    is_delete_allowed,
    is_loopback_url,
    load_config,
    read_network_check_log,
    read_upload_log,
    resolve_storage_path,
    run_smoke_check,
)
from network_sustained import SUSTAINED_LOG_FIELDS
from network_measurement import NetworkMeasurementGate
from network_probe.models import PROBE_PROTOCOL_VERSION
from network_probe.service import PROBE_LOG_FIELDS
from runtime_stability import (
    CsvIntegrityError,
    DataDirectoryLock,
    InsufficientStorageError,
    TimedSnapshotCache,
    UploadAdmissionController,
)
from tools.verify_release_zip import REQUIRED_FILES, verify_zip
from tools.generate_security_artifacts import generate_security_artifacts


def write_config(tmp_path: Path, *, base_url: str = "http://files.local:8000") -> Path:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                "HOST=0.0.0.0",
                "PORT=8000",
                f"BASE_URL={base_url}",
                "STORAGE_ROOT=uploads",
                "DELETE_ALLOWED_IPS=127.0.0.1,::1,10.10.10.5",
                "RECENT_LIMIT=50",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_repository_runtime_templates_are_sanitized_and_header_only():
    project_root = Path(__file__).resolve().parents[1]
    expected_headers = {
        "upload_log.csv": CSV_FIELDS,
        "network_check_log.csv": NETWORK_CHECK_FIELDS,
        "network_check_session_log.csv": SUSTAINED_LOG_FIELDS,
        "network_probe_log.csv": PROBE_LOG_FIELDS,
    }
    for filename, expected_header in expected_headers.items():
        with (project_root / "data" / filename).open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            assert list(csv.reader(handle)) == [expected_header]

    config = load_config(project_root / "config.ini")
    assert config.host == "0.0.0.0"
    assert config.port == 8000
    assert config.base_url == ""
    assert config.storage_root == project_root / "uploads"
    assert config.delete_allowed_ips == ("127.0.0.1", "::1")
    assert config.recent_limit == 50
    assert config.network_probe_enabled is True
    assert config.network_probe_port == 5201


@pytest.fixture()
def app_client(tmp_path):
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    app.config.update(TESTING=True)
    return app.test_client(), load_config(config_path), tmp_path


def post_file(client, filename="장애로그.txt", content=b"hello", **fields):
    data = {
        "file": (io.BytesIO(content), filename),
        "storage_subdir": fields.pop("storage_subdir", ""),
        "memo": fields.pop("memo", ""),
    }
    data.update(fields)
    return client.post("/upload", data=data, content_type="multipart/form-data")


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_base_url_download_link(tmp_path):
    config = load_config(write_config(tmp_path, base_url="http://10.10.10.25:8000"))
    assert build_download_url("abc123", config) == "http://10.10.10.25:8000/download/abc123"


def test_auto_ip_download_link(tmp_path):
    config = load_config(write_config(tmp_path, base_url=""))
    assert build_download_url("abc123", config, ip_address="10.10.10.25") == (
        "http://10.10.10.25:8000/download/abc123"
    )


def test_loopback_url_warning_detection():
    assert is_loopback_url("http://localhost:8000/download/a")
    assert is_loopback_url("http://127.0.0.1:8000/download/a")
    assert not is_loopback_url("http://10.10.10.25:8000/download/a")


def test_tcp_probe_is_enabled_by_default_when_setting_is_missing(tmp_path):
    config = load_config(write_config(tmp_path))

    assert config.network_probe_enabled is True
    assert config.network_probe_port == 5201


def test_storage_path_rejects_outside_root(tmp_path):
    config = load_config(write_config(tmp_path))
    with pytest.raises(ValueError):
        resolve_storage_path("../outside", config)
    with pytest.raises(ValueError):
        resolve_storage_path("C:\\temp", config)


def test_upload_saves_file_and_csv(app_client):
    client, config, _ = app_client
    response = post_file(client, memo="장애 로그", storage_subdir="case-001")

    assert response.status_code == 200
    rows = read_upload_log(config)
    assert len(rows) == 1
    row = rows[0]
    assert row["original_filename"] == "장애로그.txt"
    assert row["storage_subdir"] == "case-001"
    assert row["memo"] == "장애 로그"
    assert Path(row["storage_path"]).read_bytes() == b"hello"


def test_upload_rejects_when_reserved_disk_space_is_unavailable(app_client, monkeypatch):
    client, config, _ = app_client

    monkeypatch.setattr(
        app_module,
        "ensure_storage_capacity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            InsufficientStorageError("full")
        ),
    )

    response = post_file(client, filename="too-large.txt", content=b"payload")

    assert response.status_code == 507
    assert "저장 공간이 부족" in response.get_data(as_text=True)
    assert read_upload_log(config) == []
    assert list(config.storage_root.rglob(f"{app_module.UPLOAD_ARTIFACT_PREFIX}*")) == []


def test_upload_rejects_when_concurrent_upload_limit_is_reached(tmp_path):
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    controller = UploadAdmissionController(
        config.storage_root,
        max_concurrent=1,
        reserve_bytes=0,
        capacity_check=lambda *_args, **_kwargs: 10**12,
    )
    occupied = controller.acquire(1)
    flask_app = create_app(
        config_path,
        upload_admission_controller=controller,
    )
    flask_app.config.update(TESTING=True)
    try:
        with flask_app.test_client() as client:
            response = post_file(client, filename="busy.txt", content=b"payload")
    finally:
        occupied.release()

    assert response.status_code == 503
    assert "잠시 후 다시 시도" in response.get_data(as_text=True)
    assert read_upload_log(config) == []
    assert controller.status()["active_uploads"] == 0


def test_upload_cleans_partial_file_when_space_runs_out_during_copy(
    app_client,
    monkeypatch,
):
    client, config, _ = app_client
    checks = 0

    def fail_during_copy(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks >= 4:
            raise InsufficientStorageError("full during copy")
        return 10 * 1024 * 1024 * 1024

    monkeypatch.setattr(app_module, "ensure_storage_capacity", fail_during_copy)

    response = post_file(
        client,
        filename="partial.txt",
        content=b"x" * (app_module.UPLOAD_SPACE_RECHECK_BYTES + 1),
    )

    assert response.status_code == 507
    assert "파일을 저장하지 않았습니다" in response.get_data(as_text=True)
    assert read_upload_log(config) == []
    assert not (config.storage_root / "partial.txt").exists()
    assert list(config.storage_root.rglob(f"{app_module.UPLOAD_ARTIFACT_PREFIX}*")) == []


@pytest.mark.parametrize(
    "filename",
    [
        "tool.EXE",
        "installer.msi",
        "report.pdf.cmd",
        "script.ps1",
        "script.py",
        "script.sh",
        "macro.docm",
        "macro.xlsb",
        "legacy.xls",
        "shortcut.lnk",
        "disk.iso",
        "disk.vmdk",
        "android.apk",
    ],
)
def test_upload_rejects_executable_and_active_content_extensions(app_client, filename):
    client, config, _ = app_client

    response = post_file(client, filename=filename, content=b"blocked")

    assert response.status_code == 400
    assert "업로드할 수 없습니다" in response.get_data(as_text=True)
    assert read_upload_log(config) == []
    assert list(config.storage_root.iterdir()) == []


def test_upload_rejects_renamed_windows_pe_file(app_client):
    client, config, _ = app_client

    response = post_file(client, filename="report.txt", content=b"MZ" + b"payload")

    assert response.status_code == 400
    assert "Windows 실행 파일 내용" in response.get_data(as_text=True)
    assert read_upload_log(config) == []


@pytest.mark.parametrize("filename", ["trace.pcapng", "report.docx", "logs.zip", "capture.evtx"])
def test_upload_allows_business_diagnostic_and_archive_files(app_client, filename):
    client, config, _ = app_client

    response = post_file(client, filename=filename, content=b"allowed")

    assert response.status_code == 200
    assert read_upload_log(config)[0]["original_filename"] == filename


def test_upload_fsyncs_temporary_file_before_atomic_replace(app_client, monkeypatch):
    client, config, _ = app_client
    events = []
    original_fsync = os.fsync
    original_replace = app_module.durable_replace

    def recording_fsync(file_descriptor):
        events.append("fsync")
        return original_fsync(file_descriptor)

    def recording_replace(source, destination):
        if Path(source).suffix == ".part":
            events.append("replace")
            assert "fsync" in events
        return original_replace(source, destination)

    monkeypatch.setattr(app_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(app_module, "durable_replace", recording_replace)

    response = post_file(client, filename="atomic.txt", content=b"complete")

    assert response.status_code == 200
    assert events.index("fsync") < events.index("replace")
    assert (config.storage_root / "atomic.txt").read_bytes() == b"complete"
    assert list(config.storage_root.rglob(f"{app_module.UPLOAD_ARTIFACT_PREFIX}*")) == []


def test_upload_replace_failure_removes_temporary_artifacts(app_client, monkeypatch):
    client, config, _ = app_client
    original_replace = app_module.durable_replace

    def fail_upload_replace(source, destination):
        if Path(source).suffix == ".part":
            raise OSError("replace failed")
        return original_replace(source, destination)

    monkeypatch.setattr(app_module, "durable_replace", fail_upload_replace)

    response = post_file(client, filename="incomplete.txt", content=b"partial")

    assert response.status_code == 500
    assert "UPLOAD_PROCESSING_FAILED" in response.get_data(as_text=True)
    assert "replace failed" not in response.get_data(as_text=True)
    assert not (config.storage_root / "incomplete.txt").exists()
    assert list(config.storage_root.rglob(f"{app_module.UPLOAD_ARTIFACT_PREFIX}*")) == []


def test_cleanup_stale_upload_artifacts_only_removes_old_project_files(tmp_path):
    storage_root = tmp_path / "uploads"
    nested = storage_root / "case-001"
    nested.mkdir(parents=True)
    old_part = nested / f"{app_module.UPLOAD_ARTIFACT_PREFIX}old.part"
    old_lock = nested / f"{app_module.UPLOAD_ARTIFACT_PREFIX}old.lock"
    recent_part = nested / f"{app_module.UPLOAD_ARTIFACT_PREFIX}recent.part"
    user_part = nested / "capture.part"
    for path in (old_part, old_lock, recent_part, user_part):
        path.write_bytes(b"data")
    os.utime(old_part, (100, 100))
    os.utime(old_lock, (100, 100))

    removed = app_module.cleanup_stale_upload_artifacts(
        storage_root,
        older_than_seconds=100,
        now=300,
    )

    assert removed == 2
    assert not old_part.exists()
    assert not old_lock.exists()
    assert recent_part.exists()
    assert user_part.exists()


def test_cleanup_upload_artifacts_removes_dead_owner_and_preserves_live_owner(
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "uploads"
    storage_root.mkdir()
    dead_lock = storage_root / f"{app_module.UPLOAD_ARTIFACT_PREFIX}dead.lock"
    dead_part = storage_root / f"{app_module.UPLOAD_ARTIFACT_PREFIX}dead.part"
    live_lock = storage_root / f"{app_module.UPLOAD_ARTIFACT_PREFIX}live.lock"
    live_part = storage_root / f"{app_module.UPLOAD_ARTIFACT_PREFIX}live.part"
    dead_part.write_bytes(b"partial")
    live_part.write_bytes(b"partial")
    dead_lock.write_text(
        json.dumps({"pid": 101, "part": dead_part.name}),
        encoding="ascii",
    )
    live_lock.write_text(
        json.dumps({"pid": 202, "part": live_part.name}),
        encoding="ascii",
    )
    for path in (dead_lock, dead_part, live_lock, live_part):
        os.utime(path, (100, 100))
    monkeypatch.setattr(app_module, "is_process_running", lambda pid: pid == 202)

    removed = app_module.cleanup_stale_upload_artifacts(storage_root, now=200_000)

    assert removed == 2
    assert not dead_lock.exists()
    assert not dead_part.exists()
    assert live_lock.exists()
    assert live_part.exists()


def test_upload_log_reader_waits_for_in_progress_writer(app_client, monkeypatch):
    _, config, _ = app_client
    writer_started = threading.Event()
    allow_writer = threading.Event()
    reader_finished = threading.Event()
    errors = []
    observed_rows = []
    original_append = app_module._append_csv_row_with_rollback

    def blocking_append(log_path, fieldnames, row):
        writer_started.set()
        if not allow_writer.wait(timeout=3):
            raise TimeoutError("test writer was not released")
        return original_append(log_path, fieldnames, row)

    def write_log():
        try:
            app_module.append_upload_log(
                {
                    "upload_id": "concurrent-log-entry",
                    "uploaded_at": "2026-07-15 12:00:00 +0900",
                    "original_filename": "concurrent.txt",
                    "stored_filename": "concurrent.txt",
                    "storage_subdir": "",
                    "storage_path": str(config.storage_root / "concurrent.txt"),
                    "memo": "",
                    "download_url": "http://files.local:8000/download/concurrent-log-entry",
                },
                config,
            )
        except BaseException as exc:
            errors.append(exc)

    def read_log():
        try:
            observed_rows.extend(read_upload_log(config))
        except BaseException as exc:
            errors.append(exc)
        finally:
            reader_finished.set()

    monkeypatch.setattr(app_module, "_append_csv_row_with_rollback", blocking_append)
    writer = threading.Thread(target=write_log)
    reader = threading.Thread(target=read_log)
    writer.start()
    assert writer_started.wait(timeout=2)
    reader.start()

    assert reader_finished.wait(timeout=0.1) is False
    allow_writer.set()
    writer.join(timeout=3)
    reader.join(timeout=3)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert [row["upload_id"] for row in observed_rows] == ["concurrent-log-entry"]


def test_upload_log_snapshot_avoids_repeated_full_csv_reads(app_client, monkeypatch):
    client, config, _ = app_client
    assert post_file(client, filename="cached.txt", content=b"cached").status_code == 200
    original_open = Path.open
    read_open_count = 0

    def count_log_reads(path, *args, **kwargs):
        nonlocal read_open_count
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == config.log_path and mode == "r":
            read_open_count += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_log_reads)

    first = read_upload_log(config)
    second = read_upload_log(config)
    found = app_module.find_upload(first[0]["upload_id"], config)

    assert first == second
    assert found == first[0]
    assert read_open_count == 0


def test_upload_log_snapshot_reloads_after_external_file_change(app_client):
    client, config, _ = app_client
    assert post_file(client, filename="first.txt", content=b"first").status_code == 200
    assert [row["upload_id"] for row in read_upload_log(config)]
    external_row = {
        "upload_id": "external-change",
        "uploaded_at": "2026-07-16 12:00:00 +0900",
        "original_filename": "external.txt",
        "stored_filename": "external.txt",
        "storage_subdir": "",
        "storage_path": str(config.storage_root / "external.txt"),
        "memo": "",
        "download_url": "http://files.local:8000/download/external-change",
    }
    with config.log_path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=app_module.CSV_FIELDS).writerow(external_row)

    rows = read_upload_log(config)

    assert rows[0]["upload_id"] == "external-change"


def test_upload_log_failure_removes_saved_file(app_client, monkeypatch):
    client, config, _ = app_client

    def fail_append_upload_log(row, active_config):
        raise OSError("log write failed")

    monkeypatch.setattr(app_module, "append_upload_log", fail_append_upload_log)

    response = post_file(client, filename="orphan.txt", content=b"orphan")

    assert response.status_code == 500
    assert "UPLOAD_PROCESSING_FAILED" in response.get_data(as_text=True)
    assert "log write failed" not in response.get_data(as_text=True)
    assert not (config.storage_root / "orphan.txt").exists()


def test_upload_partial_log_write_failure_rolls_back_csv_and_file(app_client, monkeypatch):
    client, config, _ = app_client
    original_log = config.log_path.read_bytes()
    original_writerow = csv.DictWriter.writerow

    def write_then_fail(writer, row):
        original_writerow(writer, row)
        raise OSError("partial log write")

    monkeypatch.setattr(csv.DictWriter, "writerow", write_then_fail)

    response = post_file(client, filename="partial.txt", content=b"partial")

    assert response.status_code == 500
    assert "UPLOAD_PROCESSING_FAILED" in response.get_data(as_text=True)
    assert "partial log write" not in response.get_data(as_text=True)
    assert not (config.storage_root / "partial.txt").exists()
    assert config.log_path.read_bytes() == original_log


def test_memo_is_optional(app_client):
    client, config, _ = app_client
    response = post_file(client, filename="memo-optional.txt")

    assert response.status_code == 200
    assert read_upload_log(config)[0]["memo"] == ""


def test_duplicate_requires_confirmation_then_adds_id(app_client):
    client, config, _ = app_client
    assert post_file(client, filename="same.txt", content=b"one").status_code == 200

    conflict = post_file(client, filename="same.txt", content=b"two")
    assert conflict.status_code == 409
    assert "이미 존재".encode("utf-8") in conflict.data
    assert len(read_upload_log(config)) == 1

    confirmed = post_file(
        client,
        filename="same.txt",
        content=b"two",
        confirm_duplicate="1",
    )
    assert confirmed.status_code == 200
    rows = read_upload_log(config)
    assert len(rows) == 2
    assert rows[0]["stored_filename"].endswith("_same.txt")
    assert rows[0]["stored_filename"] != "same.txt"


def test_concurrent_duplicate_upload_does_not_overwrite_without_confirmation(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    flask_app = create_app(config_path)
    flask_app.config.update(TESTING=True)
    config = load_config(config_path)
    barrier = threading.Barrier(2)
    original_generate_upload_id = app_module.generate_upload_id
    responses = []
    errors = []

    def synchronized_generate_upload_id(now=None):
        barrier.wait(timeout=3)
        return original_generate_upload_id(now)

    def upload(content):
        try:
            with flask_app.test_client() as client:
                response = post_file(client, filename="same.txt", content=content)
                responses.append(response.status_code)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(app_module, "generate_upload_id", synchronized_generate_upload_id)
    threads = [
        threading.Thread(target=upload, args=(content,))
        for content in (b"first", b"second")
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(responses) == [200, 409]
    rows = read_upload_log(config)
    assert len(rows) == 1
    assert len({row["storage_path"] for row in rows}) == 1
    assert Path(rows[0]["storage_path"]).read_bytes() in {b"first", b"second"}


def test_download_by_id(app_client):
    client, config, _ = app_client
    post_file(client, filename="download.txt", content=b"download me")
    upload_id = read_upload_log(config)[0]["upload_id"]

    response = client.get(f"/download/{upload_id}")

    assert response.status_code == 200
    assert response.data == b"download me"
    assert response.mimetype == "application/octet-stream"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_download_and_delete_reject_csv_path_outside_storage_root(app_client):
    client, config, tmp_path = app_client
    post_file(client, filename="inside.txt", content=b"inside")
    row = read_upload_log(config)[0]
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"outside")
    row["storage_path"] = str(outside_file)
    app_module._write_upload_log_rows(config.log_path, [row])

    download = client.get(f"/download/{row['upload_id']}")
    deletion = client.post(
        f"/delete/{row['upload_id']}",
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert download.status_code == 404
    assert deletion.status_code == 409
    assert outside_file.read_bytes() == b"outside"
    assert read_upload_log(config)[0]["storage_path"] == str(outside_file)


def test_delete_requires_allowed_ip(app_client):
    client, config, _ = app_client
    post_file(client, filename="delete.txt", content=b"delete me")
    row = read_upload_log(config)[0]

    denied = client.post(
        f"/delete/{row['upload_id']}",
        environ_overrides={"REMOTE_ADDR": "10.10.10.6"},
    )
    assert denied.status_code == 403
    assert Path(row["storage_path"]).exists()
    assert len(read_upload_log(config)) == 1

    allowed = client.post(
        f"/delete/{row['upload_id']}",
        environ_overrides={"REMOTE_ADDR": "10.10.10.5"},
    )
    assert allowed.status_code == 302
    assert not Path(row["storage_path"]).exists()
    assert read_upload_log(config) == []


def test_delete_log_write_failure_preserves_file_and_record(app_client, monkeypatch):
    client, config, _ = app_client
    post_file(client, filename="keep-on-failure.txt", content=b"keep me")
    row = read_upload_log(config)[0]

    def fail_writerows(_writer, _rows):
        raise OSError("log write failed")

    monkeypatch.setattr(csv.DictWriter, "writerows", fail_writerows)

    response = client.post(
        f"/delete/{row['upload_id']}",
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 500
    assert "DELETE_PROCESSING_FAILED" in response.get_data(as_text=True)
    assert "log write failed" not in response.get_data(as_text=True)
    assert Path(row["storage_path"]).read_bytes() == b"keep me"
    assert read_upload_log(config) == [row]


def test_delete_file_failure_restores_upload_record(app_client, monkeypatch):
    client, config, _ = app_client
    post_file(client, filename="locked.txt", content=b"locked")
    row = read_upload_log(config)[0]
    file_path = Path(row["storage_path"])
    original_unlink = Path.unlink

    def fail_target_unlink(path, *args, **kwargs):
        if path == file_path:
            raise PermissionError("file is locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target_unlink)

    response = client.post(
        f"/delete/{row['upload_id']}",
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 500
    assert "DELETE_PROCESSING_FAILED" in response.get_data(as_text=True)
    assert "file is locked" not in response.get_data(as_text=True)
    assert file_path.read_bytes() == b"locked"
    assert read_upload_log(config) == [row]


def test_delete_button_only_for_allowed_ip(app_client):
    client, config, _ = app_client
    post_file(client, filename="button.txt")

    allowed = client.get("/", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    denied = client.get("/", environ_overrides={"REMOTE_ADDR": "10.10.10.6"})

    assert f"/delete/{read_upload_log(config)[0]['upload_id']}".encode() in allowed.data
    assert b"/delete/" not in denied.data


def test_delete_allowed_ip_normalization(tmp_path):
    config = load_config(write_config(tmp_path))
    assert is_delete_allowed("::ffff:127.0.0.1", config)
    assert not is_delete_allowed("10.10.10.6", config)


def test_network_check_tab_and_size_options(app_client):
    client, _, _ = app_client

    response = client.get("/")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "네트워크 체크" in body
    assert "1024MB" in body
    assert "평균 속도" in body
    assert "최근 전송 속도" in body
    assert "측정 취소" in body
    assert "HTTP 전송 측정" in body
    assert "측정 종료 기준" in body
    assert "데이터량" in body
    assert "측정 시간" in body
    assert "서버 웹 응답시간" in body
    assert "data-sustained-action" in body
    assert "최근 3초 평균 속도" in body
    assert "data-sustained-completed" in body
    assert "data-sustained-technical-details" in body
    assert "Excel 결과 받기" in body
    assert "data-sustained-excel" in body
    assert "data-probe-excel" in body
    assert "data-probe-json" not in body
    assert "data-sustained-json" not in body
    assert "data-sustained-stream" not in body
    assert "1GB 예상 시간" in body
    assert "TCP 전송 성능 측정" in body
    assert "고급 비교 측정" in body
    assert "4개 스트림 비교 측정" in body
    assert "data-probe-four-stream" in body
    assert "data-probe-stream=" not in body
    assert "data-probe-chart-panel" in body
    assert 'data-sustained-chart="upload"' in body
    assert 'data-sustained-chart="download"' in body
    assert 'data-probe-chart="upload"' in body
    assert 'data-probe-chart="download"' in body
    assert "throughput_chart.js" in body
    assert body.count("기술 상세 보기") == 2
    assert "업로드 실제 수신 속도" not in body
    assert "다운로드 실제 수신 속도" not in body
    assert "왕복 지연시간(RTT)" not in body
    assert "data-probe-cwnd" not in body
    assert "/network-check/upload" in body
    assert "/network-check/download" in body


def test_network_check_download_streams_and_logs(app_client, monkeypatch):
    client, config, _ = app_client
    monkeypatch.setattr(app_module, "MEGABYTE", 1024)

    response = client.get("/network-check/download?size_mb=10")

    assert response.status_code == 200
    assert len(response.data) == 10 * 1024
    rows = read_network_check_log(config)
    assert rows[0]["direction"] == "download"
    assert rows[0]["size_mb"] == "10"
    assert rows[0]["bytes_transferred"] == str(10 * 1024)
    assert rows[0]["status"] == "success"
    assert read_upload_log(config) == []


def test_network_check_upload_discards_body_and_logs(app_client, monkeypatch):
    client, config, _ = app_client
    monkeypatch.setattr(app_module, "MEGABYTE", 1024)

    started = client.post("/network-check/upload/start?size_mb=10")
    assert started.status_code == 200
    session_id = started.json["session_id"]

    first_chunk = client.post(
        f"/network-check/upload/chunk/{session_id}",
        data=b"x" * (6 * 1024),
        content_type="application/octet-stream",
    )
    assert first_chunk.status_code == 200
    assert first_chunk.json["bytes_received"] == 6 * 1024
    assert read_network_check_log(config) == []

    second_chunk = client.post(
        f"/network-check/upload/chunk/{session_id}",
        data=b"x" * (4 * 1024),
        content_type="application/octet-stream",
    )
    assert second_chunk.status_code == 200
    assert second_chunk.json["complete"]

    finished = client.post(f"/network-check/upload/finish/{session_id}")
    assert finished.status_code == 200
    assert finished.json["status"] == "success"
    rows = read_network_check_log(config)
    assert rows[0]["direction"] == "upload"
    assert rows[0]["bytes_transferred"] == str(10 * 1024)
    assert rows[0]["status"] == "success"
    assert read_upload_log(config) == []
    assert list(config.storage_root.rglob("*")) == []


def test_network_check_rejects_invalid_size(app_client):
    client, config, _ = app_client

    response = client.post("/network-check/upload/start?size_mb=11")

    assert response.status_code == 400
    assert read_network_check_log(config) == []


def test_network_check_upload_rejects_missing_session(app_client):
    client, config, _ = app_client

    chunk = client.post(
        "/network-check/upload/chunk/missing",
        data=b"x",
        content_type="application/octet-stream",
    )
    finished = client.post("/network-check/upload/finish/missing")

    assert chunk.status_code == 404
    assert finished.status_code == 404
    assert read_network_check_log(config) == []


def test_network_check_upload_logs_incomplete_body(app_client, monkeypatch):
    client, config, _ = app_client
    monkeypatch.setattr(app_module, "MEGABYTE", 1024)

    started = client.post("/network-check/upload/start?size_mb=10")
    session_id = started.json["session_id"]
    chunk = client.post(
        f"/network-check/upload/chunk/{session_id}",
        data=b"x" * (9 * 1024),
        content_type="application/octet-stream",
    )
    finished = client.post(f"/network-check/upload/finish/{session_id}")

    assert chunk.status_code == 200
    assert finished.status_code == 400
    assert finished.json["status"] == "failure"
    rows = read_network_check_log(config)
    assert rows[0]["direction"] == "upload"
    assert rows[0]["bytes_transferred"] == str(9 * 1024)
    assert rows[0]["status"] == "failure"


def test_network_check_upload_session_automatically_expires_and_releases_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "NETWORK_CHECK_UPLOAD_SESSION_TTL_SECONDS", 0.05)
    config_path = write_config(tmp_path)
    gate = NetworkMeasurementGate()
    flask_app = create_app(config_path, measurement_gate=gate)
    flask_app.config.update(TESTING=True)
    config = load_config(config_path)

    with flask_app.test_client() as client:
        started = client.post("/network-check/upload/start?size_mb=10")
    assert started.status_code == 200

    deadline = time.perf_counter() + 1
    while not gate.is_available() and time.perf_counter() < deadline:
        time.sleep(0.01)

    assert gate.is_available() is True
    rows = read_network_check_log(config)
    assert len(rows) == 1
    assert rows[0]["status"] == "failure"


def test_network_check_upload_session_stays_active_beyond_ttl_with_recent_chunks(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    config_path = write_config(tmp_path)
    clock = FakeMonotonicClock()
    gate = NetworkMeasurementGate(
        clock=clock,
        max_hold_seconds={
            "http_quick": app_module.NETWORK_CHECK_UPLOAD_SESSION_MAX_SECONDS
        },
    )
    flask_app = create_app(
        config_path,
        measurement_gate=gate,
        network_check_clock=clock,
    )
    flask_app.config.update(TESTING=True)
    config = load_config(config_path)

    with flask_app.test_client() as client:
        started = client.post("/network-check/upload/start?size_mb=10")
        session_id = started.json["session_id"]

        clock.advance(app_module.NETWORK_CHECK_UPLOAD_SESSION_TTL_SECONDS - 1)
        first_chunk = client.post(
            f"/network-check/upload/chunk/{session_id}",
            data=b"x" * 5,
            content_type="application/octet-stream",
        )
        clock.advance(app_module.NETWORK_CHECK_UPLOAD_SESSION_TTL_SECONDS - 1)

        expire_session = flask_app.extensions["network_check_upload_expire"]
        expire_session(session_id, 1)
        expire_session(session_id)
        assert gate.is_available() is False

        second_chunk = client.post(
            f"/network-check/upload/chunk/{session_id}",
            data=b"x" * 5,
            content_type="application/octet-stream",
        )
        finished = client.post(f"/network-check/upload/finish/{session_id}")

    assert started.status_code == 200
    assert first_chunk.status_code == 200
    assert second_chunk.status_code == 200
    assert finished.status_code == 200
    assert finished.json["status"] == "success"
    assert gate.is_available() is True
    rows = read_network_check_log(config)
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["bytes_transferred"] == "10"


def test_network_check_empty_upload_chunk_fails_and_releases_gate(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    config_path = write_config(tmp_path)
    gate = NetworkMeasurementGate()
    flask_app = create_app(config_path, measurement_gate=gate)
    flask_app.config.update(TESTING=True)
    config = load_config(config_path)

    with flask_app.test_client() as client:
        started = client.post("/network-check/upload/start?size_mb=10")
        session_id = started.json["session_id"]
        empty = client.post(
            f"/network-check/upload/chunk/{session_id}",
            data=b"",
            content_type="application/octet-stream",
        )
        replacement = client.post("/network-check/upload/start?size_mb=10")
        flask_app.extensions["shutdown_network_measurements"]()

    assert empty.status_code == 400
    assert empty.json["status"] == "failure"
    assert replacement.status_code == 200
    rows = read_network_check_log(config)
    assert [row["status"] for row in rows] == ["failure", "failure"]


def test_network_check_gate_max_hold_cancels_session_and_allows_retry(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    config_path = write_config(tmp_path)
    gate = NetworkMeasurementGate(
        max_hold_seconds={"http_quick": 0.02},
    )
    flask_app = create_app(config_path, measurement_gate=gate)
    flask_app.config.update(TESTING=True)
    config = load_config(config_path)

    with flask_app.test_client() as client:
        started = client.post("/network-check/upload/start?size_mb=10")
        time.sleep(0.04)
        status = gate.status()
        replacement = client.post("/network-check/upload/start?size_mb=10")
        flask_app.extensions["shutdown_network_measurements"]()

    assert started.status_code == 200
    assert status["active"] is False
    assert status["expired_count"] == 1
    assert replacement.status_code == 200
    rows = read_network_check_log(config)
    assert len(rows) == 2
    assert all(row["status"] == "failure" for row in rows)


def test_network_check_upload_session_expires_only_after_inactivity(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    config_path = write_config(tmp_path)
    gate = NetworkMeasurementGate()
    clock = FakeMonotonicClock()
    flask_app = create_app(
        config_path,
        measurement_gate=gate,
        network_check_clock=clock,
    )
    flask_app.config.update(TESTING=True)
    config = load_config(config_path)

    with flask_app.test_client() as client:
        started = client.post("/network-check/upload/start?size_mb=10")
        session_id = started.json["session_id"]
        clock.advance(app_module.NETWORK_CHECK_UPLOAD_SESSION_TTL_SECONDS)

        flask_app.extensions["network_check_upload_expire"](session_id)
        finished = client.post(f"/network-check/upload/finish/{session_id}")

    assert started.status_code == 200
    assert finished.status_code == 404
    assert gate.is_available() is True
    rows = read_network_check_log(config)
    assert len(rows) == 1
    assert rows[0]["status"] == "failure"


def test_shutdown_records_pending_quick_upload_and_releases_gate(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    config_path = write_config(tmp_path)
    gate = NetworkMeasurementGate()
    flask_app = create_app(config_path, measurement_gate=gate)
    flask_app.config.update(TESTING=True)
    config = load_config(config_path)

    with flask_app.test_client() as client:
        started = client.post("/network-check/upload/start?size_mb=10")

    assert started.status_code == 200
    assert gate.is_available() is False

    shutdown = flask_app.extensions["shutdown_network_measurements"]
    assert shutdown() == {"quick_uploads": 1, "sustained": 0}
    assert shutdown() == {"quick_uploads": 0, "sustained": 0}
    assert gate.is_available() is True

    rows = read_network_check_log(config)
    assert len(rows) == 1
    assert rows[0]["status"] == "failure"


def test_network_check_upload_rejects_oversized_body(app_client, monkeypatch):
    client, config, _ = app_client
    monkeypatch.setattr(app_module, "MEGABYTE", 1024)

    started = client.post("/network-check/upload/start?size_mb=10")
    session_id = started.json["session_id"]
    response = client.post(
        f"/network-check/upload/chunk/{session_id}",
        data=b"x" * (11 * 1024),
        content_type="application/octet-stream",
    )

    assert response.status_code == 400
    assert response.json["status"] == "failure"
    rows = read_network_check_log(config)
    assert rows[0]["bytes_transferred"] == str(11 * 1024)
    assert rows[0]["status"] == "failure"


def test_network_check_js_avoids_request_stream_uploads():
    script = Path("static/network_check.js").read_text(encoding="utf-8")

    assert "ReadableStream" not in script
    assert "duplex" not in script


def test_network_check_js_has_speed_and_cancel_guards():
    script = Path("static/network_check.js").read_text(encoding="utf-8")

    assert "AbortController" in script
    assert "confirm" in script
    assert "1024MB" in script
    assert "MB/s" in script
    assert "data-average-speed" in script
    assert "data-interval-speed" in script
    assert "data-cancel-check" in script
    assert "formatOneGigabyteEstimate" in script
    assert "전송한 데이터" in script


def test_sustained_network_js_uses_regular_post_chunks():
    script = Path("static/network_sustained.js").read_text(encoding="utf-8")

    assert "duplex" not in script
    assert "new ReadableStream" not in script
    assert "AbortController" in script
    assert "data-sustained-action" in script
    assert "latency_samples_ms" in script
    assert "window.confirm" in script
    assert "data-sustained-excel" in script
    assert "result.excel_url" in script
    assert "data-sustained-json" not in script
    assert "LATENCY_PROGRESS_PERCENT = 5" in script
    assert "MEASUREMENT_PROGRESS_PERCENT = 95" in script
    assert "MAX_IN_PROGRESS_PERCENT = 99.9" in script
    assert "createSustainedProgress" in script
    assert "requestAnimationFrame(tick)" in script
    assert "style.transform = `scaleX(" in script
    assert "Math.max(currentPercent" in script
    assert "result.status !== \"success\"" in script
    assert "progress.terminate(cancellationRequested" in script
    assert "function setPhase(" not in script
    assert "HTTP_STREAM_COUNT = 1" in script
    assert "stream_count: HTTP_STREAM_COUNT" in script
    assert "data-sustained-stream" not in script
    assert "selectedStreams" not in script
    assert "slice(-3)" in script
    assert "data-sustained-live-speed" in script
    assert "data-sustained-completed" in script
    assert "낮을수록 측정 중 속도가 일정함" in script
    assert "InternalUploadThroughputChart" in script
    assert "syncCharts" in script
    assert script.index("if (result.excel_url)") < script.index(
        'const partial = result.status !== "success"'
    )


def test_probe_network_js_uses_audience_friendly_summary():
    script = Path("static/network_probe.js").read_text(encoding="utf-8")

    assert "formatDirectionDifference" in script
    assert "formatRetransmission" in script
    assert "전체 송신량의" in script
    assert "운영체제에서 제공하지 않음" in script
    assert "측정 PC → 서버" in script
    assert "서버 → 측정 PC" in script
    assert "data-probe-four-stream" in script
    assert "fourStreamToggle.checked ? 4 : 1" in script
    assert "업로드·다운로드 평균 속도 차이" in script
    assert "TCP 왕복시간(RTT)" in script
    assert "1초 구간 최저 속도" in script
    assert "1초 구간 최고 속도" in script
    assert "data-probe-chart-panel" in script
    assert "data-probe-technical-details" in script
    assert "data-probe-cwnd" not in script
    assert "connectivity_status === \"ready\"" in script
    assert "TCP ${agent.probe_port} 연결 준비 완료" in script
    assert "약 20초 안에 자동 재점검" in script
    assert "최신 ZIP 사용 권장" in script
    assert "createProbeProgress" in script
    assert "Math.min(99.5" in script
    assert "animateTo(100, 300)" in script
    assert "style.transform = `scaleX(" in script


def test_shared_throughput_chart_has_readable_axes_and_interaction():
    script = Path("static/throughput_chart.js").read_text(encoding="utf-8")

    assert "niceMaximum" in script
    assert "평균 ${formatMbps(average)}" in script
    assert "최저 ${formatMbps(minimumValue)}" in script
    assert "최고 ${formatMbps(maximumValue)}" in script
    assert "pointermove" in script
    assert "ArrowLeft" in script
    assert "MB/s" in script
    assert "ResizeObserver" in script


def test_sustained_progress_uses_its_own_time_based_style():
    stylesheet = Path("static/style.css").read_text(encoding="utf-8")

    assert ".progress-bar[data-sustained-progress-bar]" in stylesheet
    assert ".progress-bar[data-probe-progress-bar]" in stylesheet
    assert "transform-origin: left center" in stylesheet
    assert "transition: none" in stylesheet
    assert ".chart-tooltip" in stylesheet


def test_windows_release_checksum_uses_portable_lf_line_ending():
    script = Path("tools/build_windows_release.ps1").read_text(encoding="utf-8")

    assert "[System.IO.File]::WriteAllText($ShaPath" in script
    assert '"$Hash  $PackageName.zip`n"' in script
    assert "ReadAllBytes($ShaPath) -contains 13" in script
    assert "Set-Content -Path $ShaPath" not in script


def test_windows_release_script_is_utf8_bom_for_windows_powershell():
    script_bytes = Path("tools/build_windows_release.ps1").read_bytes()
    script = script_bytes.decode("utf-8-sig")

    assert script_bytes.startswith(b"\xef\xbb\xbf")
    assert '$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)' in script
    assert "[System.IO.File]::WriteAllText($LauncherPath, $LauncherContent, $Utf8NoBom)" in script


def test_windows_release_build_requires_source_version_match():
    script = Path("tools/build_windows_release.ps1").read_text(encoding="utf-8")

    assert 'from app_version import APP_VERSION' in script
    assert '$SourceVersion -ne $Version' in script
    assert 'does not match requested release' in script
    assert '$SourceCommit = (git rev-parse HEAD).Trim()' in script


def test_windows_release_build_propagates_native_command_failures():
    script = Path("tools/build_windows_release.ps1").read_text(encoding="utf-8-sig")

    for operation in (
        "Source version lookup",
        "Server version metadata generation",
        "Client version metadata generation",
        "Server executable build",
        "Client executable build",
        "Security artifact generation",
        "Release ZIP verification",
    ):
        assert f'Assert-NativeSuccess "{operation}" $LASTEXITCODE' in script


def test_windows_release_build_removes_runtime_lock_and_diagnostics_after_smoke_check():
    script = Path("tools/build_windows_release.ps1").read_text(encoding="utf-8")

    assert 'data/.internal-upload.instance.lock' in script
    assert 'data/diagnostics' in script
    assert 'Remove-Item $RuntimeLock -Force' in script
    assert 'Remove-Item $RuntimeDiagnostics -Recurse -Force' in script


def test_windows_release_workflow_checks_all_release_runtime_modules_and_scripts():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert (
        "startup_ports.py runtime_stability.py upload_transactions.py "
        "measurement_transactions.py network_sustained.py"
    ) in workflow
    assert "node --check static/operations_dashboard.js" in workflow
    assert "python tools/run_stability_fault_suite.py" in workflow
    for operation in (
        "Python compileall",
        "network_check.js syntax check",
        "network_sustained.js syntax check",
        "network_probe.js syntax check",
        "throughput_chart.js syntax check",
        "operations_dashboard.js syntax check",
        "Python regression tests",
        "Stability fault suite",
        "Python dependency check",
    ):
        assert f'Assert-NativeSuccess "{operation}" $LASTEXITCODE' in workflow
    assert f'default: "{APP_VERSION}"' in workflow
    assert (
        'git fetch --force --no-tags origin '
        '"refs/tags/${env:RELEASE_VERSION}:refs/tags/${env:RELEASE_VERSION}"'
    ) in workflow
    assert "Unable to fetch release tag" in workflow
    assert 'git show-ref --verify --quiet "refs/tags/$env:RELEASE_VERSION"' in workflow
    assert 'git cat-file -t "refs/tags/$env:RELEASE_VERSION"' in workflow
    assert workflow.index("git fetch --force --no-tags origin") < workflow.index(
        'git cat-file -t "refs/tags/$env:RELEASE_VERSION"'
    )
    assert "must be an annotated tag" in workflow
    assert "--verify-tag" in workflow
    assert "--target $env:GITHUB_SHA" not in workflow
    assert "사내 업로드 사용성 및 안정성 개선" in workflow


def test_csv_header_is_utf8_sig(app_client):
    _, config, _ = app_client
    with config.log_path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))
    assert header[0] == "upload_id"

    with config.network_check_log_path.open("r", encoding="utf-8-sig", newline="") as handle:
        network_header = next(csv.reader(handle))
    assert network_header == NETWORK_CHECK_FIELDS


def test_app_startup_recovers_incomplete_upload_log_tail_after_backup(tmp_path):
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    config.log_path.parent.mkdir(parents=True)
    with config.log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=app_module.CSV_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "upload_id": "complete",
                "uploaded_at": "2026-07-16 12:00:00 +0900",
                "original_filename": "complete.txt",
                "stored_filename": "complete.txt",
                "storage_subdir": "",
                "storage_path": str(config.storage_root / "complete.txt"),
                "memo": "complete",
                "download_url": "http://files.local:8000/download/complete",
            }
        )
    with config.log_path.open("ab") as handle:
        handle.write(b'incomplete,"open')
    damaged_bytes = config.log_path.read_bytes()

    create_app(config_path)

    assert [row["upload_id"] for row in read_upload_log(config)] == ["complete"]
    backups = list(config.log_path.parent.glob("upload_log.csv.recovery-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == damaged_bytes


def test_app_startup_prunes_old_csv_recovery_backups_without_touching_logs(tmp_path):
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    app_module.ensure_directories(config)
    for index in range(7):
        backup = config.log_path.parent / (
            f"upload_log.csv.recovery-2026070{index + 1}-000000-0000000{index}.bak"
        )
        backup.write_bytes(str(index).encode("ascii"))
    original_log = config.log_path.read_bytes()

    create_app(config_path)

    assert len(list(config.log_path.parent.glob("upload_log.csv.recovery-*.bak"))) == 5
    assert config.log_path.read_bytes() == original_log


def test_app_startup_refuses_upload_log_with_invalid_header(tmp_path):
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    config.log_path.parent.mkdir(parents=True)
    config.log_path.write_text("wrong,header\n", encoding="utf-8-sig")

    with pytest.raises(CsvIntegrityError, match="invalid_header"):
        create_app(config_path)


def test_smoke_check_reports_corrupt_upload_transaction_without_traceback(
    tmp_path,
    capsys,
):
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    transaction_root = config.log_path.parent / "upload_transactions"
    transaction_root.mkdir(parents=True)
    (transaction_root / "broken.json").write_text("{not-json", encoding="utf-8")

    assert run_smoke_check(config_path) == 1

    error_output = capsys.readouterr().err
    assert "업로드 복구 기록이 손상" in error_output
    assert "Traceback" not in error_output
    assert "{not-json" not in error_output
    assert "broken.json" not in error_output


@pytest.mark.parametrize(
    ("path_attribute", "fieldnames"),
    [
        ("log_path", app_module.CSV_FIELDS),
        ("network_check_log_path", NETWORK_CHECK_FIELDS),
        ("network_check_session_log_path", SUSTAINED_LOG_FIELDS),
        ("network_probe_log_path", PROBE_LOG_FIELDS),
    ],
)
def test_app_startup_checks_and_recovers_each_operational_csv(
    tmp_path,
    path_attribute,
    fieldnames,
):
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    app_module.ensure_directories(config)
    log_path = getattr(config, path_attribute)
    with log_path.open("ab") as handle:
        handle.write(b'incomplete,"open')

    create_app(config_path)

    with log_path.open("r", encoding="utf-8-sig", newline="") as handle:
        assert next(csv.reader(handle)) == fieldnames
        assert list(csv.reader(handle)) == []
    assert len(list(log_path.parent.glob(f"{log_path.name}.recovery-*.bak"))) == 1


def test_smoke_check_returns_success(tmp_path):
    config_path = write_config(tmp_path)
    assert run_smoke_check(config_path) == 0


def test_smoke_check_refuses_data_directory_used_by_running_server(tmp_path):
    config_path = write_config(tmp_path)
    instance_lock = DataDirectoryLock(
        tmp_path / "data" / ".internal-upload.instance.lock"
    )
    instance_lock.acquire()
    try:
        assert run_smoke_check(config_path) == 1
    finally:
        instance_lock.release()


def test_health_endpoint_identifies_app_and_active_port(app_client):
    client, _, _ = app_client

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json
    assert payload["app"] == "internal-upload"
    assert payload["status"] == "degraded"
    assert payload["port"] == 8000
    assert payload["version"] == APP_VERSION
    assert payload["probe_protocol_version"] == PROBE_PROTOCOL_VERSION
    assert payload["checks"]["storage"]["status"] == "ok"
    assert payload["checks"]["csv"]["files"] == {
        "upload_log": "ok",
        "network_check_log": "ok",
        "network_check_session_log": "ok",
        "network_probe_log": "ok",
    }
    assert payload["checks"]["tcp_probe"]["enabled"] is True
    assert payload["checks"]["tcp_probe"]["available"] is False
    assert payload["checks"]["measurement"]["active"] is False
    assert payload["checks"]["file_uploads"] == {
        "status": "ok",
        "active_uploads": 0,
        "max_concurrent_uploads": 4,
        "reserved_remaining_bytes": 0,
        "at_capacity": False,
    }
    assert payload["checks"]["background_tasks"] == {
        "status": "ok",
        "failure_count": 0,
        "components": {
            "http_quick": 0,
            "http_sustained": 0,
            "tcp_probe": 0,
        },
    }
    assert len(response.data) <= 4097
    assert response.headers["Cache-Control"] == "no-store"


def test_operations_summary_prioritizes_recent_issues_without_sensitive_fields(
    app_client,
):
    client, config, _ = app_client
    rows = (
        (
            config.network_check_log_path,
            NETWORK_CHECK_FIELDS,
            {
                "checked_at": "2026-07-30 10:00:00 +0900",
                "client_ip": "10.0.0.10",
                "direction": "upload",
                "size_mb": "10",
                "bytes_transferred": "10485760",
                "duration_seconds": "1.000",
                "mbps": "83.89",
                "status": "success",
            },
        ),
        (
            config.network_check_session_log_path,
            SUSTAINED_LOG_FIELDS,
            {
                "checked_at": "2026-07-30 10:01:00 +0900",
                "client_ip": "10.0.0.11",
                "direction": "download",
                "status": "failure",
                "error": "인증 token TOP-SECRET 값이 올바르지 않습니다.",
            },
        ),
        (
            config.network_probe_log_path,
            PROBE_LOG_FIELDS,
            {
                "checked_at": "2026-07-30 10:02:00 +0900",
                "agent_hostname": "PRIVATE-PC",
                "client_ip": "10.0.0.12",
                "requested_direction": "full",
                "status": "cancelled",
                "error": "사용자가 측정을 취소했습니다.",
            },
        ),
    )
    for path, fieldnames, values in rows:
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writerow({name: values.get(name, "") for name in fieldnames})

    response = client.get("/api/operations-summary")

    assert response.status_code == 200
    payload = response.json
    assert payload["sample_size"] == 3
    assert payload["counts"] == {
        "normal": 1,
        "warning": 1,
        "problem": 1,
    }
    assert [item["status_label"] for item in payload["recent_issues"]] == [
        "취소",
        "실패",
    ]
    assert payload["recent_issues"][1]["failure_category"] == "인증"
    assert payload["status_changes"] == []
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "TOP-SECRET" not in serialized
    assert "PRIVATE-PC" not in serialized
    assert "10.0.0." not in serialized
    assert response.headers["Cache-Control"] == "no-store"


def test_operations_summary_reports_only_actual_status_transitions(
    app_client,
):
    client, config, _ = app_client
    statuses = [
        ("2026-07-30 10:00:00 +0900", "success"),
        ("2026-07-30 10:01:00 +0900", "success"),
        ("2026-07-30 10:02:00 +0900", "failure"),
        ("2026-07-30 10:03:00 +0900", "success"),
    ]
    with config.network_check_log_path.open(
        "a",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=NETWORK_CHECK_FIELDS,
        )
        for checked_at, status in statuses:
            writer.writerow(
                {
                    "checked_at": checked_at,
                    "client_ip": "10.0.0.10",
                    "direction": "upload",
                    "size_mb": "10",
                    "bytes_transferred": "10485760",
                    "duration_seconds": "1.000",
                    "mbps": "83.89",
                    "status": status,
                }
            )

    payload = client.get("/api/operations-summary").json

    assert payload["status_changes"] == [
        {
            "source": "http_quick",
            "source_label": "HTTP 데이터량",
            "direction": "upload",
            "timestamp": "2026-07-30 10:03:00 +0900",
            "from_status_label": "실패",
            "to_status_label": "완료",
        },
        {
            "source": "http_quick",
            "source_label": "HTTP 데이터량",
            "direction": "upload",
            "timestamp": "2026-07-30 10:02:00 +0900",
            "from_status_label": "완료",
            "to_status_label": "실패",
        },
    ]


def test_operations_summary_keeps_other_sources_available_when_one_is_corrupt(
    app_client,
):
    client, config, _ = app_client
    config.network_probe_log_path.write_text(
        "wrong,header\nvalue,row\n",
        encoding="utf-8",
    )

    response = client.get("/api/operations-summary")

    assert response.status_code == 200
    assert response.json["unavailable_sources"] == ["tcp_probe"]
    assert response.json["sample_size"] == 0


def test_operations_summary_source_failure_log_does_not_expose_raw_exception(
    tmp_path,
    monkeypatch,
    caplog,
):
    logger = logging.getLogger("test.operations-summary.safe-log")
    caplog.set_level(logging.INFO, logger=logger.name)
    flask_app = create_app(
        write_config(tmp_path),
        diagnostic_logger=logger,
    )

    def fail_read(*_args, **_kwargs):
        raise PermissionError(r"C:\private-customer\measurement.csv")

    monkeypatch.setattr(app_module, "read_recent_csv_rows", fail_read)

    response = flask_app.test_client().get("/api/operations-summary")

    assert response.status_code == 200
    assert response.json["unavailable_sources"] == [
        "http_quick",
        "http_sustained",
        "tcp_probe",
    ]
    assert "operations_summary_source_unavailable" in caplog.text
    assert "error_type=PermissionError" in caplog.text
    assert "private-customer" not in caplog.text
    assert "Traceback" not in caplog.text


def test_health_endpoint_reports_ok_when_probe_is_disabled_and_storage_is_healthy(tmp_path):
    config_path = write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("\n[network_probe]\nENABLED=false\nPORT=5201\n")
    flask_app = create_app(config_path)

    response = flask_app.test_client().get("/api/health")

    assert response.status_code == 200
    assert response.json["status"] == "ok"
    assert response.json["checks"]["tcp_probe"]["status"] == "ok"
    assert response.json["checks"]["tcp_probe"]["enabled"] is False


def test_health_endpoint_detects_csv_corruption_after_startup(app_client):
    client, config, _ = app_client
    with config.network_check_log_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("incomplete\n")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json["status"] == "degraded"
    assert response.json["checks"]["csv"]["status"] == "degraded"
    assert response.json["checks"]["csv"]["files"]["network_check_log"] == "wrong_column_count"


def test_health_endpoint_stays_available_when_probe_status_check_fails(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    flask_app = create_app(config_path)
    service = flask_app.extensions["network_probe"]
    monkeypatch.setattr(
        service,
        "status_payload",
        lambda: (_ for _ in ()).throw(RuntimeError("probe state failed")),
    )

    response = flask_app.test_client().get("/api/health")

    assert response.status_code == 200
    assert response.json["status"] == "degraded"
    assert response.json["checks"]["tcp_probe"]["available"] is False
    assert "확인할 수 없습니다" in response.json["checks"]["tcp_probe"]["error"]


def test_health_endpoint_warns_about_long_running_measurement(tmp_path):
    config_path = write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("\n[network_probe]\nENABLED=false\nPORT=5201\n")
    now = [0.0]
    gate = NetworkMeasurementGate(clock=lambda: now[0])
    flask_app = create_app(config_path, measurement_gate=gate)
    assert gate.acquire("http_quick", "active") is True
    now[0] = 1441.0

    response = flask_app.test_client().get("/api/health")

    measurement = response.json["checks"]["measurement"]
    assert response.json["status"] == "degraded"
    assert measurement["status"] == "warning"
    assert measurement["active"] is True
    assert measurement["long_running"] is True
    assert "owner_id" not in measurement


def test_health_endpoint_reports_cancel_callback_failure_as_degraded(tmp_path):
    config_path = write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("\n[network_probe]\nENABLED=false\nPORT=5201\n")
    now = [0.0]
    gate = NetworkMeasurementGate(
        clock=lambda: now[0],
        max_hold_seconds={"test": 1.0},
    )
    flask_app = create_app(config_path, measurement_gate=gate)

    def fail_cancel():
        gate.release("test", "active")
        raise OSError("result persistence failed")

    assert gate.acquire("test", "active", cancel_callback=fail_cancel) is True
    now[0] = 1.1

    response = flask_app.test_client().get("/api/health")

    measurement = response.json["checks"]["measurement"]
    assert response.status_code == 200
    assert response.json["status"] == "degraded"
    assert measurement["status"] == "degraded"
    assert measurement["active"] is False
    assert measurement["cancel_callback_failure_count"] == 1
    summary = flask_app.test_client().get("/api/operations-summary")
    assert summary.json["current"]["measurement_cancel_callback_failures"] == 1


def test_quick_upload_result_write_failure_returns_500_and_degrades_health(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    logger = logging.getLogger("test.quick.primary-write")
    caplog.set_level(logging.INFO, logger=logger.name)
    flask_app = create_app(
        write_config(tmp_path),
        diagnostic_logger=logger,
    )
    client = flask_app.test_client()

    def fail_write(*_args, **_kwargs):
        raise OSError("sensitive customer path")

    monkeypatch.setattr(app_module, "append_network_check_log", fail_write)

    started = client.post("/network-check/upload/start?size_mb=10")
    session_id = started.json["session_id"]
    chunk = client.post(
        f"/network-check/upload/chunk/{session_id}",
        data=b"x" * 10,
        content_type="application/octet-stream",
    )
    finished = client.post(
        f"/network-check/upload/finish/{session_id}"
    )

    assert started.status_code == 200
    assert chunk.status_code == 200
    assert finished.status_code == 500
    assert finished.json["status"] == "failure"
    assert "RESULT_WRITE_FAILED" in finished.json["error"]
    assert flask_app.extensions["network_measurement_gate"].is_available()
    health = client.get("/api/health").json
    background = health["checks"]["background_tasks"]
    assert background["status"] == "degraded"
    assert background["components"]["http_quick"] == 1
    assert "http_quick_result_persistence_failed error_type=OSError" in caplog.text
    assert "sensitive customer path" not in caplog.text


def test_quick_upload_expiry_failure_records_safe_background_event(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    clock = FakeMonotonicClock()
    logger = logging.getLogger("test.quick.expiry")
    caplog.set_level(logging.INFO, logger=logger.name)
    flask_app = create_app(
        write_config(tmp_path),
        diagnostic_logger=logger,
        network_check_clock=clock,
    )
    client = flask_app.test_client()
    started = client.post("/network-check/upload/start?size_mb=10")
    session_id = started.json["session_id"]

    def fail_payload(*_args, **_kwargs):
        raise RuntimeError("sensitive customer expiry detail")

    monkeypatch.setattr(
        app_module,
        "build_network_check_response_payload",
        fail_payload,
    )
    clock.advance(app_module.NETWORK_CHECK_UPLOAD_SESSION_TTL_SECONDS)

    flask_app.extensions["network_check_upload_expire"](session_id)

    assert flask_app.extensions["network_measurement_gate"].is_available()
    health = client.get("/api/health").json
    assert (
        health["checks"]["background_tasks"]["components"]["http_quick"]
        == 1
    )
    assert (
        "network_check_upload_expiry_record_failed error_type=RuntimeError"
        in caplog.text
    )
    assert "sensitive customer expiry detail" not in caplog.text


def test_quick_upload_archive_failure_keeps_result_and_degrades_health(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(app_module, "MEGABYTE", 1)
    logger = logging.getLogger("test.quick.archive")
    caplog.set_level(logging.INFO, logger=logger.name)
    flask_app = create_app(
        write_config(tmp_path),
        diagnostic_logger=logger,
    )
    client = flask_app.test_client()
    config = load_config(write_config(tmp_path))

    def fail_archive(*_args, **_kwargs):
        raise OSError("sensitive archive path")

    monkeypatch.setattr(app_module, "archive_csv_history", fail_archive)

    started = client.post("/network-check/upload/start?size_mb=10")
    session_id = started.json["session_id"]
    chunk = client.post(
        f"/network-check/upload/chunk/{session_id}",
        data=b"x" * 10,
        content_type="application/octet-stream",
    )
    finished = client.post(
        f"/network-check/upload/finish/{session_id}"
    )

    assert chunk.status_code == 200
    assert finished.status_code == 200
    assert finished.json["status"] == "success"
    assert read_network_check_log(config)[0]["status"] == "success"
    health = client.get("/api/health").json
    assert health["status"] == "degraded"
    assert (
        health["checks"]["background_tasks"]["components"]["http_quick"]
        == 1
    )
    assert "http_quick_csv_archive_failed error_type=OSError" in caplog.text
    assert "sensitive archive path" not in caplog.text


def test_health_endpoint_caches_disk_and_csv_checks_for_five_seconds(
    tmp_path,
    monkeypatch,
):
    config_path = write_config(tmp_path)
    now = [0.0]
    cache = TimedSnapshotCache(ttl_seconds=5.0, clock=lambda: now[0])
    storage_calls = []
    original_storage_check = app_module.check_storage_health

    def count_storage_checks(path):
        storage_calls.append(path)
        return original_storage_check(path)

    monkeypatch.setattr(app_module, "check_storage_health", count_storage_checks)
    flask_app = create_app(config_path, health_check_cache=cache)
    client = flask_app.test_client()

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert len(storage_calls) == 2

    now[0] = 5.0
    assert client.get("/api/health").status_code == 200
    assert len(storage_calls) == 4


def test_app_writes_bounded_diagnostic_log_on_startup(tmp_path):
    config_path = write_config(tmp_path)
    flask_app = create_app(config_path)
    logger = flask_app.extensions["diagnostic_logger"]
    for handler in logger.handlers:
        handler.flush()

    log_path = tmp_path / "data" / "diagnostics" / "internal-upload.log"
    assert log_path.exists()
    assert "application_initialized" in log_path.read_text(encoding="utf-8")


def test_release_zip_verifier_accepts_expected_structure(tmp_path):
    zip_path = tmp_path / "internal-upload_v0.1.0_windows.zip"
    package_root = tmp_path / "package"
    package_root.mkdir()
    csv_header = (
        "upload_id,uploaded_at,original_filename,stored_filename,storage_subdir,"
        "storage_path,memo,download_url\n"
    )
    network_csv_header = ",".join(NETWORK_CHECK_FIELDS) + "\n"
    session_csv_header = ",".join(SUSTAINED_LOG_FIELDS) + "\n"
    probe_csv_header = ",".join(PROBE_LOG_FIELDS) + "\n"
    generated = {"SECURITY_REVIEW_KO.md", "security_manifest.json", "sbom.cdx.json", "SHA256SUMS.txt"}
    special = {
        "README_START_HERE_KO.txt",
        "start_internal_upload.cmd",
        "config.ini",
        "data/upload_log.csv",
        "data/network_check_log.csv",
        "data/network_check_session_log.csv",
        "data/network_probe_log.csv",
    }
    for name in sorted(REQUIRED_FILES - generated - special):
        path = package_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sample", encoding="utf-8")
    (package_root / "InternalUploadServer.exe").write_bytes(b"MZ-server")
    (package_root / "client-template/NetworkProbeClient.exe").write_bytes(b"MZ-client")
    (package_root / "_internal").mkdir()
    (package_root / "_internal/runtime.dll").write_bytes(b"server-runtime")
    (package_root / "client-template/_internal").mkdir()
    (package_root / "client-template/_internal/runtime.dll").write_bytes(b"client-runtime")
    (package_root / "README_START_HERE_KO.txt").write_text(
        "사내 업로드 v0.1.0 Windows 실행 ZIP", encoding="utf-8"
    )
    (package_root / "start_internal_upload.cmd").write_text(
        "@echo off\nchcp 65001 >nul\n실제 접속 주소를 표시하고 "
        "config.ini에 저장합니다.\nInternalUploadServer.exe",
        encoding="utf-8",
    )
    (package_root / "config.ini").write_text(
        "[app]\nCONFIG_VERSION=2\n\n[network_probe]\nENABLED=true\nPORT=5201\n",
        encoding="utf-8",
    )
    for name, content in (
        ("data/upload_log.csv", csv_header),
        ("data/network_check_log.csv", network_csv_header),
        ("data/network_check_session_log.csv", session_csv_header),
        ("data/network_probe_log.csv", probe_csv_header),
    ):
        path = package_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    generate_security_artifacts(package_root, version="v0.1.0", source_commit="a" * 40)
    with ZipFile(zip_path, "w") as archive:
        for path in package_root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(package_root).as_posix())

    assert verify_zip(str(zip_path), "v0.1.0") == []

    tampered_path = tmp_path / "tampered.zip"
    with ZipFile(zip_path) as original, ZipFile(tampered_path, "w") as tampered:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename == "_internal/runtime.dll":
                content = b"tampered-runtime"
            tampered.writestr(item, content)

    errors = verify_zip(str(tampered_path), "v0.1.0")
    assert any("security manifest hash mismatch: _internal/runtime.dll" in error for error in errors)
    assert any("SHA256SUMS.txt hash mismatch: _internal/runtime.dll" in error for error in errors)

    bom_launcher_path = tmp_path / "bom-launcher.zip"
    with ZipFile(zip_path) as original, ZipFile(bom_launcher_path, "w") as tampered:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename == "start_internal_upload.cmd":
                content = b"\xef\xbb\xbf" + content
            tampered.writestr(item, content)

    errors = verify_zip(str(bom_launcher_path), "v0.1.0")
    assert any("must not start with a UTF-8 BOM" in error for error in errors)


def test_release_zip_verifier_rejects_unsafe_and_duplicate_windows_paths(tmp_path):
    zip_path = tmp_path / "unsafe.zip"
    with ZipFile(zip_path, "w") as archive:
        archive.writestr("../outside.txt", "bad")
        archive.writestr("Folder/File.txt", "one")
        archive.writestr("folder/file.TXT", "two")

    errors = verify_zip(str(zip_path))
    assert any("unsafe path" in error for error in errors)
    assert any("duplicate Windows path" in error for error in errors)


def test_release_zip_verifier_rejects_dev_artifacts(tmp_path):
    zip_path = tmp_path / "bad.zip"
    with ZipFile(zip_path, "w") as archive:
        for name in REQUIRED_FILES:
            archive.writestr(name, "v0.1.0")
        archive.writestr(".venv/Lib/site-packages/example.txt", "bad")

    errors = verify_zip(str(zip_path), "v0.1.0")
    assert any(".venv" in error for error in errors)


def test_release_zip_verifier_rejects_operational_network_result(tmp_path):
    zip_path = tmp_path / "bad-result.zip"
    with ZipFile(zip_path, "w") as archive:
        for name in REQUIRED_FILES:
            archive.writestr(name, "v0.3.0")
        archive.writestr("data/network_check_results/private-session.json", "{}")

    errors = verify_zip(str(zip_path), "v0.3.0")
    assert any("operational result" in error for error in errors)


def test_release_zip_verifier_rejects_operational_probe_result(tmp_path):
    zip_path = tmp_path / "bad-probe-result.zip"
    with ZipFile(zip_path, "w") as archive:
        for name in REQUIRED_FILES:
            archive.writestr(name, "v0.4.0-rc.1")
        archive.writestr("data/network_probe_results/private-session.json", "{}")

    errors = verify_zip(str(zip_path), "v0.4.0-rc.1")
    assert any("operational probe result" in error for error in errors)
