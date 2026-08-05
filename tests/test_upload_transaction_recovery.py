from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

import app as app_module
from upload_transactions import (
    UploadTransactionError,
    begin_upload_transaction,
    load_upload_transactions,
    transaction_root_for_log,
)


def build_config(tmp_path: Path) -> app_module.AppConfig:
    return app_module.load_config(tmp_path / "config.ini")


def build_row(
    config: app_module.AppConfig,
    *,
    upload_id: str = "20260730-120000-abcdef12",
    stored_filename: str = "report.txt",
) -> dict[str, str]:
    target_path = config.storage_root / stored_filename
    return {
        "upload_id": upload_id,
        "uploaded_at": "2026-07-30 12:00:00 +0900",
        "original_filename": "report.txt",
        "stored_filename": stored_filename,
        "storage_subdir": "",
        "storage_path": str(target_path.resolve()),
        "memo": "recovery",
        "download_url": f"http://127.0.0.1:8000/download/{upload_id}",
    }


def begin_transaction(
    config: app_module.AppConfig,
    *,
    operation: str,
    row: dict[str, str],
    phase: str = "prepared",
):
    return begin_upload_transaction(
        transaction_root_for_log(config.log_path),
        operation=operation,
        phase=phase,
        upload_id=row["upload_id"],
        target_relative_path=row["stored_filename"],
        row=row,
    )


def test_recovery_completes_upload_when_file_commit_preceded_log_append(tmp_path):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    target_path = Path(row["storage_path"])
    target_path.write_bytes(b"committed")
    begin_transaction(config, operation="upload", row=row)

    recovered = app_module.recover_upload_transactions(config)

    assert recovered == {"upload": 1, "delete": 0}
    assert app_module.find_upload(row["upload_id"], config) == row
    assert target_path.read_bytes() == b"committed"
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_recovery_removes_log_row_when_upload_file_is_missing(tmp_path):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    app_module.append_upload_log(row, config)
    begin_transaction(config, operation="upload", row=row)

    recovered = app_module.recover_upload_transactions(config)

    assert recovered == {"upload": 1, "delete": 0}
    assert app_module.find_upload(row["upload_id"], config) is None
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_recovery_completes_delete_when_log_was_removed_before_file(tmp_path):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    target_path = Path(row["storage_path"])
    target_path.write_bytes(b"delete-me")
    begin_transaction(config, operation="delete", row=row)

    recovered = app_module.recover_upload_transactions(config)

    assert recovered == {"upload": 0, "delete": 1}
    assert app_module.find_upload(row["upload_id"], config) is None
    assert not target_path.exists()
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_recovery_completes_delete_when_both_log_and_file_still_exist(tmp_path):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    target_path = Path(row["storage_path"])
    target_path.write_bytes(b"delete-me")
    app_module.append_upload_log(row, config)
    begin_transaction(config, operation="delete", row=row)

    recovered = app_module.recover_upload_transactions(config)

    assert recovered == {"upload": 0, "delete": 1}
    assert app_module.find_upload(row["upload_id"], config) is None
    assert not target_path.exists()


def test_recovery_is_idempotent_after_completed_operation(tmp_path):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    target_path = Path(row["storage_path"])
    target_path.write_bytes(b"committed")
    app_module.append_upload_log(row, config)
    begin_transaction(config, operation="upload", row=row)

    first = app_module.recover_upload_transactions(config)
    second = app_module.recover_upload_transactions(config)

    assert first == {"upload": 1, "delete": 0}
    assert second == {"upload": 0, "delete": 0}
    assert target_path.read_bytes() == b"committed"
    assert app_module.find_upload(row["upload_id"], config) == row


