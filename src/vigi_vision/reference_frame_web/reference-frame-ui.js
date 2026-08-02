const candidateForm = document.querySelector("#candidate-form");
const channelIdInput = document.querySelector("#channel-id");
const channelStatus = document.querySelector("#channel-status");
const referenceTimeInput = document.querySelector("#reference-time");
const generateButton = document.querySelector("#generate-button");
const requestStatus = document.querySelector("#request-status");
const requestError = document.querySelector("#request-error");
const candidateResults = document.querySelector("#candidate-results");
const generationProgress = document.querySelector("#generation-progress");
const generationSpinner = document.querySelector("#generation-spinner");
let requestSequence = 0;
let channelRequestSequence = 0;
let channelSelectionExplicit = false;
let candidateGenerationActive = false;

const SOURCE_TIMESTAMP_WARNING =
  "Source timestamp mapping is unavailable pending real-NVR replay validation.";
const USER_SOURCE_TIMESTAMP_LIMITATION =
  "정확한 원본 시각은 아직 확인되지 않았습니다. 표시된 시각은 요청한 위치입니다.";
const USER_WARNING_MESSAGES = Object.freeze({
  [SOURCE_TIMESTAMP_WARNING]: USER_SOURCE_TIMESTAMP_LIMITATION,
  "Only decoded frames before the requested clip position were available.":
    "요청한 클립 위치보다 이전에 디코딩된 프레임만 사용할 수 있습니다.",
  "Only decoded frames after the requested clip position were available.":
    "요청한 클립 위치보다 이후에 디코딩된 프레임만 사용할 수 있습니다.",
  "The channel is currently offline; historical recordings may still be available.":
    "채널이 현재 오프라인입니다. 과거 녹화 영상은 사용할 수 있을 수 있습니다.",
});
const FAILURE_CODE_LABELS = Object.freeze({
  recording_unavailable: "녹화 없음",
  channel_not_found: "채널 없음",
  replay_timeout: "재생 시간 초과",
  replay_failure: "재생 처리 실패",
  decode_timeout: "디코딩 시간 초과",
  decode_failure: "프레임 디코딩 실패",
});
const FAILURE_MESSAGES = Object.freeze({
  recording_unavailable: "요청한 시각에 녹화 영상이 없습니다.",
  channel_not_found: "요청한 채널을 찾을 수 없습니다.",
  replay_timeout: "녹화 재생 처리 시간이 초과되었습니다.",
  replay_failure: "녹화 재생을 안전하게 처리하지 못했습니다.",
  decode_timeout: "기준 프레임 디코딩 시간이 초과되었습니다.",
  decode_failure: "재생 클립에서 기준 프레임을 만들지 못했습니다.",
});
const TIMING_PRECISION_LABELS = Object.freeze({
  measured_clip_relative: "클립 기준 측정",
  estimated: "추정",
  unavailable: "사용할 수 없음",
  indeterminate: "확인 불가",
});
const OUTCOME_LABELS = Object.freeze({ created: "생성됨", reused: "재사용됨" });
const CHANNEL_LOADING_MESSAGE = "채널을 불러오는 중입니다…";
const CHANNEL_EMPTY_MESSAGE = "사용 가능한 온라인 채널이 없습니다.";
const CHANNEL_DISCOVERY_FAILURE = "채널 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.";
const CHANNEL_DISCOVERY_TIMEOUT = "채널 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
const CHANNEL_DISCOVERY_TIMEOUT_MS = 10_000;
const CHANNEL_REQUEST_ABORTED = Symbol("channel-request-aborted");
let activeChannelRequest = null;

function isUsableChannel(channel) {
  return channel
    && typeof channel === "object"
    && Number.isInteger(channel.channel_id)
    && channel.channel_id > 0
    && channel.online === true;
}

function normalizeChannelList(payload) {
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.channels)) {
    return null;
  }
  const seen = new Set();
  return payload.channels.filter((channel) => {
    if (!isUsableChannel(channel) || seen.has(channel.channel_id)) {
      return false;
    }
    seen.add(channel.channel_id);
    return true;
  });
}

