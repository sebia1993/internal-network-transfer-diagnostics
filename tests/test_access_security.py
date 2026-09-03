from __future__ import annotations

import re
from pathlib import Path

from access_security import EnrollmentTokenStore
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
                # Legacy web-token settings are intentionally retained here to
                # verify that existing config files remain compatible but no
                # access-token file is created or used.
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


def test_non_loopback_web_access_is_open_and_csrf_protected(tmp_path):
    app = create_app(write_config(tmp_path))
    app.config.update(TESTING=True)
    client = app.test_client()

    token_path = tmp_path / "data/.internal-transfer-access-token"
    assert not token_path.exists()

    index = client.get("/", environ_base=remote())
    api_response = client.get("/api/health", environ_base=remote())
    old_login = client.get("/login", environ_base=remote())

    assert index.status_code == 200
    assert api_response.status_code == 200
    assert old_login.status_code == 302
    assert old_login.location == "/"
    assert not token_path.exists()

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


def test_all_interfaces_binding_allows_remote_web_without_access_token(tmp_path):
    config_path = write_config(tmp_path)
    assert "HOST=0.0.0.0" in config_path.read_text(encoding="utf-8")
    app = create_app(config_path)
    app.config.update(TESTING=True)
    client = app.test_client()

    page_response = client.get("/", environ_base=remote())
    api_response = client.get("/api/health", environ_base=remote())

    assert page_response.status_code == 200
    assert api_response.status_code == 200
    assert not (tmp_path / "data/.internal-transfer-access-token").exists()


def test_legacy_access_token_environment_variable_is_ignored(
    tmp_path,
    monkeypatch,
):
    secret = "S" * 48
    monkeypatch.setenv("INTERNAL_TRANSFER_ACCESS_TOKEN", secret)
    app = create_app(write_config(tmp_path))
    app.config.update(TESTING=True)

    response = app.test_client().get("/", environ_base=remote())

    assert response.status_code == 200
    assert secret not in response.get_data(as_text=True)
    assert not (tmp_path / "data/.internal-transfer-access-token").exists()


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


def test_smoke_check_does_not_create_legacy_access_token_file(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "S" * 48
    monkeypatch.setenv("INTERNAL_TRANSFER_ACCESS_TOKEN", secret)

    assert app_main(["--smoke-check", "--config", str(write_config(tmp_path))]) == 0
    captured = capsys.readouterr()

    assert secret not in captured.out
    assert secret not in captured.err
    assert not (tmp_path / "data/.internal-transfer-access-token").exists()
