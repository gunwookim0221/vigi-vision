const assert = require("node:assert/strict");
const test = require("node:test");

const { createHarness, deferred, textOf } = require("./reference-frame-ui-harness");

const INVESTIGATION_ID = "object-disappearance-v3-ch1-20260720T033418Z";
const REQUEST_ID = "12345678-1234-4234-8234-123456789abc";
const RUN_ID = "search-run-12345678123442348234123456789abc";

function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

function dispatchConfirmed(harness) {
  harness.window.dispatchEvent({
    type: "vigi:investigation-confirmed",
    detail: {
      investigationId: INVESTIGATION_ID,
      anchorTimeUtc: "2026-07-20T03:34:18Z",
      sourceTimezone: "Asia/Seoul",
      schemaVersion: 3,
    },
  });
}

function accepted() {
  return {
    request_id: REQUEST_ID,
    investigation_id: INVESTIGATION_ID,
    run_id: RUN_ID,
    status: "ACCEPTED",
    status_url: `/api/v1/recording-searches/${INVESTIGATION_ID}/${RUN_ID}`,
  };
}

function status(kind, reason = null, schemaVersion = null) {
  return {
    investigation_id: INVESTIGATION_ID,
    run_id: RUN_ID,
    schema_version: schemaVersion ?? (["ACCEPTED", "RUNNING"].includes(kind) ? 0 : 7),
    status: kind,
    reason_code: reason,
    terminal_result_id: ["FOUND", "NOT_FOUND", "INCONCLUSIVE"].includes(kind)
      ? "rr-terminal-result-v1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      : null,
    phase8_status: null,
    phase8_reason: null,
    terminal_details: ["FOUND", "NOT_FOUND", "INCONCLUSIVE"].includes(kind) ? {
      last_present_time_utc: kind === "FOUND" ? "2026-07-20T03:34:40Z" : null,
      first_absent_time_utc: kind === "FOUND" ? "2026-07-20T03:34:41Z" : null,
      observed_start_time_utc: "2026-07-20T03:34:28Z",
      observed_end_time_utc: "2026-07-20T03:35:27.873Z",
      coverage_complete: true,
      source_timezone: "Asia/Seoul",
    } : null,
  };
}

function successorStatus(kind, reason = null) {
  return status(kind, reason, 8);
}

function observedSchema8InconclusiveStatus() {
  return {
    investigation_id: "object-disappearance-v3-ch2-20260910T032148Z",
    run_id: "search-run-1a7ba9fb6f9e4dd183b5b834c1b62328",
    schema_version: 8,
    status: "INCONCLUSIVE",
    reason_code: "indeterminate_observation",
    terminal_result_id: "successor-terminal-v1-b9816b6653eb929d04495e617c73a6e0fe643b72ebdbd9039e0f6f8207a8602d",
    phase8_status: "NOT_REQUESTED",
    phase8_reason: "successor_slice5_does_not_create_handoffs",
    terminal_details: {
      last_present_time_utc: null,
      first_absent_time_utc: null,
      observed_start_time_utc: "2026-09-10T03:21:48Z",
      observed_end_time_utc: "2026-09-10T03:31:48Z",
      coverage_complete: false,
      source_timezone: "Asia/Seoul",
    },
  };
}

function foundStatusWithTiming() {
  return {
    ...status("FOUND", "SUPPORTED_TRANSITION"),
    terminal_details: {
      last_present_time_utc: "2026-07-20T03:34:40Z",
      first_absent_time_utc: "2026-07-20T03:34:41Z",
      observed_start_time_utc: "2026-07-20T03:34:28Z",
      observed_end_time_utc: "2026-07-20T03:35:27.873Z",
      coverage_complete: true,
      source_timezone: "Asia/Seoul",
    },
  };
}

function evidenceEntry(overrides = {}) {
  return {
    role: "observation",
    plan_id: "successor-plan-v1-test",
    observation_id: "successor-observation-v1-" + "1".repeat(64),
    target_id: "successor-target-v1-test",
    acquisition_id: "successor-acquisition-v1-test",
    assigned_segment_id: "segment-v1-test",
    requested_time_utc: "2026-07-20T03:34:40Z",
    frame_utc: "2026-07-20T03:34:40Z",
    frame_pts_seconds: 12.5,
    frame_ordinal: 2,
    frame_offset_seconds: 0,
    digest: "b".repeat(64),
    width: 2560,
    height: 1440,
    authority_identity: "successor-authority-v1-test",
    reference_frame_resource_id: "resource--10",
    roi_identity: "successor-roi-v1-test",
    classifier_policy_identity: "classifier-policy-v1-test",
    acquisition_status: "FRAME_AVAILABLE",
    state: "PRESENT",
    reason_code: null,
    comparison: {
      baseline_mask_pixel_count: 100,
      probe_mask_pixel_count: 100,
      roi_pixel_count: 43200,
      mask_intersection_pixel_count: 100,
      mask_union_pixel_count: 100,
      baseline_mask_coverage: 0.01,
      probe_mask_coverage: 0.01,
      mask_iou: 1,
      effective_comparison_area: 100,
      roi_luma_ncc: 1,
      visual_status: "COMPARABLE",
      unusable_reason: null,
    },
    classifier_stage: "completed",
    classifier_elapsed_ms: 42,
    path: "frames/" + "b".repeat(64) + ".jpg",
    roi_path: "frames/" + "d".repeat(64) + ".jpg",
    roi_digest: "d".repeat(64),
    ...overrides,
  };
}