function defaultChannelId(channels, payload) {
  const responseDefault = Number(payload.default_channel_id);
  if (Number.isInteger(responseDefault) && channels.some((channel) => channel.channel_id === responseDefault)) {
    return responseDefault;
  }
  const preferred = channels.find((channel) => channel.channel_id === 1);
  if (preferred) {
    return preferred.channel_id;
  }
  return channels.reduce((smallest, channel) =>
    smallest === null || channel.channel_id < smallest ? channel.channel_id : smallest, null);
}

function decodeChannelMetadata(value) {
  if (typeof value !== "string") {
    return "";
  }
  const normalized = value.trim();
  if (normalized.length === 0) {
    return "";
  }
  try {
    return decodeURIComponent(normalized).trim();
  } catch {
    return normalized;
  }
}

function channelLabel(channel) {
  const metadata = [channel.name, channel.alias]
    .map(decodeChannelMetadata)
    .filter((value, index, values) => value.length > 0 && values.indexOf(value) === index);
  return [`채널 ${channel.channel_id}`, ...metadata].join(" - ");
}

function confirmationLocked() {
  return window.vigiVisionReferenceFrameConfirmation?.getState?.().locked === true;
}

function replaceChannelOptions(channels, selectedId) {
  if (confirmationLocked()) {
    return;
  }
  const currentId = Number(channelIdInput.value);
  const channelChanged = selectedId !== currentId;
  if (channelChanged) {
    transitionChannel("채널 목록이 변경되어 후보를 다시 생성해야 합니다.");
  }
  channelIdInput.replaceChildren();
  channels.forEach((channel) => {
    const option = document.createElement("option");
    option.value = String(channel.channel_id);
    option.textContent = channelLabel(channel);
    channelIdInput.append(option);
  });
  channelIdInput.value = selectedId === null ? "" : String(selectedId);
  channelIdInput.disabled = channels.length === 0 || confirmationLocked();
  referenceFrameForm.refresh();
}

function abortChannelRequest(record) {
  if (record === null || record === undefined) {
    return;
  }
  record.aborted = true;
  if (record.timerId !== null) {
    window.clearTimeout(record.timerId);
    record.timerId = null;
  }
  if (record.controller !== null) {
    record.controller.abort();
  }
  record.rejectControl?.(CHANNEL_REQUEST_ABORTED);
}

async function loadChannels() {
  const sequence = ++channelRequestSequence;
  abortChannelRequest(activeChannelRequest);
  const hadKnownOptions = Array.from(channelIdInput.children).some((option) =>
    /^\d+$/.test(option.value) && Number(option.value) > 0);
  channelStatus.textContent = CHANNEL_LOADING_MESSAGE;
  if (!hadKnownOptions) {
    channelIdInput.disabled = true;
  }
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const record = {
    aborted: false,
    controller,
    rejectControl: null,
    timerId: null,
    timedOut: false,
  };
  activeChannelRequest = record;
  const timeoutToken = Symbol("channel-request-timeout");
  const control = new Promise((_, reject) => {
    record.rejectControl = reject;
  });
  record.timerId = window.setTimeout(() => {
    if (record.aborted || sequence !== channelRequestSequence) {
      return;
    }
    record.timedOut = true;
    if (record.controller !== null) {
      record.controller.abort();
    }
    record.rejectControl?.(timeoutToken);
  }, CHANNEL_DISCOVERY_TIMEOUT_MS);
  try {
    const request = fetch(
      "/api/v1/reference-frames/channels",
      controller === null ? undefined : { signal: controller.signal },
    ).then(async (response) => {
      if (!response.ok) {
        throw new Error("channel discovery failed");
      }
      return response.json();
    });
    const payload = await Promise.race([request, control]);
    const channels = normalizeChannelList(payload);
    if (channels === null) {
      throw new Error("channel discovery response was invalid");
    }
    if (sequence !== channelRequestSequence) {
      return;
    }
    const latestValue = channelIdInput.value;
    const currentId = Number(latestValue);
    const retained = Number.isInteger(currentId)
      && channels.some((channel) => channel.channel_id === currentId);
    const selectedId = retained ? currentId : defaultChannelId(channels, payload);
    channelSelectionExplicit = retained && channelSelectionExplicit;
    replaceChannelOptions(channels, selectedId);
    channelStatus.textContent = channels.length > 0
      ? `온라인 채널 ${channels.length}개를 사용할 수 있습니다.`
      : CHANNEL_EMPTY_MESSAGE;
  } catch (error) {
    if (sequence !== channelRequestSequence) {
      return;
    }
    if (confirmationLocked()) {
      return;
    }
    if (!hadKnownOptions) {
      replaceChannelOptions([], null);
    } else {
      channelIdInput.disabled = channelIdInput.value.length === 0;
      referenceFrameForm.refresh();
    }
    channelStatus.textContent = error === timeoutToken || record.timedOut
      ? CHANNEL_DISCOVERY_TIMEOUT
      : CHANNEL_DISCOVERY_FAILURE;
  } finally {
    if (record.timerId !== null) {
      window.clearTimeout(record.timerId);
      record.timerId = null;
    }
    if (activeChannelRequest === record) {
      activeChannelRequest = null;
    }
  }
}

