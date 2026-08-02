const assert = require("node:assert");
const test = require("node:test");
const {
  applyReferenceTime,
  candidate,
  candidateSet,
  createHarness,
  deferred,
  findElement,
  submit,
} = require("./reference-frame-ui-harness.js");
const { drag, emit, pointerEvent } = require("./reference-frame-roi-test-helpers.js");

function suggestion(resourceId, bbox = { x: 1200, y: 600, width: 240, height: 180 }) {
  return {
    resource_id: resourceId,
    source_width: 2560,
    source_height: 1440,
    bbox,
    mask_preview: maskPreview(bbox),
  };
}

function maskPreview(bbox) {
  return {
    width: 2560,
    height: 1440,
    rows: Array.from({ length: 1440 }, (_, y) => (
      y >= bbox.y && y < bbox.y + bbox.height
        ? [[bbox.x, bbox.x + bbox.width]]
        : []
    )),
  };
}

function irregularSuggestion(resourceId) {
  const rows = Array.from({ length: 1440 }, () => []);
  rows[600] = [[1200, 1400]];
  rows[601] = [[1200, 1300], [1350, 1400]];
  rows[602] = [[1250, 1400]];
  rows[603] = [[1200, 1400]];
  return {
    resource_id: resourceId,
    source_width: 2560,
    source_height: 1440,
    bbox: { x: 1200, y: 600, width: 200, height: 4 },
    mask_preview: { width: 2560, height: 1440, rows },
  };
}

async function selectedHarness(fetchImplementation) {
  const harness = createHarness(fetchImplementation);
  applyReferenceTime(harness);
  await submit(harness);
  const card = harness.results.children[0];
  const thumbnail = card.children[0].children[0];
  const radio = findElement(card, "input");
  thumbnail.listeners.load();
  radio.checked = true;
  radio.listeners.change();
  harness.previewImage.rect = { left: 10, top: 20, width: 200, height: 160 };
  harness.previewImage.complete = true;
  harness.previewImage.listeners.load();
  return harness;
}

function activate(harness) {
  harness.assistedButton.listeners.click();
}

function tap(harness, clientX, clientY, pointerId = 1, pointerType = "touch") {
  emit(harness, "pointerdown", pointerEvent(pointerId, pointerType, clientX, clientY));
  emit(harness, "pointerup", pointerEvent(pointerId, pointerType, clientX, clientY));
}

function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

test("assisted mode is inactive by default and ordinary image taps do not request", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) });
  });

  tap(harness, 110, 100);

  assert.equal(requests.length, 1);
  assert.equal(harness.assistedMarker.hidden, true);
  assert.equal(harness.window.vigiVisionReferenceFrameAssistedRoi.getState().active, false);
});

test("the assisted control requires a selected usable candidate and exposes guidance", async () => {
  const harness = createHarness(async () => ({ ok: true, json: async () => candidateSet([]) }));

  assert.equal(harness.assistedButton.disabled, true);

  const selected = await selectedHarness(async () => ({ ok: true, json: async () => candidateSet([candidate(0)]) }));
  activate(selected);

  assert.equal(selected.assistedButton.disabled, false);
  assert.equal(selected.assistedButton.attributes["aria-pressed"], "true");
  assert.equal(selected.assistedButton.attributes["aria-label"], "ROI 자동 제안 취소");
  assert.match(selected.assistedGuidance.textContent, /대상을 눌러 ROI 자동 제안을 요청/);
  assert.match(selected.roiStatus.textContent, /대상을 눌러 ROI 자동 제안을 요청/);
  assert.equal(selected.roiStatus.dataset.state, "active");
  assert.equal(selected.roiStatus.attributes["aria-busy"], "false");
});

test("one assisted tap posts the selected resource and an intrinsic source-space point", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
      : new Promise(() => {});
  });
  activate(harness);

  tap(harness, 110, 100);

  assert.equal(requests.length, 2);
  assert.equal(requests[1].url, "/api/v1/reference-frames/resource-0/roi-suggestions");
  assert.deepEqual(JSON.parse(requests[1].options.body), { point: { x: 1280, y: 720 } });
  assert.equal(requests[1].options.signal.aborted, false);
  assert.equal(harness.assistedMarker.hidden, false);
  assert.match(harness.roiStatus.textContent, /ROI 자동 제안을 요청하는 중/);
  assert.equal(harness.roiStatus.dataset.state, "loading");
  assert.equal(harness.roiStatus.attributes["aria-busy"], "true");
});

