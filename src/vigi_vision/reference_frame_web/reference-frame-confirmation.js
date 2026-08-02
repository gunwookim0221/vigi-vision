(function () {
  const panel = document.querySelector("#confirmation-panel");
  const review = document.querySelector("#confirmation-review");
  const status = document.querySelector("#confirmation-status");
  const error = document.querySelector("#confirmation-error");
  const action = document.querySelector("#confirmation-action");
  const result = document.querySelector("#confirmation-result");
  const resultId = document.querySelector("#confirmation-id");
  const resultTime = document.querySelector("#confirmation-confirmed-at");
  const resultArtifact = document.querySelector("#confirmation-artifact");
  const channelInput = document.querySelector("#channel-id");
  const timeInput = document.querySelector("#reference-time");
  const timezoneInput = document.querySelector("#source-timezone");
  const form = window.vigiVisionReferenceFrameForm;
  const selection = window.vigiVisionReferenceFrameSelection;
  const roi = window.vigiVisionReferenceFrameRoi;
  const ALLOWED_PROVENANCE = new Set(["manual", "assisted", "assisted_then_adjusted"]);
  const ALLOWED_TIMING_PRECISION = new Set(["measured_clip_relative", "estimated", "unavailable", "indeterminate"]);
  const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$/;
  const INVESTIGATION_ID_PATTERN = /^object-disappearance-ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z$/;
  const RESPONSE_KEYS = Object.freeze([
    "investigation_id",
    "outcome",
    "status",
    "schema_version",
    "confirmed_at_utc",
    "artifact_directory_relative",
    "confirmation",
  ]);
  const CONFIRMATION_KEYS = Object.freeze([
    "channel_id",
    "candidate_offset_seconds",
    "reference_frame_resource_id",
    "requested_time_utc",
    "timing",
    "source_width",
    "source_height",
    "roi",
  ]);
  const TIMING_KEYS = Object.freeze(["estimated_source_time_utc", "timing_precision_status"]);
  const FRAME_TIMING_KEYS = Object.freeze([
    "precision_status",
    "decoded_clip_relative_pts_seconds",
    "estimated_source_time_utc",
    "offset_from_requested_seconds",
  ]);
  const ROI_KEYS = Object.freeze(["x", "y", "width", "height", "coordinate_space", "provenance"]);
  const ERROR_MESSAGES = Object.freeze({
    invalid_confirmation: "확인할 수 없는 선택입니다. 후보와 ROI를 다시 검토하세요.",
    invalid_request: "확인 요청 형식을 확인할 수 없습니다.",
    resource_not_found: "선택한 기준 프레임을 더 이상 확인할 수 없습니다.",
    investigation_not_found: "아직 확인된 조사가 없습니다.",
    stale_selection: "선택한 후보가 최신 상태가 아닙니다. 후보를 다시 검토하세요.",
    confirmation_conflict: "이미 다른 선택으로 확인된 조사입니다.",
    confirmation_in_progress: "다른 확인 작업이 진행 중입니다. 잠시 후 다시 시도하세요.",
    artifact_failure: "확인 결과를 안전하게 저장하지 못했습니다.",
    confirmation_unavailable: "조사 확인 기능을 사용할 수 없습니다.",
  });
  let phase = "unavailable";
  let currentKey = null;
  let currentResult = null;
  let operationVersion = 0;
  let locked = false;
  let controller = null;
  let activeSubmission = null;

  function safeCode(payload) {
    const code = payload?.error?.code;
    return typeof code === "string" && Object.prototype.hasOwnProperty.call(ERROR_MESSAGES, code)
      ? code
      : "confirmation_unavailable";
  }

  function safeJson(response) {
    return response.json().catch(() => null);
  }

  function setStatus(message, state) {
    status.textContent = message;
    status.dataset.state = state;
    status.setAttribute("aria-busy", String(state === "loading"));
  }

  function validPositive(value) {
    return Number.isInteger(value) && value > 0;
  }

  function validResourceId(value) {
    return typeof value === "string" && RESOURCE_ID_PATTERN.test(value);
  }

  function validUtcTimestamp(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)$/.test(value)) {
      return false;
    }
    const parsed = new Date(value);
    return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 19) === value.slice(0, 19);
  }

  function sameUtcTimestamp(left, right) {
    return validUtcTimestamp(left) && validUtcTimestamp(right) && new Date(left).getTime() === new Date(right).getTime();
  }

  function sameNullableUtcTimestamp(left, right) {
    return left === null && right === null
      || typeof left === "string" && typeof right === "string" && sameUtcTimestamp(left, right);
  }

  function hasExactKeys(value, keys) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
      && Object.keys(value).length === keys.length
      && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
  }

  function validRoiShape(value, width, height) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
      && [value.x, value.y, value.width, value.height].every(Number.isInteger)
      && value.x >= 0 && value.y >= 0 && value.width >= 4 && value.height >= 4
      && value.x + value.width <= width && value.y + value.height <= height;
  }

  function validResponseRoi(value, width, height) {
    return hasExactKeys(value, ROI_KEYS)
      && value.coordinate_space === "source_pixels"
      && ALLOWED_PROVENANCE.has(value.provenance)
      && validRoiShape(value, width, height);
  }

  function validFrameTiming(value) {
    const decodedPts = value?.decoded_clip_relative_pts_seconds;
    return hasExactKeys(value, FRAME_TIMING_KEYS)
      && value.precision_status === "measured_clip_relative"
      && (decodedPts === null || typeof decodedPts === "number" && Number.isFinite(decodedPts))
      && value.estimated_source_time_utc === null
      && value.offset_from_requested_seconds === null;
  }

  function utcFromLocal(local, timezone) {
    if (typeof local !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(local)) {
      return null;
    }
    if (timezone === "UTC") {
      const parsed = new Date(`${local}Z`);
      return Number.isNaN(parsed.getTime()) ? null : parsed;
    }
    if (timezone === "Asia/Seoul") {
      const parsed = new Date(`${local}+09:00`);
      return Number.isNaN(parsed.getTime()) ? null : parsed;
    }
    return null;
  }

  function investigationId(channelId, request) {
    const anchor = utcFromLocal(request.reference_time, request.source_timezone);
    return anchor === null || !validPositive(channelId)
      ? null
      : `object-disappearance-ch${channelId}-${anchor.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z")}`;
  }

  function validCandidateFrame(candidate, frame, request, channelId) {
    const anchor = utcFromLocal(request?.reference_time, request?.source_timezone);
    if (anchor === null || !validUtcTimestamp(candidate?.candidate_requested_time_utc)
      || !validUtcTimestamp(frame?.requested_time_utc)) {
      return false;
    }
    const expectedCandidateTime = anchor.getTime() + candidate.offset_seconds * 1000;
    return frame.channel_id === channelId
      && sameUtcTimestamp(frame.requested_time_utc, candidate.candidate_requested_time_utc)
      && new Date(candidate.candidate_requested_time_utc).getTime() === expectedCandidateTime
      && validFrameTiming(frame.timing);
  }

  function assistedPending() {
    const pending = window.vigiVisionReferenceFrameAssistedRoi?.getState?.()?.pending;
    return pending !== null && pending !== undefined;
  }

  function lookup() {
    const candidate = selection?.getSelectedCandidate?.();
    const request = form?.getRequestPayload?.();
    const frame = candidate?.reference_frame;
    const channelId = Number(channelInput.value);
    if (!candidate || !frame || request === null || form?.isReady?.() !== true
      || !validPositive(channelId) || !validResourceId(frame.resource_id)
      || !Number.isInteger(candidate.offset_seconds) || candidate.offset_seconds < -300 || candidate.offset_seconds > 300
      || !validUtcTimestamp(candidate.candidate_requested_time_utc)
      || !validPositive(frame.image?.width) || !validPositive(frame.image?.height)
      || !validCandidateFrame(candidate, frame, request, channelId)
    ) {
      return null;
    }
    const id = investigationId(channelId, request);
    return id === null ? null : { candidate, frame, request, channelId, id };
  }

  function draft() {
    const value = lookup();
    const snapshot = roi?.getPhase6Snapshot?.();
    if (value === null || snapshot === null || assistedPending()
      || snapshot.candidateId !== value.frame.resource_id
      || snapshot.sourceWidth !== value.frame.image.width || snapshot.sourceHeight !== value.frame.image.height
      || snapshot.coordinateSpace !== "source_pixels"
      || !ALLOWED_PROVENANCE.has(snapshot.provenance)
      || !validRoiShape(snapshot.roi, value.frame.image.width, value.frame.image.height)) {
      return null;
    }
    return { ...value, snapshot };
  }

  function keyFor(value) {
    return value === null ? null : JSON.stringify({
      id: value.id,
      resource: value.frame.resource_id,
      offset: value.candidate.offset_seconds,
      width: value.frame.image.width,
      height: value.frame.image.height,
      roi: value.snapshot?.roi ?? null,
      provenance: value.snapshot?.provenance ?? null,
    });
  }

  function confirmationSubmission(value) {
    const snapshot = value.snapshot;
    return Object.freeze({
      investigationId: value.id,
      channelId: value.channelId,
      resourceId: value.frame.resource_id,
      referenceTime: value.request.reference_time,
      sourceTimezone: value.request.source_timezone,
      candidateOffsetSeconds: value.candidate.offset_seconds,
      candidateRequestedTimeUtc: value.candidate.candidate_requested_time_utc,
      frameRequestedTimeUtc: value.frame.requested_time_utc,
      sourceWidth: value.frame.image.width,
      sourceHeight: value.frame.image.height,
      timing: Object.freeze({
        precisionStatus: value.frame.timing.precision_status,
        estimatedSourceTimeUtc: value.frame.timing.estimated_source_time_utc,
      }),
      roi: Object.freeze({
        ...snapshot.roi,
        coordinateSpace: snapshot.coordinateSpace,
        provenance: snapshot.provenance,
      }),
    });
  }

  function responseMatchesSubmission(payload, submitted) {
    const confirmation = payload?.confirmation;
    const returnedRoi = confirmation?.roi;
    return payload?.investigation_id === submitted.investigationId
      && confirmation?.channel_id === submitted.channelId
      && confirmation?.candidate_offset_seconds === submitted.candidateOffsetSeconds
      && confirmation?.reference_frame_resource_id === submitted.resourceId
      && sameUtcTimestamp(confirmation?.requested_time_utc, submitted.candidateRequestedTimeUtc)
      && sameUtcTimestamp(submitted.frameRequestedTimeUtc, submitted.candidateRequestedTimeUtc)
      && confirmation?.source_width === submitted.sourceWidth
      && confirmation?.source_height === submitted.sourceHeight
      && confirmation?.timing?.timing_precision_status === submitted.timing.precisionStatus
      && sameNullableUtcTimestamp(
        confirmation?.timing?.estimated_source_time_utc,
        submitted.timing.estimatedSourceTimeUtc,
      )
      && returnedRoi?.x === submitted.roi.x
      && returnedRoi?.y === submitted.roi.y
      && returnedRoi?.width === submitted.roi.width
      && returnedRoi?.height === submitted.roi.height
      && returnedRoi?.coordinate_space === submitted.roi.coordinateSpace
      && returnedRoi?.provenance === submitted.roi.provenance;
  }

  function safeArtifactPath(value, investigation) {
    return typeof value === "string" && value === `artifacts/investigations/${investigation}`
      ? value
      : null;
  }

  function validResponse(payload, value) {
    const confirmation = payload?.confirmation;
    const returnedFrame = value.frame;
    const returnedRoi = confirmation?.roi;
    return hasExactKeys(payload, RESPONSE_KEYS)
      && typeof payload.investigation_id === "string"
      && INVESTIGATION_ID_PATTERN.test(payload.investigation_id)
      && payload.investigation_id === value.id
      && (payload.outcome === "created" || payload.outcome === "reused")
      && payload.status === "confirmed" && payload.schema_version === 2
      && validUtcTimestamp(payload.confirmed_at_utc)
      && safeArtifactPath(payload.artifact_directory_relative, value.id) !== null
      && hasExactKeys(confirmation, CONFIRMATION_KEYS)
      && confirmation.channel_id === value.channelId
      && confirmation.candidate_offset_seconds === value.candidate.offset_seconds
      && validResourceId(confirmation.reference_frame_resource_id)
      && confirmation.reference_frame_resource_id === value.frame.resource_id
      && validCandidateFrame(value.candidate, returnedFrame, value.request, value.channelId)
      && validPositive(confirmation.source_width) && confirmation.source_width === value.frame.image.width
      && validPositive(confirmation.source_height) && confirmation.source_height === value.frame.image.height
      && sameUtcTimestamp(confirmation.requested_time_utc, value.candidate.candidate_requested_time_utc)
      && hasExactKeys(confirmation.timing, TIMING_KEYS)
      && (confirmation.timing.estimated_source_time_utc === null || validUtcTimestamp(confirmation.timing.estimated_source_time_utc))
      && ALLOWED_TIMING_PRECISION.has(confirmation.timing.timing_precision_status)
      && confirmation.timing.timing_precision_status === returnedFrame.timing?.precision_status
      && sameNullableUtcTimestamp(
        confirmation.timing.estimated_source_time_utc,
        returnedFrame.timing?.estimated_source_time_utc ?? null,
      )
      && validResponseRoi(returnedRoi, value.frame.image.width, value.frame.image.height);
  }

  function setUiLocked(value) {
    locked = value;
    form?.setReadOnly?.(value);
    selection?.setReadOnly?.(value);
    roi?.setReadOnly?.(value);
    window.vigiVisionReferenceFrameAssistedRoi?.setReadOnly?.(value);
    channelInput.disabled = value || channelInput.disabled;
    timeInput.disabled = value;
    timezoneInput.disabled = value;
  }

  function displayConfirmed(payload) {
    const confirmation = payload.confirmation;
    resultId.textContent = payload.investigation_id;
    resultTime.textContent = new Date(payload.confirmed_at_utc).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });
    resultArtifact.textContent = payload.artifact_directory_relative;
    result.hidden = false;
    result.focus?.({ preventScroll: true });
    review.hidden = true;
    setStatus(`조사가 확인되었습니다. ${confirmation.roi.width} × ${confirmation.roi.height} 픽셀 ROI를 저장했습니다.`, "success");
    error.hidden = true;
    error.textContent = "";
    action.disabled = true;
  }

  function reviewText(value) {
    const snapshot = value.snapshot;
    if (snapshot === undefined) {
      return `조사 ${value.id} · 채널 ${value.channelId} · 후보 ${value.candidate.offset_seconds}초 · ROI를 먼저 그리세요.`;
    }
    const candidateTime = typeof value.candidate.candidate_requested_time_utc === "string"
      ? value.candidate.candidate_requested_time_utc
      : "확인 불가";
    const precision = value.frame.timing?.precision_status === "measured_clip_relative"
      ? "클립 기준 측정"
      : value.frame.timing?.precision_status === "estimated"
        ? "추정"
        : "확인 불가";
    const timingCaveat = value.frame.timing?.estimated_source_time_utc === null
      ? "정확한 원본 시각은 아직 확인되지 않았습니다."
      : "원본 시각은 서버가 제공한 추정값입니다.";
    const roiValue = snapshot.roi;
    return [
      `조사 ${value.id}`,
      `리소스 ${value.frame.resource_id}`,
      `후보 시각 ${candidateTime}`,
      `후보 오프셋 ${value.candidate.offset_seconds}초`,
      `시각 ${precision} (${timingCaveat})`,
      `원본 ${value.frame.image.width} × ${value.frame.image.height}`,
      `ROI x ${roiValue.x}, y ${roiValue.y}, width ${roiValue.width}, height ${roiValue.height}`,
      `출처 ${snapshot.provenance}`,
    ].join(" · ");
  }

  function applyConfirmed(payload, value) {
    currentResult = payload;
    phase = "confirmed";
    setUiLocked(true);
    roi.replaceCommittedRoi(
      {
        source_width: value.frame.image.width,
        source_height: value.frame.image.height,
        x: payload.confirmation.roi.x,
        y: payload.confirmation.roi.y,
        width: payload.confirmation.roi.width,
        height: payload.confirmation.roi.height,
      },
      "서버가 확인한 ROI를 적용했습니다.",
      "success",
      payload.confirmation.roi.provenance,
    );
    displayConfirmed(payload);
  }

  function safeFailure(code, message = ERROR_MESSAGES[code] ?? ERROR_MESSAGES.confirmation_unavailable) {
    phase = code === "confirmation_conflict" ? "conflict" : "error";
    setStatus(message, "error");
    error.hidden = false;
    error.textContent = message;
    error.focus?.({ preventScroll: true });
    action.disabled = draft() === null || activeSubmission !== null || assistedPending();
    result.hidden = true;
  }

  async function loadExisting(value, version, conflictRefresh = false) {
    controller?.abort();
    controller = typeof AbortController === "function" ? new AbortController() : null;
    phase = "loading";
    setStatus("기존 확인 상태를 확인하는 중입니다…", "loading");
    render(value);
    try {
      const response = await fetch(`/api/v1/investigation-confirmations/${encodeURIComponent(value.id)}`, { signal: controller?.signal });
      const payload = await safeJson(response);
      if (version !== operationVersion || currentKey !== keyFor(value)) return;
      if (locked) return;
      if (response.status === 404) {
        if (conflictRefresh) {
          safeFailure("confirmation_conflict");
          return;
        }
        phase = "ready";
        setStatus("아직 확인된 조사가 없습니다. 선택을 검토한 후 확인하세요.", "ready");
        render(value);
        return;
      }
      if (!response.ok) {
        safeFailure(conflictRefresh ? "confirmation_conflict" : safeCode(payload));
        return;
      }
      if (!validResponse(payload, value)) {
        safeFailure("confirmation_unavailable");
        return;
      }
      applyConfirmed(payload, value);
    } catch (caught) {
      if (version !== operationVersion || currentKey !== keyFor(value) || caught?.name === "AbortError") return;
      safeFailure(conflictRefresh ? "confirmation_conflict" : "confirmation_unavailable");
    }
  }

  function render(value) {
    panel.hidden = value === null;
    if (value === null || locked) return;
    const currentDraft = draft();
    const displayValue = currentDraft ?? value;
    result.hidden = true;
    review.hidden = false;
    review.textContent = reviewText(displayValue);
    action.disabled = phase !== "ready" || currentDraft === null || activeSubmission !== null || assistedPending();
  }

  async function refresh() {
    const value = lookup();
    const nextKey = keyFor(value);
    const version = ++operationVersion;
    if (locked) return;
    currentResult = null;
    currentKey = nextKey;
    if (value === null) {
      phase = "unavailable";
      panel.hidden = true;
      action.disabled = true;
      result.hidden = true;
      return;
    }
    phase = "loading";
    setStatus("선택을 확인한 후 조사를 확정할 수 있습니다.", "ready");
    render(value);
    void loadExisting(value, version);
  }

  async function confirm(event) {
    event.preventDefault();
    if (locked || phase === "submitting" || activeSubmission !== null) return;
    if (selection?.getSelectedCandidate?.() && lookup() === null) {
      selection.reset("선택한 후보를 사용할 수 없습니다.");
      panel.hidden = false;
      safeFailure("confirmation_unavailable");
      return;
    }
    const value = draft();
    if (value === null) return;
    const version = operationVersion;
    const requestKey = keyFor(value);
    const submitted = confirmationSubmission(value);
    const submission = { version, requestKey, submitted };
    activeSubmission = submission;
    phase = "submitting";
    action.disabled = true;
    setStatus("조사 확인을 저장하는 중입니다…", "loading");
    try {
      const response = await fetch("/api/v1/investigation-confirmations", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          reference_frame_resource_id: submitted.resourceId,
          reference_time: submitted.referenceTime,
          source_timezone: submitted.sourceTimezone,
          candidate_offset_seconds: submitted.candidateOffsetSeconds,
          source_width: submitted.sourceWidth,
          source_height: submitted.sourceHeight,
          roi: {
            x: submitted.roi.x,
            y: submitted.roi.y,
            width: submitted.roi.width,
            height: submitted.roi.height,
            coordinate_space: submitted.roi.coordinateSpace,
            provenance: submitted.roi.provenance,
          },
        }),
      });
      const payload = await safeJson(response);
      if (activeSubmission !== submission) return;
      if (version !== operationVersion || keyFor(draft()) !== requestKey) {
        activeSubmission = null;
        void refresh();
        return;
      }
      activeSubmission = null;
      if (!response.ok) {
        const code = safeCode(payload);
        safeFailure(code);
        if (code === "confirmation_conflict") {
          const conflictVersion = ++operationVersion;
          currentKey = keyFor(value);
          void loadExisting(value, conflictVersion, true);
        }
        return;
      }
      if (!validResponse(payload, value) || !responseMatchesSubmission(payload, submitted)) {
        safeFailure("confirmation_unavailable");
        return;
      }
      applyConfirmed(payload, value);
    } catch (caught) {
      if (activeSubmission !== submission) return;
      if (version !== operationVersion || keyFor(draft()) !== requestKey || caught?.name === "AbortError") {
        activeSubmission = null;
        void refresh();
        return;
      }
      activeSubmission = null;
      safeFailure("confirmation_unavailable");
    }
  }

  action.addEventListener("click", confirm);
  window.vigiVisionReferenceFrameConfirmation = Object.freeze({ getState: () => ({ phase, locked, submitting: activeSubmission !== null, investigationId: currentResult?.investigation_id ?? null }), refresh });
  void refresh();
})();
