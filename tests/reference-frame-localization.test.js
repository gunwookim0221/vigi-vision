const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const page = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "index.html"), "utf8");

test("reference-frame page exposes Korean labels and accessible ROI guidance", () => {
  assert.match(page, /<html lang="ko">/);
  for (const text of [
    "요청한 프레임 후보 생성",
    "기준 날짜 및 시각",
    "원본 시간대",
    "후보 생성",
    "ROI 작업 영역",
    "ROI 자동 제안",
    "ROI 초기화",
    "ROI 편집기",
    "ROI 북서쪽 크기 조정",
  ]) {
    assert.match(page, new RegExp(text));
  }
  assert.doesNotMatch(page, /Channel ID|Generate candidates|Source timezone|ROI workspace/);
});

test("localization keeps timezone values and machine DOM contracts unchanged", () => {
  assert.match(page, /id="channel-id"/);
  assert.match(page, /id="source-timezone"/);
  assert.match(page, /value="Asia\/Seoul"/);
  assert.match(page, /value="UTC"/);
  assert.match(page, /id="reference-time"/);
  assert.match(page, /id="generate-button"/);
});
