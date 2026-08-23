import json

from tools.render_windows_soak_summary import MAX_MARKDOWN_BYTES, main, render_markdown


def test_bounded_markdown_reports_functional_and_post_processing_results():
    summary = {
        "status": "success",
        "duration_seconds": 2700,
        "completed_cycles": 25,
        "uploaded_bytes": 123,
        "tcp_self_checks": 25,
    }
    analysis = {
        "verdict": "PASS_NO_REPEATED_PROCESS_GROWTH",
        "data_quality": {"status": "pass"},
        "review_findings": [
            {"stage": "initial", "metric": "working_set_bytes", "kind": "review", "observed": "가" * 10000}
            for _ in range(100)
        ],
    }
    markdown = render_markdown(
        summary,
        analysis,
        summary_state="available",
        analysis_state="available",
    )
    assert "| 기능 실행 | success |" in markdown
    assert "| 분석 후처리 | PASS_NO_REPEATED_PROCESS_GROWTH |" in markdown
    assert "원시 `windows-soak-summary.json`" in markdown
    assert len(markdown.encode("utf-8")) <= MAX_MARKDOWN_BYTES


def test_renderer_writes_evidence_even_when_analysis_is_missing(tmp_path):
    summary = tmp_path / "summary.json"
    output = tmp_path / "summary.md"
    summary.write_text(json.dumps({"status": "success"}), encoding="utf-8")
    assert main(["--summary", str(summary), "--analysis", str(tmp_path / "missing.json"), "--output", str(output)]) == 2
    assert "| 기능 실행 | success |" in output.read_text(encoding="utf-8")
    assert "| 분석 후처리 | missing |" in output.read_text(encoding="utf-8")
