from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from result_storage import write_json_atomically
from runtime_stability import durable_replace


MEASUREMENT_TRANSACTION_VERSION = 2
MEASUREMENT_TRANSACTION_DIRECTORY = "measurement_transactions"
MEASUREMENT_TRANSACTION_MAX_BYTES = 256 * 1024
MEASUREMENT_TRANSACTION_MAX_ROWS = 2
MEASUREMENT_TRANSACTION_MAX_VALUE_CHARS = 10_000
MEASUREMENT_SOURCES = frozenset({"http_sustained", "tcp_probe"})
MEASUREMENT_TRANSACTION_STATES = frozenset(
    {"prepared", "rollback_requested"}
)


class MeasurementTransactionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MeasurementTransaction:
    marker_path: Path
    source: str
    session_id: str
    result_sha256: str
    rows: tuple[dict[str, str], ...]
    state: str = "prepared"


@dataclass(frozen=True)
class MeasurementRecoverySpec:
    source: str
    log_path: Path
    results_root: Path
    fieldnames: tuple[str, ...]
    key_field: str
    build_rows_from_result: Callable[
        [dict[str, Any]], Sequence[Mapping[str, object]]
    ]


def measurement_transaction_root_for_log(log_path: Path) -> Path:
    return log_path.parent / MEASUREMENT_TRANSACTION_DIRECTORY


def commit_measurement_result(
    *,
    source: str,
    result_path: Path,
    result: dict[str, Any],
    log_path: Path,
    fieldnames: Sequence[str],
    key_field: str,
    rows: Sequence[Mapping[str, object]],
) -> bool:
    """Commit one result JSON and its CSV rows.

    Returns True when the durable intent marker was removed. A False return
    means JSON and CSV are committed but marker cleanup must be retried at the
    next startup; callers should expose a diagnostic warning and skip archive
    or pruning for this cycle.
    """

    normalized_fields = _validated_fieldnames(fieldnames, key_field)
    session_id = _validated_session_id(result.get("session_id"))
    expected_result_path = result_path.parent / f"{session_id}.json"
    if result_path != expected_result_path:
        raise MeasurementTransactionError(
            "측정 결과 파일명이 세션 ID와 일치하지 않습니다."
        )
    if result_path.exists():
        raise MeasurementTransactionError(
            "같은 세션 ID의 측정 결과 파일이 이미 존재합니다."
        )

    normalized_rows = _validated_rows(
        rows,
        fieldnames=normalized_fields,
        key_field=key_field,
        session_id=session_id,
    )
    _ensure_no_existing_session_rows(
        log_path,
        normalized_fields,
        session_id=session_id,
    )
    result_sha256 = _semantic_json_sha256(result)
    transaction = _begin_measurement_transaction(
        measurement_transaction_root_for_log(log_path),
        source=source,
        session_id=session_id,
        result_sha256=result_sha256,
        rows=normalized_rows,
    )
    try:
        _checkpoint(source, "journal_committed", 0)
        write_json_atomically(result_path, result)
        _checkpoint(source, "json_committed", 0)
        with log_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(normalized_fields))
            for index, row in enumerate(normalized_rows, start=1):
                _append_csv_row_durably(handle, writer, row)
                _checkpoint(source, "csv_row_committed", index)
        _verify_committed_rows(
            log_path,
            normalized_fields,
            key_field=key_field,
            session_id=session_id,
            expected_rows=normalized_rows,
        )
        _checkpoint(source, "csv_committed", len(normalized_rows))
    except Exception as commit_error:
        try:
            rollback_transaction = _change_measurement_transaction_state(
                transaction,
                "rollback_requested",
            )
        except Exception as state_error:
            raise MeasurementTransactionError(
                "측정 저장 실패 뒤 복구 방향을 확정하지 못했습니다. "
                "프로그램을 다시 시작해 복구를 완료하세요."
            ) from state_error
        rollback_complete = _rollback_incomplete_commit(
            log_path=log_path,
            result_path=result_path,
            fieldnames=normalized_fields,
            session_id=session_id,
            expected_rows=normalized_rows,
            result_sha256=result_sha256,
        )
        if rollback_complete:
            _finish_measurement_transaction(rollback_transaction)
        raise commit_error
    return _finish_measurement_transaction(transaction)


