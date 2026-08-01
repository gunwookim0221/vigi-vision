const assert = require("node:assert/strict");
const test = require("node:test");
const { applyReferenceTime, candidate, candidateSet, createHarness, deferred, findElement, headingTexts, submit, textOf } = require("./reference-frame-ui-harness.js");

test("renders successful thumbnails in API order and exposes backend timing", async () => {
  const data = candidateSet([-10, 0, 10].map((offset) => candidate(offset)));
  const harness = createHarness(async () => ({ ok: true, json: async () => data }));
  applyReferenceTime(harness);

  const request = submit(harness);

  assert.equal(harness.status.dataset.state, "loading");
  assert.equal(harness.button.disabled, true);
  await request;

  assert.deepEqual(headingTexts(harness.results), ["-10 sec", "Reference", "+10 sec"]);
  assert.equal(harness.status.dataset.state, "success");
  assert.deepEqual(
    harness.results.children.map((card) => card.children[0].children[0].src),
    [
      "/api/v1/reference-frames/frame--10/image",
      "/api/v1/reference-frames/frame-0/image",
      "/api/v1/reference-frames/frame-10/image",
    ],
  );
  assert.equal(harness.results.children[0].children[0].children[0].alt, "Recorded frame candidate at -10 sec.");
  assert.equal(harness.results.children[0].children[0].children[1].hidden, true);
  assert.match(textOf(harness.results.children[0].children[1]), /Exact source timestamp is not yet verified/);
  assert.doesNotMatch(textOf(harness.results.children[0].children[1]), /pending real-NVR replay validation/);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.previewContent.hidden, true);
});

test("keeps failed candidates in place and falls back when an image load fails", async () => {
  const data = candidateSet([candidate(-10), candidate(0, "failed"), candidate(10)]);
  const harness = createHarness(async () => ({ ok: true, json: async () => data }));
  applyReferenceTime(harness);

  await submit(harness);

  assert.deepEqual(headingTexts(harness.results), ["-10 sec", "Reference", "+10 sec"]);
  assert.equal(harness.results.children[1].children[0].children[0].textContent, "Preview unavailable for this candidate.");
  assert.equal(textOf(harness.results.children[1].children[1].children[1].children[3]), "Failure: recording_unavailableNo recording is available for this position.");
  const image = harness.results.children[0].children[0].children[0];
  const placeholder = harness.results.children[0].children[0].children[1];
  image.listeners.error();
  assert.equal(image.hidden, true);
  assert.equal(placeholder.hidden, false);
  assert.equal(findElement(harness.results.children[1], "input"), undefined);
});

test("keeps unknown warnings visible as safe text", async () => {
  const data = candidateSet([candidate(0)]);
  data.candidates[0].reference_frame.warnings = ["Unknown <warning>"];
  const harness = createHarness(async () => ({ ok: true, json: async () => data }));
  applyReferenceTime(harness);

  await submit(harness);

  assert.match(textOf(harness.results.children[0]), /Unknown <warning>/);
});

test("a late response cannot overwrite a newer request", async () => {
  const requests = [];
  const harness = createHarness(() => {
    const request = deferred();
    requests.push(request);
    return request.promise;
  });
  applyReferenceTime(harness);

  const first = submit(harness);
  const second = submit(harness);
  requests[0].resolve({ ok: true, json: async () => candidateSet([candidate(-10)]) });
  requests[1].resolve({ ok: true, json: async () => candidateSet([candidate(10)]) });
  await Promise.all([first, second]);

  assert.deepEqual(headingTexts(harness.results), ["+10 sec"]);
  assert.equal(harness.status.dataset.state, "success");
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.previewContent.hidden, true);
});

test("request failures stay safe and leave the form usable", async () => {
  const harness = createHarness(async () => ({ ok: false }));
  applyReferenceTime(harness);

  await submit(harness);

  assert.equal(harness.status.dataset.state, "error");
  assert.equal(harness.error.hidden, false);
  assert.match(harness.error.textContent, /channel and recorded time/);
  assert.equal(harness.button.disabled, false);
  assert.equal(harness.form.attributes["aria-busy"], "false");
  assert.equal(harness.results.children.length, 0);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.previewContent.hidden, true);
});

test("distinguishes empty and all-failed candidate responses", async () => {
  const allFailedHarness = createHarness(async () => ({
    ok: true,
    json: async () => candidateSet([-10, 0, 10].map((offset) => candidate(offset, "failed"))),
  }));
  applyReferenceTime(allFailedHarness);
  await submit(allFailedHarness);
  assert.equal(allFailedHarness.status.dataset.state, "all-failed");
  assert.equal(allFailedHarness.results.children.length, 3);

  const emptyHarness = createHarness(async () => ({ ok: true, json: async () => candidateSet([]) }));
  applyReferenceTime(emptyHarness);
  await submit(emptyHarness);
  assert.equal(emptyHarness.status.dataset.state, "empty");
  assert.equal(emptyHarness.status.textContent, "No candidate positions were returned.");
});

test("invalid response data becomes a safe request error", async () => {
  const harness = createHarness(async () => ({
    ok: true,
    json: async () => ({ candidates: [{ status: "succeeded" }], summary: {} }),
  }));
  applyReferenceTime(harness);

  await submit(harness);

  assert.equal(harness.status.dataset.state, "error");
  assert.equal(harness.results.children.length, 0);
  assert.match(harness.error.textContent, /request could not be completed/);
});
