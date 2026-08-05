(function () {
  const TELEMETRY_UNAVAILABLE = "운영체제에서 제공하지 않음";
  const REQUEST_TIMEOUT_MS = 10_000;

  function formatSpeed(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return "-";
    return `${number.toFixed(1)} Mbps / ${(number / 8).toFixed(1)} MB/s`;
  }

  function formatBytes(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return "-";
    if (number >= 1024 * 1024) return `${(number / 1024 / 1024).toFixed(1)} MB`;
    if (number >= 1024) return `${(number / 1024).toFixed(1)} KB`;
    return `${number.toFixed(0)} B`;
  }

  function directionLabel(direction) {
    return direction === "upload" ? "업로드" : "다운로드";
  }

  function directionPath(direction) {
    return direction === "upload" ? "측정 PC → 서버" : "서버 → 측정 PC";
  }

  function formatRtt(telemetry) {
    if (!telemetry || !telemetry.available) return TELEMETRY_UNAVAILABLE;
    const rtt = Number(telemetry.rtt_us) / 1000;
    const minimum = Number(telemetry.min_rtt_us) / 1000;
    if (!Number.isFinite(rtt) || !Number.isFinite(minimum)) return TELEMETRY_UNAVAILABLE;
    return `${rtt.toFixed(2)} ms (최소 ${minimum.toFixed(2)} ms)`;
  }

  function formatRetransmission(sender, telemetry) {
    if (!telemetry || !telemetry.available) return TELEMETRY_UNAVAILABLE;
    const retransmittedBytes = Number(telemetry.bytes_retrans);
    const sentBytes = Number(sender && sender.bytes);
    if (!Number.isFinite(retransmittedBytes) || retransmittedBytes < 0) return TELEMETRY_UNAVAILABLE;
    if (!Number.isFinite(sentBytes) || sentBytes <= 0) return formatBytes(retransmittedBytes);
    return `${formatBytes(retransmittedBytes)} (전체 송신량의 ${(retransmittedBytes / sentBytes * 100).toFixed(2)}%)`;
  }

  function formatDirectionDifference(results) {
    const upload = Number(results.upload && results.upload.receiver && results.upload.receiver.average_mbps);
    const download = Number(results.download && results.download.receiver && results.download.receiver.average_mbps);
    const maximum = Math.max(upload, download);
    if (!Number.isFinite(upload) || !Number.isFinite(download) || maximum <= 0) return "";
    return `업로드·다운로드 평균 속도 차이: ${(Math.abs(upload - download) / maximum * 100).toFixed(1)}%`;
  }

  function setOperationState(element, state) {
    element.classList.remove("is-idle", "is-running", "is-success", "is-warning", "is-error");
    element.classList.add(`is-${state}`);
    element.dataset.state = state;
  }

  function setTextIfChanged(element, message) {
    if (element.textContent !== message) {
      element.textContent = message;
    }
  }

  async function fetchJson(url, options, label) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let response;
    try {
      response = await fetch(url, {
        cache: "no-store",
        ...options,
        signal: controller.signal,
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error(`${label} 요청 시간이 초과되었습니다. 다시 시도하세요.`);
      }
      throw new Error(`${label} 요청에 실패했습니다. (${error.message})`);
    } finally {
      window.clearTimeout(timeout);
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `${label} 요청이 실패했습니다.`);
    return payload;
  }

  function createProbeProgress(progressBar) {
    const progressTrack = progressBar.closest('[role="progressbar"]');
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let animationFrameId = 0;
    let currentPercent = 0;
    let fromPercent = 0;
    let targetPercent = 0;
    let startedAt = 0;
    let durationMs = 650;

    function render() {
      progressBar.style.transform = `scaleX(${(currentPercent / 100).toFixed(4)})`;
      progressTrack.setAttribute("aria-valuenow", currentPercent.toFixed(1));
      progressTrack.setAttribute(
        "aria-valuetext",
        `TCP 전송 성능 측정 · ${currentPercent.toFixed(1)}%`
      );
    }

    function cancelAnimation() {
      if (animationFrameId) {
        window.cancelAnimationFrame(animationFrameId);
        animationFrameId = 0;
      }
    }

    function sample(timestamp) {
      if (!animationFrameId || durationMs <= 0) return currentPercent;
      const fraction = Math.min(1, Math.max(0, (timestamp - startedAt) / durationMs));
      currentPercent = fromPercent + (targetPercent - fromPercent) * fraction;
      return currentPercent;
    }

    function animateTo(nextPercent, nextDuration) {
      const now = performance.now();
      sample(now);
      cancelAnimation();
      const bounded = Math.max(currentPercent, Math.min(100, Number(nextPercent) || 0));
      if (reducedMotion || bounded <= currentPercent) {
        currentPercent = bounded;
        targetPercent = bounded;
        render();
        return;
      }
      fromPercent = currentPercent;
      targetPercent = bounded;
      startedAt = now;
      durationMs = nextDuration;

      function tick(timestamp) {
        sample(timestamp);
        render();
        if (currentPercent < targetPercent) {
          animationFrameId = window.requestAnimationFrame(tick);
        } else {
          animationFrameId = 0;
        }
      }

      animationFrameId = window.requestAnimationFrame(tick);
    }

    function reset() {
      cancelAnimation();
      currentPercent = 0;
      fromPercent = 0;
      targetPercent = 0;
      startedAt = 0;
      render();
      progressTrack.setAttribute("aria-valuetext", "대기 · 0%");
    }

    function stop() {
      sample(performance.now());
      cancelAnimation();
      render();
    }

    function update(serverPercent, status, persistenceComplete) {
      if (status === "completed" && persistenceComplete !== false) {
        animateTo(100, 300);
        return;
      }
      if (status === "cancelled" || status === "failed") {
        animateTo(Math.min(99.5, Number(serverPercent) || 0), 300);
        return;
      }
      animateTo(Math.min(99.5, Number(serverPercent) || 0), 650);
    }

    return { reset, stop, update };
  }

  function initProbe() {
    const root = document.querySelector("[data-network-check]");
    if (!root || !root.dataset.probeStatusUrl) return;

    const serviceStatus = root.querySelector("[data-probe-service-status]");
    const packageLink = root.querySelector("[data-probe-client-package]");
    const packageAddress = root.querySelector("[data-probe-client-package-address]");
    const packageHash = root.querySelector("[data-probe-client-package-hash]");
    const agentSelect = root.querySelector("[data-probe-agent]");
    const clientReadiness = root.querySelector("[data-probe-client-readiness]");
    const durationSelect = root.querySelector("[data-probe-duration]");
    const fourStreamToggle = root.querySelector("[data-probe-four-stream]");
    const advancedSummary = root.querySelector("[data-probe-advanced-summary]");
    const actionButtons = root.querySelectorAll("[data-probe-action]");
    const cancelButton = root.querySelector("[data-probe-cancel]");
    const modeButtons = root.querySelectorAll("[data-measurement-mode]");
    const statusText = root.querySelector("[data-probe-status]");
    const guidance = root.querySelector("[data-probe-guidance]");
    const phaseText = root.querySelector("[data-probe-phase]");
    const progressBar = root.querySelector("[data-probe-progress-bar]");
    const summaryList = root.querySelector("[data-probe-summary]");
    const chartPanel = root.querySelector("[data-probe-chart-panel]");
    const chartCards = new Map(
      Array.from(root.querySelectorAll("[data-probe-chart-card]"))
        .map((card) => [card.dataset.probeChartCard, card])
    );
    const technicalDetails = root.querySelector("[data-probe-technical-details]");
    const conditionsText = root.querySelector("[data-probe-conditions]");
    const clientText = root.querySelector("[data-probe-client]");
    const detailList = root.querySelector("[data-probe-detail-list]");
    const excelLink = root.querySelector("[data-probe-excel]");
    const criterionButtons = root.querySelectorAll("[data-http-criterion]");
    const resultPanel = statusText.closest("[data-measurement-result]");

    let serviceAvailable = false;
    let running = false;
    let activeSessionId = "";
    let agentRefreshInFlight = false;
    let lastAgentSignature = "";
    let graphSeries = { upload: [], download: [] };
    let chartAverages = { upload: null, download: null };
    let agentsById = new Map();
    const progress = createProbeProgress(progressBar);
    const chartRenderers = new Map();
    root.querySelectorAll("[data-probe-chart]").forEach((canvas) => {
      const direction = canvas.dataset.probeChart;
      const renderer = window.InternalUploadThroughputChart && window.InternalUploadThroughputChart.create(
        canvas,
        {
          color: direction === "upload" ? "#246b54" : "#c15f2e",
          fillColor: direction === "upload" ? "rgba(36, 107, 84, 0.10)" : "rgba(193, 95, 46, 0.10)",
          label: direction === "upload" ? "TCP 업로드" : "TCP 다운로드",
        }
      );
      if (renderer) chartRenderers.set(direction, renderer);
    });

    function syncCharts() {
      let visibleCount = 0;
      ["upload", "download"].forEach((direction) => {
        const series = graphSeries[direction];
        const visible = Array.isArray(series) && series.length > 0;
        const card = chartCards.get(direction);
        if (card) card.hidden = !visible;
        const renderer = chartRenderers.get(direction);
        if (renderer) renderer.setData(visible ? series : [], { averageMbps: chartAverages[direction] });
        if (visible) visibleCount += 1;
      });
      chartPanel.hidden = visibleCount === 0;
    }

    function updateAdvancedSummary() {
      advancedSummary.textContent = fourStreamToggle.checked
        ? "고급 비교 측정 · 4개 스트림 사용 중"
        : "고급 비교 측정";
    }

    function selectedAgent() {
      return agentsById.get(agentSelect.value) || null;
    }

    function connectivityLabel(agent) {
      if (agent.status === "busy") return "측정 중";
      return ({
        checking: "연결 확인 중",
        ready: "준비 완료",
        failed: "연결 실패",
        stale: "재확인 대기",
      })[agent.connectivity_status] || "연결 확인 중";
    }

    function renderClientReadiness() {
      const agent = selectedAgent();
      clientReadiness.classList.remove("success", "warning", "error");
      if (!agent) {
        clientReadiness.classList.add("warning");
        setTextIfChanged(
          clientReadiness,
          "주의 · 연결된 클라이언트가 없습니다. 다음 조치: Windows 클라이언트 ZIP을 실행하세요."
        );
        return;
      }
      if (agent.status === "busy") {
        clientReadiness.classList.add("warning");
        setTextIfChanged(
          clientReadiness,
          `주의 · 다른 측정 진행 중 · ${agent.hostname} · TCP ${agent.probe_port} · 다음 조치: 현재 측정이 끝날 때까지 기다리세요.`
        );
        return;
      }
      if (agent.connectivity_status === "ready") {
        if (agent.version_match) {
          clientReadiness.classList.add("success");
          setTextIfChanged(
            clientReadiness,
            `정상 · TCP ${agent.probe_port} 연결 준비 완료 · 클라이언트 ${agent.client_version} · 다음 조치: 측정 방향을 선택하세요.`
          );
        } else {
          clientReadiness.classList.add("warning");
          setTextIfChanged(
            clientReadiness,
            `주의 · TCP ${agent.probe_port} 연결 준비 완료 · 클라이언트 ${agent.client_version} / 서버 ${agent.server_version} · 다음 조치: 최신 ZIP 사용 권장`
          );
        }
        return;
      }
      if (agent.connectivity_status === "failed") {
        clientReadiness.classList.add("error");
        const reason = agent.connectivity_message || "TCP 측정 포트에 연결하지 못했습니다.";
        setTextIfChanged(
          clientReadiness,
          `장애 · ${reason} 다음 조치: 서버 콘솔과 Windows 방화벽의 TCP ${agent.probe_port} 인바운드 허용을 확인하세요. 약 20초 안에 자동 재점검합니다.`
        );
      } else if (agent.connectivity_status === "stale") {
        clientReadiness.classList.add("warning");
        setTextIfChanged(
          clientReadiness,
          `주의 · TCP ${agent.probe_port} 연결 결과가 오래되었습니다. 다음 조치: 자동 재점검을 기다리세요.`
        );
      } else {
        clientReadiness.classList.add("warning");
        setTextIfChanged(
          clientReadiness,
          `주의 · TCP ${agent.probe_port} 측정 포트 연결을 확인하는 중입니다. 다음 조치: 확인이 끝날 때까지 기다리세요.`
        );
      }
    }

    function setControlsEnabled() {
      const agent = selectedAgent();
      const ready = Boolean(agent) && agent.status !== "busy" && agent.connectivity_status === "ready";
      const enabled = serviceAvailable && ready && !running;
      agentSelect.disabled = !serviceAvailable || running;
      durationSelect.disabled = !enabled;
      fourStreamToggle.disabled = !enabled;
      actionButtons.forEach((button) => { button.disabled = !enabled; });
      modeButtons.forEach((button) => { button.disabled = running; });
      criterionButtons.forEach((button) => { button.disabled = running; });
      cancelButton.hidden = !running;
      cancelButton.disabled = !running;
      root.dataset.probeRunning = running ? "true" : "";
      resultPanel.setAttribute("aria-busy", running ? "true" : "false");
      renderClientReadiness();
    }

    function setStatus(message, state, guidanceMessage) {
      const operationState = state || "idle";
      const guidanceByState = {
        idle: "대기 · Windows 클라이언트가 준비되면 측정 방향을 선택하세요.",
        running: "진행 중 · 완료되거나 취소될 때까지 중복 실행할 수 없습니다.",
        success: "정상 완료 · 결과와 재전송량을 확인하세요. 완료 표시는 네트워크 정상 판정이 아닙니다.",
        warning: "주의 · 준비 상태나 취소 결과를 확인한 뒤 필요하면 다시 시도하세요.",
        error: "장애 · 오류 내용과 클라이언트, TCP 포트, 방화벽 상태를 확인한 뒤 다시 시도하세요.",
      };
      statusText.textContent = message;
      statusText.setAttribute("aria-live", operationState === "error" ? "assertive" : "polite");
      setOperationState(statusText, operationState);
      guidance.textContent = guidanceMessage || guidanceByState[operationState];
      guidance.setAttribute("aria-live", operationState === "error" ? "assertive" : "polite");
      setOperationState(guidance, operationState);
      progressBar.closest('[role="progressbar"]').dataset.state = operationState;
    }

    function resetResult() {
      setStatus("준비", "idle");
      phaseText.textContent = "TCP 전송 성능 측정 시작 대기";
      progress.reset();
      summaryList.innerHTML = "";
      chartPanel.hidden = true;
      technicalDetails.hidden = true;
      technicalDetails.open = false;
      conditionsText.textContent = "-";
      clientText.textContent = "-";
      detailList.innerHTML = "";
      excelLink.hidden = true;
      excelLink.removeAttribute("href");
      graphSeries = { upload: [], download: [] };
      chartAverages = { upload: null, download: null };
      syncCharts();
    }

    function renderServiceStatus(payload) {
      serviceAvailable = Boolean(payload.available);
      serviceStatus.classList.remove("success", "warning", "error");
      if (!payload.enabled) {
        serviceStatus.classList.add("warning");
        setTextIfChanged(
          serviceStatus,
          "주의 · TCP 전송 성능 측정 비활성 · 다음 조치: config.ini의 [network_probe] ENABLED=true 설정 필요"
        );
      } else if (!payload.available) {
        serviceStatus.classList.add("error");
        setTextIfChanged(
          serviceStatus,
          `장애 · ${payload.error || "TCP 측정 서버를 사용할 수 없습니다."} 다음 조치: 서버 콘솔과 TCP 포트를 확인하세요.`
        );
      } else {
        serviceStatus.classList.add("success");
        setTextIfChanged(
          serviceStatus,
          `정상 · TCP 측정 서버 준비 완료 · 서버 ${payload.server_version || "-"} · TCP ${payload.port}`
        );
      }
      const packageAvailable = Boolean(payload.client_package_available);
      packageLink.setAttribute("aria-disabled", packageAvailable ? "false" : "true");
      packageLink.classList.toggle("is-disabled", !packageAvailable);
      if (packageAvailable) {
        packageLink.href = payload.client_package_url || root.dataset.probeClientPackageUrl;
        packageAddress.textContent = `자동 연결 주소 · ${payload.client_package_server_url}`;
        const executableHash = payload.client_executable_sha256 || "";
        packageHash.hidden = !executableHash;
        packageHash.textContent = executableHash ? `클라이언트 EXE SHA256 · ${executableHash}` : "";
      } else {
        packageLink.removeAttribute("href");
        packageAddress.textContent = payload.client_package_error || "Windows 클라이언트 ZIP을 사용할 수 없습니다.";
        packageHash.hidden = true;
        packageHash.textContent = "";
      }
      setControlsEnabled();
    }

    async function refreshAgents() {
      if (agentRefreshInFlight || document.visibilityState === "hidden") {
        return;
      }
      agentRefreshInFlight = true;
      try {
        const [status, agentsPayload] = await Promise.all([
          fetchJson(root.dataset.probeStatusUrl, {}, "TCP 서버 상태"),
          fetchJson(root.dataset.probeAgentsUrl, {}, "TCP 클라이언트 목록"),
        ]);
        renderServiceStatus(status);
        const previous = agentSelect.value;
        const agents = Array.isArray(agentsPayload.agents) ? agentsPayload.agents : [];
        agentsById = new Map(agents.map((agent) => [agent.agent_id, agent]));
        const agentSignature = JSON.stringify(
          agents.map((agent) => [
            agent.agent_id,
            agent.hostname,
            agent.client_ip,
            agent.status,
            agent.connectivity_status,
          ])
        );
        if (agentSignature !== lastAgentSignature) {
          lastAgentSignature = agentSignature;
          agentSelect.replaceChildren();
          if (!agents.length) {
            const option = document.createElement("option");
            option.value = "";
            option.textContent = "연결된 Windows 클라이언트 없음";
            agentSelect.appendChild(option);
          } else {
            agents.forEach((agent) => {
              const option = document.createElement("option");
              option.value = agent.agent_id;
              option.disabled = agent.status === "busy" && agent.agent_id !== previous;
              option.textContent = `${agent.hostname} · ${agent.client_ip} · ${connectivityLabel(agent)}`;
              agentSelect.appendChild(option);
            });
            if (agents.some((agent) => agent.agent_id === previous)) {
              agentSelect.value = previous;
            }
          }
        }
        setControlsEnabled();
      } catch (error) {
        serviceAvailable = false;
        agentsById = new Map();
        serviceStatus.classList.remove("success", "warning");
        serviceStatus.classList.add("error");
        setTextIfChanged(
          serviceStatus,
          `장애 · ${error.message} 다음 조치: 서버 연결과 방화벽을 확인하세요.`
        );
        packageLink.removeAttribute("href");
        packageLink.setAttribute("aria-disabled", "true");
        packageLink.classList.add("is-disabled");
        packageAddress.textContent = error.message;
        setControlsEnabled();
      } finally {
        agentRefreshInFlight = false;
      }
    }

    function stateLabel(payload) {
      const phase = payload.active_phase === "upload" ? "업로드" : payload.active_phase === "download" ? "다운로드" : "";
      return ({
        queued: "클라이언트 작업 대기",
        attaching: `${phase} TCP 연결 준비`,
        warmup: `${phase} 3초 워밍업`,
        running: `${phase} 본 측정`,
        awaiting_result: `${phase} 결과 집계`,
        completed: "TCP 전송 성능 측정 완료",
        cancelled: "TCP 전송 성능 측정 취소",
        failed: "TCP 전송 성능 측정 실패",
      })[payload.status] || payload.status;
    }

    function renderPhaseResult(direction, combined) {
      if (!combined || !combined.sender || !combined.receiver) return;
      const sender = combined.sender;
      const receiver = combined.receiver;
      const telemetry = sender.telemetry || {};
      graphSeries[direction] = Array.isArray(receiver.intervals)
        ? receiver.intervals.map((item, index) => ({
          index: Number(item.index) || index + 1,
          mbps: Number(item.mbps) || 0,
        }))
        : [];
      chartAverages[direction] = Number(receiver.average_mbps);

      const summary = document.createElement("div");
      summary.className = "result-summary-card";
      const summaryHeading = document.createElement("h3");
      summaryHeading.textContent = `${directionLabel(direction)} 평균 속도`;
      const route = document.createElement("span");
      route.className = "summary-route";
      route.textContent = directionPath(direction);
      const primary = document.createElement("strong");
      primary.className = "summary-speed";
      primary.textContent = `${Number(receiver.average_mbps).toFixed(1)} Mbps`;
      const secondary = document.createElement("span");
      secondary.className = "summary-secondary";
      secondary.textContent = `초당 파일 전송량 ${(Number(receiver.average_mbps) / 8).toFixed(1)} MB/s`;
      summary.append(summaryHeading, route, primary, secondary);
      summaryList.appendChild(summary);

      const item = document.createElement("div");
      item.className = "transfer-result";
      const title = document.createElement("h3");
      title.textContent = `${directionLabel(direction)} · ${directionPath(direction)}`;
      const details = document.createElement("dl");
      details.className = "transfer-result-details";
      [
        ["1초 구간 중앙 속도", formatSpeed(receiver.median_mbps)],
        ["1초 구간 최저 속도", formatSpeed(receiver.min_mbps)],
        ["1초 구간 최고 속도", formatSpeed(receiver.max_mbps)],
        ["TCP 왕복시간(RTT)", formatRtt(telemetry)],
        ["TCP 재전송량", formatRetransmission(sender, telemetry)],
      ].forEach(([label, value]) => {
        const row = document.createElement("div");
        const term = document.createElement("dt");
        const description = document.createElement("dd");
        term.textContent = label;
        description.textContent = value;
        row.append(term, description);
        details.appendChild(row);
      });
      item.append(title, details);
      detailList.appendChild(item);
    }

    function renderSession(payload) {
      setStatus("진행 중", "running");
      phaseText.textContent = stateLabel(payload);
      progress.update(payload.progress_percent, payload.status, payload.persistence_complete);
      if (payload.agent) clientText.textContent = `${payload.agent.hostname} · ${payload.agent.client_ip}`;
      if (payload.requested) {
        conditionsText.textContent = `${Number(payload.requested.duration_seconds)}초 · TCP ${Number(payload.requested.stream_count)}개 스트림`;
      }
      summaryList.innerHTML = "";
      detailList.innerHTML = "";
      graphSeries = { upload: [], download: [] };
      chartAverages = { upload: null, download: null };
      const results = payload.results || {};
      const completed = payload.status === "completed" && payload.persistence_complete !== false;
      technicalDetails.hidden = !completed;
      if (completed) {
        Object.entries(results).forEach(([direction, result]) => renderPhaseResult(direction, result));
        const difference = formatDirectionDifference(results);
        if (difference) {
          const comparison = document.createElement("p");
          comparison.className = "measurement-explanation probe-comparison";
          comparison.textContent = difference;
          detailList.appendChild(comparison);
        }
        syncCharts();
      } else {
        chartPanel.hidden = true;
        technicalDetails.open = false;
      }
      if (payload.error) phaseText.textContent = payload.error;
      if (payload.excel_url) {
        excelLink.href = payload.excel_url;
        excelLink.hidden = false;
        excelLink.setAttribute("download", "");
      }
      if (["completed", "cancelled", "failed"].includes(payload.status)) {
        if (payload.persistence_complete === false) {
          setStatus("결과 저장 중", "running");
          phaseText.textContent = "측정 결과를 파일에 저장하고 있습니다.";
        } else {
          if (payload.status === "completed") {
            setStatus("완료", "success");
          } else if (payload.status === "cancelled") {
            setStatus("취소", "warning");
          } else {
            setStatus("실패", "error");
          }
        }
      }
    }

    async function pollSession() {
      while (running && activeSessionId) {
        const payload = await fetchJson(`${root.dataset.probeSessionsUrl}/${activeSessionId}`, {}, "TCP 측정 상태");
        renderSession(payload);
        if (["completed", "cancelled", "failed"].includes(payload.status) && payload.persistence_complete !== false) {
          running = false;
          activeSessionId = "";
          setControlsEnabled();
          await refreshAgents();
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 500));
      }
    }

    async function startMeasurement(direction) {
      if (
        running ||
        root.dataset.simpleRunning === "true" ||
        root.dataset.sustainedRunning === "true"
      ) {
        return;
      }
      const durationSeconds = Number(durationSelect.value);
      const selectedStreams = fourStreamToggle.checked ? 4 : 1;
      const agent = selectedAgent();
      if (!agent || agent.status === "busy" || agent.connectivity_status !== "ready") {
        setStatus("시작 대기", "warning");
        phaseText.textContent = "클라이언트의 TCP 연결 준비 완료를 확인한 후 시작하세요.";
        renderClientReadiness();
        return;
      }
      if ((durationSeconds === 30 || selectedStreams === 4 || direction === "full") &&
          !window.confirm("선택한 TCP 측정은 사내망 부하가 커질 수 있습니다. 시작할까요?")) return;
      resetResult();
      running = true;
      setControlsEnabled();
      setStatus("시작 중", "running");
      try {
        const payload = await fetchJson(root.dataset.probeSessionsUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            agent_id: agentSelect.value,
            direction,
            duration_seconds: durationSeconds,
            stream_count: selectedStreams,
          }),
        }, "TCP 측정 시작");
        activeSessionId = payload.session_id;
        renderSession(payload);
        await pollSession();
      } catch (error) {
        running = false;
        activeSessionId = "";
        progress.stop();
        setStatus("실패", "error");
        phaseText.textContent = error.message;
        setControlsEnabled();
      }
    }

    async function cancelMeasurement() {
      if (!activeSessionId) return;
      cancelButton.disabled = true;
      try {
        renderSession(await fetchJson(`${root.dataset.probeSessionsUrl}/${activeSessionId}/cancel`, {
          method: "POST",
        }, "TCP 측정 취소"));
      } catch (error) {
        phaseText.textContent = error.message;
        setStatus(
          "진행 중",
          "warning",
          "주의 · 취소 요청에 실패했습니다. 현재 측정 상태를 확인한 뒤 다시 시도하세요."
        );
      }
    }

    actionButtons.forEach((button) => button.addEventListener("click", () => startMeasurement(button.dataset.probeAction)));
    agentSelect.addEventListener("change", setControlsEnabled);
    fourStreamToggle.addEventListener("change", updateAdvancedSummary);
    packageLink.addEventListener("click", (event) => {
      if (packageLink.getAttribute("aria-disabled") === "true") event.preventDefault();
    });
    cancelButton.addEventListener("click", cancelMeasurement);
    window.setInterval(() => {
      if (!running && document.visibilityState === "visible") {
        refreshAgents();
      }
    }, 3000);
    document.addEventListener("visibilitychange", () => {
      if (!running && document.visibilityState === "visible") {
        refreshAgents();
      }
    });
    updateAdvancedSummary();
    resetResult();
    refreshAgents();
  }

  document.addEventListener("DOMContentLoaded", initProbe);
})();