test("an image-boundary tap is bounded, while an outside tap is ignored", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
      : new Promise(() => {});
  });
  activate(harness);

  tap(harness, 10, 20);
  assert.deepEqual(JSON.parse(requests[1].options.body), { point: { x: 0, y: 0 } });
  harness.assistedButton.listeners.click();
  activate(harness);
  tap(harness, 9, 19, 2);

  assert.equal(requests.length, 2);
});

test("a current response enters the canonical ROI and remains manually editable", async () => {
  const requests = [];
  const response = deferred();
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
      : response.promise;
  });
  activate(harness);
  tap(harness, 110, 100);
  response.resolve({ ok: true, json: async () => suggestion("resource-0") });
  await settle();

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, {
    source_width: 2560,
    source_height: 1440,
    x: 1200,
    y: 600,
    width: 240,
    height: 180,
  });
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getPhase6Snapshot(), {
    candidateId: "resource-0",
    sourceWidth: 2560,
    sourceHeight: 1440,
    roi: { x: 1200, y: 600, width: 240, height: 180 },
  });
  assert.match(harness.roiStatus.textContent, /ROI 자동 제안을 받았습니다\. 확인하고 수동 조정/);
  assert.equal(harness.roiStatus.dataset.state, "success");
  assert.equal(harness.roiStatus.attributes["aria-busy"], "false");
  assert.equal(harness.committedOverlay.textContent, "");
  assert.equal(harness.assistedMarker.hidden, true);
  assert.equal(harness.assistedMask.hidden, false);
  assert.equal(harness.assistedMask.width, 2560);
  assert.equal(harness.assistedMask.height, 1440);
  assert.ok(harness.assistedMask.canvasOperations.some((operation) => operation[0] === "fillRect"));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);

  drag(harness, [110, 95], [120, 105]);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.x, 1328);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.y, 690);
  assert.equal(harness.assistedMask.hidden, true);
});

test("the silhouette follows responsive image scaling and reset clears preview state", async () => {
  const harness = await selectedHarness((url, options) => (
    url.includes("roi-suggestions")
      ? Promise.resolve({ ok: true, json: async () => suggestion("resource-0") })
      : Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
  ));
  activate(harness);
  tap(harness, 110, 100);
  await settle();

  harness.previewImage.rect = { left: 10, top: 20, width: 400, height: 225 };
  harness.windowListeners.resize();
  assert.equal(harness.assistedMask.style.width, "400px");
  assert.equal(harness.assistedMask.style.height, "225px");

  harness.resetButton.listeners.click();
  assert.equal(harness.assistedMask.hidden, true);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().assistedPreviewActive, false);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.roiStatus.dataset.state, "ready");
  assert.match(harness.roiStatus.textContent, /ROI를 초기화했습니다/);
});

test("an irregular mask renders exposed row edges instead of a rectangle outline", async () => {
  const harness = await selectedHarness((url) => (
    url.includes("roi-suggestions")
      ? Promise.resolve({ ok: true, json: async () => irregularSuggestion("resource-0") })
      : Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
  ));
  activate(harness);
  tap(harness, 110, 87);
  await settle();

  assert.equal(harness.assistedMask.hidden, false);
  assert.ok(harness.assistedMask.canvasOperations.some((operation) => (
    operation[0] === "fillRect"
      && operation[1] === 1300
      && operation[2] === 587
      && operation[3] === 50
      && operation[4] > 1
  )));
});

