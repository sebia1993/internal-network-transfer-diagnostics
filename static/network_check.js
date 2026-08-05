(function () {
  const MB = 1024 * 1024;
  const CHUNK_SIZE = MB;

  function initTabs() {
    const buttons = document.querySelectorAll("[data-tab-button]");
    const panels = document.querySelectorAll("[data-tab-panel]");

    function activateTab(button) {
      const targetId = button.dataset.tabButton;
      buttons.forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", active ? "true" : "false");
        item.tabIndex = active ? 0 : -1;
      });
      panels.forEach((panel) => {
        const active = panel.id === targetId;
        panel.classList.toggle("is-active", active);
        panel.hidden = !active;
      });
    }

    buttons.forEach((button) => {
      button.addEventListener("click", () => activateTab(button));
    });
  }

  function initTabKeyboardNavigation() {
    document.querySelectorAll('[role="tablist"]').forEach((tablist) => {
      tablist.addEventListener("keydown", (event) => {
        if (!event.target.matches('[role="tab"]')) {
          return;
        }
        const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'))
          .filter((tab) => !tab.disabled);
        const currentIndex = tabs.indexOf(event.target);
        if (currentIndex < 0) {
          return;
        }
        let nextIndex = currentIndex;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          nextIndex = (currentIndex + 1) % tabs.length;
        } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
        } else if (event.key === "Home") {
          nextIndex = 0;
        } else if (event.key === "End") {
          nextIndex = tabs.length - 1;
        } else {
          return;
        }
        event.preventDefault();
        tabs[nextIndex].focus();
        tabs[nextIndex].click();
      });
    });
  }

  function setOperationState(element, state) {
    element.classList.remove("is-idle", "is-running", "is-success", "is-warning", "is-error");
    element.classList.add(`is-${state}`);
    element.dataset.state = state;
  }

  function initUploadForm() {
    const form = document.querySelector("[data-upload-form]");
    if (!form) {
      return;
    }
    const submitButton = form.querySelector("[data-upload-submit]");
    const submitStatus = form.querySelector("[data-upload-submit-status]");
    const defaultButtonText = submitButton.textContent;
    let submitting = false;

    function resetSubmissionState() {
      submitting = false;
      form.setAttribute("aria-busy", "false");
      submitButton.disabled = false;
      submitButton.textContent = defaultButtonText;
      submitStatus.textContent = "대기 · 파일을 선택한 뒤 업로드를 누르세요.";
      setOperationState(submitStatus, "idle");
    }

    form.addEventListener("submit", (event) => {
      if (submitting) {
        event.preventDefault();
        return;
      }
      submitting = true;
      form.setAttribute("aria-busy", "true");
      submitButton.disabled = true;
      submitButton.textContent = "업로드 중…";
      submitStatus.textContent = "진행 중 · 업로드가 끝날 때까지 창을 닫거나 다시 누르지 마세요.";
      setOperationState(submitStatus, "running");
    });
    window.addEventListener("pageshow", resetSubmissionState);
  }

  function formatSpeed(mbps) {
    if (!Number.isFinite(mbps) || mbps <= 0) {
      return "-";
    }
    const mbpsText = mbps >= 100 ? mbps.toFixed(1) : mbps.toFixed(2);
    const megaBytesPerSecond = mbps / 8;
    const mbText = megaBytesPerSecond >= 100 ? megaBytesPerSecond.toFixed(1) : megaBytesPerSecond.toFixed(2);
    return `${mbpsText} Mbps / ${mbText} MB/s`;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) {
      return "-";
    }
    return `${seconds.toFixed(2)}초`;
  }

  function formatDataAmount(byteCount) {
    if (!Number.isFinite(byteCount) || byteCount < 0) {
      return "-";
    }
    const megaBytes = byteCount / MB;
    if (megaBytes >= 1024) {
      return `${(megaBytes / 1024).toFixed(2)} GB (${megaBytes.toFixed(0)} MB)`;
    }
    return `${megaBytes.toFixed(megaBytes >= 100 ? 0 : 1)} MB`;
  }

  function formatOneGigabyteEstimate(mbps) {
    if (!Number.isFinite(mbps) || mbps <= 0) {
      return "-";
    }
    const seconds = 1024 / (mbps / 8);
    if (seconds < 60) {
      return `약 ${seconds.toFixed(1)}초`;
    }
    if (seconds < 3600) {
      const minutes = Math.floor(seconds / 60);
      return `약 ${minutes}분 ${Math.round(seconds % 60)}초`;
    }
    const hours = Math.floor(seconds / 3600);
    return `약 ${hours}시간 ${Math.round((seconds % 3600) / 60)}분`;
  }

  function buildUrl(baseUrl, params) {
    const url = new URL(baseUrl, window.location.href);
    Object.entries(params || {}).forEach(([key, value]) => {
      url.searchParams.set(key, String(value));
    });
    url.searchParams.set("_", `${Date.now()}-${Math.random()}`);
    return url.toString();
  }

  function initNetworkCheck() {
    const root = document.querySelector("[data-network-check]");
    if (!root) {
      return;
    }

    const sizeSelect = root.querySelector("[data-network-size]");
    const actionButtons = root.querySelectorAll("[data-check-action]");
    const cancelButton = root.querySelector("[data-cancel-check]");
    const statusText = root.querySelector("[data-network-status]");
    const progressBar = root.querySelector("[data-progress-bar]");
    const progressTrack = progressBar.closest('[role="progressbar"]');
    const progressText = root.querySelector("[data-progress-text]");
    const averageSpeed = root.querySelector("[data-average-speed]");
    const intervalSpeed = root.querySelector("[data-interval-speed]");
    const summary = root.querySelector("[data-summary]");
    const resultList = root.querySelector("[data-result-list]");
    const guidance = root.querySelector("[data-network-guidance]");
    const resultPanel = statusText.closest("[data-http-criterion-result]");
    const measurementModeButtons = root.querySelectorAll("[data-measurement-mode]");
    const criterionButtons = root.querySelectorAll("[data-http-criterion]");

    let running = false;
    let activeController = null;
    let lastProgressBytes = 0;
    let lastProgressAt = 0;

    function setRunning(nextRunning) {
      running = nextRunning;
      actionButtons.forEach((button) => {
        button.disabled = nextRunning;
      });
      cancelButton.disabled = !nextRunning;
      cancelButton.hidden = !nextRunning;
      sizeSelect.disabled = nextRunning;
      measurementModeButtons.forEach((button) => {
        button.disabled = nextRunning;
      });
      criterionButtons.forEach((button) => {
        button.disabled = nextRunning;
      });
      root.dataset.simpleRunning = nextRunning ? "true" : "";
      resultPanel.setAttribute("aria-busy", nextRunning ? "true" : "false");
    }

    function setStatus(message, state, guidanceMessage) {
      const operationState = state || "idle";
      statusText.textContent = message;
      statusText.setAttribute("aria-live", operationState === "error" ? "assertive" : "polite");
      setOperationState(statusText, operationState);
      const guidanceByState = {
        idle: "대기 · 측정 종류와 데이터량을 선택한 뒤 측정을 시작하세요.",
        running: "진행 중 · 완료되거나 취소될 때까지 중복 실행할 수 없습니다.",
        success: "정상 완료 · 측정값을 확인하세요. 완료 표시는 네트워크 정상 판정이 아닙니다.",
        warning: "주의 · 측정이 취소되었습니다. 필요하면 동일 조건으로 다시 시작하세요.",
        error: "장애 · 오류 내용을 확인하고 서버 주소, 방화벽, 연결 상태를 점검한 뒤 다시 시도하세요.",
      };
      guidance.textContent = guidanceMessage || guidanceByState[operationState];
      guidance.setAttribute("aria-live", operationState === "error" ? "assertive" : "polite");
      setOperationState(guidance, operationState);
      progressTrack.dataset.state = operationState;
    }

    function updateProgressAccessibility(percent, label) {
      const boundedPercent = Math.min(100, Math.max(0, Number(percent) || 0));
      progressTrack.setAttribute("aria-valuenow", boundedPercent.toFixed(1));
      progressTrack.setAttribute(
        "aria-valuetext",
        `${label || statusText.textContent} · ${boundedPercent.toFixed(1)}%`
      );
    }

    function resetProgress(message) {
      setStatus(message, "running");
      progressBar.style.width = "0%";
      progressText.textContent = "0%";
      updateProgressAccessibility(0, message);
      averageSpeed.textContent = "-";
      intervalSpeed.textContent = "-";
      summary.textContent = "-";
      lastProgressBytes = 0;
      lastProgressAt = performance.now();
    }

    function updateProgress(bytesDone, totalBytes, startedAt) {
      const now = performance.now();
      const elapsedSeconds = Math.max((now - startedAt) / 1000, 0.001);
      const percent = Math.min(100, (bytesDone / totalBytes) * 100);
      const averageMbps = (bytesDone * 8) / elapsedSeconds / 1_000_000;
      const intervalSeconds = Math.max((now - lastProgressAt) / 1000, 0.001);
      const intervalBytes = Math.max(0, bytesDone - lastProgressBytes);
      const intervalMbps = (intervalBytes * 8) / intervalSeconds / 1_000_000;
      progressBar.style.width = `${percent.toFixed(1)}%`;
      progressText.textContent = `${percent.toFixed(1)}%`;
      updateProgressAccessibility(percent);
      averageSpeed.textContent = formatSpeed(averageMbps);
      intervalSpeed.textContent = formatSpeed(intervalMbps);
      lastProgressBytes = bytesDone;
      lastProgressAt = now;
    }

    function completeResult(label, bytesDone, totalBytes, startedAt) {
      const elapsedSeconds = Math.max((performance.now() - startedAt) / 1000, 0.001);
      const mbps = (bytesDone * 8) / elapsedSeconds / 1_000_000;
      updateProgress(bytesDone, totalBytes, startedAt);
      return {
        label,
        sizeMb: totalBytes / MB,
        bytesDone,
        elapsedSeconds,
        mbps,
      };
    }

    function renderResults(results) {
      resultList.innerHTML = "";
      results.forEach((result) => {
        const item = document.createElement("div");
        item.className = "transfer-result";

        const title = document.createElement("h3");
        title.textContent = `${result.label} 결과`;
        item.appendChild(title);

        const details = document.createElement("dl");
        details.className = "transfer-result-details";
        const rows = [
          ["전송한 데이터", formatDataAmount(result.bytesDone)],
          ["걸린 시간", formatDuration(result.elapsedSeconds)],
          ["최종 평균 속도", formatSpeed(result.mbps)],
          ["파일 전송량", `초당 ${(result.mbps / 8).toFixed(result.mbps >= 800 ? 1 : 2)} MB`],
          ["1GB 예상 시간", formatOneGigabyteEstimate(result.mbps)],
        ];
        rows.forEach(([label, value]) => {
          const row = document.createElement("div");
          const term = document.createElement("dt");
          const description = document.createElement("dd");
          term.textContent = label;
          description.textContent = value;
          row.append(term, description);
          details.appendChild(row);
        });
        item.appendChild(details);
        resultList.appendChild(item);
      });
      summary.textContent = `${results.map((result) => result.label).join("·")} 측정 완료`;
    }

    function renderPartialResults(results, action, errorMessage, cancelled) {
      renderResults(results);
      const completedLabels = new Set(results.map((result) => result.label));
      let retryLabel = action === "upload" ? "업로드" : "다운로드";
      if (action === "full") {
        retryLabel = completedLabels.has("업로드") ? "다운로드" : "업로드";
      }
      const completedText = Array.from(completedLabels).join("·");
      setStatus(
        "부분 완료",
        "warning",
        `주의 · ${completedText} 결과는 보존했습니다. 연결 상태를 확인한 뒤 ${retryLabel} 측정만 다시 실행하세요.`
      );
      summary.setAttribute("aria-live", "assertive");
      summary.textContent = `부분 완료 · ${completedText} 측정 성공 · ${retryLabel} 측정 ${cancelled ? "중단" : "실패"}: ${errorMessage}`;
    }

    function makeUploadChunk() {
      const chunk = new Uint8Array(CHUNK_SIZE);
      for (let index = 0; index < chunk.length; index += 1) {
        chunk[index] = index % 251;
      }
      return chunk;
    }

    async function fetchJson(url, options, label) {
      let response;
      try {
        response = await fetch(url, options);
      } catch (error) {
        if (error.name === "AbortError") {
          throw new Error("측정이 취소되었습니다.");
        }
        throw new Error(`${label} 요청에 실패했습니다. 서버 주소, 방화벽, 브라우저 연결을 확인하세요. (${error.message})`);
      }

      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || `${label} 요청이 실패했습니다.`);
      }
      return payload;
    }

    async function runDownload(sizeMb, signal) {
      const totalBytes = sizeMb * MB;
      const startedAt = performance.now();
      let receivedBytes = 0;

      resetProgress("다운로드 측정 중");
      let response;
      try {
        response = await fetch(buildUrl(root.dataset.downloadUrl, { size_mb: sizeMb }), {
          cache: "no-store",
          signal,
        });
      } catch (error) {
        if (error.name === "AbortError") {
          throw new Error("측정이 취소되었습니다.");
        }
        throw new Error(`다운로드 측정 요청에 실패했습니다. 서버 주소, 방화벽, 브라우저 연결을 확인하세요. (${error.message})`);
      }
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || "다운로드 측정 요청이 실패했습니다.");
      }
      if (!response.body) {
        throw new Error("현재 브라우저는 스트리밍 다운로드를 지원하지 않습니다.");
      }

      const reader = response.body.getReader();
      while (true) {
        let result;
        try {
          result = await reader.read();
        } catch (error) {
          if (error.name === "AbortError") {
            throw new Error("측정이 취소되었습니다.");
          }
          throw error;
        }
        const { done, value } = result;
        if (done) {
          break;
        }
        receivedBytes += value.byteLength;
        updateProgress(receivedBytes, totalBytes, startedAt);
      }

      return completeResult("다운로드", receivedBytes, totalBytes, startedAt);
    }

    async function runUpload(sizeMb, signal) {
      const totalBytes = sizeMb * MB;
      const startedAt = performance.now();
      const uploadBaseUrl = root.dataset.uploadUrl;
      const chunk = makeUploadChunk();
      let sessionId = "";
      let sentBytes = 0;

      resetProgress("업로드 측정 중");
      try {
        const started = await fetchJson(
          buildUrl(`${uploadBaseUrl}/start`, { size_mb: sizeMb }),
          { method: "POST", cache: "no-store", signal },
          "업로드 시작"
        );
        sessionId = started.session_id;

        while (sentBytes < totalBytes) {
          const remaining = totalBytes - sentBytes;
          const nextSize = Math.min(CHUNK_SIZE, remaining);
          const body = nextSize === CHUNK_SIZE ? chunk : chunk.subarray(0, nextSize);
          const chunkNumber = Math.floor(sentBytes / CHUNK_SIZE) + 1;
          const chunkResult = await fetchJson(
            buildUrl(`${uploadBaseUrl}/chunk/${encodeURIComponent(sessionId)}`),
            {
              method: "POST",
              body,
              cache: "no-store",
              signal,
              headers: {
                "Content-Type": "application/octet-stream",
              },
            },
            `업로드 ${chunkNumber}번째 조각`
          );
          sentBytes = Number(chunkResult.bytes_received);
          updateProgress(sentBytes, totalBytes, startedAt);
        }

        const finished = await fetchJson(
          buildUrl(`${uploadBaseUrl}/finish/${encodeURIComponent(sessionId)}`),
          { method: "POST", cache: "no-store", signal },
          "업로드 완료"
        );
        if (finished.status !== "success") {
          throw new Error(
            finished.error || "업로드 측정 결과를 저장하지 못했습니다."
          );
        }
        updateProgress(totalBytes, totalBytes, startedAt);
        return {
          label: "업로드",
          sizeMb,
          bytesDone: Number(finished.bytes_transferred),
          elapsedSeconds: Number(finished.duration_seconds),
          mbps: Number(finished.mbps),
        };
      } catch (error) {
        if (sessionId) {
          await fetch(buildUrl(`${uploadBaseUrl}/finish/${encodeURIComponent(sessionId)}`), {
            method: "POST",
            cache: "no-store",
          }).catch(() => {});
        }
        throw error;
      }
    }

    async function runAction(action) {
      if (
        running ||
        root.dataset.sustainedRunning === "true" ||
        root.dataset.probeRunning === "true"
      ) {
        return;
      }

      const sizeMb = Number.parseInt(sizeSelect.value, 10);
      if (sizeMb === 1024 && !window.confirm("1024MB 측정은 사내망과 서버 PC에 부하를 줄 수 있습니다. 계속 진행할까요?")) {
        return;
      }

      const results = [];
      resultList.innerHTML = "";
      activeController = new AbortController();
      setRunning(true);

      try {
        if (action === "upload" || action === "full") {
          results.push(await runUpload(sizeMb, activeController.signal));
        }
        if (action === "download" || action === "full") {
          results.push(await runDownload(sizeMb, activeController.signal));
        }
        setStatus("완료", "success");
        renderResults(results);
      } catch (error) {
        const cancelled = error.message === "측정이 취소되었습니다.";
        const errorMessage = error.message || "측정 중 오류가 발생했습니다.";
        if (results.length > 0) {
          renderPartialResults(results, action, errorMessage, cancelled);
        } else {
          setStatus(cancelled ? "취소됨" : "실패", cancelled ? "warning" : "error");
          summary.setAttribute("aria-live", cancelled ? "polite" : "assertive");
          summary.textContent = errorMessage;
        }
      } finally {
        activeController = null;
        setRunning(false);
      }
    }

    actionButtons.forEach((button) => {
      button.addEventListener("click", () => {
        runAction(button.dataset.checkAction);
      });
    });

    cancelButton.addEventListener("click", () => {
      if (activeController) {
        activeController.abort();
      }
    });
  }

  initUploadForm();
  initTabs();
  initTabKeyboardNavigation();
  initNetworkCheck();
})();