function evidenceManifest(statusKind = "FOUND") {
  const baselineDigest = "a".repeat(64);
  const presentId = "successor-observation-v1-" + "1".repeat(64);
  const absentId = "successor-observation-v1-" + "2".repeat(64);
  const present = evidenceEntry({ observation_id: presentId, digest: "b".repeat(64), path: "frames/" + "b".repeat(64) + ".jpg" });
  const absent = evidenceEntry({
    observation_id: absentId,
    requested_time_utc: "2026-07-20T03:34:41Z",
    frame_utc: "2026-07-20T03:34:41Z",
    digest: "c".repeat(64),
    path: "frames/" + "c".repeat(64) + ".jpg",
    state: "ABSENT",
    comparison: { ...present.comparison, mask_iou: 0, roi_luma_ncc: 0 },
    roi_path: "frames/" + "e".repeat(64) + ".jpg",
    roi_digest: "e".repeat(64),
  });
  const baseline = {
    role: "baseline",
    plan_id: "successor-plan-v1-test",
    observation_id: null,
    target_id: "historical-baseline",
    acquisition_id: null,
    assigned_segment_id: null,
    requested_time_utc: "2026-07-20T03:34:08Z",
    frame_utc: "2026-07-20T03:34:08Z",
    frame_pts_seconds: 0,
    frame_ordinal: 1,
    frame_offset_seconds: null,
    digest: baselineDigest,
    width: 2560,
    height: 1440,
    authority_identity: "successor-authority-v1-test",
    reference_frame_resource_id: "resource--10",
    roi_identity: "successor-roi-v1-test",
    classifier_policy_identity: null,
    acquisition_status: "FRAME_AVAILABLE",
    state: "PRESENT",
    reason_code: null,
    comparison: null,
    classifier_stage: null,
    classifier_elapsed_ms: null,
    path: "frames/" + baselineDigest + ".jpg",
    roi_path: "frames/" + "f".repeat(64) + ".jpg",
    roi_digest: "f".repeat(64),
  };
  return {
    version: "phase7e-successor-evidence-v1",
    investigation_id: INVESTIGATION_ID,
    run_id: RUN_ID,
    plan_id: "successor-plan-v1-test",
    authority_identity: "successor-authority-v1-test",
    roi_identity: "successor-roi-v1-test",
    roi: { x: 120, y: 80, width: 240, height: 180, coordinate_space: "source_pixels", provenance: "manual" },
    source_width: 2560,
    source_height: 1440,
    terminal_status: statusKind,
    terminal_reason: statusKind === "FOUND" ? "disappearance_confirmed" : "indeterminate_observation",
    last_present_observation_id: statusKind === "FOUND" ? presentId : null,
    first_absent_observation_id: statusKind === "FOUND" ? absentId : null,
    review_clip: { status: "UNAVAILABLE", reason: "phase8_not_requested" },
    entries: [baseline, present, ...(statusKind === "FOUND" ? [absent] : [])],
  };
}

function currentEvidenceManifest(statusKind = "INCONCLUSIVE") {
  const payload = evidenceManifest(statusKind);
  payload.entries = payload.entries.map((entry) => ({
    ...entry,
    acquisition_mode: "normal",
    target_delta_ms: entry.role === "baseline" ? null : 0,
    cadence_source: entry.role === "baseline" ? null : "adjacent_pts",
    cadence_ms: entry.role === "baseline" ? null : 34,
    tolerance_ms: entry.role === "baseline" ? null : 134,
    raw_segment_end_utc: entry.role === "baseline" ? null : "2026-07-20T03:35:00Z",
    media_validation_outcome: entry.role === "baseline" ? "validated" : "validated",
  }));
  return payload;
}

function loadedConfirmation() {
  return {
    investigation_id: INVESTIGATION_ID,
    outcome: "created",
    status: "confirmed",
    schema_version: 3,
    confirmed_at_utc: "2026-07-20T03:35:00Z",
    artifact_directory_relative: `artifacts/investigations/${INVESTIGATION_ID}`,
    confirmation: {
      channel_id: 1,
      candidate_offset_seconds: -10,
      reference_frame_resource_id: "resource--10",
      requested_time_utc: "2026-07-20T03:34:08Z",
      source_timezone: "Asia/Seoul",
      timing: { estimated_source_time_utc: null, timing_precision_status: "measured_clip_relative" },
      source_width: 2560,
      source_height: 1440,
      roi: { x: 120, y: 80, width: 240, height: 180, coordinate_space: "source_pixels", provenance: "manual" },
    },
  };
}

function loadedFutureBaselineConfirmation() {
  const payload = loadedConfirmation();
  return {
    ...payload,
    confirmation: {
      ...payload.confirmation,
      candidate_offset_seconds: 60,
      requested_time_utc: "2026-07-20T03:35:18Z",
    },
  };
}

test("confirmed workflow submits only the closed start body and blocks a double click", async () => {
  const post = deferred();
  const requests = [];
  const harness = createHarness((url, options) => {
    requests.push({ url, options });
    if (url === "/api/v1/recording-searches") return post.promise;
    return Promise.resolve({ ok: true, status: 200, json: async () => status("RUNNING") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();

  assert.equal(harness.recordingSearchPanel.hidden, false);
  assert.equal(harness.recordingSearchTimezone.textContent, "Asia/Seoul");
  assert.equal(harness.recordingSearchStart.disabled, false);
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  assert.equal(requests.filter((entry) => entry.url === "/api/v1/recording-searches").length, 1);
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    investigation_id: INVESTIGATION_ID,
    search_end: "2026-07-20T12:40:00",
    request_id: REQUEST_ID,
  });

  post.resolve({ ok: true, status: 202, json: async () => accepted() });
  await settle();
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().runId, RUN_ID);
  assert.match(harness.window.location.href, /run_id=search-run-/);
  assert.equal(harness.recordingSearchStatus.textContent, "검색 중입니다.");
});

test("successor admission failure is shown as a safe configuration message", async () => {
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({
        ok: false,
        status: 503,
        json: async () => ({
          error: {
            code: "successor_unavailable",
            message: "The long-range recording search is unavailable.",
            details: null,
          },
        }),
      });
    }
    throw new Error("status must not be polled after admission failure");
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();

  assert.equal(
    harness.recordingSearchStatus.textContent,
    "장시간 녹화 검색 기능을 준비할 수 없습니다. 서버 설정을 확인하세요.",
  );
  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
});

