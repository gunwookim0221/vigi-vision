const assert = require("node:assert/strict");
const test = require("node:test");
const { applyReferenceTime, candidate, candidateSet, createHarness, deferred, findElement, submit, textOf } = require("./reference-frame-ui-harness.js");

test("selects exactly one loaded candidate and updates the larger preview", async () => {
  const data = candidateSet([-10, 0, 10].map((offset) => candidate(offset)));
  const harness = createHarness(async () => ({ ok: true, json: async () => data }));
  applyReferenceTime(harness);

  await submit(harness);
  const firstCard = harness.results.children[0];
  const secondCard = harness.results.children[1];
  const firstImage = firstCard.children[0].children[0];
  const secondImage = secondCard.children[0].children[0];
  const firstRadio = findElement(firstCard, "input");
  const secondRadio = findElement(secondCard, "input");

  assert.equal(firstRadio.type, "radio");
  assert.equal(firstRadio.disabled, true);
  assert.equal(firstRadio.name, secondRadio.name);
  firstImage.listeners.load();
  secondImage.listeners.load();
  assert.equal(firstRadio.disabled, false);
  assert.equal(secondRadio.disabled, false);

  firstRadio.checked = true;
  firstRadio.listeners.change();
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate().reference_frame.resource_id, "resource--10");
  assert.equal(firstCard.dataset.selected, "true");
  assert.equal(firstRadio.checked, true);
  assert.equal(harness.previewContent.hidden, false);
  assert.equal(harness.previewImage.src, "/api/v1/reference-frames/frame--10/image");
  assert.match(textOf(harness.previewFacts), /resource--10/);
  assert.match(textOf(harness.previewFacts), /클립 기준 측정/);

  secondRadio.checked = true;
  secondRadio.listeners.change();
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate().reference_frame.resource_id, "resource-0");
  assert.equal(firstCard.dataset.selected, "false");
  assert.equal(firstRadio.checked, false);
  assert.equal(secondCard.dataset.selected, "true");
  assert.equal(harness.previewImage.src, "/api/v1/reference-frames/frame-0/image");
  assert.match(harness.selectionStatus.textContent, /선택한 후보:/);
});

test("thumbnail or detail-preview failure disables and clears a selected candidate", async () => {
  const harness = createHarness(async () => ({ ok: true, json: async () => candidateSet([candidate(-10)]) }));
  applyReferenceTime(harness);

  await submit(harness);
  const card = harness.results.children[0];
  const image = card.children[0].children[0];
  const radio = findElement(card, "input");
  image.listeners.load();
  radio.checked = true;
  radio.listeners.change();
  harness.previewImage.onerror();

  assert.equal(radio.disabled, true);
  assert.equal(radio.checked, false);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.previewContent.hidden, true);
  assert.equal(card.children[0].children[1].hidden, false);
  assert.equal(harness.selectionStatus.textContent, "선택한 후보를 사용할 수 없습니다.");
});

test("a succeeded candidate without backend identity stays unavailable", async () => {
  const unavailable = candidate(-10);
  unavailable.reference_frame.resource_id = "";
  const harness = createHarness(async () => ({ ok: true, json: async () => candidateSet([unavailable]) }));
  applyReferenceTime(harness);

  await submit(harness);
  const card = harness.results.children[0];

  assert.equal(findElement(card, "input"), undefined);
  assert.ok(textOf(card).includes("선택할 수 없습니다."));
});

test("a new request clears the current selection before the response arrives", async () => {
  let requestCount = 0;
  const nextRequest = deferred();
  const harness = createHarness(() => {
    requestCount += 1;
    if (requestCount === 1) {
      return Promise.resolve({ ok: true, json: async () => candidateSet([candidate(-10)]) });
    }
    return nextRequest.promise;
  });
  applyReferenceTime(harness);

  await submit(harness);
  const card = harness.results.children[0];
  const image = card.children[0].children[0];
  const radio = findElement(card, "input");
  image.listeners.load();
  radio.checked = true;
  radio.listeners.change();
  assert.notEqual(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);

  const second = submit(harness);
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  assert.equal(harness.previewContent.hidden, true);
  assert.equal(harness.results.children.length, 0);
  nextRequest.resolve({ ok: true, json: async () => candidateSet([candidate(10)]) });
  await second;
  assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
});
