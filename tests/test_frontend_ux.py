from pathlib import Path


TEMPLATE = Path("templates/index.html")
NETWORK_CHECK_SCRIPT = Path("static/network_check.js")
SUSTAINED_SCRIPT = Path("static/network_sustained.js")
PROBE_SCRIPT = Path("static/network_probe.js")
OPERATIONS_DASHBOARD_SCRIPT = Path("static/operations_dashboard.js")
STYLESHEET = Path("static/style.css")


def test_template_exposes_accessible_tabs_statuses_and_progress():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert template.count('role="tablist"') == 2
    assert template.count('role="tab"') == 4
    assert 'aria-controls="upload-mode"' in template
    assert 'aria-controls="network-mode"' in template
    assert template.count('role="progressbar"') == 3
    assert template.count('aria-valuenow="0"') == 3
    assert template.count('class="status-indicator is-idle"') == 3
    assert template.count('aria-live="polite"') >= 8
    assert 'data-network-guidance' in template
    assert 'data-sustained-guidance' in template
    assert 'data-probe-guidance' in template
    assert '<caption class="visually-hidden">' in template
    assert '<th scope="col">' in template


def test_upload_form_prevents_duplicate_submission_and_restores_on_history_return():
    template = TEMPLATE.read_text(encoding="utf-8")
    script = NETWORK_CHECK_SCRIPT.read_text(encoding="utf-8")

    assert "data-upload-form" in template
    assert "data-upload-submit" in template
    assert 'aria-busy="false"' in template
    assert "if (submitting)" in script
    assert "event.preventDefault()" in script
    assert "submitButton.disabled = true" in script
    assert 'form.setAttribute("aria-busy", "true")' in script
    assert 'window.addEventListener("pageshow", resetSubmissionState)' in script


def test_measurement_scripts_keep_running_guards_and_update_accessible_state():
    scripts = {
        "quick": NETWORK_CHECK_SCRIPT.read_text(encoding="utf-8"),
        "sustained": SUSTAINED_SCRIPT.read_text(encoding="utf-8"),
        "probe": PROBE_SCRIPT.read_text(encoding="utf-8"),
    }

    for script in scripts.values():
        assert "setOperationState" in script
        assert 'setAttribute("aria-valuenow"' in script
        assert '"aria-valuetext"' in script
        assert "중복 실행할 수 없습니다." in script

    assert 'root.dataset.sustainedRunning === "true"' in scripts["quick"]
    assert 'root.dataset.simpleRunning === "true"' in scripts["sustained"]
    assert 'root.dataset.sustainedRunning === "true"' in scripts["probe"]
    assert 'completed.status === "success"' in scripts["sustained"]
    assert "완료 표시는 네트워크 정상 판정이 아닙니다." in scripts["quick"]
    assert "완료 표시는 네트워크 정상 판정이 아닙니다." in scripts["sustained"]
    assert "완료 표시는 네트워크 정상 판정이 아닙니다." in scripts["probe"]


def test_quick_full_measurement_preserves_completed_direction_on_partial_failure():
    script = NETWORK_CHECK_SCRIPT.read_text(encoding="utf-8")

    assert "function renderPartialResults(" in script
    assert "if (results.length > 0)" in script
    assert "renderPartialResults(results, action, errorMessage, cancelled)" in script
    assert 'resultList.innerHTML = ""' in script
    assert '"부분 완료"' in script
    assert "결과는 보존했습니다." in script
    assert "측정만 다시 실행하세요." in script


def test_sustained_full_measurement_preserves_valid_direction_on_partial_failure():
    script = SUSTAINED_SCRIPT.read_text(encoding="utf-8")

    assert "const directionEntries" in script
    assert "const partial = result.status !== \"success\"" in script
    assert "partial && !directionEntries.length" in script
    assert "부분 완료" in script
    assert "완료된 방향의 결과만 보존했습니다." in script
    assert "실패한 방향만 같은 조건으로 다시 측정하세요." in script


