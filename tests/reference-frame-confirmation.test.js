const assert = require("node:assert/strict");
const test = require("node:test");

const {
  applyReferenceTime,
  candidate,
  candidateSet,
  createHarness,
  deferred,
  findElement,
  submit,
} = require("./reference-frame-ui-harness");

function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

function confirmationResponse(overrides = {}) {
  const { confirmation: confirmationOverrides = {}, ...responseOverrides } = overrides;
  const response = {
    investigation_id: "object-disappearance-ch1-20260720T033418Z",
    outcome: "created",
    status: "confirmed",
    schema_version: 2,
    confirmed_at_utc: "2026-08-02T03:04:05Z",
    artifact_directory_relative: "artifacts/investigations/object-disappearance-ch1-20260720T033418Z",
    confirmation: {
      channel_id: 1,
      candidate_offset_seconds: -10,
      reference_frame_resource_id: "resource--10",
      requested_time_utc: "2026-07-20T03:34:08Z",
      timing: { estimated_source_time_utc: null, timing_precision_status: "measured_clip_relative" },
      source_width: 2560,
      source_height: 1440,
      roi: { x: 120, y: 80, width: 240, height: 180, coordinate_space: "source_pixels", provenance: "manual" },
    },
    ...responseOverrides,
  };
  response.confirmation = { ...response.confirmation, ...confirmationOverrides };
  return response;
}

function schemaThreeConfirmationResponse(overrides = {}) {
  return confirmationResponse({
    ...overrides,
    investigation_id: "object-disappearance-v3-ch1-20260720T033418Z",
    schema_version: 3,
    artifact_directory_relative: "artifacts/investigations/object-disappearance-v3-ch1-20260720T033418Z",
  });
}

async function selectedHarness(fetchImplementation, channelResponse) {
  const harness = createHarness(fetchImplementation, channelResponse, { confirmation: true });
  applyReferenceTime(harness);
  await submit(harness);
  const card = harness.results.children[0];
  const image = card.children[0].children[0];
  const radio = findElement(card, "input");
  image.listeners.load();
  radio.checked = true;
  radio.listeners.change();
  harness.previewImage.rect = { left: 10, top: 20, width: 200, height: 160 };
  harness.previewImage.complete = true;
  harness.previewImage.listeners.load();
  await settle();
  return harness;
}

function commitRoi(harness) {
  harness.window.vigiVisionReferenceFrameRoi.replaceCommittedRoi(
    { source_width: 2560, source_height: 1440, x: 120, y: 80, width: 240, height: 180 },
    "ROI committed.",
  );
}

function assistedConfirmationResponse(roi) {
  return confirmationResponse({
    confirmation: {
      roi,
    },
  });
}

async function postAfterAssistedKeyboardAdjustment(event) {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      const body = JSON.parse(options.body);
      return Promise.resolve({ ok: true, status: 201, json: async () => assistedConfirmationResponse(body.roi) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  harness.window.vigiVisionReferenceFrameRoi.replaceCommittedRoi(
    { source_width: 2560, source_height: 1440, x: 120, y: 80, width: 240, height: 180 },
    "assisted ROI",
    "success",
    "assisted",
  );
  await settle();
  harness.roiStage.listeners.keydown({ ...event, preventDefault() {} });
  await settle();
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  return { harness, request: requests.find((entry) => entry.url === "/api/v1/investigation-confirmations") };
}

test("review flow posts the exact Phase 6 body and locks after confirmation", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();

  assert.equal(harness.confirmationAction.disabled, false);
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();

  const post = requests.find((request) => request.url === "/api/v1/investigation-confirmations");
  assert.ok(post);
  assert.deepEqual(JSON.parse(post.options.body), {
    reference_frame_resource_id: "resource--10",
    reference_time: "2026-07-20T12:34:18",
    source_timezone: "Asia/Seoul",
    candidate_offset_seconds: -10,
    source_width: 2560,
    source_height: 1440,
    roi: { x: 120, y: 80, width: 240, height: 180, coordinate_space: "source_pixels", provenance: "manual" },
  });
  assert.equal(harness.confirmationResult.hidden, false);
  assert.equal(harness.confirmationAction.disabled, true);
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);
  const postCount = requests.filter((request) => request.url === "/api/v1/investigation-confirmations").length;
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  assert.equal(requests.filter((request) => request.url === "/api/v1/investigation-confirmations").length, postCount);
  assert.match(harness.confirmationArtifact.textContent, /^artifacts\/investigations\//);
  assert.doesNotMatch(harness.confirmationArtifact.textContent, /[A-Za-z]:\\|^\//);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().readOnly, true);
});

test("a late channel refresh cannot unlock or replace a confirmed control", async () => {
  let channelCalls = 0;
  const lateChannelResponse = deferred();
  const channelResponse = () => {
    channelCalls += 1;
    return channelCalls === 1 ? Promise.resolve({
      ok: true,
      json: async () => ({ channels: [{ channel_id: 1, name: "Counter", online: true }], default_channel_id: 1 }),
    }) : lateChannelResponse.promise;
  };
  const harness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  }, channelResponse);
  commitRoi(harness);
  await settle();
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);

  harness.window.vigiVisionReferenceFrameChannels.refresh();
  await settle();
  lateChannelResponse.resolve({
    ok: true,
    json: async () => ({ channels: [{ channel_id: 2, name: "Dining", online: true }], default_channel_id: 2 }),
  });
  for (let index = 0; index < 3; index += 1) {
    await settle();
  }

  assert.equal(harness.channel.disabled, true);
  assert.equal(harness.channel.value, "1");
});

