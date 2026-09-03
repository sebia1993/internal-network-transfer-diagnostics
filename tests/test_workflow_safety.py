from pathlib import Path

from app_version import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_pr_validation_cannot_mask_native_failures_and_runs_on_main():
    workflow = (ROOT / ".github/workflows/pr-validation.yml").read_text(encoding="utf-8")
    assert "- main" in workflow
    assert '"portfolio/**"' in workflow
    assert "Assert-NativeSuccess" in workflow
    commands = (
        "python -m compileall",
        "node --check static/security.js",
        "node --check static/network_check.js",
        "node --check static/network_sustained.js",
        "node --check static/network_probe.js",
        "node --check static/throughput_chart.js",
        "node --check static/operations_dashboard.js",
        "python -m pytest -q",
        "python tools/scan_tracked_secrets.py",
        "python tools/run_stability_fault_suite.py",
        "python -m pip check",
    )
    for command in commands:
        command_index = workflow.index(command)
        next_guard = workflow.index("Assert-NativeSuccess", command_index)
        next_native = min(
            (workflow.find(candidate, command_index + len(command)) for candidate in commands),
            key=lambda value: float("inf") if value < 0 else value,
        )
        assert next_native < 0 or next_guard < next_native
    assert APP_VERSION in workflow


def test_soak_keeps_step_summary_bounded_and_raw_json_in_artifact():
    workflow = (ROOT / ".github/workflows/stability-windows.yml").read_text(encoding="utf-8")
    assert 'PYTHONUTF8: "1"' in workflow
    assert 'PYTHONIOENCODING: "utf-8"' in workflow
    assert "render_windows_soak_summary.py" in workflow
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1"
        in workflow
    )
    assert "windows-soak-summary.json" in workflow
    assert "windows-soak-analysis.json" in workflow
    assert "Get-Content windows-soak-summary.json >> $env:GITHUB_STEP_SUMMARY" not in workflow
    assert "Get-Content windows-soak-analysis.json >> $env:GITHUB_STEP_SUMMARY" not in workflow
    assert "Functional soak failed" in workflow
    assert "Soak analysis failed" in workflow
