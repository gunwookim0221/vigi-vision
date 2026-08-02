const assert = require("node:assert");
const test = require("node:test");
const { activeRoiHarness, emit, pointerEvent, drag } = require("./reference-frame-roi-test-helpers.js");

async function committedHarness() {
  const harness = await activeRoiHarness();
  drag(harness, [30, 40], [150, 120]);
  return harness;
}

function keyEvent(key, options = {}) {
  return {
    key,
    altKey: options.altKey === true,
    shiftKey: options.shiftKey === true,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
  };
}

test("interior pointer drag moves the ROI and clamps it to source bounds", async () => {
  const harness = await committedHarness();
  emit(harness, "pointerdown", pointerEvent(20, "mouse", 90, 80));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activeEdit.mode, "moving");
  emit(harness, "pointermove", pointerEvent(20, "mouse", 120, 100));
  emit(harness, "pointerup", pointerEvent(20, "mouse", 120, 100));

  const moved = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
  assert.deepEqual(moved, { source_width: 2560, source_height: 1440, x: 640, y: 360, width: 1536, height: 720 });

  emit(harness, "pointerdown", pointerEvent(21, "touch", 90, 80));
  emit(harness, "pointermove", pointerEvent(21, "touch", -100, -100));
  emit(harness, "pointerup", pointerEvent(21, "touch", -100, -100));
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, {
    source_width: 2560,
    source_height: 1440,
    x: 0,
    y: 0,
    width: 1536,
    height: 720,
  });
});

test("each visible handle starts its matching resize mode and updates the expected axes", async () => {
  const edits = {
    nw: [[30, 40], [25, 35], "both"],
    n: [[90, 40], [90, 30], "height"],
    ne: [[150, 40], [160, 30], "both"],
    e: [[150, 80], [165, 80], "width"],
    se: [[150, 120], [165, 135], "both"],
    s: [[90, 120], [90, 135], "height"],
    sw: [[30, 120], [25, 135], "both"],
    w: [[30, 80], [25, 80], "width"],
  };
  for (const [handle, [start, end, axes]] of Object.entries(edits)) {
    const harness = await committedHarness();
    const before = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
    emit(harness, "pointerdown", pointerEvent(30, "pen", start[0], start[1]));
    assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().activeEdit, { mode: "resizing", handle });
    emit(harness, "pointermove", pointerEvent(30, "pen", end[0], end[1]));
    emit(harness, "pointerup", pointerEvent(30, "pen", end[0], end[1]));
    const after = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
    assert.equal(after.width !== before.width, axes === "width" || axes === "both");
    assert.equal(after.height !== before.height, axes === "height" || axes === "both");
  }
});

test("clicking or cancelling an edit restores the last committed ROI", async () => {
  const harness = await committedHarness();
  const previous = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
  emit(harness, "pointerdown", pointerEvent(40, "touch", 90, 80));
  emit(harness, "pointerup", pointerEvent(40, "touch", 90, 80));
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, previous);

  emit(harness, "pointerdown", pointerEvent(41, "touch", 90, 80));
  emit(harness, "pointermove", pointerEvent(41, "touch", 120, 100));
  emit(harness, "pointercancel", pointerEvent(41, "touch", 120, 100));
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, previous);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().draftRoi, null);
});

test("Reset ROI preserves the candidate and permits immediate recreation", async () => {
  const harness = await committedHarness();
  assert.equal(harness.resetButton.disabled, false);
  const selected = harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate();
  harness.resetButton.listeners.click();
  assert.equal(harness.resetButton.disabled, true);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), selected);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.roiStatus.dataset.state, "ready");
  assert.match(harness.roiStatus.textContent, /ROI reset/);

  drag(harness, [160, 130], [190, 150], "mouse", 42);
  assert.notEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
});

test("focused keyboard shortcuts move, resize, and reset in source pixels", async () => {
  const harness = await committedHarness();
  const moveOne = keyEvent("ArrowRight");
  harness.roiStage.listeners.keydown(moveOne);
  assert.equal(moveOne.defaultPrevented, true);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.x, 257);

  harness.roiStage.listeners.keydown(keyEvent("ArrowDown", { shiftKey: true }));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.y, 190);
  harness.roiStage.listeners.keydown(keyEvent("ArrowRight", { altKey: true }));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.width, 1537);
  harness.roiStage.listeners.keydown(keyEvent("ArrowDown", { altKey: true, shiftKey: true }));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi.height, 730);

  const reset = keyEvent("Delete");
  harness.roiStage.listeners.keydown(reset);
  assert.equal(reset.defaultPrevented, true);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.notEqual(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
});

test("Escape and candidate lifecycle replacement cannot leak old pointer edits", async () => {
  const harness = await committedHarness();
  const previous = harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi;
  emit(harness, "pointerdown", pointerEvent(50, "mouse", 90, 80));
  const escape = keyEvent("Escape");
  harness.roiStage.listeners.keydown(escape);
  assert.equal(escape.defaultPrevented, true);
  assert.deepEqual(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, previous);

  emit(harness, "pointerdown", pointerEvent(51, "mouse", 90, 80));
  harness.window.vigiVisionReferenceFrameRoi.reset("Candidate results replaced.");
  emit(harness, "pointerup", pointerEvent(51, "mouse", 150, 100));
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().committedRoi, null);
  assert.equal(harness.window.vigiVisionReferenceFrameRoi.getState().activePointerId, null);
});

test("Phase 6 snapshot is source-space, candidate-bound, and immutable", async () => {
  const harness = await committedHarness();
  const api = harness.window.vigiVisionReferenceFrameRoi;
  const snapshot = api.getPhase6Snapshot();
  assert.deepEqual(snapshot, {
    candidateId: "resource-0",
    sourceWidth: 2560,
    sourceHeight: 1440,
    roi: { x: 256, y: 180, width: 1536, height: 720 },
  });
  snapshot.roi.x = 0;
  assert.equal(api.getPhase6Snapshot().roi.x, 256);
  api.clearRoi();
  assert.equal(api.getPhase6Snapshot(), null);
});
