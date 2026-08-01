const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");

const formScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-form.js"), "utf8");
const selectionScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-selection.js"), "utf8");
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
      resource_id: `resource-${offsetSeconds}`,
      image_url: `/api/v1/reference-frames/frame-${offsetSeconds}/image`,
      image: { width: 2560, height: 1440 },
      timing: { precision_status: "measured_clip_relative" },
      warnings: ["Source timestamp mapping is unavailable pending real-NVR replay validation."],
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
    ["#source-timezone", new FakeElement("select")],
    ["#apply-reference-time", new FakeElement("button")],
    ["#generate-button", new FakeElement("button")],
    ["#reference-time-state", new FakeElement("p")],
    ["#applied-reference-time", new FakeElement("div")],
    ["#applied-reference-time-value", new FakeElement("p")],
    ["#applied-reference-time-zone", new FakeElement("p")],
    ["#generation-progress", new FakeElement("div")],
    ["#generation-spinner", new FakeElement("span")],
    ["#generation-indicator", new FakeElement("progress")],
    ["#request-status", new FakeElement("p")],
    ["#request-error", new FakeElement("p")],
    ["#candidate-results", new FakeElement("ol")],
    ["#selection-status", new FakeElement("p")],
    ["#selected-preview-content", new FakeElement("div")],
    ["#selected-preview-image", new FakeElement("img")],
    ["#selected-preview-facts", new FakeElement("dl")],
    ["#selected-preview-warnings", new FakeElement("div")],
  ]);
  elements.get("#channel-id").value = "1";
  elements.get("#source-timezone").value = "Asia/Seoul";
  elements.get("#apply-reference-time").disabled = true;
  elements.get("#generate-button").disabled = true;
  elements.get("#applied-reference-time").hidden = true;
  elements.get("#generation-progress").hidden = true;
  elements.get("#generation-spinner").hidden = true;
  const context = vm.createContext({
    document: {
      createElement: (tagName) => new FakeElement(tagName),
      querySelector: (selector) => elements.get(selector),
    },
    window: {},
    fetch: fetchImplementation,
  });
  vm.runInContext(formScript, context);
  vm.runInContext(selectionScript, context);
  vm.runInContext(script, context);
  return {
    form,
    channel: elements.get("#channel-id"),
    referenceTime: elements.get("#reference-time"),
    timezone: elements.get("#source-timezone"),
    applyButton: elements.get("#apply-reference-time"),
    button: elements.get("#generate-button"),
    referenceState: elements.get("#reference-time-state"),
    appliedSummary: elements.get("#applied-reference-time"),
    appliedValue: elements.get("#applied-reference-time-value"),
    appliedTimezone: elements.get("#applied-reference-time-zone"),
    generationProgress: elements.get("#generation-progress"),
    generationSpinner: elements.get("#generation-spinner"),
    generationIndicator: elements.get("#generation-indicator"),
    status: elements.get("#request-status"),
    error: elements.get("#request-error"),
    results: elements.get("#candidate-results"),
    selectionStatus: elements.get("#selection-status"),
    previewContent: elements.get("#selected-preview-content"),
    previewImage: elements.get("#selected-preview-image"),
    previewFacts: elements.get("#selected-preview-facts"),
    previewWarnings: elements.get("#selected-preview-warnings"),
    window: context.window,
  };
}

function submit(harness) {
  return harness.form.listeners.submit({ preventDefault() {} });
}

function applyReferenceTime(harness, value = "2026-07-20T12:34:18", timezone = "Asia/Seoul") {
  harness.referenceTime.value = value;
  harness.timezone.value = timezone;
  harness.referenceTime.listeners.input();
  harness.timezone.listeners.change();
  harness.applyButton.listeners.click();
}

function headingTexts(results) {
  return results.children.map((card) => card.children[1].children[0].textContent);
}

function textOf(element) {
  return [element.textContent, ...element.children.map(textOf)].join("");
}

function findElement(root, tagName) {
  for (const child of root.children) {
    if (child.tagName === tagName) {
      return child;
    }
    const nested = findElement(child, tagName);
    if (nested) {
      return nested;
    }
  }
  return undefined;
}

module.exports = {
  applyReferenceTime,
  candidate,
  candidateSet,
  createHarness,
  deferred,
  findElement,
  headingTexts,
  submit,
  textOf,
};
