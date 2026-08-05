from __future__ import annotations

import csv
import ctypes
import errno
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class CsvIntegrityError(RuntimeError):
    pass


class InstanceLockError(RuntimeError):
    pass


class InsufficientStorageError(RuntimeError):
    pass


class UploadConcurrencyError(RuntimeError):
    pass


INSTANCE_LOCK_RETRY_ATTEMPTS = 5
INSTANCE_LOCK_RETRY_INTERVAL_SECONDS = 0.05


DIAGNOSTIC_LOG_MAX_BYTES = 2 * 1024 * 1024
DIAGNOSTIC_LOG_BACKUP_COUNT = 5
LOW_FREE_SPACE_WARNING_BYTES = 1024 * 1024 * 1024
STORAGE_RESERVE_BYTES = LOW_FREE_SPACE_WARNING_BYTES
RECOVERY_BACKUP_COUNT = 5
MEASUREMENT_CSV_COMPACT_MIN_BYTES = 8 * 1024 * 1024
MEASUREMENT_CSV_MAX_ACTIVE_ROWS = 10_000
MEASUREMENT_CSV_KEEP_ROWS = 5_000
HEALTH_CHECK_CACHE_TTL_SECONDS = 5.0
UPLOAD_MAX_CONCURRENT = 4
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8
WINDOWS_DURABLE_REPLACE_FLAGS = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
_ARCHIVE_MONTH_PATTERN = re.compile(r"^(\d{4})-(\d{2})")


@dataclass(frozen=True)
class CsvIntegrityResult:
    path: Path
    valid: bool
    row_count: int = 0
    issue: str = ""
    recoverable_offset: int | None = None
    repaired: bool = False
    backup_path: Path | None = None


class TimedSnapshotCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = HEALTH_CHECK_CACHE_TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self.clock = clock
        self._lock = threading.Lock()
        self._created_at: float | None = None
        self._value = None

    def get(self, factory):
        now = self.clock()
        with self._lock:
            if (
                self._created_at is not None
                and now - self._created_at < self.ttl_seconds
            ):
                return self._value
            value = factory()
            self._value = value
            self._created_at = self.clock()
            return value

    def invalidate(self) -> None:
        with self._lock:
            self._created_at = None
            self._value = None


class UploadAdmissionReservation:
    def __init__(
        self,
        controller: "UploadAdmissionController",
        reservation_id: str,
    ) -> None:
        self._controller = controller
        self._reservation_id = reservation_id
        self._released = False

    def record_written(self, byte_count: int) -> None:
        if not self._released:
            self._controller._record_written(self._reservation_id, byte_count)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._controller._release(self._reservation_id)


class UploadAdmissionController:
    def __init__(
        self,
        storage_root: Path,
        *,
        max_concurrent: int = UPLOAD_MAX_CONCURRENT,
        reserve_bytes: int = STORAGE_RESERVE_BYTES,
        capacity_check: Callable[..., int] | None = None,
    ) -> None:
        self.storage_root = storage_root.resolve()
        self.max_concurrent = max(int(max_concurrent), 1)
        self.reserve_bytes = max(int(reserve_bytes), 0)
        self._capacity_check = capacity_check
        self._lock = threading.Lock()
        self._remaining_by_id: dict[str, int] = {}

    def acquire(self, required_bytes: int) -> UploadAdmissionReservation:
        required = max(int(required_bytes), 0)
        with self._lock:
            if len(self._remaining_by_id) >= self.max_concurrent:
                raise UploadConcurrencyError("동시에 처리할 수 있는 업로드 수를 초과했습니다.")
            total_required = sum(self._remaining_by_id.values()) + required
            capacity_check = self._capacity_check or ensure_storage_capacity
            capacity_check(
                self.storage_root,
                required_bytes=total_required,
                reserve_bytes=self.reserve_bytes,
            )
            reservation_id = uuid.uuid4().hex
            self._remaining_by_id[reservation_id] = required
        return UploadAdmissionReservation(self, reservation_id)

    def status(self) -> dict[str, int | bool]:
        with self._lock:
            active_uploads = len(self._remaining_by_id)
            return {
                "active_uploads": active_uploads,
                "max_concurrent_uploads": self.max_concurrent,
                "reserved_remaining_bytes": sum(self._remaining_by_id.values()),
                "at_capacity": active_uploads >= self.max_concurrent,
            }

    def _record_written(self, reservation_id: str, byte_count: int) -> None:
        written = max(int(byte_count), 0)
        if written == 0:
            return
        with self._lock:
            remaining = self._remaining_by_id.get(reservation_id)
            if remaining is not None:
                self._remaining_by_id[reservation_id] = max(remaining - written, 0)

    def _release(self, reservation_id: str) -> None:
        with self._lock:
            self._remaining_by_id.pop(reservation_id, None)