test("frontend reason vocabulary covers every successor terminal reason", () => {
  const harness = createHarness(() => Promise.resolve({ ok: true, status: 200, json: async () => status("RUNNING") }), undefined, {
    confirmation: true,
    search: true,
    requestId: REQUEST_ID,
  });
  const expected = [
    "disappearance_confirmed", "complete_present_coverage", "no_present_absent_bracket",
    "incomplete_coverage", "indeterminate_observation", "insufficient_visual_evidence",
    "invalid_frame_or_roi", "frame_decode_failed", "frame_resolution_mismatch",
    "target_unavailable_gap", "target_recording_unavailable", "target_replay_timeout",
    "target_replay_failed", "target_decode_timeout", "target_decode_unavailable",
    "classifier_timeout", "classifier_failed", "midpoint_gap",
    "midpoint_acquisition_unavailable", "midpoint_indeterminate",
    "midpoint_classification_unavailable", "no_progress", "cancelled",
    "abandoned_after_restart", "internal_error",
    "execution_deadline_exhausted",
  ];
  const actual = new Set(harness.window.vigiVisionRecordingSearch.getReasonCodes());
  expected.forEach((reason) => assert.equal(actual.has(reason), true, reason));
});

test("recording-unavailable terminal reason is rendered with its fixed explanation", async () => {
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => status("FAILED", "recording_unavailable"),
    });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(
    harness.recordingSearchResultReason.textContent,
    "해당 검색 범위의 녹화 기록을 충분히 확인할 수 없습니다.",
  );
});

test("real Schema 8 INCONCLUSIVE terminal payload is accepted", async () => {
  const requestId = "1a7ba9fb-6f9e-4dd1-83b5-b834c1b62328";
  const investigationId = "object-disappearance-v3-ch2-20260910T032148Z";
  const runId = "search-run-1a7ba9fb6f9e4dd183b5b834c1b62328";
  let statusCalls = 0;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({
        ok: true,
        status: 202,
        json: async () => ({
          request_id: requestId,
          investigation_id: investigationId,
          run_id: runId,
          status: "ACCEPTED",
          status_url: `/api/v1/recording-searches/${investigationId}/${runId}`,
        }),
      });
    }
    statusCalls += 1;
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => observedSchema8InconclusiveStatus(),
    });
  }, undefined, { confirmation: true, search: true, requestId });
  harness.window.dispatchEvent({
    type: "vigi:investigation-confirmed",
    detail: {
      investigationId,
      anchorTimeUtc: "2026-09-10T03:21:48Z",
      sourceTimezone: "Asia/Seoul",
      schemaVersion: 3,
    },
  });
  harness.recordingSearchEnd.value = "2026-09-10T12:44:18";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(statusCalls, 1);
  assert.equal(harness.recordingSearchStatus.textContent, "녹화 기록 검색이 종료되었습니다.");
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.equal(
    harness.recordingSearchResultKind.textContent,
    "자동 판정이 불확실합니다. 기준 시점과 종료 시점을 직접 비교하세요.",
  );
  assert.match(harness.recordingSearchResultReason.textContent, /신뢰성 있게 판단할 수 없습니다/);
  assert.equal(harness.recordingSearchLastPresent.textContent, "해당 없음");
  assert.equal(harness.recordingSearchFirstAbsent.textContent, "해당 없음");
  assert.match(harness.recordingSearchObservedRange.textContent, /2026-09-10T12:21:48/);
  assert.equal(harness.pendingTimerCount(), 0);
});

test("malformed Phase 8 status/reason pairs are rejected instead of rendered", async () => {
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    const payload = successorStatus("INCONCLUSIVE", "indeterminate_observation");
    payload.phase8_status = "READY";
    payload.phase8_reason = "phase8_media_corrupt";
    return Promise.resolve({ ok: true, status: 200, json: async () => payload });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:44:18";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.match(harness.recordingSearchStatus.textContent, /다시 확인하고 있습니다/);
  assert.equal(harness.pendingTimerCount(), 1);
});

test("reload restores the exact submitted run and search end from session storage", async () => {
  const storage = new Map();
  const first = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("RUNNING") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID, storage });
  dispatchConfirmed(first);
  first.recordingSearchEnd.value = "2026-07-20T12:44:18";
  first.recordingSearchEnd.listeners.input();
  first.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();

  const stored = JSON.parse(storage.get("vigiVision.recordingSearch.activeRun.v1"));
  assert.deepEqual(stored, {
    version: 1,
    investigation_id: INVESTIGATION_ID,
    run_id: RUN_ID,
    request_id: REQUEST_ID,
    search_end: "2026-07-20T12:44:18",
    duration_seconds: 600,
  });

  const location = `http://127.0.0.1/?investigation_id=${INVESTIGATION_ID}&run_id=${RUN_ID}`;
  const restored = createHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => loadedConfirmation() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, location, storage });
  await settle();
  restored.runTimers();
  await settle();

  assert.equal(restored.recordingSearchEnd.value, "2026-07-20T12:44:18");
  assert.equal(restored.recordingSearchQuickButtons[0].attributes["aria-pressed"], "true");
  assert.equal(restored.recordingSearchStart.disabled, false);
  assert.equal(restored.recordingSearchResult.hidden, false);
  assert.equal(restored.recordingSearchStatus.textContent, "녹화 기록 검색이 종료되었습니다.");
  assert.equal(restored.pendingTimerCount(), 0);
});

test("legacy run without stored search end derives a safe observed end on terminal restore", async () => {
  const location = `http://127.0.0.1/?investigation_id=${INVESTIGATION_ID}&run_id=${RUN_ID}`;
  const harness = createHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => loadedConfirmation() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, location, storage: new Map() });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(harness.recordingSearchEnd.value, "2026-07-20T12:35:27");
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.equal(harness.pendingTimerCount(), 0);
});

test("a stored run for another run ID never crosses the restored lifecycle", async () => {
  const otherRunId = "search-run-87654321432144328432abcdefabcdef";
  const storage = new Map([
    ["vigiVision.recordingSearch.activeRun.v1", JSON.stringify({
      version: 1,
      investigation_id: INVESTIGATION_ID,
      run_id: RUN_ID,
      request_id: REQUEST_ID,
      search_end: "2026-07-20T12:44:18",
      duration_seconds: 600,
    })],
  ]);
  const location = `http://127.0.0.1/?investigation_id=${INVESTIGATION_ID}&run_id=${otherRunId}`;
  const harness = createHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => loadedConfirmation() });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ ...status("RUNNING"), run_id: otherRunId }),
    });
  }, undefined, { confirmation: true, search: true, location, storage });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(harness.recordingSearchEnd.value, "2026-07-20T13:04:18");
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().runId, otherRunId);
  assert.equal(harness.pendingTimerCount(), 1);
});

