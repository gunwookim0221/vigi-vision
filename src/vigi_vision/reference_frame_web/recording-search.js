(function () {
  const panel = document.querySelector("#recording-search-panel");
  const confirmedTime = document.querySelector("#recording-search-confirmed-time");
  const timezone = document.querySelector("#recording-search-timezone");
  const endInput = document.querySelector("#recording-search-end");
  const quickRanges = document.querySelector("#recording-search-quick-ranges");
  const startAction = document.querySelector("#recording-search-start");
  const status = document.querySelector("#recording-search-status");
  const error = document.querySelector("#recording-search-error");
  const result = document.querySelector("#recording-search-result");
  const resultKind = document.querySelector("#recording-search-result-kind");
  const resultReason = document.querySelector("#recording-search-result-reason");
  const resultTiming = document.querySelector("#recording-search-result-timing");
  const lastPresent = document.querySelector("#recording-search-last-present");
  const firstAbsent = document.querySelector("#recording-search-first-absent");
  const disappearanceInterval = document.querySelector("#recording-search-interval");
  const observedRange = document.querySelector("#recording-search-observed-range");
  const evidencePanel = document.querySelector("#recording-search-evidence");
  const evidenceStatus = document.querySelector("#recording-search-evidence-status");
  const baselineImage = document.querySelector("#recording-search-baseline-image");
  const baselineRoi = document.querySelector("#recording-search-baseline-roi");
  const baselineTime = document.querySelector("#recording-search-baseline-time");
  const baselineHighlight = document.querySelector("#recording-search-baseline-highlight");
  const endImage = document.querySelector("#recording-search-end-image");
  const endRoi = document.querySelector("#recording-search-end-roi");
  const endTime = document.querySelector("#recording-search-end-time");
  const endHighlight = document.querySelector("#recording-search-end-highlight");
  const evidenceMetrics = document.querySelector("#recording-search-evidence-metrics");
  const coarseEvidence = document.querySelector("#recording-search-coarse-evidence");
  const coarseList = document.querySelector("#recording-search-coarse-list");
  const foundEvidence = document.querySelector("#recording-search-found-evidence");
  const lastPresentImage = document.querySelector("#recording-search-last-present-image");
  const lastPresentRoi = document.querySelector("#recording-search-last-present-roi");
  const lastPresentTime = document.querySelector("#recording-search-last-present-time");
  const lastPresentHighlight = document.querySelector("#recording-search-last-present-highlight");
  const firstAbsentImage = document.querySelector("#recording-search-first-absent-image");
  const firstAbsentRoi = document.querySelector("#recording-search-first-absent-roi");
  const firstAbsentTime = document.querySelector("#recording-search-first-absent-time");
  const firstAbsentHighlight = document.querySelector("#recording-search-first-absent-highlight");
  const reviewClipStatus = document.querySelector("#recording-search-review-clip-status");
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
  const LOCAL_TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/;
  const TERMINAL = new Set(["FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED", "INTERRUPTED", "CORRUPT"]);
  const REQUEST_TIMEOUT_MS = 15_000;
  // The backend invocation ceiling is 2,520 seconds; retain a bounded
  // three-minute observation margin without turning polling into a promise
  // of completion after the client has gone away.
  const CLIENT_POLL_DEADLINE_MS = 45 * 60 * 1_000;
  const DEFAULT_SEARCH_DURATION_SECONDS = 30 * 60;
  const MAX_SEARCH_DURATION_SECONDS = 2 * 60 * 60;
  const STATUS_RETRY_LIMIT = 5;
  const STATUS_RETRY_INITIAL_MS = 2_000;
  const STATUS_RETRY_MAX_MS = 15_000;
  const PHASE8_STATUSES = new Set([
    "NOT_REQUESTED", "RETRYABLE", "READY", "MEDIA_MISSING", "MEDIA_CORRUPT",
    "DELETING", "DELETED",
  ]);
  const PHASE8_REASONS_BY_STATUS = Object.freeze({
    NOT_REQUESTED: new Set([null, "successor_slice5_does_not_create_handoffs"]),
    RETRYABLE: new Set(["phase8_clip_failed", "phase8_media_unavailable", "phase8_media_corrupt"]),
    READY: new Set([null]),
    MEDIA_MISSING: new Set(["phase8_media_unavailable"]),
    MEDIA_CORRUPT: new Set(["phase8_media_corrupt"]),
    DELETING: new Set([null]),
    DELETED: new Set([null]),
  });
  const RUN_STORAGE_KEY = "vigiVision.recordingSearch.activeRun.v1";
  const RUN_STORAGE_KEYS = Object.freeze([
    "version", "investigation_id", "run_id", "request_id", "search_end", "duration_seconds",
  ]);
  const START_KEYS = Object.freeze(["request_id", "investigation_id", "run_id", "status", "status_url"]);
  const STATUS_KEYS = Object.freeze([
    "investigation_id", "run_id", "schema_version", "status", "reason_code",
    "terminal_result_id", "phase8_status", "phase8_reason", "terminal_details",
  ]);
  const TERMINAL_DETAIL_KEYS = Object.freeze([
    "last_present_time_utc", "first_absent_time_utc", "observed_start_time_utc",
    "observed_end_time_utc", "coverage_complete", "source_timezone",
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
    successor_unavailable: "장시간 녹화 검색 기능을 준비할 수 없습니다. 서버 설정을 확인하세요.",
    recording_unavailable: "해당 검색 범위의 녹화 기록을 충분히 확인할 수 없습니다.",
    status_confirmation_failed: "검색 상태를 확인할 수 없습니다.",
    search_run_not_found: "접수된 검색 실행을 찾을 수 없습니다.",
    internal_error: "검색 작업을 안전하게 완료할 수 없습니다.",
  });
  const RESULT_REASON_MESSAGES = Object.freeze({
    disappearance_confirmed: "대상이 사라진 구간이 확인되었습니다.",
    complete_present_coverage: "관측 가능한 검색 범위에서는 대상이 계속 존재했습니다.",
    no_present_absent_bracket: "관측 가능한 증거에서 소실 전환 구간을 찾지 못했습니다.",
    unavailable_gap: "녹화 공백 또는 확인되지 않은 범위가 있어 결론을 낼 수 없습니다.",
    incomplete_coverage: "일부 검색 범위를 확인하지 못해 결론을 낼 수 없습니다.",
    INCOMPLETE_MEDIA_COVERAGE: "사용 가능한 녹화가 요청 종료 전 끝나 전체 구간을 판단할 수 없습니다.",
    VISUAL_INDETERMINATE: "사용 가능한 화면만으로 대상의 존재 여부를 신뢰성 있게 판단할 수 없습니다.",
    INCOMPLETE_VISUAL_EVIDENCE: "판정에 필요한 화면 증거가 충분하지 않습니다.",
    BASELINE_ONLY_LOWER_BOUND: "기준 화면 이후의 존재 증거가 충분하지 않습니다.",
    indeterminate_observation: "일부 관측 프레임에서 대상의 존재 여부를 신뢰성 있게 판단할 수 없습니다.",
    insufficient_visual_evidence: "판정에 필요한 시각 증거가 충분하지 않습니다.",
    invalid_frame_or_roi: "판정 프레임 또는 ROI가 유효하지 않습니다.",
    frame_decode_failed: "관측 프레임을 안전하게 디코딩할 수 없습니다.",
    frame_resolution_mismatch: "관측 프레임의 해상도가 기준과 일치하지 않습니다.",
    target_unavailable_gap: "대상 시각에 녹화 공백이 있습니다.",
    target_recording_unavailable: "대상 시각의 녹화 기록을 확인할 수 없습니다.",
    target_replay_timeout: "대상 시각의 녹화 재생이 제한 시간 안에 끝나지 않았습니다.",
    target_replay_failed: "대상 시각의 녹화 재생에 실패했습니다.",
    target_decode_timeout: "대상 프레임 디코딩이 제한 시간 안에 끝나지 않았습니다.",
    target_decode_unavailable: "대상 프레임을 디코딩할 수 없습니다.",
    classifier_timeout: "화면 판정이 제한 시간 안에 끝나지 않았습니다.",
    classifier_failed: "화면 판정에 실패했습니다.",
    midpoint_gap: "소실 구간을 좁히는 중 녹화 공백이 확인되었습니다.",
    midpoint_acquisition_unavailable: "소실 구간을 좁힐 프레임을 가져올 수 없습니다.",
    midpoint_indeterminate: "소실 구간을 좁히는 중 화면 판정이 불확실했습니다.",
    midpoint_classification_unavailable: "소실 구간을 좁히는 화면 판정을 완료할 수 없습니다.",
    no_progress: "소실 구간을 더 좁힐 수 있는 관측이 부족합니다.",
    cancelled: "검색이 취소되었습니다.",
    abandoned_after_restart: "서버 재시작으로 검색이 중단되었습니다.",
    recording_unavailable: "해당 검색 범위의 녹화 기록을 충분히 확인할 수 없습니다.",
    successor_unavailable: "장시간 녹화 검색 기능을 준비할 수 없습니다. 서버 설정을 확인하세요.",
    media_probe_failed: "녹화 미디어를 검증할 수 없습니다.",
    media_probe_timeout: "녹화 미디어 검증이 제한 시간 안에 끝나지 않았습니다.",
    decoder_timeout: "녹화 프레임 디코딩이 제한 시간 안에 끝나지 않았습니다.",
    decoder_failed: "녹화 프레임 디코딩에 실패했습니다.",
    replay_authentication: "녹화 재생 인증에 실패했습니다.",
    replay_unavailable: "요청한 시각의 녹화를 사용할 수 없습니다.",
    replay_timeout: "녹화 재생이 제한 시간 안에 끝나지 않았습니다.",
    replay_failed: "녹화 재생에 실패했습니다.",
    acquisition_failed: "녹화 프레임 획득에 실패했습니다.",
    missing_pts: "녹화 프레임 시간 정보를 확인할 수 없습니다.",
    nonmonotonic_pts: "녹화 프레임 시간 순서가 유효하지 않습니다.",
    timestamp_reset: "녹화 프레임 시간 기준이 재설정되었습니다.",
    recording_gap: "녹화 구간 사이에 공백이 있습니다.",
    segment_boundary: "녹화 구간 경계를 안전하게 확인할 수 없습니다.",
    target_unavailable: "판정에 사용할 녹화 프레임이 없습니다.",
    insufficient_support: "판정에 필요한 지지 프레임이 부족합니다.",
    duplicate_frame: "중복 프레임 때문에 판정할 수 없습니다.",
    media_resource_exceeded: "녹화 미디어 자원 한도를 초과했습니다.",
    capacity_exhausted: "검색 처리 용량을 초과했습니다.",
    invocation_deadline_exhausted: "검색 전체 제한 시간을 초과했습니다.",
    successor_publication_corrupt: "검색 결과 기록이 손상되었습니다.",
    successor_publication_readback_failed: "검색 결과 기록을 다시 확인할 수 없습니다.",
    recording_search_execution_unavailable: "녹화 검색 실행기를 사용할 수 없습니다.",
    internal_error: "검색 작업을 안전하게 완료할 수 없습니다.",
  });
  let confirmation = null;
  let activeRun = null;
  let controller = null;
  let pollCount = 0;
  let submitting = false;
  let terminalReady = false;
  let lifecycleGeneration = 0;
  let lifecycle = null;

  function isCurrentLifecycle(owner) {
    return owner !== null && lifecycle === owner && !owner.closed
      && owner.generation === lifecycleGeneration;
  }

  function clearRequest(owner) {
    if (owner.requestTimer !== null) window.clearTimeout(owner.requestTimer);
    owner.requestTimer = null;
    owner.requestController = null;
    owner.requestActive = false;
    owner.requestKind = null;
  }

  function stopPolling(owner, announce = true) {
    if (owner === null) return;
    if (owner.pollTimer !== null) window.clearTimeout(owner.pollTimer);
    owner.pollTimer = null;
    if (announce && isCurrentLifecycle(owner)) status.setAttribute("aria-busy", "false");
  }

  function invalidateLifecycle(owner) {
    if (owner === null) return;
    owner.closed = true;
    stopPolling(owner, false);
    if (owner.requestTimer !== null) window.clearTimeout(owner.requestTimer);
    owner.requestTimer = null;
    owner.requestController?.abort();
    owner.requestController = null;
    owner.requestActive = false;
    owner.requestKind = null;
    if (lifecycle === owner) lifecycle = null;
    lifecycleGeneration += 1;
  }

  function createLifecycle() {
    if (lifecycle !== null) invalidateLifecycle(lifecycle);
    const owner = {
      generation: lifecycleGeneration + 1,
      deadlineAt: Date.now() + CLIENT_POLL_DEADLINE_MS,
      requestController: null,
      requestTimer: null,
      pollTimer: null,
      requestActive: false,
      transientFailures: 0,
      closed: false,
    };
    lifecycleGeneration = owner.generation;
    lifecycle = owner;
    return owner;
  }

  function requestControllerFor(owner, kind) {
    if (!isCurrentLifecycle(owner)) return null;
    if (owner.requestActive) {
      fail("internal_error", owner);
      return null;
    }
    const remaining = owner.deadlineAt - Date.now();
    if (remaining <= 0) {
      if (kind === "status") endStatusObservation(owner);
      else fail("recording_search_unavailable", owner);
      return null;
    }
    if (typeof AbortController !== "function") {
      fail(kind === "status" ? "status_confirmation_failed" : "recording_search_unavailable", owner);
      return null;
    }
    const requestController = new AbortController();
    owner.requestController = requestController;
    owner.requestActive = true;
    owner.requestKind = kind;
    owner.requestTimer = window.setTimeout(() => {
      if (!isCurrentLifecycle(owner) || !owner.requestActive || owner.requestController !== requestController) return;
      requestController?.abort();
      clearRequest(owner);
      if (kind === "status") retryStatus(owner);
      else fail("recording_search_unavailable", owner);
    }, Math.max(1, Math.min(REQUEST_TIMEOUT_MS, remaining)));
    return requestController;
  }

  function completeRequest(owner, requestController) {
    if (!isCurrentLifecycle(owner) || owner.requestController !== requestController) return false;
    if (owner.requestTimer !== null) window.clearTimeout(owner.requestTimer);
    owner.requestTimer = null;
    owner.requestController = null;
    owner.requestActive = false;
    owner.requestKind = null;
    return true;
  }

  function hasExactKeys(value, keys) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
      && Object.keys(value).length === keys.length
      && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
  }

  function validUtc(value) {
    return typeof value === "string"
      && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)
      && !Number.isNaN(new Date(value).getTime());
  }

  function setStatus(message, state, busy = false) {
    const busyText = String(busy);
    if (status.textContent !== message) status.textContent = message;
    if (status.dataset.state !== state) status.dataset.state = state;
    status.setAttribute("aria-busy", busyText);
  }

  function fail(code, owner = null) {
    if (owner !== null) {
      if (!isCurrentLifecycle(owner)) return;
      invalidateLifecycle(owner);
    }
    const message = ERROR_MESSAGES[code] ?? ERROR_MESSAGES.internal_error;
    setStatus(message, "error");
    // The live status is the single visible error announcement.  Keep the
    // alert node empty so the same safe message is not rendered twice.
    error.textContent = "";
    error.hidden = true;
    status.focus?.({ preventScroll: true });
    submitting = false;
    terminalReady = true;
    renderInput();
  }

  function endStatusObservation(owner) {
    if (!isCurrentLifecycle(owner)) return;
    invalidateLifecycle(owner);
    setStatus(
      "클라이언트의 상태 확인이 종료되었습니다. 서버 작업은 계속될 수 있습니다.",
      "observation-ended",
    );
    error.hidden = true;
    submitting = false;
    renderInput();
  }

  function retryStatus(owner) {
    if (!isCurrentLifecycle(owner)) return;
    if (Date.now() >= owner.deadlineAt) {
      endStatusObservation(owner);
      return;
    }
    owner.transientFailures += 1;
    if (owner.transientFailures > STATUS_RETRY_LIMIT) {
      endStatusObservation(owner);
      return;
    }
    setStatus(
      "검색 상태를 다시 확인하고 있습니다. 서버 작업은 계속될 수 있습니다.",
      "reconnecting",
      true,
    );
    error.hidden = true;
    const delay = Math.min(
      STATUS_RETRY_INITIAL_MS * (2 ** (owner.transientFailures - 1)),
      STATUS_RETRY_MAX_MS,
    );
    schedulePoll(owner, delay);
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

  function canonicalLocalTime(value) {
    return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) ? `${value}:00` : value;
  }

  function effectiveSearchStartUtc() {
    if (confirmation === null) return null;
    const anchor = new Date(confirmation.anchorTimeUtc);
    const baseline = new Date(confirmation.baselineTimeUtc ?? confirmation.anchorTimeUtc);
    if (Number.isNaN(anchor.getTime()) || Number.isNaN(baseline.getTime())) return null;
    return new Date(Math.max(anchor.getTime(), baseline.getTime()));
  }

  function searchEndValidation() {
    if (confirmation === null) return { valid: false, reason: "pending" };
    const end = utcFromLocal(endInput.value, confirmation.sourceTimezone);
    const start = effectiveSearchStartUtc();
    if (end === null || start === null) return { valid: false, reason: "invalid" };
    if (end.getTime() <= start.getTime()) return { valid: false, reason: "before_start" };
    const duration = (end.getTime() - start.getTime()) / 1000;
    if (!Number.isInteger(duration) || duration > MAX_SEARCH_DURATION_SECONDS) {
      return { valid: false, reason: "too_long" };
    }
    if (end.getTime() > Date.now()) return { valid: false, reason: "future" };
    return { valid: true, reason: null };
  }

  function validSearchEnd() {
    return searchEndValidation().valid;
  }

  function storage() {
    try {
      return window.sessionStorage ?? null;
    } catch (_caught) {
      return null;
    }
  }

  function readStoredRun() {
    const store = storage();
    if (store === null) return null;
    let parsed;
    try {
      const raw = store.getItem(RUN_STORAGE_KEY);
      parsed = raw === null ? null : JSON.parse(raw);
    } catch (_caught) {
      return null;
    }
    if (!hasExactKeys(parsed, RUN_STORAGE_KEYS)
      || parsed.version !== 1
      || !INVESTIGATION_PATTERN.test(parsed.investigation_id)
      || !RUN_PATTERN.test(parsed.run_id)
      || !UUID_V4_PATTERN.test(parsed.request_id)
      || !LOCAL_TIME_PATTERN.test(parsed.search_end)
      || !Number.isInteger(parsed.duration_seconds)
      || parsed.duration_seconds <= 0
      || parsed.duration_seconds > MAX_SEARCH_DURATION_SECONDS) {
      return null;
    }
    return parsed;
  }

  function rememberStoredRun(record) {
    const store = storage();
    if (store === null) return;
    try {
      store.setItem(RUN_STORAGE_KEY, JSON.stringify(record));
    } catch (_caught) {
      // A storage quota or privacy failure must not break the active run.
    }
  }

  function durationFromSearchEnd(value) {
    if (confirmation === null) return null;
    const end = utcFromLocal(value, confirmation.sourceTimezone);
    const start = effectiveSearchStartUtc();
    if (end === null || start === null) return null;
    const duration = (end.getTime() - start.getTime()) / 1000;
    return Number.isInteger(duration) && duration > 0 && duration <= MAX_SEARCH_DURATION_SECONDS
      ? duration
      : null;
  }

  function phase8Valid(statusValue, reason) {
    if (statusValue === null && reason === null) return true;
    if (!PHASE8_STATUSES.has(statusValue)) return false;
    return PHASE8_REASONS_BY_STATUS[statusValue]?.has(reason) === true;
  }

  function renderInput() {
    const validation = searchEndValidation();
    const pending = confirmation === null || submitting || (activeRun !== null && !terminalReady);
    startAction.disabled = pending || !validation.valid;
    startAction.dataset.state = pending ? "pending" : validation.valid ? "ready" : "invalid";
    if (confirmation !== null && !submitting && (activeRun === null || terminalReady)) {
      if (validation.valid && ["ready", "invalid"].includes(status.dataset.state)) {
        setStatus("검색 종료 시각을 검토한 뒤 검색을 시작하세요.", "ready");
      } else if (!validation.valid) {
        const messages = {
          invalid: "검색 종료 시각 형식을 확인하세요.",
          before_start: "검색 종료 시각은 확인된 조사 시각과 선택한 기준 프레임 중 늦은 시각 이후여야 합니다.",
          too_long: "검색 범위는 최대 2시간까지 입력할 수 있습니다.",
          future: "검색 종료 시각은 현재보다 미래일 수 없습니다.",
        };
        setStatus(messages[validation.reason] ?? "검색 종료 시각을 확인하세요.", "invalid");
      }
    }
  }

  function setQuickRangePressed(seconds) {
    if (quickRanges === null) return;
    Array.from(quickRanges.children ?? []).forEach((button) => {
      const selected = Number(button.dataset.searchDurationSeconds) === seconds;
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function setSearchEndForDuration(seconds) {
    if (confirmation === null || !Number.isInteger(seconds) || seconds <= 0) return;
    const start = effectiveSearchStartUtc();
    if (start === null) return;
    const target = Math.min(start.getTime() + seconds * 1_000, Date.now());
    endInput.value = localFromUtc(new Date(target).toISOString(), confirmation.sourceTimezone);
    setQuickRangePressed(seconds);
    renderInput();
  }

  function showConfirmation(value) {
    confirmation = value;
    terminalReady = false;
    panel.hidden = false;
    confirmedTime.textContent = localFromUtc(value.anchorTimeUtc, value.sourceTimezone);
    timezone.textContent = value.sourceTimezone;
    let locationRunId = null;
    try {
      locationRunId = new URL(window.location.href).searchParams.get("run_id");
    } catch (_caught) {
      // The current input remains the safe fallback when URL access is unavailable.
    }
    const stored = readStoredRun();
    const restoresStoredEnd = stored !== null
      && stored.investigation_id === value.investigationId
      && durationFromSearchEnd(stored.search_end) === stored.duration_seconds
      && (locationRunId === null || locationRunId === stored.run_id);
    if (restoresStoredEnd) {
      endInput.value = stored.search_end;
      setQuickRangePressed(stored.duration_seconds);
    } else if (!LOCAL_TIME_PATTERN.test(endInput.value)) {
      const suggested = new Date(Math.min(
        effectiveSearchStartUtc().getTime() + DEFAULT_SEARCH_DURATION_SECONDS * 1_000,
        Date.now(),
      ));
      endInput.value = localFromUtc(suggested.toISOString(), value.sourceTimezone);
      setQuickRangePressed(DEFAULT_SEARCH_DURATION_SECONDS);
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
      baselineTimeUtc: value.requested_time_utc,
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

  function rememberRun(receipt, expected) {
    const durationSeconds = durationFromSearchEnd(expected.searchEnd);
    activeRun = {
      investigationId: receipt.investigation_id,
      runId: receipt.run_id,
      requestId: expected.requestId,
      searchEnd: expected.searchEnd,
      durationSeconds,
    };
    if (durationSeconds !== null) {
      rememberStoredRun({
        version: 1,
        investigation_id: receipt.investigation_id,
        run_id: receipt.run_id,
        request_id: expected.requestId,
        search_end: expected.searchEnd,
        duration_seconds: durationSeconds,
      });
    }
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
    if (submitting || (activeRun !== null && !terminalReady) || !validSearchEnd()) return;
    const requestId = window.crypto?.randomUUID?.();
    if (typeof requestId !== "string" || !UUID_V4_PATTERN.test(requestId)) {
      fail("recording_search_unavailable");
      return;
    }
    const expected = {
      requestId,
      investigationId: confirmation.investigationId,
      // Native datetime-local controls may omit seconds.  Keep the UI value
      // intact, but send the transport's canonical whole-second form.
      searchEnd: canonicalLocalTime(endInput.value),
    };
    const owner = createLifecycle();
    const requestController = requestControllerFor(owner, "start");
    if (!isCurrentLifecycle(owner) || requestController === null) return;
    submitting = true;
    terminalReady = false;
    result.hidden = true;
    resultReason.textContent = "";
    renderInput();
    setStatus("검색 요청을 접수하는 중입니다…", "loading", true);
    error.hidden = true;
    try {
      const requestOptions = {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          investigation_id: expected.investigationId,
          search_end: expected.searchEnd,
          request_id: expected.requestId,
        }),
      };
      requestOptions.signal = requestController.signal;
      const response = await fetch("/api/v1/recording-searches", requestOptions);
      if (!isCurrentLifecycle(owner)) return;
      const payload = await response.json().catch(() => null);
      if (!isCurrentLifecycle(owner)) return;
      completeRequest(owner, requestController);
      submitting = false;
      if (response.status !== 202) {
        fail(payload?.error?.code, owner);
        return;
      }
      if (!validStart(payload, expected)) {
        fail("internal_error", owner);
        return;
      }
      rememberRun(payload, expected);
      owner.runId = payload.run_id;
      setStatus("검색 중입니다.", "accepted", true);
      pollCount = 0;
      schedulePoll(owner, 0);
    } catch (_caught) {
      if (!isCurrentLifecycle(owner)) return;
      completeRequest(owner, requestController);
      submitting = false;
      fail("recording_search_unavailable", owner);
    }
  }

  function validStatus(payload) {
    const details = payload?.terminal_details;
    const validDetails = details === null || (
      hasExactKeys(details, TERMINAL_DETAIL_KEYS)
      && (details.last_present_time_utc === null || validUtc(details.last_present_time_utc))
      && (details.first_absent_time_utc === null || validUtc(details.first_absent_time_utc))
      && validUtc(details.observed_start_time_utc)
      && validUtc(details.observed_end_time_utc)
      && typeof details.coverage_complete === "boolean"
      && ["Asia/Seoul", "UTC"].includes(details.source_timezone)
    );
    const terminalNeedsDetails = ["FOUND", "NOT_FOUND", "INCONCLUSIVE"].includes(payload?.status);
    return hasExactKeys(payload, STATUS_KEYS)
      && payload.investigation_id === activeRun?.investigationId
      && payload.run_id === activeRun?.runId
      && Number.isInteger(payload.schema_version) && payload.schema_version >= 0
      && typeof payload.status === "string"
      && (payload.reason_code === null || typeof payload.reason_code === "string")
      && (payload.terminal_result_id === null || typeof payload.terminal_result_id === "string")
      && phase8Valid(payload.phase8_status, payload.phase8_reason)
      && validDetails
      && (!terminalNeedsDetails || details !== null);
  }

  function terminalText(kind) {
    return {
      FOUND: "요청한 검색 범위에서 대상이 사라진 구간을 찾았습니다.",
      NOT_FOUND: "검색 종료 시점에도 대상이 존재합니다.",
      INCONCLUSIVE: "자동 판정이 불확실합니다. 기준 시점과 종료 시점을 직접 비교하세요.",
      FAILED: "검색이 안전하게 실패했습니다.",
      INTERRUPTED: "검색이 중단되었습니다.",
      CORRUPT: "검색 실행 기록을 안전하게 읽을 수 없습니다.",
    }[kind] ?? "검색 상태를 확인할 수 없습니다.";
  }

  function finish(payload, owner) {
    if (!isCurrentLifecycle(owner)) return;
    invalidateLifecycle(owner);
    terminalReady = true;
    setStatus("녹화 기록 검색이 종료되었습니다.", "complete");
    resultKind.textContent = terminalText(payload.status);
    const mappedReason = Object.prototype.hasOwnProperty.call(
      RESULT_REASON_MESSAGES,
      payload.reason_code,
    ) ? RESULT_REASON_MESSAGES[payload.reason_code] : null;
    resultReason.textContent = mappedReason
      ?? (payload.reason_code === null ? "서버가 추가 사유를 제공하지 않았습니다." : "서버가 제공한 안전한 결과 사유가 있습니다.");
    const details = payload.terminal_details;
    if ((activeRun?.searchEnd === null || activeRun?.searchEnd === undefined)
      && details !== null && validUtc(details.observed_end_time_utc)) {
      const restoredEnd = localFromUtc(details.observed_end_time_utc, details.source_timezone);
      if (LOCAL_TIME_PATTERN.test(restoredEnd)) {
        const restoredDuration = durationFromSearchEnd(restoredEnd);
        if (restoredDuration !== null) {
          activeRun.searchEnd = restoredEnd;
          activeRun.durationSeconds = restoredDuration;
          endInput.value = restoredEnd;
          setQuickRangePressed(restoredDuration);
        }
      }
    }
    resultTiming.hidden = details === null;
    if (details !== null) {
      const zone = details.source_timezone;
      const observedStart = localFromUtc(details.observed_start_time_utc, zone);
      const observedEnd = localFromUtc(details.observed_end_time_utc, zone);
      observedRange.textContent = `${observedStart} ~ ${observedEnd} (${zone})${details.coverage_complete ? "" : " — 요청 종료 전 녹화 종료"}`;
      if (payload.status === "FOUND" && details.last_present_time_utc !== null && details.first_absent_time_utc !== null) {
        const present = localFromUtc(details.last_present_time_utc, zone);
        const absent = localFromUtc(details.first_absent_time_utc, zone);
        lastPresent.textContent = `${present} (${zone})`;
        firstAbsent.textContent = `${absent} (${zone})`;
        disappearanceInterval.textContent = `${present} ~ ${absent} (${zone})`;
      } else {
        lastPresent.textContent = "해당 없음";
        firstAbsent.textContent = "해당 없음";
        disappearanceInterval.textContent = "확정되지 않음";
      }
    }
    result.hidden = false;
    void loadEvidence(payload);
    renderInput();
    result.focus?.({ preventScroll: true });
  }

  function evidenceUrl(entry) {
    if (entry === null || typeof entry !== "object" || typeof entry.digest !== "string") return null;
    if (!/^[0-9a-f]{64}$/.test(entry.digest) || activeRun === null) return null;
    return `/api/v1/recording-searches/${encodeURIComponent(activeRun.investigationId)}/${encodeURIComponent(activeRun.runId)}/evidence/${entry.digest}`;
  }

  function validEvidence(payload) {
    const manifestKeys = [
      "version", "investigation_id", "run_id", "plan_id", "authority_identity",
      "roi_identity", "roi", "source_width", "source_height", "terminal_status",
      "terminal_reason", "last_present_observation_id", "first_absent_observation_id",
      "review_clip", "entries",
    ];
    if (!hasExactKeys(payload, manifestKeys)
      || payload.version !== "phase7e-successor-evidence-v1"
      || payload.investigation_id !== activeRun?.investigationId
      || payload.run_id !== activeRun?.runId
      || typeof payload.plan_id !== "string" || payload.plan_id === ""
      || typeof payload.authority_identity !== "string" || payload.authority_identity === ""
      || typeof payload.roi_identity !== "string" || payload.roi_identity === ""
      || !Number.isInteger(payload.source_width) || payload.source_width <= 0
      || !Number.isInteger(payload.source_height) || payload.source_height <= 0
      || !hasExactKeys(payload.roi, ["x", "y", "width", "height", "coordinate_space", "provenance"])
      || payload.roi.coordinate_space !== "source_pixels"
      || !Number.isInteger(payload.roi.x) || !Number.isInteger(payload.roi.y)
      || !Number.isInteger(payload.roi.width) || !Number.isInteger(payload.roi.height)
      || payload.roi.x < 0 || payload.roi.y < 0 || payload.roi.width <= 0 || payload.roi.height <= 0
      || payload.roi.x + payload.roi.width > payload.source_width
      || payload.roi.y + payload.roi.height > payload.source_height
      || !["FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED", "INTERRUPTED"].includes(payload.terminal_status)
      || (payload.terminal_reason !== null && typeof payload.terminal_reason !== "string")
      || (payload.last_present_observation_id !== null && typeof payload.last_present_observation_id !== "string")
      || (payload.first_absent_observation_id !== null && typeof payload.first_absent_observation_id !== "string")
      || !hasExactKeys(payload.review_clip, ["status", "reason"])
      || !["UNAVAILABLE", "READY"].includes(payload.review_clip.status)
      || typeof payload.review_clip.reason !== "string"
      || !Array.isArray(payload.entries) || payload.entries.length === 0 || payload.entries.length > 64) {
      return false;
    }
    const entryKeys = [
      "role", "plan_id", "observation_id", "target_id", "acquisition_id",
      "assigned_segment_id", "requested_time_utc", "frame_utc", "frame_pts_seconds",
      "frame_ordinal", "frame_offset_seconds", "digest", "width", "height",
      "authority_identity", "reference_frame_resource_id", "roi_identity",
      "classifier_policy_identity", "acquisition_status", "state", "reason_code",
      "comparison", "classifier_stage", "classifier_elapsed_ms", "path", "roi_path",
      "roi_digest",
    ];
    return payload.entries.every((entry) => {
      if (!hasExactKeys(entry, entryKeys)
        || !["baseline", "baseline_link", "anchor", "observation"].includes(entry.role)
        || entry.plan_id !== payload.plan_id
        || entry.authority_identity !== payload.authority_identity
        || entry.roi_identity !== payload.roi_identity
        || !["FRAME_AVAILABLE", "UNAVAILABLE_GAP", "RECORDING_UNAVAILABLE", "REPLAY_TIMEOUT", "REPLAY_FAILED", "DECODE_TIMEOUT", "DECODE_UNAVAILABLE"].includes(entry.acquisition_status)
        || !["PRESENT", "ABSENT", "INDETERMINATE", "UNAVAILABLE_GAP", "RECORDING_UNAVAILABLE", "REPLAY_TIMEOUT", "REPLAY_FAILED", "DECODE_TIMEOUT", "DECODE_UNAVAILABLE", "CLASSIFIER_TIMEOUT", "CLASSIFIER_FAILED"].includes(entry.state)) return false;
      const validDigest = (value) => value === null || (typeof value === "string" && /^[0-9a-f]{64}$/.test(value));
      if (!validDigest(entry.digest) || !validDigest(entry.roi_digest)) return false;
      if ((entry.digest === null) !== (entry.path === null)
        || (entry.digest !== null && entry.path !== `frames/${entry.digest}.jpg`)
        || (entry.roi_digest === null) !== (entry.roi_path === null)
        || (entry.roi_digest !== null && entry.roi_path !== `frames/${entry.roi_digest}.jpg`)) return false;
      if (entry.acquisition_status === "FRAME_AVAILABLE"
        && (entry.width !== payload.source_width || entry.height !== payload.source_height)) return false;
      if (entry.acquisition_status !== "FRAME_AVAILABLE" && (entry.width !== null || entry.height !== null)) return false;
      if (entry.comparison !== null && (typeof entry.comparison !== "object" || Array.isArray(entry.comparison))) return false;
      return true;
    });
  }

  function setEvidenceImage(image, src) {
    if (image == null) return;
    image.src = src;
    image.hidden = false;
  }

  function setEvidenceHighlight(highlight, roi, sourceWidth, sourceHeight) {
    if (highlight == null || roi === null || typeof roi !== "object") return;
    if (!Number.isInteger(sourceWidth) || !Number.isInteger(sourceHeight)
      || !Number.isInteger(roi.x) || !Number.isInteger(roi.y)
      || !Number.isInteger(roi.width) || !Number.isInteger(roi.height)
      || roi.x < 0 || roi.y < 0 || roi.width <= 0 || roi.height <= 0
      || roi.x + roi.width > sourceWidth || roi.y + roi.height > sourceHeight) return;
    highlight.style.left = `${(roi.x / sourceWidth) * 100}%`;
    highlight.style.top = `${(roi.y / sourceHeight) * 100}%`;
    highlight.style.width = `${(roi.width / sourceWidth) * 100}%`;
    highlight.style.height = `${(roi.height / sourceHeight) * 100}%`;
    highlight.hidden = false;
  }

  function renderEvidence(payload, terminalPayload) {
    if (evidencePanel == null || !validEvidence(payload)) return;
    const entries = payload.entries.filter((item) => item && typeof item === "object");
    const baseline = entries.find((item) => item.role === "baseline");
    const observed = entries.filter((item) => item.role !== "baseline"
      && typeof item.frame_utc === "string" && typeof item.digest === "string")
      .sort((left, right) => String(left.frame_utc).localeCompare(String(right.frame_utc)));
    const coarseObserved = observed.filter((item) => item.role === "observation");
    let ending = coarseObserved.at(-1) ?? null;
    if (ending === null && terminalPayload?.status === "FOUND") {
      const terminalAbsent = entries.find((item) => item.observation_id === payload.first_absent_observation_id
        && typeof item.digest === "string");
      ending = terminalAbsent ?? null;
    }
    const roi = payload.roi;
    const sourceWidth = payload.source_width;
    const sourceHeight = payload.source_height;
    if (baseline === undefined || ending === null) {
      evidenceStatus.textContent = "이 실행의 시각 증거를 사용할 수 없습니다.";
      evidencePanel.hidden = false;
      return;
    }
    const baselineSrc = evidenceUrl(baseline);
    const endingSrc = evidenceUrl(ending);
    const baselineRoiSrc = typeof baseline.roi_digest === "string"
      ? evidenceUrl({ digest: baseline.roi_digest }) : baselineSrc;
    const endingRoiSrc = typeof ending.roi_digest === "string"
      ? evidenceUrl({ digest: ending.roi_digest }) : endingSrc;
    if (baselineSrc === null || endingSrc === null || baselineRoiSrc === null || endingRoiSrc === null) {
      evidenceStatus.textContent = "시각 증거를 안전하게 확인할 수 없습니다.";
      evidencePanel.hidden = false;
      return;
    }
    setEvidenceImage(baselineImage, baselineSrc);
    setEvidenceImage(baselineRoi, baselineRoiSrc);
    setEvidenceImage(endImage, endingSrc);
    setEvidenceImage(endRoi, endingRoiSrc);
    baselineTime.textContent = baseline.frame_utc ?? baseline.requested_time_utc;
    endTime.textContent = ending.frame_utc ?? ending.requested_time_utc;
    setEvidenceHighlight(baselineHighlight, roi, sourceWidth, sourceHeight);
    setEvidenceHighlight(endHighlight, roi, sourceWidth, sourceHeight);
    if (foundEvidence != null) foundEvidence.hidden = true;
    if (terminalPayload?.status === "FOUND" && foundEvidence != null) {
      const present = entries.find((item) => item.observation_id === payload.last_present_observation_id
        && typeof item.digest === "string");
      const absent = entries.find((item) => item.observation_id === payload.first_absent_observation_id
        && typeof item.digest === "string");
      const presentSrc = evidenceUrl(present);
      const absentSrc = evidenceUrl(absent);
      if (present !== undefined && absent !== undefined && presentSrc !== null && absentSrc !== null) {
        const presentRoiSrc = typeof present.roi_digest === "string"
          ? evidenceUrl({ digest: present.roi_digest }) : presentSrc;
        const absentRoiSrc = typeof absent.roi_digest === "string"
          ? evidenceUrl({ digest: absent.roi_digest }) : absentSrc;
        if (presentRoiSrc !== null && absentRoiSrc !== null) {
          setEvidenceImage(lastPresentImage, presentSrc);
          setEvidenceImage(lastPresentRoi, presentRoiSrc);
          setEvidenceImage(firstAbsentImage, absentSrc);
          setEvidenceImage(firstAbsentRoi, absentRoiSrc);
          if (lastPresentTime != null) lastPresentTime.textContent = present.frame_utc ?? present.requested_time_utc;
          if (firstAbsentTime != null) firstAbsentTime.textContent = absent.frame_utc ?? absent.requested_time_utc;
          setEvidenceHighlight(lastPresentHighlight, roi, sourceWidth, sourceHeight);
          setEvidenceHighlight(firstAbsentHighlight, roi, sourceWidth, sourceHeight);
          foundEvidence.hidden = false;
        }
      }
    }
    evidenceStatus.textContent = "기준 프레임과 실제 관측 프레임을 비교할 수 있습니다.";
    const comparison = ending.comparison;
    if (comparison !== null && typeof comparison === "object" && evidenceMetrics !== null) {
      evidenceMetrics.replaceChildren();
      for (const [label, key] of [
        ["Baseline mask pixels", "baseline_mask_pixel_count"],
        ["Probe mask pixels", "probe_mask_pixel_count"],
        ["Mask IoU", "mask_iou"],
        ["ROI NCC", "roi_luma_ncc"],
        ["Baseline mask coverage", "baseline_mask_coverage"],
        ["Probe mask coverage", "probe_mask_coverage"],
        ["Effective comparison area", "effective_comparison_area"],
      ]) {
        if (typeof comparison[key] !== "number") continue;
        const row = document.createElement("div");
        const term = document.createElement("dt");
        const value = document.createElement("dd");
        term.textContent = label;
        value.textContent = Number(comparison[key]).toFixed(6);
        row.append(term, value);
        evidenceMetrics.append(row);
      }
      evidenceMetrics.hidden = evidenceMetrics.children.length === 0;
    }
    if (coarseEvidence !== null && coarseList !== null) {
      coarseList.replaceChildren();
      for (const entry of observed) {
        const item = document.createElement("p");
        item.textContent = `${entry.frame_utc ?? entry.requested_time_utc} — ${entry.state ?? "UNAVAILABLE"}`;
        coarseList.append(item);
      }
      coarseEvidence.hidden = observed.length <= 1;
    }
    if (reviewClipStatus != null) {
      const clip = payload.review_clip;
      if (terminalPayload?.status === "FOUND") {
        reviewClipStatus.textContent = clip?.status === "READY"
          ? "검토 클립을 사용할 수 있습니다."
          : "검토 클립은 현재 사용할 수 없습니다 (Phase 8에서 제공).";
        reviewClipStatus.hidden = false;
      } else {
        reviewClipStatus.hidden = true;
      }
    }
    evidencePanel.hidden = false;
  }

  async function loadEvidence(payload) {
    if (evidencePanel == null || activeRun === null || !TERMINAL.has(payload?.status)) return;
    evidencePanel.hidden = false;
    evidenceStatus.textContent = "증거를 불러오는 중입니다…";
    try {
      const response = await fetch(`/api/v1/recording-searches/${encodeURIComponent(activeRun.investigationId)}/${encodeURIComponent(activeRun.runId)}/evidence`);
      const body = await response.json().catch(() => null);
      if (response.status === 404) {
        evidenceStatus.textContent = "이전 실행에는 시각 증거가 보존되지 않았습니다.";
        return;
      }
      if (!response.ok || !validEvidence(body)) {
        evidenceStatus.textContent = "시각 증거를 안전하게 확인할 수 없습니다.";
        return;
      }
      renderEvidence(body, payload);
    } catch (_caught) {
      evidenceStatus.textContent = "시각 증거를 불러오지 못했습니다.";
    }
  }

  for (const image of [
    baselineImage,
    baselineRoi,
    endImage,
    endRoi,
    lastPresentImage,
    lastPresentRoi,
    firstAbsentImage,
    firstAbsentRoi,
  ]) {
    if (image == null) continue;
    const open = () => {
      if (typeof image.src !== "string" || image.src === "" || typeof window.open !== "function") return;
      window.open(image.src, "_blank", "noopener,noreferrer");
    };
    image.addEventListener("click", open);
    image.addEventListener("error", () => {
      image.hidden = true;
      if (evidenceStatus != null) evidenceStatus.textContent = "시각 증거 이미지를 안전하게 불러오지 못했습니다.";
    });
    image.addEventListener("keydown", (event) => {
      if (event?.key !== "Enter" && event?.key !== " ") return;
      event.preventDefault?.();
      open();
    });
  }

  async function poll(owner) {
    if (!isCurrentLifecycle(owner) || activeRun === null || owner.runId !== activeRun.runId) return;
    if (Date.now() >= owner.deadlineAt) {
      endStatusObservation(owner);
      return;
    }
    pollCount += 1;
    if (pollCount > 1350) {
      endStatusObservation(owner);
      return;
    }
    const run = activeRun;
    const requestController = requestControllerFor(owner, "status");
    if (!isCurrentLifecycle(owner) || requestController === null) {
      return;
    }
    try {
      const requestOptions = { signal: requestController.signal };
      const response = await fetch(
        `/api/v1/recording-searches/${encodeURIComponent(run.investigationId)}/${encodeURIComponent(run.runId)}`,
        requestOptions,
      );
      if (!isCurrentLifecycle(owner) || activeRun !== run) return;
      const payload = await response.json().catch(() => null);
      if (!isCurrentLifecycle(owner) || activeRun !== run) return;
      if (!completeRequest(owner, requestController)) return;
      if (response.status >= 500 && response.status <= 599) {
        retryStatus(owner);
        return;
      }
      if (!response.ok) {
        fail(response.status === 404 ? "search_run_not_found" : payload?.error?.code, owner);
        return;
      }
      if (!validStatus(payload)) {
        retryStatus(owner);
        return;
      }
      if (TERMINAL.has(payload.status)) {
        finish(payload, owner);
        return;
      }
      if (!["ACCEPTED", "RUNNING"].includes(payload.status)) {
        fail("internal_error", owner);
        return;
      }
      owner.transientFailures = 0;
      setStatus(
        payload.status === "ACCEPTED" ? "검색 요청이 대기 중입니다." : "녹화 기록을 검색하는 중입니다…",
        "loading",
        true,
      );
      schedulePoll(owner, 2000);
    } catch (_caught) {
      if (!isCurrentLifecycle(owner)) return;
      if (!completeRequest(owner, requestController)) return;
      retryStatus(owner);
    }
  }

  function schedulePoll(owner, delay) {
    if (!isCurrentLifecycle(owner)) return;
    if (owner.pollTimer !== null) window.clearTimeout(owner.pollTimer);
    const remaining = owner.deadlineAt - Date.now();
    if (remaining <= 0) {
      endStatusObservation(owner);
      return;
    }
    owner.pollTimer = window.setTimeout(() => {
      owner.pollTimer = null;
      if (isCurrentLifecycle(owner)) void poll(owner);
    }, Math.min(delay, remaining));
  }

  function resumeFromLocation() {
    let runId = null;
    try {
      const location = new URL(window.location.href);
      runId = location.searchParams.get("run_id");
    } catch (_caught) {
      return;
    }
    if (runId !== null && RUN_PATTERN.test(runId) && confirmation !== null) {
      const stored = readStoredRun();
      const storedForRun = stored !== null
        && stored.investigation_id === confirmation.investigationId
        && stored.run_id === runId
        && durationFromSearchEnd(stored.search_end) === stored.duration_seconds;
      const owner = createLifecycle();
      terminalReady = false;
      activeRun = {
        investigationId: confirmation.investigationId,
        runId,
        requestId: storedForRun ? stored.request_id : null,
        searchEnd: storedForRun ? stored.search_end : null,
        durationSeconds: storedForRun ? stored.duration_seconds : null,
      };
      if (storedForRun) {
        endInput.value = stored.search_end;
        setQuickRangePressed(stored.duration_seconds);
      }
      owner.runId = runId;
      setStatus("이전 검색 상태를 다시 확인하는 중입니다…", "loading", true);
      renderInput();
      schedulePoll(owner, 0);
    }
  }

  function receiveConfirmation(event) {
    const detail = event?.detail;
    if (detail?.schemaVersion !== 3 || !INVESTIGATION_PATTERN.test(detail.investigationId)
      || !validUtc(detail.anchorTimeUtc) || !["Asia/Seoul", "UTC"].includes(detail.sourceTimezone)) {
      return;
    }
    const baselineTimeUtc = detail.baselineTimeUtc ?? detail.anchorTimeUtc;
    if (!validUtc(baselineTimeUtc)) return;
    showConfirmation({ ...detail, baselineTimeUtc });
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
  Array.from(quickRanges?.children ?? []).forEach((button) => {
    button.addEventListener("click", () => {
      setSearchEndForDuration(Number(button.dataset.searchDurationSeconds));
    });
  });
  startAction.addEventListener("click", start);
  window.addEventListener("vigi:investigation-confirmed", receiveConfirmation);
  window.addEventListener("pagehide", () => {
    controller?.abort();
    invalidateLifecycle(lifecycle);
  });
  window.vigiVisionRecordingSearch = Object.freeze({
    getState: () => ({
      investigationId: confirmation?.investigationId ?? null,
      runId: activeRun?.runId ?? null,
      submitting,
      polling: lifecycle !== null,
    }),
    getReasonCodes: () => Object.keys(RESULT_REASON_MESSAGES),
  });
  loadFromLocation();
}());
