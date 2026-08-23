from __future__ import annotations

import json
import threading
from pathlib import Path

import tools.run_windows_stability_soak as soak_module
from tools.run_windows_stability_soak import (
    PROCESS_RESOURCE_METRICS,
    SOAK_UPLOAD_BYTES,
    ProcessResourceMonitor,
    ProcessResourceSample,
    SoakSummary,
    WindowsProcessResourceReader,
    build_multipart_upload,
    build_subprocess_environment,
    run_soak,
    summarize_process_resources,
    summarize_process_samples,
    write_soak_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_soak_workflow_runs_weekly_for_45_minutes():
    workflow = (PROJECT_ROOT / ".github/workflows/stability-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "schedule:" in workflow
    assert 'cron: "23 18 * * 0"' in workflow
    assert "SOAK_DURATION_MINUTES" in workflow
    assert "|| '45'" in workflow
    assert "run_windows_stability_soak.py" in workflow
    assert "analyze_windows_soak_summary.py" in workflow
    assert "--minimum-duration-minutes $env:SOAK_DURATION_MINUTES" in workflow
    assert "--output windows-soak-analysis.json" in workflow
    assert "render_windows_soak_summary.py" in workflow
    assert "actions/upload-artifact@" in workflow
    assert '"30"' in workflow
    assert '"60"' in workflow


def test_soak_config_uses_separate_loopback_web_and_probe_ports(tmp_path):
    path = write_soak_config(tmp_path, 18000, 15201)
    content = path.read_text(encoding="utf-8")

    assert "HOST=127.0.0.1" in content
    assert "PORT=18000" in content
    assert "BASE_URL=http://127.0.0.1:18000" in content
    assert "ENABLED=true" in content
    assert "PORT=15201" in content


def test_soak_multipart_contains_complete_file_payload():
    content = b"x" * SOAK_UPLOAD_BYTES
    body, boundary = build_multipart_upload("soak.txt", content)

    assert body.startswith(f"--{boundary}\r\n".encode("ascii"))
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode("ascii"))
    assert content in body


def test_soak_subprocesses_force_utf8_output():
    environment = build_subprocess_environment()

    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"


def test_process_resource_summary_contains_start_end_maximum_and_increase():
    samples = [
        ProcessResourceSample(
            values={
                "working_set_bytes": 100,
                "handle_count": 10,
                "thread_count": 3,
                "tcp_socket_count": 1,
            }
        ),
        ProcessResourceSample(
            values={
                "working_set_bytes": 180,
                "handle_count": 14,
                "thread_count": 5,
                "tcp_socket_count": 4,
            }
        ),
        ProcessResourceSample(
            values={
                "working_set_bytes": 150,
                "handle_count": 12,
                "thread_count": 4,
                "tcp_socket_count": 2,
            }
        ),
    ]

    process = summarize_process_samples(
        pid=1234,
        label="cycle-000001-initial",
        samples=samples,
    )
    report = summarize_process_resources(
        [process],
        sample_interval_seconds=1.0,
    )

    assert report["status"] == "available"
    assert report["process_count"] == 1
    assert report["sample_count"] == 3
    for metric in PROCESS_RESOURCE_METRICS:
        assert set(report["metrics"][metric]) == {
            "status",
            "start",
            "end",
            "maximum",
            "increase",
            "reason",
        }
    assert report["metrics"]["working_set_bytes"] == {
        "status": "available",
        "start": 100,
        "end": 150,
        "maximum": 180,
        "increase": 50,
        "reason": "",
    }
    assert process["metrics"]["tcp_socket_count"]["maximum"] == 4


def test_non_windows_resource_reader_reports_unavailable_without_raising(monkeypatch):
    monkeypatch.setattr(soak_module, "_IS_WINDOWS", False)

    sample = WindowsProcessResourceReader().sample(1234)
    process = summarize_process_samples(
        pid=1234,
        label="unsupported-platform",
        samples=[sample],
    )

    assert process["status"] == "unavailable"
    assert all(value is None for value in sample.values.values())
    assert all(
        "unavailable on this platform" in reason
        for reason in sample.errors.values()
    )


def test_windows_api_initialization_failure_reports_unavailable(monkeypatch):
    def fail_api_initialization():
        raise OSError("simulated WinDLL load failure")

    monkeypatch.setattr(soak_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(soak_module, "_WindowsApi", fail_api_initialization)

    sample = WindowsProcessResourceReader().sample(1234)

    assert all(value is None for value in sample.values.values())
    assert all(
        "simulated WinDLL load failure" in reason
        for reason in sample.errors.values()
    )


def test_resource_monitor_keeps_sampling_when_reader_fails():
    sampled_three_times = threading.Event()

    class FailingReader:
        def __init__(self):
            self.calls = 0

        def sample(self, _pid):
            self.calls += 1
            if self.calls >= 3:
                sampled_three_times.set()
            raise OSError("simulated API failure")

    reader = FailingReader()
    monitor = ProcessResourceMonitor(
        pid=1234,
        label="api-failure",
        reader=reader,
        interval_seconds=0.01,
    )

    monitor.start()
    assert sampled_three_times.wait(timeout=1)
    summary = monitor.stop()

    assert reader.calls >= 4
    assert summary["status"] == "unavailable"
    assert summary["sample_count"] == reader.calls
    assert "simulated API failure" in summary["metrics"]["handle_count"]["reason"]


def test_main_writes_process_resources_to_json_summary(tmp_path, monkeypatch, capsys):
    process_resources = {
        "status": "unavailable",
        "reason": "Windows process APIs are unavailable on this platform.",
        "metrics": {},
        "processes": [],
    }
    monkeypatch.setattr(
        soak_module,
        "run_soak",
        lambda **_kwargs: SoakSummary(
            status="success",
            duration_seconds=1.0,
            completed_cycles=1,
            uploaded_bytes=SOAK_UPLOAD_BYTES,
            tcp_self_checks=1,
            process_resources=process_resources,
        ),
    )
    summary_path = tmp_path / "soak-summary.json"

    assert (
        soak_module.main(
            [
                "--duration-minutes",
                "0.01",
                "--max-cycles",
                "1",
                "--summary-path",
                str(summary_path),
            ]
        )
        == 0
    )

    written = json.loads(summary_path.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert written["process_resources"] == process_resources
    assert printed == written


def test_single_soak_cycle_runs_real_upload_tcp_and_restart():
    summary = run_soak(duration_minutes=0.01, max_cycles=1)

    assert summary.status == "success"
    assert summary.completed_cycles == 1
    assert summary.uploaded_bytes == SOAK_UPLOAD_BYTES
    assert summary.tcp_self_checks == 1
    assert summary.process_resources["status"] in {
        "available",
        "partial",
        "unavailable",
    }
    assert summary.process_resources["process_count"] == 2
    assert summary.process_resources["sample_count"] >= 4
    assert set(summary.process_resources["metrics"]) == set(
        PROCESS_RESOURCE_METRICS
    )
    tcp_metric = summary.process_resources["metrics"]["tcp_socket_count"]
    if tcp_metric["status"] == "available":
        assert tcp_metric["maximum"] >= 2
    assert [
        process["label"]
        for process in summary.process_resources["processes"]
    ] == [
        "cycle-000001-initial",
        "cycle-000001-restart",
    ]
