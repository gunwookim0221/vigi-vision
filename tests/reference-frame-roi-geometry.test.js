const assert = require("node:assert");
const test = require("node:test");
const { createHarness } = require("./reference-frame-ui-harness.js");

function geometry() {
  return createHarness(() => Promise.reject(new Error("geometry must not fetch"))).window.vigiVisionReferenceFrameRoi;
}

test("converts displayed CSS coordinates to source pixels with deterministic rounding", () => {
  const { pointToSource } = geometry();
  const display = { left: 10, top: 20, width: 200, height: 100 };

  assert.deepEqual(pointToSource({ clientX: 111, clientY: 70 }, display, 100, 50), { x: 51, y: 25 });
  assert.deepEqual(pointToSource({ clientX: 110.9, clientY: 69.9 }, display, 100, 50), { x: 50, y: 25 });
});

test("normalizes every drag direction into one bounded source rectangle", () => {
  const { normalizeRoi } = geometry();
  const directions = [
    [{ x: 10, y: 12 }, { x: 70, y: 52 }],
    [{ x: 70, y: 52 }, { x: 10, y: 12 }],
    [{ x: 70, y: 12 }, { x: 10, y: 52 }],
    [{ x: 10, y: 52 }, { x: 70, y: 12 }],
  ];

  directions.forEach(([start, end]) => {
    assert.deepEqual(normalizeRoi(start, end, 100, 80), {
      source_width: 100,
      source_height: 80,
      x: 10,
      y: 12,
      width: 60,
      height: 40,
    });
  });
});

test("clamps all endpoint boundaries before source conversion", () => {
  const { pointToSource } = geometry();
  const display = { left: 10, top: 20, width: 200, height: 160 };

  assert.deepEqual(pointToSource({ clientX: -100, clientY: -100 }, display, 100, 80), { x: 0, y: 0 });
  assert.deepEqual(pointToSource({ clientX: 400, clientY: 400 }, display, 100, 80), { x: 100, y: 80 });
});

test("recalculates overlay geometry from canonical pixels after display resize", () => {
  const { sourceRoiToDisplay } = geometry();
  const roi = { source_width: 100, source_height: 80, x: 10, y: 20, width: 40, height: 24 };

  assert.deepEqual(sourceRoiToDisplay(roi, { width: 200, height: 160 }), {
    left: 20,
    top: 40,
    width: 80,
    height: 48,
  });
  assert.deepEqual(sourceRoiToDisplay(roi, { width: 400, height: 320 }), {
    left: 40,
    top: 80,
    width: 160,
    height: 96,
  });
});

test("moves a canonical ROI while preserving size and clamping to all bounds", () => {
  const { moveRoi } = geometry();
  const roi = { source_width: 100, source_height: 80, x: 20, y: 15, width: 30, height: 20 };

  assert.deepEqual(moveRoi(roi, { x: 10, y: 5 }), { ...roi, x: 30, y: 20 });
  assert.deepEqual(moveRoi(roi, { x: -100, y: -100 }), { ...roi, x: 0, y: 0 });
  assert.deepEqual(moveRoi(roi, { x: 100, y: 100 }), { ...roi, x: 70, y: 60 });
});

test("resizes every edge and corner from the opposite anchored boundary", () => {
  const { resizeRoi } = geometry();
  const roi = { source_width: 100, source_height: 80, x: 20, y: 20, width: 40, height: 30 };
  const cases = {
    nw: [{ x: 10, y: 12 }, { x: 10, y: 12, width: 50, height: 38 }],
    n: [{ x: 45, y: 5 }, { x: 20, y: 5, width: 40, height: 45 }],
    ne: [{ x: 80, y: 10 }, { x: 20, y: 10, width: 60, height: 40 }],
    e: [{ x: 75, y: 35 }, { x: 20, y: 20, width: 55, height: 30 }],
    se: [{ x: 90, y: 70 }, { x: 20, y: 20, width: 70, height: 50 }],
    s: [{ x: 45, y: 65 }, { x: 20, y: 20, width: 40, height: 45 }],
    sw: [{ x: 5, y: 70 }, { x: 5, y: 20, width: 55, height: 50 }],
    w: [{ x: 8, y: 35 }, { x: 8, y: 20, width: 52, height: 30 }],
  };

  Object.entries(cases).forEach(([handle, [point, expected]]) => {
    assert.deepEqual(resizeRoi(roi, handle, point), {
      source_width: 100,
      source_height: 80,
      ...expected,
    });
  });
});

test("resize crossover clamps at the minimum source size without flipping handles", () => {
  const { resizeRoi } = geometry();
  const roi = { source_width: 100, source_height: 80, x: 20, y: 20, width: 40, height: 30 };

  assert.deepEqual(resizeRoi(roi, "e", { x: 0, y: 35 }), {
    source_width: 100,
    source_height: 80,
    x: 20,
    y: 20,
    width: 4,
    height: 30,
  });
  assert.deepEqual(resizeRoi(roi, "nw", { x: 95, y: 75 }), {
    source_width: 100,
    source_height: 80,
    x: 56,
    y: 46,
    width: 4,
    height: 4,
  });
});

test("handle hit testing prefers corners and does not treat the ROI interior as a handle", () => {
  const { pointInRoi, resizeHandleAt } = geometry();
  const roi = { source_width: 100, source_height: 80, x: 20, y: 20, width: 40, height: 30 };

  assert.equal(pointInRoi({ x: 40, y: 30 }, roi), true);
  assert.equal(resizeHandleAt({ x: 20, y: 20 }, roi, 3), "nw");
  assert.equal(resizeHandleAt({ x: 40, y: 20 }, roi, 3), "n");
  assert.equal(resizeHandleAt({ x: 40, y: 35 }, roi, 3), null);
});
