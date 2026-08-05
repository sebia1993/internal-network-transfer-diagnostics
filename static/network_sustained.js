(function () {
  const MB = 1024 * 1024;
  const MIN_UPLOAD_CHUNK = 256 * 1024;
  const TARGET_CHUNK_SECONDS = 0.25;
  const UPLOAD_CHUNK_STEP = 64 * 1024;
  const LATENCY_PROGRESS_PERCENT = 5;
  const MEASUREMENT_PROGRESS_PERCENT = 95;
  const MAX_IN_PROGRESS_PERCENT = 99.9;
  const PROGRESS_LABEL_INTERVAL_MS = 100;
  const HTTP_STREAM_COUNT = 1;

  function buildUrl(baseUrl, suffix) {
    const normalized = baseUrl.replace(/\/$/, "");
    const url = new URL(`${normalized}${suffix || ""}`, window.location.href);
    url.searchParams.set("_", `${Date.now()}-${Math.random()}`);
    return url.toString();
  }

  function formatSpeed(mbps) {
    if (!Number.isFinite(mbps) || mbps < 0) {
      return "-";
    }
    const digits = mbps >= 100 ? 1 : 2;
    return `${mbps.toFixed(digits)} Mbps / ${(mbps / 8).toFixed(digits)} MB/s`;
  }

  function formatMbps(mbps) {
    if (!Number.isFinite(mbps) || mbps < 0) {
      return "-";
    }
    return mbps.toFixed(mbps >= 100 ? 1 : 2);
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) {
      return "-";
    }
    if (value >= 1024 * 1024 * 1024) {
      return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
    }
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  function setOperationState(element, state) {
    element.classList.remove("is-idle", "is-running", "is-success", "is-warning", "is-error");
    element.classList.add(`is-${state}`);
    element.dataset.state = state;
  }

  function wait(milliseconds, signal) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) {
        reject(new Error("측정이 취소되었습니다."));
        return;
      }
      const abortHandler = () => {
        window.clearTimeout(timer);
        reject(new Error("측정이 취소되었습니다."));
      };
      const timer = window.setTimeout(() => {
        if (signal) {
          signal.removeEventListener("abort", abortHandler);
        }
        resolve();
      }, milliseconds);
      if (signal) {
        signal.addEventListener("abort", abortHandler, { once: true });
      }
    });
  }

  async function fetchJson(url, options, label) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error("측정이 취소되었습니다.");
      }
      throw new Error(`${label} 요청에 실패했습니다. (${error.message})`);
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `${label} 요청이 실패했습니다.`);
    }
    return payload;
  }

  function createSustainedProgress(progressBar, phaseText) {
    const progressTrack = progressBar.closest('[role="progressbar"]');
    let animationFrameId = 0;
    let currentPercent = 0;
    let completedMeasurementSeconds = 0;
    let totalMeasurementSeconds = 0;
    let activePhase = null;
    let lastLabelUpdatedAt = 0;

    function cancelAnimation() {
      if (animationFrameId) {
        window.cancelAnimationFrame(animationFrameId);
        animationFrameId = 0;
      }
    }

    function setPercent(nextPercent, label, detail, options) {
      const settings = options || {};
      const upperBound = settings.allowComplete ? 100 : MAX_IN_PROGRESS_PERCENT;
      const boundedPercent = Math.min(upperBound, Math.max(0, Number(nextPercent) || 0));
      currentPercent = Math.max(currentPercent, boundedPercent);
      progressBar.style.transform = `scaleX(${(currentPercent / 100).toFixed(4)})`;
      progressTrack.setAttribute("aria-valuenow", currentPercent.toFixed(1));
      progressTrack.setAttribute(
        "aria-valuetext",
        `${label} · ${currentPercent.toFixed(1)}%${detail ? ` · ${detail}` : ""}`
      );

      const now = performance.now();
      if (settings.forceLabel || now - lastLabelUpdatedAt >= PROGRESS_LABEL_INTERVAL_MS) {
        phaseText.textContent = `${label} · 전체 ${currentPercent.toFixed(1)}%${detail ? ` · ${detail}` : ""}`;
        lastLabelUpdatedAt = now;
      }
    }

    function reset() {
      cancelAnimation();
      currentPercent = 0;
      completedMeasurementSeconds = 0;
      totalMeasurementSeconds = 0;
      activePhase = null;
      lastLabelUpdatedAt = 0;
      progressBar.style.transform = "scaleX(0)";
      phaseText.textContent = "HTTP 응답시간 측정 준비 · 전체 0.0%";
      progressTrack.setAttribute("aria-valuenow", "0");
      progressTrack.setAttribute("aria-valuetext", "대기 · 0%");
    }

    function setLatencyStep(completedSteps, totalSteps) {
      const safeTotal = Math.max(1, Number(totalSteps) || 1);
      const safeCompleted = Math.min(safeTotal, Math.max(0, Number(completedSteps) || 0));
      setPercent(
        (safeCompleted / safeTotal) * LATENCY_PROGRESS_PERCENT,
        "HTTP 응답시간 확인",
        `${safeCompleted} / ${safeTotal}회`,
        { forceLabel: true }
      );
    }

    function configure(direction, warmupSeconds, durationSeconds) {
      const directionCount = direction === "full" ? 2 : 1;
      const safeWarmup = Math.max(0, Number(warmupSeconds) || 0);
      const safeDuration = Math.max(0, Number(durationSeconds) || 0);
      completedMeasurementSeconds = 0;
      totalMeasurementSeconds = directionCount * (safeWarmup + safeDuration);
    }

    function startPhase(label, durationSeconds) {
      cancelAnimation();
      const safeDuration = Math.max(0, Number(durationSeconds) || 0);
      const safeTotal = Math.max(totalMeasurementSeconds, safeDuration, 0.001);
      const startPercent =
        LATENCY_PROGRESS_PERCENT +
        (completedMeasurementSeconds / safeTotal) * MEASUREMENT_PROGRESS_PERCENT;
      const endPercent =
        LATENCY_PROGRESS_PERCENT +
        ((completedMeasurementSeconds + safeDuration) / safeTotal) * MEASUREMENT_PROGRESS_PERCENT;
      const phase = {
        label,
        durationSeconds: safeDuration,
        startedAt: performance.now(),
        startPercent,
        endPercent,
      };
      activePhase = phase;

      function tick(timestamp) {
        if (activePhase !== phase) {
          return;
        }
        const elapsedSeconds = Math.min(
          Math.max((timestamp - phase.startedAt) / 1000, 0),
          phase.durationSeconds
        );
        const fraction = phase.durationSeconds > 0 ? elapsedSeconds / phase.durationSeconds : 1;
        const nextPercent = phase.startPercent + (phase.endPercent - phase.startPercent) * fraction;
        setPercent(
          nextPercent,
          phase.label,
          `약 ${Math.ceil(Math.max(phase.durationSeconds - elapsedSeconds, 0))}초 남음`
        );
        if (elapsedSeconds < phase.durationSeconds) {
          animationFrameId = window.requestAnimationFrame(tick);
        } else {
          animationFrameId = 0;
        }
      }

      setPercent(startPercent, label, `약 ${Math.ceil(safeDuration)}초 남음`, { forceLabel: true });
      animationFrameId = window.requestAnimationFrame(tick);
    }

    function finishPhase() {
      if (!activePhase) {
        return;
      }
      const phase = activePhase;
      cancelAnimation();
      completedMeasurementSeconds += phase.durationSeconds;
      activePhase = null;
      setPercent(phase.endPercent, `${phase.label} 완료`, "", { forceLabel: true });
    }

    function stop() {
      cancelAnimation();
      activePhase = null;
    }

    function complete() {
      stop();
      setPercent(100, "HTTP 시간 기준 측정 완료", "", { allowComplete: true, forceLabel: true });
    }

    function terminate(statusLabel, message) {
      stop();
      phaseText.textContent = `${statusLabel} · 전체 ${currentPercent.toFixed(1)}%${message ? ` · ${message}` : ""}`;
      progressTrack.setAttribute(
        "aria-valuetext",
        `${statusLabel} · ${currentPercent.toFixed(1)}%${message ? ` · ${message}` : ""}`
      );
    }

    return {
      complete,
      configure,
      finishPhase,
      getPercent: () => currentPercent,
      reset,
      setLatencyStep,
      startPhase,
      stop,
      terminate,
    };
  }

  function initSustainedCheck() {
    const root = document.querySelector("[data-network-check]");
    if (!root || !root.dataset.sustainedSessionUrl) {
      return;
    }

    const modeButtons = root.querySelectorAll("[data-measurement-mode]");
    const controlPanels = root.querySelectorAll("[data-measurement-control]");
    const resultPanels = root.querySelectorAll("[data-measurement-result]");
    const criterionButtons = root.querySelectorAll("[data-http-criterion]");
    const criterionControlPanels = root.querySelectorAll("[data-http-criterion-control]");
    const criterionResultPanels = root.querySelectorAll("[data-http-criterion-result]");
    const durationSelect = root.querySelector("[data-sustained-duration]");
    const actionButtons = root.querySelectorAll("[data-sustained-action]");
    const cancelButton = root.querySelector("[data-sustained-cancel]");
    const statusText = root.querySelector("[data-sustained-status]");
    const guidance = root.querySelector("[data-sustained-guidance]");
    const phaseText = root.querySelector("[data-sustained-phase]");
    const progressBar = root.querySelector("[data-sustained-progress-bar]");
    const livePanel = root.querySelector("[data-sustained-live]");
    const liveSpeedText = root.querySelector("[data-sustained-live-speed]");
    const completedPanel = root.querySelector("[data-sustained-completed]");
    const summaryList = root.querySelector("[data-sustained-summary]");
    const latencyText = root.querySelector("[data-sustained-latency]");
    const conditionsText = root.querySelector("[data-sustained-conditions]");
    const detailList = root.querySelector("[data-sustained-detail-list]");
    const technicalDetails = root.querySelector("[data-sustained-technical-details]");
    const excelLink = root.querySelector("[data-sustained-excel]");
    const chartPanel = root.querySelector("[data-sustained-chart-panel]");
    const resultPanel = statusText.closest("[data-http-criterion-result]");
    const chartCards = new Map(
      Array.from(root.querySelectorAll("[data-sustained-chart-card]"))
        .map((card) => [card.dataset.sustainedChartCard, card])
    );

    let selectedCriterion = "simple";
    let running = false;
    let activeController = null;
    let activeSessionId = "";
    let cancellationRequested = false;
    let latencySamples = [];
    let clientResults = {};
    const graphSeries = { upload: [], download: [] };
    const chartAverages = { upload: null, download: null };
    const progress = createSustainedProgress(progressBar, phaseText);
    const chartRenderers = new Map();
    root.querySelectorAll("[data-sustained-chart]").forEach((canvas) => {
      const direction = canvas.dataset.sustainedChart;
      const renderer = window.InternalUploadThroughputChart && window.InternalUploadThroughputChart.create(
        canvas,
        {
          color: direction === "upload" ? "#246b54" : "#c15f2e",
          fillColor: direction === "upload" ? "rgba(36, 107, 84, 0.10)" : "rgba(193, 95, 46, 0.10)",
          label: direction === "upload" ? "HTTP 업로드" : "HTTP 다운로드",
        }
      );
      if (renderer) chartRenderers.set(direction, renderer);
    });

    function drawCharts() {
      chartRenderers.forEach((renderer) => renderer.draw());
    }

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
      if (chartPanel) chartPanel.hidden = visibleCount === 0;
    }

    function setMeasurementMode(mode) {
      if (running || root.dataset.simpleRunning === "true" || root.dataset.probeRunning === "true") {
        return;
      }
      modeButtons.forEach((button) => {
        const active = button.dataset.measurementMode === mode;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
        button.tabIndex = active ? 0 : -1;
      });
      controlPanels.forEach((panel) => {
        panel.hidden = panel.dataset.measurementControl !== mode;
      });
      resultPanels.forEach((panel) => {
        panel.hidden = panel.dataset.measurementResult !== mode;
      });
      if (mode === "http" && selectedCriterion === "sustained") {
        window.requestAnimationFrame(drawCharts);
      }
    }

    function setHttpCriterion(criterion) {
      if (running || root.dataset.simpleRunning === "true" || root.dataset.probeRunning === "true") {
        return;
      }
      selectedCriterion = criterion;
      criterionButtons.forEach((button) => {
        const active = button.dataset.httpCriterion === criterion;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      criterionControlPanels.forEach((panel) => {
        panel.hidden = panel.dataset.httpCriterionControl !== criterion;
      });
      criterionResultPanels.forEach((panel) => {
        panel.hidden = panel.dataset.httpCriterionResult !== criterion;
      });
      if (criterion === "sustained") {
        window.requestAnimationFrame(drawCharts);
      }
    }

    function setRunning(nextRunning) {
      running = nextRunning;
      root.dataset.sustainedRunning = nextRunning ? "true" : "";
      actionButtons.forEach((button) => {
        button.disabled = nextRunning;
      });
      criterionButtons.forEach((button) => {
        button.disabled = nextRunning;
      });
      modeButtons.forEach((button) => {
        button.disabled = nextRunning;
      });
      durationSelect.disabled = nextRunning;
      cancelButton.disabled = !nextRunning;
      cancelButton.hidden = !nextRunning;
      resultPanel.setAttribute("aria-busy", nextRunning ? "true" : "false");
    }

    function setStatus(message, state) {
      const operationState = state || "idle";
      const guidanceByState = {
        idle: "대기 · 측정 방향과 시간을 선택한 뒤 측정을 시작하세요.",
        running: "진행 중 · 완료되거나 취소될 때까지 중복 실행할 수 없습니다.",
        success: "완료 · 결과와 변동률을 확인하세요. 완료 표시는 네트워크 정상 판정이 아닙니다.",
        warning: "취소 · 측정이 중단되었습니다. 필요하면 동일 조건으로 다시 시작하세요.",
        error: "실패 · 오류 내용과 서버 연결 상태를 확인한 뒤 다시 시도하세요.",
      };
      statusText.textContent = message;
      statusText.setAttribute("aria-live", operationState === "error" ? "assertive" : "polite");
      setOperationState(statusText, operationState);
      guidance.textContent = guidanceByState[operationState];
      guidance.setAttribute("aria-live", operationState === "error" ? "assertive" : "polite");
      setOperationState(guidance, operationState);
      progressBar.closest('[role="progressbar"]').dataset.state = operationState;
    }

    function resetResult() {
      setStatus("준비", "idle");
      progress.reset();
      livePanel.hidden = false;
      liveSpeedText.textContent = "측정 준비 중";
      completedPanel.hidden = true;
      summaryList.innerHTML = "";
      latencyText.textContent = "-";
      conditionsText.textContent = "-";
      detailList.innerHTML = "";
      technicalDetails.open = false;
      excelLink.hidden = true;
      excelLink.removeAttribute("href");
      latencySamples = [];
      clientResults = {};
      graphSeries.upload = [];
      graphSeries.download = [];
      chartAverages.upload = null;
      chartAverages.download = null;
      syncCharts();
    }

    async function measureLatency(signal) {
      setStatus("응답시간 측정 중", "running");
      liveSpeedText.textContent = "측정 준비 중";
      const samples = [];
      progress.setLatencyStep(0, 6);
      for (let index = 0; index < 6; index += 1) {
        const startedAt = performance.now();
        await fetchJson(
          buildUrl(root.dataset.sustainedLatencyUrl),
          { cache: "no-store", signal },
          "HTTP 응답시간"
        );
        const elapsed = performance.now() - startedAt;
        if (index > 0) {
          samples.push(elapsed);
        }
        progress.setLatencyStep(index + 1, 6);
      }
      return samples;
    }

    async function beginPhase(direction, phase, signal) {
      return fetchJson(
        buildUrl(root.dataset.sustainedSessionUrl, `/${encodeURIComponent(activeSessionId)}/phase`),
        {
          method: "POST",
          cache: "no-store",
          signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ direction, phase }),
        },
        "측정 단계 시작"
      );
    }

    async function getStatus(signal) {
      return fetchJson(
        buildUrl(root.dataset.sustainedSessionUrl, `/${encodeURIComponent(activeSessionId)}/status`),
        { cache: "no-store", signal },
        "측정 상태"
      );
    }

    function appendLiveSample(direction, byteDelta, elapsedSeconds, options) {
      if (byteDelta < 0 || elapsedSeconds <= 0) {
        return;
      }
      const settings = options || {};
      const series = graphSeries[direction];
      const previous = series[series.length - 1];
      if (settings.mergeShortTail && previous && elapsedSeconds < 0.5) {
        previous.bytes_transferred += byteDelta;
        previous.duration_seconds += elapsedSeconds;
        previous.mbps = (previous.bytes_transferred * 8) / previous.duration_seconds / 1_000_000;
      } else {
        series.push({
          index: series.length + 1,
          bytes_transferred: byteDelta,
          duration_seconds: elapsedSeconds,
          mbps: (byteDelta * 8) / elapsedSeconds / 1_000_000,
        });
      }
      const recentSamples = graphSeries[direction].slice(-3);
      const rollingMbps = recentSamples.reduce((total, item) => total + item.mbps, 0) / recentSamples.length;
      liveSpeedText.textContent = `${formatMbps(rollingMbps)} Mbps`;
    }

    async function runUploadPhase(phase, chunkSize, signal) {
      const phaseInfo = await beginPhase("upload", phase, signal);
      const durationSeconds = Number(phaseInfo.duration_seconds);
      const startedAt = performance.now();
      const deadline = startedAt + durationSeconds * 1000;
      const body = new Uint8Array(chunkSize);
      const phaseLabel = phase === "warmup" ? "업로드 워밍" : "업로드 본 측정";
      liveSpeedText.textContent = phase === "warmup" ? "측정 준비 중" : "속도 계산 중";
      progress.startPhase(phaseLabel, durationSeconds);
      let workersFinished = false;
      let previousBytes = 0;
      let previousSampleAt = startedAt;

      async function worker(streamId) {
        while (performance.now() < deadline) {
          await fetchJson(
            buildUrl(
              root.dataset.sustainedSessionUrl,
              `/${encodeURIComponent(activeSessionId)}/upload/${streamId}`
            ),
            {
              method: "POST",
              body,
              cache: "no-store",
              signal,
              headers: { "Content-Type": "application/octet-stream" },
            },
            `업로드 HTTP 연결 ${streamId + 1}`
          );
        }
      }

      const workers = Promise.all(Array.from({ length: HTTP_STREAM_COUNT }, (_, index) => worker(index))).finally(() => {
        workersFinished = true;
      });

      while (!workersFinished) {
        await Promise.race([workers, wait(500, signal)]);
        const status = await getStatus(signal);
        const now = performance.now();
        if (phase === "measure" && now - previousSampleAt >= 1000) {
          const phaseBytes = Number(status.phase_bytes);
          appendLiveSample("upload", phaseBytes - previousBytes, (now - previousSampleAt) / 1000);
          previousBytes = phaseBytes;
          previousSampleAt = now;
        }
      }
      await workers;
      const finalStatus = await getStatus(signal);
      const endedAt = performance.now();
      const tailDurationSeconds = (endedAt - previousSampleAt) / 1000;
      if (phase === "measure" && tailDurationSeconds > 0.01) {
        appendLiveSample(
          "upload",
          Number(finalStatus.phase_bytes) - previousBytes,
          tailDurationSeconds,
          { mergeShortTail: true }
        );
      }
      progress.finishPhase();
      return {
        bytes_transferred: Number(finalStatus.phase_bytes),
        actual_duration_seconds: durationSeconds,
        intervals: graphSeries.upload.map((point) => ({
          bytes_transferred: point.bytes_transferred,
          duration_seconds: point.duration_seconds,
        })),
      };
    }

    async function runDownloadPhase(phase, signal) {
      const phaseInfo = await beginPhase("download", phase, signal);
      const durationSeconds = Number(phaseInfo.duration_seconds);
      const startedAt = performance.now();
      const phaseLabel = phase === "warmup" ? "다운로드 워밍" : "다운로드 본 측정";
      liveSpeedText.textContent = phase === "warmup" ? "측정 준비 중" : "속도 계산 중";
      progress.startPhase(phaseLabel, durationSeconds);
      let totalBytes = 0;
      let previousBytes = 0;
      let previousSampleAt = startedAt;
      if (phase === "measure") {
        clientResults.download = {
          bytes_transferred: 0,
          actual_duration_seconds: 0.001,
          intervals: [],
        };
      }

      async function worker(streamId) {
        const response = await fetch(
          buildUrl(
            root.dataset.sustainedSessionUrl,
            `/${encodeURIComponent(activeSessionId)}/download/${streamId}`
          ),
          { cache: "no-store", signal }
        );
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.error || `다운로드 HTTP 연결 ${streamId + 1}이 실패했습니다.`);
        }
        if (!response.body) {
          throw new Error("현재 브라우저는 스트리밍 다운로드를 지원하지 않습니다.");
        }
        const reader = response.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }
          totalBytes += value.byteLength;
          const now = performance.now();
          const elapsed = Math.min((now - startedAt) / 1000, durationSeconds);
          if (phase === "measure") {
            clientResults.download.bytes_transferred = totalBytes;
            clientResults.download.actual_duration_seconds = Math.max(elapsed, 0.001);
          }
          if (phase === "measure" && now - previousSampleAt >= 1000) {
            appendLiveSample("download", totalBytes - previousBytes, (now - previousSampleAt) / 1000);
            clientResults.download.intervals = graphSeries.download.map((point) => ({
              bytes_transferred: point.bytes_transferred,
              duration_seconds: point.duration_seconds,
            }));
            previousBytes = totalBytes;
            previousSampleAt = now;
          }
        }
      }

      await Promise.all(Array.from({ length: HTTP_STREAM_COUNT }, (_, index) => worker(index)));
      const endedAt = performance.now();
      const tailDurationSeconds = (endedAt - previousSampleAt) / 1000;
      if (phase === "measure" && tailDurationSeconds > 0.01) {
        appendLiveSample(
          "download",
          totalBytes - previousBytes,
          tailDurationSeconds,
          { mergeShortTail: true }
        );
      }
      progress.finishPhase();
      const completedResult = {
        bytes_transferred: totalBytes,
        actual_duration_seconds: Math.max((endedAt - startedAt) / 1000, 0.001),
        intervals: graphSeries.download.map((point) => ({
          bytes_transferred: point.bytes_transferred,
          duration_seconds: point.duration_seconds,
        })),
      };
      if (phase === "measure") {
        clientResults.download = completedResult;
      }
      return completedResult;
    }

    function chooseUploadChunk(warmupBytes, warmupSeconds, maxChunkBytes) {
      const estimatedPerStreamBytesPerSecond = warmupBytes / Math.max(warmupSeconds, 0.001) / HTTP_STREAM_COUNT;
      const targetBytes = estimatedPerStreamBytesPerSecond * TARGET_CHUNK_SECONDS;
      const clamped = Math.max(MIN_UPLOAD_CHUNK, Math.min(maxChunkBytes, targetBytes));
      return Math.max(MIN_UPLOAD_CHUNK, Math.round(clamped / UPLOAD_CHUNK_STEP) * UPLOAD_CHUNK_STEP);
    }

    async function runDirection(direction, session, signal) {
      if (direction === "upload") {
        const warmup = await runUploadPhase("warmup", MB, signal);
        const chunkSize = chooseUploadChunk(
          warmup.bytes_transferred,
          Number(session.warmup_seconds),
          Number(session.max_upload_chunk_bytes)
        );
        const measured = await runUploadPhase("measure", chunkSize, signal);
        clientResults.upload = measured;
        return;
      }
      await runDownloadPhase("warmup", signal);
      clientResults.download = await runDownloadPhase("measure", signal);
    }

    function renderCompletedResult(result) {
      if (result.excel_url) {
        excelLink.href = new URL(result.excel_url, window.location.href).toString();
        excelLink.hidden = false;
        excelLink.setAttribute("download", "");
      }
      const directionEntries = Object.entries(result.directions || {}).filter(
        ([direction, summary]) => (
          (direction === "upload" || direction === "download") &&
          Number(summary && summary.bytes_transferred) > 0 &&
          Number(summary && summary.actual_duration_seconds) > 0
        )
      );
      const partial = result.status !== "success";
      if (partial && !directionEntries.length) {
        completedPanel.hidden = true;
        livePanel.hidden = false;
        liveSpeedText.textContent = "결과 없음";
        return;
      }

      livePanel.hidden = true;
      completedPanel.hidden = false;
      summaryList.innerHTML = "";
      detailList.innerHTML = "";
      technicalDetails.open = false;
      if (partial) {
        const partialNotice = document.createElement("div");
        partialNotice.className = "result-summary-card";
        const partialHeading = document.createElement("h3");
        partialHeading.textContent = "부분 완료";
        const partialText = document.createElement("span");
        partialText.className = "summary-secondary";
        partialText.textContent = (
          "완료된 방향의 결과만 보존했습니다. 실패한 방향만 같은 조건으로 다시 측정하세요."
        );
        partialNotice.append(partialHeading, partialText);
        summaryList.appendChild(partialNotice);
      }
      directionEntries.forEach(([direction, summary]) => {
        const label = direction === "upload" ? "업로드" : "다운로드";
        const path = direction === "upload" ? "사용자 PC → 서버" : "서버 → 사용자 PC";
        const averageMbps = Number(summary.average_mbps);
        const variability = Number(summary.variability_percent);
        if (direction === "upload" || direction === "download") {
          const intervals = Array.isArray(summary.intervals) ? summary.intervals : [];
          graphSeries[direction] = intervals.map((point, index) => ({
            index: Number(point.index) || index + 1,
            mbps: Number(point.mbps) || 0,
          }));
          chartAverages[direction] = averageMbps;
        }

        const card = document.createElement("div");
        card.className = "result-summary-card";
        const heading = document.createElement("h3");
        heading.textContent = `${label} 평균 속도`;
        const route = document.createElement("span");
        route.className = "summary-route";
        route.textContent = path;
        const primary = document.createElement("strong");
        primary.className = "summary-speed";
        primary.textContent = `${formatMbps(averageMbps)} Mbps`;
        const secondary = document.createElement("span");
        secondary.className = "summary-secondary";
        secondary.textContent = `초당 파일 전송량 ${formatMbps(averageMbps / 8)} MB/s`;
        const variation = document.createElement("span");
        variation.className = "summary-secondary";
        variation.textContent = `속도 변동률 ${variability.toFixed(1)}% · 낮을수록 측정 중 속도가 일정함`;
        card.append(heading, route, primary, secondary, variation);
        summaryList.appendChild(card);

        const item = document.createElement("div");
        item.className = "transfer-result";
        const title = document.createElement("h3");
        title.textContent = `${label} · ${path}`;
        const details = document.createElement("dl");
        details.className = "transfer-result-details";
        [
          ["1초 구간 중앙 속도", formatSpeed(Number(summary.median_mbps))],
          ["1초 구간 최저 속도", formatSpeed(Number(summary.min_mbps))],
          ["1초 구간 최고 속도", formatSpeed(Number(summary.max_mbps))],
          ["총 전송량", formatBytes(summary.bytes_transferred)],
          ["실제 측정시간", `${Number(summary.actual_duration_seconds).toFixed(1)}초`],
        ].forEach(([termText, descriptionText]) => {
          const row = document.createElement("div");
          const term = document.createElement("dt");
          const description = document.createElement("dd");
          term.textContent = termText;
          description.textContent = descriptionText;
          row.append(term, description);
          details.appendChild(row);
        });
        item.append(title, details);
        detailList.appendChild(item);
      });
      if (
        result.http_latency &&
        result.http_latency.median_ms !== null &&
        Number.isFinite(Number(result.http_latency.median_ms))
      ) {
        latencyText.textContent = `${Number(result.http_latency.median_ms).toFixed(2)} ms`;
      }
      const requested = result.requested || {};
      conditionsText.textContent = `${Number(requested.duration_seconds)}초 본 측정 · ${Number(requested.warmup_seconds)}초 워밍업 · HTTP 연결 ${Number(requested.stream_count)}개`;
      if (!partial) {
        progress.complete();
      }
      syncCharts();
      window.requestAnimationFrame(drawCharts);
    }

    async function finalizeSession(path, payload) {
      return fetchJson(
        buildUrl(root.dataset.sustainedSessionUrl, `/${encodeURIComponent(activeSessionId)}/${path}`),
        {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
        "측정 결과 저장"
      );
    }

    async function runAction(direction) {
      if (
        running ||
        root.dataset.simpleRunning === "true" ||
        root.dataset.probeRunning === "true"
      ) {
        return;
      }
      const durationSeconds = Number.parseInt(durationSelect.value, 10);
      if (
        durationSeconds === 30 &&
        !window.confirm("30초 HTTP 측정은 사내망과 서버 PC에 부하를 줄 수 있습니다. 계속할까요?")
      ) {
        return;
      }

      resetResult();
      setRunning(true);
      activeController = new AbortController();
      cancellationRequested = false;

      try {
        latencySamples = await measureLatency(activeController.signal);
        const session = await fetchJson(
          buildUrl(root.dataset.sustainedSessionUrl),
          {
            method: "POST",
            cache: "no-store",
            signal: activeController.signal,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              direction,
              duration_seconds: durationSeconds,
              stream_count: HTTP_STREAM_COUNT,
            }),
          },
          "HTTP 시간 기준 측정 시작"
        );
        activeSessionId = session.session_id;
        setStatus("측정 중", "running");
        progress.configure(direction, session.warmup_seconds, session.duration_seconds);

        if (direction === "upload" || direction === "full") {
          await runDirection("upload", session, activeController.signal);
        }
        if (direction === "download" || direction === "full") {
          await runDirection("download", session, activeController.signal);
        }

        const completed = await finalizeSession("complete", {
          status: "success",
          latency_samples_ms: latencySamples,
          results: clientResults,
        });
        if (completed.status === "success") {
          setStatus("완료", "success");
        } else {
          setStatus("실패", "error");
          progress.terminate("실패", completed.error || "측정 결과가 유효하지 않습니다.");
        }
        renderCompletedResult(completed);
      } catch (error) {
        const errorMessage = error.message || "측정 중 오류가 발생했습니다.";
        progress.terminate(cancellationRequested ? "취소됨" : "실패", cancellationRequested ? "" : errorMessage);
        let savedResult = null;
        if (activeSessionId) {
          try {
            savedResult = await finalizeSession(cancellationRequested ? "cancel" : "complete", {
              status: cancellationRequested ? "cancelled" : "failure",
              error: errorMessage,
              latency_samples_ms: latencySamples,
              results: clientResults,
            });
          } catch (_) {
            savedResult = null;
          }
        }
        setStatus(
          cancellationRequested ? "취소됨" : "실패",
          cancellationRequested ? "warning" : "error"
        );
        if (savedResult) {
          renderCompletedResult(savedResult);
        }
      } finally {
        progress.stop();
        activeSessionId = "";
        activeController = null;
        setRunning(false);
      }
    }

    modeButtons.forEach((button) => {
      button.addEventListener("click", () => setMeasurementMode(button.dataset.measurementMode));
    });
    criterionButtons.forEach((button) => {
      button.addEventListener("click", () => setHttpCriterion(button.dataset.httpCriterion));
    });
    actionButtons.forEach((button) => {
      button.addEventListener("click", () => runAction(button.dataset.sustainedAction));
    });
    cancelButton.addEventListener("click", () => {
      cancellationRequested = true;
      progress.stop();
      if (activeController) {
        activeController.abort();
      }
    });
    setHttpCriterion("simple");
    setMeasurementMode("http");
  }

  initSustainedCheck();
})();
