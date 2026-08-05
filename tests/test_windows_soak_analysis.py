from __future__ import annotations

import json

import pytest

from tools import analyze_windows_soak_summary as analysis_module
from tools.analyze_windows_soak_summary import (
    EXPECTED_UPLOAD_BYTES_PER_CYCLE,
    METRIC_THRESHOLDS,
    analyze_summary,
)
from tools.run_windows_stability_soak import SOAK_UPLOAD_BYTES


MIB = 1024 * 1024
BASE_VALUES = {
    "working_set_bytes": 64 * MIB,
    "handle_count": 220,
    "thread_count": 5,
    "tcp_socket_count": 2,
}
WITHIN_PROCESS_INCREASE = {
    "working_set_bytes": 2 * MIB,
    "handle_count": 2,
    "thread_count": 0,
    "tcp_socket_count": 0,
}
PEAK_INCREMENT = {
    "working_set_bytes": 1 * MIB,
    "handle_count": 1,
    "thread_count": 0,
    "tcp_socket_count": 0,
}


def build_metric(end_value: int | float, metric: str) -> dict:
    increase = WITHIN_PROCESS_INCREASE[metric]
    start_value = end_value - increase
    return {
        "status": "available",
        "start": start_value,
        "end": end_value,
        "maximum": end_value + PEAK_INCREMENT[metric],
        "increase": increase,
        "reason": "",
    }


def combined_status(statuses: list[str]) -> str:
    if statuses and all(status == "available" for status in statuses):
        return "available"
    if not statuses or all(status == "unavailable" for status in statuses):
        return "unavailable"
    return "partial"


def refresh_resource_contract(summary: dict) -> None:
    resources = summary["process_resources"]
    processes = resources["processes"]
    for process in processes:
        process["status"] = combined_status(
            [
                process["metrics"][metric]["status"]
                for metric in METRIC_THRESHOLDS
            ]
        )

    aggregate_metrics = {}
    for metric in METRIC_THRESHOLDS:
        metric_rows = [process["metrics"][metric] for process in processes]
        usable_rows = [
            row for row in metric_rows if row["status"] != "unavailable"
        ]
        status = combined_status([row["status"] for row in metric_rows])
        reasons = sorted(
            {
                row["reason"]
                for row in metric_rows
                if row["reason"]
            }
        )
        if usable_rows:
            start = usable_rows[0]["start"]
            end = usable_rows[-1]["end"]
            maximum = max(row["maximum"] for row in usable_rows)
            increase = end - start
        else:
            start = end = maximum = increase = None
        aggregate_metrics[metric] = {
            "status": status,
            "start": start,
            "end": end,
            "maximum": maximum,
            "increase": increase,
            "reason": "; ".join(reasons),
        }

    resources["metrics"] = aggregate_metrics
    resources["status"] = combined_status(
        [row["status"] for row in aggregate_metrics.values()]
    )
    resources["reason"] = "; ".join(
        sorted(
            {
                row["reason"]
                for row in aggregate_metrics.values()
                if row["reason"]
            }
        )
    )
    resources["sample_interval_seconds"] = 1.0
    resources["process_count"] = len(processes)
    resources["sample_count"] = sum(
        process["sample_count"] for process in processes
    )


def build_summary(
    *,
    cycles: int = 30,
    value_override=None,
    status: str = "success",
) -> dict:
    processes = []
    for cycle in range(1, cycles + 1):
        for stage in ("initial", "restart"):
            metrics = {}
            for metric, base_value in BASE_VALUES.items():
                stage_offset = (
                    MIB
                    if metric == "working_set_bytes" and stage == "restart"
                    else 1
                    if metric == "handle_count" and stage == "restart"
                    else 0
                )
                end_value = base_value + stage_offset
                if value_override is not None:
                    end_value = value_override(
                        cycle,
                        stage,
                        metric,
                        end_value,
                    )
                metrics[metric] = build_metric(end_value, metric)
            processes.append(
                {
                    "label": f"cycle-{cycle:06d}-{stage}",
                    "pid": 10_000 + (cycle * 2) + (stage == "restart"),
                    "status": "available",
                    "sample_count": 3,
                    "metrics": metrics,
                }
            )
    summary = {
        "status": status,
        "duration_seconds": 45 * 60,
        "completed_cycles": cycles,
        "uploaded_bytes": cycles * SOAK_UPLOAD_BYTES,
        "tcp_self_checks": cycles,
        "process_resources": {
            "status": "available",
            "process_count": cycles * 2,
            "sample_count": cycles * 6,
            "sample_interval_seconds": 1.0,
            "reason": "",
            "metrics": {},
            "processes": processes,
        },
    }
    refresh_resource_contract(summary)
    return summary


