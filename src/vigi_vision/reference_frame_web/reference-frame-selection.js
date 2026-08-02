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

function clearPreview(message = "No candidate selected.") {
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

function clearSelection(message = "No candidate selected.") {
  if (selectedView) {
    selectedView.card.dataset.selected = "false";
    selectedView.control.checked = false;
    if (!selectedView.unavailable) {
      selectedView.status.textContent = "Ready to select.";
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
  selectedPreviewImage.alt = `Selected recorded frame candidate at ${formatOffset(candidate.offset_seconds)}.`;
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
  appendPreviewFact("Resource ID", frame.resource_id, true);
  appendPreviewFact("Requested position", `${candidate.offset_seconds}s`, true);
  appendPreviewFact("Requested UTC time", candidate.candidate_requested_time_utc, true);
  appendPreviewFact("Timing precision", frame.timing.precision_status);
  selectedPreviewWarnings.replaceChildren();
  appendWarnings(selectedPreviewWarnings, frame.warnings);
  selectedPreviewContent.hidden = false;
  selectionStatus.textContent = `Selected candidate: ${formatOffset(candidate.offset_seconds)}.`;
}

function selectCandidate(candidate, view) {
  if (!view.control || view.control.disabled || view.unavailable) {
    return;
  }
  if (selectedView && selectedView !== view) {
    selectedView.card.dataset.selected = "false";
    selectedView.control.checked = false;
    if (!selectedView.unavailable) {
      selectedView.status.textContent = "Ready to select.";
    }
  }
  selectedCandidate = candidate;
  selectedView = view;
  selectedView.card.dataset.selected = "true";
  selectedView.status.textContent = "Selected candidate.";
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
  view.status.textContent = "Preview unavailable; selection disabled.";
  if (selectedCandidate === candidate) {
    clearSelection("Selected candidate is unavailable.");
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
    control.setAttribute("aria-label", `Select ${formatOffset(candidate.offset_seconds)} candidate`);
    const labelText = document.createElement("span");
    labelText.textContent = "Select candidate";
    label.append(control, labelText);
    details.append(label);
    const status = document.createElement("p");
    status.className = "candidate-selection-status";
    status.setAttribute("role", "status");
    status.textContent = "Loading preview.";
    details.append(status);
    view.control = control;
    view.status = status;
    control.addEventListener("change", () => selectCandidate(candidate, view));
  } else {
    const status = document.createElement("p");
    status.className = "candidate-selection-status";
    status.setAttribute("role", "status");
    status.textContent = "Unavailable for selection.";
    details.append(status);
    view.status = status;
  }
  image.addEventListener("load", () => {
    if (view.control && !view.unavailable) {
      view.control.disabled = false;
      view.status.textContent = "Ready to select.";
    }
  }, { once: true });
  image.addEventListener("error", () => markUnavailable(candidate), { once: true });
}

function resetSelection(message = "No candidate selected.") {
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