test("a late candidate thumbnail load cannot unlock a confirmed selection", async () => {
  const harness = createHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  }, undefined, { confirmation: true });
  applyReferenceTime(harness);
  await submit(harness);
  const card = harness.results.children[0];
  const image = card.children[0].children[0];
  const radio = findElement(card, "input");
  radio.disabled = false;
  radio.checked = true;
  radio.listeners.change();
  harness.previewImage.rect = { left: 10, top: 20, width: 200, height: 160 };
  harness.previewImage.complete = true;
  harness.previewImage.listeners.load();
  commitRoi(harness);
  for (let index = 0; index < 3; index += 1) {
    await settle();
  }
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);

  image.listeners.load();
  assert.equal(radio.disabled, true);
});

test("a late candidate response cannot cross a changed channel", async () => {
  const response = deferred();
  const harness = createHarness(() => response.promise);
  await settle();
  applyReferenceTime(harness);
  const request = submit(harness);

  harness.channel.value = "2";
  harness.channel.listeners.change();
  response.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  await request;

  assert.equal(harness.results.children.length, 0);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.confirmationAction.disabled, true);
  assert.match(harness.status.textContent, /채널이 변경/);
});

test("a late candidate response cannot cross a dirty time", async () => {
  const response = deferred();
  const harness = createHarness(() => response.promise);
  await settle();
  applyReferenceTime(harness);
  const request = submit(harness);

  harness.referenceTime.value = "2026-07-20T12:35:18";
  harness.referenceTime.listeners.input();
  response.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  await request;

  assert.equal(harness.results.children.length, 0);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.confirmationAction.disabled, true);
  assert.match(harness.status.textContent, /기준 시각이 변경/);
});

test("a late candidate response cannot cross a newly applied time", async () => {
  const response = deferred();
  const harness = createHarness(() => response.promise);
  await settle();
  applyReferenceTime(harness);
  const request = submit(harness);

  harness.referenceTime.value = "2026-07-20T12:35:18";
  harness.referenceTime.listeners.input();
  harness.applyButton.listeners.click();
  response.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  await request;

  assert.equal(harness.results.children.length, 0);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.confirmationAction.disabled, true);
});

test("a late selected-preview error cannot clear a POST-confirmed ROI", async () => {
  const harness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  const before = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;

  harness.previewImage.listeners.error();

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, before);
  assert.equal(harness.committedOverlay.hidden, false);
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);
});

test("a late selected-preview load cannot clear a GET-restored ROI", async () => {
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  await settle();
  const before = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;

  harness.previewImage.listeners.load();

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, before);
  assert.equal(harness.committedOverlay.hidden, false);
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);
});

