from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from access_security import (
    ACCESS_TOKEN_ENV,
    AccessSecurityError,
    EnrollmentTokenStore,
    load_or_create_access_token,
)
from app import create_app, main as app_main
from app_version import APP_VERSION
from network_probe.models import PROBE_PROTOCOL_VERSION


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.ini"
    path.write_text(
        "\n".join(
            [
                "[app]",
                "CONFIG_VERSION=3",
                "HOST=0.0.0.0",
                "PORT=8000",
                "STORAGE_ROOT=uploads",
                "DELETE_ALLOWED_IPS=127.0.0.1,::1",
                "RECENT_LIMIT=50",
                "",
                "[network_probe]",
                "ENABLED=false",
                "PORT=5201",
                "",
                "[security]",
                "ACCESS_TOKEN_FILE=data/.internal-transfer-access-token",
                "SESSION_TTL_MINUTES=480",
                "ENROLLMENT_TOKEN_TTL_SECONDS=300",
            ]
        ),
        encoding="utf-8",
    )
    return path


def remote() -> dict[str, str]:
    return {"REMOTE_ADDR": "10.20.30.40"}


def test_non_loopback_requires_login_or_master_bearer_and_csrf(tmp_path):
    app = create_app(write_config(tmp_path))
    app.config.update(TESTING=True)
    client = app.test_client()
    token = (tmp_path / "data/.internal-transfer-access-token").read_text(encoding="ascii")

    redirect_response = client.get("/", environ_base=remote())
    api_response = client.get("/api/health", environ_base=remote())
    bearer_response = client.get(
        "/api/health",
        headers={"Authorization": f"Bearer {token}"},
        environ_base=remote(),
    )
    bearer_write_response = client.post(
        "/api/network-probe/sessions",
        json={},
        headers={"Authorization": f"Bearer {token}"},
        environ_base=remote(),
    )

    assert redirect_response.status_code == 302
    assert redirect_response.location == "/login"
    assert api_response.status_code == 401
    assert bearer_response.status_code == 200
    assert bearer_write_response.status_code == 503

    login_page = client.get("/login", environ_base=remote())
    csrf = re.search(r'name="_csrf_token" value="([^"]+)"', login_page.get_data(as_text=True))
    assert csrf is not None
    logged_in = client.post(
        "/login",
        data={"access_token": token, "_csrf_token": csrf.group(1)},
        environ_base=remote(),
    )
    assert logged_in.status_code == 302
    index = client.get("/", environ_base=remote())
    assert index.status_code == 200
    assert token not in index.get_data(as_text=True)

    missing_csrf = client.post(
        "/api/network-probe/sessions",
        json={},
        environ_base=remote(),
    )
    assert missing_csrf.status_code == 403
    page_csrf = re.search(
        r'<meta name="csrf-token" content="([^"]+)"',
        index.get_data(as_text=True),
    )
    assert page_csrf is not None
    verified = client.post(
        "/api/network-probe/sessions",
        json={},
        headers={"X-CSRF-Token": page_csrf.group(1)},
        environ_base=remote(),
    )
    assert verified.status_code == 503


def test_loopback_keeps_no_auth_compatibility(tmp_path):
    app = create_app(write_config(tmp_path))
    app.config.update(TESTING=True)
    response = app.test_client().get("/api/health")
    assert response.status_code == 200


def test_all_interfaces_binding_does_not_bypass_remote_authentication(tmp_path):
    config_path = write_config(tmp_path)
    assert "HOST=0.0.0.0" in config_path.read_text(encoding="utf-8")
    app = create_app(config_path)
    app.config.update(TESTING=True)
    client = app.test_client()

    page_response = client.get("/", environ_base=remote())
    api_response = client.get("/api/health", environ_base=remote())

    assert page_response.status_code == 302
    assert page_response.location == "/login"
    assert api_response.status_code == 401


def test_generated_token_file_is_private_and_environment_avoids_file(tmp_path, monkeypatch):
    token, path = load_or_create_access_token(tmp_path)
    assert path is not None
    assert len(token) >= 32
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    alternate = tmp_path / "env-only-token"
    monkeypatch.setenv(ACCESS_TOKEN_ENV, "x" * 48)
    environment_token, environment_path = load_or_create_access_token(
        tmp_path,
        str(alternate),
    )
    assert environment_token == "x" * 48
    assert environment_path is None
    assert not alternate.exists()


def test_existing_overpermissive_token_file_fails_closed(tmp_path):
    path = tmp_path / "token"
    path.write_text("x" * 48, encoding="ascii")
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not available on Windows")
    path.chmod(0o644)
    with pytest.raises(AccessSecurityError, match="0600"):
        load_or_create_access_token(tmp_path, str(path), environ={})


def test_symlink_token_file_fails_closed(tmp_path):
    target = tmp_path / "target-token"
    target.write_text("x" * 48, encoding="ascii")
    target.chmod(0o600)
    link = tmp_path / "token-link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(AccessSecurityError, match="심볼릭 링크"):
        load_or_create_access_token(tmp_path, str(link), environ={})


def test_enrollment_token_is_bounded_expiring_and_single_use():
    now = [100.0]
    store = EnrollmentTokenStore(ttl_seconds=30, max_tokens=2, clock=lambda: now[0])
    first = store.issue()
    assert store.consume(first) is True
    assert store.consume(first) is False
    expired = store.issue()
    now[0] += 31
    assert store.consume(expired) is False


def test_remote_agent_registration_consumes_enrollment_before_service(tmp_path):
    app = create_app(write_config(tmp_path))
    app.config.update(TESTING=True)
    client = app.test_client()
    security = app.extensions["access_security"]
    enrollment = security.issue_enrollment_token()
    payload = {
        "agent_id": "a" * 32,
        "hostname": "SYNTHETIC-PC",
        "server_host": "192.0.2.10",
        "protocol_version": PROBE_PROTOCOL_VERSION,
        "client_version": APP_VERSION,
    }

    first = client.post(
        "/api/network-probe/agents/register",
        json=payload,
        headers={"Authorization": f"Bearer {enrollment}"},
        environ_base=remote(),
    )
    replay = client.post(
        "/api/network-probe/agents/register",
        json=payload,
        headers={"Authorization": f"Bearer {enrollment}"},
        environ_base=remote(),
    )

    assert first.status_code == 503
    assert replay.status_code == 401
    assert "이미 사용" in replay.get_json()["error"]


def test_access_token_is_not_printed_by_smoke_check(tmp_path, monkeypatch, capsys):
    secret = "S" * 48
    monkeypatch.setenv(ACCESS_TOKEN_ENV, secret)
    assert app_main(["--smoke-check", "--config", str(write_config(tmp_path))]) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