def find_process(summary: dict, cycle: int, stage: str) -> dict:
    label = f"cycle-{cycle:06d}-{stage}"
    return next(
        process
        for process in summary["process_resources"]["processes"]
        if process["label"] == label
    )


def test_stable_summary_passes_and_reports_stage_statistics():
    result = analyze_summary(build_summary())

    assert result["verdict"] == "PASS_NO_REPEATED_PROCESS_GROWTH"
    assert result["data_quality"]["status"] == "pass"
    assert result["review_findings"] == []

    working_set = result["stages"]["initial"]["metrics"]["working_set_bytes"]
    assert working_set["valid_cycles"] == 30
    assert working_set["warmup_cycles_excluded"] == 3
    assert working_set["analyzed_cycles"] == 27
    assert working_set["window_size"] == 5
    assert working_set["baseline_median"] == 64 * MIB
    assert working_set["final_median"] == 64 * MIB
    assert working_set["absolute_change"] == 0
    assert working_set["baseline_mad"] == 0
    assert working_set["theil_sen_per_cycle"] == 0
    assert working_set["maximum_peak"] == 65 * MIB
    assert working_set["within_pid_increase"] == {
        "median": 2 * MIB,
        "p95": 2 * MIB,
        "maximum": 2 * MIB,
    }
    assert not any(working_set["conditions"].values())


def test_linear_working_set_growth_requires_review_when_all_conditions_hold():
    def grow_initial_working_set(cycle, stage, metric, value):
        if stage == "initial" and metric == "working_set_bytes":
            return value + (cycle * 3 * MIB)
        return value

    result = analyze_summary(
        build_summary(value_override=grow_initial_working_set)
    )

    assert result["verdict"] == "REVIEW_RESOURCE_ANOMALY"
    metric = result["stages"]["initial"]["metrics"]["working_set_bytes"]
    assert result["review_findings"] == [
        {
            "stage": "initial",
            "metric": "working_set_bytes",
            "kind": "repeated_process_growth",
            "observed": 66 * MIB,
            "threshold": metric["thresholds"]["review"],
        }
    ]
    assert metric["theil_sen_per_cycle"] == 3 * MIB
    assert metric["positive_pair_ratio"] == 1
    assert metric["tail_theil_sen_per_cycle"] == 3 * MIB
    assert metric["all_sustained_growth_conditions_met"] is True
    assert all(metric["conditions"].values())


@pytest.mark.parametrize(
    ("metric_name", "growth_per_cycle"),
    [
        ("handle_count", 3),
        ("thread_count", 1),
        ("tcp_socket_count", 1),
    ],
)
def test_each_count_metric_requires_review_for_robust_linear_growth(
    metric_name,
    growth_per_cycle,
):
    def grow_selected_metric(cycle, stage, metric, value):
        if stage == "restart" and metric == metric_name:
            return value + (cycle * growth_per_cycle)
        return value

    result = analyze_summary(
        build_summary(value_override=grow_selected_metric)
    )

    assert result["verdict"] == "REVIEW_RESOURCE_ANOMALY"
    metric = result["stages"]["restart"]["metrics"][metric_name]
    assert metric["all_sustained_growth_conditions_met"] is True
    assert all(metric["conditions"].values())
    assert any(
        finding["stage"] == "restart"
        and finding["metric"] == metric_name
        for finding in result["review_findings"]
    )


def test_large_single_peak_triggers_strong_peak_review():
    def spike_last_cycle(cycle, stage, metric, value):
        if (
            cycle == 30
            and stage == "initial"
            and metric == "working_set_bytes"
        ):
            return value + (256 * MIB)
        return value

    result = analyze_summary(build_summary(value_override=spike_last_cycle))

    assert result["verdict"] == "REVIEW_RESOURCE_ANOMALY"
    metric = result["stages"]["initial"]["metrics"]["working_set_bytes"]
    assert metric["maximum_peak"] > 300 * MIB
    assert metric["all_sustained_growth_conditions_met"] is False
    assert metric["conditions"]["change_threshold_met"] is False
    assert metric["anomaly_evidence"][0]["kind"] == "strong_peak_excursion"


def test_early_growth_that_plateaus_triggers_level_shift_review():
    def early_growth_then_plateau(cycle, stage, metric, value):
        if stage == "restart" and metric == "handle_count":
            if cycle <= 8:
                return value
            return value + (min(cycle - 8, 4) * 20)
        return value

    result = analyze_summary(
        build_summary(value_override=early_growth_then_plateau)
    )

    assert result["verdict"] == "REVIEW_RESOURCE_ANOMALY"
    metric = result["stages"]["restart"]["metrics"]["handle_count"]
    assert metric["absolute_change"] >= metric["thresholds"]["review"]
    assert metric["conditions"]["tail_slope_positive"] is False
    assert metric["all_sustained_growth_conditions_met"] is False
    assert metric["anomaly_evidence"][0]["kind"] == "persistent_level_shift"


