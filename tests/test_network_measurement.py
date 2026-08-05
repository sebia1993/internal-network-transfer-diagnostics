import logging

from network_measurement import NetworkMeasurementGate


def test_measurement_gate_allows_only_one_owner():
    gate = NetworkMeasurementGate()

    assert gate.acquire("http", "one") is True
    assert gate.acquire("http", "one") is True
    assert gate.acquire("tcp", "two") is False
    assert gate.release("tcp", "two") is False
    assert gate.release("http", "one") is True
    assert gate.acquire("tcp", "two") is True


def test_measurement_gate_requests_cancel_once_and_waits_for_owner_release():
    now = [100.0]
    cancel_requests = []
    gate = NetworkMeasurementGate(
        clock=lambda: now[0],
        max_hold_seconds={"http": 10.0},
    )

    assert gate.acquire(
        "http",
        "one",
        cancel_callback=lambda: cancel_requests.append("cancel"),
    ) is True
    now[0] = 108.0
    assert gate.status()["long_running"] is True
    assert gate.acquire("tcp", "two") is False

    now[0] = 110.1
    assert gate.acquire("tcp", "two") is False
    assert cancel_requests == ["cancel"]
    assert gate.acquire("tcp", "two") is False
    assert gate.status()["active"] is True
    assert cancel_requests == ["cancel"]
    assert gate.release("http", "one") is True
    assert gate.acquire("tcp", "two") is True
    status = gate.status()
    assert status["kind"] == "tcp"
    assert status["expired_count"] == 1


def test_measurement_gate_does_not_extend_lease_for_same_owner_reacquire():
    now = [0.0]
    gate = NetworkMeasurementGate(
        clock=lambda: now[0],
        max_hold_seconds={"http": 10.0},
    )

    assert gate.acquire("http", "one") is True
    now[0] = 9.0
    assert gate.acquire("http", "one") is True
    now[0] = 10.1

    assert gate.is_available() is False
    assert gate.status()["expired_count"] == 1
    assert gate.release("http", "one") is True
    assert gate.is_available() is True


def test_measurement_gate_cancel_callback_can_release_without_deadlock():
    now = [0.0]
    gate = NetworkMeasurementGate(
        clock=lambda: now[0],
        max_hold_seconds={"http": 1.0},
    )
    cancel_requests = []

    def cancel_owner():
        cancel_requests.append("cancel")
        assert gate.release("http", "one") is True

    assert gate.acquire("http", "one", cancel_callback=cancel_owner) is True
    now[0] = 1.1

    assert gate.is_available() is True
    assert cancel_requests == ["cancel"]
    assert gate.acquire("tcp", "two") is True
    assert gate.status()["expired_count"] == 1


def test_measurement_gate_cancel_callback_exception_keeps_owner_locked(caplog):
    now = [0.0]
    logger = logging.getLogger("test.measurement_gate_cancel_failure")
    gate = NetworkMeasurementGate(
        clock=lambda: now[0],
        max_hold_seconds={"http": 1.0},
    )
    gate.set_diagnostic_logger(logger)
    cancel_requests = []

    def fail_cancel():
        cancel_requests.append("cancel")
        raise RuntimeError("cancel failed")

    assert gate.acquire("http", "one", cancel_callback=fail_cancel) is True
    now[0] = 1.1

    status = gate.status()
    assert status["active"] is True
    assert status["long_running"] is True
    assert status["expired_count"] == 1
    assert status["cancel_callback_failure_count"] == 1
    assert cancel_requests == ["cancel"]
    assert "measurement_cancel_callback_failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "cancel failed" not in caplog.text
    assert gate.acquire("tcp", "two") is False
    assert gate.current_owner().owner_id == "one"
    assert cancel_requests == ["cancel"]

    assert gate.release("http", "one") is True
    assert gate.acquire("tcp", "two") is True
    assert gate.status()["cancel_callback_failure_count"] == 1
