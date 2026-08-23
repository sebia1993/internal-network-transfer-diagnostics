from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Response, jsonify, request, send_file, url_for

from access_security import AccessSecurity, AccessSecurityError
from .client_package import (
    ClientPackageError,
    build_client_package,
    client_executable_sha256,
    resolve_client_server_url,
    runtime_client_bundle,
)
from .excel import (
    EXCEL_MIME_TYPE,
    ProbeExcelError,
    build_probe_excel,
    build_probe_excel_filename,
)
from .service import ProbeService, ProbeServiceError


PROBE_JSON_MAX_BYTES = 64 * 1024
PROBE_JSON_READ_CHUNK_BYTES = 8 * 1024


def _default_lan_ip() -> str:
    return "127.0.0.1"


def create_probe_blueprint(
    service: ProbeService,
    *,
    web_port: int = 8000,
    lan_ip_resolver: Callable[[], str] = _default_lan_ip,
    client_bundle_path: str | Path | None = None,
    access_security: AccessSecurity | None = None,
) -> Blueprint:
    blueprint = Blueprint("network_probe", __name__, url_prefix="/api/network-probe")
    package_lock = threading.Lock()
    cached_client_hash: str | None = None
    bundle_path = (
        Path(client_bundle_path).resolve()
        if client_bundle_path is not None
        else runtime_client_bundle()
    )

    @blueprint.after_request
    def disable_sensitive_response_caching(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def client_ip() -> str:
        return service.normalize_ip(request.remote_addr)

    def package_client_hash(package_bundle: Path) -> str:
        nonlocal cached_client_hash
        with package_lock:
            if cached_client_hash is None:
                cached_client_hash = client_executable_sha256(package_bundle)
            return cached_client_hash

    def bearer_token() -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise ProbeServiceError("에이전트 인증 토큰이 없습니다.", 401)
        return header[7:].strip()

    def error_response(exc: ProbeServiceError):
        messages = {
            400: "TCP 측정 요청이 올바르지 않습니다.",
            401: "TCP 측정 인증이 필요하거나 올바르지 않습니다.",
            403: "TCP 측정 요청 권한이 없습니다.",
            404: "TCP 측정 결과나 클라이언트를 찾을 수 없습니다.",
            409: "TCP 측정 요청이 현재 상태와 충돌했습니다. Windows 방화벽과 클라이언트 상태를 확인하세요.",
            413: "TCP 측정 요청 본문은 64 KiB를 초과할 수 없습니다.",
            429: "등록 가능한 TCP 측정 클라이언트 수를 초과했습니다.",
            500: "RESULT_READ_FAILED: TCP 측정 결과를 안전하게 읽거나 생성할 수 없습니다.",
            503: "TCP 측정 기능을 사용할 수 없습니다. 설정과 Windows 클라이언트 프로그램 폴더를 확인하세요.",
        }
        return jsonify({"error": messages.get(exc.status_code, "TCP 측정 요청을 처리할 수 없습니다.")}), exc.status_code

    def payload() -> dict[str, Any]:
        content_length = request.content_length
        if content_length is not None and content_length > PROBE_JSON_MAX_BYTES:
            raise ProbeServiceError(
                "TCP 측정 JSON 요청 본문은 64 KiB를 초과할 수 없습니다.",
                413,
            )

        raw_body = bytearray()
        while True:
            remaining = PROBE_JSON_MAX_BYTES + 1 - len(raw_body)
            if remaining <= 0:
                raise ProbeServiceError(
                    "TCP 측정 JSON 요청 본문은 64 KiB를 초과할 수 없습니다.",
                    413,
                )
            chunk = request.stream.read(min(PROBE_JSON_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            raw_body.extend(chunk)
            if len(raw_body) > PROBE_JSON_MAX_BYTES:
                raise ProbeServiceError(
                    "TCP 측정 JSON 요청 본문은 64 KiB를 초과할 수 없습니다.",
                    413,
                )

        if not raw_body or not request.is_json:
            return {}
        try:
            value = json.loads(bytes(raw_body))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return {}
        return value if isinstance(value, dict) else {}

    def package_context() -> tuple[Path, str]:
        status_value = service.status_payload()
        if not status_value["enabled"]:
            raise ProbeServiceError("TCP 전송 성능 측정이 비활성화되어 있어 클라이언트 ZIP을 제공할 수 없습니다.", 503)
        if not status_value["available"]:
            raise ProbeServiceError(
                str(status_value.get("error") or "TCP 전송 성능 측정 서버를 사용할 수 없습니다."),
                503,
            )
        if bundle_path is None:
            raise ProbeServiceError("Windows Release 폴더로 실행할 때만 클라이언트 ZIP을 받을 수 있습니다.", 503)
        if not bundle_path.is_dir():
            raise ProbeServiceError("Windows 클라이언트 프로그램 폴더를 찾을 수 없습니다.", 503)
        try:
            server_url = resolve_client_server_url(
                request.host,
                fallback_host=lan_ip_resolver(),
                fallback_port=web_port,
                scheme=request.scheme,
            )
        except ClientPackageError as exc:
            raise ProbeServiceError(
                "클라이언트 패키지 접속 주소를 확인할 수 없습니다.",
                400,
            ) from exc
        return bundle_path, server_url

    @blueprint.get("/status")
    def status():
        value = service.status_payload()
        value.update(
            {
                "client_package_available": False,
                "client_package_error": "",
                "client_package_server_url": "",
                "client_executable_sha256": "",
                "client_package_url": url_for("network_probe.client_package"),
            }
        )
        try:
            package_bundle, server_url = package_context()
            value["client_package_available"] = True
            value["client_package_server_url"] = server_url
            value["client_executable_sha256"] = package_client_hash(package_bundle)
        except ClientPackageError:
            value["client_package_error"] = "Windows 클라이언트 패키지를 준비할 수 없습니다."
        except ProbeServiceError:
            value["client_package_error"] = (
                "Windows Release 폴더의 클라이언트 패키지를 준비할 수 없습니다."
            )
        return jsonify(value)

    @blueprint.get("/client-package.zip")
    def client_package():
        try:
            package_bundle, server_url = package_context()
            enrollment_token = (
                access_security.issue_enrollment_token()
                if access_security is not None
                else ""
            )
            with package_lock:
                package = build_client_package(
                    package_bundle,
                    server_url,
                    enrollment_token=enrollment_token,
                )
        except ProbeServiceError as exc:
            return error_response(exc)
        except ClientPackageError:
            return jsonify({"error": "Windows 클라이언트 패키지 생성에 실패했습니다."}), 500
        response = send_file(
            io.BytesIO(package.payload),
            mimetype="application/zip",
            as_attachment=True,
            download_name=package.download_name,
            max_age=0,
            conditional=False,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Client-Executable-SHA256"] = package.client_executable_sha256
        return response

    @blueprint.get("/agents")
    def agents():
        return jsonify({"agents": service.list_agents()})

    @blueprint.post("/agents/register")
    def register_agent():
        try:
            if access_security is not None:
                access_security.require_agent_enrollment(client_ip())
            return jsonify(service.register_agent(payload(), client_ip()))
        except AccessSecurityError:
            return jsonify(
                {
                    "error": "클라이언트 등록 토큰이 없거나 만료 또는 이미 사용되었습니다. "
                    "서버 화면에서 새 ZIP을 받으세요."
                }
            ), 401
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.post("/agents/<agent_id>/connectivity-failure")
    def connectivity_failure(agent_id: str):
        try:
            return jsonify(
                service.report_connectivity_failure(
                    agent_id,
                    bearer_token(),
                    client_ip(),
                    str(payload().get("error_code", "")),
                )
            )
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.get("/agents/<agent_id>/jobs/next")
    def next_job(agent_id: str):
        try:
            return jsonify(service.next_job(agent_id, bearer_token(), client_ip()))
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.post("/sessions")
    def create_session():
        try:
            value = payload()
            result = service.create_session(
                agent_id=str(value.get("agent_id", "")),
                direction=str(value.get("direction", "")),
                duration_seconds=int(value.get("duration_seconds", 0)),
                stream_count=int(value.get("stream_count", 0)),
            )
            return jsonify(result), 202
        except (TypeError, ValueError):
            return jsonify({"error": "TCP 측정 조건 형식이 올바르지 않습니다."}), 400
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.get("/sessions/<session_id>")
    def session_status(session_id: str):
        try:
            return jsonify(service.session_status(session_id))
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.post("/sessions/<session_id>/cancel")
    def cancel_session(session_id: str):
        try:
            return jsonify(service.cancel_session(session_id))
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.get("/sessions/<session_id>/control")
    def session_control(session_id: str):
        agent_id = request.args.get("agent_id", "")
        try:
            return jsonify(service.control_status(session_id, agent_id, bearer_token(), client_ip()))
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.post("/sessions/<session_id>/complete")
    def complete_phase(session_id: str):
        try:
            value = payload()
            agent_id = str(value.get("agent_id", ""))
            return jsonify(
                service.complete_agent_phase(session_id, agent_id, bearer_token(), client_ip(), value)
            )
        except ProbeServiceError as exc:
            return error_response(exc)

    @blueprint.get("/results/<session_id>.json")
    def result_json(session_id: str):
        try:
            result_text = service.result_text_for(session_id)
        except ProbeServiceError as exc:
            return error_response(exc)
        return Response(
            result_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="network-probe-{session_id}.json"'},
        )

    @blueprint.get("/results/<session_id>.xlsx")
    def result_excel(session_id: str):
        try:
            saved = service.saved_result_for(session_id)
            workbook = build_probe_excel(saved)
            filename = build_probe_excel_filename(saved)
        except ProbeServiceError as exc:
            return error_response(exc)
        except ProbeExcelError:
            return jsonify({"error": "TCP 측정 Excel 결과 생성에 실패했습니다."}), 500
        response = send_file(
            io.BytesIO(workbook),
            mimetype=EXCEL_MIME_TYPE,
            as_attachment=True,
            download_name=filename,
            max_age=0,
            conditional=False,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return blueprint
