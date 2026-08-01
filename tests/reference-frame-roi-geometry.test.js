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
