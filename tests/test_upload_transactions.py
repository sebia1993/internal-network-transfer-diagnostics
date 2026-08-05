from __future__ import annotations

import json

import pytest

from upload_transactions import (
    UploadTransactionError,
    advance_upload_transaction,
    begin_upload_transaction,
    finish_upload_transaction,
    load_upload_transactions,
)


def sample_row(upload_id: str = "20260730-120000-abcdef12") -> dict[str, str]:
    return {
        "upload_id": upload_id,
        "uploaded_at": "2026-07-30 12:00:00 +0900",
        "original_filename": "보고서.txt",
        "stored_filename": "보고서.txt",
        "storage_subdir": "점검",
        "storage_path": "D:\\uploads\\점검\\보고서.txt",
        "memo": "트랜잭션 테스트",
        "download_url": "http://127.0.0.1:8000/download/test",
    }


def test_transaction_round_trip_and_phase_advance(tmp_path):
    root = tmp_path / "data" / "upload_transactions"
    transaction = begin_upload_transaction(
        root,
        operation="upload",
        phase="prepared",
        upload_id="20260730-120000-abcdef12",
        target_relative_path="점검/보고서.txt",
        row=sample_row(),
    )

    loaded = load_upload_transactions(root)

    assert len(loaded) == 1
    assert loaded[0].operation == "upload"
    assert loaded[0].phase == "prepared"
    assert loaded[0].target_relative_path == "점검/보고서.txt"
    assert loaded[0].row["memo"] == "트랜잭션 테스트"

    advanced = advance_upload_transaction(transaction, "file_committed")
    assert load_upload_transactions(root)[0].phase == "file_committed"

    finish_upload_transaction(advanced)
    assert load_upload_transactions(root) == []


@pytest.mark.parametrize(
    "relative_path",
    ("", "../outside.txt", "/absolute.txt", "C:/outside.txt", "folder/../file.txt"),
)
def test_transaction_rejects_paths_outside_storage_root(tmp_path, relative_path):
    with pytest.raises(UploadTransactionError, match="상대 경로|비어"):
        begin_upload_transaction(
            tmp_path,
            operation="upload",
            phase="prepared",
            upload_id="20260730-120000-abcdef12",
            target_relative_path=relative_path,
            row=sample_row(),
        )


def test_transaction_rejects_invalid_phase_and_row_id(tmp_path):
    with pytest.raises(UploadTransactionError, match="단계"):
        begin_upload_transaction(
            tmp_path,
            operation="upload",
            phase="unknown",
            upload_id="20260730-120000-abcdef12",
            target_relative_path="보고서.txt",
            row=sample_row(),
        )

    with pytest.raises(UploadTransactionError, match="ID"):
        begin_upload_transaction(
            tmp_path,
            operation="upload",
            phase="prepared",
            upload_id="different",
            target_relative_path="보고서.txt",
            row=sample_row(),
        )


def test_transaction_load_fails_closed_for_corrupt_marker(tmp_path):
    root = tmp_path / "upload_transactions"
    root.mkdir()
    (root / "broken.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(UploadTransactionError, match="읽을 수 없습니다"):
        load_upload_transactions(root)


def test_transaction_load_removes_only_abandoned_transaction_temps(tmp_path):
    root = tmp_path / "upload_transactions"
    root.mkdir()
    abandoned = root / ".marker.json.abc.tmp"
    unrelated = root / "keep.tmp"
    abandoned.write_text("partial", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    assert load_upload_transactions(root) == []
    assert not abandoned.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_transaction_marker_filename_must_match_payload(tmp_path):
    root = tmp_path / "upload_transactions"
    root.mkdir()
    payload = {
        "version": 1,
        "operation": "delete",
        "phase": "prepared",
        "upload_id": "20260730-120000-abcdef12",
        "target_relative_path": "보고서.txt",
        "row": sample_row(),
    }
    (root / "wrong.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(UploadTransactionError, match="파일명"):
        load_upload_transactions(root)
