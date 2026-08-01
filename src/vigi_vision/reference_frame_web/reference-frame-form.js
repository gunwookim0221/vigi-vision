const referenceFrameChannelInput = document.querySelector("#channel-id");
const referenceFrameTimeInput = document.querySelector("#reference-time");
const referenceFrameTimezoneInput = document.querySelector("#source-timezone");
const referenceFrameApplyButton = document.querySelector("#apply-reference-time");
const referenceFrameGenerateButton = document.querySelector("#generate-button");
const referenceFrameState = document.querySelector("#reference-time-state");
const appliedReferenceTime = document.querySelector("#applied-reference-time");
const appliedReferenceTimeValue = document.querySelector("#applied-reference-time-value");
const appliedReferenceTimeZone = document.querySelector("#applied-reference-time-zone");

let appliedRequest = null;
let appliedRequestDirty = false;
let generationActive = false;

function normalizeLocalDateTime(value) {
  if (typeof value !== "string") {
    return null;
  }
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) {
    return null;
  }
  const normalized = match[1] + ":" + (match[2] ?? "00");
  const parsed = new Date(normalized + "Z");
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 19) !== normalized) {
    return null;
  }
  return normalized;
}

function hasValidChannel() {
  return /^\d+$/.test(referenceFrameChannelInput.value)
    && Number(referenceFrameChannelInput.value) > 0;
}

function getNormalizedTime() {
  return normalizeLocalDateTime(referenceFrameTimeInput.value);
}

function hasValidReferenceInputs() {
  return getNormalizedTime() !== null
    && typeof referenceFrameTimezoneInput.value === "string"
    && referenceFrameTimezoneInput.value.length > 0;
}

function updateReferenceFormState() {
  const validReferenceInputs = hasValidReferenceInputs();
  referenceFrameApplyButton.disabled = !validReferenceInputs || generationActive;
  const ready = validReferenceInputs
    && hasValidChannel()
    && appliedRequest !== null
    && !appliedRequestDirty
    && !generationActive;
  referenceFrameGenerateButton.disabled = !ready;

  if (appliedRequest === null) {
    referenceFrameState.textContent = validReferenceInputs
      ? "Date and time are ready to apply."
      : "Apply date and time before generating candidates.";
  } else if (appliedRequestDirty) {
    referenceFrameState.textContent =
      "Date and time changed. Apply date and time again before generating candidates.";
  } else {
    referenceFrameState.textContent = "Applied reference time is ready for candidate generation.";
  }
}

function applyReferenceTime() {
  const normalized = getNormalizedTime();
  if (normalized === null || !hasValidReferenceInputs()) {
    referenceFrameState.textContent =
      "Enter a valid date and time with seconds, then choose a timezone.";
    updateReferenceFormState();
    return;
  }
  referenceFrameTimeInput.value = normalized;
  appliedRequest = {
    reference_time: normalized,
    source_timezone: referenceFrameTimezoneInput.value,
  };
  appliedRequestDirty = false;
  const [date, time] = normalized.split("T");
  appliedReferenceTimeValue.textContent = date + " " + time;
  appliedReferenceTimeZone.textContent = "Timezone: " + appliedRequest.source_timezone;
  appliedReferenceTime.hidden = false;
  updateReferenceFormState();
}

function markReferenceTimeDirty() {
  if (appliedRequest !== null) {
    appliedRequestDirty = true;
  }
  updateReferenceFormState();
}

referenceFrameApplyButton.addEventListener("click", applyReferenceTime);
referenceFrameTimeInput.addEventListener("input", markReferenceTimeDirty);
referenceFrameTimeInput.addEventListener("change", markReferenceTimeDirty);
referenceFrameTimezoneInput.addEventListener("change", markReferenceTimeDirty);
referenceFrameChannelInput.addEventListener("input", updateReferenceFormState);

const referenceFrameForm = Object.freeze({
  getRequestPayload() {
    return appliedRequest === null ? null : { ...appliedRequest };
  },
  isReady() {
    updateReferenceFormState();
    return hasValidReferenceInputs()
      && hasValidChannel()
      && appliedRequest !== null
      && !appliedRequestDirty;
  },
  setGenerationActive(active) {
    generationActive = active;
    updateReferenceFormState();
  },
});

updateReferenceFormState();