test("restored search defaults to a 30-minute range and exposes keyboard quick ranges", async () => {
  const harness = createHarness(() => Promise.resolve({ ok: true, status: 200, json: async () => status("RUNNING") }), undefined, {
    confirmation: true,
    search: true,
  });
  dispatchConfirmed(harness);

  assert.equal(harness.recordingSearchEnd.value, "2026-07-20T13:04:18");
  assert.deepEqual(
    harness.recordingSearchQuickButtons.map((button) => button.attributes["aria-pressed"]),
    ["false", "true", "false", "false"],
  );
  const expected = [
    [0, "2026-07-20T12:44:18"],
    [1, "2026-07-20T13:04:18"],
    [2, "2026-07-20T13:34:18"],
    [3, "2026-07-20T14:34:18"],
  ];
  expected.forEach(([index, value]) => {
    harness.recordingSearchQuickButtons[index].listeners.click({ preventDefault() {} });
    assert.equal(harness.recordingSearchEnd.value, value);
    assert.equal(harness.recordingSearchQuickButtons[index].attributes["aria-pressed"], "true");
  });
});

test("confirmed future baseline admits minute input only after the effective start", async () => {
  const harness = createHarness(() => Promise.resolve({ ok: true, status: 202, json: async () => accepted() }), undefined, {
    confirmation: true,
    search: true,
  });
  harness.window.dispatchEvent({
    type: "vigi:investigation-confirmed",
    detail: {
      investigationId: INVESTIGATION_ID,
      anchorTimeUtc: "2026-07-20T03:34:18Z",
      baselineTimeUtc: "2026-07-20T03:35:18Z",
      sourceTimezone: "Asia/Seoul",
      schemaVersion: 3,
    },
  });

  harness.recordingSearchEnd.value = "2026-07-20T13:47";
  harness.recordingSearchEnd.listeners.input();
  assert.equal(harness.recordingSearchStart.disabled, false);

  harness.recordingSearchEnd.value = "2026-07-20T12:35:18";
  harness.recordingSearchEnd.listeners.input();
  assert.equal(harness.recordingSearchStart.disabled, true);
  assert.equal(harness.recordingSearchStart.dataset.state, "invalid");
  assert.match(harness.recordingSearchStatus.textContent, /기준 프레임 중 늦은 시각 이후/);
});

test("minute input is canonicalized before one successor admission POST", async () => {
  const requests = [];
  const harness = createHarness((url, options) => {
    requests.push({ url, options });
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("RUNNING") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  harness.window.dispatchEvent({
    type: "vigi:investigation-confirmed",
    detail: {
      investigationId: INVESTIGATION_ID,
      anchorTimeUtc: "2026-07-20T03:34:18Z",
      baselineTimeUtc: "2026-07-20T03:35:18Z",
      sourceTimezone: "Asia/Seoul",
      schemaVersion: 3,
    },
  });
  harness.recordingSearchEnd.value = "2026-07-20T13:47";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();

  const startRequests = requests.filter((entry) => entry.url === "/api/v1/recording-searches");
  assert.equal(startRequests.length, 1);
  assert.equal(JSON.parse(startRequests[0].options.body).search_end, "2026-07-20T13:47:00");
});

test("reopened confirmation restores a future baseline as the effective start", async () => {
  const location = `http://127.0.0.1/?investigation_id=${INVESTIGATION_ID}`;
  const harness = createHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => loadedFutureBaselineConfirmation() });
    }
    throw new Error("status must not be polled without a run");
  }, undefined, { confirmation: true, search: true, location });
  await settle();

  assert.equal(harness.recordingSearchEnd.value, "2026-07-20T13:05:18");
  assert.equal(harness.recordingSearchStart.disabled, false);
  assert.equal(harness.recordingSearchStart.dataset.state, "ready");
});