def test_styles_provide_keyboard_focus_and_non_color_status_cues():
    stylesheet = STYLESHEET.read_text(encoding="utf-8")

    assert "button:focus-visible" in stylesheet
    assert "[tabindex]:focus-visible" in stylesheet
    assert ".status-indicator.is-success" in stylesheet
    assert ".status-indicator.is-warning" in stylesheet
    assert ".status-indicator.is-error" in stylesheet
    assert ".operation-guidance.is-running" in stylesheet
    assert ".visually-hidden" in stylesheet


def test_operations_dashboard_uses_existing_read_only_apis_and_clear_sample_scope():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "operations_dashboard.js" in template
    assert 'data-health-url="{{ url_for(\'health_check\') }}"' in template
    assert 'data-summary-url="{{ url_for(\'operations_summary\') }}"' in template
    assert "최근 측정 표본" in template
    assert "장비 수나 전체 네트워크 상태를 뜻하지 않습니다." in template
    assert "최근 문제 우선" in template
    assert "권장 조치" in template
    assert "IP, PC 이름, 원시 오류는 표시하지 않습니다." in template
    assert 'data-operations-overall' in template
    assert 'data-operations-refresh' in template
    assert 'data-operations-issues' in template
    assert 'data-operations-changes' in template
    assert "과거 기록이며 현재 미조치 장애라는 뜻은 아닙니다." in template
    assert "조치 확인·담당자·해결 이력은 이 화면에서 기록하지 않습니다." in template
    assert 'aria-label="최근 실패와 취소 측정 표본"' in template
    assert "백그라운드 저장" in template
    assert "운영 요약 API 열기" in template


def test_operations_dashboard_refresh_is_bounded_and_renders_only_safe_fields():
    script = OPERATIONS_DASHBOARD_SCRIPT.read_text(encoding="utf-8")

    assert "REFRESH_INTERVAL_MS = 30_000" in script
    assert "REQUEST_TIMEOUT_MS = 10_000" in script
    assert "AbortController" in script
    assert "운영 상태 요청 시간 초과" in script
    assert "document.visibilityState === \"visible\"" in script
    assert "Promise.allSettled" in script
    assert "if (loading)" in script
    assert "ISSUE_LIMIT = 5" in script
    assert "priority = { problem: 0, warning: 1 }" in script
    assert "measurement_cancel_callback_failures" in script
    assert "장애 · 중단 처리 실패" in script
    assert 'checks.measurement.status === "degraded"' in script
    assert "checks.background_tasks.status === \"degraded\"" in script
    assert 'return "partial"' in script
    assert 'partial: "부분 장애"' in script
    assert "replaceChildren()" in script
    assert "function renderChanges" in script
    assert "status_changes" in script
    assert ".textContent =" in script
    assert ".innerHTML" not in script
    assert ".client_ip" not in script
    assert ".agent_hostname" not in script
    assert ".hostname" not in script
    assert ".owner_id" not in script
    assert ".recommended_action" not in script
    assert ".impact" not in script
    assert "일부 정보를 갱신하지 못해" in script
    assert "운영 상태를 불러오지 못했습니다." in script


def test_probe_polling_has_timeout_visibility_and_inflight_guards():
    script = PROBE_SCRIPT.read_text(encoding="utf-8")

    assert "REQUEST_TIMEOUT_MS = 10_000" in script
    assert "AbortController" in script
    assert "agentRefreshInFlight" in script
    assert 'document.visibilityState === "hidden"' in script
    assert 'document.visibilityState === "visible"' in script
    assert "agentSelect.replaceChildren()" in script
    assert "agentSignature !== lastAgentSignature" in script


def test_operations_dashboard_styles_keep_summary_and_issue_priority_scannable():
    stylesheet = STYLESHEET.read_text(encoding="utf-8")

    assert ".operations-dashboard" in stylesheet
    assert ".operations-summary-grid" in stylesheet
    assert ".operations-current-grid" in stylesheet
    assert ".operations-issue.is-problem" in stylesheet
    assert ".operations-recommendation" in stylesheet
