from __future__ import annotations

import csv
import json

import pytest

import measurement_transactions as transactions
from measurement_transactions import (
    MeasurementRecoverySpec,
    MeasurementTransactionError,
    commit_measurement_result,
    load_measurement_transactions,
    measurement_transaction_root_for_log,
    recover_measurement_transactions,
)


FIELDS = ("session_id", "direction", "status", "result_json")
SESSION_ID = "a" * 32


def make_storage(tmp_path):
    data_root = tmp_path / "data"
    log_path = data_root / "network_check_session_log.csv"
    results_root = data_root / "network_check_results"
    results_root.mkdir(parents=True)
    with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=FIELDS).writeheader()
    return log_path, results_root


def result_payload():
    return {
        "schema_version": 1,
        "session_id": SESSION_ID,
        "directions": {"upload": {}, "download": {}},
        "status": "success",
    }


def expected_rows():
    relative = f"data/network_check_results/{SESSION_ID}.json"
    return [
        {
            "session_id": SESSION_ID,
            "direction": "upload",
            "status": "success",
            "result_json": relative,
        },
        {
            "session_id": SESSION_ID,
            "direction": "download",
            "status": "success",
            "result_json": relative,
        },
    ]


def recovery_spec(log_path, results_root):
    return MeasurementRecoverySpec(
        source="http_sustained",
        log_path=log_path,
        results_root=results_root,
        fieldnames=FIELDS,
        key_field="direction",
        build_rows_from_result=lambda _payload: expected_rows(),
    )


def read_rows(log_path):
    with log_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def leave_committed_marker(tmp_path, monkeypatch):
    log_path, results_root = make_storage(tmp_path)
    result_path = results_root / f"{SESSION_ID}.json"
    with monkeypatch.context() as patch:
        patch.setattr(
            transactions,
            "_finish_measurement_transaction",
            lambda _transaction: False,
        )
        removed = commit_measurement_result(
            source="http_sustained",
            result_path=result_path,
            result=result_payload(),
            log_path=log_path,
            fieldnames=FIELDS,
            key_field="direction",
            rows=expected_rows(),
        )
    assert removed is False
    return log_path, results_root, result_path


def test_commit_writes_json_rows_and_removes_marker(tmp_path):
    log_path, results_root = make_storage(tmp_path)
    result_path = results_root / f"{SESSION_ID}.json"

    removed = commit_measurement_result(
        source="http_sustained",
        result_path=result_path,
        result=result_payload(),
        log_path=log_path,
        fieldnames=FIELDS,
        key_field="direction",
        rows=expected_rows(),
    )

    assert removed is True
    assert json.loads(result_path.read_text(encoding="utf-8")) == result_payload()
    assert read_rows(log_path) == expected_rows()
    assert load_measurement_transactions(
        measurement_transaction_root_for_log(log_path)
    ) == []


def test_recovery_appends_only_missing_full_direction_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    log_path, results_root, result_path = leave_committed_marker(
        tmp_path,
        monkeypatch,
    )
    with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(expected_rows()[0])
    original_json = result_path.read_bytes()
    root = measurement_transaction_root_for_log(log_path)
    spec = recovery_spec(log_path, results_root)

    assert recover_measurement_transactions(root, (spec,)) == {
        "http_sustained": 1
    }
    assert read_rows(log_path) == expected_rows()
    first_recovery = log_path.read_bytes()
    assert result_path.read_bytes() == original_json
    assert recover_measurement_transactions(root, (spec,)) == {
        "http_sustained": 0
    }
    assert log_path.read_bytes() == first_recovery


def test_recovery_restores_all_rows_after_json_only_commit(
    tmp_path,
    monkeypatch,
):
    log_path, results_root, _ = leave_committed_marker(tmp_path, monkeypatch)
    with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=FIELDS).writeheader()

    recover_measurement_transactions(
        measurement_transaction_root_for_log(log_path),
        (recovery_spec(log_path, results_root),),
    )

    assert read_rows(log_path) == expected_rows()


def test_recovery_discards_intent_when_json_and_rows_do_not_exist(
    tmp_path,
    monkeypatch,
):
    log_path, results_root, result_path = leave_committed_marker(
        tmp_path,
        monkeypatch,
    )
    result_path.unlink()
    with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=FIELDS).writeheader()
    root = measurement_transaction_root_for_log(log_path)

    assert recover_measurement_transactions(
        root,
        (recovery_spec(log_path, results_root),),
    ) == {"http_sustained": 1}
    assert load_measurement_transactions(root) == []
    assert read_rows(log_path) == []


