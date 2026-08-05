from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MINIMUM_DURATION_MINUTES = 45.0
MAXIMUM_DURATION_MINUTES = 60.0
EXPECTED_UPLOAD_BYTES_PER_CYCLE = 256 * 1024
MIN_VALID_CYCLES = 20
MIN_COVERAGE = 0.90
MIN_SAMPLES_PER_PROCESS = 2
MIN_POSITIVE_PAIR_RATIO = 0.70
MIN_PROJECTED_THRESHOLD_RATIO = 0.75
MIN_TAIL_THRESHOLD_RATIO = 0.20
MIN_FINAL_WINDOW_DOMINANCE = 0.80
MAD_SCALE = 1.4826
NOISE_MULTIPLIER = 6.0

STAGES = ("initial", "restart")
RESOURCE_STATUSES = ("available", "partial", "unavailable")
METRIC_FIELDS = ("status", "start", "end", "maximum", "increase", "reason")
PROCESS_LABEL_PATTERN = re.compile(
    r"^cycle-(?P<cycle>[0-9]+)-(?P<stage>initial|restart)$"
)

METRIC_THRESHOLDS = {
    "working_set_bytes": {
        "review_absolute": 16 * 1024 * 1024,
        "review_relative": 0.15,
        "strong_absolute": 32 * 1024 * 1024,
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

LIMITATIONS = [
    "각 cycle의 initial/restart는 서로 다른 프로세스이므로 전체 increase를 단일 프로세스 증가로 해석하지 않습니다.",
    "프로세스별 원시 샘플과 시각이 없어 stage 내부 시간 slope가 아니라 cycle별 end 값의 slope를 계산합니다.",
    "1초보다 짧은 자원 spike와 TCP 연결은 누락될 수 있습니다.",
    "working set은 private memory가 아니며 OS paging과 공유 페이지 영향을 받습니다.",
    "업로드 파일과 CSV가 cycle마다 증가하므로 새 프로세스의 시작 크기 증가는 데이터 규모 증가일 수 있습니다.",
]


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _rounded(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    rounded = round(float(value), 6)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return (
        ordered[lower_index]
        + (ordered[upper_index] - ordered[lower_index]) * fraction
    )


def _theil_sen(
    points: list[tuple[int, float]],
) -> tuple[float, float]:
    slopes = []
    for left_index, (left_cycle, left_value) in enumerate(points):
        for right_index in range(left_index + 1, len(points)):
            right_cycle, right_value = points[right_index]
            cycle_delta = right_cycle - left_cycle
            if cycle_delta <= 0:
                continue
            slopes.append((right_value - left_value) / cycle_delta)
    if not slopes:
        return 0.0, 0.0
    return (
        float(statistics.median(slopes)),
        sum(1 for slope in slopes if slope > 0) / len(slopes),
    )


def _combined_status(statuses: list[str]) -> str:
    if statuses and all(status == "available" for status in statuses):
        return "available"
    if not statuses or all(status == "unavailable" for status in statuses):
        return "unavailable"
    return "partial"


def _metric_structure_error(value: Any) -> str:
    if not isinstance(value, dict):
        return "metric 값이 객체가 아닙니다."
    missing = [name for name in METRIC_FIELDS if name not in value]
    if missing:
        return f"필수 필드가 없습니다: {', '.join(missing)}"
    status = value.get("status")
    if status not in RESOURCE_STATUSES:
        return f"status가 올바르지 않습니다: {status!r}"
    if not isinstance(value.get("reason"), str):
        return "reason은 문자열이어야 합니다."
    numeric_fields = ("start", "end", "maximum", "increase")
    if status == "unavailable":
        if any(value.get(name) is not None for name in numeric_fields):
            return "unavailable metric의 숫자 필드는 모두 null이어야 합니다."
        return ""
    if not all(_is_number(value.get(name)) for name in numeric_fields):
        return f"{status} metric의 숫자 필드가 올바르지 않습니다."
    if not all(
        isinstance(value.get(name), int)
        and not isinstance(value.get(name), bool)
        for name in numeric_fields
    ):
        return f"{status} metric의 숫자 필드는 정수여야 합니다."
    start = float(value["start"])
    end = float(value["end"])
    maximum = float(value["maximum"])
    increase = float(value["increase"])
    if start < 0 or end < 0 or maximum < 0:
        return "start/end/maximum은 음수일 수 없습니다."
    if maximum < max(start, end):
        return "maximum이 start 또는 end보다 작습니다."
    expected_increase = end - start
    tolerance = max(1e-6, abs(expected_increase) * 1e-9)
    if abs(increase - expected_increase) > tolerance:
        return "increase가 end-start와 일치하지 않습니다."
    return ""


def _metric_values(process: dict[str, Any], metric: str) -> dict[str, float] | None:
    metrics = process.get("metrics")
    if not isinstance(metrics, dict):
        return None
    value = metrics.get(metric)
    if _metric_structure_error(value):
        return None
    if value["status"] != "available":
        return None
    return {
        "start": float(value["start"]),
        "end": float(value["end"]),
        "maximum": float(value["maximum"]),
        "increase": float(value["increase"]),
    }


def _source_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "duration_seconds": payload.get("duration_seconds"),
        "completed_cycles": payload.get("completed_cycles"),
        "uploaded_bytes": payload.get("uploaded_bytes"),
        "tcp_self_checks": payload.get("tcp_self_checks"),
    }


def _issue(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def evaluate_data_quality(
    payload: dict[str, Any],
    *,
    minimum_duration_minutes: float = DEFAULT_MINIMUM_DURATION_MINUTES,
) -> tuple[
    dict[str, Any],
    dict[str, dict[int, dict[str, Any]]],
    int,
    float | None,
]:
    issues: list[dict[str, str]] = []
    if (
        not _is_number(minimum_duration_minutes)
        or float(minimum_duration_minutes) < 0
        or float(minimum_duration_minutes) > MAXIMUM_DURATION_MINUTES
    ):
        valid_minimum_duration: float | None = None
        issues.append(
            _issue(
                "INVALID_MINIMUM_DURATION_MINUTES",
                (
                    "minimum_duration_minutes는 "
                    f"0~{MAXIMUM_DURATION_MINUTES:g} 범위여야 합니다."
                ),
            )
        )
    else:
        valid_minimum_duration = float(minimum_duration_minutes)

    completed_value = payload.get("completed_cycles")
    if (
        not isinstance(completed_value, int)
        or isinstance(completed_value, bool)
        or completed_value < 1
    ):
        issues.append(
            _issue(
                "INVALID_COMPLETED_CYCLES",
                "completed_cycles는 1 이상의 정수여야 합니다.",
            )
        )
        completed_cycles = 0
    else:
        completed_cycles = completed_value
        if completed_cycles < MIN_VALID_CYCLES:
            issues.append(
                _issue(
                    "COMPLETED_CYCLES_BELOW_MINIMUM",
                    f"최소 {MIN_VALID_CYCLES} cycles가 필요합니다.",
                )
            )

    duration_value = payload.get("duration_seconds")
    if not _is_number(duration_value) or float(duration_value) <= 0:
        duration_seconds = None
        issues.append(
            _issue(
                "INVALID_DURATION_SECONDS",
                "duration_seconds는 0보다 큰 숫자여야 합니다.",
            )
        )
    else:
        duration_seconds = float(duration_value)
        if (
            valid_minimum_duration is not None
            and valid_minimum_duration > 0
            and duration_seconds < valid_minimum_duration * 60
        ):
            issues.append(
                _issue(
                    "DURATION_BELOW_MINIMUM",
                    (
                        f"duration_seconds={duration_seconds:g}, "
                        f"minimum={valid_minimum_duration * 60:g}"
                    ),
                )
            )

    expected_uploaded_bytes = (
        completed_cycles * EXPECTED_UPLOAD_BYTES_PER_CYCLE
    )
    uploaded_bytes = payload.get("uploaded_bytes")
    if not isinstance(uploaded_bytes, int) or isinstance(uploaded_bytes, bool):
        issues.append(
            _issue(
                "INVALID_UPLOADED_BYTES",
                "uploaded_bytes는 정수여야 합니다.",
            )
        )
    elif completed_cycles > 0 and uploaded_bytes != expected_uploaded_bytes:
        issues.append(
            _issue(
                "UPLOADED_BYTES_MISMATCH",
                (
                    f"uploaded_bytes={uploaded_bytes}, "
                    f"expected={expected_uploaded_bytes}"
                ),
            )
        )

    tcp_self_checks = payload.get("tcp_self_checks")
    if not isinstance(tcp_self_checks, int) or isinstance(tcp_self_checks, bool):
        issues.append(
            _issue(
                "INVALID_TCP_SELF_CHECKS",
                "tcp_self_checks는 정수여야 합니다.",
            )
        )
    elif completed_cycles > 0 and tcp_self_checks != completed_cycles:
        issues.append(
            _issue(
                "TCP_SELF_CHECKS_MISMATCH",
                (
                    f"tcp_self_checks={tcp_self_checks}, "
                    f"expected={completed_cycles}"
                ),
            )
        )

    resources = payload.get("process_resources")
    if not isinstance(resources, dict):
        resources = {}
        issues.append(
            _issue(
                "PROCESS_RESOURCES_MISSING",
                "process_resources 객체가 없습니다.",
            )
        )
    resource_status = resources.get("status")
    if resource_status not in RESOURCE_STATUSES:
        issues.append(
            _issue(
                "INVALID_PROCESS_RESOURCE_STATUS",
                f"process_resources.status={resource_status!r}",
            )
        )
    elif resource_status == "unavailable":
        issues.append(
            _issue(
                "PROCESS_RESOURCES_UNAVAILABLE",
                str(
                    resources.get("reason")
                    or "프로세스 자원 계측이 불가능합니다."
                ),
            )
        )
    if not isinstance(resources.get("reason"), str):
        issues.append(
            _issue(
                "INVALID_PROCESS_RESOURCE_REASON",
                "process_resources.reason은 문자열이어야 합니다.",
            )
        )
    sample_interval = resources.get("sample_interval_seconds")
    if not _is_number(sample_interval) or float(sample_interval) <= 0:
        issues.append(
            _issue(
                "INVALID_SAMPLE_INTERVAL",
                "sample_interval_seconds는 0보다 큰 숫자여야 합니다.",
            )
        )

    aggregate_metrics = resources.get("metrics")
    aggregate_metric_statuses: list[str] = []
    if not isinstance(aggregate_metrics, dict):
        issues.append(
            _issue(
                "AGGREGATE_METRICS_MISSING",
                "process_resources.metrics 객체가 없습니다.",
            )
        )
    else:
        for metric in METRIC_THRESHOLDS:
            error = _metric_structure_error(aggregate_metrics.get(metric))
            if error:
                issues.append(
                    _issue(
                        "AGGREGATE_METRIC_STRUCTURE_INVALID",
                        f"{metric}: {error}",
                    )
                )
            else:
                aggregate_metric_statuses.append(
                    aggregate_metrics[metric]["status"]
                )
    if (
        len(aggregate_metric_statuses) == len(METRIC_THRESHOLDS)
        and resource_status in RESOURCE_STATUSES
        and resource_status != _combined_status(aggregate_metric_statuses)
    ):
        issues.append(
            _issue(
                "PROCESS_RESOURCE_STATUS_MISMATCH",
                (
                    f"status={resource_status}, "
                    f"expected={_combined_status(aggregate_metric_statuses)}"
                ),
            )
        )

    processes_value = resources.get("processes")
    if not isinstance(processes_value, list):
        processes: list[Any] = []
        issues.append(
            _issue(
                "PROCESS_LIST_MISSING",
                "process_resources.processes 배열이 없습니다.",
            )
        )
    else:
        processes = processes_value

    expected_process_count = completed_cycles * len(STAGES)
    reported_process_count = resources.get("process_count")
    if reported_process_count != expected_process_count:
        issues.append(
            _issue(
                "PROCESS_COUNT_MISMATCH",
                (
                    f"보고된 process_count={reported_process_count!r}, "
                    f"예상값={expected_process_count}"
                ),
            )
        )
    if len(processes) != expected_process_count:
        issues.append(
            _issue(
                "PROCESS_LIST_COUNT_MISMATCH",
                (
                    f"processes 배열 길이={len(processes)}, "
                    f"예상값={expected_process_count}"
                ),
            )
        )

    stage_records: dict[str, dict[int, dict[str, Any]]] = {
        stage: {} for stage in STAGES
    }
    invalid_label_count = 0
    duplicate_count = 0
    invalid_process_count = 0
    invalid_pid_count = 0
    invalid_sample_count = 0
    invalid_process_status_count = 0
    missing_process_metrics_count = 0
    invalid_process_metric_count = 0
    process_status_mismatch_count = 0
    calculated_sample_count = 0
    for process in processes:
        if not isinstance(process, dict):
            invalid_process_count += 1
            invalid_label_count += 1
            continue

        pid = process.get("pid")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
        ):
            invalid_pid_count += 1

        process_sample_count = process.get("sample_count")
        if (
            not isinstance(process_sample_count, int)
            or isinstance(process_sample_count, bool)
            or process_sample_count < MIN_SAMPLES_PER_PROCESS
        ):
            invalid_sample_count += 1
        if (
            isinstance(process_sample_count, int)
            and not isinstance(process_sample_count, bool)
            and process_sample_count >= 0
        ):
            calculated_sample_count += process_sample_count

        process_status = process.get("status")
        if process_status not in RESOURCE_STATUSES:
            invalid_process_status_count += 1

        process_metrics = process.get("metrics")
        process_metric_statuses: list[str] = []
        if not isinstance(process_metrics, dict):
            missing_process_metrics_count += 1
        else:
            for metric in METRIC_THRESHOLDS:
                error = _metric_structure_error(process_metrics.get(metric))
                if error:
                    invalid_process_metric_count += 1
                else:
                    process_metric_statuses.append(
                        process_metrics[metric]["status"]
                    )
        if (
            len(process_metric_statuses) == len(METRIC_THRESHOLDS)
            and process_status in RESOURCE_STATUSES
            and process_status != _combined_status(process_metric_statuses)
        ):
            process_status_mismatch_count += 1

        label = process.get("label")
        match = (
            PROCESS_LABEL_PATTERN.fullmatch(label)
            if isinstance(label, str)
            else None
        )
        if match is None:
            invalid_label_count += 1
            continue
        cycle = int(match.group("cycle"))
        stage = match.group("stage")
        if not 1 <= cycle <= completed_cycles:
            invalid_label_count += 1
            continue
        if cycle in stage_records[stage]:
            duplicate_count += 1
            continue
        stage_records[stage][cycle] = process

    reported_sample_count = resources.get("sample_count")
    if (
        not isinstance(reported_sample_count, int)
        or isinstance(reported_sample_count, bool)
        or reported_sample_count < 0
    ):
        issues.append(
            _issue(
                "INVALID_AGGREGATE_SAMPLE_COUNT",
                f"process_resources.sample_count={reported_sample_count!r}",
            )
        )
    elif reported_sample_count != calculated_sample_count:
        issues.append(
            _issue(
                "AGGREGATE_SAMPLE_COUNT_MISMATCH",
                (
                    f"sample_count={reported_sample_count}, "
                    f"calculated={calculated_sample_count}"
                ),
            )
        )

    structural_counts = (
        (
            "INVALID_PROCESS_OBJECT",
            invalid_process_count,
            "객체가 아닌 process",
        ),
        ("INVALID_PROCESS_PID", invalid_pid_count, "올바르지 않은 PID"),
        (
            "INVALID_PROCESS_SAMPLE_COUNT",
            invalid_sample_count,
            "sample_count가 2 미만이거나 올바르지 않은 process",
        ),
        (
            "INVALID_PROCESS_STATUS",
            invalid_process_status_count,
            "status가 올바르지 않은 process",
        ),
        (
            "PROCESS_METRICS_MISSING",
            missing_process_metrics_count,
            "metrics 객체가 없는 process",
        ),
        (
            "PROCESS_METRIC_STRUCTURE_INVALID",
            invalid_process_metric_count,
            "구조가 올바르지 않은 process metric",
        ),
        (
            "PROCESS_STATUS_MISMATCH",
            process_status_mismatch_count,
            "metric status 조합과 status가 다른 process",
        ),
    )
    for code, count, description in structural_counts:
        if count:
            issues.append(
                _issue(code, f"{description}: {count}개")
            )

    if invalid_label_count:
        issues.append(
            _issue(
                "INVALID_PROCESS_LABEL",
                f"유효하지 않은 process label이 {invalid_label_count}개 있습니다.",
            )
        )
    if duplicate_count:
        issues.append(
            _issue(
                "DUPLICATE_STAGE_CYCLE",
                f"중복된 stage/cycle 항목이 {duplicate_count}개 있습니다.",
            )
        )

    denominator = completed_cycles if completed_cycles > 0 else 1
    expected_warmup_count = (
        min(10, max(3, math.ceil(completed_cycles * 0.05)))
        if completed_cycles > 0
        else 0
    )
    expected_stable_count = max(
        completed_cycles - expected_warmup_count,
        0,
    )
    expected_window_size = (
        min(20, max(5, math.ceil(expected_stable_count * 0.10)))
        if expected_stable_count > 0
        else 0
    )
    final_cycle_numbers = range(
        max(1, completed_cycles - expected_window_size + 1),
        completed_cycles + 1,
    )
    stages_quality: dict[str, Any] = {}
    for stage in STAGES:
        records = stage_records[stage]
        process_coverage = len(records) / denominator
        samples_ok = sum(
            1
            for process in records.values()
            if isinstance(process.get("sample_count"), int)
            and not isinstance(process.get("sample_count"), bool)
            and process["sample_count"] >= MIN_SAMPLES_PER_PROCESS
        )
        sample_coverage = samples_ok / denominator
        metric_coverage: dict[str, float] = {}
        final_window_coverage: dict[str, float] = {}
        for metric in METRIC_THRESHOLDS:
            valid_count = sum(
                1
                for process in records.values()
                if _metric_values(process, metric) is not None
            )
            metric_coverage[metric] = valid_count / denominator
            final_available_count = sum(
                1
                for cycle in final_cycle_numbers
                if cycle in records
                and _metric_values(records[cycle], metric) is not None
            )
            final_window_coverage[metric] = (
                final_available_count / expected_window_size
                if expected_window_size > 0
                else 0.0
            )

        if process_coverage < MIN_COVERAGE:
            issues.append(
                _issue(
                    "STAGE_COVERAGE_BELOW_MINIMUM",
                    (
                        f"{stage} process coverage={process_coverage:.3f}, "
                        f"minimum={MIN_COVERAGE:.2f}"
                    ),
                )
            )
        if sample_coverage < MIN_COVERAGE:
            issues.append(
                _issue(
                    "SAMPLE_COVERAGE_BELOW_MINIMUM",
                    (
                        f"{stage} sample coverage={sample_coverage:.3f}, "
                        f"minimum={MIN_COVERAGE:.2f}"
                    ),
                )
            )
        for metric, coverage in metric_coverage.items():
            if coverage < MIN_COVERAGE:
                issues.append(
                    _issue(
                        "METRIC_COVERAGE_BELOW_MINIMUM",
                        (
                            f"{stage}/{metric} coverage={coverage:.3f}, "
                            f"minimum={MIN_COVERAGE:.2f}"
                        ),
                    )
                )
        for metric, coverage in final_window_coverage.items():
            if coverage < 1.0:
                issues.append(
                    _issue(
                        "FINAL_ANALYSIS_WINDOW_INCOMPLETE",
                        (
                            f"{stage}/{metric} final window "
                            f"coverage={coverage:.3f}, required=1.000"
                        ),
                    )
                )

        stages_quality[stage] = {
            "process_coverage": round(process_coverage, 6),
            "sample_coverage": round(sample_coverage, 6),
            "metric_coverage": {
                metric: round(coverage, 6)
                for metric, coverage in metric_coverage.items()
            },
            "final_window_size": expected_window_size,
            "final_window_coverage": {
                metric: round(coverage, 6)
                for metric, coverage in final_window_coverage.items()
            },
        }

    return (
        {
            "status": "pass" if not issues else "failed",
            "minimum_cycles": MIN_VALID_CYCLES,
            "minimum_duration_minutes": valid_minimum_duration,
            "minimum_coverage": MIN_COVERAGE,
            "minimum_samples_per_process": MIN_SAMPLES_PER_PROCESS,
            "expected_uploaded_bytes": expected_uploaded_bytes,
            "expected_process_count": expected_process_count,
            "reported_process_count": reported_process_count,
            "process_list_count": len(processes),
            "reported_sample_count": reported_sample_count,
            "calculated_sample_count": calculated_sample_count,
            "stages": stages_quality,
            "issues": issues,
        },
        stage_records,
        completed_cycles,
        duration_seconds,
    )


def _analyze_metric(
    records: dict[int, dict[str, Any]],
    metric: str,
    *,
    duration_seconds: float,
    completed_cycles: int,
) -> dict[str, Any]:
    points = []
    for cycle, process in sorted(records.items()):
        values = _metric_values(process, metric)
        if values is not None:
            points.append((cycle, values))

    warmup_count = min(
        10,
        max(3, math.ceil(completed_cycles * 0.05)),
    )
    stable_points = [
        point for point in points if point[0] > warmup_count
    ]
    expected_stable_count = completed_cycles - warmup_count
    window_size = min(
        20,
        max(5, math.ceil(expected_stable_count * 0.10)),
    )
    baseline_points = stable_points[:window_size]
    final_points = stable_points[-window_size:]

    end_points = [(cycle, values["end"]) for cycle, values in stable_points]
    baseline_values = [values["end"] for _, values in baseline_points]
    final_values = [values["end"] for _, values in final_points]
    end_values = [values["end"] for _, values in stable_points]
    peak_values = [values["maximum"] for _, values in stable_points]
    increase_values = [values["increase"] for _, values in stable_points]

    baseline_median = float(statistics.median(baseline_values))
    final_median = float(statistics.median(final_values))
    median_end = float(statistics.median(end_values))
    baseline_mad = float(
        statistics.median(
            abs(value - baseline_median) for value in baseline_values
        )
    )
    noise = MAD_SCALE * baseline_mad
    absolute_change = final_median - baseline_median
    percent_change = (
        absolute_change / baseline_median
        if baseline_median != 0
        else None
    )

    slope, positive_pair_ratio = _theil_sen(end_points)
    cycle_span = end_points[-1][0] - end_points[0][0]
    projected_change = slope * cycle_span

    tail_points = end_points[len(end_points) // 2 :]
    tail_slope, tail_positive_pair_ratio = _theil_sen(tail_points)
    tail_cycle_span = tail_points[-1][0] - tail_points[0][0]
    tail_projected_change = tail_slope * tail_cycle_span

    threshold_config = METRIC_THRESHOLDS[metric]
    review_threshold = max(
        float(threshold_config["review_absolute"]),
        float(threshold_config["review_relative"]) * baseline_median,
        NOISE_MULTIPLIER * noise,
    )
    strong_threshold = max(
        float(threshold_config["strong_absolute"]),
        float(threshold_config["strong_relative"]) * baseline_median,
        NOISE_MULTIPLIER * noise,
    )
    final_window_dominance = sum(
        1
        for value in final_values
        if value > baseline_median + (review_threshold / 2)
    ) / len(final_values)

    conditions = {
        "change_threshold_met": absolute_change >= review_threshold,
        "projected_change_met": (
            projected_change
            >= review_threshold * MIN_PROJECTED_THRESHOLD_RATIO
        ),
        "positive_pair_ratio_met": (
            positive_pair_ratio >= MIN_POSITIVE_PAIR_RATIO
        ),
        "tail_slope_positive": tail_slope > 0,
        "tail_projected_change_met": (
            tail_projected_change
            >= review_threshold * MIN_TAIL_THRESHOLD_RATIO
        ),
        "final_window_dominance_met": (
            final_window_dominance >= MIN_FINAL_WINDOW_DOMINANCE
        ),
    }
    sustained_growth = all(conditions.values())
    maximum_peak = max(peak_values)
    peak_excursion = maximum_peak - baseline_median
    within_pid_median = float(statistics.median(increase_values))
    within_pid_p95 = _percentile(increase_values, 0.95)
    within_pid_maximum = max(increase_values)
    level_shift = (
        absolute_change >= review_threshold
        and final_window_dominance >= MIN_FINAL_WINDOW_DOMINANCE
    )
    strong_peak = peak_excursion >= strong_threshold
    within_pid_review = within_pid_p95 >= review_threshold
    within_pid_strong = within_pid_maximum >= strong_threshold

    anomaly_evidence = []
    if sustained_growth:
        anomaly_evidence.append(
            {
                "kind": "repeated_process_growth",
                "observed": _rounded(absolute_change),
                "threshold": _rounded(review_threshold),
            }
        )
    elif level_shift:
        anomaly_evidence.append(
            {
                "kind": "persistent_level_shift",
                "observed": _rounded(absolute_change),
                "threshold": _rounded(review_threshold),
            }
        )
    elif strong_peak:
        anomaly_evidence.append(
            {
                "kind": "strong_peak_excursion",
                "observed": _rounded(peak_excursion),
                "threshold": _rounded(strong_threshold),
            }
        )
    if within_pid_review or within_pid_strong:
        anomaly_evidence.append(
            {
                "kind": "within_pid_increase",
                "observed_p95": _rounded(within_pid_p95),
                "review_threshold": _rounded(review_threshold),
                "observed_maximum": _rounded(within_pid_maximum),
                "strong_threshold": _rounded(strong_threshold),
                "p95_review_met": within_pid_review,
                "maximum_strong_met": within_pid_strong,
            }
        )
    resource_anomaly = bool(anomaly_evidence)
    cycles_per_hour = completed_cycles / (duration_seconds / 3600)

    return {
        "verdict": (
            "REVIEW_RESOURCE_ANOMALY"
            if resource_anomaly
            else "PASS_NO_REPEATED_PROCESS_GROWTH"
        ),
        "valid_cycles": len(points),
        "warmup_cycles_excluded": warmup_count,
        "analyzed_cycles": len(stable_points),
        "window_size": window_size,
        "first_end": _rounded(end_values[0]),
        "last_end": _rounded(end_values[-1]),
        "median_end": _rounded(median_end),
        "maximum_peak": _rounded(maximum_peak),
        "peak_excursion_from_baseline": _rounded(peak_excursion),
        "baseline_median": _rounded(baseline_median),
        "final_median": _rounded(final_median),
        "absolute_change": _rounded(absolute_change),
        "percent_change": _rounded(percent_change),
        "baseline_mad": _rounded(baseline_mad),
        "scaled_mad_noise": _rounded(noise),
        "theil_sen_per_cycle": _rounded(slope),
        "approximate_theil_sen_per_hour": _rounded(
            slope * cycles_per_hour
        ),
        "projected_change": _rounded(projected_change),
        "positive_pair_ratio": _rounded(positive_pair_ratio),
        "tail_theil_sen_per_cycle": _rounded(tail_slope),
        "tail_projected_change": _rounded(tail_projected_change),
        "tail_positive_pair_ratio": _rounded(tail_positive_pair_ratio),
        "final_window_dominance": _rounded(final_window_dominance),
        "within_pid_increase": {
            "median": _rounded(within_pid_median),
            "p95": _rounded(within_pid_p95),
            "maximum": _rounded(within_pid_maximum),
        },
        "thresholds": {
            "review": _rounded(review_threshold),
            "strong": _rounded(strong_threshold),
            "absolute_review": threshold_config["review_absolute"],
            "relative_review": threshold_config["review_relative"],
            "absolute_strong": threshold_config["strong_absolute"],
            "relative_strong": threshold_config["strong_relative"],
            "noise_multiplier": NOISE_MULTIPLIER,
        },
        "conditions": conditions,
        "all_sustained_growth_conditions_met": sustained_growth,
        "anomaly_checks": {
            "persistent_level_shift": level_shift,
            "strong_peak_excursion": strong_peak,
            "within_pid_p95_review": within_pid_review,
            "within_pid_maximum_strong": within_pid_strong,
        },
        "anomaly_evidence": anomaly_evidence,
    }


def analyze_summary(
    payload: Any,
    minimum_duration_minutes: float = DEFAULT_MINIMUM_DURATION_MINUTES,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": "INCONCLUSIVE_TELEMETRY",
            "source": {},
            "data_quality": {
                "status": "failed",
                "issues": [
                    _issue(
                        "INVALID_SUMMARY_ROOT",
                        "입력 JSON 최상위 값은 객체여야 합니다.",
                    )
                ],
            },
            "stages": {},
            "review_findings": [],
            "limitations": LIMITATIONS,
        }

    source = _source_summary(payload)
    if payload.get("status") != "success":
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": "FUNCTIONAL_FAIL",
            "source": source,
            "data_quality": {
                "status": "not_evaluated",
                "issues": [
                    _issue(
                        "SOAK_STATUS_NOT_SUCCESS",
                        f"soak status={payload.get('status')!r}",
                    )
                ],
            },
            "stages": {},
            "review_findings": [],
            "limitations": LIMITATIONS,
        }

    (
        quality,
        stage_records,
        completed_cycles,
        duration_seconds,
    ) = evaluate_data_quality(
        payload,
        minimum_duration_minutes=minimum_duration_minutes,
    )
    if quality["status"] != "pass" or duration_seconds is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": "INCONCLUSIVE_TELEMETRY",
            "source": source,
            "data_quality": quality,
            "stages": {},
            "review_findings": [],
            "limitations": LIMITATIONS,
        }

    stages: dict[str, Any] = {}
    findings = []
    for stage in STAGES:
        metrics = {}
        for metric in METRIC_THRESHOLDS:
            analysis = _analyze_metric(
                stage_records[stage],
                metric,
                duration_seconds=duration_seconds,
                completed_cycles=completed_cycles,
            )
            metrics[metric] = analysis
            for evidence in analysis["anomaly_evidence"]:
                findings.append(
                    {
                        "stage": stage,
                        "metric": metric,
                        **evidence,
                    }
                )
        stages[stage] = {
            "valid_processes": len(stage_records[stage]),
            "metrics": metrics,
        }

    verdict = (
        "REVIEW_RESOURCE_ANOMALY"
        if findings
        else "PASS_NO_REPEATED_PROCESS_GROWTH"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict,
        "source": source,
        "data_quality": quality,
        "analysis_parameters": {
            "minimum_cycles": MIN_VALID_CYCLES,
            "minimum_duration_minutes": minimum_duration_minutes,
            "minimum_coverage": MIN_COVERAGE,
            "minimum_samples_per_process": MIN_SAMPLES_PER_PROCESS,
            "minimum_positive_pair_ratio": MIN_POSITIVE_PAIR_RATIO,
            "minimum_projected_threshold_ratio": (
                MIN_PROJECTED_THRESHOLD_RATIO
            ),
            "minimum_tail_threshold_ratio": MIN_TAIL_THRESHOLD_RATIO,
            "minimum_final_window_dominance": (
                MIN_FINAL_WINDOW_DOMINANCE
            ),
            "mad_scale": MAD_SCALE,
            "noise_multiplier": NOISE_MULTIPLIER,
        },
        "stages": stages,
        "review_findings": findings,
        "limitations": LIMITATIONS,
    }


def _input_error_result(code: str, detail: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": "INCONCLUSIVE_TELEMETRY",
        "source": {},
        "data_quality": {
            "status": "failed",
            "issues": [_issue(code, detail)],
        },
        "stages": {},
        "review_findings": [],
        "limitations": LIMITATIONS,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Windows stability soak JSON 자원 증가 분석"
    )
    parser.add_argument("summary_path", help="run_windows_stability_soak JSON 경로")
    parser.add_argument(
        "--minimum-duration-minutes",
        type=float,
        default=DEFAULT_MINIMUM_DURATION_MINUTES,
        help="최소 soak 시간(분), 0이면 duration gate 비활성화",
    )
    parser.add_argument("--output", default="", help="분석 JSON 저장 경로")
    args = parser.parse_args(argv)
    if not 0 <= args.minimum_duration_minutes <= MAXIMUM_DURATION_MINUTES:
        parser.error(
            "--minimum-duration-minutes는 "
            f"0~{MAXIMUM_DURATION_MINUTES:g} 범위여야 합니다."
        )
    return args


def _write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary_path = Path(args.summary_path)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except OSError as exc:
        result = _input_error_result(
            "INPUT_READ_FAILED",
            f"{type(exc).__name__}: {exc}",
        )
        exit_code = 2
    except json.JSONDecodeError as exc:
        result = _input_error_result(
            "INPUT_JSON_INVALID",
            f"line={exc.lineno}, column={exc.colno}",
        )
        exit_code = 2
    else:
        result = analyze_summary(
            payload,
            minimum_duration_minutes=args.minimum_duration_minutes,
        )
        exit_code = {
            "PASS_NO_REPEATED_PROCESS_GROWTH": 0,
            "REVIEW_RESOURCE_ANOMALY": 1,
            "INCONCLUSIVE_TELEMETRY": 2,
            "FUNCTIONAL_FAIL": 3,
        }[result["verdict"]]

    output_text = json.dumps(result, ensure_ascii=False, indent=2)
    print(output_text)
    if args.output:
        output_path = Path(args.output)
        try:
            if output_path.resolve() == summary_path.resolve():
                raise OSError("입력 JSON과 출력 JSON 경로가 같습니다.")
            _write_output(output_path, output_text)
        except OSError as exc:
            print(
                f"분석 JSON 저장 실패: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