test("successor range bounds accept 30 minutes through two hours and reject unsafe ends", async () => {
  const requests = [];
  const harness = createHarness((url) => {
    requests.push(url);
    return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  for (const value of ["2026-07-20T14:34:18"]) {
    harness.recordingSearchEnd.value = value;
    harness.recordingSearchEnd.listeners.input();
    assert.equal(harness.recordingSearchStart.disabled, false);
  }
  for (const value of ["2026-07-20T12:34:18", "2026-07-20T14:34:19"]) {
    harness.recordingSearchEnd.value = value;
    harness.recordingSearchEnd.listeners.input();
    assert.equal(harness.recordingSearchStart.disabled, true);
    harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  }
  assert.equal(requests.length, 0);
});

test("Schema 8 terminal payloads render localized facts and remain restartable", async () => {
  const secondRequestId = "87654321-4321-4432-8432-abcdefabcdef";
  let postCalls = 0;
  const harness = createHarness((url, options) => {
    if (url === "/api/v1/recording-searches") {
      postCalls += 1;
      const requestId = JSON.parse(options.body).request_id;
      const runId = `search-run-${requestId.replaceAll("-", "")}`;
      return Promise.resolve({
        ok: true,
        status: 202,
        json: async () => ({
          request_id: requestId,
          investigation_id: INVESTIGATION_ID,
          run_id: runId,
          status: "ACCEPTED",
          status_url: `/api/v1/recording-searches/${INVESTIGATION_ID}/${runId}`,
        }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("FOUND") });
  }, undefined, {
    confirmation: true,
    search: true,
    requestIds: [REQUEST_ID, secondRequestId],
  });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T14:34:18";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.match(harness.recordingSearchResultKind.textContent, /사라진 구간/);
  assert.match(harness.recordingSearchObservedRange.textContent, /Asia\/Seoul/);
  assert.equal(harness.recordingSearchError.hidden, true);
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(postCalls, 2);
});

for (const terminal of ["FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED", "INTERRUPTED", "CORRUPT"]) {
  test(`polling stops and renders safe request-relative ${terminal}`, async () => {
    let statusCalls = 0;
    const harness = createHarness((url) => {
      if (url === "/api/v1/recording-searches") {
        return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
      }
      statusCalls += 1;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => status(statusCalls === 1 ? "RUNNING" : terminal, "bounded_reason"),
      });
    }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
    dispatchConfirmed(harness);
    harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
    harness.recordingSearchEnd.listeners.input();
    harness.recordingSearchStart.listeners.click({ preventDefault() {} });
    await settle();
    harness.runTimers();
    await settle();
    assert.equal(statusCalls, 1);
    harness.runTimers();
    await settle();
    assert.equal(statusCalls, 2);
    assert.equal(harness.pendingTimerCount(), 0);
    assert.equal(harness.recordingSearchResult.hidden, false);
    assert.equal(harness.recordingSearchStart.disabled, false);
    assert.doesNotMatch(harness.recordingSearchResultKind.textContent, /theft|identity|intent|UTC/i);
    if (terminal === "NOT_FOUND") {
      assert.equal(harness.recordingSearchResultKind.textContent, "검색 종료 시점에도 대상이 존재합니다.");
    }
    if (terminal === "INCONCLUSIVE") {
      assert.equal(harness.recordingSearchResultKind.textContent, "자동 판정이 불확실합니다. 기준 시점과 종료 시점을 직접 비교하세요.");
    }
  });
}

for (const terminal of ["FOUND", "NOT_FOUND", "INCONCLUSIVE", "FAILED", "INTERRUPTED", "CORRUPT"]) {
  test(`accepts ${terminal} with the documented NOT_REQUESTED Phase 8 status`, async () => {
    let statusCalls = 0;
    const harness = createHarness((url) => {
      if (url === "/api/v1/recording-searches") {
        return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
      }
      statusCalls += 1;
      const payload = status(terminal, terminal === "FAILED" ? "media_probe_failed" : null);
      payload.phase8_status = "NOT_REQUESTED";
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
    dispatchConfirmed(harness);
    harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
    harness.recordingSearchEnd.listeners.input();
    harness.recordingSearchStart.listeners.click({ preventDefault() {} });
    await settle();
    harness.runTimers();
    await settle();
    assert.equal(statusCalls, 1);
    assert.equal(harness.recordingSearchStatus.textContent, "녹화 기록 검색이 종료되었습니다.");
    assert.equal(harness.recordingSearchStart.disabled, false);
    assert.equal(harness.pendingTimerCount(), 0);
    assert.equal(harness.window.vigiVisionRecordingSearch.getState().runId, RUN_ID);
  });
}

test("FOUND renders the honest localized disappearance interval and observed range", async () => {
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => foundStatusWithTiming() });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();

  assert.match(harness.recordingSearchLastPresent.textContent, /2026-07-20T12:34:40.*Asia\/Seoul/);
  assert.match(harness.recordingSearchFirstAbsent.textContent, /2026-07-20T12:34:41.*Asia\/Seoul/);
  assert.match(harness.recordingSearchInterval.textContent, /12:34:40.*12:34:41/);
  assert.match(harness.recordingSearchObservedRange.textContent, /12:34:28.*12:35:27/);
});

test("terminal FOUND loads identity-bound visual evidence and both bracket frames", async () => {
  const requests = [];
  const harness = createHarness((url) => {
    requests.push(url);
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => evidenceManifest("FOUND") });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => foundStatusWithTiming() });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(requests.filter((url) => url.endsWith("/evidence")).length, 1);
  assert.equal(harness.recordingSearchEvidence.hidden, false);
  assert.match(harness.recordingSearchEvidenceStatus.textContent, /비교할 수 있습니다/);
  assert.match(harness.recordingSearchBaselineImage.src, /evidence\/a{64}$/);
  assert.match(harness.recordingSearchEndImage.src, /evidence\/[bc]{64}$/);
  assert.equal(harness.recordingSearchFoundEvidence.hidden, false);
  assert.match(harness.recordingSearchLastPresentImage.src, /evidence\/b{64}$/);
  assert.match(harness.recordingSearchFirstAbsentImage.src, /evidence\/c{64}$/);
  assert.match(harness.recordingSearchReviewClipStatus.textContent, /사용할 수 없습니다/);
});

test("search-end evidence selection is role and time based, not array order", async () => {
  const payload = evidenceManifest("FOUND");
  payload.entries = [payload.entries[0], payload.entries[2], payload.entries[1]];
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => foundStatusWithTiming() });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.match(harness.recordingSearchEndImage.src, /evidence\/c{64}$/);
  assert.match(harness.recordingSearchEndTime.textContent, /03:34:41/);
});

test("legacy terminal run without evidence remains readable with a safe unavailable message", async () => {
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "evidence_unavailable" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(harness.recordingSearchEvidence.hidden, false);
  assert.equal(harness.recordingSearchEvidenceStatus.textContent, "이 실행에 보존된 시각 증거가 없습니다.");
});

test("an anchor-only terminal manifest never masquerades as search-end evidence", async () => {
  const payload = evidenceManifest("INCONCLUSIVE");
  payload.entries[1].role = "anchor";
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(harness.recordingSearchEvidence.hidden, false);
  assert.equal(harness.recordingSearchEndImage.hidden, true);
  assert.equal(harness.recordingSearchEndTime.textContent, "");
  assert.equal(harness.recordingSearchEvidenceStatus.textContent, "이 실행에 보존된 시각 증거가 없습니다.");
});

test("current 34-key evidence renders an INDETERMINATE observation for visual review", async () => {
  const payload = currentEvidenceManifest("INCONCLUSIVE");
  payload.entries[1].state = "INDETERMINATE";
  payload.entries[1].reason_code = "insufficient_visual_evidence";
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE", "insufficient_visual_evidence") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(harness.recordingSearchEvidence.hidden, false);
  assert.equal(harness.recordingSearchEvidenceStatus.textContent, "자동 판정이 불확실하므로 직접 비교하세요.");
  assert.match(harness.recordingSearchBaselineImage.src, /evidence\/a{64}$/);
  assert.match(harness.recordingSearchEndImage.src, /evidence\/b{64}$/);
  assert.equal(harness.recordingSearchEndCaption.textContent, "최근 유효 관측");
  assert.equal(harness.recordingSearchEndFrameMeta.hidden, true);
});

