const assert = require("node:assert/strict");
const test = require("node:test");

const { createHarness, deferred } = require("./reference-frame-ui-harness");

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

function status(kind, reason = null) {
  return {
    investigation_id: INVESTIGATION_ID,
    run_id: RUN_ID,
    schema_version: ["ACCEPTED", "RUNNING"].includes(kind) ? 0 : 7,
    status: kind,
    reason_code: reason,
    terminal_result_id: ["FOUND", "NOT_FOUND", "INCONCLUSIVE"].includes(kind)
      ? "rr-terminal-result-v1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      : null,
    phase8_status: null,
    phase8_reason: null,
  };
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
    assert.equal(harness.recordingSearchStart.disabled, true);
    assert.doesNotMatch(harness.recordingSearchResultKind.textContent, /theft|identity|intent|UTC/i);
  });
}

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
    "2026-07-20T12:44:19",
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

test("never-resolving status request is aborted at its bounded request deadline", async () => {
  const statusResponse = new Promise(() => {});
  let statusOptions;
  const nativeError = "rtsp://user:password@nvr.example/private";
  const harness = createHarness((url, options) => {
    if (url === "/api/v1/recording-searches") {
      return Promise.resolve({ ok: true, status: 202, json: async () => accepted() });
    }
    statusOptions = options;
    return statusResponse;
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
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
  assert.equal(harness.pendingTimerCount(), 0);
  assert.equal(harness.recordingSearchResult.hidden, true);
  assert.match(harness.recordingSearchStatus.textContent, /녹화 기록 검색을 사용할 수 없습니다/);
  assert.doesNotMatch(harness.recordingSearchStatus.textContent, /rtsp|password|nvr\.example/i);
  assert.doesNotMatch(harness.recordingSearchError.textContent, new RegExp(nativeError));
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
  harness.runTimers();
  await settle();
  assert.equal(statusOptions.signal.aborted, true);
  assert.equal(harness.window.vigiVisionRecordingSearch.getState().polling, false);
  assert.equal(harness.pendingTimerCount(), 0);
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
