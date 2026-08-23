from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import sys
import tempfile
from configparser import ConfigParser, Error as ConfigParserError, SectionProxy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import SplitResult, urlsplit, urlunsplit

from runtime_stability import durable_replace


APP_ID = "internal-upload"
APP_SECTION = "app"
PROBE_SECTION = "network_probe"
SECURITY_SECTION = "security"
CURRENT_CONFIG_VERSION = 3
PORT_SEARCH_LIMIT = 99
DEFAULT_APP_SETTINGS = {
    "CONFIG_VERSION": str(CURRENT_CONFIG_VERSION),
    "HOST": "0.0.0.0",
    "PORT": "8000",
    "BASE_URL": "",
    "STORAGE_ROOT": "uploads",
    "DELETE_ALLOWED_IPS": "127.0.0.1,::1",
    "RECENT_LIMIT": "50",
}
DEFAULT_PROBE_SETTINGS = {
    "ENABLED": "true",
    "PORT": "5201",
}
DEFAULT_SECURITY_SETTINGS = {
    "ACCESS_TOKEN_FILE": "data/.internal-transfer-access-token",
    "SESSION_TTL_MINUTES": "480",
    "ENROLLMENT_TOKEN_TTL_SECONDS": "300",
}
CONFIG_VALUE_GUIDANCE = (
    "주요 키 허용값: [app] PORT=1~65535, RECENT_LIMIT=1~10000, "
    "BASE_URL=비어 있음 또는 http(s) URL; [network_probe] "
    "ENABLED=true/false, yes/no, on/off, 1/0, PORT=1~65535; [security] "
    "ACCESS_TOKEN_FILE=비어 있지 않은 파일 경로, SESSION_TTL_MINUTES=5~1440, "
    "ENROLLMENT_TOKEN_TTL_SECONDS=30~3600."
)

FIREWALL_ALLOWED = "allowed"
FIREWALL_NOT_FOUND = "not_found"
FIREWALL_UNKNOWN = "unknown"
FIREWALL_NOT_APPLICABLE = "not_applicable"


class StartupPortError(RuntimeError):
    pass


class ConfigFileError(StartupPortError):
    pass


class PortChangeDeclined(StartupPortError):
    pass


@dataclass(frozen=True)
class PortResolution:
    configured_port: int
    selected_port: int
    existing_instance: bool = False

    @property
    def changed(self) -> bool:
        return self.configured_port != self.selected_port


@dataclass(frozen=True)
class ConfigUpdateResult:
    base_url_changed: bool
    warning: str = ""


@dataclass(frozen=True)
class ConfigMigrationResult:
    previous_version: int
    current_version: int
    probe_enabled_changed: bool


