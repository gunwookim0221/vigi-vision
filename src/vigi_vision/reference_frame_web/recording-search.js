(function () {
  const panel = document.querySelector("#recording-search-panel");
  const confirmedTime = document.querySelector("#recording-search-confirmed-time");
  const timezone = document.querySelector("#recording-search-timezone");
  const endInput = document.querySelector("#recording-search-end");
  const startAction = document.querySelector("#recording-search-start");
  const status = document.querySelector("#recording-search-status");
  const error = document.querySelector("#recording-search-error");
  const result = document.querySelector("#recording-search-result");
  const resultKind = document.querySelector("#recording-search-result-kind");
  const resultReason = document.querySelector("#recording-search-result-reason");
  const candidateWorkflow = [
    "#candidate-intro",
    "#candidate-request-panel",
    "#candidate-results-panel",
    "#selected-preview-panel",
    "#confirmation-panel",
  ].map((selector) => document.querySelector(selector)).filter((element) => element !== null);
  const INVESTIGATION_PATTERN = /^object-disappearance-v3-ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z$/;
  const RUN_PATTERN = /^search-run-[0-9a-f]{32}$/;
  const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const LOCAL_TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/;
  const TERMINAL = new Set(["FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED", "INTERRUPTED", "CORRUPT"]);
  const START_KEYS = Object.freeze(["request_id", "investigation_id", "run_id", "status", "status_url"]);
  const STATUS_KEYS = Object.freeze([
    "investigation_id", "run_id", "schema_version", "status", "reason_code",
    "terminal_result_id", "phase8_status", "phase8_reason",
  ]);
  const CONFIRMATION_KEYS = Object.freeze([
    "channel_id", "candidate_offset_seconds", "reference_frame_resource_id",
    "requested_time_utc", "source_timezone", "timing", "source_width", "source_height", "roi",
  ]);
  const ERROR_MESSAGES = Object.freeze({
    invalid_recording_search_request: "검색 종료 시각을 확인하세요.",
    investigation_not_found: "확인된 조사를 찾을 수 없습니다.",
    reconfirmation_required: "녹화 기록 검색 전에 조사를 다시 확인하세요.",
    already_running: "다른 녹화 기록 검색이 진행 중입니다.",
    request_conflict: "이 검색 요청 식별자는 다른 입력에 이미 사용되었습니다.",
    confirmation_unavailable: "확인된 조사를 안전하게 불러올 수 없습니다.",
    confirmation_corrupt: "확인된 조사 기록이 손상되었습니다.",
    search_run_corrupt: "검색 실행 기록이 손상되었습니다.",
    recording_search_unavailable: "녹화 기록 검색을 사용할 수 없습니다.",
    internal_error: "검색 작업을 안전하게 완료할 수 없습니다.",
  });
  let confirmation = null;
  let activeRun = null;
  let controller = null;
  let timer = null;
  let pollCount = 0;
  let submitting = false;

  function hasExactKeys(value, keys) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
      && Object.keys(value).length === keys.length
      && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
  }

  function validUtc(value) {
    return typeof value === "string"
      && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)$/.test(value)
      && !Number.isNaN(new Date(value).getTime());
  }

  function setStatus(message, state, busy = false) {
    const busyText = String(busy);
    if (status.textContent !== message) status.textContent = message;
    if (status.dataset.state !== state) status.dataset.state = state;
    status.setAttribute("aria-busy", busyText);
  }

  function fail(code) {
    stopPolling();
    const message = ERROR_MESSAGES[code] ?? ERROR_MESSAGES.internal_error;
    setStatus(message, "error");
    error.textContent = message;
    error.hidden = false;
    error.focus?.({ preventScroll: true });
    submitting = false;
    renderInput();
  }

  function localFromUtc(value, zone) {
    const date = new Date(value);
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: zone,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    });
    const fields = Object.fromEntries(formatter.formatToParts(date).map((part) => [part.type, part.value]));
    return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}:${fields.second}`;
  }

  function utcFromLocal(value, zone) {
    if (!LOCAL_TIME_PATTERN.test(value)) return null;
    const suffix = zone === "UTC" ? "Z" : zone === "Asia/Seoul" ? "+09:00" : null;
    if (suffix === null) return null;
    const parsed = new Date(`${value}${suffix}`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function validSearchEnd() {
    if (confirmation === null) return false;
    const end = utcFromLocal(endInput.value, confirmation.sourceTimezone);
    if (end === null) return false;
    const duration = (end.getTime() - new Date(confirmation.anchorTimeUtc).getTime()) / 1000;
    return Number.isInteger(duration) && duration > 0 && duration <= 600 && end.getTime() <= Date.now();
  }

  function renderInput() {
    startAction.disabled = submitting || activeRun !== null || !validSearchEnd();
  }

  function showConfirmation(value) {
    confirmation = value;
    panel.hidden = false;
    confirmedTime.textContent = localFromUtc(value.anchorTimeUtc, value.sourceTimezone);
    timezone.textContent = value.sourceTimezone;
    if (!LOCAL_TIME_PATTERN.test(endInput.value)) {
      const suggested = new Date(Math.min(
        new Date(value.anchorTimeUtc).getTime() + 600000,
        Date.now(),
      ));
      endInput.value = localFromUtc(suggested.toISOString(), value.sourceTimezone);
    }
    setStatus("검색 종료 시각을 검토한 뒤 검색을 시작하세요.", "ready");
    error.hidden = true;
    renderInput();
    resumeFromLocation();
  }

  function confirmationFromPayload(payload) {
    const value = payload?.confirmation;
    if (!hasExactKeys(payload, [
      "investigation_id", "outcome", "status", "schema_version", "confirmed_at_utc",
      "artifact_directory_relative", "confirmation",
    ]) || !hasExactKeys(value, CONFIRMATION_KEYS)
      || payload.schema_version !== 3 || payload.status !== "confirmed"
      || !INVESTIGATION_PATTERN.test(payload.investigation_id)
      || !validUtc(value.requested_time_utc)
      || !Number.isInteger(value.candidate_offset_seconds)
      || !["Asia/Seoul", "UTC"].includes(value.source_timezone)) {
      return null;
    }
    const anchor = new Date(
      new Date(value.requested_time_utc).getTime() - value.candidate_offset_seconds * 1000,
    );
    return {
      investigationId: payload.investigation_id,
      anchorTimeUtc: anchor.toISOString().replace(".000Z", "Z"),
      sourceTimezone: value.source_timezone,
    };
  }

  async function loadConfirmation(investigationId) {
    controller?.abort();
    controller = typeof AbortController === "function" ? new AbortController() : null;
    setStatus("확인된 조사를 불러오는 중입니다…", "loading", true);
    try {
      const response = await fetch(
        `/api/v1/investigation-confirmations/${encodeURIComponent(investigationId)}`,
        { signal: controller?.signal },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        fail(payload?.error?.code);
        return;
      }
      const loaded = confirmationFromPayload(payload);
      if (loaded === null || loaded.investigationId !== investigationId) {
        fail("confirmation_unavailable");
        return;
      }
      showConfirmation(loaded);
    } catch (caught) {
      if (caught?.name !== "AbortError") fail("confirmation_unavailable");
    }
  }

  function rememberRun(receipt) {
    activeRun = { investigationId: receipt.investigation_id, runId: receipt.run_id };
    try {
      const location = new URL(window.location.href);
      location.searchParams.set("investigation_id", receipt.investigation_id);
      location.searchParams.set("run_id", receipt.run_id);
      window.history.replaceState(null, "", location);
    } catch (_caught) {
      // Polling remains active even if URL history is unavailable.
    }
  }

  function validStart(payload, expected) {
    return hasExactKeys(payload, START_KEYS)
      && payload.request_id === expected.requestId
      && payload.investigation_id === expected.investigationId
      && payload.run_id === `search-run-${expected.requestId.replaceAll("-", "")}`
      && RUN_PATTERN.test(payload.run_id)
      && payload.status === "ACCEPTED"
      && payload.status_url === `/api/v1/recording-searches/${payload.investigation_id}/${payload.run_id}`;
  }

  async function start(event) {
    event.preventDefault();
    if (submitting || activeRun !== null || !validSearchEnd()) return;
    const requestId = window.crypto?.randomUUID?.();
    if (typeof requestId !== "string" || !UUID_V4_PATTERN.test(requestId)) {
      fail("recording_search_unavailable");
      return;
    }
    const expected = {
      requestId,
      investigationId: confirmation.investigationId,
      searchEnd: endInput.value,
    };
    submitting = true;
    renderInput();
    setStatus("검색 요청을 접수하는 중입니다…", "loading", true);
    error.hidden = true;
    try {
      const response = await fetch("/api/v1/recording-searches", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          investigation_id: expected.investigationId,
          search_end: expected.searchEnd,
          request_id: expected.requestId,
        }),
      });
      const payload = await response.json().catch(() => null);
      submitting = false;
      if (response.status !== 202) {
        fail(payload?.error?.code);
        return;
      }
      if (!validStart(payload, expected)) {
        fail("internal_error");
        return;
      }
      rememberRun(payload);
      setStatus("검색 요청이 접수되었습니다.", "accepted", true);
      pollCount = 0;
      schedulePoll(0);
    } catch (_caught) {
      submitting = false;
      fail("recording_search_unavailable");
    }
  }

  function validStatus(payload) {
    return hasExactKeys(payload, STATUS_KEYS)
      && payload.investigation_id === activeRun?.investigationId
      && payload.run_id === activeRun?.runId
      && Number.isInteger(payload.schema_version) && payload.schema_version >= 0
      && typeof payload.status === "string"
      && (payload.reason_code === null || typeof payload.reason_code === "string")
      && (payload.terminal_result_id === null || typeof payload.terminal_result_id === "string")
      && payload.phase8_status === null && payload.phase8_reason === null;
  }

  function terminalText(kind) {
    return {
      FOUND: "요청한 검색 범위에서 대상이 사라진 구간을 찾았습니다.",
      NOT_FOUND: "요청한 검색 범위에서는 대상이 사라진 구간을 찾지 못했습니다.",
      INCONCLUSIVE: "요청한 검색 범위의 증거만으로 결과를 확정할 수 없습니다.",
      FAILED: "검색이 안전하게 실패했습니다.",
      INTERRUPTED: "검색이 중단되었습니다.",
      CORRUPT: "검색 실행 기록을 안전하게 읽을 수 없습니다.",
    }[kind] ?? "검색 상태를 확인할 수 없습니다.";
  }

  function finish(payload) {
    stopPolling();
    setStatus("녹화 기록 검색이 종료되었습니다.", "complete");
    resultKind.textContent = terminalText(payload.status);
    resultReason.textContent = payload.reason_code === null
      ? "서버가 추가 사유를 제공하지 않았습니다."
      : `결과 사유: ${payload.reason_code}`;
    result.hidden = false;
    result.focus?.({ preventScroll: true });
  }

  async function poll() {
    if (activeRun === null) return;
    pollCount += 1;
    if (pollCount > 1350) {
      fail("recording_search_unavailable");
      return;
    }
    try {
      const response = await fetch(
        `/api/v1/recording-searches/${encodeURIComponent(activeRun.investigationId)}/${encodeURIComponent(activeRun.runId)}`,
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok || !validStatus(payload)) {
        fail(payload?.error?.code);
        return;
      }
      if (TERMINAL.has(payload.status)) {
        finish(payload);
        return;
      }
      if (!["ACCEPTED", "RUNNING"].includes(payload.status)) {
        fail("internal_error");
        return;
      }
      setStatus(
        payload.status === "ACCEPTED" ? "검색 요청이 대기 중입니다." : "녹화 기록을 검색하는 중입니다…",
        "loading",
        true,
      );
      schedulePoll(2000);
    } catch (_caught) {
      fail("recording_search_unavailable");
    }
  }

  function schedulePoll(delay) {
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(() => { void poll(); }, delay);
  }

  function stopPolling() {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
    status.setAttribute("aria-busy", "false");
  }

  function resumeFromLocation() {
    let runId = null;
    try {
      runId = new URL(window.location.href).searchParams.get("run_id");
    } catch (_caught) {
      return;
    }
    if (runId !== null && RUN_PATTERN.test(runId) && confirmation !== null) {
      activeRun = { investigationId: confirmation.investigationId, runId };
      setStatus("이전 검색 상태를 다시 확인하는 중입니다…", "loading", true);
      schedulePoll(0);
    }
  }

  function receiveConfirmation(event) {
    const detail = event?.detail;
    if (detail?.schemaVersion !== 3 || !INVESTIGATION_PATTERN.test(detail.investigationId)
      || !validUtc(detail.anchorTimeUtc) || !["Asia/Seoul", "UTC"].includes(detail.sourceTimezone)) {
      return;
    }
    showConfirmation(detail);
  }

  function loadFromLocation() {
    let investigationId = null;
    try {
      investigationId = new URL(window.location.href).searchParams.get("investigation_id");
    } catch (_caught) {
      return;
    }
    if (investigationId !== null && INVESTIGATION_PATTERN.test(investigationId)) {
      candidateWorkflow.forEach((element) => {
        element.hidden = true;
      });
      panel.hidden = false;
      panel.scrollIntoView?.({ block: "start", behavior: "auto" });
      void loadConfirmation(investigationId);
    }
  }

  endInput.addEventListener("input", renderInput);
  startAction.addEventListener("click", start);
  window.addEventListener("vigi:investigation-confirmed", receiveConfirmation);
  window.addEventListener("pagehide", () => {
    controller?.abort();
    stopPolling();
  });
  window.vigiVisionRecordingSearch = Object.freeze({
    getState: () => ({
      investigationId: confirmation?.investigationId ?? null,
      runId: activeRun?.runId ?? null,
      submitting,
      polling: timer !== null,
    }),
  });
  loadFromLocation();
}());
