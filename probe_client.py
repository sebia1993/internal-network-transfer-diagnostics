from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app_version import APP_VERSION
from network_probe.agent import ProbeClientError, normalize_server_url, run_probe_client
from access_security import ENROLLMENT_TOKEN_PREFIX
from network_probe.client_package import CLIENT_CONFIG_NAME


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_client_config(config_path: str | Path) -> tuple[str, str]:
    path = Path(config_path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProbeClientError(
            f"자동 연결 설정을 찾을 수 없습니다: {path.name}. 서버 웹 화면에서 클라이언트 ZIP을 다시 받으세요."
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeClientError(f"자동 연결 설정을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise ProbeClientError("자동 연결 설정 형식이 올바르지 않습니다.")
    if payload.get("client_version") != APP_VERSION:
        raise ProbeClientError("서버와 클라이언트 설정 버전이 다릅니다. 클라이언트 ZIP을 다시 받으세요.")
    server_url = str(payload.get("server_url", ""))
    enrollment_token = str(payload.get("enrollment_token", ""))
    if not enrollment_token.startswith(ENROLLMENT_TOKEN_PREFIX) or len(enrollment_token) < 40:
        raise ProbeClientError(
            "클라이언트 등록 토큰이 없거나 올바르지 않습니다. 서버 웹 화면에서 새 ZIP을 받으세요."
        )
    normalized, _ = normalize_server_url(server_url)
    return normalized, enrollment_token


def load_server_url(config_path: str | Path) -> str:
    server_url, _ = load_client_config(config_path)
    return server_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="사내 업로드 TCP 전송 성능 측정 전용 클라이언트")
    parser.add_argument("--config", default="", help="자동 연결 JSON 경로")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        print(f"NetworkProbeClient {APP_VERSION} self-check passed")
        return 0

    try:
        server_url, enrollment_token = load_client_config(
            args.config or runtime_root() / CLIENT_CONFIG_NAME
        )
        return run_probe_client(server_url, enrollment_token)
    except ProbeClientError as exc:
        print(f"TCP 측정 클라이언트 실행 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