def recover_measurement_transactions(
    root: Path,
    specs: Sequence[MeasurementRecoverySpec],
) -> dict[str, int]:
    specs_by_source = {}
    for spec in specs:
        if spec.source in specs_by_source or spec.source not in MEASUREMENT_SOURCES:
            raise MeasurementTransactionError(
                "측정 복구 종류 설정이 올바르지 않습니다."
            )
        _validated_fieldnames(spec.fieldnames, spec.key_field)
        if not callable(spec.build_rows_from_result):
            raise MeasurementTransactionError(
                "측정 복구 행 생성 설정이 올바르지 않습니다."
            )
        specs_by_source[spec.source] = spec

    recovered = {source: 0 for source in specs_by_source}
    for transaction in load_measurement_transactions(root):
        spec = specs_by_source.get(transaction.source)
        if spec is None:
            raise MeasurementTransactionError(
                "현재 프로그램이 지원하지 않는 측정 복구 기록이 있습니다."
            )
        _recover_measurement_transaction(transaction, spec)
        recovered[transaction.source] += 1
    return recovered


def load_measurement_transactions(root: Path) -> list[MeasurementTransaction]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise MeasurementTransactionError(
            "측정 복구 기록 경로가 폴더가 아닙니다."
        )
    canonical_root = _validated_directory(root, expected_parent=root.parent)
    transactions: list[MeasurementTransaction] = []
    for source_root in sorted(root.iterdir(), key=lambda path: path.name):
        if not source_root.is_dir() or source_root.name not in MEASUREMENT_SOURCES:
            raise MeasurementTransactionError(
                "측정 복구 기록 폴더 구조가 올바르지 않습니다."
            )
        _validated_directory(source_root, expected_parent=canonical_root)
        _cleanup_abandoned_temporary_files(source_root)
        marker_paths = []
        for path in sorted(source_root.iterdir(), key=lambda item: item.name):
            if (
                path.suffix != ".json"
                or not path.is_file()
                or path.is_symlink()
                or path.resolve().parent != source_root.resolve()
            ):
                raise MeasurementTransactionError(
                    "측정 복구 기록 폴더에 예상하지 못한 항목이 있습니다."
                )
            marker_paths.append(path)
        transactions.extend(
            _load_measurement_transaction(path) for path in marker_paths
        )
    return transactions


def has_pending_measurement_transactions(root: Path, source: str) -> bool:
    if source not in MEASUREMENT_SOURCES:
        raise MeasurementTransactionError(
            "지원하지 않는 측정 복구 종류입니다."
        )
    if not root.exists():
        return False
    canonical_root = _validated_directory(root, expected_parent=root.parent)
    source_root = root / source
    if not source_root.exists():
        return False
    _validated_directory(source_root, expected_parent=canonical_root)
    _cleanup_abandoned_temporary_files(source_root)
    for path in source_root.iterdir():
        if (
            path.suffix != ".json"
            or not path.is_file()
            or path.is_symlink()
            or path.resolve().parent != source_root.resolve()
        ):
            raise MeasurementTransactionError(
                "측정 복구 기록 폴더에 예상하지 못한 항목이 있습니다."
            )
        _load_measurement_transaction(path)
        return True
    return False