def test_large_within_pid_p95_triggers_review_without_cross_cycle_growth():
    summary = build_summary()
    for cycle in range(1, 31):
        metric = find_process(summary, cycle, "initial")["metrics"][
            "handle_count"
        ]
        metric["start"] = metric["end"] - 24
        metric["increase"] = 24
    refresh_resource_contract(summary)

    result = analyze_summary(summary)

    assert result["verdict"] == "REVIEW_RESOURCE_ANOMALY"
    metric = result["stages"]["initial"]["metrics"]["handle_count"]
    assert metric["all_sustained_growth_conditions_met"] is False
    assert metric["anomaly_checks"]["within_pid_p95_review"] is True
    assert metric["anomaly_evidence"][0]["kind"] == "within_pid_increase"


def test_single_strong_within_pid_maximum_triggers_review():
    summary = build_summary()
    metric = find_process(summary, 10, "initial")["metrics"][
        "working_set_bytes"
    ]
    metric["start"] = metric["end"] - (40 * MIB)
    metric["increase"] = 40 * MIB
    refresh_resource_contract(summary)

    result = analyze_summary(summary)

    assert result["verdict"] == "REVIEW_RESOURCE_ANOMALY"
    metric = result["stages"]["initial"]["metrics"]["working_set_bytes"]
    assert metric["anomaly_checks"]["within_pid_p95_review"] is False
    assert metric["anomaly_checks"]["within_pid_maximum_strong"] is True
    assert metric["anomaly_evidence"][0]["kind"] == "within_pid_increase"


def test_failed_soak_is_functional_fail_without_resource_judgment():
    result = analyze_summary(build_summary(status="failed"))

    assert result["verdict"] == "FUNCTIONAL_FAIL"
    assert result["data_quality"]["status"] == "not_evaluated"
    assert result["stages"] == {}


def test_process_count_mismatch_is_inconclusive():
    summary = build_summary()
    summary["process_resources"]["process_count"] -= 1

    result = analyze_summary(summary)

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert result["data_quality"]["status"] == "failed"
    assert "PROCESS_COUNT_MISMATCH" in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }


@pytest.mark.parametrize(
    ("field", "value", "expected_issue"),
    [
        ("uploaded_bytes", 0, "UPLOADED_BYTES_MISMATCH"),
        ("tcp_self_checks", 0, "TCP_SELF_CHECKS_MISMATCH"),
    ],
)
def test_completed_work_contract_mismatch_is_inconclusive(
    field,
    value,
    expected_issue,
):
    summary = build_summary()
    summary[field] = value

    result = analyze_summary(summary)

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert expected_issue in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ("aggregate_sample_count", "AGGREGATE_SAMPLE_COUNT_MISMATCH"),
        ("resource_status", "INVALID_PROCESS_RESOURCE_STATUS"),
        ("resource_status_mismatch", "PROCESS_RESOURCE_STATUS_MISMATCH"),
        ("sample_interval", "INVALID_SAMPLE_INTERVAL"),
        ("aggregate_metric", "AGGREGATE_METRIC_STRUCTURE_INVALID"),
    ],
)
def test_aggregate_resource_contract_violation_is_inconclusive(
    mutation,
    expected_issue,
):
    summary = build_summary()
    resources = summary["process_resources"]
    if mutation == "aggregate_sample_count":
        resources["sample_count"] -= 1
    elif mutation == "resource_status":
        resources["status"] = "corrupt"
    elif mutation == "resource_status_mismatch":
        resources["status"] = "partial"
    elif mutation == "sample_interval":
        resources["sample_interval_seconds"] = 0
    else:
        del resources["metrics"]["thread_count"]

    result = analyze_summary(summary)

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert expected_issue in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ("pid", "INVALID_PROCESS_PID"),
        ("sample_count", "INVALID_PROCESS_SAMPLE_COUNT"),
        ("status_enum", "INVALID_PROCESS_STATUS"),
        ("status_mismatch", "PROCESS_STATUS_MISMATCH"),
        ("missing_metric", "PROCESS_METRIC_STRUCTURE_INVALID"),
        ("malformed_metric", "PROCESS_METRIC_STRUCTURE_INVALID"),
        ("fractional_metric", "PROCESS_METRIC_STRUCTURE_INVALID"),
    ],
)
def test_process_contract_violation_is_inconclusive(
    mutation,
    expected_issue,
):
    summary = build_summary()
    process = find_process(summary, 1, "initial")
    if mutation == "pid":
        process["pid"] = None
    elif mutation == "sample_count":
        process["sample_count"] = 1
    elif mutation == "status_enum":
        process["status"] = "corrupt"
    elif mutation == "status_mismatch":
        process["status"] = "partial"
    elif mutation == "missing_metric":
        del process["metrics"]["thread_count"]
    elif mutation == "fractional_metric":
        process["metrics"]["thread_count"].update(
            {
                "start": 2.5,
                "end": 3.5,
                "maximum": 3.5,
                "increase": 1.0,
            }
        )
    else:
        process["metrics"]["working_set_bytes"]["maximum"] = 0

    result = analyze_summary(summary)

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert expected_issue in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }


def test_positive_pid_reuse_is_allowed():
    summary = build_summary()
    processes = summary["process_resources"]["processes"]
    processes[1]["pid"] = processes[0]["pid"]

    result = analyze_summary(summary)

    assert result["verdict"] == "PASS_NO_REPEATED_PROCESS_GROWTH"
    assert result["data_quality"]["status"] == "pass"


def test_default_duration_gate_requires_forty_five_minutes():
    summary = build_summary()
    summary["duration_seconds"] = 30 * 60

    result = analyze_summary(summary)

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert "DURATION_BELOW_MINIMUM" in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }


def test_duration_gate_can_be_overridden_or_disabled():
    summary = build_summary()
    summary["duration_seconds"] = 30 * 60

    thirty_minute_result = analyze_summary(
        summary,
        minimum_duration_minutes=30,
    )
    summary["duration_seconds"] = 60
    disabled_result = analyze_summary(
        summary,
        minimum_duration_minutes=0,
    )

    assert (
        thirty_minute_result["verdict"]
        == "PASS_NO_REPEATED_PROCESS_GROWTH"
    )
    assert disabled_result["verdict"] == "PASS_NO_REPEATED_PROCESS_GROWTH"


def test_stage_sample_and_metric_coverage_below_ninety_percent_is_inconclusive():
    summary = build_summary()
    for cycle in range(1, 5):
        initial = find_process(summary, cycle, "initial")
        initial["sample_count"] = 1
        restart = find_process(summary, cycle, "restart")
        restart["metrics"]["tcp_socket_count"] = {
            "status": "unavailable",
            "start": None,
            "end": None,
            "maximum": None,
            "increase": None,
            "reason": "simulated API failure",
        }
    refresh_resource_contract(summary)

    result = analyze_summary(summary)
    issue_codes = {
        issue["code"] for issue in result["data_quality"]["issues"]
    }

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert "SAMPLE_COVERAGE_BELOW_MINIMUM" in issue_codes
    assert "METRIC_COVERAGE_BELOW_MINIMUM" in issue_codes
    assert (
        result["data_quality"]["stages"]["initial"]["sample_coverage"]
        < 0.90
    )
    assert (
        result["data_quality"]["stages"]["restart"]["metric_coverage"][
            "tcp_socket_count"
        ]
        < 0.90
    )


def test_well_formed_partial_metrics_at_ninety_percent_coverage_are_allowed():
    summary = build_summary()
    for cycle in range(1, 4):
        metric = find_process(summary, cycle, "initial")["metrics"][
            "thread_count"
        ]
        metric["status"] = "partial"
        metric["reason"] = "simulated partial sample"
    refresh_resource_contract(summary)

    result = analyze_summary(summary)

    assert result["verdict"] == "PASS_NO_REPEATED_PROCESS_GROWTH"
    quality = result["data_quality"]["stages"]["initial"]
    assert quality["metric_coverage"]["thread_count"] == 0.9
    assert quality["final_window_coverage"]["thread_count"] == 1


def test_partial_metric_over_ten_percent_is_inconclusive():
    summary = build_summary()
    for cycle in range(1, 5):
        metric = find_process(summary, cycle, "initial")["metrics"][
            "thread_count"
        ]
        metric["status"] = "partial"
        metric["reason"] = "simulated partial sample"
    refresh_resource_contract(summary)

    result = analyze_summary(summary)

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert "METRIC_COVERAGE_BELOW_MINIMUM" in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }


def test_partial_metric_in_final_analysis_window_is_inconclusive():
    summary = build_summary()
    metric = find_process(summary, 30, "restart")["metrics"][
        "tcp_socket_count"
    ]
    metric["status"] = "partial"
    metric["reason"] = "simulated final-window API failure"
    refresh_resource_contract(summary)

    result = analyze_summary(summary)

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert "FINAL_ANALYSIS_WINDOW_INCOMPLETE" in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }
    assert (
        result["data_quality"]["stages"]["restart"][
            "final_window_coverage"
        ]["tcp_socket_count"]
        == 0.8
    )


def test_invalid_stage_labels_reduce_stage_coverage_and_are_inconclusive():
    summary = build_summary()
    for cycle in range(1, 5):
        find_process(summary, cycle, "initial")["label"] = (
            f"cycle-{cycle:06d}-unknown"
        )

    result = analyze_summary(summary)
    issue_codes = {
        issue["code"] for issue in result["data_quality"]["issues"]
    }

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert "INVALID_PROCESS_LABEL" in issue_codes
    assert "STAGE_COVERAGE_BELOW_MINIMUM" in issue_codes


def test_less_than_twenty_cycles_is_inconclusive():
    result = analyze_summary(build_summary(cycles=19))

    assert result["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert "COMPLETED_CYCLES_BELOW_MINIMUM" in {
        issue["code"] for issue in result["data_quality"]["issues"]
    }


def test_threshold_configuration_matches_metric_policy():
    assert EXPECTED_UPLOAD_BYTES_PER_CYCLE == SOAK_UPLOAD_BYTES
    assert METRIC_THRESHOLDS == {
        "working_set_bytes": {
            "review_absolute": 16 * MIB,
            "review_relative": 0.15,
            "strong_absolute": 32 * MIB,
            "strong_relative": 0.25,
        },
        "handle_count": {
            "review_absolute": 16,
            "review_relative": 0.10,
            "strong_absolute": 32,
            "strong_relative": 0.20,
        },
        "thread_count": {
            "review_absolute": 2,
            "review_relative": 0.0,
            "strong_absolute": 4,
            "strong_relative": 0.0,
        },
        "tcp_socket_count": {
            "review_absolute": 2,
            "review_relative": 0.0,
            "strong_absolute": 4,
            "strong_relative": 0.0,
        },
    }


def test_cli_prints_json_and_writes_output_file(tmp_path, capsys):
    input_path = tmp_path / "soak-summary.json"
    output_path = tmp_path / "reports" / "analysis.json"
    input_path.write_text(
        json.dumps(build_summary(), ensure_ascii=False),
        encoding="utf-8",
    )

    assert (
        analysis_module.main(
            [str(input_path), "--output", str(output_path)]
        )
        == 0
    )

    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert printed == written
    assert written["verdict"] == "PASS_NO_REPEATED_PROCESS_GROWTH"


def test_cli_invalid_json_still_prints_structured_json(tmp_path, capsys):
    input_path = tmp_path / "broken.json"
    input_path.write_text("{", encoding="utf-8")

    assert analysis_module.main([str(input_path)]) == 2

    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == "INCONCLUSIVE_TELEMETRY"
    assert printed["data_quality"]["issues"][0]["code"] == "INPUT_JSON_INVALID"


def test_cli_exit_code_one_for_resource_anomaly(tmp_path, capsys):
    def spike_working_set(cycle, stage, metric, value):
        if cycle == 15 and stage == "initial" and metric == "working_set_bytes":
            return value + (40 * MIB)
        return value

    input_path = tmp_path / "review.json"
    input_path.write_text(
        json.dumps(
            build_summary(value_override=spike_working_set),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert analysis_module.main([str(input_path)]) == 1
    assert (
        json.loads(capsys.readouterr().out)["verdict"]
        == "REVIEW_RESOURCE_ANOMALY"
    )


def test_cli_exit_code_three_for_functional_failure(tmp_path, capsys):
    input_path = tmp_path / "failed.json"
    input_path.write_text(
        json.dumps(build_summary(status="failed"), ensure_ascii=False),
        encoding="utf-8",
    )

    assert analysis_module.main([str(input_path)]) == 3
    assert json.loads(capsys.readouterr().out)["verdict"] == "FUNCTIONAL_FAIL"


def test_cli_duration_override_allows_shorter_valid_soak(tmp_path, capsys):
    summary = build_summary()
    summary["duration_seconds"] = 30 * 60
    input_path = tmp_path / "thirty-minutes.json"
    input_path.write_text(
        json.dumps(summary, ensure_ascii=False),
        encoding="utf-8",
    )

    assert (
        analysis_module.main(
            [
                str(input_path),
                "--minimum-duration-minutes",
                "30",
            ]
        )
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["verdict"]
        == "PASS_NO_REPEATED_PROCESS_GROWTH"
    )