function setRequestState(state, message) {
  requestStatus.dataset.state = state;
  requestStatus.textContent = message;
}

function displayWarning(warning) {
  return USER_WARNING_MESSAGES[warning] ?? warning;
}

function displayFailureCode(code) {
  return FAILURE_CODE_LABELS[code] ?? code;
}

function displayFailureMessage(failure) {
  return FAILURE_MESSAGES[failure?.code] ?? failure?.message ?? "요청을 완료하지 못했습니다.";
}

function displayTimingPrecision(value) {
  return TIMING_PRECISION_LABELS[value] ?? value;
}

function displayOutcome(value) {
  return OUTCOME_LABELS[value] ?? value;
}

function clearResults() {
  referenceFrameSelection.reset();
  requestError.hidden = true;
  requestError.textContent = "";
  candidateResults.replaceChildren();
}

function invalidateCandidateRequest(message = "") {
  requestSequence += 1;
  clearResults();
  if (candidateGenerationActive) {
    setGenerationBusy(false);
  }
  if (message.length > 0) {
    setRequestState("ready", message);
  }
}

function transitionChannel(message = "") {
  if (confirmationLocked()) {
    return;
  }
  invalidateCandidateRequest(message);
  window.vigiVisionReferenceFrameConfirmation?.refresh?.();
}

function currentCandidateRequestContext() {
  const payload = referenceFrameForm.getRequestPayload();
  return payload === null
    ? null
    : {
      channelId: Number(channelIdInput.value),
      referenceTime: payload.reference_time,
      sourceTimezone: payload.source_timezone,
    };
}

function isCurrentCandidateRequest(sequence, context) {
  const current = currentCandidateRequestContext();
  return sequence === requestSequence
    && current !== null
    && referenceFrameForm.isReady()
    && current.channelId === context.channelId
    && current.referenceTime === context.referenceTime
    && current.sourceTimezone === context.sourceTimezone;
}

function renderRequestError(message = "요청을 완료하지 못했습니다. 채널과 녹화 시각을 확인한 후 다시 시도하세요.") {
  referenceFrameSelection.reset();
  requestError.hidden = false;
  requestError.textContent = message;
  setRequestState("error", "후보 요청을 완료하지 못했습니다.");
}

function appendFact(row, label, value, isMono = false) {
  const fact = document.createElement("div");
  fact.className = "candidate-fact";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  if (isMono) {
    description.className = "mono";
  }
  fact.append(term, description);
  row.append(fact);
}