test("pending, failed, and timed-out suggestions preserve the previous ROI", async () => {
  const requests = [];
  const response = deferred();
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1 ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) }) : response.promise;
  });
  harness.window.vigiVisionReferenceFrameRoi.replaceCommittedRoi(
    { source_width: 2560, source_height: 1440, x: 20, y: 30, width: 80, height: 60 },
    "Previous ROI committed.",
  );
  const previous = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
  activate(harness);
  tap(harness, 110, 100);
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, previous);

  response.resolve({ ok: false, json: async () => ({ error: { code: "suggestion_timeout", message: "secret stderr" } }) });
  await settle();

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, previous);
  assert.match(harness.roiStatus.textContent, /제안 생성 시간이 너무 오래/);
  assert.equal(harness.roiStatus.dataset.state, "error");
  assert.doesNotMatch(harness.roiStatus.textContent, /secret stderr|checkpoint|ffmpeg|rtsp/i);
  assert.equal(harness.assistedMarker.hidden, true);
  assert.equal(harness.window.vigiVisionReferenceFrameAssistedRoi.getState().active, false);
  assert.equal(harness.assistedButton.textContent, "ROI 자동 제안");

  drag(harness, [110, 100], [120, 105]);
  assert.notDeepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, previous);
});

test("unavailable assisted selection is identified without removing manual fallback", async () => {
  const harness = await selectedHarness((url) => (
    url.includes("roi-suggestions")
      ? Promise.resolve({ ok: false, json: async () => ({ error: { code: "suggestion_unavailable" } }) })
      : Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
  ));
  activate(harness);
  tap(harness, 110, 100);
  await settle();

  assert.equal(harness.roiStatus.dataset.state, "unavailable");
  assert.match(harness.roiStatus.textContent, /ROI 자동 제안을 사용할 수 없습니다/);
  assert.equal(harness.assistedMask.hidden, true);
  drag(harness, [110, 100], [120, 105]);
  assert.notEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("an assisted failure removes the old silhouette while keeping manual fallback", async () => {
  let suggestionCount = 0;
  const harness = await selectedHarness((url) => {
    if (!url.includes("roi-suggestions")) {
      return Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) });
    }
    suggestionCount += 1;
    return suggestionCount === 1
      ? Promise.resolve({ ok: true, json: async () => suggestion("resource-0") })
      : Promise.resolve({ ok: false, json: async () => ({ error: { code: "suggestion_failure" } }) });
  });
  activate(harness);
  tap(harness, 110, 100);
  await settle();
  assert.equal(harness.assistedMask.hidden, false);

  activate(harness);
  tap(harness, 150, 140, 2);
  await settle();
  assert.equal(harness.assistedMask.hidden, true);
  assert.equal(harness.window.vigiVisionReferenceFrameAssistedRoi.getState().active, false);
  assert.equal(harness.roiStatus.dataset.state, "error");
  drag(harness, [110, 100], [120, 105]);
  assert.notEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("button cancellation clears a stale silhouette while retaining the canonical ROI", async () => {
  const harness = await selectedHarness((url) => (
    url.includes("roi-suggestions")
      ? Promise.resolve({ ok: true, json: async () => suggestion("resource-0") })
      : Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
  ));
  activate(harness);
  tap(harness, 110, 100);
  await settle();
  assert.equal(harness.assistedMask.hidden, false);

  activate(harness);
  harness.assistedButton.listeners.click();

  assert.equal(harness.assistedMask.hidden, true);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().assistedPreviewActive, false);
  assert.notEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("Reset ROI aborts pending assisted work without clearing the selected candidate", async () => {
  const requests = [];
  const response = deferred();
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
      : response.promise;
  });
  activate(harness);
  tap(harness, 110, 100);
  assert.equal(requests.length, 2);

  harness.resetButton.listeners.click();

  assert.equal(requests[1].options.signal.aborted, true);
  assert.notEqual(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.window.vigiVisionReferenceFrameAssistedRoi.getState().active, false);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.assistedMask.hidden, true);

  response.resolve({ ok: true, json: async () => suggestion("resource-0") });
  await settle();
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.roiStatus.dataset.state, "ready");
  assert.match(harness.roiStatus.textContent, /ROI를 초기화했습니다/);
});

