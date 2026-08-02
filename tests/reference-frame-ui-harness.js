const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");

const formScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-form.js"), "utf8");
const selectionScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-selection.js"), "utf8");
const roiGeometryScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-roi-geometry.js"), "utf8");
const roiScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-roi.js"), "utf8");
const assistedRequestScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-roi-assisted-request.js"), "utf8");
const assistedRoiScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-roi-assisted.js"), "utf8");
const assistedPointerScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-roi-assisted-pointer.js"), "utf8");
const roiInteractionScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-roi-interaction.js"), "utf8");
const script = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-ui.js"), "utf8");
const confirmationScript = readFileSync(join(__dirname, "..", "src", "vigi_vision", "reference_frame_web", "reference-frame-confirmation.js"), "utf8");

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.handlers = {};
    this.attributes = {};
    this.style = {};
    this.rect = { left: 0, top: 0, width: 640, height: 360 };
    this.capturedPointers = new Set();
    this.naturalWidth = 0;
    this.naturalHeight = 0;
    this.complete = false;
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.value = "";
    this.disabled = false;
    this.focused = false;
    this.width = 0;
    this.height = 0;
    this.canvasOperations = [];
  }

  addEventListener(name, handler, options = {}) {
    if (!this.handlers[name]) {
      this.handlers[name] = [];
      this.listeners[name] = (...args) => {
        let result;
        this.handlers[name].slice().forEach((entry) => {
          result = entry.handler(...args);
          if (entry.once) {
            this.removeEventListener(name, entry.handler);
          }
        });
        return result;
      };
    }
    this.handlers[name].push({ handler, once: options.once === true });
  }

  removeEventListener(name, handler) {
    this.handlers[name] = (this.handlers[name] ?? []).filter((entry) => entry.handler !== handler);
  }

  setPointerCapture(pointerId) {
    this.capturedPointers.add(pointerId);
  }

  releasePointerCapture(pointerId) {
    this.capturedPointers.delete(pointerId);
  }

  hasPointerCapture(pointerId) {
    return this.capturedPointers.has(pointerId);
  }

  focus() {
    this.focused = true;
  }

  getBoundingClientRect() {
    return this.rect;
  }

  getContext(name) {
    if (this.tagName !== "canvas" || name !== "2d") {
      return null;
    }
    return {
      clearRect: (...args) => this.canvasOperations.push(["clearRect", ...args]),
      fillRect: (...args) => this.canvasOperations.push(["fillRect", ...args]),
      fillStyle: "",
    };
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

class FakeAbortController {
  constructor() {
    this.signal = { aborted: false };
  }

  abort() {
    this.signal.aborted = true;
  }
}

function candidate(offsetSeconds, status = "succeeded", channelId = 1) {
  const candidateRequestedTime = new Date(
    Date.parse("2026-07-20T03:34:18Z") + offsetSeconds * 1000,
  ).toISOString().replace(".000Z", "Z");
  const base = {
    offset_seconds: offsetSeconds,
    candidate_requested_time_utc: candidateRequestedTime,
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
      channel_id: channelId,
      requested_time_utc: candidateRequestedTime,
      timing: {
        precision_status: "measured_clip_relative",
        decoded_clip_relative_pts_seconds: 2.04,
        estimated_source_time_utc: null,
        offset_from_requested_seconds: null,
      },
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

function createHarness(
  fetchImplementation,
  channelResponse = {
    ok: true,
    json: async () => ({
      channels: [{ channel_id: 1, name: "Counter", alias: "Counter", online: true }],
      default_channel_id: 1,
    }),
  },
  options = {},
) {
  const form = new FakeElement("form");
  form.reportValidity = () => true;
  const elements = new Map([
    ["#candidate-form", form],
    ["#channel-id", new FakeElement("select")],
    ["#channel-status", new FakeElement("p")],
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
    ["#roi-workspace", new FakeElement("div")],
    ["#roi-stage", new FakeElement("div")],
    ["#roi-assisted-button", new FakeElement("button")],
    ["#roi-assisted-spinner", new FakeElement("span")],
    ["#roi-assisted-guidance", new FakeElement("p")],
    ["#roi-assisted-marker", new FakeElement("div")],
    ["#roi-assisted-mask", new FakeElement("canvas")],
    ["#roi-committed-overlay", new FakeElement("div")],
    ["#roi-draft-overlay", new FakeElement("div")],
    ["#roi-reset", new FakeElement("button")],
    ...["nw", "n", "ne", "e", "se", "s", "sw", "w"].map((handle) => [
      `#roi-handle-${handle}`,
      new FakeElement("button"),
    ]),
    ["#roi-status", new FakeElement("p")],
    ["#roi-summary", new FakeElement("div")],
    ["#roi-summary-x", new FakeElement("dd")],
    ["#roi-summary-y", new FakeElement("dd")],
    ["#roi-summary-width", new FakeElement("dd")],
    ["#roi-summary-height", new FakeElement("dd")],
    ["#roi-summary-source-width", new FakeElement("dd")],
    ["#roi-summary-source-height", new FakeElement("dd")],
    ["#confirmation-panel", new FakeElement("section")],
    ["#confirmation-review", new FakeElement("p")],
    ["#confirmation-status", new FakeElement("p")],
    ["#confirmation-error", new FakeElement("p")],
    ["#confirmation-action", new FakeElement("button")],
    ["#confirmation-result", new FakeElement("div")],
    ["#confirmation-id", new FakeElement("dd")],
    ["#confirmation-confirmed-at", new FakeElement("dd")],
    ["#confirmation-artifact", new FakeElement("dd")],
  ]);
  elements.get("#channel-id").value = "1";
  elements.get("#source-timezone").value = "Asia/Seoul";
  const kstOption = new FakeElement("option");
  kstOption.value = "Asia/Seoul";
  kstOption.textContent = "Asia/Seoul (KST, UTC+09:00)";
  const utcOption = new FakeElement("option");
  utcOption.value = "UTC";
  utcOption.textContent = "UTC (UTC+00:00)";
  elements.get("#source-timezone").replaceChildren(kstOption, utcOption);
  elements.get("#apply-reference-time").disabled = true;
  elements.get("#generate-button").disabled = true;
  elements.get("#applied-reference-time").hidden = true;
  elements.get("#generation-progress").hidden = true;
  elements.get("#generation-spinner").hidden = true;
  elements.get("#roi-stage").hidden = true;
  elements.get("#roi-assisted-button").disabled = true;
  elements.get("#roi-assisted-button").setAttribute("aria-pressed", "false");
  elements.get("#roi-assisted-spinner").hidden = true;
  elements.get("#roi-assisted-spinner").setAttribute("aria-hidden", "true");
  elements.get("#roi-assisted-marker").hidden = true;
  elements.get("#roi-assisted-mask").hidden = true;
  elements.get("#roi-summary").hidden = true;
  elements.get("#confirmation-panel").hidden = true;
  elements.get("#confirmation-result").hidden = true;
  elements.get("#confirmation-error").hidden = true;
  elements.get("#roi-status").textContent = "Select a candidate first.";
  elements.get("#roi-status").dataset.state = "disabled";
  elements.get("#roi-status").setAttribute("aria-busy", "false");
  elements.get("#selected-preview-image").naturalWidth = 2560;
  elements.get("#selected-preview-image").naturalHeight = 1440;
  const windowListeners = {};
  const timers = new Map();
  const timerDelays = [];
  let timerSequence = 0;
  const setTimeout = (handler, delay) => {
    const id = ++timerSequence;
    timers.set(id, { handler, delay });
    timerDelays.push(delay);
    return id;
  };
  const clearTimeout = (id) => {
    timers.delete(id);
  };
  const runTimers = () => {
    const scheduled = Array.from(timers.values());
    timers.clear();
    scheduled.forEach(({ handler }) => handler());
  };
  const context = vm.createContext({
    document: {
      createElement: (tagName) => new FakeElement(tagName),
      querySelector: (selector) => elements.get(selector),
    },
    window: {
      clearTimeout,
      setTimeout,
      addEventListener(name, handler) {
        if (windowListeners[name] === undefined) {
          const handlers = [];
          const listener = (...args) => handlers.forEach((entry) => entry(...args));
          listener.handlers = handlers;
          windowListeners[name] = listener;
        }
        windowListeners[name].handlers.push(handler);
      },
    },
    AbortController: FakeAbortController,
     fetch: (url, requestOptions) => url === "/api/v1/reference-frames/channels"
       ? (typeof channelResponse === "function" ? channelResponse(url, requestOptions) : Promise.resolve(channelResponse))
       : (!options.confirmation && url.startsWith("/api/v1/investigation-confirmations/")
         ? Promise.resolve({ ok: false, status: 404, json: async () => ({ error: { code: "investigation_not_found" } }) })
         : fetchImplementation(url, requestOptions)),
  });
  vm.runInContext(formScript, context);
  vm.runInContext(selectionScript, context);
  vm.runInContext(roiGeometryScript, context);
  vm.runInContext(roiScript, context);
  vm.runInContext(assistedRequestScript, context);
  vm.runInContext(assistedRoiScript, context);
  vm.runInContext(assistedPointerScript, context);
  vm.runInContext(roiInteractionScript, context);
  vm.runInContext(script, context);
  vm.runInContext(confirmationScript, context);
  return {
    form,
    channel: elements.get("#channel-id"),
    channelStatus: elements.get("#channel-status"),
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
    roiWorkspace: elements.get("#roi-workspace"),
    roiStage: elements.get("#roi-stage"),
    assistedButton: elements.get("#roi-assisted-button"),
    assistedSpinner: elements.get("#roi-assisted-spinner"),
    assistedGuidance: elements.get("#roi-assisted-guidance"),
    assistedMarker: elements.get("#roi-assisted-marker"),
    assistedMask: elements.get("#roi-assisted-mask"),
    committedOverlay: elements.get("#roi-committed-overlay"),
    draftOverlay: elements.get("#roi-draft-overlay"),
    resetButton: elements.get("#roi-reset"),
    handles: Object.fromEntries(["nw", "n", "ne", "e", "se", "s", "sw", "w"].map((handle) => [
      handle,
      elements.get(`#roi-handle-${handle}`),
    ])),
    roiStatus: elements.get("#roi-status"),
    roiSummary: elements.get("#roi-summary"),
    roiSummaryX: elements.get("#roi-summary-x"),
    roiSummaryY: elements.get("#roi-summary-y"),
    roiSummaryWidth: elements.get("#roi-summary-width"),
    roiSummaryHeight: elements.get("#roi-summary-height"),
    roiSummarySourceWidth: elements.get("#roi-summary-source-width"),
    roiSummarySourceHeight: elements.get("#roi-summary-source-height"),
    confirmationPanel: elements.get("#confirmation-panel"),
    confirmationReview: elements.get("#confirmation-review"),
    confirmationStatus: elements.get("#confirmation-status"),
    confirmationError: elements.get("#confirmation-error"),
    confirmationAction: elements.get("#confirmation-action"),
    confirmationResult: elements.get("#confirmation-result"),
    confirmationId: elements.get("#confirmation-id"),
    confirmationConfirmedAt: elements.get("#confirmation-confirmed-at"),
    confirmationArtifact: elements.get("#confirmation-artifact"),
    windowListeners,
    window: context.window,
    runTimers,
    timerDelays,
    pendingTimerCount: () => timers.size,
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