def is_port_available(host: str, port: int) -> bool:
    if not 1 <= port <= 65535:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def is_existing_instance(
    port: int,
    timeout_seconds: float = 0.5,
    *,
    host: str = "127.0.0.1",
) -> bool:
    target_host = "127.0.0.1" if host == "0.0.0.0" else host
    connection = http.client.HTTPConnection(target_host, port, timeout=timeout_seconds)
    try:
        connection.request("GET", "/api/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        payload = json.loads(response.read(4097).decode("utf-8"))
        return (
            payload.get("app") == APP_ID
            and payload.get("status") in {"ok", "degraded"}
            and payload.get("port") == port
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        connection.close()


def find_available_port(
    host: str,
    configured_port: int,
    *,
    excluded_ports: set[int] | frozenset[int] | None = None,
    search_limit: int = PORT_SEARCH_LIMIT,
    availability_check: Callable[[str, int], bool] = is_port_available,
) -> int | None:
    excluded = excluded_ports or set()
    upper_port = min(65535, configured_port + max(0, search_limit))
    for candidate in range(configured_port + 1, upper_port + 1):
        if candidate in excluded:
            continue
        if availability_check(host, candidate):
            return candidate
    return None


def prompt_for_port_change(
    configured_port: int,
    selected_port: int,
    *,
    input_func: Callable[[str], str] | None = None,
    output_func: Callable[[str], None] = print,
    interactive: bool | None = None,
) -> bool:
    if input_func is None:
        if interactive is None:
            interactive = bool(sys.stdin and sys.stdin.isatty())
        if not interactive:
            output_func("입력할 수 없는 실행 환경이므로 웹 포트를 자동으로 변경하지 않습니다.")
            return False
        input_func = input

    prompt = (
        f"웹 포트 {configured_port}이(가) 사용 중입니다. "
        f"사용 가능한 {selected_port}(으)로 변경할까요? [Y/n]: "
    )
    while True:
        try:
            answer = input_func(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            output_func("웹 포트 변경을 취소했습니다.")
            return False
        if answer in {"", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        output_func("Y 또는 N을 입력하세요. Enter는 Y로 처리됩니다.")


def resolve_startup_port(
    host: str,
    configured_port: int,
    *,
    excluded_ports: set[int] | frozenset[int] | None = None,
    availability_check: Callable[[str, int], bool] = is_port_available,
    existing_instance_check: Callable[[int], bool] = is_existing_instance,
    confirm_change: Callable[[int, int], bool] = prompt_for_port_change,
) -> PortResolution:
    if availability_check(host, configured_port):
        return PortResolution(configured_port, configured_port)
    if existing_instance_check(configured_port):
        return PortResolution(configured_port, configured_port, existing_instance=True)

    selected_port = find_available_port(
        host,
        configured_port,
        excluded_ports=excluded_ports,
        availability_check=availability_check,
    )
    if selected_port is None:
        upper = min(65535, configured_port + PORT_SEARCH_LIMIT)
        raise StartupPortError(
            f"웹 포트 {configured_port}부터 {upper} 사이에서 사용할 수 있는 포트를 찾지 못했습니다."
        )
    if not confirm_change(configured_port, selected_port):
        raise PortChangeDeclined("사용자가 웹 포트 변경을 취소했습니다.")
    return PortResolution(configured_port, selected_port)


def prompt_for_probe_port_change(
    configured_port: int,
    selected_port: int,
    *,
    input_func: Callable[[str], str] | None = None,
    output_func: Callable[[str], None] = print,
    interactive: bool | None = None,
) -> bool:
    if input_func is None:
        if interactive is None:
            interactive = bool(sys.stdin and sys.stdin.isatty())
        if not interactive:
            output_func("입력할 수 없는 실행 환경이므로 TCP 측정 포트를 자동으로 변경하지 않습니다.")
            return False
        input_func = input

    prompt = (
        f"TCP 전송 성능 측정 포트 {configured_port}이(가) 사용 중입니다. "
        f"사용 가능한 {selected_port}(으)로 변경할까요? [Y/n]: "
    )
    while True:
        try:
            answer = input_func(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            output_func("TCP 측정 포트 변경을 취소했습니다.")
            return False
        if answer in {"", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        output_func("Y 또는 N을 입력하세요. Enter는 Y로 처리됩니다.")


def resolve_probe_port(
    host: str,
    configured_port: int,
    *,
    excluded_ports: set[int] | frozenset[int] | None = None,
    availability_check: Callable[[str, int], bool] = is_port_available,
    confirm_change: Callable[[int, int], bool] = prompt_for_probe_port_change,
) -> PortResolution:
    excluded = excluded_ports or set()
    if configured_port not in excluded and availability_check(host, configured_port):
        return PortResolution(configured_port, configured_port)

    selected_port = find_available_port(
        host,
        configured_port,
        excluded_ports=excluded,
        availability_check=availability_check,
    )
    if selected_port is None:
        upper = min(65535, configured_port + PORT_SEARCH_LIMIT)
        raise StartupPortError(
            f"TCP 측정 포트 {configured_port}부터 {upper} 사이에서 사용할 수 있는 포트를 찾지 못했습니다."
        )
    if not confirm_change(configured_port, selected_port):
        raise PortChangeDeclined("사용자가 TCP 측정 포트 변경을 취소했습니다.")
    return PortResolution(configured_port, selected_port)


def _find_option(section: SectionProxy, option: str) -> str | None:
    expected = option.casefold()
    return next((name for name in section if name.casefold() == expected), None)


def _get_option(section: SectionProxy, option: str, fallback: str = "") -> str:
    existing = _find_option(section, option)
    return section[existing] if existing is not None else fallback


def _set_option(section: SectionProxy, option: str, value: str) -> None:
    existing = _find_option(section, option)
    section[existing or option] = value


def _ensure_defaults(section: SectionProxy, defaults: dict[str, str]) -> None:
    for option, value in defaults.items():
        if _find_option(section, option) is None:
            section[option] = value


def _raise_invalid_config_value(
    path: Path,
    section: str,
    option: str,
    allowed: str,
) -> None:
    raise ConfigFileError(
        "설정값이 올바르지 않습니다. "
        f"키: [{section}] {option}. 허용값: {allowed}. 설정 파일: {path.name}"
    )


def _validate_integer_option(
    parser: ConfigParser,
    path: Path,
    section_name: str,
    option: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    if not parser.has_section(section_name):
        return
    section = parser[section_name]
    existing = _find_option(section, option)
    if existing is None:
        return
    raw_value = section[existing].strip()
    allowed = (
        f"{minimum}~{maximum} 사이의 정수"
        if maximum is not None
        else f"{minimum} 이상의 정수"
    )
    try:
        value = int(raw_value)
    except ValueError:
        _raise_invalid_config_value(path, section_name, option, allowed)
    if value < minimum or (maximum is not None and value > maximum):
        _raise_invalid_config_value(path, section_name, option, allowed)


def _validate_boolean_option(
    parser: ConfigParser,
    path: Path,
    section_name: str,
    option: str,
) -> None:
    if not parser.has_section(section_name):
        return
    section = parser[section_name]
    existing = _find_option(section, option)
    if existing is None:
        return
    raw_value = section[existing].strip().casefold()
    if raw_value not in ConfigParser.BOOLEAN_STATES:
        _raise_invalid_config_value(
            path,
            section_name,
            option,
            "true/false, yes/no, on/off 또는 1/0",
        )


def _option_value(
    parser: ConfigParser,
    section_name: str,
    option: str,
) -> str | None:
    if not parser.has_section(section_name):
        return None
    section = parser[section_name]
    existing = _find_option(section, option)
    return section[existing].strip() if existing is not None else None


def _validate_host_option(parser: ConfigParser, path: Path) -> None:
    value = _option_value(parser, APP_SECTION, "HOST")
    if value is None or not value:
        return
    allowed = "IPv4 주소 또는 포트가 없는 호스트 이름"
    if (
        len(value) > 253
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(token in value for token in ("://", "/", "\\", "[", "]"))
        or ":" in value
    ):
        _raise_invalid_config_value(path, APP_SECTION, "HOST", allowed)
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError:
        _raise_invalid_config_value(path, APP_SECTION, "HOST", allowed)
    labels = ascii_value.split(".")
    if (
        any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(
                not (character.isalnum() or character in {"-", "_"})
                for character in label
            )
            for label in labels
        )
    ):
        _raise_invalid_config_value(path, APP_SECTION, "HOST", allowed)
    if all(character.isdigit() or character == "." for character in value):
        try:
            ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            _raise_invalid_config_value(path, APP_SECTION, "HOST", allowed)


def _validate_base_url_option(parser: ConfigParser, path: Path) -> None:
    value = _option_value(parser, APP_SECTION, "BASE_URL")
    if value is None or not value:
        return
    allowed = (
        "비어 있음 또는 사용자정보·query·fragment가 없는 http(s) URL "
        "(예: http://server.example:8000)"
    )
    if "\\" in value or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        _raise_invalid_config_value(path, APP_SECTION, "BASE_URL", allowed)
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        _raise_invalid_config_value(path, APP_SECTION, "BASE_URL", allowed)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed_port is not None and not 1 <= parsed_port <= 65535)
    ):
        _raise_invalid_config_value(path, APP_SECTION, "BASE_URL", allowed)


def _validate_storage_root_option(parser: ConfigParser, path: Path) -> None:
    value = _option_value(parser, APP_SECTION, "STORAGE_ROOT")
    if value is None:
        return
    if not value or any(ord(character) < 32 for character in value):
        _raise_invalid_config_value(
            path,
            APP_SECTION,
            "STORAGE_ROOT",
            "비어 있지 않은 폴더 경로",
        )


def _validate_delete_allowed_ips_option(
    parser: ConfigParser,
    path: Path,
) -> None:
    value = _option_value(parser, APP_SECTION, "DELETE_ALLOWED_IPS")
    if value is None or not value:
        return
    allowed = "쉼표로 구분한 개별 IPv4 또는 IPv6 주소(CIDR 제외)"
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        return
    try:
        for item in values:
            ipaddress.ip_address(item)
    except ValueError:
        _raise_invalid_config_value(
            path,
            APP_SECTION,
            "DELETE_ALLOWED_IPS",
            allowed,
        )


def _validate_config_values(parser: ConfigParser, path: Path) -> None:
    _validate_integer_option(
        parser,
        path,
        APP_SECTION,
        "CONFIG_VERSION",
        minimum=0,
        maximum=CURRENT_CONFIG_VERSION,
    )
    _validate_integer_option(
        parser,
        path,
        APP_SECTION,
        "PORT",
        minimum=1,
        maximum=65535,
    )
    token_file = _option_value(parser, SECURITY_SECTION, "ACCESS_TOKEN_FILE")
    if token_file is not None and (
        not token_file or any(ord(character) < 32 for character in token_file)
    ):
        _raise_invalid_config_value(
            path,
            SECURITY_SECTION,
            "ACCESS_TOKEN_FILE",
            "비어 있지 않은 파일 경로",
        )
    _validate_integer_option(
        parser,
        path,
        SECURITY_SECTION,
        "SESSION_TTL_MINUTES",
        minimum=5,
        maximum=1440,
    )
    _validate_integer_option(
        parser,
        path,
        SECURITY_SECTION,
        "ENROLLMENT_TOKEN_TTL_SECONDS",
        minimum=30,
        maximum=3600,
    )
    _validate_integer_option(
        parser,
        path,
        APP_SECTION,
        "RECENT_LIMIT",
        minimum=1,
        maximum=10000,
    )
    _validate_host_option(parser, path)
    _validate_base_url_option(parser, path)
    _validate_storage_root_option(parser, path)
    _validate_delete_allowed_ips_option(parser, path)
    _validate_boolean_option(parser, path, PROBE_SECTION, "ENABLED")
    _validate_integer_option(
        parser,
        path,
        PROBE_SECTION,
        "PORT",
        minimum=1,
        maximum=65535,
    )


def read_config_parser(
    path: Path,
    *,
    preserve_option_case: bool = False,
    ensure_sections: bool = False,
) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    if preserve_option_case:
        parser.optionxform = str
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
    except UnicodeError:
        raise ConfigFileError(
            "설정 파일 인코딩이 올바르지 않습니다. 허용값: UTF-8. "
            f"{CONFIG_VALUE_GUIDANCE} "
            f"설정 파일: {path.name}"
        ) from None
    except ConfigParserError:
        raise ConfigFileError(
            "설정 파일 형식이 올바르지 않습니다. "
            "허용 형식: [app] 및 [network_probe] INI 형식. "
            f"{CONFIG_VALUE_GUIDANCE} "
            f"설정 파일: {path.name}"
        ) from None
    except OSError:
        raise ConfigFileError(
            "설정 파일을 읽을 수 없습니다. 허용 조건: 읽기 가능한 UTF-8 INI 파일. "
            f"{CONFIG_VALUE_GUIDANCE} "
            f"설정 파일: {path.name}"
        ) from None

    _validate_config_values(parser, path)
    if ensure_sections:
        if not parser.has_section(APP_SECTION):
            parser.add_section(APP_SECTION)
        if not parser.has_section(PROBE_SECTION):
            parser.add_section(PROBE_SECTION)
        if not parser.has_section(SECURITY_SECTION):
            parser.add_section(SECURITY_SECTION)
    return parser


def _read_parser(path: Path) -> ConfigParser:
    return read_config_parser(
        path,
        preserve_option_case=True,
        ensure_sections=True,
    )


def _write_parser(path: Path, parser: ConfigParser) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = path.stat().st_mode if path.exists() else None
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            parser.write(handle, space_around_delimiters=False)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        if original_mode is not None:
            os.chmod(temporary_path, original_mode)
        durable_replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def config_requires_probe_enable_migration(config_path: str | os.PathLike[str]) -> bool:
    path = Path(config_path).resolve()
    if not path.exists():
        return False
    parser = _read_parser(path)
    raw_version = _get_option(parser[APP_SECTION], "CONFIG_VERSION", "0")
    try:
        version = int(raw_version)
    except ValueError:
        version = 0
    return version < CURRENT_CONFIG_VERSION


def config_requires_legacy_probe_enable(config_path: str | os.PathLike[str]) -> bool:
    path = Path(config_path).resolve()
    if not path.exists():
        return False
    parser = _read_parser(path)
    raw_version = _get_option(parser[APP_SECTION], "CONFIG_VERSION", "0")
    try:
        version = int(raw_version)
    except ValueError:
        version = 0
    return version < 2


def migrate_config(config_path: str | os.PathLike[str]) -> ConfigMigrationResult:
    path = Path(config_path).resolve()
    parser = _read_parser(path)
    app_section = parser[APP_SECTION]
    probe_section = parser[PROBE_SECTION]
    security_section = parser[SECURITY_SECTION]
    raw_version = _get_option(app_section, "CONFIG_VERSION", "0")
    try:
        previous_version = int(raw_version)
    except ValueError:
        previous_version = 0

    if previous_version >= CURRENT_CONFIG_VERSION:
        return ConfigMigrationResult(previous_version, previous_version, False)

    _ensure_defaults(app_section, DEFAULT_APP_SETTINGS)
    _ensure_defaults(probe_section, DEFAULT_PROBE_SETTINGS)
    _ensure_defaults(security_section, DEFAULT_SECURITY_SETTINGS)
    raw_enabled = _get_option(probe_section, "ENABLED", "true").strip().casefold()
    probe_enabled_changed = previous_version < 2 and raw_enabled not in {
        "1",
        "yes",
        "true",
        "on",
    }
    _set_option(app_section, "CONFIG_VERSION", str(CURRENT_CONFIG_VERSION))
    if previous_version < 2:
        _set_option(probe_section, "ENABLED", "true")
    _write_parser(path, parser)
    return ConfigMigrationResult(previous_version, CURRENT_CONFIG_VERSION, probe_enabled_changed)


def _netloc_with_port(parsed: SplitResult, port: int) -> str:
    user_info = ""
    if "@" in parsed.netloc:
        user_info = parsed.netloc.rsplit("@", 1)[0] + "@"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{user_info}{host}:{port}"


def rewrite_base_url_port(base_url: str, old_port: int, new_port: int) -> tuple[str, str]:
    value = base_url.strip()
    if not value:
        return base_url, ""
    try:
        parsed = urlsplit(value)
        explicit_port = parsed.port
    except ValueError:
        return base_url, "BASE_URL 형식을 확인할 수 없어 기존 값을 유지했습니다."
    if not parsed.scheme or not parsed.hostname:
        return base_url, "BASE_URL 형식을 확인할 수 없어 기존 값을 유지했습니다."

    default_port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    effective_port = explicit_port if explicit_port is not None else default_port
    if effective_port != old_port:
        return (
            base_url,
            f"BASE_URL이 기존 웹 포트 {old_port}을(를) 사용하지 않아 기존 값을 유지했습니다.",
        )

    updated = urlunsplit(parsed._replace(netloc=_netloc_with_port(parsed, new_port)))
    return updated, ""


def persist_port_change(
    config_path: str | os.PathLike[str],
    old_port: int,
    new_port: int,
) -> ConfigUpdateResult:
    path = Path(config_path).resolve()
    parser = _read_parser(path)

    app_section = parser[APP_SECTION]
    probe_section = parser[PROBE_SECTION]
    _ensure_defaults(app_section, DEFAULT_APP_SETTINGS)
    _ensure_defaults(probe_section, DEFAULT_PROBE_SETTINGS)
    _set_option(app_section, "PORT", str(new_port))

    base_url = _get_option(app_section, "BASE_URL")
    updated_base_url, warning = rewrite_base_url_port(base_url, old_port, new_port)
    base_url_changed = updated_base_url != base_url
    if base_url_changed:
        _set_option(app_section, "BASE_URL", updated_base_url)

    _write_parser(path, parser)

    return ConfigUpdateResult(base_url_changed=base_url_changed, warning=warning)


def persist_probe_port_change(
    config_path: str | os.PathLike[str],
    new_port: int,
) -> None:
    if not 1 <= new_port <= 65535:
        raise ValueError("TCP 측정 포트는 1~65535 범위여야 합니다.")
    path = Path(config_path).resolve()
    parser = _read_parser(path)
    app_section = parser[APP_SECTION]
    probe_section = parser[PROBE_SECTION]
    _ensure_defaults(app_section, DEFAULT_APP_SETTINGS)
    _ensure_defaults(probe_section, DEFAULT_PROBE_SETTINGS)
    _set_option(probe_section, "PORT", str(new_port))
    _write_parser(path, parser)


def check_windows_firewall_port(
    port: int,
    *,
    platform: str | None = None,
) -> str:
    active_platform = platform or sys.platform
    if active_platform != "win32":
        return FIREWALL_NOT_APPLICABLE
    if not 1 <= port <= 65535:
        return FIREWALL_UNKNOWN
    return FIREWALL_UNKNOWN


def firewall_add_command(port: int) -> str:
    return (
        'netsh advfirewall firewall add rule '
        f'name="InternalUpload TCP {port}" dir=in action=allow protocol=TCP localport={port}'
    )
