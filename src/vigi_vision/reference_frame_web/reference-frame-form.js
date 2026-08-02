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
      ? "날짜와 시각을 적용할 준비가 되었습니다."
      : "후보를 생성하기 전에 날짜와 시각을 적용하세요.";
  } else if (appliedRequestDirty) {
    referenceFrameState.textContent =
      "날짜 또는 시각이 변경되었습니다. 후보를 생성하기 전에 다시 적용하세요.";
  } else {
    referenceFrameState.textContent = "적용된 기준 시각으로 후보를 생성할 준비가 되었습니다.";
  }
}

function applyReferenceTime() {
  const normalized = getNormalizedTime();
  if (normalized === null || !hasValidReferenceInputs()) {
    referenceFrameState.textContent =
      "초 단위가 포함된 올바른 날짜와 시각을 입력한 다음 시간대를 선택하세요.";
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
  appliedReferenceTimeZone.textContent = "시간대: " + appliedRequest.source_timezone;
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
referenceFrameChannelInput.addEventListener("change", updateReferenceFormState);

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
  refresh() {
    updateReferenceFormState();
  },
});

updateReferenceFormState();