function appendWarnings(container, warnings) {
  if (!Array.isArray(warnings)) {
    return;
  }
  warnings.filter((warning) => typeof warning === "string").forEach((warning) => {
    const text = document.createElement("p");
    text.className = "candidate-warning";
    text.textContent = displayWarning(warning);
    container.append(text);
  });
}

function setGenerationBusy(active) {
  candidateGenerationActive = active;
  referenceFrameForm.setGenerationActive(active);
  generateButton.textContent = active ? "후보를 생성하는 중…" : "후보 생성";
  generationProgress.hidden = !active;
  generationSpinner.hidden = !active;
  candidateForm.setAttribute("aria-busy", String(active));
}

function formatOffset(offsetSeconds) {
  if (offsetSeconds === 0) {
    return "기준 위치";
  }
  return `${offsetSeconds > 0 ? "+" : ""}${offsetSeconds}초`;
}

function isSupportedImageUrl(value) {
  return typeof value === "string" && value.startsWith("/api/v1/reference-frames/");
}

function appendCandidateImage(card, candidate) {
  const frame = document.createElement("div");
  frame.className = "candidate-media";
  if (candidate.status !== "succeeded" || !isSupportedImageUrl(candidate.reference_frame.image_url)) {
    const unavailable = document.createElement("p");
    unavailable.className = "candidate-thumbnail-placeholder";
    unavailable.textContent = "이 후보의 미리보기를 사용할 수 없습니다.";
    unavailable.setAttribute("role", "status");
    frame.append(unavailable);
    card.append(frame);
    return { image: null, placeholder: unavailable };
  }

  const image = document.createElement("img");
  image.className = "candidate-thumbnail";
  image.src = candidate.reference_frame.image_url;
  image.alt = `${formatOffset(candidate.offset_seconds)}의 녹화 프레임 후보.`;
  image.loading = "lazy";
  const dimensions = candidate.reference_frame.image;
  if (dimensions && Number.isInteger(dimensions.width) && Number.isInteger(dimensions.height)) {
    image.width = dimensions.width;
    image.height = dimensions.height;
  }

  const unavailable = document.createElement("p");
  unavailable.className = "candidate-thumbnail-placeholder";
  unavailable.textContent = "미리보기를 사용할 수 없습니다.";
  unavailable.hidden = true;
  unavailable.setAttribute("role", "status");
  image.addEventListener("error", () => {
    image.hidden = true;
    unavailable.hidden = false;
  }, { once: true });
  frame.append(image, unavailable);
  card.append(frame);
  return { image, placeholder: unavailable };
}

function renderCandidate(candidate) {
  const card = document.createElement("li");
  card.className = "candidate-row";
  card.dataset.status = candidate.status;

  const media = appendCandidateImage(card, candidate);
  const details = document.createElement("div");
  details.className = "candidate-details";
  const heading = document.createElement("h3");
  heading.className = "candidate-heading";
  heading.textContent = formatOffset(candidate.offset_seconds);
  details.append(heading);
  referenceFrameSelection.attachCandidate(candidate, card, details, media.image, media.placeholder);
  const facts = document.createElement("dl");
  facts.className = "candidate-facts";
  appendFact(facts, "요청 위치", `${candidate.offset_seconds}초`, true);
  appendFact(facts, "요청한 UTC 시각", candidate.candidate_requested_time_utc, true);

  if (candidate.status === "succeeded") {
    appendFact(facts, "상태", `성공, ${displayOutcome(candidate.outcome)}`);
    appendFact(facts, "시각 정밀도", displayTimingPrecision(candidate.reference_frame.timing.precision_status));
  } else {
    appendFact(facts, "상태", "실패");
    appendFact(facts, `실패: ${displayFailureCode(candidate.failure.code)}`, displayFailureMessage(candidate.failure));
  }
  details.append(facts);
  appendWarnings(details, candidate.status === "succeeded" ? candidate.reference_frame.warnings : candidate.warnings);
  card.append(details);
  candidateResults.append(card);
}

