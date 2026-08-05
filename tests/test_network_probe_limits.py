from __future__ import annotations

import copy
import io
import json
import uuid

import pytest
from flask import Flask, request

import network_probe.service as service_module
from app_version import APP_VERSION
from network_measurement import NetworkMeasurementGate
from network_probe.models import PROBE_PROTOCOL_VERSION, ProbeConfig
from network_probe.routes import PROBE_JSON_MAX_BYTES, create_probe_blueprint
from network_probe.service import (
    PROBE_INTERVAL_FIELDS,
    PROBE_MAX_RESULT_BYTES,
    PROBE_RESULT_FIELDS,
    PROBE_STREAM_RESULT_FIELDS,
    PROBE_TELEMETRY_FIELDS,
    ProbeService,
    ProbeServiceError,
)


def build_service_and_app(tmp_path, *, clock=None):
    service = ProbeService(
        config=ProbeConfig(
            enabled=True,
            host="127.0.0.1",
            port=39001,
            log_path=tmp_path / "network_probe.csv",
            results_root=tmp_path / "results",
        ),
        measurement_gate=NetworkMeasurementGate(),
        normalize_ip=lambda value: str(value or ""),
        clock=clock or (lambda: 0.0),
    )
    service.started = True
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(create_probe_blueprint(service))
    return service, app


def registration_payload(agent_id: str | None = None, **extra):
    return {
        "agent_id": agent_id or uuid.uuid4().hex,
        "hostname": "TEST-PC",
        "server_host": "127.0.0.1",
        "protocol_version": PROBE_PROTOCOL_VERSION,
        "client_version": APP_VERSION,
        **extra,
    }


def valid_side_result() -> dict:
    interval_bytes = [100] * 9 + [124]
    intervals = [
        {
            "index": index,
            "bytes": byte_count,
            "mbps": round(byte_count * 8 / 1_000_000, 2),
        }
        for index, byte_count in enumerate(interval_bytes, start=1)
    ]
    unavailable = {"available": False, "error": "not available"}
    return {
        "role": "sender",
        "bytes": 1024,
        "duration_seconds": 10,
        "average_mbps": 999.0,
        "median_mbps": 999.0,
        "min_mbps": 999.0,
        "max_mbps": 999.0,
        "intervals": intervals,
        "streams": [
            {
                "stream_id": 0,
                "role": "sender",
                "bytes": 1024,
                "duration_seconds": 10.0,
                "mbps": 999.0,
                "interval_bytes": interval_bytes,
                "telemetry": dict(unavailable),
            }
        ],
        "telemetry": dict(unavailable),
    }