test("malformed, mismatched, and out-of-bounds responses fail safely", async () => {
  const oversized = suggestion("resource-0");
  oversized.mask_preview.rows[600] = Array.from({ length: 50001 }, () => [1200, 1201]);
  const responses = [
    { resource_id: "other-resource", source_width: 2560, source_height: 1440, bbox: { x: 1, y: 1, width: 40, height: 40 }, mask_preview: maskPreview({ x: 1, y: 1, width: 40, height: 40 }) },
    { resource_id: "resource-0", source_width: 1280, source_height: 720, bbox: { x: 1, y: 1, width: 40, height: 40 }, mask_preview: maskPreview({ x: 1, y: 1, width: 40, height: 40 }) },
    { resource_id: "resource-0", source_width: 2560, source_height: 1440, bbox: { x: 1200, y: 700, width: 1400, height: 800 } },
    oversized,
  ];
  for (const invalid of responses) {
    const requests = [];
    const harness = await selectedHarness((url, options) => {
      requests.push({ url, options });
      return requests.length === 1
        ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
        : Promise.resolve({ ok: true, json: async () => invalid });
    });
    activate(harness);
    tap(harness, 110, 100);
    await settle();
    assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
    assert.match(harness.roiStatus.textContent, /안전하게 적용할 수 없습니다|대상을 다시 누르/);
  }
});

test("a newer tap aborts the older request and only its current response can replace the ROI", async () => {
  const requests = [];
  const first = deferred();
  const second = deferred();
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    if (requests.length === 1) return Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) });
    return requests.length === 2 ? first.promise : second.promise;
  });
  activate(harness);
  tap(harness, 110, 100);
  tap(harness, 150, 140, 2);

  assert.equal(requests[1].options.signal.aborted, true);
  first.resolve({ ok: true, json: async () => suggestion("resource-0", { x: 100, y: 100, width: 100, height: 100 }) });
  second.resolve({ ok: true, json: async () => suggestion("resource-0", { x: 1700, y: 1000, width: 220, height: 160 }) });
  await settle();

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, {
    source_width: 2560,
    source_height: 1440,
    x: 1700,
    y: 1000,
    width: 220,
    height: 160,
  });
  assert.equal(harness.assistedMask.hidden, false);
  assert.ok(harness.assistedMask.canvasOperations.some((operation) => operation[1] === 1700));
  assert.equal(harness.roiStatus.dataset.state, "success");
});

test("candidate change and pointer cancellation invalidate pending assisted work", async () => {
  const requests = [];
  const pending = new Promise(() => {});
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1 ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) }) : pending;
  });
  activate(harness);
  const down = pointerEvent(3, "touch", 110, 100);
  emit(harness, "pointerdown", down);
  emit(harness, "pointercancel", pointerEvent(3, "touch", 110, 100));
  assert.equal(requests.length, 1);
  assert.equal(harness.assistedMarker.hidden, true);

  tap(harness, 110, 100, 4);
  assert.equal(requests.length, 2);
  harness.window.vigiVisionReferenceFrameAssistedRoi.reset("Candidate changed.");
  assert.equal(requests[1].options.signal.aborted, true);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("manual keyboard edits invalidate pending assisted work", async () => {
  const requests = [];
  const response = deferred();
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
      : response.promise;
  });
  harness.window.vigiVisionReferenceFrameRoi.replaceCommittedRoi(
    { source_width: 2560, source_height: 1440, x: 20, y: 30, width: 80, height: 60 },
    "Previous ROI committed.",
  );
  activate(harness);
  tap(harness, 110, 100);
  assert.equal(requests.length, 2);

  harness.roiStage.listeners.keydown({
    altKey: false,
    key: "ArrowRight",
    shiftKey: false,
    preventDefault() {},
  });
  const manualRoi = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
  assert.equal(requests[1].options.signal.aborted, true);

  response.resolve({ ok: true, json: async () => suggestion("resource-0") });
  await settle();
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, manualRoi);
  assert.equal(harness.assistedMask.hidden, true);
});

test("non-primary and non-left-button pointers do not issue assisted requests", async () => {
  const requests = [];
  const harness = await selectedHarness((url, options) => {
    requests.push({ url, options });
    return requests.length === 1
      ? Promise.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) })
      : new Promise(() => {});
  });
  activate(harness);

  emit(harness, "pointerdown", pointerEvent(10, "pen", 110, 100, 0, false));
  emit(harness, "pointerup", pointerEvent(10, "pen", 110, 100, 0, false));
  emit(harness, "pointerdown", pointerEvent(11, "pen", 110, 100, 2, true));
  emit(harness, "pointerup", pointerEvent(11, "pen", 110, 100, 2, true));
  assert.equal(requests.length, 1);
  assert.equal(harness.assistedMarker.hidden, true);
});
