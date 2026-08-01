const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-ui.js"), "utf8");

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.attributes = {};
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.value = "";
    this.disabled = false;
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }
}

function candidate(offsetSeconds, status = "succeeded") {
  const base = {
    offset_seconds: offsetSeconds,
    candidate_requested_time_utc: `2026-07-20T03:${String(10 + offsetSeconds).padStart(2, "0")}:00Z`,
    status,
  };
  if (status === "failed") {
    return {
      ...base,
      failure: { code: "recording_unavailable", message: "No recording is available for this position." },
      warnings: [],
    };
  }
  return {
    ...base,
    outcome: "created",
    reference_frame: {
      image_url: `/api/v1/reference-frames/frame-${offsetSeconds}/image`,
      image: { width: 2560, height: 1440 },
      timing: { precision_status: "measured_clip_relative" },
      warnings: ["Absolute source time is not established."],
    },
  };
}

function candidateSet(candidates) {
  return {
    candidates,
    summary: {
      created: candidates.filter((item) => item.status === "succeeded").length,
      reused: 0,
      failed: candidates.filter((item) => item.status === "failed").length,
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function createHarness(fetchImplementation) {
  const form = new FakeElement("form");
  form.reportValidity = () => true;
  const elements = new Map([
    ["#candidate-form", form],
    ["#channel-id", new FakeElement("input")],
    ["#reference-time", new FakeElement("input")],
    ["#generate-button", new FakeElement("button")],
    ["#request-status", new FakeElement("p")],
    ["#request-error", new FakeElement("p")],
    ["#candidate-results", new FakeElement("ol")],
  ]);
  elements.get("#channel-id").value = "1";
  elements.get("#reference-time").value = "2026-07-20T12:34:18";
  const context = vm.createContext({
    document: {
      createElement: (tagName) => new FakeElement(tagName),
      querySelector: (selector) => elements.get(selector),
    },
    fetch: fetchImplementation,
  });
  vm.runInContext(script, context);
  return {
    form,
    button: elements.get("#generate-button"),
    status: elements.get("#request-status"),
    error: elements.get("#request-error"),
    results: elements.get("#candidate-results"),
  };
}

function submit(harness) {
  return harness.form.listeners.submit({ preventDefault() {} });
}

function headingTexts(results) {
  return results.children.map((card) => card.children[1].children[0].textContent);
}

function textOf(element) {
  return [element.textContent, ...element.children.map(textOf)].join("");
}

test("renders successful thumbnails in API order and exposes backend timing", async () => {
  const data = candidateSet([-10, 0, 10].map((offset) => candidate(offset)));
  const harness = createHarness(async () => ({ ok: true, json: async () => data }));

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
  assert.match(harness.results.children[0].children[1].children[2].textContent, /Absolute source time/);
});

test("keeps failed candidates in place and falls back when an image load fails", async () => {
  const data = candidateSet([candidate(-10), candidate(0, "failed"), candidate(10)]);
  const harness = createHarness(async () => ({ ok: true, json: async () => data }));

  await submit(harness);

  assert.deepEqual(headingTexts(harness.results), ["-10 sec", "Reference", "+10 sec"]);
  assert.equal(harness.results.children[1].children[0].children[0].textContent, "Preview unavailable for this candidate.");
  assert.equal(textOf(harness.results.children[1].children[1].children[1].children[3]), "Failure: recording_unavailableNo recording is available for this position.");
  const image = harness.results.children[0].children[0].children[0];
  const placeholder = harness.results.children[0].children[0].children[1];
  image.listeners.error();
  assert.equal(image.hidden, true);
  assert.equal(placeholder.hidden, false);
});

test("a late response cannot overwrite a newer request", async () => {
  const requests = [];
  const harness = createHarness(() => {
    const request = deferred();
    requests.push(request);
    return request.promise;
  });

  const first = submit(harness);
  const second = submit(harness);
  requests[0].resolve({ ok: true, json: async () => candidateSet([candidate(-10)]) });
  requests[1].resolve({ ok: true, json: async () => candidateSet([candidate(10)]) });
  await Promise.all([first, second]);

  assert.deepEqual(headingTexts(harness.results), ["+10 sec"]);
  assert.equal(harness.status.dataset.state, "success");
});

test("request failures stay safe and leave the form usable", async () => {
  const harness = createHarness(async () => ({ ok: false }));

  await submit(harness);

  assert.equal(harness.status.dataset.state, "error");
  assert.equal(harness.error.hidden, false);
  assert.match(harness.error.textContent, /channel and recorded time/);
  assert.equal(harness.button.disabled, false);
  assert.equal(harness.form.attributes["aria-busy"], undefined);
  assert.equal(harness.results.children.length, 0);
});

test("distinguishes empty and all-failed candidate responses", async () => {
  const allFailedHarness = createHarness(async () => ({
    ok: true,
    json: async () => candidateSet([-10, 0, 10].map((offset) => candidate(offset, "failed"))),
  }));
  await submit(allFailedHarness);
  assert.equal(allFailedHarness.status.dataset.state, "all-failed");
  assert.equal(allFailedHarness.results.children.length, 3);

  const emptyHarness = createHarness(async () => ({ ok: true, json: async () => candidateSet([]) }));
  await submit(emptyHarness);
  assert.equal(emptyHarness.status.dataset.state, "empty");
  assert.equal(emptyHarness.status.textContent, "No candidate positions were returned.");
});

test("invalid response data becomes a safe request error", async () => {
  const harness = createHarness(async () => ({
    ok: true,
    json: async () => ({ candidates: [{ status: "succeeded" }], summary: {} }),
  }));

  await submit(harness);

  assert.equal(harness.status.dataset.state, "error");
  assert.equal(harness.results.children.length, 0);
  assert.match(harness.error.textContent, /request could not be completed/);
});
