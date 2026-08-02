const selectionStatus = document.querySelector("#selection-status");
const selectedPreviewContent = document.querySelector("#selected-preview-content");
const selectedPreviewImage = document.querySelector("#selected-preview-image");
const selectedPreviewFacts = document.querySelector("#selected-preview-facts");
const selectedPreviewWarnings = document.querySelector("#selected-preview-warnings");

let selectedCandidate = null;
let selectedView = null;
let candidateViews = new WeakMap();
let previewSequence = 0;

function resetRoi(message) {
  const roi = window.vigiVisionReferenceFrameRoi;
  if (roi) {
    roi.reset(message);
  }
}

function resetAssistedRoi(message) {
  const assistedRoi = window.vigiVisionReferenceFrameAssistedRoi;
  if (assistedRoi) {
    assistedRoi.reset(message);
  }
}

function isSelectableCandidate(candidate) {
  const frame = candidate.reference_frame;
  return candidate.status === "succeeded"
    && typeof frame.resource_id === "string"
    && frame.resource_id.length > 0
    && typeof frame.image_url === "string"
    && frame.image_url.startsWith("/api/v1/reference-frames/")
    && frame.image
    && Number.isInteger(frame.image.width)
    && frame.image.width > 0
    && Number.isInteger(frame.image.height)
    && frame.image.height > 0;
}

function appendPreviewFact(label, value, isMono = false) {
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
  selectedPreviewFacts.append(fact);
}

function clearPreview(message = "선택한 후보가 없습니다.") {
  previewSequence += 1;
  selectedPreviewContent.hidden = true;
  selectedPreviewImage.hidden = true;
  selectedPreviewImage.removeAttribute("src");
  selectedPreviewImage.alt = "";
  selectedPreviewImage.onerror = null;
  selectedPreviewImage.onload = null;
  selectedPreviewFacts.replaceChildren();
  selectedPreviewWarnings.replaceChildren();
  selectionStatus.textContent = message;
}

function clearSelection(message = "선택한 후보가 없습니다.") {
  if (selectedView) {
    selectedView.card.dataset.selected = "false";
    selectedView.control.checked = false;
    if (!selectedView.unavailable) {
      selectedView.status.textContent = "선택할 수 있습니다.";
    }
  }
  selectedCandidate = null;
  selectedView = null;
  clearPreview(message);
  resetAssistedRoi(message);
  resetRoi(message);
}

function renderPreview(candidate) {
  const frame = candidate.reference_frame;
  const sequence = ++previewSequence;
  selectedPreviewImage.src = frame.image_url;
  selectedPreviewImage.alt = `${formatOffset(candidate.offset_seconds)}의 선택한 녹화 프레임 후보.`;
  selectedPreviewImage.width = frame.image.width;
  selectedPreviewImage.height = frame.image.height;
  selectedPreviewImage.hidden = false;
  selectedPreviewImage.onload = () => {
    if (sequence === previewSequence && selectedCandidate === candidate) {
      selectedPreviewContent.hidden = false;
    }
  };
  selectedPreviewImage.onerror = () => {
    if (sequence === previewSequence && selectedCandidate === candidate) {
      markUnavailable(candidate);
    }
  };
  selectedPreviewFacts.replaceChildren();
  appendPreviewFact("리소스 ID", frame.resource_id, true);
  appendPreviewFact("요청 위치", `${candidate.offset_seconds}초`, true);
  appendPreviewFact("요청한 UTC 시각", candidate.candidate_requested_time_utc, true);
  appendPreviewFact("시각 정밀도", displayTimingPrecision(frame.timing.precision_status));
  selectedPreviewWarnings.replaceChildren();
  appendWarnings(selectedPreviewWarnings, frame.warnings);
  selectedPreviewContent.hidden = false;
  selectionStatus.textContent = `선택한 후보: ${formatOffset(candidate.offset_seconds)}.`;
}

function selectCandidate(candidate, view) {
  if (!view.control || view.control.disabled || view.unavailable) {
    return;
  }
  if (selectedView && selectedView !== view) {
    selectedView.card.dataset.selected = "false";
    selectedView.control.checked = false;
    if (!selectedView.unavailable) {
      selectedView.status.textContent = "선택할 수 있습니다.";
    }
  }
  selectedCandidate = candidate;
  selectedView = view;
  selectedView.card.dataset.selected = "true";
  selectedView.status.textContent = "선택한 후보입니다.";
  selectedView.control.checked = true;
  renderPreview(candidate);
  const roi = window.vigiVisionReferenceFrameRoi;
  if (roi) {
    roi.setSelectedCandidate(candidate, selectedPreviewImage);
  }
  const assistedRoi = window.vigiVisionReferenceFrameAssistedRoi;
  if (assistedRoi) {
    assistedRoi.setSelectedCandidate(candidate, selectedPreviewImage);
  }
}

function markUnavailable(candidate) {
  const view = candidateViews.get(candidate);
  if (!view || view.unavailable) {
    return;
  }
  view.unavailable = true;
  view.image.hidden = true;
  view.placeholder.hidden = false;
  view.card.dataset.selectable = "false";
  if (view.control) {
    view.control.disabled = true;
    view.control.checked = false;
  }
  view.status.textContent = "미리보기를 사용할 수 없어 선택할 수 없습니다.";
  if (selectedCandidate === candidate) {
    clearSelection("선택한 후보를 사용할 수 없습니다.");
  }
}

function attachCandidate(candidate, card, details, image, placeholder) {
  if (candidate.status !== "succeeded" || !image) {
    return;
  }
  const view = {
    candidate,
    card,
    image,
    placeholder,
    control: null,
    status: null,
    unavailable: false,
  };
  candidateViews.set(candidate, view);
  if (isSelectableCandidate(candidate)) {
    const label = document.createElement("label");
    label.className = "candidate-select";
    const control = document.createElement("input");
    control.type = "radio";
    control.name = "reference-frame-candidate";
    control.value = candidate.reference_frame.resource_id;
    control.disabled = true;
    control.setAttribute("aria-label", `${formatOffset(candidate.offset_seconds)} 후보 선택`);
    const labelText = document.createElement("span");
    labelText.textContent = "후보 선택";
    label.append(control, labelText);
    details.append(label);
    const status = document.createElement("p");
    status.className = "candidate-selection-status";
    status.setAttribute("role", "status");
    status.textContent = "미리보기를 불러오는 중입니다.";
    details.append(status);
    view.control = control;
    view.status = status;
    control.addEventListener("change", () => selectCandidate(candidate, view));
  } else {
    const status = document.createElement("p");
    status.className = "candidate-selection-status";
    status.setAttribute("role", "status");
    status.textContent = "선택할 수 없습니다.";
    details.append(status);
    view.status = status;
  }
  image.addEventListener("load", () => {
    if (view.control && !view.unavailable) {
      view.control.disabled = false;
      view.status.textContent = "선택할 수 있습니다.";
    }
  }, { once: true });
  image.addEventListener("error", () => markUnavailable(candidate), { once: true });
}

function resetSelection(message = "선택한 후보가 없습니다.") {
  clearSelection(message);
  candidateViews = new WeakMap();
}

function getSelectedCandidate() {
  return selectedCandidate;
}

const referenceFrameSelection = Object.freeze({
  attachCandidate,
  getSelectedCandidate,
  reset: resetSelection,
});

window.vigiVisionReferenceFrameSelection = Object.freeze({ getSelectedCandidate });