test("a late selected-preview error cannot clear a GET-restored canonical state", async () => {
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  await settle();
  const beforeRoi = harness.window.vigiVisionReferenceFrameRoi.getState();
  const beforeCandidate = harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate();
  const beforeResult = {
    id: harness.confirmationId.textContent,
    artifact: harness.confirmationArtifact.textContent,
  };

  harness.previewImage.listeners.error();

  const afterRoi = harness.window.vigiVisionReferenceFrameRoi.getState();
  assert.deepEqual(afterRoi.committedRoi, beforeRoi.committedRoi);
  assert.equal(afterRoi.sourceWidth, beforeRoi.sourceWidth);
  assert.equal(afterRoi.sourceHeight, beforeRoi.sourceHeight);
  assert.equal(afterRoi.provenance, beforeRoi.provenance);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), beforeCandidate);
  assert.deepEqual({ id: harness.confirmationId.textContent, artifact: harness.confirmationArtifact.textContent }, beforeResult);
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);
});

test("an old preview callback cannot clear a newer candidate ROI", async () => {
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  const oldErrorCallback = harness.previewImage.handlers.error[0].handler;
  const nextCandidate = candidate(-5);
  harness.window.vigiVisionReferenceFrameRoi.setSelectedCandidate(nextCandidate, harness.previewImage);
  harness.window.vigiVisionReferenceFrameRoi.replaceCommittedRoi(
    { source_width: 2560, source_height: 1440, x: 300, y: 120, width: 200, height: 160 },
    "new candidate ROI",
  );
  const before = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;

  oldErrorCallback();

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, before);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().selectedCandidate, nextCandidate);
});

test("a refresh fallback invalidates a candidate when its channel disappears", async () => {
  let channelCalls = 0;
  const refreshed = deferred();
  const channelResponse = () => {
    channelCalls += 1;
    return channelCalls === 1 ? Promise.resolve({
      ok: true,
      json: async () => ({ channels: [{ channel_id: 1, name: "Counter", online: true }], default_channel_id: 1 }),
    }) : refreshed.promise;
  };
  const harness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  }, channelResponse);
  commitRoi(harness);
  await settle();
  assert.equal(harness.confirmationAction.disabled, false);

  harness.window.vigiVisionReferenceFrameChannels.refresh();
  await settle();
  refreshed.resolve({
    ok: true,
    json: async () => ({ channels: [{ channel_id: 2, name: "Dining", online: true }], default_channel_id: 2 }),
  });
  for (let index = 0; index < 3; index += 1) {
    await settle();
  }

  assert.equal(harness.channel.value, "2");
  assert.equal(harness.confirmationAction.disabled, true);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("programmatic channel refresh removes stale cards and disables their old radios", async () => {
  let channelCalls = 0;
  const refreshed = deferred();
  const channelResponse = () => {
    channelCalls += 1;
    return channelCalls === 1 ? Promise.resolve({
      ok: true,
      json: async () => ({ channels: [{ channel_id: 1, name: "Counter", online: true }], default_channel_id: 1 }),
    }) : refreshed.promise;
  };
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  }, channelResponse);
  const oldCard = harness.results.children[0];
  const oldRadio = findElement(oldCard, "input");

  harness.window.vigiVisionReferenceFrameChannels.refresh();
  await settle();
  refreshed.resolve({
    ok: true,
    json: async () => ({ channels: [{ channel_id: 2, name: "Dining", online: true }], default_channel_id: 2 }),
  });
  await settle();
  await settle();

  assert.equal(harness.channel.value, "2");
  assert.equal(harness.results.children.length, 0);
  assert.equal(oldRadio.disabled, true);
  oldRadio.checked = true;
  oldRadio.listeners.change();
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.confirmationAction.disabled, true);
});

