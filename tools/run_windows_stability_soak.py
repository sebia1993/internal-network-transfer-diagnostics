from __future__ import annotations

import argparse
import ctypes
import csv
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DURATION_MINUTES = 45.0
HEALTH_TIMEOUT_SECONDS = 20.0
PROCESS_STOP_TIMEOUT_SECONDS = 10.0
SOAK_UPLOAD_BYTES = 256 * 1024
PROCESS_SAMPLE_INTERVAL_SECONDS = 1.0
PROCESS_RESOURCE_METRICS = (
    "working_set_bytes",
    "handle_count",
    "thread_count",
    "tcp_socket_count",
)
_IS_WINDOWS = os.name == "nt"

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010
_TH32CS_SNAPTHREAD = 0x00000004
_ERROR_NO_MORE_FILES = 18
_ERROR_INSUFFICIENT_BUFFER = 122
_AF_INET = 2
_AF_INET6 = 23
_TCP_TABLE_OWNER_PID_ALL = 5
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class ProcessResourceSample:
    values: dict[str, int | None]
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SoakSummary:
    status: str
    duration_seconds: float
    completed_cycles: int
    uploaded_bytes: int
    tcp_self_checks: int
    process_resources: dict[str, Any] = field(default_factory=dict)


class _WindowsApi:
    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

        self.open_process = kernel32.OpenProcess
        self.open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.open_process.restype = wintypes.HANDLE

        self.close_handle = kernel32.CloseHandle
        self.close_handle.argtypes = [wintypes.HANDLE]
        self.close_handle.restype = wintypes.BOOL

        self.get_process_handle_count = kernel32.GetProcessHandleCount
        self.get_process_handle_count.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.get_process_handle_count.restype = wintypes.BOOL

        self.create_toolhelp32_snapshot = kernel32.CreateToolhelp32Snapshot
        self.create_toolhelp32_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.create_toolhelp32_snapshot.restype = wintypes.HANDLE

        self.thread32_first = kernel32.Thread32First
        self.thread32_first.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_THREADENTRY32),
        ]
        self.thread32_first.restype = wintypes.BOOL

        self.thread32_next = kernel32.Thread32Next
        self.thread32_next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_THREADENTRY32),
        ]
        self.thread32_next.restype = wintypes.BOOL

        self.get_process_memory_info = psapi.GetProcessMemoryInfo
        self.get_process_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.get_process_memory_info.restype = wintypes.BOOL

        self.get_extended_tcp_table = iphlpapi.GetExtendedTcpTable
        self.get_extended_tcp_table.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.ULONG,
        ]
        self.get_extended_tcp_table.restype = wintypes.DWORD


