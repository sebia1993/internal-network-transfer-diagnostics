from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


FAULT_TESTS = [
    "tests/test_fault_injection.py",
    "tests/test_app.py::test_upload_cleans_partial_file_when_space_runs_out_during_copy",
    "tests/test_app.py::test_upload_partial_log_write_failure_rolls_back_csv_and_file",
    "tests/test_bounded_server.py::test_bounded_server_rejects_excess_slow_clients_and_recovers_capacity",
    "tests/test_bounded_server.py::test_force_shutdown_keeps_socket_object_valid_for_handler_cleanup",
    "tests/test_startup_ports.py::test_main_hard_exits_without_releasing_data_lock_when_handler_stays_alive",
    "tests/test_network_measurement.py::test_measurement_gate_requests_cancel_once_and_waits_for_owner_release",
    "tests/test_network_measurement.py::test_measurement_gate_cancel_callback_exception_keeps_owner_locked",
    "tests/test_network_sustained.py::test_gate_reports_sustained_max_hold_result_persistence_failure",
    "tests/test_upload_transaction_recovery.py",
]


def main() -> int:
    return int(pytest.main(["-q", *FAULT_TESTS]))


if __name__ == "__main__":
    raise SystemExit(main())