test("retiring a pending channel request settles its busy state and protects a newer request", async () => {
  let channelCalls = 0;
  let candidateCalls = 0;
  const refreshed = deferred();
  const retiredResponse = deferred();
  const currentResponse = deferred();
  const channelResponse = () => {
    channelCalls += 1;
    return channelCalls === 1 ? Promise.resolve({
      ok: true,
      json: async () => ({ channels: [{ channel_id: 1, name: "Counter", online: true }], default_channel_id: 1 }),
    }) : refreshed.promise;
  };
  const harness = createHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    candidateCalls += 1;
    return candidateCalls === 1 ? retiredResponse.promise : currentResponse.promise;
  }, channelResponse, { confirmation: true });
  applyReferenceTime(harness);
  const retiredRequest = submit(harness);
  await settle();
  assert.equal(harness.generationProgress.hidden, false);

  harness.window.vigiVisionReferenceFrameChannels.refresh();
  await settle();
  refreshed.resolve({
    ok: true,
    json: async () => ({ channels: [{ channel_id: 2, name: "Dining", online: true }], default_channel_id: 2 }),
  });
  await settle();
  await settle();
  assert.equal(harness.channel.value, "2");
  assert.equal(harness.generationProgress.hidden, true);
  assert.equal(harness.form.attributes["aria-busy"], "false");

  const currentRequest = submit(harness);
  await settle();
  assert.equal(harness.generationProgress.hidden, false);
  retiredResponse.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  await retiredRequest;
  await settle();
  assert.equal(harness.generationProgress.hidden, false);
  assert.equal(harness.results.children.length, 0);

  currentResponse.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10, "succeeded", 2)]) });
  await currentRequest;
  await settle();
  assert.equal(harness.generationProgress.hidden, true);
  assert.equal(harness.results.children.length, 1);
});

test("changing channel invalidates the previous candidate and prevents a stale confirmation", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  assert.equal(harness.confirmationAction.disabled, false);

  harness.channel.value = "2";
  harness.channel.listeners.change();
  assert.equal(harness.confirmationAction.disabled, true);
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();

  assert.equal(requests.filter((request) => request.url === "/api/v1/investigation-confirmations").length, 0);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("editing the applied time invalidates confirmation readiness immediately", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();

  harness.referenceTime.value = "2026-07-20T12:35:18";
  harness.referenceTime.listeners.input();
  assert.equal(harness.confirmationAction.disabled, true);
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();

  assert.equal(requests.filter((request) => request.url === "/api/v1/investigation-confirmations").length, 0);
});

test("assisted ROI requests hold the confirmation action until the request settles", async () => {
  const suggestion = deferred();
  const harness = await selectedHarness((url, options) => {
    if (url.endsWith("/roi-suggestions")) {
      return suggestion.promise;
    }
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  assert.equal(harness.confirmationAction.disabled, false);

  harness.window.vigiVisionReferenceFrameAssistedRoi.requestTap({ x: 200, y: 100 }, { x: 20, y: 10 });
  await settle();
  assert.equal(harness.window.vigiVisionReferenceFrameAssistedRoi.getState().pending !== null, true);
  assert.equal(harness.confirmationAction.disabled, true);
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(harness.confirmationAction.disabled, true);

  suggestion.resolve({ ok: false, status: 503, json: async () => ({ error: { code: "suggestion_unavailable" } }) });
  await settle();
});

test("an unresolved confirmation POST keeps its operation lock across a refresh", async () => {
  const post = deferred();
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return post.promise;
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(harness.confirmationAction.disabled, true);

  harness.referenceTime.value = "2026-07-20T12:35:18";
  harness.referenceTime.listeners.input();
  await settle();
  assert.equal(harness.confirmationAction.disabled, true);
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  assert.equal(requests.filter((request) => request.url === "/api/v1/investigation-confirmations").length, 1);

  post.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
  await settle();
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);
});

test("keyboard ROI adjustment changes assisted provenance to assisted_then_adjusted", async () => {
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  harness.window.vigiVisionReferenceFrameRoi.replaceCommittedRoi(
    { source_width: 2560, source_height: 1440, x: 120, y: 80, width: 240, height: 180 },
    "assisted ROI",
    "success",
    "assisted",
  );
  harness.roiStage.listeners.keydown({ key: "ArrowRight", shiftKey: false, altKey: false, preventDefault() {} });
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getPhase6Snapshot().provenance, "assisted_then_adjusted");
});

test("keyboard ROI resize also records an assisted adjustment", async () => {
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  harness.window.vigiVisionReferenceFrameRoi.replaceCommittedRoi(
    { source_width: 2560, source_height: 1440, x: 120, y: 80, width: 240, height: 180 },
    "assisted ROI",
    "success",
    "assisted",
  );
  harness.roiStage.listeners.keydown({ key: "ArrowRight", shiftKey: false, altKey: true, preventDefault() {} });
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getPhase6Snapshot().provenance, "assisted_then_adjusted");
});

