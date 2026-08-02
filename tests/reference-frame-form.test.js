const assert = require("node:assert/strict");
const test = require("node:test");
const {
  applyReferenceTime,
  candidate,
  candidateSet,
  createHarness,
  deferred,
  submit,
} = require("./reference-frame-ui-harness.js");

test("starts unapplied and does not generate until date/time is applied", async () => {
  const calls = [];
  const harness = createHarness(async (_url, options) => {
    calls.push(JSON.parse(options.body));
    return { ok: true, json: async () => candidateSet([candidate(0)]) };
  });

  assert.equal(harness.appliedSummary.hidden, true);
  assert.equal(harness.button.disabled, true);
  assert.equal(harness.applyButton.disabled, true);
  assert.match(harness.referenceState.textContent, /날짜와 시각을 적용/);
  await submit(harness);

  assert.equal(calls.length, 0);
  assert.equal(harness.status.dataset.state, "error");
});

test("applies seconds and timezone without sending a request", () => {
  const harness = createHarness(async () => {
    throw new Error("apply must not fetch");
  });

  applyReferenceTime(harness, "2026-07-31T17:02:29", "Asia/Seoul");

  assert.equal(harness.appliedSummary.hidden, false);
  assert.equal(harness.appliedValue.textContent, "2026-07-31 17:02:29");
  assert.equal(harness.appliedTimezone.textContent, "시간대: Asia/Seoul");
  assert.equal(harness.button.disabled, false);
  assert.equal(harness.applyButton.disabled, false);
  assert.match(harness.referenceState.textContent, /후보를 생성할 준비/);
});

test("an invalid local time cannot be applied", () => {
  const harness = createHarness(async () => {
    throw new Error("invalid input must not fetch");
  });

  harness.referenceTime.value = "2026-02-30T17:02:29";
  harness.referenceTime.listeners.input();
  harness.applyButton.listeners.click();

  assert.equal(harness.appliedSummary.hidden, true);
  assert.equal(harness.button.disabled, true);
  assert.match(harness.referenceState.textContent, /날짜와 시각을 적용/);
});

test("editing an applied time or timezone makes generation dirty until reapplication", async () => {
  const harness = createHarness(async () => ({ ok: true, json: async () => candidateSet([candidate(0)]) }));
  applyReferenceTime(harness, "2026-07-31T17:02:29");
  await submit(harness);

  harness.referenceTime.value = "2026-07-31T17:02:30";
  harness.referenceTime.listeners.input();
  assert.equal(harness.button.disabled, true);
  assert.equal(harness.results.children.length, 0);
  assert.match(harness.referenceState.textContent, /변경되었습니다/);

  harness.timezone.value = "UTC";
  harness.timezone.listeners.change();
  applyReferenceTime(harness, "2026-07-31T17:02:30", "UTC");
  assert.equal(harness.button.disabled, false);
  assert.equal(harness.appliedValue.textContent, "2026-07-31 17:02:30");
  assert.equal(harness.appliedTimezone.textContent, "시간대: UTC");
});

test("submits the applied local timestamp and selected timezone", async () => {
  const calls = [];
  const harness = createHarness(async (_url, options) => {
    calls.push(JSON.parse(options.body));
    return { ok: true, json: async () => candidateSet([candidate(0)]) };
  });
  applyReferenceTime(harness, "2026-07-31T17:02:29", "UTC");

  await submit(harness);

  assert.deepEqual(calls, [{
    channel_id: 1,
    reference_time: "2026-07-31T17:02:29",
    source_timezone: "UTC",
  }]);
});

test("shows indeterminate busy feedback and restores it after success", async () => {
  const next = deferred();
  const harness = createHarness(() => next.promise);
  applyReferenceTime(harness);

  const request = submit(harness);

  assert.equal(harness.button.disabled, true);
  assert.equal(harness.applyButton.disabled, true);
  assert.equal(harness.button.textContent, "후보를 생성하는 중…");
  assert.equal(harness.generationProgress.hidden, false);
  assert.equal(harness.generationSpinner.hidden, false);
  assert.equal(harness.form.attributes["aria-busy"], "true");
  assert.equal(harness.generationIndicator.attributes["aria-valuenow"], undefined);
  assert.equal(harness.generationIndicator.attributes.value, undefined);
  assert.match(harness.status.textContent, /몇 분 정도 걸릴 수/);

  next.resolve({ ok: true, json: async () => candidateSet([candidate(0)]) });
  await request;

  assert.equal(harness.button.disabled, false);
  assert.equal(harness.applyButton.disabled, false);
  assert.equal(harness.button.textContent, "후보 생성");
  assert.equal(harness.generationProgress.hidden, true);
  assert.equal(harness.generationSpinner.hidden, true);
  assert.equal(harness.form.attributes["aria-busy"], "false");
});

test("failure and malformed responses clear busy feedback safely", async () => {
  for (const response of [
    { ok: false },
    { ok: true, json: async () => ({ candidates: [], summary: {} }) },
  ]) {
    const harness = createHarness(async () => response);
    applyReferenceTime(harness);

    await submit(harness);

    assert.equal(harness.button.disabled, false);
    assert.equal(harness.generationProgress.hidden, true);
    assert.equal(harness.generationSpinner.hidden, true);
    assert.equal(harness.form.attributes["aria-busy"], "false");
    assert.equal(harness.window.vigiVisionReferenceFrameSelection.getSelectedCandidate(), null);
  }
});

test("a stale response cannot stop the current request busy state", async () => {
  const requests = [];
  const harness = createHarness(() => {
    const request = deferred();
    requests.push(request);
    return request.promise;
  });
  applyReferenceTime(harness, "2026-07-31T17:02:29", "Asia/Seoul");
  const appliedSummary = harness.appliedValue.textContent;

  const first = submit(harness);
  const second = submit(harness);
  assert.equal(harness.button.textContent, "후보를 생성하는 중…");
  assert.equal(harness.generationProgress.hidden, false);

  requests[0].resolve({ ok: true, json: async () => candidateSet([candidate(-10)]) });
  await first;
  assert.equal(harness.button.textContent, "후보를 생성하는 중…");
  assert.equal(harness.generationProgress.hidden, false);
  assert.equal(harness.appliedValue.textContent, appliedSummary);

  requests[1].resolve({ ok: true, json: async () => candidateSet([candidate(10)]) });
  await second;
  assert.equal(harness.button.textContent, "후보 생성");
  assert.equal(harness.generationProgress.hidden, true);
  assert.equal(harness.appliedValue.textContent, appliedSummary);
});