test("Schema 8 evidence review projects scene and alignment observability", async () => {
  const payload = currentEvidenceManifest("INCONCLUSIVE");
  payload.entries[1].state = "INDETERMINATE";
  payload.entries[1].reason_code = "insufficient_visual_evidence";
  payload.entries[1].comparison = {
    ...payload.entries[1].comparison,
    baseline_support_stability_pixel_count: 43200,
    baseline_support_stability_changed_pixel_count: 100,
    baseline_support_stability_valid_pixel_count: 200,
    baseline_support_stability_excluded_pixel_count: 43000,
    baseline_support_alignment_candidates_generated: 25,
    baseline_support_alignment_candidates_evaluated: 25,
    baseline_support_alignment_valid_candidates: 25,
    baseline_support_alignment_state: "aligned",
    baseline_support_scene_stable: true,
    baseline_support_scene_stability_veto_reason: null,
    baseline_support_present_gate_passed: false,
    baseline_support_absent_gate_passed: true,
    baseline_support_empty_background_evidence: true,
    baseline_support_replacement_evidence: false,
    baseline_support_occlusion_evidence: false,
    baseline_support_decision_path: "absent",
    baseline_support_decision_reason: "absent_empty_background",
  };
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  const metrics = textOf(harness.recordingSearchEvidenceMetrics);
  assert.match(metrics, /Alignment statealigned/);
  assert.match(metrics, /Scene stabilitytrue/);
  assert.match(metrics, /Stability changed pixels100/);
  assert.match(metrics, /Decision pathabsent/);
  assert.match(metrics, /Decision reasonabsent_empty_background/);
  assert.equal(harness.recordingSearchEvidenceDecision.textContent, "세부 판정 경로: 객체 없음 및 빈 배경 확인");
  assert.equal(harness.recordingSearchEvidenceDecision.hidden, false);
});

test("current evidence uses the search-end caption only when the observation reaches the terminal end", async () => {
  const payload = currentEvidenceManifest("INCONCLUSIVE");
  payload.entries[1].frame_utc = "2026-07-20T03:35:27.873Z";
  payload.entries[1].requested_time_utc = "2026-07-20T03:35:27.873Z";
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(harness.recordingSearchEndCaption.textContent, "검색 종료 관측");
});

test("extended evidence exposes observable-frame fallback metadata", async () => {
  const payload = currentEvidenceManifest("INCONCLUSIVE");
  payload.entries = payload.entries.map((entry) => ({
    ...entry,
    fallback_used: entry.role !== "baseline",
    fallback_reason: entry.role !== "baseline" ? "ROI_OCCLUDED" : null,
    observability: entry.role !== "baseline" ? "USABLE" : "USABLE",
  }));
  payload.entries[1].requested_time_utc = "2026-07-20T03:34:41Z";
  payload.entries[1].frame_utc = "2026-07-20T03:34:40Z";
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(harness.recordingSearchEndFrameMeta.hidden, false);
  assert.equal(harness.recordingSearchEndFrameMeta.children.length, 4);
  assert.equal(harness.recordingSearchEndFrameMeta.children[2].children[1].textContent,
    "ROI가 관측되지 않아 가까운 프레임을 사용");
});

test("a verified evidence image failure is reported separately from schema failure", async () => {
  const payload = currentEvidenceManifest("INCONCLUSIVE");
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  harness.recordingSearchEndImage.listeners.error();
  assert.equal(harness.recordingSearchEvidenceStatus.textContent, "검증된 시각 증거 이미지를 불러올 수 없습니다.");
});

test("current evidence rejects malformed nested comparison data", async () => {
  const payload = currentEvidenceManifest("INCONCLUSIVE");
  payload.entries[1].comparison.unexpected_metric = 1;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(harness.recordingSearchEvidenceStatus.textContent, "시각 증거 형식을 안전하게 확인할 수 없습니다.");
});

test("current evidence rejects a resource identity mismatch", async () => {
  const payload = currentEvidenceManifest("INCONCLUSIVE");
  payload.entries[1].reference_frame_resource_id = "foreign-resource";
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    if (url.endsWith("/evidence")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => payload });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE") });
  }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  await settle();

  assert.equal(harness.recordingSearchEvidenceStatus.textContent, "시각 증거 형식을 안전하게 확인할 수 없습니다.");
});

test("current evidence accepts all closed transport metadata values and rejects unknown keys", async () => {
  const fields = [
    ["acquisition_mode", "invalid"],
    ["cadence_ms", -1],
    ["cadence_ms", Number.NaN],
    ["cadence_ms", Number.POSITIVE_INFINITY],
    ["cadence_ms", 10001],
    ["tolerance_ms", -1],
    ["tolerance_ms", 2001],
    ["cadence_source", "other"],
    ["media_validation_outcome", "unknown"],
    ["raw_segment_end_utc", "not-a-timestamp"],
    ["target_delta_ms", -1],
    ["target_delta_ms", 10001],
    ["unknown_key", true],
  ];
  for (const [field, value] of fields) {
    const payload = currentEvidenceManifest("INCONCLUSIVE");
    payload.entries[1][field] = value;
    const harness = createHarness((url) => {
      if (url === "/api/v1/recording-searches") {
        return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
      }
      if (url.endsWith("/evidence")) {
        return Promise.resolve({ ok: true, status: 200, json: async () => payload });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => successorStatus("INCONCLUSIVE") });
    }, undefined, { confirmation: true, search: true, evidence: true, requestId: REQUEST_ID });
    dispatchConfirmed(harness);
    harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
    harness.recordingSearchEnd.listeners.input();
    harness.recordingSearchStart.listeners.click({ preventDefault() {} });
    await settle();
    harness.runTimers();
    await settle();
    await settle();
    assert.equal(harness.recordingSearchEvidenceStatus.textContent, "시각 증거 형식을 안전하게 확인할 수 없습니다.", field);
  }
});