test("keyboard movement sends assisted_then_adjusted provenance in the POST", async () => {
  const { harness, request } = await postAfterAssistedKeyboardAdjustment({ key: "ArrowRight", shiftKey: false, altKey: false });
  assert.ok(request);
  assert.equal(JSON.parse(request.options.body).roi.provenance, "assisted_then_adjusted");
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getPhase6Snapshot().provenance, "assisted_then_adjusted");
});

test("keyboard resizing sends assisted_then_adjusted provenance in the POST", async () => {
  const { harness, request } = await postAfterAssistedKeyboardAdjustment({ key: "ArrowRight", shiftKey: false, altKey: true });
  assert.ok(request);
  assert.equal(JSON.parse(request.options.body).roi.provenance, "assisted_then_adjusted");
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getPhase6Snapshot().provenance, "assisted_then_adjusted");
});

test("review text includes identity, timing, source dimensions, ROI, and provenance", async () => {
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  assert.match(harness.confirmationReview.textContent, /object-disappearance-v3-ch1/);
  assert.match(harness.confirmationReview.textContent, /resource--10/);
  assert.match(harness.confirmationReview.textContent, /2026-07-20T03:34:08Z/);
  assert.match(harness.confirmationReview.textContent, /2560 × 1440/);
  assert.match(harness.confirmationReview.textContent, /x 120, y 80, width 240, height 180/);
  assert.match(harness.confirmationReview.textContent, /manual/);
});

test("an existing confirmation restores the server ROI through GET without local persistence", async () => {
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  await settle();

  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().phase, "confirmed");
  assert.equal(JSON.stringify(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi), JSON.stringify({
    source_width: 2560, source_height: 1440, x: 120, y: 80, width: 240, height: 180,
  }));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().provenance, "manual");
  assert.equal(harness.confirmationAction.disabled, true);
  assert.equal(harness.confirmationId.textContent, "object-disappearance-ch1-20260720T033418Z");
});

test("a schema 2 confirmation exposes an explicit recording-search reconfirm action", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/reconfirm-for-recording-search")) {
      return Promise.resolve({ ok: true, status: 201, json: async () => schemaThreeConfirmationResponse({ outcome: "created" }) });
    }
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  await settle();

  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().phase, "confirmed");
  assert.equal(harness.confirmationReconfirmAction.hidden, false);
  assert.equal(harness.confirmationReconfirmAction.disabled, false);
  harness.confirmationReconfirmAction.listeners.click({ preventDefault() {} });
  await settle();

  const reconfirmRequest = requests.find((entry) => entry.url.endsWith("/reconfirm-for-recording-search"));
  assert.ok(reconfirmRequest);
  assert.equal(reconfirmRequest.options.method, "POST");
  assert.equal(reconfirmRequest.options.body, "{}");
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().investigationId, "object-disappearance-v3-ch1-20260720T033418Z");
  assert.equal(harness.confirmationReconfirmAction.hidden, true);
  assert.equal(harness.confirmationReconfirmAction.disabled, true);
});