function isCandidate(candidate) {
  if (!candidate || typeof candidate !== "object" || !Number.isInteger(candidate.offset_seconds)) {
    return false;
  }
  if (typeof candidate.candidate_requested_time_utc !== "string") {
    return false;
  }
  if (candidate.status === "failed") {
    return candidate.failure && typeof candidate.failure.code === "string" && typeof candidate.failure.message === "string";
  }
  return candidate.status === "succeeded"
    && (candidate.outcome === "created" || candidate.outcome === "reused")
    && candidate.reference_frame
    && isSupportedImageUrl(candidate.reference_frame.image_url)
    && candidate.reference_frame.timing
    && typeof candidate.reference_frame.timing.precision_status === "string";
}

function isCandidateSet(candidateSet) {
  return candidateSet
    && typeof candidateSet === "object"
    && Array.isArray(candidateSet.candidates)
    && candidateSet.candidates.every(isCandidate)
    && candidateSet.summary
    && Number.isInteger(candidateSet.summary.created)
    && Number.isInteger(candidateSet.summary.reused)
    && Number.isInteger(candidateSet.summary.failed);
}

function renderCandidateSet(candidateSet) {
  if (!isCandidateSet(candidateSet)) {
    renderRequestError();
    return;
  }

  candidateSet.candidates.forEach(renderCandidate);
  if (candidateSet.candidates.length === 0) {
    setRequestState("empty", "후보 위치가 반환되지 않았습니다.");
    return;
  }
  const { created, reused, failed } = candidateSet.summary;
  const succeeded = created + reused;
  if (failed === 0) {
    setRequestState("success", `후보 요청 ${succeeded}개를 완료했습니다.`);
  } else if (succeeded === 0) {
    setRequestState("all-failed", "이 유효한 요청에서 사용할 수 있는 후보 미디어가 없습니다.");
  } else {
    setRequestState("partial", `후보 요청 ${succeeded}개를 완료했고 ${failed}개는 안전하게 실패했습니다.`);
  }
}

async function submitCandidateRequest(event) {
  event.preventDefault();

  if (!candidateForm.reportValidity()) {
    setRequestState("error", "양의 정수 채널과 적용된 올바른 시각을 입력하세요.");
    return;
  }
  if (!referenceFrameForm.isReady()) {
    setRequestState("error", "후보를 생성하기 전에 날짜와 시각을 적용하세요.");
    return;
  }

  const sequence = ++requestSequence;
  const context = currentCandidateRequestContext();
  if (context === null) {
    return;
  }
  clearResults();
  setGenerationBusy(true);
  setRequestState("loading", "후보 요청을 생성하는 중입니다. 몇 분 정도 걸릴 수 있습니다.");

  try {
    const response = await fetch("/api/v1/reference-frame-candidate-sets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        channel_id: Number(channelIdInput.value),
        ...referenceFrameForm.getRequestPayload(),
      }),
    });
    if (!response.ok) {
      if (isCurrentCandidateRequest(sequence, context)) {
        renderRequestError();
      }
      return;
    }
    const candidateSet = await response.json();
    if (isCurrentCandidateRequest(sequence, context)) {
      renderCandidateSet(candidateSet);
    }
  } catch {
    if (isCurrentCandidateRequest(sequence, context)) {
      renderRequestError();
    }
  } finally {
    if (isCurrentCandidateRequest(sequence, context)) {
      setGenerationBusy(false);
    }
  }
}

candidateForm.addEventListener("submit", submitCandidateRequest);
channelIdInput.addEventListener("change", () => {
  channelSelectionExplicit = /^\d+$/.test(channelIdInput.value)
    && Number(channelIdInput.value) > 0;
  transitionChannel("채널이 변경되어 후보를 다시 생성해야 합니다.");
});
window.vigiVisionReferenceFrameCandidates = Object.freeze({ invalidate: invalidateCandidateRequest });
window.vigiVisionReferenceFrameChannels = Object.freeze({ refresh: loadChannels });
void loadChannels();