test("an exact missing polled run never inherits another run and is permanent", async () => {
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    return Promise.resolve({
      ok: false,
      status: 404,
      json: async () => ({ error: { code: "search_run_not_found" } }),
    });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.match(harness.recordingSearchStatus.textContent, /접수된 검색 실행을 찾을 수 없습니다/);
  assert.doesNotMatch(harness.recordingSearchStatus.textContent, /검색이 안전하게 실패했습니다/);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
});

test("a transient polling failure reconnects and reaches the exact terminal run", async () => {
  let statusCalls = 0;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusCalls += 1;
    return statusCalls === 1
      ? Promise.reject(new Error("temporary status transport failure"))
      : Promise.resolve({ ok: true, status: 200, json: async () => status("FOUND") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.match(harness.recordingSearchStatus.textContent, /검색 상태를 다시 확인하고 있습니다/);
  assert.doesNotMatch(harness.recordingSearchStatus.textContent, /검색이 안전하게 실패했습니다/);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, true);
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 2);
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.equal(harness.pendingTimerCount(), 0);
});

test("bounded exponential polling failures recover without overlapping requests", async () => {
  let statusCalls = 0;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusCalls += 1;
    if (statusCalls === 2) {
      return Promise.resolve({ ok: false, status: 503, json: async () => ({}) });
    }
    if (statusCalls < 4) return Promise.reject(new Error("transient"));
    return Promise.resolve({ ok: true, status: 200, json: async () => status("FOUND") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();

  for (const delay of [2_000, 4_000, 8_000]) {
    harness.runTimers();
    await settle();
    assert.deepEqual(harness.pendingTimerDelays(), [delay]);
    assert.match(harness.recordingSearchStatus.textContent, /서버 작업은 계속될 수 있습니다/);
  }
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 4);
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.equal(harness.pendingTimerCount(), 0);
});

test("polling retry budget ends only client observation after five retries", async () => {
  let statusCalls = 0;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusCalls += 1;
    return Promise.reject(new Error("still transient"));
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();

  for (const delay of [2_000, 4_000, 8_000, 15_000, 15_000]) {
    harness.runTimers();
    await settle();
    assert.deepEqual(harness.pendingTimerDelays(), [delay]);
  }
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 6);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
  assert.equal(harness.pendingTimerCount(), 0);
  assert.match(harness.recordingSearchStatus.textContent, /클라이언트의 상태 확인이 종료되었습니다/);
  assert.doesNotMatch(harness.recordingSearchStatus.textContent, /검색이 안전하게 실패했습니다/);
});

test("pagehide during a pending reconnect permanently cancels the lifecycle", async () => {
  let statusCalls = 0;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusCalls += 1;
    return Promise.reject(new Error("transient"));
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  assert.equal(harness.pendingTimerCount(), 1);
  const reconnecting = harness.recordingSearchStatus.textContent;
  harness.window.dispatchEvent({ type: "pagehide" });
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 1);
  assert.equal(harness.pendingTimerCount(), 0);
  assert.equal(harness.recordingSearchStatus.textContent, reconnecting);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
});

test("reload strictly reopens confirmation and resumes status from the server", async () => {
  const requests = [];
  const location = `http://127.0.0.1/?investigation_id=${INVESTIGATION_ID}&run_id=${RUN_ID}`;
  const harness = createHarness((url) => {
    requests.push(url);
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => loadedConfirmation() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("NOT_FOUND", "search_exhausted") });
  }, undefined, { confirmation: true, search: true, location });
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(requests[0], `/api/v1/investigation-confirmations/${INVESTIGATION_ID}`);
  assert.ok(requests.some((url) => url === `/api/v1/recording-searches/${INVESTIGATION_ID}/${RUN_ID}`));
  assert.equal(harness.channelRequests(), 0);
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.equal(harness.recordingSearchPanel.scrollCalls.length, 1);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().runId, RUN_ID);
});

test("restoration wins asynchronous startup ordering before confirmation resolves", async () => {
  const confirmation = deferred();
  const channels = deferred();
  const location = `http://127.0.0.1/?investigation_id=${INVESTIGATION_ID}`;
  const requests = [];
  const harness = createHarness((url) => {
    requests.push(url);
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return confirmation.promise;
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("RUNNING") });
  }, () => channels.promise, { confirmation: true, search: true, location });

  assert.equal(harness.channelRequests(), 0);
  assert.equal(harness.candidateIntro.hidden, true);
  assert.equal(harness.candidateRequestPanel.hidden, true);
  assert.equal(harness.candidateResultsPanel.hidden, true);
  assert.equal(harness.selectedPreviewPanel.hidden, true);
  assert.equal(harness.recordingSearchPanel.hidden, false);
  assert.deepEqual(requests, [`/api/v1/investigation-confirmations/${INVESTIGATION_ID}`]);

  confirmation.resolve({ ok: true, status: 200, json: async () => loadedConfirmation() });
  await settle();
  assert.equal(harness.channelRequests(), 0);
  assert.equal(harness.recordingSearchConfirmedTime.textContent, "2026-07-20T12:34:18");
  assert.equal(harness.recordingSearchTimezone.textContent, "Asia/Seoul");
});