class _BinaryCsvLines:
    def __init__(self, handle) -> None:
        self.handle = handle
        self.position = 0
        self.last_line_terminated = True
        self._first_line = True

    def __iter__(self):
        return self

    def __next__(self) -> str:
        raw_line = self.handle.readline()
        if not raw_line:
            raise StopIteration
        self.position = self.handle.tell()
        self.last_line_terminated = raw_line.endswith(b"\n")
        encoding = "utf-8-sig" if self._first_line else "utf-8"
        self._first_line = False
        return raw_line.decode(encoding)


def inspect_csv_integrity(path: Path, fieldnames: list[str]) -> CsvIntegrityResult:
    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        return CsvIntegrityResult(path=path, valid=False, issue="missing")
    except OSError:
        return CsvIntegrityResult(path=path, valid=False, issue="unreadable")
    if file_size == 0:
        return CsvIntegrityResult(path=path, valid=False, issue="empty")

    try:
        handle = path.open("rb")
    except OSError:
        return CsvIntegrityResult(path=path, valid=False, issue="unreadable")
    with handle:
        lines = _BinaryCsvLines(handle)
        reader = csv.reader(lines, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            return CsvIntegrityResult(path=path, valid=False, issue="empty")
        except (csv.Error, UnicodeDecodeError):
            return CsvIntegrityResult(path=path, valid=False, issue="invalid_header")

        if header != fieldnames:
            return CsvIntegrityResult(path=path, valid=False, issue="invalid_header")
        if lines.position == file_size and not lines.last_line_terminated:
            return CsvIntegrityResult(path=path, valid=False, issue="unterminated_header")

        last_valid_offset = lines.position
        row_count = 0
        while True:
            previous_valid_offset = last_valid_offset
            try:
                row = next(reader)
            except StopIteration:
                return CsvIntegrityResult(path=path, valid=True, row_count=row_count)
            except (csv.Error, UnicodeDecodeError):
                recoverable = last_valid_offset if lines.position == file_size else None
                return CsvIntegrityResult(
                    path=path,
                    valid=False,
                    row_count=row_count,
                    issue="malformed_row",
                    recoverable_offset=recoverable,
                )

            if len(row) != len(fieldnames):
                recoverable = previous_valid_offset if lines.position == file_size else None
                return CsvIntegrityResult(
                    path=path,
                    valid=False,
                    row_count=row_count,
                    issue="wrong_column_count",
                    recoverable_offset=recoverable,
                )
            if lines.position == file_size and not lines.last_line_terminated:
                return CsvIntegrityResult(
                    path=path,
                    valid=False,
                    row_count=row_count,
                    issue="unterminated_last_row",
                    recoverable_offset=previous_valid_offset,
                )

            last_valid_offset = lines.position
            row_count += 1


def ensure_csv_integrity(path: Path, fieldnames: list[str]) -> CsvIntegrityResult:
    result = inspect_csv_integrity(path, fieldnames)
    if result.valid:
        return result
    if result.recoverable_offset is None:
        raise CsvIntegrityError(
            f"{path.name} CSV 손상을 자동 복구할 수 없습니다 ({result.issue})."
        )

    backup_path = _backup_csv(path)
    _replace_with_prefix(path, result.recoverable_offset)
    repaired = inspect_csv_integrity(path, fieldnames)
    if not repaired.valid:
        raise CsvIntegrityError(
            f"{path.name} CSV 마지막 행 복구 후 검증에 실패했습니다 ({repaired.issue})."
        )
    prune_recovery_backups(path)
    return replace(repaired, repaired=True, backup_path=backup_path)


def _backup_csv(path: Path) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(
        f"{path.name}.recovery-{timestamp}-{uuid.uuid4().hex[:8]}.bak"
    )
    with path.open("rb") as source, backup_path.open("xb") as backup:
        shutil.copyfileobj(source, backup)
        backup.flush()
        os.fsync(backup.fileno())
    _fsync_directory(path.parent)
    return backup_path


def prune_recovery_backups(path: Path, *, keep_count: int = RECOVERY_BACKUP_COUNT) -> int:
    keep = max(int(keep_count), 1)
    backups = sorted(
        path.parent.glob(f"{path.name}.recovery-*.bak"),
        key=lambda candidate: candidate.name,
        reverse=True,
    )
    removed = 0
    for backup in backups[keep:]:
        try:
            backup.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        _fsync_directory(path.parent)
    return removed


def archive_csv_history(
    path: Path,
    fieldnames: list[str],
    *,
    timestamp_field: str = "checked_at",
    min_size_bytes: int = MEASUREMENT_CSV_COMPACT_MIN_BYTES,
    max_active_rows: int = MEASUREMENT_CSV_MAX_ACTIVE_ROWS,
    keep_rows: int = MEASUREMENT_CSV_KEEP_ROWS,
    archive_root: Path | None = None,
) -> int:
    try:
        if path.stat().st_size < max(int(min_size_bytes), 0):
            return 0
    except FileNotFoundError:
        return 0

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != fieldnames:
            raise CsvIntegrityError(f"{path.name} CSV 헤더가 보관 기준과 다릅니다.")
        rows = list(reader)

    maximum = max(int(max_active_rows), 1)
    retained_count = min(max(int(keep_rows), 1), maximum)
    if len(rows) <= maximum:
        return 0

    archived_rows = rows[:-retained_count]
    retained_rows = rows[-retained_count:]
    root = archive_root or path.parent / "archives" / path.stem
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in archived_rows:
        grouped.setdefault(_archive_month(row.get(timestamp_field, "")), []).append(row)

    root.mkdir(parents=True, exist_ok=True)
    for month, month_rows in grouped.items():
        archive_path = root / f"{month}.csv"
        existing_rows: list[dict[str, str]] = []
        if archive_path.exists():
            with archive_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != fieldnames:
                    raise CsvIntegrityError(
                        f"{archive_path.name} CSV 보관 파일 헤더가 올바르지 않습니다."
                    )
                existing_rows = list(reader)
        existing_counts: dict[tuple[str, ...], int] = {}
        for row in existing_rows:
            row_key = tuple(row.get(field, "") for field in fieldnames)
            existing_counts[row_key] = existing_counts.get(row_key, 0) + 1
        candidate_counts: dict[tuple[str, ...], int] = {}
        merged_rows = list(existing_rows)
        for row in month_rows:
            row_key = tuple(row.get(field, "") for field in fieldnames)
            candidate_counts[row_key] = candidate_counts.get(row_key, 0) + 1
            if candidate_counts[row_key] <= existing_counts.get(row_key, 0):
                continue
            merged_rows.append(row)
        _write_csv_atomically(archive_path, fieldnames, merged_rows)

    _write_csv_atomically(path, fieldnames, retained_rows)
    return len(archived_rows)


def _archive_month(value: str) -> str:
    match = _ARCHIVE_MONTH_PATTERN.match(str(value).strip())
    if not match:
        return "unknown"
    year, month = match.groups()
    if not 1 <= int(month) <= 12:
        return "unknown"
    return f"{year}-{month}"


def _write_csv_atomically(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(
                {field: row.get(field, "") for field in fieldnames} for row in rows
            )
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _replace_with_prefix(path: Path, byte_count: int) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with path.open("rb") as source, temporary_path.open("xb") as destination:
            remaining = byte_count
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError("CSV 복구 대상 범위를 읽을 수 없습니다.")
                destination.write(chunk)
                remaining -= len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        durable_replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def durable_replace(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    _platform_name: str | None = None,
) -> None:
    source_path = Path(os.path.abspath(os.fspath(source)))
    destination_path = Path(os.path.abspath(os.fspath(destination)))
    platform_name = os.name if _platform_name is None else _platform_name
    if platform_name == "nt":
        _windows_durable_replace(source_path, destination_path)
        return

    os.replace(source_path, destination_path)
    _fsync_directory(destination_path.parent)


def _windows_durable_replace(source: Path, destination: Path) -> None:
    _call_move_file_ex(
        source,
        destination,
        WINDOWS_DURABLE_REPLACE_FLAGS,
    )


def _call_move_file_ex(source: Path, destination: Path, flags: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    )
    move_file_ex.restype = ctypes.c_int
    if move_file_ex(
        str(source),
        str(destination),
        flags,
    ):
        return

    error_code = ctypes.get_last_error()
    message = ctypes.FormatError(error_code).strip() or "MoveFileExW failed"
    raise OSError(error_code, message, str(destination))


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class DataDirectoryLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise InstanceLockError(
                "데이터 폴더의 단일 실행 잠금 파일을 준비할 수 없습니다."
            ) from exc

        lock_error: OSError | None = None
        for attempt in range(INSTANCE_LOCK_RETRY_ATTEMPTS):
            try:
                _lock_file(handle)
                lock_error = None
                break
            except OSError as exc:
                lock_error = exc
                if attempt + 1 < INSTANCE_LOCK_RETRY_ATTEMPTS:
                    time.sleep(INSTANCE_LOCK_RETRY_INTERVAL_SECONDS)
        if lock_error is not None:
            handle.close()
            raise InstanceLockError(
                "같은 데이터 폴더를 사용하는 사내 업로드 서버가 이미 실행 중입니다."
            ) from lock_error

        try:
            metadata = json.dumps(
                {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                ensure_ascii=True,
                sort_keys=True,
            ).encode("ascii")
            handle.seek(1)
            handle.truncate()
            handle.write(b"\n" + metadata + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                _unlock_file(handle)
            except OSError:
                pass
            handle.close()
            raise InstanceLockError(
                "데이터 폴더의 단일 실행 잠금 정보를 기록할 수 없습니다."
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            _unlock_file(handle)
        finally:
            handle.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except OSError:
            pass


def _lock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def configure_diagnostic_logger(
    data_root: Path,
    *,
    max_bytes: int = DIAGNOSTIC_LOG_MAX_BYTES,
    backup_count: int = DIAGNOSTIC_LOG_BACKUP_COUNT,
) -> logging.Logger:
    log_path = (data_root / "diagnostics" / "internal-upload.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger_name = f"internal_upload.{uuid.uuid5(uuid.NAMESPACE_URL, str(log_path)).hex}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    expected_path = str(log_path)
    if not any(getattr(handler, "_internal_upload_log_path", "") == expected_path for handler in logger.handlers):
        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        handler._internal_upload_log_path = expected_path
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)
    return logger


def attach_diagnostic_handlers(target: logging.Logger, source: logging.Logger) -> None:
    source_paths = {
        getattr(handler, "_internal_upload_log_path", "") for handler in source.handlers
    }
    for handler in list(target.handlers):
        path = getattr(handler, "_internal_upload_log_path", "")
        if path and path not in source_paths:
            target.removeHandler(handler)
            handler.close()
    existing_paths = {
        getattr(handler, "_internal_upload_log_path", "") for handler in target.handlers
    }
    for handler in source.handlers:
        path = getattr(handler, "_internal_upload_log_path", "")
        if path and path not in existing_paths:
            target.addHandler(handler)
            existing_paths.add(path)
    target.setLevel(logging.INFO)


def detach_diagnostic_handlers(target: logging.Logger, source: logging.Logger) -> None:
    source_handlers = set(source.handlers)
    for handler in list(target.handlers):
        if handler in source_handlers:
            target.removeHandler(handler)


def close_diagnostic_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if not getattr(handler, "_internal_upload_log_path", ""):
            continue
        logger.removeHandler(handler)
        handler.close()


def check_storage_health(
    path: Path,
    *,
    low_space_warning_bytes: int = LOW_FREE_SPACE_WARNING_BYTES,
) -> dict[str, object]:
    writable = False
    issue = ""
    probe_path = path / f".internal-upload-health-{uuid.uuid4().hex}.tmp"
    try:
        path.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"ok")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        writable = True
    except OSError:
        issue = "not_writable"
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        usage = shutil.disk_usage(path)
        free_bytes = int(usage.free)
    except OSError:
        free_bytes = -1
        issue = issue or "disk_usage_unavailable"
    low_space = free_bytes >= 0 and free_bytes < low_space_warning_bytes
    return {
        "writable": writable,
        "free_bytes": free_bytes,
        "low_space": low_space,
        "issue": issue,
    }


def ensure_storage_capacity(
    path: Path,
    *,
    required_bytes: int = 0,
    reserve_bytes: int = STORAGE_RESERVE_BYTES,
) -> int:
    required = max(int(required_bytes), 0)
    reserve = max(int(reserve_bytes), 0)
    try:
        path.mkdir(parents=True, exist_ok=True)
        free_bytes = int(shutil.disk_usage(path).free)
    except (OSError, ValueError) as exc:
        raise InsufficientStorageError(
            "서버 저장소의 여유 공간을 확인할 수 없습니다."
        ) from exc
    if free_bytes - required < reserve:
        raise InsufficientStorageError(
            "서버 저장 공간이 부족하여 요청을 처리할 수 없습니다."
        )
    return free_bytes


def is_storage_full_error(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    full_error_numbers = {errno.ENOSPC}
    if hasattr(errno, "EDQUOT"):
        full_error_numbers.add(errno.EDQUOT)
    return exc.errno in full_error_numbers or getattr(exc, "winerror", None) in {39, 112}


def is_process_running(process_id: int) -> bool:
    try:
        pid = int(process_id)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _is_windows_process_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _is_windows_process_running(process_id: int) -> bool:
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87

    if process_id > 0xFFFFFFFF:
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_invalid_parameter:
            return False
        if error_code == error_access_denied:
            return True
        return True

    exit_code = ctypes.c_uint32()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