test("a failed schema 2 reconfirm keeps the legacy result and safe retry action", async () => {
  const harness = await selectedHarness((url) => {
    if (url.endsWith("/reconfirm-for-recording-search")) {
      return Promise.resolve({ ok: false, status: 500, json: async () => ({ error: { code: "artifact_failure", message: "C:\\\\private" } }) });
    }
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  await settle();
  harness.confirmationReconfirmAction.listeners.click({ preventDefault() {} });
  await settle();

  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().phase, "error");
  assert.equal(harness.confirmationResult.hidden, false);
  assert.equal(harness.confirmationReconfirmAction.hidden, false);
  assert.equal(harness.confirmationReconfirmAction.disabled, false);
  assert.doesNotMatch(harness.confirmationError.textContent, /private|C:/i);
});

test("repeated schema 2 reconfirm clicks produce one request", async () => {
  const requests = [];
  const pending = deferred();
  const harness = await selectedHarness((url) => {
    if (url.endsWith("/reconfirm-for-recording-search")) {
      requests.push(url);
      return pending.promise;
    }
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  await settle();
  harness.confirmationReconfirmAction.listeners.click({ preventDefault() {} });
  harness.confirmationReconfirmAction.listeners.click({ preventDefault() {} });
  assert.equal(requests.length, 1);
  pending.resolve({ ok: true, status: 201, json: async () => schemaThreeConfirmationResponse() });
  await settle();
});

test("confirmation conflict stays safe and never displays server details", async () => {
  const harness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: false, status: 409, json: async () => ({ error: { code: "confirmation_conflict", message: "C:\\secret\\claims\\token" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  for (let index = 0; index < 5; index += 1) {
    await settle();
  }
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().phase, "conflict");
  assert.match(harness.confirmationStatus.textContent, /이미 다른 선택/);
  assert.doesNotMatch(harness.confirmationStatus.textContent, /secret|claims|token|C:/i);
  assert.equal(harness.confirmationError.hidden, false);
  assert.equal(harness.confirmationError.focused, true);
  assert.equal(harness.confirmationAction.disabled, false);
  assert.equal(harness.confirmationResult.hidden, true);
});

test("a confirmation conflict refreshes and adopts the canonical existing result", async () => {
  const harness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse({ outcome: "reused" }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: false, status: 409, json: async () => ({ error: { code: "confirmation_conflict" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  harness.confirmationAction.listeners.click({ preventDefault() {} });
  for (let index = 0; index < 5; index += 1) {
    await settle();
  }

  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);
  assert.equal(harness.confirmationResult.hidden, false);
  assert.equal(harness.confirmationArtifact.textContent, "artifacts/investigations/object-disappearance-ch1-20260720T033418Z");
});

test("confirmation failures stay in fixed safe categories and focus the actionable error", async () => {
  const cases = [
    ["invalid_confirmation", 422],
    ["invalid_request", 400],
    ["resource_not_found", 404],
    ["stale_selection", 409],
    ["confirmation_in_progress", 409],
    ["artifact_failure", 500],
  ];
  for (const [code, status] of cases) {
    const harness = await selectedHarness((url, options) => {
      if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
        return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
      }
      if (url === "/api/v1/investigation-confirmations") {
        return Promise.resolve({ ok: false, status, json: async () => ({ error: { code, message: "C:\\unsafe\\details" } }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
    });
    commitRoi(harness);
    await settle();
    harness.confirmationAction.listeners.click({ preventDefault() {} });
    await settle();
    assert.equal(harness.confirmationError.hidden, false);
    assert.equal(harness.confirmationError.focused, true);
    assert.doesNotMatch(harness.confirmationError.textContent, /unsafe|details|C:/i);
    assert.equal(harness.confirmationAction.disabled, false);
  }

  const malformed = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: true, status: 201, json: async () => ({}) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(malformed);
  await settle();
  malformed.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(malformed.confirmationError.focused, true);
  assert.doesNotMatch(malformed.confirmationError.textContent, /undefined|object|C:|Error/i);

  const nonJson = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: false, status: 500, json: async () => { throw new Error("raw native failure"); } });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(nonJson);
  await settle();
  nonJson.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(nonJson.confirmationError.focused, true);
  assert.doesNotMatch(nonJson.confirmationError.textContent, /raw|native|failure|Error/i);
});

test("internally inconsistent success responses never become authoritative", async () => {
  const mismatchedCandidate = candidate(-10);
  mismatchedCandidate.candidate_requested_time_utc = "2026-07-20T03:34:09Z";
  const candidateMismatch = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([mismatchedCandidate]) });
  });
  commitRoi(candidateMismatch);
  await settle();
  candidateMismatch.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(candidateMismatch.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);

  const malformedResponses = [
    {
      confirmation: {
        timing: { estimated_source_time_utc: "2026-07-20T03:34:08Z", timing_precision_status: "measured_clip_relative" },
      },
    },
    {
      confirmation: {
        timing: { estimated_source_time_utc: "2026-07-20T03:34:08Z", timing_precision_status: "estimated" },
      },
    },
    {
      confirmation: {
        roi: { x: 120, y: 80, width: 240, height: 180, coordinate_space: "source_pixels", provenance: "manual", source_width: 1 },
      },
    },
    { unexpected: "foreign response field" },
  ];
  for (const overrides of malformedResponses) {
    const harness = await selectedHarness((url, options) => {
      if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
        return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
      }
      if (url === "/api/v1/investigation-confirmations") {
        return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse(overrides) });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
    });
    commitRoi(harness);
    await settle();
    harness.confirmationAction.listeners.click({ preventDefault() {} });
    await settle();
    assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);
    assert.equal(harness.confirmationResult.hidden, true);
    assert.equal(harness.confirmationError.hidden, false);
  }
});

test("malformed or foreign POST success payloads are rejected safely", async () => {
  const cases = [
    ["foreign artifact", { artifact_directory_relative: "artifacts/investigations/other-investigation" }],
    ["foreign investigation", { investigation_id: "object-disappearance-ch2-20260720T033418Z", artifact_directory_relative: "artifacts/investigations/object-disappearance-ch2-20260720T033418Z" }],
    ["missing requested time", { confirmation: { requested_time_utc: undefined } }],
    ["malformed timing", { confirmation: { timing: { estimated_source_time_utc: "not-a-timestamp", timing_precision_status: "measured_clip_relative" } } }],
    ["invalid confirmed time", { confirmed_at_utc: "2026-08-02T03:04:05.123Z" }],
    ["invalid resource", { confirmation: { reference_frame_resource_id: "../foreign" } }],
    ["invalid dimensions", { confirmation: { source_width: 0 } }],
    ["invalid ROI", { confirmation: { roi: { x: -1, y: 80, width: 240, height: 180, coordinate_space: "source_pixels", provenance: "manual" } } }],
  ];
  for (const [label, overrides] of cases) {
    const harness = await selectedHarness((url, options) => {
      if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
        return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
      }
      if (url === "/api/v1/investigation-confirmations") {
        return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse(overrides) });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
    });
    commitRoi(harness);
    await settle();
    harness.confirmationAction.listeners.click({ preventDefault() {} });
    await settle();
    assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, false, label);
    assert.equal(harness.confirmationResult.hidden, true, label);
    assert.equal(harness.confirmationError.hidden, false, label);
    assert.doesNotMatch(harness.confirmationError.textContent, /foreign|other-investigation|not-a-timestamp|2026-08-02T03:04:05\.123|\.\.|Error|undefined/i, label);
  }
});

