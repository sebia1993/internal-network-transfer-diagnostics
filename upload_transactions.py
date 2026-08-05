from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping

from runtime_stability import durable_replace


TRANSACTION_VERSION = 1
TRANSACTION_DIRECTORY_NAME = "upload_transactions"
TRANSACTION_PHASES = {
    "upload": frozenset(
        {"prepared", "file_committed", "log_committed", "rolled_back"}
    ),
    "delete": frozenset(
        {"prepared", "log_removed", "file_deleted", "rolled_back"}
    ),
}


class UploadTransactionError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadTransaction:
    marker_path: Path
    operation: str
    phase: str
    upload_id: str
    target_relative_path: str
    row: dict[str, str]


def transaction_root_for_log(log_path: Path) -> Path:
    return log_path.parent / TRANSACTION_DIRECTORY_NAME


def begin_upload_transaction(
    root: Path,
    *,
    operation: str,
    phase: str,
    upload_id: str,
    target_relative_path: str,
    row: Mapping[str, str],
) -> UploadTransaction:
    transaction = UploadTransaction(
        marker_path=root / _marker_name(operation, upload_id),
        operation=operation,
        phase=phase,
        upload_id=upload_id,
        target_relative_path=_validated_relative_path(target_relative_path),
        row=_validated_row(row, upload_id),
    )
    _validate_operation_phase(operation, phase)
    _persist_transaction(transaction)
    return transaction


def advance_upload_transaction(
    transaction: UploadTransaction,
    phase: str,
) -> UploadTransaction:
    _validate_operation_phase(transaction.operation, phase)
    advanced = replace(transaction, phase=phase)
    _persist_transaction(advanced)
    return advanced


def finish_upload_transaction(transaction: UploadTransaction) -> bool:
    try:
        transaction.marker_path.unlink(missing_ok=True)
        return True
    except OSError:
        # A committed marker is intentionally safe to replay. Failing the
        # completed user operation here would create a worse split-brain state.
        return False


def load_upload_transactions(root: Path) -> list[UploadTransaction]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise UploadTransactionError(
            f"업로드 트랜잭션 경로가 폴더가 아닙니다: {root}"
        )
    _cleanup_abandoned_temporary_files(root)
    transactions = [_load_transaction(path) for path in sorted(root.glob("*.json"))]
    return transactions


def _persist_transaction(transaction: UploadTransaction) -> None:
    transaction.marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = transaction.marker_path.with_name(
        f".{transaction.marker_path.name}.{uuid.uuid4().hex}.tmp"
    )
    payload = {
        "version": TRANSACTION_VERSION,
        "operation": transaction.operation,
        "phase": transaction.phase,
        "upload_id": transaction.upload_id,
        "target_relative_path": transaction.target_relative_path,
        "row": transaction.row,
    }
    try:
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary_path, transaction.marker_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_transaction(path: Path) -> UploadTransaction:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UploadTransactionError(
            f"업로드 트랜잭션 파일을 읽을 수 없습니다: {path.name}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != TRANSACTION_VERSION:
        raise UploadTransactionError(
            f"지원하지 않는 업로드 트랜잭션 파일입니다: {path.name}"
        )

    operation = payload.get("operation")
    phase = payload.get("phase")
    upload_id = payload.get("upload_id")
    target_relative_path = payload.get("target_relative_path")
    row = payload.get("row")
    if not all(
        isinstance(value, str)
        for value in (operation, phase, upload_id, target_relative_path)
    ):
        raise UploadTransactionError(
            f"업로드 트랜잭션 필수 값이 올바르지 않습니다: {path.name}"
        )
    _validate_operation_phase(operation, phase)
    expected_name = _marker_name(operation, upload_id)
    if path.name != expected_name:
        raise UploadTransactionError(
            f"업로드 트랜잭션 파일명이 내용과 일치하지 않습니다: {path.name}"
        )

    return UploadTransaction(
        marker_path=path,
        operation=operation,
        phase=phase,
        upload_id=upload_id,
        target_relative_path=_validated_relative_path(target_relative_path),
        row=_validated_row(row, upload_id),
    )


def _validate_operation_phase(operation: str, phase: str) -> None:
    allowed_phases = TRANSACTION_PHASES.get(operation)
    if allowed_phases is None or phase not in allowed_phases:
        raise UploadTransactionError(
            f"업로드 트랜잭션 단계가 올바르지 않습니다: {operation}/{phase}"
        )


def _validated_relative_path(value: str) -> str:
    if not value:
        raise UploadTransactionError("업로드 트랜잭션 대상 경로가 비어 있습니다.")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise UploadTransactionError(
            "업로드 트랜잭션 대상 경로는 저장소 내부 상대 경로여야 합니다."
        )
    return posix_path.as_posix()


def _validated_row(value, upload_id: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise UploadTransactionError("업로드 트랜잭션 행 데이터가 올바르지 않습니다.")
    row: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise UploadTransactionError(
                "업로드 트랜잭션 행 데이터는 문자열만 포함해야 합니다."
            )
        row[key] = item
    if row.get("upload_id") != upload_id:
        raise UploadTransactionError(
            "업로드 트랜잭션 ID와 행 데이터가 일치하지 않습니다."
        )
    return row


def _marker_name(operation: str, upload_id: str) -> str:
    if not operation or not upload_id:
        raise UploadTransactionError("업로드 트랜잭션 식별자가 비어 있습니다.")
    digest = hashlib.sha256(f"{operation}:{upload_id}".encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _cleanup_abandoned_temporary_files(root: Path) -> None:
    for path in root.glob(".*.json.*.tmp"):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