@pytest.mark.parametrize(
    ("operation", "phase"),
    (("upload", "log_committed"), ("delete", "file_deleted")),
)
def test_terminal_marker_cleanup_never_mutates_reused_target(
    tmp_path,
    operation,
    phase,
):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    old_row = build_row(config, upload_id="20260730-120000-oldold12")
    begin_transaction(
        config,
        operation=operation,
        row=old_row,
        phase=phase,
    )

    new_row = build_row(config, upload_id="20260730-120100-newnew12")
    target_path = Path(new_row["storage_path"])
    target_path.write_bytes(b"NEW")
    app_module.append_upload_log(new_row, config)

    recovered = app_module.recover_upload_transactions(config)

    assert recovered[operation] == 1
    assert target_path.read_bytes() == b"NEW"
    assert app_module.read_upload_log(config) == [new_row]
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_rolled_back_delete_marker_cleanup_preserves_restored_file_and_row(
    tmp_path,
):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    target_path = Path(row["storage_path"])
    target_path.write_bytes(b"RESTORED")
    app_module.append_upload_log(row, config)
    begin_transaction(
        config,
        operation="delete",
        row=row,
        phase="rolled_back",
    )

    recovered = app_module.recover_upload_transactions(config)

    assert recovered == {"upload": 0, "delete": 1}
    assert target_path.read_bytes() == b"RESTORED"
    assert app_module.find_upload(row["upload_id"], config) == row
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_terminal_marker_cleanup_does_not_depend_on_current_storage_root(
    tmp_path,
):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    begin_transaction(
        config,
        operation="upload",
        row=row,
        phase="log_committed",
    )
    moved_config = replace(
        config,
        storage_root=(tmp_path / "moved-uploads").resolve(),
    )

    recovered = app_module.recover_upload_transactions(moved_config)

    assert recovered == {"upload": 1, "delete": 0}
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_nonterminal_marker_fails_closed_when_target_is_reused(tmp_path):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    old_row = build_row(config, upload_id="20260730-120000-oldold12")
    begin_transaction(
        config,
        operation="delete",
        row=old_row,
        phase="log_removed",
    )
    new_row = build_row(config, upload_id="20260730-120100-newnew12")
    target_path = Path(new_row["storage_path"])
    target_path.write_bytes(b"NEW")
    app_module.append_upload_log(new_row, config)

    with pytest.raises(UploadTransactionError, match="재사용"):
        app_module.recover_upload_transactions(config)

    assert target_path.read_bytes() == b"NEW"
    assert app_module.read_upload_log(config) == [new_row]


def test_recovery_fails_closed_when_marker_target_disagrees_with_csv_row(tmp_path):
    config = build_config(tmp_path)
    app_module.ensure_directories(config)
    row = build_row(config)
    begin_upload_transaction(
        transaction_root_for_log(config.log_path),
        operation="upload",
        phase="prepared",
        upload_id=row["upload_id"],
        target_relative_path="different.txt",
        row=row,
    )

    with pytest.raises(UploadTransactionError, match="경로"):
        app_module.recover_upload_transactions(config)


def test_normal_upload_finishes_without_leaving_transaction_marker(tmp_path):
    config = build_config(tmp_path)
    flask_app = app_module.create_app(app_config=config)
    client = flask_app.test_client()

    response = client.post(
        "/upload",
        data={"file": (BytesIO(b"normal-upload"), "normal.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_normal_delete_finishes_without_leaving_transaction_marker(tmp_path):
    config = build_config(tmp_path)
    flask_app = app_module.create_app(app_config=config)
    client = flask_app.test_client()
    upload_response = client.post(
        "/upload",
        data={"file": (BytesIO(b"normal-delete"), "normal.txt")},
        content_type="multipart/form-data",
    )
    assert upload_response.status_code == 200
    row = app_module.read_upload_log(config)[0]

    response = client.post(
        f"/delete/{row['upload_id']}",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 302
    assert app_module.find_upload(row["upload_id"], config) is None
    assert not Path(row["storage_path"]).exists()
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []


def test_delete_refuses_directory_target_without_removing_log_row(tmp_path):
    config = build_config(tmp_path)
    flask_app = app_module.create_app(app_config=config)
    client = flask_app.test_client()
    upload_id = "20260730-120000-directory"
    target_path = config.storage_root / "directory-target"
    target_path.mkdir()
    row = build_row(
        config,
        upload_id=upload_id,
        stored_filename=target_path.name,
    )
    app_module.append_upload_log(row, config)

    response = client.post(
        f"/delete/{upload_id}",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    assert target_path.is_dir()
    assert app_module.find_upload(upload_id, config) == row
    assert load_upload_transactions(transaction_root_for_log(config.log_path)) == []
