from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "docs" / "PROJECT_DIAGNOSTIC_AND_IMPROVEMENT_PLAN_KO.md"
README_PATH = PROJECT_ROOT / "README.md"


def test_diagnostic_report_keeps_required_structure_and_evidence_labels() -> None:
    report = REPORT_PATH.read_text(encoding="utf-8")
    readme = README_PATH.read_text(encoding="utf-8")

    required_sections = [
        "## 1. 프로젝트 목적과 현재 구조 요약",
        "## 2. 전체 평가 점수",
        "## 3. 가장 잘 구현된 부분",
        "## 4. 가장 위험한 문제",
        "## 5. 초급 사용자 관점 문제점",
        "## 6. 관리자 관점 문제점",
        "## 7. 안정성 문제점",
        "## 8. P0, P1, P2 우선순위 개선 목록",
        "## 9. 단계별 개선 계획",
        "## 10. 테스트 및 검증 계획",
        "## 11. 수정하면 안 되는 기존 동작",
        "## 12. 최종 권고사항",
    ]
    positions = [report.index(heading) for heading in required_sections]
    assert positions == sorted(positions)

    assert (
        "| 우선순위 | 구분 | 문제 | 발생 조건 | 사용자 영향 | 원인 | "
        "관련 코드 | 개선 방법 | 검증 방법 | 예상 변경 범위 |"
    ) in report

    for evidence_label in (
        "코드에서 확인된 사실",
        "실행 또는 테스트가 필요한 사항",
        "합리적으로 예상되는 위험",
        "추가 확인이 필요한 사항",
    ):
        assert evidence_label in report

    for summary_heading in (
        "### 즉시 수정해야 할 항목 5개",
        "### 사용성 개선 효과가 가장 큰 항목 5개",
        "### 안정성 개선 효과가 가장 큰 항목 5개",
        "### 가장 먼저 수정할 파일 또는 모듈",
        "### 첫 번째 작업 단위에서 수행할 구체적인 변경사항",
    ):
        assert summary_heading in report

    assert "docs/PROJECT_DIAGNOSTIC_AND_IMPROVEMENT_PLAN_KO.md" in readme