def _recover_measurement_transaction(
    transaction: MeasurementTransaction,
    spec: MeasurementRecoverySpec,
) -> None:
    fieldnames = _validated_fieldnames(spec.fieldnames, spec.key_field)
    rows = _validated_rows(
        transaction.rows,
        fieldnames=fieldnames,
        key_field=spec.key_field,
        session_id=transaction.session_id,
    )
    result_path = spec.results_root / f"{transaction.session_id}.json"
    if transaction.state == "rollback_requested":
        if not _rollback_incomplete_commit(
            log_path=spec.log_path,
            result_path=result_path,
            fieldnames=fieldnames,
            session_id=transaction.session_id,
            expected_rows=rows,
            result_sha256=transaction.result_sha256,
        ):
            raise MeasurementTransactionError(
                "취소된 측정 저장을 완전히 정리하지 못했습니다."
            )
        if not _finish_measurement_transaction(transaction):
            raise MeasurementTransactionError(
                "취소된 측정 복구 기록을 정리하지 못했습니다."
            )
        return

    current_rows = _read_csv_rows(spec.log_path, fieldnames)
    session_rows = [
        row for row in current_rows if row.get("session_id") == transaction.session_id
    ]

    if not result_path.exists():
        if session_rows:
            raise MeasurementTransactionError(
                "측정 요약 행은 있지만 상세 결과 파일이 없어 자동 복구를 "
                "중단합니다."
            )
        if not _finish_measurement_transaction(transaction):
            raise MeasurementTransactionError(
                "완료되지 않은 측정 복구 기록을 정리하지 못했습니다."
            )
        return
    if not result_path.is_file():
        raise MeasurementTransactionError(
            "측정 상세 결과 경로가 일반 파일이 아닙니다."
        )

    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MeasurementTransactionError(
            "측정 상세 결과 파일을 읽거나 검증할 수 없습니다."
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("session_id") != transaction.session_id
        or _semantic_json_sha256(payload) != transaction.result_sha256
    ):
        raise MeasurementTransactionError(
            "측정 복구 기록과 상세 결과 파일이 일치하지 않습니다."
        )
    try:
        result_rows = _validated_rows(
            spec.build_rows_from_result(payload),
            fieldnames=fieldnames,
            key_field=spec.key_field,
            session_id=transaction.session_id,
        )
    except MeasurementTransactionError:
        raise
    except Exception as exc:
        raise MeasurementTransactionError(
            "측정 상세 결과에서 복구 요약 행을 만들 수 없습니다."
        ) from exc
    if result_rows != rows:
        raise MeasurementTransactionError(
            "측정 복구 기록의 요약 행이 상세 결과와 일치하지 않습니다."
        )

    expected_by_key = {row[spec.key_field]: row for row in rows}
    existing_by_key: dict[str, list[dict[str, str]]] = {}
    for row in session_rows:
        key = row.get(spec.key_field, "")
        if key not in expected_by_key:
            raise MeasurementTransactionError(
                "측정 복구 기록에 없는 요약 행이 같은 세션에 존재합니다."
            )
        existing_by_key.setdefault(key, []).append(row)

    missing_rows = []
    for key, expected in expected_by_key.items():
        existing = existing_by_key.get(key, [])
        if len(existing) > 1:
            raise MeasurementTransactionError(
                "같은 측정 요약 행이 중복되어 자동 복구를 중단합니다."
            )
        if existing and existing[0] != expected:
            raise MeasurementTransactionError(
                "측정 복구 기록과 현재 요약 행의 내용이 일치하지 않습니다."
            )
        if not existing:
            missing_rows.append(expected)

    if missing_rows:
        with spec.log_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
            for row in missing_rows:
                _append_csv_row_durably(handle, writer, row)
        _verify_committed_rows(
            spec.log_path,
            fieldnames,
            key_field=spec.key_field,
            session_id=transaction.session_id,
            expected_rows=rows,
        )
    if not _finish_measurement_transaction(transaction):
        raise MeasurementTransactionError(
            "복구가 끝난 측정 기록 표식을 정리하지 못했습니다."
        )


