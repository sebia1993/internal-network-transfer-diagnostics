from __future__ import annotations

import errno
import json
import socket
import threading
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from app_version import APP_VERSION

from .models import PROBE_CONNECTIVITY_TIMEOUT_SECONDS, PROBE_PROTOCOL_VERSION
from .protocol import (
    ProbeProtocolError,
    ReplayGuard,
    recv_frame,
    send_frame,
    sign_frame,
    verify_frame_signature,
)
from .tcp_engine import (
    ProbeCancelled,
    ProbeTransferError,
    aggregate_stream_results,
    run_receiver_stream,
    run_sender_stream,
)


class ProbeClientError(RuntimeError):
    pass


def normalize_server_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        raise ProbeClientError("서버 주소는 http://PC이름:포트 또는 http://IP:포트 형식이어야 합니다.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ProbeClientError("서버 주소에는 계정, 경로, 쿼리 문자열을 넣을 수 없습니다.")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ProbeClientError("서버 웹 포트가 올바르지 않습니다.") from exc
    return f"http://{parsed.hostname}:{port}", parsed.hostname


class ProbeHttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        token: str = "",
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                message = error_payload.get("error") or str(exc)
            except Exception:
                message = str(exc)
            raise ProbeClientError(str(message)) from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            raise ProbeClientError(f"서버 API 연결에 실패했습니다: {exc}") from exc
        if not isinstance(value, dict):
            raise ProbeClientError("서버 API 응답 형식이 올바르지 않습니다.")
        return value


class ProbeAgent:
    def __init__(self, server_url: str, enrollment_token: str = "") -> None:
        self.base_url, self.server_host = normalize_server_url(server_url)
        self.http = ProbeHttpClient(self.base_url)
        self.agent_id = uuid.uuid4().hex
        self.hostname = socket.gethostname()[:64] or "Windows-PC"
        self.token = ""
        self.enrollment_token = enrollment_token
        self.tcp_hmac_key = ""
        self.replay_guard = ReplayGuard()
        self.registration: dict[str, Any] | None = None
        self.stop_event = threading.Event()
        self._last_connectivity_notice: tuple[str, str] | None = None

    def register(self) -> dict[str, Any]:
        response = self.http.request_json(
            "POST",
            "/api/network-probe/agents/register",
            payload={
                "agent_id": self.agent_id,
                "hostname": self.hostname,
                "server_host": self.server_host,
                "protocol_version": PROBE_PROTOCOL_VERSION,
                "client_version": APP_VERSION,
            },
            token=self.enrollment_token,
        )
        self.token = str(response.get("agent_token", ""))
        self.tcp_hmac_key = str(response.get("tcp_hmac_key", ""))
        if not self.token or not self.tcp_hmac_key:
            self.token = ""
            self.tcp_hmac_key = ""
            raise ProbeClientError("서버가 필요한 에이전트 인증 정보를 반환하지 않았습니다.")
        self.enrollment_token = ""
        self.registration = response
        return response

    def run_forever(self) -> int:
        retry_seconds = 1
        while not self.stop_event.is_set():
            try:
                registration = self.registration or self.register()
                if retry_seconds == 1:
                    print(
                        f"TCP 전송 성능 측정 클라이언트 {APP_VERSION} 등록됨: {self.hostname} "
                        f"({registration.get('client_ip', '-')})"
                    )
                self._check_connectivity(registration)
                print("웹 화면에서 이 PC를 선택해 측정을 시작하세요. 종료: Ctrl+C")
                retry_seconds = 1
                while not self.stop_event.is_set():
                    response = self.http.request_json(
                        "GET",
                        f"/api/network-probe/agents/{self.agent_id}/jobs/next",
                        token=self.token,
                        timeout=25,
                    )
                    job = response.get("job")
                    if isinstance(job, dict):
                        self._run_job(job)
                    self._check_connectivity(registration)
            except KeyboardInterrupt:
                self.stop_event.set()
            except ProbeClientError as exc:
                if self.stop_event.is_set():
                    break
                print(f"TCP 측정 서버 연결 오류: {exc}")
                if "인증" in str(exc) or "토큰" in str(exc):
                    print("서버 웹 화면에서 새 Windows 클라이언트 ZIP을 받아 다시 실행하세요.")
                    return 2
                print(f"{retry_seconds}초 후 다시 연결합니다.")
                self.stop_event.wait(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 15)
        return 0

    def _check_connectivity(self, registration: dict[str, Any]) -> bool:
        try:
            probe_port = int(registration.get("probe_port", 0))
            if not 1 <= probe_port <= 65535:
                raise ProbeClientError("서버가 올바른 TCP 측정 포트를 반환하지 않았습니다.")
            with socket.create_connection(
                (self.server_host, probe_port),
                timeout=PROBE_CONNECTIVITY_TIMEOUT_SECONDS,
            ) as sock:
                sock.settimeout(PROBE_CONNECTIVITY_TIMEOUT_SECONDS)
                send_frame(
                    sock,
                    sign_frame(
                        {
                            "type": "connectivity_check",
                            "protocol_version": PROBE_PROTOCOL_VERSION,
                            "agent_id": self.agent_id,
                            "client_version": APP_VERSION,
                        },
                        self.tcp_hmac_key,
                    ),
                )
                response = recv_frame(sock)
            if response.get("type") == "error":
                raise ProbeClientError(str(response.get("error", "TCP 연결 점검이 거부되었습니다.")))
            response = verify_frame_signature(
                response,
                self.tcp_hmac_key,
                self.replay_guard,
            )
            if (
                response.get("type") != "connectivity_ready"
                or int(response.get("protocol_version", 0)) != PROBE_PROTOCOL_VERSION
            ):
                raise ProbeClientError("TCP 연결 점검 응답이 올바르지 않습니다.")
        except (OSError, ProbeProtocolError, ProbeClientError, TypeError, ValueError) as exc:
            error_code = connectivity_error_code(exc)
            self._report_connectivity_failure(error_code)
            self._print_connectivity_notice(
                "failed",
                f"TCP {registration.get('probe_port', '-')} 연결 점검 실패: {exc}",
            )
            return False

        self._print_connectivity_notice(
            "ready",
            f"TCP {probe_port} 연결 준비 완료",
        )
        return True

    def _report_connectivity_failure(self, error_code: str) -> None:
        try:
            self.http.request_json(
                "POST",
                f"/api/network-probe/agents/{self.agent_id}/connectivity-failure",
                token=self.token,
                payload={"error_code": error_code},
                timeout=5,
            )
        except ProbeClientError:
            pass

    def _print_connectivity_notice(self, status: str, message: str) -> None:
        notice = (status, message)
        if notice == self._last_connectivity_notice:
            return
        self._last_connectivity_notice = notice
        print(message)

    def _run_job(self, job: dict[str, Any]) -> None:
        session_id = str(job.get("session_id", ""))
        phases = job.get("phases")
        if not session_id or not isinstance(phases, list):
            raise ProbeClientError("서버가 올바르지 않은 TCP 측정 작업을 전달했습니다.")
        print(
            f"측정 시작: {job.get('direction')} / {job.get('duration_seconds')}초 / "
            f"{job.get('stream_count')}개 스트림"
        )
        for phase in phases:
            try:
                result = self._run_phase(job, str(phase))
                response = self.http.request_json(
                    "POST",
                    f"/api/network-probe/sessions/{session_id}/complete",
                    token=self.token,
                    payload={
                        "agent_id": self.agent_id,
                        "phase": phase,
                        "status": "success",
                        "result": result,
                    },
                )
                if response.get("status") in {"cancelled", "failed"}:
                    print(f"측정 중단: {response.get('error', '')}")
                    return
            except (ProbeClientError, ProbeProtocolError, ProbeCancelled, ProbeTransferError, OSError) as exc:
                try:
                    self.http.request_json(
                        "POST",
                        f"/api/network-probe/sessions/{session_id}/complete",
                        token=self.token,
                        payload={
                            "agent_id": self.agent_id,
                            "phase": phase,
                            "status": "failure",
                            "error": str(exc),
                        },
                    )
                except ProbeClientError:
                    pass
                print(f"측정 실패: {exc}")
                return
        print("TCP 측정 완료")

    def _run_phase(self, job: dict[str, Any], phase: str) -> dict[str, Any]:
        session_id = str(job["session_id"])
        session_token = str(job["session_token"])
        probe_port = int(job["probe_port"])
        stream_count = int(job["stream_count"])
        duration_seconds = int(job["duration_seconds"])
        warmup_seconds = float(job["warmup_seconds"])
        sockets: dict[int, socket.socket] = {}
        cancel_event = threading.Event()
        done_event = threading.Event()
        try:
            for stream_id in range(stream_count):
                sock = socket.create_connection((self.server_host, probe_port), timeout=10)
                sock.settimeout(10)
                send_frame(
                    sock,
                    sign_frame(
                        {
                            "type": "data_stream",
                            "protocol_version": PROBE_PROTOCOL_VERSION,
                            "session_id": session_id,
                            "phase": phase,
                            "stream_id": stream_id,
                        },
                        session_token,
                    ),
                )
                ready = recv_frame(sock)
                if ready.get("type") == "error":
                    raise ProbeClientError(str(ready.get("error", "TCP 스트림 연결이 거부되었습니다.")))
                ready = verify_frame_signature(ready, session_token, self.replay_guard)
                if ready.get("type") != "ready":
                    raise ProbeClientError("TCP 스트림 준비 응답이 올바르지 않습니다.")
                sockets[stream_id] = sock
            for stream_id, sock in sockets.items():
                go = recv_frame(sock)
                if go.get("type") == "error":
                    raise ProbeClientError(str(go.get("error", "TCP 측정 시작이 거부되었습니다.")))
                go = verify_frame_signature(go, session_token, self.replay_guard)
                if go.get("type") != "go" or int(go.get("stream_id", -1)) != stream_id:
                    raise ProbeClientError("TCP 측정 시작 응답이 올바르지 않습니다.")

            control_thread = threading.Thread(
                target=self._poll_control,
                args=(session_id, cancel_event, done_event, sockets),
                daemon=True,
            )
            control_thread.start()
            role = "sender" if phase == "upload" else "receiver"
            results: list[dict[str, Any]] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()

            def worker(stream_id: int, sock: socket.socket) -> None:
                try:
                    if role == "sender":
                        result = run_sender_stream(
                            sock,
                            stream_id=stream_id,
                            warmup_seconds=warmup_seconds,
                            duration_seconds=duration_seconds,
                            cancel_event=cancel_event,
                        )
                    else:
                        result = run_receiver_stream(
                            sock,
                            stream_id=stream_id,
                            warmup_seconds=warmup_seconds,
                            duration_seconds=duration_seconds,
                            cancel_event=cancel_event,
                        )
                    with result_lock:
                        results.append(result)
                except BaseException as exc:
                    cancel_event.set()
                    with result_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=worker, args=item, daemon=True) for item in sockets.items()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=warmup_seconds + duration_seconds + 15)
            done_event.set()
            control_thread.join(timeout=2)
            if any(thread.is_alive() for thread in threads):
                raise ProbeClientError("TCP 클라이언트 측정이 제한 시간 안에 종료되지 않았습니다.")
            if errors:
                raise ProbeClientError(str(errors[0]))
            return aggregate_stream_results(results, role=role, duration_seconds=duration_seconds)
        finally:
            done_event.set()
            for sock in sockets.values():
                try:
                    sock.close()
                except OSError:
                    pass

    def _poll_control(
        self,
        session_id: str,
        cancel_event: threading.Event,
        done_event: threading.Event,
        sockets: dict[int, socket.socket],
    ) -> None:
        query = urlencode({"agent_id": self.agent_id})
        while not done_event.wait(0.5):
            try:
                response = self.http.request_json(
                    "GET",
                    f"/api/network-probe/sessions/{session_id}/control?{query}",
                    token=self.token,
                    timeout=3,
                )
            except ProbeClientError:
                continue
            if response.get("cancelled"):
                cancel_event.set()
                for sock in sockets.values():
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return


def run_probe_client(server_url: str, enrollment_token: str = "") -> int:
    return ProbeAgent(server_url, enrollment_token).run_forever()


def connectivity_error_code(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return "name_resolution_failed"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "connect_timeout"
    if isinstance(exc, (ProbeProtocolError, ProbeClientError, TypeError, ValueError)):
        return "protocol_error"
    if isinstance(exc, OSError) and exc.errno in {
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }:
        return "network_unreachable"
    return "connection_error"
