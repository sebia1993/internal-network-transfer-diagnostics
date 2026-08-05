(function () {
  const REFRESH_INTERVAL_MS = 30_000;
  const REQUEST_TIMEOUT_MS = 10_000;
  const ISSUE_LIMIT = 5;
  const STATE_CLASSES = ["is-idle", "is-running", "is-success", "is-warning", "is-error"];
  const SOURCE_LABELS = {
    http_quick: "HTTP 데이터량",
    http_sustained: "HTTP 시간 기준",
    tcp_probe: "TCP 전송",
  };
  const DIRECTION_LABELS = {
    upload: "업로드",
    download: "다운로드",
    full: "전체",
  };
  const MEASUREMENT_KIND_LABELS = {
    http_quick: "HTTP 데이터량",
    http_sustained: "HTTP 시간 기준",
    tcp_probe: "TCP 전송",
  };
  const FAILURE_CATEGORIES = new Set([
    "시간 초과",
    "결과 저장",
    "연결",
    "인증",
    "중단",
    "측정 처리",
    "상세 원인 미기록",
  ]);
  const FAILURE_ACTIONS = {
    "시간 초과": "같은 조건으로 한 번만 다시 측정하고, 반복되면 경로 지연과 서버 부하를 확인하세요.",
    "결과 저장": "새 측정을 잠시 중지하고 서버 저장 공간, 쓰기 권한, 진단 로그를 확인하세요.",
    "연결": "서버 연결, 대상 클라이언트, 포트와 방화벽 상태를 확인하세요.",
    "인증": "최신 TCP 클라이언트를 다시 내려받아 연결 토큰을 갱신하세요.",
    "중단": "의도한 취소인지 확인하고 필요하면 같은 조건으로 다시 측정하세요.",
    "측정 처리": "진단 로그의 오류 코드를 확인한 뒤 같은 조건으로 한 번만 다시 측정하세요.",
    "상세 원인 미기록": "진단 로그와 결과 파일 상태를 확인한 뒤 다시 측정하세요.",
  };

  function setState(element, state) {
    STATE_CLASSES.forEach((className) => element.classList.remove(className));
    element.classList.add(`is-${state}`);
    element.dataset.state = state;
  }

  function setTextIfChanged(element, value) {
    if (element.textContent !== value) {
      element.textContent = value;
    }
  }

  function safeCount(value) {
    const count = Number(value);
    return Number.isInteger(count) && count >= 0 && count <= 1_000_000 ? count : null;
  }

  function formatCount(value) {
    const count = safeCount(value);
    return count === null ? "-" : count.toLocaleString("ko-KR");
  }

  function formatElapsed(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) {
      return "경과 시간 미확인";
    }
    if (value < 60) {
      return `${Math.floor(value)}초 경과`;
    }
    return `${Math.floor(value / 60)}분 경과`;
  }

  function statusLabel(status) {
    if (status === "ok") {
      return "정상";
    }
    if (status === "warning" || status === "busy") {
      return "주의";
    }
    if (status === "degraded") {
      return "장애";
    }
    return "미확인";
  }

  async function fetchJson(url) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error("운영 상태 요청 실패");
      }
      const payload = await response.json();
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new Error("운영 상태 응답 형식 오류");
      }
      return payload;
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error("운영 상태 요청 시간 초과");
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function initOperationsDashboard() {
    const root = document.querySelector("[data-operations-dashboard]");
    if (!root) {
      return;
    }

    const overall = root.querySelector("[data-operations-overall]");
    const message = root.querySelector("[data-operations-message]");
    const refreshButton = root.querySelector("[data-operations-refresh]");
    const recommendation = root.querySelector("[data-operations-recommendation]");
    const issueList = root.querySelector("[data-operations-issues]");
    const changeList = root.querySelector("[data-operations-changes]");
    const sampleSize = root.querySelector("[data-operations-sample-size]");
    const generatedAt = root.querySelector("[data-operations-generated-at]");
    const countElements = new Map(
      Array.from(root.querySelectorAll("[data-operations-count]"))
        .map((element) => [element.dataset.operationsCount, element])
    );
    const currentCards = new Map(
      Array.from(root.querySelectorAll("[data-operations-current-card]"))
        .map((element) => [element.dataset.operationsCurrentCard, element])
    );
    const currentValues = new Map(
      Array.from(root.querySelectorAll("[data-operations-current]"))
        .map((element) => [element.dataset.operationsCurrent, element])
    );
    const currentDetails = new Map(
      Array.from(root.querySelectorAll("[data-operations-current-detail]"))
        .map((element) => [element.dataset.operationsCurrentDetail, element])
    );
    const technicalValues = new Map(
      Array.from(root.querySelectorAll("[data-operations-technical]"))
        .map((element) => [element.dataset.operationsTechnical, element])
    );

    let loading = false;
    let lastAttemptAt = 0;
    let lastHealth = null;
    let lastSummary = null;
    let lastIssueSignature = "";
    let lastChangeSignature = "";

    function setCurrent(name, state, value, detail) {
      setState(currentCards.get(name), state);
      currentValues.get(name).textContent = value;
      currentDetails.get(name).textContent = detail;
    }

    function renderHealth(health) {
      const checks = health && typeof health.checks === "object" ? health.checks : {};
      const storage = checks.storage && typeof checks.storage === "object" ? checks.storage : {};
      const csv = checks.csv && typeof checks.csv === "object" ? checks.csv : {};
      const tcp = checks.tcp_probe && typeof checks.tcp_probe === "object" ? checks.tcp_probe : {};
      const background = checks.background_tasks && typeof checks.background_tasks === "object"
        ? checks.background_tasks
        : {};

      technicalValues.get("storage").textContent = statusLabel(storage.status);
      technicalValues.get("csv").textContent = statusLabel(csv.status);
      if (tcp.enabled === false) {
        technicalValues.get("tcp").textContent = "사용 안 함";
      } else {
        technicalValues.get("tcp").textContent = tcp.available === true ? "정상" : "장애";
      }
      technicalValues.get("background").textContent =
        background.status === "ok"
          ? "정상"
          : background.status === "degraded"
            ? `확인 필요 · ${formatCount(background.failure_count)}건`
            : "미확인";
    }

    function renderHealthUnavailable() {
      technicalValues.forEach((element) => {
        element.textContent = "미확인";
      });
    }

    function renderCounts(summary) {
      const counts = summary && typeof summary.counts === "object" ? summary.counts : {};
      countElements.forEach((element, level) => {
        element.textContent = formatCount(counts[level]);
      });
      sampleSize.textContent = formatCount(summary && summary.sample_size);
      generatedAt.textContent = String(summary && summary.generated_at || "-");
    }

    function renderSummaryUnavailable() {
      countElements.forEach((element) => {
        element.textContent = "-";
      });
      sampleSize.textContent = "-";
      generatedAt.textContent = "-";
      if (lastIssueSignature === "unavailable") {
        return;
      }
      lastIssueSignature = "unavailable";
      issueList.replaceChildren();
      const unavailableItem = document.createElement("li");
      unavailableItem.className = "empty";
      unavailableItem.textContent = "최근 문제 목록을 불러오지 못했습니다. 서버 연결을 확인한 뒤 다시 시도하세요.";
      issueList.appendChild(unavailableItem);
      if (lastChangeSignature !== "unavailable") {
        lastChangeSignature = "unavailable";
        changeList.replaceChildren();
        const changeUnavailable = document.createElement("li");
        changeUnavailable.className = "empty";
        changeUnavailable.textContent = "최근 상태 변경을 불러오지 못했습니다.";
        changeList.appendChild(changeUnavailable);
      }
    }

    function renderCurrent(summary, health) {
      const current = summary && typeof summary.current === "object" ? summary.current : {};
      const healthChecks = health && typeof health.checks === "object" ? health.checks : {};
      const uploadHealth = healthChecks.file_uploads && typeof healthChecks.file_uploads === "object"
        ? healthChecks.file_uploads
        : {};
      const cancellationFailures = safeCount(
        current.measurement_cancel_callback_failures
      );

      if (cancellationFailures !== null && cancellationFailures > 0) {
        setCurrent(
          "measurement",
          "error",
          "장애 · 중단 처리 실패",
          `결과 저장 또는 정리 실패 ${cancellationFailures}건 · 진단 로그 확인 필요`
        );
      } else if (current.measurement_active === true) {
        const kind = MEASUREMENT_KIND_LABELS[current.measurement_kind] || "네트워크 측정";
        const longRunning = current.measurement_long_running === true;
        setCurrent(
          "measurement",
          longRunning ? "warning" : "running",
          longRunning ? "주의 · 장시간 실행" : "진행 중",
          `${kind} · ${formatElapsed(current.measurement_age_seconds)}`
        );
      } else {
        setCurrent("measurement", "success", "대기", "실행 중인 네트워크 측정 없음");
      }

      const activeUploads = safeCount(current.active_file_uploads);
      const maxUploads = safeCount(uploadHealth.max_concurrent_uploads);
      if (current.file_uploads_at_capacity === true) {
        setCurrent(
          "uploads",
          "warning",
          "주의 · 접수 한도",
          `${activeUploads === null ? "-" : activeUploads}건 진행 중`
        );
      } else if (activeUploads !== null && activeUploads > 0) {
        setCurrent(
          "uploads",
          "running",
          "진행 중",
          `${activeUploads}${maxUploads === null ? "" : ` / ${maxUploads}`}건`
        );
      } else {
        setCurrent("uploads", "success", "대기", "진행 중인 파일 업로드 없음");
      }

      if (current.tcp_probe_enabled === false) {
        setCurrent("tcp", "idle", "사용 안 함", "설정에서 TCP 측정 기능 비활성");
      } else if (current.tcp_probe_available === true) {
        setCurrent("tcp", "success", "정상", "TCP 측정 서비스 사용 가능");
      } else {
        setCurrent("tcp", "error", "장애", "TCP 측정 서비스를 사용할 수 없음");
      }
    }

    function normalizedIssues(summary) {
      const issues = Array.isArray(summary && summary.recent_issues)
        ? summary.recent_issues
        : [];
      const priority = { problem: 0, warning: 1 };
      return issues
        .filter((item) => item && (item.level === "problem" || item.level === "warning"))
        .sort((left, right) => {
          const levelDifference = priority[left.level] - priority[right.level];
          if (levelDifference !== 0) {
            return levelDifference;
          }
          return String(right.timestamp || "").localeCompare(String(left.timestamp || ""));
        })
        .slice(0, ISSUE_LIMIT);
    }

    function renderIssues(summary) {
      const issues = normalizedIssues(summary);
      const signature = JSON.stringify(
        issues.map((item) => [
          item.level,
          item.source,
          item.direction,
          item.timestamp,
          item.failure_category,
        ])
      );
      if (signature === lastIssueSignature) {
        return;
      }
      lastIssueSignature = signature;
      issueList.replaceChildren();
      if (!issues.length) {
        const emptyItem = document.createElement("li");
        emptyItem.className = "empty";
        emptyItem.textContent = "최근 측정 표본에 취소 또는 실패 기록이 없습니다.";
        issueList.appendChild(emptyItem);
        return;
      }

      issues.forEach((item) => {
        const level = item.level === "problem" ? "problem" : "warning";
        const source = SOURCE_LABELS[item.source] || "측정";
        const direction = DIRECTION_LABELS[item.direction] || "방향 미기록";
        const category = FAILURE_CATEGORIES.has(item.failure_category)
          ? item.failure_category
          : "측정 처리";
        const listItem = document.createElement("li");
        listItem.className = `operations-issue is-${level}`;

        const heading = document.createElement("strong");
        heading.textContent = `${level === "problem" ? "실패" : "취소"} · ${source} · ${direction}`;
        const metadata = document.createElement("span");
        metadata.textContent = `${String(item.timestamp || "시각 미기록")} · 원인 분류 ${category}`;
        const impact = document.createElement("p");
        impact.textContent = level === "problem"
          ? "영향: 측정이 완료되지 않아 현재 상태 판단에 사용할 수 없습니다."
          : "영향: 측정이 중단되어 결과 판단에 사용할 수 없습니다.";
        const action = document.createElement("p");
        action.textContent = `권장 조치: ${FAILURE_ACTIONS[category]}`;

        listItem.append(heading, metadata, impact, action);
        issueList.appendChild(listItem);
      });
    }

    function renderChanges(summary) {
      const changes = Array.isArray(summary && summary.status_changes)
        ? summary.status_changes.slice(0, ISSUE_LIMIT)
        : [];
      const signature = JSON.stringify(
        changes.map((item) => [
          item.source,
          item.direction,
          item.timestamp,
          item.from_status_label,
          item.to_status_label,
        ])
      );
      if (signature === lastChangeSignature) {
        return;
      }
      lastChangeSignature = signature;
      changeList.replaceChildren();
      if (!changes.length) {
        const emptyItem = document.createElement("li");
        emptyItem.className = "empty";
        emptyItem.textContent = "최근 표본에서 완료·취소·실패 상태 변경이 없습니다.";
        changeList.appendChild(emptyItem);
        return;
      }
      changes.forEach((item) => {
        const source = SOURCE_LABELS[item.source] || "측정";
        const direction = DIRECTION_LABELS[item.direction] || "방향 미기록";
        const listItem = document.createElement("li");
        listItem.className = "operations-change";
        const heading = document.createElement("strong");
        heading.textContent = `${source} · ${direction}`;
        const detail = document.createElement("span");
        detail.textContent = (
          `${String(item.from_status_label || "미기록")} → ` +
          `${String(item.to_status_label || "미기록")} · ` +
          `${String(item.timestamp || "시각 미기록")}`
        );
        listItem.append(heading, detail);
        changeList.appendChild(listItem);
      });
    }

    function overallState(health, summary, freshness) {
      if (!health && !summary) {
        return "error";
      }
      const checks = health && typeof health.checks === "object" ? health.checks : {};
      const storageProblem = checks.storage && checks.storage.status !== "ok";
      const csvProblem = checks.csv && checks.csv.status !== "ok";
      const tcpProblem = checks.tcp_probe &&
        checks.tcp_probe.enabled !== false &&
        checks.tcp_probe.status !== "ok";
      const measurementProblem = checks.measurement &&
        checks.measurement.status === "degraded";
      const backgroundProblem = checks.background_tasks &&
        checks.background_tasks.status === "degraded";
      if (storageProblem || csvProblem || measurementProblem || backgroundProblem) {
        return "error";
      }
      if (tcpProblem) {
        return "partial";
      }
      const current = summary && typeof summary.current === "object" ? summary.current : {};
      if (current.tcp_probe_enabled === true && current.tcp_probe_available !== true) {
        return "partial";
      }
      if (
        !freshness.health ||
        !freshness.summary ||
        (health && health.status !== "ok") ||
        current.measurement_long_running === true ||
        current.file_uploads_at_capacity === true ||
        (Array.isArray(summary && summary.unavailable_sources) && summary.unavailable_sources.length > 0)
      ) {
        return "warning";
      }
      return "success";
    }

    function renderOverall(health, summary, freshness) {
      const state = overallState(health, summary, freshness);
      const labels = {
        success: "사용 가능",
        warning: "확인 필요",
        partial: "부분 장애",
        error: "장애",
      };
      setTextIfChanged(overall, labels[state]);
      setState(overall, state === "partial" ? "warning" : state);

      const actions = [];
      const checks = health && typeof health.checks === "object" ? health.checks : {};
      if (checks.storage && checks.storage.status !== "ok") {
        actions.push("저장 공간과 쓰기 권한을 확인하세요.");
      }
      if (checks.csv && checks.csv.status !== "ok") {
        actions.push("결과 파일 상태를 확인하고 새 측정을 잠시 중지하세요.");
      }
      if (checks.background_tasks && checks.background_tasks.status !== "ok") {
        actions.push(
          `백그라운드 저장 또는 보관 실패 ${formatCount(checks.background_tasks.failure_count)}건이 기록됐습니다. 진단 로그를 확인하세요.`
        );
      }
      const current = summary && typeof summary.current === "object" ? summary.current : {};
      if (current.tcp_probe_enabled === true && current.tcp_probe_available !== true) {
        actions.push("TCP 측정 서비스와 방화벽 상태를 확인하세요.");
      }
      if (current.measurement_long_running === true) {
        actions.push("장시간 실행 중인 측정의 취소 또는 종료 여부를 확인하세요.");
      }
      if (
        safeCount(current.measurement_cancel_callback_failures) !== null &&
        safeCount(current.measurement_cancel_callback_failures) > 0
      ) {
        actions.push(
          "측정 강제 중단 중 결과 저장 또는 정리에 실패했습니다. 진단 로그를 확인하고 서버를 재시작하세요."
        );
      }
      if (current.file_uploads_at_capacity === true) {
        actions.push("진행 중인 파일 업로드가 끝난 뒤 새 업로드를 시작하세요.");
      }
      if (
        Array.isArray(summary && summary.unavailable_sources) &&
        summary.unavailable_sources.length > 0
      ) {
        actions.push("일부 측정 이력을 읽지 못했습니다. 결과 파일 상태를 확인하세요.");
      }
      if (!freshness.health || !freshness.summary) {
        actions.push("일부 정보를 갱신하지 못했습니다. API 링크 또는 서버 연결을 확인하세요.");
      }
      if (!actions.length) {
        const problemCount = safeCount(summary && summary.counts && summary.counts.problem) || 0;
        actions.push(
          problemCount > 0
            ? "현재 서버 기능은 사용할 수 있습니다. 최근 실패 표본의 권장 조치를 검토하세요."
            : "현재 즉시 조치할 서버 문제는 없습니다. 필요하면 최근 측정값과 비교하세요."
        );
      }
      setTextIfChanged(recommendation, `${labels[state]} · ${actions.join(" ")}`);
      setState(recommendation, state === "partial" ? "warning" : state);
    }

    function renderUnavailableCurrent() {
      setCurrent("measurement", "warning", "미확인", "운영 요약 API 연결 확인 필요");
      setCurrent("uploads", "warning", "미확인", "운영 요약 API 연결 확인 필요");
      setCurrent("tcp", "warning", "미확인", "운영 요약 API 연결 확인 필요");
    }

    async function refreshDashboard() {
      if (loading) {
        return;
      }
      loading = true;
      lastAttemptAt = Date.now();
      root.setAttribute("aria-busy", "true");
      refreshButton.disabled = true;

      try {
        const [healthResult, summaryResult] = await Promise.allSettled([
          fetchJson(root.dataset.healthUrl),
          fetchJson(root.dataset.summaryUrl),
        ]);
        const freshness = {
          health: healthResult.status === "fulfilled",
          summary: summaryResult.status === "fulfilled",
        };
        if (freshness.health) {
          lastHealth = healthResult.value;
          renderHealth(lastHealth);
        } else if (!lastHealth) {
          renderHealthUnavailable();
        }
        if (freshness.summary) {
          lastSummary = summaryResult.value;
          renderCounts(lastSummary);
          renderIssues(lastSummary);
          renderChanges(lastSummary);
        } else if (!lastSummary) {
          renderSummaryUnavailable();
        }
        if (lastSummary) {
          renderCurrent(lastSummary, lastHealth);
        } else {
          renderUnavailableCurrent();
        }
        renderOverall(lastHealth, lastSummary, freshness);

        if (freshness.health && freshness.summary) {
          setTextIfChanged(
            message,
            "최신 상태입니다. 화면이 열려 있을 때 30초마다 갱신합니다."
          );
          setState(message, "success");
        } else if (freshness.health || freshness.summary) {
          setTextIfChanged(
            message,
            "주의 · 일부 정보를 갱신하지 못해 확인 가능한 항목만 표시합니다. 30초 후 다시 시도합니다."
          );
          setState(message, "warning");
        } else {
          setTextIfChanged(
            message,
            "장애 · 운영 상태를 불러오지 못했습니다. 서버 연결을 확인하고 지금 새로고침을 누르세요."
          );
          setState(message, "error");
        }
      } finally {
        loading = false;
        root.setAttribute("aria-busy", "false");
        refreshButton.disabled = false;
      }
    }

    refreshButton.addEventListener("click", refreshDashboard);
    document.addEventListener("visibilitychange", () => {
      if (
        document.visibilityState === "visible" &&
        Date.now() - lastAttemptAt >= REFRESH_INTERVAL_MS
      ) {
        refreshDashboard();
      }
    });
    window.setInterval(() => {
      if (document.visibilityState === "visible") {
        refreshDashboard();
      }
    }, REFRESH_INTERVAL_MS);
    refreshDashboard();
  }

  initOperationsDashboard();
})();
