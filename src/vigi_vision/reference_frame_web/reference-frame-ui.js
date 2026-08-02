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

const SOURCE_TIMESTAMP_WARNING =
  "Source timestamp mapping is unavailable pending real-NVR replay validation.";
const USER_SOURCE_TIMESTAMP_LIMITATION =
  "Exact source timestamp is not yet verified. The displayed time is the requested position.";
const CHANNEL_DISCOVERY_FAILURE = "Channel list could not be loaded. Try again later.";

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

function replaceChannelOptions(channels, selectedId) {
  channelIdInput.replaceChildren();
  channels.forEach((channel) => {
    const option = document.createElement("option");
    option.value = String(channel.channel_id);
    option.textContent = channelLabel(channel);
    channelIdInput.append(option);
  });
  channelIdInput.value = selectedId === null ? "" : String(selectedId);
  channelIdInput.disabled = channels.length === 0;
  referenceFrameForm.refresh();
}

async function loadChannels() {
  const sequence = ++channelRequestSequence;
  const currentValue = channelIdInput.value;
  const hadKnownOptions = Array.from(channelIdInput.children).some((option) =>
    /^\d+$/.test(option.value) && Number(option.value) > 0);
  channelStatus.textContent = "Loading channels…";
  if (!hadKnownOptions) {
    channelIdInput.disabled = true;
  }
  try {
    const response = await fetch("/api/v1/reference-frames/channels");
    if (!response.ok) {
      throw new Error("channel discovery failed");
    }
    const payload = await response.json();
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
      ? `${channels.length} online channel${channels.length === 1 ? "" : "s"} available.`
      : "No online channels are available.";
  } catch {
    if (sequence !== channelRequestSequence) {
      return;
    }
    if (!hadKnownOptions) {
      replaceChannelOptions([], null);
    } else {
      channelIdInput.disabled = channelIdInput.value.length === 0;
      referenceFrameForm.refresh();
    }
    channelStatus.textContent = CHANNEL_DISCOVERY_FAILURE;
  }
}

function setRequestState(state, message) {
  requestStatus.dataset.state = state;
  requestStatus.textContent = message;
}

function clearResults() {
  referenceFrameSelection.reset();
  requestError.hidden = true;
  requestError.textContent = "";
  candidateResults.replaceChildren();
}

function renderRequestError(message = "The request could not be completed. Check the channel and recorded time, then try again.") {
  referenceFrameSelection.reset();
  requestError.hidden = false;
  requestError.textContent = message;
  setRequestState("error", "Candidate request was not completed.");
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
    text.textContent = warning === SOURCE_TIMESTAMP_WARNING
      ? USER_SOURCE_TIMESTAMP_LIMITATION
      : warning;
    container.append(text);
  });
}

function setGenerationBusy(active) {
  referenceFrameForm.setGenerationActive(active);
  generateButton.textContent = active ? "Generating candidates…" : "Generate candidates";
  generationProgress.hidden = !active;
  generationSpinner.hidden = !active;
  candidateForm.setAttribute("aria-busy", String(active));
}

function formatOffset(offsetSeconds) {
  if (offsetSeconds === 0) {
    return "Reference";
  }
  return `${offsetSeconds > 0 ? "+" : ""}${offsetSeconds} sec`;
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
    unavailable.textContent = "Preview unavailable for this candidate.";
    unavailable.setAttribute("role", "status");
    frame.append(unavailable);
    card.append(frame);
    return { image: null, placeholder: unavailable };
  }

  const image = document.createElement("img");
  image.className = "candidate-thumbnail";
  image.src = candidate.reference_frame.image_url;
  image.alt = `Recorded frame candidate at ${formatOffset(candidate.offset_seconds)}.`;
  image.loading = "lazy";
  const dimensions = candidate.reference_frame.image;
  if (dimensions && Number.isInteger(dimensions.width) && Number.isInteger(dimensions.height)) {
    image.width = dimensions.width;
    image.height = dimensions.height;
  }

  const unavailable = document.createElement("p");
  unavailable.className = "candidate-thumbnail-placeholder";
  unavailable.textContent = "Preview unavailable.";
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
  appendFact(facts, "Requested position", `${candidate.offset_seconds}s`, true);
  appendFact(facts, "Requested UTC time", candidate.candidate_requested_time_utc, true);

  if (candidate.status === "succeeded") {
    appendFact(facts, "Status", `Succeeded, ${candidate.outcome}`);
    appendFact(facts, "Timing precision", candidate.reference_frame.timing.precision_status);
  } else {
    appendFact(facts, "Status", "Failed");
    appendFact(facts, `Failure: ${candidate.failure.code}`, candidate.failure.message);
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
    setRequestState("empty", "No candidate positions were returned.");
    return;
  }
  const { created, reused, failed } = candidateSet.summary;
  const succeeded = created + reused;
  if (failed === 0) {
    setRequestState("success", `Completed ${succeeded} candidate requests.`);
  } else if (succeeded === 0) {
    setRequestState("all-failed", "No candidate media was available for this valid request.");
  } else {
    setRequestState("partial", `${succeeded} candidate requests completed; ${failed} failed safely.`);
  }
}

async function submitCandidateRequest(event) {
  event.preventDefault();

  if (!candidateForm.reportValidity()) {
    setRequestState("error", "Enter a positive channel ID and a valid applied time.");
    return;
  }
  if (!referenceFrameForm.isReady()) {
    setRequestState("error", "Apply date and time before generating candidates.");
    return;
  }

  const sequence = ++requestSequence;
  clearResults();
  setGenerationBusy(true);
  setRequestState("loading", "Generating candidate requests. This may take a few minutes.");

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
      if (sequence === requestSequence) {
        renderRequestError();
      }
      return;
    }
    const candidateSet = await response.json();
    if (sequence === requestSequence) {
      renderCandidateSet(candidateSet);
    }
  } catch {
    if (sequence === requestSequence) {
      renderRequestError();
    }
  } finally {
    if (sequence === requestSequence) {
      setGenerationBusy(false);
    }
  }
}

candidateForm.addEventListener("submit", submitCandidateRequest);
channelIdInput.addEventListener("change", () => {
  channelSelectionExplicit = /^\d+$/.test(channelIdInput.value)
    && Number(channelIdInput.value) > 0;
});
window.vigiVisionReferenceFrameChannels = Object.freeze({ refresh: loadChannels });
void loadChannels();