def test_probe_json_rejects_declared_body_over_64_kib(tmp_path):
    _service, app = build_service_and_app(tmp_path)
    marker = "raw-body-secret-marker"
    body = json.dumps(
        registration_payload(padding=marker + ("x" * PROBE_JSON_MAX_BYTES)),
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(body) > PROBE_JSON_MAX_BYTES

    response = app.test_client().post(
        "/api/network-probe/agents/register",
        data=body,
        content_type="application/json",
    )

    assert response.status_code == 413
    assert "64 KiB" in response.get_json()["error"]
    assert marker not in response.get_data(as_text=True)


def test_probe_json_rejects_actual_chunked_body_over_64_kib(tmp_path):
    _service, app = build_service_and_app(tmp_path)
    observed_content_lengths: list[int | None] = []

    @app.before_request
    def record_content_length():
        observed_content_lengths.append(request.content_length)

    body = json.dumps(
        registration_payload(padding="x" * PROBE_JSON_MAX_BYTES),
        separators=(",", ":"),
    ).encode("utf-8")
    response = app.test_client().open(
        "/api/network-probe/agents/register",
        method="POST",
        input_stream=io.BytesIO(body),
        content_type="application/json",
        environ_overrides={
            "CONTENT_LENGTH": "",
            "HTTP_TRANSFER_ENCODING": "chunked",
            "wsgi.input_terminated": True,
        },
    )

    assert observed_content_lengths == [None]
    assert response.status_code == 413
    assert "64 KiB" in response.get_json()["error"]


def test_probe_agent_registry_cap_allows_refresh_and_cleans_expired_agents(
    tmp_path,
    monkeypatch,
):
    now = [0.0]
    service, app = build_service_and_app(tmp_path, clock=lambda: now[0])
    monkeypatch.setattr(service_module, "PROBE_MAX_REGISTERED_AGENTS", 2)
    client = app.test_client()
    first_id = "1" * 32
    second_id = "2" * 32
    third_id = "3" * 32

    first = client.post(
        "/api/network-probe/agents/register",
        json=registration_payload(first_id),
    )
    second = client.post(
        "/api/network-probe/agents/register",
        json=registration_payload(second_id),
    )
    blocked = client.post(
        "/api/network-probe/agents/register",
        json=registration_payload(third_id),
    )
    refreshed = client.post(
        "/api/network-probe/agents/register",
        json=registration_payload(first_id),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert "agent_token" not in blocked.get_data(as_text=True)
    assert refreshed.status_code == 200

    now[0] = service.config.agent_ttl_seconds + 0.1
    after_expiry = client.post(
        "/api/network-probe/agents/register",
        json=registration_payload(third_id),
    )

    assert after_expiry.status_code == 200
    assert set(service.agents) == {third_id}


def test_probe_result_is_rebuilt_from_allowed_fields_and_bounded_values(tmp_path):
    service, _app = build_service_and_app(tmp_path)

    sanitized = service._validated_side_result(
        valid_side_result(),
        "upload",
        expected_duration_seconds=10,
        expected_stream_count=1,
    )

    assert set(sanitized) == set(PROBE_RESULT_FIELDS)
    assert set(sanitized["streams"][0]) == set(PROBE_STREAM_RESULT_FIELDS)
    assert set(sanitized["intervals"][0]) == set(PROBE_INTERVAL_FIELDS)
    assert set(sanitized["telemetry"]) <= set(PROBE_TELEMETRY_FIELDS)
    assert set(sanitized["streams"][0]["telemetry"]) <= set(PROBE_TELEMETRY_FIELDS)
    assert sanitized["average_mbps"] == pytest.approx(0.0, abs=0.01)
    assert sanitized["streams"][0]["mbps"] == pytest.approx(0.0, abs=0.01)


@pytest.mark.parametrize(
    "target_path",
    [
        (),
        ("streams", 0),
        ("intervals", 0),
        ("telemetry",),
        ("streams", 0, "telemetry"),
    ],
)
def test_probe_result_rejects_fields_outside_allowlist(tmp_path, target_path):
    service, _app = build_service_and_app(tmp_path)
    result = valid_side_result()
    target = result
    for key in target_path:
        target = target[key]
    target["unexpected"] = "not stored"

    with pytest.raises(ProbeServiceError, match="허용되지 않은 항목"):
        service._validated_side_result(
            result,
            "upload",
            expected_duration_seconds=10,
            expected_stream_count=1,
        )


def test_probe_result_rejects_oversized_collections_and_numeric_values(tmp_path):
    service, _app = build_service_and_app(tmp_path)
    invalid_results = []

    too_many_intervals = valid_side_result()
    too_many_intervals["intervals"].append({"index": 11, "bytes": 0, "mbps": 0.0})
    invalid_results.append(too_many_intervals)

    too_many_stream_intervals = valid_side_result()
    too_many_stream_intervals["streams"][0]["interval_bytes"].append(0)
    invalid_results.append(too_many_stream_intervals)

    empty_intervals = valid_side_result()
    empty_intervals["intervals"] = []
    invalid_results.append(empty_intervals)

    short_intervals = valid_side_result()
    short_intervals["intervals"].pop()
    invalid_results.append(short_intervals)

    empty_stream_intervals = valid_side_result()
    empty_stream_intervals["streams"][0]["interval_bytes"] = []
    invalid_results.append(empty_stream_intervals)

    short_stream_intervals = valid_side_result()
    short_stream_intervals["streams"][0]["interval_bytes"].pop()
    invalid_results.append(short_stream_intervals)

    implausibly_short_stream = valid_side_result()
    implausibly_short_stream["streams"][0]["duration_seconds"] = 0.0001
    invalid_results.append(implausibly_short_stream)

    long_telemetry_error = valid_side_result()
    long_telemetry_error["telemetry"]["error"] = "x" * 501
    invalid_results.append(long_telemetry_error)

    oversized_bytes = valid_side_result()
    oversized_bytes["bytes"] = PROBE_MAX_RESULT_BYTES + 1
    invalid_results.append(oversized_bytes)

    non_finite_metric = valid_side_result()
    non_finite_metric["average_mbps"] = float("nan")
    invalid_results.append(non_finite_metric)

    for result in invalid_results:
        with pytest.raises(ProbeServiceError):
            service._validated_side_result(
                result,
                "upload",
                expected_duration_seconds=10,
                expected_stream_count=1,
            )
