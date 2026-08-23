from __future__ import annotations

import json

import pytest

from app_version import APP_VERSION
from network_probe.agent import ProbeClientError
from probe_client import load_server_url, main


def test_probe_client_loads_validated_adjacent_json_config(tmp_path):
    config = tmp_path / "client-config.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "server_url": "http://SERVER-PC:8123",
                "client_version": APP_VERSION,
                "enrollment_token": "enr_v1_" + ("x" * 44),
            }
        ),
        encoding="utf-8",
    )

    assert load_server_url(config) == "http://server-pc:8123"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": 2, "server_url": "http://server-pc:8000", "client_version": APP_VERSION},
        {"schema_version": 2, "server_url": "http://server-pc:8000", "client_version": "old", "enrollment_token": "enr_v1_" + ("x" * 44)},
        {"schema_version": 2, "server_url": "https://server-pc:8000", "client_version": APP_VERSION, "enrollment_token": "enr_v1_" + ("x" * 44)},
    ],
)
def test_probe_client_rejects_invalid_or_mismatched_config(tmp_path, payload):
    config = tmp_path / "client-config.json"
    config.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProbeClientError):
        load_server_url(config)


def test_probe_client_self_check_does_not_require_server_config(capsys):
    assert main(["--self-check"]) == 0
    assert "self-check passed" in capsys.readouterr().out


def test_probe_client_does_not_accept_arbitrary_server_argument():
    with pytest.raises(SystemExit):
        main(["--server", "http://other-server:8000"])