test("malformed GET and conflict-refresh payloads never become confirmed", async () => {
  const malformedGet = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse({ artifact_directory_relative: "artifacts/investigations/foreign" }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  await settle();
  assert.equal(malformedGet.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);
  assert.equal(malformedGet.confirmationResult.hidden, true);
  assert.equal(malformedGet.confirmationError.hidden, false);

  let conflictGetCalls = 0;
  const malformedConflict = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      conflictGetCalls += 1;
      return conflictGetCalls === 1
        ? Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) })
        : Promise.resolve({ ok: true, status: 200, json: async () => confirmationResponse({ confirmed_at_utc: "not-a-timestamp" }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: false, status: 409, json: async () => ({ error: { code: "confirmation_conflict" } }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(malformedConflict);
  await settle();
  malformedConflict.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(malformedConflict.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);
  assert.equal(malformedConflict.confirmationResult.hidden, true);
});

test("a stale confirmation response cannot overwrite a newer request", async () => {
  const pendingRequests = [];
  const harness = await selectedHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      const pending = deferred();
      pendingRequests.push(pending);
      return pending.promise;
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  harness.referenceTime.value = "2026-07-20T12:35:18";
  harness.referenceTime.listeners.input();
  harness.applyButton.listeners.click();
  pendingRequests[0].resolve({ ok: true, status: 200, json: async () => confirmationResponse() });
  await settle();

  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);
  assert.equal(harness.confirmationResult.hidden, true);
});

test("POST success must match the immutable submitted ROI and provenance", async () => {
  let postCount = 0;
  const harness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      postCount += 1;
      return Promise.resolve({
        ok: true,
        status: 201,
        json: async () => confirmationResponse({
          confirmation: {
            roi: { x: 900, y: 700, width: 100, height: 100, coordinate_space: "source_pixels", provenance: "assisted" },
          },
        }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10)]) });
  });
  commitRoi(harness);
  await settle();
  const submitted = harness.window.vigiVisionReferenceFrameRoi.getPhase6Snapshot();

  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();

  assert.equal(postCount, 1);
  assert.equal(harness.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getPhase6Snapshot(), submitted);
  assert.equal(harness.confirmationResult.hidden, true);
  assert.equal(harness.confirmationError.hidden, false);
  assert.doesNotMatch(harness.confirmationError.textContent, /900|700|assisted|Error/i);
});