@pytest.mark.parametrize("conflict", ["hash", "row", "duplicate", "orphan-row"])
def test_recovery_fails_closed_on_conflicting_state(
    tmp_path,
    monkeypatch,
    conflict,
):
    log_path, results_root, result_path = leave_committed_marker(
        tmp_path,
        monkeypatch,
    )
    rows = expected_rows()
    if conflict == "hash":
        result_path.write_text(
            json.dumps(
                {
                    **result_payload(),
                    "status": "changed",
                }
            ),
            encoding="utf-8",
        )
    else:
        with log_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            if conflict == "row":
                writer.writerow({**rows[0], "status": "failure"})
            elif conflict == "duplicate":
                writer.writerow(rows[0])
                writer.writerow(rows[0])
            elif conflict == "orphan-row":
                result_path.unlink()
                writer.writerow(rows[0])

    with pytest.raises(MeasurementTransactionError):
        recover_measurement_transactions(
            measurement_transaction_root_for_log(log_path),
            (recovery_spec(log_path, results_root),),
        )


def test_corrupt_or_oversized_marker_fails_closed(tmp_path):
    log_path, results_root = make_storage(tmp_path)
    root = measurement_transaction_root_for_log(log_path)
    source_root = root / "http_sustained"
    source_root.mkdir(parents=True)
    marker = source_root / "broken.json"
    marker.write_text("{not-json", encoding="utf-8")

    with pytest.raises(MeasurementTransactionError):
        recover_measurement_transactions(
            root,
            (recovery_spec(log_path, results_root),),
        )


def test_recovery_rejects_marker_rows_that_do_not_match_result_json(
    tmp_path,
    monkeypatch,
):
    log_path, results_root, _ = leave_committed_marker(
        tmp_path,
        monkeypatch,
    )
    root = measurement_transaction_root_for_log(log_path)
    marker = load_measurement_transactions(root)[0].marker_path
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["rows"][0]["status"] = "forged"
    marker.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(
        MeasurementTransactionError,
        match="상세 결과와 일치하지 않습니다",
    ):
        recover_measurement_transactions(
            root,
            (recovery_spec(log_path, results_root),),
        )
    assert read_rows(log_path) == expected_rows()


@pytest.mark.parametrize(
    "failed_cleanup",
    ["csv", "json"],
)
def test_rollback_state_recovers_partial_cleanup_idempotently(
    tmp_path,
    monkeypatch,
    failed_cleanup,
):
    log_path, results_root = make_storage(tmp_path)
    result_path = results_root / f"{SESSION_ID}.json"
    original_append = transactions._append_csv_row_durably
    append_calls = 0

    def fail_after_first_row(handle, writer, row):
        nonlocal append_calls
        append_calls += 1
        if append_calls == 2:
            raise OSError("simulated second row failure")
        original_append(handle, writer, row)

    cleanup_name = (
        "_remove_expected_session_rows"
        if failed_cleanup == "csv"
        else "_remove_expected_result"
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            transactions,
            "_append_csv_row_durably",
            fail_after_first_row,
        )
        patch.setattr(
            transactions,
            cleanup_name,
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("simulated cleanup failure")
            ),
        )
        with pytest.raises(OSError, match="second row failure"):
            commit_measurement_result(
                source="http_sustained",
                result_path=result_path,
                result=result_payload(),
                log_path=log_path,
                fieldnames=FIELDS,
                key_field="direction",
                rows=expected_rows(),
            )

    root = measurement_transaction_root_for_log(log_path)
    pending = load_measurement_transactions(root)
    assert len(pending) == 1
    assert pending[0].state == "rollback_requested"
    assert result_path.exists() is (failed_cleanup == "json")
    assert bool(read_rows(log_path)) is (failed_cleanup == "csv")

    recover_measurement_transactions(
        root,
        (recovery_spec(log_path, results_root),),
    )
    assert result_path.exists() is False
    assert read_rows(log_path) == []
    assert load_measurement_transactions(root) == []

    snapshot = log_path.read_bytes()
    assert recover_measurement_transactions(
        root,
        (recovery_spec(log_path, results_root),),
    ) == {"http_sustained": 0}
    assert log_path.read_bytes() == snapshot


def test_unexpected_or_linked_source_entries_fail_closed(tmp_path):
    log_path, results_root = make_storage(tmp_path)
    root = measurement_transaction_root_for_log(log_path)
    source_root = root / "http_sustained"
    source_root.mkdir(parents=True)
    (source_root / "unexpected.txt").write_text(
        "not a transaction",
        encoding="utf-8",
    )

    with pytest.raises(MeasurementTransactionError):
        recover_measurement_transactions(
            root,
            (recovery_spec(log_path, results_root),),
        )
