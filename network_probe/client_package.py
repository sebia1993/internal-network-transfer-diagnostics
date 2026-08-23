from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from app_version import APP_VERSION
from access_security import ENROLLMENT_TOKEN_PREFIX


HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
NUMERIC_ADDRESS_RE = re.compile(r"^[0-9.]+$")
CLIENT_EXECUTABLE_NAME = "NetworkProbeClient.exe"
CLIENT_CONFIG_NAME = "client-config.json"
CLIENT_MANIFEST_NAME = "client-manifest.json"
CLIENT_README_NAME = "README_CLIENT_KO.txt"


class ClientPackageError(ValueError):
    pass


@dataclass(frozen=True)
class ClientPackage:
    payload: bytes
    server_url: str
    download_name: str
    root_name: str
    client_executable_sha256: str


def runtime_client_bundle() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent / "client-template"


def _normalize_host(value: str) -> tuple[str, ipaddress.IPv4Address | None]:
    if not value or value != value.strip():
        raise ClientPackageError("현재 접속 주소의 호스트 형식이 올바르지 않습니다.")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ClientPackageError("클라이언트 ZIP은 ASCII PC 이름 또는 IPv4 주소만 지원합니다.") from exc

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None
    if address is not None:
        if not isinstance(address, ipaddress.IPv4Address):
            raise ClientPackageError("클라이언트 ZIP은 IPv6 주소를 지원하지 않습니다.")
        if address.is_unspecified or address.is_multicast:
            raise ClientPackageError("클라이언트가 접속할 수 있는 서버 IPv4 주소가 아닙니다.")
        return str(address), address

    if NUMERIC_ADDRESS_RE.fullmatch(value):
        raise ClientPackageError("서버 IPv4 주소 형식이 올바르지 않습니다.")
    if len(value) > 253:
        raise ClientPackageError("서버 PC 이름이 너무 깁니다.")
    labels = value.split(".")
    if not labels or any(not HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise ClientPackageError("서버 PC 이름에는 영문자, 숫자, 점과 하이픈만 사용할 수 있습니다.")
    return value.lower(), None


def _parse_host_port(value: str, default_port: int) -> tuple[str, int, ipaddress.IPv4Address | None]:
    raw = value.strip()
    if not raw or raw != value:
        raise ClientPackageError("현재 접속 주소의 형식이 올바르지 않습니다.")
    if raw.startswith("[") or raw.count(":") > 1:
        raise ClientPackageError("클라이언트 ZIP은 IPv6 주소를 지원하지 않습니다.")

    host_value = raw
    port = default_port
    if ":" in raw:
        host_value, port_value = raw.rsplit(":", 1)
        if not port_value.isascii() or not port_value.isdigit():
            raise ClientPackageError("서버 웹 포트 형식이 올바르지 않습니다.")
        port = int(port_value)
    if not 1 <= port <= 65535:
        raise ClientPackageError("서버 웹 포트는 1~65535 범위여야 합니다.")

    host, address = _normalize_host(host_value)
    return host, port, address


def resolve_client_server_url(
    request_host: str,
    *,
    fallback_host: str,
    fallback_port: int,
    scheme: str = "http",
) -> str:
    if scheme.lower() != "http":
        raise ClientPackageError("TCP 측정 클라이언트는 HTTP 서버 주소만 지원합니다.")
    host, port, address = _parse_host_port(request_host, 80)
    is_loopback = host == "localhost" or bool(address and address.is_loopback)
    if is_loopback:
        host, fallback_address = _normalize_host(fallback_host.strip())
        if host == "localhost" or (fallback_address and fallback_address.is_loopback):
            raise ClientPackageError("서버 PC의 사내 IPv4 주소를 자동 감지하지 못했습니다.")
        if not 1 <= fallback_port <= 65535:
            raise ClientPackageError("설정된 서버 웹 포트가 올바르지 않습니다.")
        port = fallback_port
    return f"http://{host}:{port}"


def _validated_server_host(server_url: str) -> str:
    parsed = urlsplit(server_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ClientPackageError("클라이언트 서버 URL 형식이 올바르지 않습니다.")
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ClientPackageError("클라이언트 서버 URL에는 주소와 포트만 사용할 수 있습니다.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ClientPackageError("클라이언트 서버 URL 포트가 올바르지 않습니다.") from exc
    if port is None:
        raise ClientPackageError("클라이언트 서버 URL에 웹 포트가 필요합니다.")
    host, _ = _normalize_host(parsed.hostname)
    return host


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _client_readme(server_url: str, executable_sha256: str) -> bytes:
    text = "\r\n".join(
        [
            "사내 업로드 TCP 전송 성능 측정 Windows 클라이언트",
            "",
            f"클라이언트 버전: {APP_VERSION}",
            f"자동 설정된 서버 주소: {server_url}",
            f"클라이언트 EXE SHA256: {executable_sha256}",
            "",
            "1. ZIP 파일을 원하는 폴더에 완전히 압축 해제합니다.",
            f"2. {CLIENT_EXECUTABLE_NAME}를 더블클릭합니다.",
            "3. 콘솔 창을 열어 둔 상태에서 서버 웹 화면의 TCP 전송 성능 측정을 실행합니다.",
            "4. 종료할 때는 콘솔 창에서 Ctrl+C를 누릅니다.",
            "",
            "별도 주소 입력이나 config.ini 설정은 필요하지 않습니다.",
            "서버 PC의 IP 또는 웹 포트가 바뀌면 서버 웹 화면에서 ZIP을 다시 받으세요.",
            "TCP 측정 포트는 서버가 자동으로 전달하므로 포트만 바뀐 경우에는 다시 받을 필요가 없습니다.",
            "이 ZIP의 등록 토큰은 짧은 시간 동안 한 번만 사용할 수 있습니다. 등록에 실패하거나 ZIP을 다른 PC에서 이미 실행했다면 새 ZIP을 받으세요.",
            "등록이 끝나면 ZIP과 압축 해제한 client-config.json을 안전하게 삭제하세요.",
            "클라이언트 PC의 인바운드 방화벽 포트는 열 필요가 없습니다.",
            "이 클라이언트에는 파일 업로드 서버 기능이 포함되어 있지 않습니다.",
            "코드서명은 적용하지 않았습니다. EXE 해시와 client-manifest.json을 확인하세요.",
            "",
        ]
    )
    return text.encode("utf-8-sig")


def _bundle_files(bundle_path: Path) -> list[tuple[Path, Path]]:
    if not bundle_path.is_dir():
        raise ClientPackageError("Windows 클라이언트 프로그램 폴더를 찾을 수 없습니다.")
    executable = bundle_path / CLIENT_EXECUTABLE_NAME
    if not executable.is_file():
        raise ClientPackageError(f"Windows 클라이언트 실행 파일을 찾을 수 없습니다: {CLIENT_EXECUTABLE_NAME}")

    files: list[tuple[Path, Path]] = []
    for path in sorted(bundle_path.rglob("*")):
        if path.is_symlink():
            raise ClientPackageError("클라이언트 프로그램 폴더에는 심볼릭 링크를 포함할 수 없습니다.")
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_path)
        lowered = relative.as_posix().lower()
        if relative.name in {CLIENT_CONFIG_NAME, CLIENT_MANIFEST_NAME, CLIENT_README_NAME}:
            raise ClientPackageError(f"클라이언트 프로그램 폴더에 예약 파일이 있습니다: {relative.name}")
        if lowered.endswith(".cmd") or lowered.endswith("config.ini"):
            raise ClientPackageError(f"클라이언트 프로그램 폴더에 허용되지 않는 파일이 있습니다: {relative}")
        if relative.name.casefold() == "internaluploadserver.exe".casefold():
            raise ClientPackageError("클라이언트 프로그램 폴더에 서버 실행 파일이 포함되어 있습니다.")
        if relative.suffix.casefold() == ".exe" and relative.as_posix() != CLIENT_EXECUTABLE_NAME:
            raise ClientPackageError(f"클라이언트 프로그램 폴더에 추가 실행 파일이 있습니다: {relative}")
        if relative.as_posix() != CLIENT_EXECUTABLE_NAME and relative.parts[0] != "_internal":
            raise ClientPackageError(f"클라이언트 프로그램 폴더에 예상하지 않은 파일이 있습니다: {relative}")
        files.append((path, relative))
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _manifest_entry(path: str, payload: bytes) -> dict[str, object]:
    return {"path": path, "size": len(payload), "sha256": _sha256_bytes(payload)}


def client_executable_sha256(bundle_path: Path) -> str:
    _bundle_files(bundle_path)
    return _sha256_file(bundle_path / CLIENT_EXECUTABLE_NAME)


def verify_client_package(payload: bytes, server_url: str) -> list[str]:
    errors: list[str] = []
    host = _validated_server_host(server_url)
    root_name = f"InternalUpload_Client_{host}"
    prefix = f"{root_name}/"
    required = {
        f"{prefix}{CLIENT_EXECUTABLE_NAME}",
        f"{prefix}{CLIENT_CONFIG_NAME}",
        f"{prefix}{CLIENT_MANIFEST_NAME}",
        f"{prefix}{CLIENT_README_NAME}",
    }
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            file_names = [name for name in archive.namelist() if not name.endswith("/")]
            names = set(file_names)
            if len(file_names) != len(names):
                errors.append("클라이언트 ZIP에 중복 파일 이름이 있습니다.")
            if not required.issubset(names):
                errors.append("클라이언트 ZIP 필수 파일 구성이 올바르지 않습니다.")
            if any(not name.startswith(prefix) for name in names):
                errors.append("클라이언트 ZIP 루트 폴더 구성이 올바르지 않습니다.")
            lowered_names = {name.lower() for name in names}
            if any(name.endswith(".cmd") for name in lowered_names):
                errors.append("클라이언트 ZIP에 CMD 실행 파일이 포함되어 있습니다.")
            if any(name.endswith("config.ini") for name in lowered_names):
                errors.append("클라이언트 ZIP에 서버 config.ini가 포함되어 있습니다.")
            if any(name.endswith("internaluploadserver.exe") for name in lowered_names):
                errors.append("클라이언트 ZIP에 서버 실행 파일이 포함되어 있습니다.")

            config_name = f"{prefix}{CLIENT_CONFIG_NAME}"
            manifest_name = f"{prefix}{CLIENT_MANIFEST_NAME}"
            executable_name = f"{prefix}{CLIENT_EXECUTABLE_NAME}"
            readme_name = f"{prefix}{CLIENT_README_NAME}"
            if config_name in names:
                config = json.loads(archive.read(config_name).decode("utf-8"))
                enrollment_token = (
                    config.get("enrollment_token", "")
                    if isinstance(config, dict)
                    else ""
                )
                valid_enrollment = enrollment_token == "" or (
                    isinstance(enrollment_token, str)
                    and enrollment_token.startswith(ENROLLMENT_TOKEN_PREFIX)
                    and len(enrollment_token) >= len(ENROLLMENT_TOKEN_PREFIX) + 32
                )
                if not isinstance(config, dict) or config != {
                    "schema_version": 2,
                    "server_url": server_url,
                    "client_version": APP_VERSION,
                    "enrollment_token": enrollment_token,
                } or not valid_enrollment:
                    errors.append("클라이언트 자동 연결 설정이 올바르지 않습니다.")
                if any(
                    key in config
                    for key in (
                        "token",
                        "access_token",
                        "agent_token",
                        "session_token",
                        "tcp_hmac_key",
                    )
                ):
                    errors.append("클라이언트 자동 연결 설정에 장기 인증 정보가 포함되어 있습니다.")
            if manifest_name in names and executable_name in names:
                manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
                actual_hash = hashlib.sha256(archive.read(executable_name)).hexdigest()
                if not isinstance(manifest, dict):
                    errors.append("클라이언트 매니페스트 형식이 올바르지 않습니다.")
                    manifest = {}
                if manifest.get("executable_sha256") != actual_hash:
                    errors.append("클라이언트 실행 파일 SHA256이 매니페스트와 다릅니다.")
                if manifest.get("executable") != CLIENT_EXECUTABLE_NAME or manifest.get("version") != APP_VERSION:
                    errors.append("클라이언트 매니페스트 정보가 올바르지 않습니다.")
                manifest_files = manifest.get("files")
                manifest_by_path: dict[str, dict[str, object]] = {}
                if not isinstance(manifest_files, list):
                    errors.append("클라이언트 매니페스트 파일 목록이 없습니다.")
                else:
                    for item in manifest_files:
                        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                            errors.append("클라이언트 매니페스트 파일 항목이 올바르지 않습니다.")
                            continue
                        relative = str(item["path"])
                        parsed = PurePosixPath(relative)
                        if parsed.is_absolute() or ".." in parsed.parts or "\\" in relative:
                            errors.append("클라이언트 매니페스트 파일 경로가 안전하지 않습니다.")
                            continue
                        if relative in manifest_by_path:
                            errors.append("클라이언트 매니페스트에 중복 파일 경로가 있습니다.")
                            continue
                        manifest_by_path[relative] = item

                    expected_relative = {
                        name[len(prefix):]
                        for name in names
                        if name != manifest_name
                    }
                    if set(manifest_by_path) != expected_relative:
                        errors.append("클라이언트 매니페스트 파일 목록이 ZIP과 다릅니다.")
                    for relative, item in manifest_by_path.items():
                        archive_name = f"{prefix}{relative}"
                        if archive_name not in names:
                            continue
                        content = archive.read(archive_name)
                        if item.get("size") != len(content) or item.get("sha256") != _sha256_bytes(content):
                            errors.append(f"클라이언트 파일 해시가 일치하지 않습니다: {relative}")
            if readme_name in names and server_url not in archive.read(readme_name).decode("utf-8-sig"):
                errors.append("클라이언트 안내문에 서버 주소가 없습니다.")
    except (OSError, TypeError, ValueError, KeyError, AttributeError):
        errors.append(
            "클라이언트 ZIP을 확인할 수 없습니다. 오류 코드: "
            "CLIENT_PACKAGE_VERIFY_FAILED."
        )
    return errors


def build_client_package(
    bundle_path: Path,
    server_url: str,
    *,
    enrollment_token: str = "",
) -> ClientPackage:
    host = _validated_server_host(server_url)
    files = _bundle_files(bundle_path)
    executable_sha256 = client_executable_sha256(bundle_path)
    root_name = f"InternalUpload_Client_{host}"
    config_payload = _json_bytes(
        {
            "schema_version": 2,
            "server_url": server_url,
            "client_version": APP_VERSION,
            "enrollment_token": enrollment_token,
        }
    )
    readme_payload = _client_readme(server_url, executable_sha256)
    manifest_files = [
        {
            "path": relative.as_posix(),
            "size": source.stat().st_size,
            "sha256": _sha256_file(source),
        }
        for source, relative in files
    ]
    manifest_files.extend(
        [
            _manifest_entry(CLIENT_CONFIG_NAME, config_payload),
            _manifest_entry(CLIENT_README_NAME, readme_payload),
        ]
    )
    manifest_payload = _json_bytes(
        {
            "schema_version": 1,
            "product": "NetworkProbeClient",
            "version": APP_VERSION,
            "executable": CLIENT_EXECUTABLE_NAME,
            "executable_sha256": executable_sha256,
            "files": manifest_files,
        }
    )
    output = io.BytesIO()
    try:
        with ZipFile(output, mode="w", compression=ZIP_DEFLATED, allowZip64=True) as archive:
            for source, relative in files:
                archive.write(source, f"{root_name}/{relative.as_posix()}")
            archive.writestr(
                f"{root_name}/{CLIENT_CONFIG_NAME}",
                config_payload,
            )
            archive.writestr(
                f"{root_name}/{CLIENT_MANIFEST_NAME}",
                manifest_payload,
            )
            archive.writestr(
                f"{root_name}/{CLIENT_README_NAME}",
                readme_payload,
            )
    except OSError:
        raise ClientPackageError(
            "Windows 클라이언트 ZIP 생성에 실패했습니다. 오류 코드: "
            "CLIENT_PACKAGE_BUILD_FAILED. 서버 저장 공간과 파일 접근 권한을 "
            "확인하세요."
        ) from None

    payload = output.getvalue()
    errors = verify_client_package(payload, server_url)
    if errors:
        raise ClientPackageError(errors[0])
    return ClientPackage(
        payload=payload,
        server_url=server_url,
        download_name=f"internal-upload-client_{host}.zip",
        root_name=root_name,
        client_executable_sha256=executable_sha256,
    )
