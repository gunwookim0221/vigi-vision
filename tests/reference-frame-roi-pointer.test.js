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
const { activeRoiHarness, drag, emit, pointerEvent } = require("./reference-frame-roi-test-helpers.js");

test("ROI is inactive before a candidate is selected", () => {
  const harness = createHarness(() => Promise.reject(new Error("inactive ROI must not fetch")));
  const event = pointerEvent(1, "touch", 20, 30);

  emit(harness, "pointerdown", event);

  assert.equal(harness.roiStage.hidden, true);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);
  assert.equal(event.defaultPrevented, false);
});

test("selecting a candidate activates the ROI workspace and mouse drag commits source pixels", async () => {
  const harness = await activeRoiHarness();

  assert.equal(harness.roiStage.hidden, false);
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().sourceSize, { width: 2560, height: 1440 });
  drag(harness, [30, 40], [150, 120]);

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, {
    source_width: 2560,
    source_height: 1440,
    x: 256,
    y: 180,
    width: 1536,
    height: 720,
  });
  assert.equal(harness.roiSummary.hidden, false);
  assert.equal(harness.roiSummaryX.textContent, "256");
  assert.equal(harness.roiSummaryY.textContent, "180");
  assert.equal(harness.roiSummaryWidth.textContent, "1536");
  assert.equal(harness.roiSummaryHeight.textContent, "720");
  assert.match(harness.roiStatus.textContent, /original-image pixels/);
  assert.equal(harness.roiStatus.dataset.state, "success");
  assert.equal(harness.committedOverlay.textContent, "");
  assert.equal(harness.roiStage.capturedPointers.size, 0);
});

test("all drag directions normalize to the same ROI", async () => {
  const directions = [
    [[30, 40], [150, 120]],
    [[150, 120], [30, 40]],
    [[150, 40], [30, 120]],
    [[30, 120], [150, 40]],
  ];
  for (const [start, end] of directions) {
    const harness = await activeRoiHarness();
    drag(harness, start, end);
    const roi = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
    assert.deepEqual(roi, { source_width: 2560, source_height: 1440, x: 256, y: 180, width: 1536, height: 720 });
  }
});

test("touch and pen share the same pointer path", async () => {
  const touchHarness = await activeRoiHarness();
  const touchStart = pointerEvent(5, "touch", 30, 40);
  emit(touchHarness, "pointerdown", touchStart);
  emit(touchHarness, "pointermove", pointerEvent(5, "touch", 150, 120));
  const touchEnd = emit(touchHarness, "pointerup", pointerEvent(5, "touch", 150, 120));
  assert.equal(touchStart.defaultPrevented, true);
  assert.equal(touchEnd.defaultPrevented, true);
  assert.equal(touchHarness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.width, 1536);

  const penHarness = await activeRoiHarness();
  drag(penHarness, [150, 120], [30, 40], "pen", 7);
  assert.equal(penHarness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.height, 720);
});

test("only one active pointer is accepted and non-primary mouse buttons are ignored", async () => {
  const harness = await activeRoiHarness();
  emit(harness, "pointerdown", pointerEvent(1, "mouse", 30, 40, 2));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);

  emit(harness, "pointerdown", pointerEvent(1, "touch", 30, 40));
  emit(harness, "pointerdown", pointerEvent(2, "touch", 50, 60));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, 1);
  emit(harness, "pointerup", pointerEvent(2, "touch", 50, 60));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, 1);
  emit(harness, "pointercancel", pointerEvent(1, "touch", 30, 40));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);
});

test("endpoints outside the image clamp to source bounds", async () => {
  const harness = await activeRoiHarness();

  drag(harness, [-100, -100], [500, 500]);

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, {
    source_width: 2560,
    source_height: 1440,
    x: 0,
    y: 0,
    width: 2560,
    height: 1440,
  });
});

test("tiny drags and mobile taps are rejected while preserving a previous ROI", async () => {
  const harness = await activeRoiHarness();
  drag(harness, [30, 40], [150, 120]);
  const previous = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;

  drag(harness, [30, 40], [30.1, 40.1], "touch", 8);

  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, previous);
  assert.match(harness.roiStatus.textContent, /too small/);
  assert.equal(harness.roiStatus.dataset.state, "warning");
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);
});

test("pointer cancel and lost capture clear draft state without leaking active pointers", async () => {
  const harness = await activeRoiHarness();
  emit(harness, "pointerdown", pointerEvent(11, "touch", 30, 40));
  emit(harness, "pointermove", pointerEvent(11, "touch", 150, 120));
  emit(harness, "pointercancel", pointerEvent(11, "touch", 150, 120));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().draftRoi, null);
  assert.equal(harness.roiStage.capturedPointers.size, 0);

  drag(harness, [30, 40], [150, 120]);
  emit(harness, "pointerdown", pointerEvent(12, "pen", 50, 60));
  emit(harness, "pointermove", pointerEvent(12, "pen", 100, 100));
  emit(harness, "lostpointercapture", pointerEvent(12, "pen", 100, 100));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);
  assert.notEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("candidate replacement and new results clear the ROI", async () => {
  const harness = await activeRoiHarness();
  drag(harness, [30, 40], [150, 120]);
  const candidate = { reference_frame: { image: { width: 2560, height: 1440 } } };
  harness.window.vigiVisionReferenceFrameRoi.setSelectedCandidate(candidate, harness.previewImage);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.roiSummary.hidden, true);

  const secondHarness = await activeRoiHarness();
  drag(secondHarness, [30, 40], [150, 120]);
  assert.notEqual(secondHarness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  secondHarness.window.vigiVisionReferenceFrameRoi.reset("Results were replaced.");
  assert.equal(secondHarness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("replacing candidate results through the existing request path clears ROI state", async () => {
  const requests = [];
  const harness = createHarness(() => {
    const request = deferred();
    requests.push(request);
    return request.promise;
  });
  applyReferenceTime(harness);

  const firstRequest = submit(harness);
  requests[0].resolve({ ok: true, json: async () => candidateSet([candidate(0)]) });
  await firstRequest;
  const card = harness.results.children[0];
  const thumbnail = card.children[0].children[0];
  const radio = findElement(card, "input");
  thumbnail.listeners.load();
  radio.checked = true;
  radio.listeners.change();
  harness.previewImage.rect = { left: 10, top: 20, width: 200, height: 160 };
  harness.previewImage.complete = true;
  harness.previewImage.listeners.load();
  drag(harness, [30, 40], [150, 120]);
  assert.notEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);

  const secondRequest = submit(harness);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.roiSummary.hidden, true);
  requests[1].resolve({ ok: true, json: async () => candidateSet([candidate(10)]) });
  await secondRequest;
});

test("committed overlay rerenders from source pixels after display resize", async () => {
  const harness = await activeRoiHarness();
  drag(harness, [30, 40], [150, 120]);
  assert.equal(harness.committedOverlay.style.width, "120px");
  assert.equal(harness.committedOverlay.style.height, "80px");

  harness.previewImage.rect = { left: 10, top: 20, width: 400, height: 320 };
  harness.windowListeners.resize();

  assert.equal(harness.committedOverlay.style.width, "240px");
  assert.equal(harness.committedOverlay.style.height, "160px");
});