def _exception_text(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"[:500]
    return type(exc).__name__


def _winapi_failure(name: str) -> OSError:
    return OSError(f"{name} failed (winerror={ctypes.get_last_error()})")


def _unavailable_resource_sample(reason: str) -> ProcessResourceSample:
    return ProcessResourceSample(
        values={name: None for name in PROCESS_RESOURCE_METRICS},
        errors={name: reason for name in PROCESS_RESOURCE_METRICS},
    )


class WindowsProcessResourceReader:
    """Read one process snapshot using only Windows standard ctypes APIs."""

    def __init__(self) -> None:
        self._api: _WindowsApi | None = None
        self._initialization_error = ""
        if not _IS_WINDOWS:
            self._initialization_error = (
                "Windows process APIs are unavailable on this platform."
            )
            return
        try:
            self._api = _WindowsApi()
        except Exception as exc:
            self._initialization_error = (
                "Windows process API initialization failed: "
                + _exception_text(exc)
            )

    def sample(self, pid: int) -> ProcessResourceSample:
        if self._api is None:
            return _unavailable_resource_sample(self._initialization_error)

        values = {name: None for name in PROCESS_RESOURCE_METRICS}
        errors: dict[str, str] = {}
        process_handle = None
        try:
            process_handle = self._open_process(pid)
        except Exception as exc:
            reason = _exception_text(exc)
            errors["working_set_bytes"] = reason
            errors["handle_count"] = reason
        else:
            try:
                values["working_set_bytes"] = self._working_set_bytes(process_handle)
            except Exception as exc:
                errors["working_set_bytes"] = _exception_text(exc)
            try:
                values["handle_count"] = self._handle_count(process_handle)
            except Exception as exc:
                errors["handle_count"] = _exception_text(exc)
        finally:
            if process_handle:
                self._api.close_handle(process_handle)

        try:
            values["thread_count"] = self._thread_count(pid)
        except Exception as exc:
            errors["thread_count"] = _exception_text(exc)

        try:
            tcp_count, tcp_warning = self._tcp_socket_count(pid)
            values["tcp_socket_count"] = tcp_count
            if tcp_warning:
                errors["tcp_socket_count"] = tcp_warning
        except Exception as exc:
            errors["tcp_socket_count"] = _exception_text(exc)

        return ProcessResourceSample(values=values, errors=errors)

    def resolve_tcp_owner_pid(
        self,
        launcher_pid: int,
        local_ports: set[int],
    ) -> int:
        """Return the process that owns the server ports when a launcher forks."""
        if self._api is None or not local_ports:
            return launcher_pid
        owner_matches: dict[int, int] = {}
        for family, row_type in (
            (_AF_INET, _MIB_TCPROW_OWNER_PID),
            (_AF_INET6, _MIB_TCP6ROW_OWNER_PID),
        ):
            try:
                rows = self._tcp_table_rows(family, row_type)
            except Exception:
                continue
            for row in rows:
                local_port = socket.ntohs(int(row.dwLocalPort) & 0xFFFF)
                if local_port not in local_ports:
                    continue
                owner_pid = int(row.dwOwningPid)
                if owner_pid > 0:
                    owner_matches[owner_pid] = owner_matches.get(owner_pid, 0) + 1
        if not owner_matches:
            return launcher_pid
        return max(
            owner_matches,
            key=lambda pid: (owner_matches[pid], pid == launcher_pid),
        )

    def _open_process(self, pid: int):
        assert self._api is not None
        if pid <= 0:
            raise ValueError("pid must be a positive integer")
        for access in (
            _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
            _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ,
        ):
            ctypes.set_last_error(0)
            handle = self._api.open_process(access, False, pid)
            if handle:
                return handle
        raise _winapi_failure("OpenProcess")

    def _working_set_bytes(self, process_handle) -> int:
        assert self._api is not None
        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ctypes.set_last_error(0)
        if not self._api.get_process_memory_info(
            process_handle,
            ctypes.byref(counters),
            counters.cb,
        ):
            raise _winapi_failure("GetProcessMemoryInfo")
        return int(counters.WorkingSetSize)

    def _handle_count(self, process_handle) -> int:
        assert self._api is not None
        count = wintypes.DWORD()
        ctypes.set_last_error(0)
        if not self._api.get_process_handle_count(process_handle, ctypes.byref(count)):
            raise _winapi_failure("GetProcessHandleCount")
        return int(count.value)

    def _thread_count(self, pid: int) -> int:
        assert self._api is not None
        ctypes.set_last_error(0)
        snapshot = self._api.create_toolhelp32_snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            raise _winapi_failure("CreateToolhelp32Snapshot")
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            ctypes.set_last_error(0)
            if not self._api.thread32_first(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() == _ERROR_NO_MORE_FILES:
                    return 0
                raise _winapi_failure("Thread32First")
            count = 0
            while True:
                if int(entry.th32OwnerProcessID) == pid:
                    count += 1
                ctypes.set_last_error(0)
                if not self._api.thread32_next(snapshot, ctypes.byref(entry)):
                    error = ctypes.get_last_error()
                    if error not in (0, _ERROR_NO_MORE_FILES):
                        raise _winapi_failure("Thread32Next")
                    break
            return count
        finally:
            self._api.close_handle(snapshot)

    def _tcp_socket_count(self, pid: int) -> tuple[int | None, str]:
        counts = []
        errors = []
        for family, row_type, label in (
            (_AF_INET, _MIB_TCPROW_OWNER_PID, "IPv4"),
            (_AF_INET6, _MIB_TCP6ROW_OWNER_PID, "IPv6"),
        ):
            try:
                counts.append(self._tcp_table_pid_count(pid, family, row_type))
            except Exception as exc:
                errors.append(f"{label}: {_exception_text(exc)}")
        if not counts:
            raise OSError("; ".join(errors) or "TCP table APIs returned no result")
        warning = "; ".join(errors)[:500]
        return sum(counts), warning

    def _tcp_table_pid_count(
        self,
        pid: int,
        family: int,
        row_type: type[ctypes.Structure],
    ) -> int:
        return sum(
            1
            for row in self._tcp_table_rows(family, row_type)
            if int(row.dwOwningPid) == pid
        )

    def _tcp_table_rows(
        self,
        family: int,
        row_type: type[ctypes.Structure],
    ) -> list[ctypes.Structure]:
        assert self._api is not None
        size = wintypes.DWORD()
        result = self._api.get_extended_tcp_table(
            None,
            ctypes.byref(size),
            False,
            family,
            _TCP_TABLE_OWNER_PID_ALL,
            0,
        )
        if result not in (0, _ERROR_INSUFFICIENT_BUFFER):
            raise OSError(f"GetExtendedTcpTable failed (status={int(result)})")
        if size.value == 0:
            return []

        buffer = None
        for _ in range(3):
            buffer = ctypes.create_string_buffer(size.value)
            result = self._api.get_extended_tcp_table(
                buffer,
                ctypes.byref(size),
                False,
                family,
                _TCP_TABLE_OWNER_PID_ALL,
                0,
            )
            if result == _ERROR_INSUFFICIENT_BUFFER:
                continue
            if result != 0:
                raise OSError(
                    f"GetExtendedTcpTable failed (status={int(result)})"
                )
            break
        else:
            raise OSError("GetExtendedTcpTable changed size repeatedly")

        assert buffer is not None
        if len(buffer) < ctypes.sizeof(wintypes.DWORD):
            raise OSError("GetExtendedTcpTable returned a truncated table")
        row_count = int(wintypes.DWORD.from_buffer_copy(buffer.raw).value)
        row_size = ctypes.sizeof(row_type)
        required_size = ctypes.sizeof(wintypes.DWORD) + (row_count * row_size)
        if required_size > len(buffer):
            raise OSError("GetExtendedTcpTable returned invalid row count")

        rows = []
        offset = ctypes.sizeof(wintypes.DWORD)
        for _ in range(row_count):
            row = row_type.from_buffer_copy(buffer.raw, offset)
            rows.append(row)
            offset += row_size
        return rows


def _metric_summary(
    samples: list[ProcessResourceSample],
    metric: str,
) -> dict[str, Any]:
    values = [
        int(sample.values[metric])
        for sample in samples
        if sample.values.get(metric) is not None
    ]
    reasons = sorted(
        {
            sample.errors[metric]
            for sample in samples
            if sample.errors.get(metric)
        }
    )
    missing_count = sum(
        1 for sample in samples if sample.values.get(metric) is None
    )
    if not values:
        status = "unavailable"
        start = end = maximum = increase = None
    else:
        status = "partial" if missing_count or reasons else "available"
        start = values[0]
        end = values[-1]
        maximum = max(values)
        increase = end - start
    return {
        "status": status,
        "start": start,
        "end": end,
        "maximum": maximum,
        "increase": increase,
        "reason": "; ".join(reasons)[:500],
    }


def _combined_status(statuses: list[str]) -> str:
    if statuses and all(status == "available" for status in statuses):
        return "available"
    if not statuses or all(status == "unavailable" for status in statuses):
        return "unavailable"
    return "partial"


def summarize_process_samples(
    *,
    pid: int,
    label: str,
    samples: list[ProcessResourceSample],
) -> dict[str, Any]:
    metrics = {
        name: _metric_summary(samples, name)
        for name in PROCESS_RESOURCE_METRICS
    }
    return {
        "label": label,
        "pid": pid,
        "status": _combined_status(
            [metric["status"] for metric in metrics.values()]
        ),
        "sample_count": len(samples),
        "metrics": metrics,
    }


def summarize_process_resources(
    process_summaries: list[dict[str, Any]],
    *,
    sample_interval_seconds: float,
) -> dict[str, Any]:
    metrics: dict[str, dict[str, Any]] = {}
    for name in PROCESS_RESOURCE_METRICS:
        run_metrics = [
            process["metrics"][name]
            for process in process_summaries
            if process["metrics"][name]["status"] != "unavailable"
        ]
        reasons = sorted(
            {
                process["metrics"][name]["reason"]
                for process in process_summaries
                if process["metrics"][name]["reason"]
            }
        )
        if not run_metrics:
            metrics[name] = {
                "status": "unavailable",
                "start": None,
                "end": None,
                "maximum": None,
                "increase": None,
                "reason": (
                    "; ".join(reasons)
                    or "No process resource sample was available."
                )[:500],
            }
            continue
        start = next(
            metric["start"] for metric in run_metrics if metric["start"] is not None
        )
        end = next(
            metric["end"]
            for metric in reversed(run_metrics)
            if metric["end"] is not None
        )
        statuses = [
            process["metrics"][name]["status"]
            for process in process_summaries
        ]
        metrics[name] = {
            "status": _combined_status(statuses),
            "start": start,
            "end": end,
            "maximum": max(
                metric["maximum"]
                for metric in run_metrics
                if metric["maximum"] is not None
            ),
            "increase": end - start,
            "reason": "; ".join(reasons)[:500],
        }

    statuses = [metric["status"] for metric in metrics.values()]
    reasons = sorted(
        {
            metric["reason"]
            for metric in metrics.values()
            if metric["reason"]
        }
    )
    return {
        "status": _combined_status(statuses),
        "platform": sys.platform,
        "api": "Windows ctypes",
        "sample_interval_seconds": sample_interval_seconds,
        "process_count": len(process_summaries),
        "sample_count": sum(
            int(process["sample_count"]) for process in process_summaries
        ),
        "reason": "; ".join(reasons)[:500],
        "metrics": metrics,
        "processes": process_summaries,
    }


class ProcessResourceMonitor:
    def __init__(
        self,
        *,
        pid: int,
        label: str,
        reader: WindowsProcessResourceReader,
        interval_seconds: float = PROCESS_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self.pid = pid
        self.label = label
        self.reader = reader
        self.interval_seconds = max(float(interval_seconds), 0.01)
        self.samples: list[ProcessResourceSample] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._summary: dict[str, Any] | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._capture()
        self._thread = threading.Thread(
            target=self._sample_loop,
            name=f"soak-resource-monitor-{self.pid}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        if self._summary is not None:
            return self._summary
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_seconds * 2))
        self._capture()
        self._summary = summarize_process_samples(
            pid=self.pid,
            label=self.label,
            samples=self.samples,
        )
        return self._summary

    def _sample_loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._capture()

    def _capture(self) -> None:
        try:
            sample = self.reader.sample(self.pid)
            if not isinstance(sample, ProcessResourceSample):
                raise TypeError("resource reader returned an invalid sample")
        except Exception as exc:
            sample = _unavailable_resource_sample(
                "Process resource sampling failed: " + _exception_text(exc)
            )
        self.samples.append(sample)


def available_port(*, excluded: set[int] | None = None) -> int:
    blocked = excluded or set()
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port not in blocked:
            return port
    raise RuntimeError("서로 다른 로컬 시험 포트를 확보하지 못했습니다.")


def write_soak_config(root: Path, web_port: int, probe_port: int) -> Path:
    config_path = root / "config.ini"
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                "CONFIG_VERSION=3",
                "HOST=127.0.0.1",
                f"PORT={web_port}",
                f"BASE_URL=http://127.0.0.1:{web_port}",
                "STORAGE_ROOT=uploads",
                "DELETE_ALLOWED_IPS=127.0.0.1,::1",
                "RECENT_LIMIT=50",
                "",
                "[network_probe]",
                "ENABLED=true",
                f"PORT={probe_port}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def build_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def start_server(config_path: Path, log_path: Path) -> subprocess.Popen[bytes]:
    with log_path.open("ab") as output:
        return subprocess.Popen(
            [sys.executable, "app.py", "--config", str(config_path)],
            cwd=PROJECT_ROOT,
            env=build_subprocess_environment(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def process_log_tail(log_path: Path, *, limit: int = 8_000) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def wait_for_health(
    web_port: int,
    process: subprocess.Popen[bytes],
    log_path: Path,
    *,
    timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "시험 서버가 상태 확인 전에 종료되었습니다.\n"
                + process_log_tail(log_path)
            )
        connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=2)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            probe = payload.get("checks", {}).get("tcp_probe", {})
            if (
                response.status == 200
                and payload.get("app") == "internal-upload"
                and probe.get("enabled") is True
                and probe.get("available") is True
            ):
                return payload
            last_error = f"HTTP {response.status}, TCP probe={probe!r}"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        finally:
            connection.close()
        time.sleep(0.1)
    raise RuntimeError(
        f"시험 서버 상태 확인 시간이 초과되었습니다: {last_error}\n"
        + process_log_tail(log_path)
    )


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)


def wait_for_port_release(port: int, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                time.sleep(0.05)
                continue
            return
    raise RuntimeError(f"TCP {port} 포트가 서버 종료 후 해제되지 않았습니다.")


def build_multipart_upload(filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"internal-upload-soak-{uuid.uuid4().hex}"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="storage_subdir"\r\n\r\n'
        "soak\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="memo"\r\n\r\n'
        "windows stability soak\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return prefix + content + suffix, boundary


def upload_file(web_port: int, filename: str, content: bytes) -> None:
    body, boundary = build_multipart_upload(filename, content)
    connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=30)
    try:
        connection.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        response_body = response.read()
        if response.status != 200:
            raise RuntimeError(
                f"시험 업로드 실패: HTTP {response.status}, body={response_body[-500:]!r}"
            )
    finally:
        connection.close()


def find_download_path(data_root: Path, filename: str) -> str:
    log_path = data_root / "upload_log.csv"
    with log_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in reversed(rows):
        if row.get("original_filename") == filename:
            parsed = urlsplit(row.get("download_url", ""))
            if parsed.path.startswith("/download/"):
                return parsed.path
    raise RuntimeError(f"업로드 기록에서 {filename} 다운로드 경로를 찾지 못했습니다.")


def download_file(web_port: int, path: str) -> bytes:
    connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=30)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        content = response.read()
        if response.status != 200:
            raise RuntimeError(f"시험 다운로드 실패: HTTP {response.status}")
        return content
    finally:
        connection.close()


def run_tcp_self_check() -> None:
    completed = subprocess.run(
        [sys.executable, "app.py", "--probe-self-check"],
        cwd=PROJECT_ROOT,
        env=build_subprocess_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "TCP 자체 시험 실패:\n"
            + completed.stdout[-4_000:]
            + completed.stderr[-4_000:]
        )


def run_cycle(
    root: Path,
    cycle: int,
    *,
    resource_reader: WindowsProcessResourceReader | None = None,
    sample_interval_seconds: float = PROCESS_SAMPLE_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    web_port = available_port()
    probe_port = available_port(excluded={web_port})
    config_path = write_soak_config(root, web_port, probe_port)
    filename = f"soak-{cycle:06d}.txt"
    marker = f"cycle={cycle};".encode("ascii")
    content = (marker * ((SOAK_UPLOAD_BYTES // len(marker)) + 1))[:SOAK_UPLOAD_BYTES]
    first_log = root / f"server-{cycle:06d}-first.log"
    second_log = root / f"server-{cycle:06d}-restart.log"
    reader = resource_reader or WindowsProcessResourceReader()
    process_summaries: list[dict[str, Any]] = []

    first = start_server(config_path, first_log)
    first_monitor: ProcessResourceMonitor | None = None
    try:
        wait_for_health(web_port, first, first_log)
        first_monitor = ProcessResourceMonitor(
            pid=reader.resolve_tcp_owner_pid(first.pid, {web_port, probe_port}),
            label=f"cycle-{cycle:06d}-initial",
            reader=reader,
            interval_seconds=sample_interval_seconds,
        )
        first_monitor.start()
        upload_file(web_port, filename, content)
        download_path = find_download_path(root / "data", filename)
    finally:
        if first_monitor is not None:
            process_summaries.append(first_monitor.stop())
        stop_server(first)
    wait_for_port_release(web_port)
    wait_for_port_release(probe_port)

    restarted = start_server(config_path, second_log)
    restart_monitor: ProcessResourceMonitor | None = None
    try:
        wait_for_health(web_port, restarted, second_log)
        restart_monitor = ProcessResourceMonitor(
            pid=reader.resolve_tcp_owner_pid(
                restarted.pid,
                {web_port, probe_port},
            ),
            label=f"cycle-{cycle:06d}-restart",
            reader=reader,
            interval_seconds=sample_interval_seconds,
        )
        restart_monitor.start()
        if download_file(web_port, download_path) != content:
            raise RuntimeError("서버 재시작 후 다운로드 내용이 업로드 원본과 다릅니다.")
    finally:
        if restart_monitor is not None:
            process_summaries.append(restart_monitor.stop())
        stop_server(restarted)
    wait_for_port_release(web_port)
    wait_for_port_release(probe_port)
    run_tcp_self_check()
    return process_summaries


def run_soak(*, duration_minutes: float, max_cycles: int | None = None) -> SoakSummary:
    duration_seconds = max(float(duration_minutes), 0.01) * 60.0
    started = time.monotonic()
    deadline = started + duration_seconds
    completed_cycles = 0
    resource_reader = WindowsProcessResourceReader()
    process_summaries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="internal-upload-windows-soak-") as temporary_root:
        root = Path(temporary_root)
        while completed_cycles == 0 or time.monotonic() < deadline:
            if max_cycles is not None and completed_cycles >= max_cycles:
                break
            process_summaries.extend(
                run_cycle(
                    root,
                    completed_cycles + 1,
                    resource_reader=resource_reader,
                    sample_interval_seconds=PROCESS_SAMPLE_INTERVAL_SECONDS,
                )
            )
            completed_cycles += 1
            print(
                f"soak cycle {completed_cycles} passed "
                f"({time.monotonic() - started:.1f}s elapsed)",
                flush=True,
            )
    return SoakSummary(
        status="success",
        duration_seconds=round(time.monotonic() - started, 3),
        completed_cycles=completed_cycles,
        uploaded_bytes=completed_cycles * SOAK_UPLOAD_BYTES,
        tcp_self_checks=completed_cycles,
        process_resources=summarize_process_resources(
            process_summaries,
            sample_interval_seconds=PROCESS_SAMPLE_INTERVAL_SECONDS,
        ),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows 장시간 안정성 반복 시험")
    parser.add_argument("--duration-minutes", type=float, default=DEFAULT_DURATION_MINUTES)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--summary-path", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0.01 <= args.duration_minutes <= 60:
        raise SystemExit("--duration-minutes는 0.01~60 범위여야 합니다.")
    if args.max_cycles is not None and args.max_cycles < 1:
        raise SystemExit("--max-cycles는 1 이상이어야 합니다.")
    summary = run_soak(
        duration_minutes=args.duration_minutes,
        max_cycles=args.max_cycles,
    )
    payload = json.dumps(asdict(summary), ensure_ascii=False, indent=2)
    binary_stdout = getattr(sys.stdout, "buffer", None)
    if binary_stdout is not None:
        binary_stdout.write((payload + "\n").encode("utf-8"))
        binary_stdout.flush()
    else:
        sys.stdout.write(payload + "\n")
    if args.summary_path:
        Path(args.summary_path).write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