test("a selected candidate from another channel is rejected before POST", async () => {
  let postCount = 0;
  const harness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      postCount += 1;
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10, "succeeded", 2)]) });
  });
  commitRoi(harness);
  await settle();

  harness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();

  assert.equal(postCount, 0);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.confirmationAction.disabled, true);
  assert.equal(harness.confirmationError.hidden, false);
});

test("a retired thumbnail load cannot revive or select a previous-channel candidate", async () => {
  let channelCalls = 0;
  const refreshed = deferred();
  const channelResponse = () => {
    channelCalls += 1;
    return channelCalls === 1
      ? Promise.resolve({
        ok: true,
        json: async () => ({ channels: [{ channel_id: 1, name: "Counter", online: true }], default_channel_id: 1 }),
      })
      : refreshed.promise;
  };
  const harness = createHarness((url) => {
    if (url.startsWith("/api/v1/investigation-confirmations/")) {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    const channelId = Number(harness?.channel?.value ?? 1);
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([candidate(-10, "succeeded", channelId)]) });
  }, channelResponse, { confirmation: true });
  applyReferenceTime(harness);
  await submit(harness);
  const oldCard = harness.results.children[0];
  const oldImage = oldCard.children[0].children[0];
  const oldRadio = findElement(oldCard, "input");

  harness.window.vigiVisionReferenceFrameChannels.refresh();
  await settle();
  refreshed.resolve({
    ok: true,
    json: async () => ({ channels: [{ channel_id: 2, name: "Dining", online: true }], default_channel_id: 2 }),
  });
  await settle();
  await settle();
  oldImage.listeners.load();
  oldRadio.checked = true;
  oldRadio.listeners.change();

  assert.equal(harness.channel.value, "2");
  assert.equal(oldRadio.disabled, true);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.results.children.length, 0);

  await submit(harness);
  assert.equal(harness.results.children.length, 1);
  const newCard = harness.results.children[0];
  const newImage = newCard.children[0].children[0];
  const newRadio = findElement(newCard, "input");
  assert.equal(newCard === oldCard, false);
  newImage.listeners.load();
  newRadio.checked = true;
  newRadio.listeners.change();
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate().reference_frame.channel_id, 2);
});

test("decoded timing rejects contradictory offset evidence without banning negative PTS", async () => {
  let invalidPostCount = 0;
  const invalidCandidate = candidate(-10);
  invalidCandidate.reference_frame.timing.decoded_clip_relative_pts_seconds = -99;
  invalidCandidate.reference_frame.timing.offset_from_requested_seconds = 777;
  const invalidHarness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      invalidPostCount += 1;
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([invalidCandidate]) });
  });
  commitRoi(invalidHarness);
  await settle();
  invalidHarness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(invalidPostCount, 0);
  assert.equal(invalidHarness.window.vigiVisionReferenceFrameConfirmation.getState().locked, false);
  assert.equal(invalidHarness.confirmationError.hidden, false);

  const validCandidate = candidate(-10);
  validCandidate.reference_frame.timing.decoded_clip_relative_pts_seconds = -0.04;
  const validHarness = await selectedHarness((url, options) => {
    if (url.startsWith("/api/v1/investigation-confirmations/") && options?.method !== "POST") {
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) });
    }
    if (url === "/api/v1/investigation-confirmations") {
      return Promise.resolve({ ok: true, status: 201, json: async () => confirmationResponse() });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => candidateSet([validCandidate]) });
  });
  commitRoi(validHarness);
  await settle();
  validHarness.confirmationAction.listeners.click({ preventDefault() {} });
  await settle();
  assert.equal(validHarness.window.vigiVisionReferenceFrameConfirmation.getState().locked, true);
});