def _begin_measurement_transaction(
    root: Path,
    *,
    source: str,
    session_id: str,
    result_sha256: str,
    rows: tuple[dict[str, str], ...],
) -> MeasurementTransaction:
    if source not in MEASUREMENT_SOURCES:
        raise MeasurementTransactionError(
            "지원하지 않는 측정 복구 종류입니다."
        )
    session_id = _validated_session_id(session_id)
    if len(result_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in result_sha256
    ):
        raise MeasurementTransactionError(
            "측정 결과 해시가 올바르지 않습니다."
        )
    marker_path = root / source / _marker_name(source, session_id)
    if marker_path.exists():
        raise MeasurementTransactionError(
            "같은 세션 ID의 측정 복구 기록이 이미 존재합니다."
        )
    transaction = MeasurementTransaction(
        marker_path=marker_path,
        source=source,
        session_id=session_id,
        result_sha256=result_sha256,
        rows=rows,
        state="prepared",
    )
    _persist_measurement_transaction(transaction)
    return transaction


def _persist_measurement_transaction(transaction: MeasurementTransaction) -> None:
    transaction.marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = transaction.marker_path.with_name(
        f".{transaction.marker_path.name}.{uuid.uuid4().hex}.tmp"
    )
    payload = {
        "version": MEASUREMENT_TRANSACTION_VERSION,
        "source": transaction.source,
        "session_id": transaction.session_id,
        "result_sha256": transaction.result_sha256,
        "rows": list(transaction.rows),
        "state": transaction.state,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    if len(serialized.encode("utf-8")) > MEASUREMENT_TRANSACTION_MAX_BYTES:
        raise MeasurementTransactionError(
            "측정 복구 기록이 허용 크기를 초과합니다."
        )
    try:
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary_path, transaction.marker_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_measurement_transaction(path: Path) -> MeasurementTransaction:
    try:
        if path.stat().st_size > MEASUREMENT_TRANSACTION_MAX_BYTES:
            raise MeasurementTransactionError(
                "측정 복구 기록이 허용 크기를 초과합니다."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except MeasurementTransactionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MeasurementTransactionError(
            "측정 복구 기록을 읽을 수 없습니다."
        ) from exc
    if not isinstance(payload, dict):
        raise MeasurementTransactionError(
            "측정 복구 기록 형식이 올바르지 않습니다."
        )
    version = payload.get("version")
    expected_fields = {
        "version",
        "source",
        "session_id",
        "result_sha256",
        "rows",
    }
    if version == MEASUREMENT_TRANSACTION_VERSION:
        expected_fields.add("state")
    elif version != 1:
        raise MeasurementTransactionError(
            "지원하지 않는 측정 복구 기록 버전입니다."
        )
    if set(payload) != expected_fields:
        raise MeasurementTransactionError(
            "측정 복구 기록 형식이 올바르지 않습니다."
        )
    source = payload.get("source")
    session_id = _validated_session_id(payload.get("session_id"))
    result_sha256 = payload.get("result_sha256")
    rows = payload.get("rows")
    state = payload.get("state", "prepared")
    if (
        not isinstance(source, str)
        or source not in MEASUREMENT_SOURCES
        or not isinstance(result_sha256, str)
        or len(result_sha256) != 64
        or any(character not in "0123456789abcdef" for character in result_sha256)
        or not isinstance(rows, list)
        or state not in MEASUREMENT_TRANSACTION_STATES
    ):
        raise MeasurementTransactionError(
            "측정 복구 기록 필수 값이 올바르지 않습니다."
        )
    expected_path = (
        path.parent.parent / source / _marker_name(source, session_id)
    )
    if path != expected_path:
        raise MeasurementTransactionError(
            "측정 복구 기록 파일명과 내용이 일치하지 않습니다."
        )
    return MeasurementTransaction(
        marker_path=path,
        source=source,
        session_id=session_id,
        result_sha256=result_sha256,
        rows=tuple(rows),
        state=state,
    )


def _change_measurement_transaction_state(
    transaction: MeasurementTransaction,
    state: str,
) -> MeasurementTransaction:
    if state not in MEASUREMENT_TRANSACTION_STATES:
        raise MeasurementTransactionError(
            "측정 복구 기록 상태가 올바르지 않습니다."
        )
    updated = MeasurementTransaction(
        marker_path=transaction.marker_path,
        source=transaction.source,
        session_id=transaction.session_id,
        result_sha256=transaction.result_sha256,
        rows=transaction.rows,
        state=state,
    )
    _persist_measurement_transaction(updated)
    return updated


def _finish_measurement_transaction(
    transaction: MeasurementTransaction,
) -> bool:
    try:
        transaction.marker_path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _validated_fieldnames(
    fieldnames: Sequence[str],
    key_field: str,
) -> tuple[str, ...]:
    normalized = tuple(fieldnames)
    if (
        not normalized
        or len(set(normalized)) != len(normalized)
        or "session_id" not in normalized
        or key_field not in normalized
        or any(not isinstance(field, str) or not field for field in normalized)
    ):
        raise MeasurementTransactionError(
            "측정 CSV 필드 설정이 올바르지 않습니다."
        )
    return normalized


def _validated_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    fieldnames: tuple[str, ...],
    key_field: str,
    session_id: str,
) -> tuple[dict[str, str], ...]:
    if (
        isinstance(rows, (str, bytes))
        or not 1 <= len(rows) <= MEASUREMENT_TRANSACTION_MAX_ROWS
    ):
        raise MeasurementTransactionError(
            "측정 복구 요약 행 개수가 올바르지 않습니다."
        )
    normalized_rows = []
    keys = set()
    expected_fields = set(fieldnames)
    for value in rows:
        if not isinstance(value, Mapping) or set(value) != expected_fields:
            raise MeasurementTransactionError(
                "측정 복구 요약 행 필드가 CSV 헤더와 일치하지 않습니다."
            )
        row = {}
        for field in fieldnames:
            item = value[field]
            text = "" if item is None else str(item)
            if len(text) > MEASUREMENT_TRANSACTION_MAX_VALUE_CHARS:
                raise MeasurementTransactionError(
                    "측정 복구 요약 값이 허용 길이를 초과합니다."
                )
            row[field] = text
        if row["session_id"] != session_id or not row[key_field]:
            raise MeasurementTransactionError(
                "측정 복구 세션 또는 행 식별자가 일치하지 않습니다."
            )
        if row[key_field] in keys:
            raise MeasurementTransactionError(
                "측정 복구 요약 행 식별자가 중복되었습니다."
            )
        keys.add(row[key_field])
        normalized_rows.append(row)
    return tuple(normalized_rows)


def _validated_session_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MeasurementTransactionError(
            "측정 복구 세션 ID가 올바르지 않습니다."
        )
    return value


def _semantic_json_sha256(payload: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MeasurementTransactionError(
            "측정 상세 결과를 안정적으로 직렬화할 수 없습니다."
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _marker_name(source: str, session_id: str) -> str:
    digest = hashlib.sha256(
        f"{source}:{session_id}".encode("utf-8")
    ).hexdigest()
    return f"{digest}.json"


def _append_csv_row_durably(
    handle,
    writer: csv.DictWriter,
    row: Mapping[str, str],
) -> None:
    writer.writerow(row)
    handle.flush()
    os.fsync(handle.fileno())


def _read_csv_rows(
    path: Path,
    fieldnames: tuple[str, ...],
) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != fieldnames:
                raise MeasurementTransactionError(
                    "측정 CSV 헤더가 복구 기준과 일치하지 않습니다."
                )
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise MeasurementTransactionError(
                        "측정 CSV 행 구조가 복구 기준과 일치하지 않습니다."
                    )
                rows.append({field: row[field] for field in fieldnames})
            return rows
    except MeasurementTransactionError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise MeasurementTransactionError(
            "측정 CSV를 복구 기준으로 읽을 수 없습니다."
        ) from exc


def _ensure_no_existing_session_rows(
    path: Path,
    fieldnames: tuple[str, ...],
    *,
    session_id: str,
) -> None:
    if any(
        row.get("session_id") == session_id
        for row in _read_csv_rows(path, fieldnames)
    ):
        raise MeasurementTransactionError(
            "같은 세션 ID의 측정 요약 행이 이미 존재합니다."
        )


def _verify_committed_rows(
    path: Path,
    fieldnames: tuple[str, ...],
    *,
    key_field: str,
    session_id: str,
    expected_rows: tuple[dict[str, str], ...],
) -> None:
    session_rows = [
        row
        for row in _read_csv_rows(path, fieldnames)
        if row.get("session_id") == session_id
    ]
    expected_by_key = {row[key_field]: row for row in expected_rows}
    if len(session_rows) != len(expected_rows):
        raise MeasurementTransactionError(
            "확정된 측정 요약 행 개수가 예상과 다릅니다."
        )
    for row in session_rows:
        expected = expected_by_key.get(row.get(key_field, ""))
        if expected is None or row != expected:
            raise MeasurementTransactionError(
                "확정된 측정 요약 행 내용이 예상과 다릅니다."
            )


def _rollback_incomplete_commit(
    *,
    log_path: Path,
    result_path: Path,
    fieldnames: tuple[str, ...],
    session_id: str,
    expected_rows: tuple[dict[str, str], ...],
    result_sha256: str,
) -> bool:
    complete = True
    try:
        _remove_expected_session_rows(
            log_path,
            fieldnames,
            session_id=session_id,
            expected_rows=expected_rows,
        )
    except (OSError, UnicodeError, csv.Error, MeasurementTransactionError):
        complete = False
    try:
        _remove_expected_result(
            result_path,
            session_id=session_id,
            result_sha256=result_sha256,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, MeasurementTransactionError):
        complete = False
    return complete


def _remove_expected_session_rows(
    log_path: Path,
    fieldnames: tuple[str, ...],
    *,
    session_id: str,
    expected_rows: tuple[dict[str, str], ...],
) -> None:
    current_rows = _read_csv_rows(log_path, fieldnames)
    expected_by_key = {
        tuple(row[field] for field in fieldnames): row for row in expected_rows
    }
    session_rows = [
        row for row in current_rows if row.get("session_id") == session_id
    ]
    if len(session_rows) > len(expected_rows) or any(
        tuple(row[field] for field in fieldnames) not in expected_by_key
        for row in session_rows
    ):
        raise MeasurementTransactionError(
            "취소할 측정 요약 행이 복구 기록과 일치하지 않습니다."
        )
    if not session_rows:
        return
    retained_rows = [
        row for row in current_rows if row.get("session_id") != session_id
    ]
    temporary_path = log_path.with_name(
        f".{log_path.name}.{uuid.uuid4().hex}.rollback.tmp"
    )
    try:
        with temporary_path.open(
            "x",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
            writer.writeheader()
            writer.writerows(retained_rows)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary_path, log_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _remove_expected_result(
    result_path: Path,
    *,
    session_id: str,
    result_sha256: str,
) -> None:
    if not result_path.exists():
        return
    if not result_path.is_file():
        raise MeasurementTransactionError(
            "취소할 측정 상세 결과 경로가 일반 파일이 아닙니다."
        )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("session_id") != session_id
        or _semantic_json_sha256(payload) != result_sha256
    ):
        raise MeasurementTransactionError(
            "취소할 측정 상세 결과가 복구 기록과 일치하지 않습니다."
        )
    result_path.unlink()


def _cleanup_abandoned_temporary_files(root: Path) -> None:
    for path in root.glob(".*.json.*.tmp"):
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _validated_directory(path: Path, *, expected_parent: Path) -> Path:
    try:
        canonical_parent = expected_parent.resolve()
        canonical_path = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise MeasurementTransactionError(
            "측정 복구 기록 경로를 확인할 수 없습니다."
        ) from exc
    if (
        not path.is_dir()
        or path.is_symlink()
        or canonical_path.parent != canonical_parent
    ):
        raise MeasurementTransactionError(
            "측정 복구 기록 경로가 허용 폴더 밖을 가리킵니다."
        )
    return canonical_path


def _checkpoint(source: str, checkpoint: str, row_index: int) -> None:
    """No-op durability hook used only by subprocess fault tests."""