test("invalid search bounds never reach the HTTP start boundary and page teardown stops polling", async () => {
  const requests = [];
  const harness = createHarness((url) => {
    requests.push(url);
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("RUNNING") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  for (const invalid of [
    "2026-07-20T12:34:18",
    "2026-07-20T12:34:17",
    "2026-07-20T14:34:19",
    "not-a-time",
  ]) {
    harness.recordingSearchEnd.value = invalid;
    harness.recordingSearchEnd.listeners.input();
    harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  }
  assert.equal(requests.length, 0);

  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(harness.pendingTimerCount(), 1);
  harness.windowListeners.pagehide({});
  assert.equal(harness.pendingTimerCount(), 0);
});

test("deferred status completion after pagehide cannot render stale terminal evidence", async () => {
  const statusResponse = deferred();
  let statusOptions;
  const harness = createHarness((url, options) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusOptions = options;
    return statusResponse.promise;
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  const statusBeforeTeardown = harness.recordingSearchStatus.textContent;

  harness.window.dispatchEvent({ type: "pagehide" });
  statusResponse.resolve({ ok: true, status: 200, json: async () => status("FOUND") });
  await settle();
  await settle();

  assert.equal(statusOptions.signal.aborted, true);
  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.equal(harness.recordingSearchStatus.textContent, statusBeforeTeardown);
  assert.equal(harness.pendingTimerCount(), 0);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
});

test("a timed-out status request is aborted, retried, and reaches terminal", async () => {
  const statusResponse = new Promise(() => {});
  let statusCalls = 0;
  let statusOptions;
  const nativeError = "rtsp://user:password@nvr.example/private";
  const harness = createHarness((url, options) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusCalls += 1;
    if (statusCalls === 1) {
      statusOptions = options;
      return statusResponse;
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => status("NOT_FOUND") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  harness.runTimers();
  await settle();

  assert.equal(statusOptions.signal.aborted, true);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, true);
  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.match(harness.recordingSearchStatus.textContent, /검색 상태를 다시 확인하고 있습니다/);
  assert.doesNotMatch(harness.recordingSearchStatus.textContent, /rtsp|password|nvr\.example/i);
  assert.doesNotMatch(harness.recordingSearchError.textContent, new RegExp(nativeError));
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 2);
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.equal(harness.pendingTimerCount(), 0);
});

test("overall client deadline bounds an in-flight status request", async () => {
  const baseNow = Date.now();
  let now = baseNow;
  const statusResponse = new Promise(() => {});
  let statusOptions;
  const harness = createHarness((url, options) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusOptions = options;
    return statusResponse;
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID, now: () => now });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  now = baseNow + (45 * 60 * 1_000) - 5_000;
  harness.runTimers();
  await settle();
  assert.equal(harness.timerDelays.at(-1), 5_000);
  now = baseNow + (45 * 60 * 1_000);
  harness.runTimers();
  await settle();
  assert.equal(statusOptions.signal.aborted, true);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
  assert.equal(harness.pendingTimerCount(), 0);
  assert.match(harness.recordingSearchStatus.textContent, /클라이언트의 상태 확인이 종료되었습니다/);
  assert.doesNotMatch(harness.recordingSearchStatus.textContent, /검색이 안전하게 실패했습니다/);
});

test("intentional status abort after teardown does not announce a failure", async () => {
  let rejectStatus;
  const statusResponse = new Promise((_resolve, reject) => { rejectStatus = reject; });
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    return statusResponse;
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  const statusBeforeTeardown = harness.recordingSearchStatus.textContent;
  harness.window.dispatchEvent({ type: "pagehide" });
  rejectStatus(Object.assign(new Error("aborted"), { name: "AbortError" }));
  await settle();
  await settle();

  assert.equal(harness.recordingSearchStatus.textContent, statusBeforeTeardown);
  assert.equal(harness.recordingSearchError.hidden, true);
  assert.equal(harness.pendingTimerCount(), 0);
});

test("deferred start response after pagehide cannot establish a run or polling", async () => {
  const startResponse = deferred();
  let startOptions;
  const harness = createHarness((url, options) => {
    startOptions = options;
    return startResponse.promise;
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  const statusBeforeTeardown = harness.recordingSearchStatus.textContent;
  harness.window.dispatchEvent({ type: "pagehide" });
  startResponse.resolve({ ok: true, status: 202, json: async () => accepted() });
  await settle();
  await settle();

  assert.equal(startOptions.signal.aborted, true);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().runId, null);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
  assert.equal(harness.recordingSearchStatus.textContent, statusBeforeTeardown);
  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.equal(harness.pendingTimerCount(), 0);
});

test("old status completion cannot mutate a newer lifecycle", async () => {
  const firstStatus = deferred();
  const secondStatus = deferred();
  let statusCalls = 0;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusCalls += 1;
    return statusCalls === 1 ? firstStatus.promise : secondStatus.promise;
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  harness.window.dispatchEvent({ type: "pagehide" });

  harness.window.dispatchEvent({
    type: "vigi:investigation-confirmed",
    detail: {
      investigationId: INVESTIGATION_ID,
      anchorTimeUtc: "2026-07-20T03:34:18Z",
      sourceTimezone: "Asia/Seoul",
      schemaVersion: 3,
    },
  });
  await settle();
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 2);
  const statusBeforeOldResponse = harness.recordingSearchStatus.textContent;
  firstStatus.resolve({ ok: true, status: 200, json: async () => status("FOUND") });
  await settle();
  await settle();
  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.equal(harness.recordingSearchStatus.textContent, statusBeforeOldResponse);

  secondStatus.resolve({ ok: true, status: 200, json: async () => status("FOUND") });
  await settle();
  await settle();
  assert.equal(harness.recordingSearchResult.hidden, false);
});

test("status requests remain serialized and active timeout is safe", async () => {
  const firstStatus = deferred();
  let statusCalls = 0;
  const harness = createHarness((url) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusCalls += 1;
    return statusCalls === 1
      ? firstStatus.promise
      : Promise.resolve({ ok: true, status: 200, json: async () => status("FOUND") });
  }, undefined, { confirmation: true, search: true, requestId: REQUEST_ID });
  dispatchConfirmed(harness);
  harness.recordingSearchEnd.value = "2026-07-20T12:40:00";
  harness.recordingSearchEnd.listeners.input();
  harness.recordingSearchStart.listeners.click({ preventDefault() {} });
  await settle();
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 1);

  firstStatus.resolve({ ok: true, status: 200, json: async () => status("RUNNING") });
  await settle();
  await settle();
  harness.runTimers();
  await settle();
  assert.equal(statusCalls, 2);
  harness.runTimers();
  await settle();
  assert.equal(harness.recordingSearchResult.hidden, false);
  assert.equal(harness.pendingTimerCount(), 0);
});
