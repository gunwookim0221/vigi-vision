(function () {
const assistedButton = document.querySelector("#roi-assisted-button");
const assistedGuidance = document.querySelector("#roi-assisted-guidance");
const assistedMarker = document.querySelector("#roi-assisted-marker");
const assistedMask = document.querySelector("#roi-assisted-mask");
const roi = window.vigiVisionReferenceFrameRoi;
const requestBoundary = window.vigiVisionReferenceFrameAssistedRequest;
const ERROR_MESSAGES = requestBoundary.ERROR_MESSAGES;

let selectedCandidate = null;
let selectedImage = null;
let mode = "inactive";
let capabilityUnavailable = false;
let generation = 0;
let pending = null;
let armedPointerId = null;
let maskPreview = null;
let boundImage = null;
let boundImageHandlers = null;

function currentSourceSize() {
  const sourceSize = roi.getState().sourceSize;
  return sourceSize === null ? null : { ...sourceSize };
}

function selectedResourceId() {
  const resourceId = selectedCandidate?.reference_frame?.resource_id;
  return typeof resourceId === "string" && resourceId.length > 0 ? resourceId : null;
}

function setStatus(message, state = "ready") { roi.setStatus(message, state); }

function setGuidance(message) { assistedGuidance.textContent = message; }

function hideMarker() {
  assistedMarker.hidden = true;
  assistedMarker.style.left = "";
  assistedMarker.style.top = "";
}

function drawHorizontalBoundary(context, start, end, y, thickness, height) {
  const top = Math.max(0, y - Math.floor(thickness / 2));
  const bottom = Math.min(height, top + thickness);
  if (end > start && bottom > top) {
    context.fillRect(start, top, end - start, bottom - top);
  }
}

function drawExposedBoundary(context, start, end, neighbor, y, thickness, height) {
  let cursor = start;
  neighbor.forEach(([neighborStart, neighborEnd]) => {
    if (neighborEnd <= cursor || neighborStart >= end) {
      return;
    }
    if (neighborStart > cursor) {
      drawHorizontalBoundary(context, cursor, Math.min(neighborStart, end), y, thickness, height);
    }
    cursor = Math.max(cursor, neighborEnd);
  });
  if (cursor < end) {
    drawHorizontalBoundary(context, cursor, end, y, thickness, height);
  }
}

function drawVerticalBoundary(context, x, y, thickness, width) {
  const left = Math.max(0, x - Math.floor(thickness / 2));
  const right = Math.min(width, left + thickness);
  if (right > left) {
    context.fillRect(left, y, right - left, 1);
  }
}

function clearMaskPreview() {
  maskPreview = null;
  const context = assistedMask.getContext?.("2d");
  if (context !== null && context !== undefined) {
    context.clearRect(0, 0, assistedMask.width, assistedMask.height);
  }
  assistedMask.hidden = true;
  assistedMask.style.left = "";
  assistedMask.style.top = "";
  assistedMask.style.width = "";
  assistedMask.style.height = "";
  roi.setAssistedPreviewActive?.(false);
}

function renderMaskPreview() {
  if (maskPreview === null || selectedImage === null || !selectedImage.complete) {
    assistedMask.hidden = true;
    return;
  }
  const imageRect = selectedImage.getBoundingClientRect();
  const stageRect = document.querySelector("#roi-stage").getBoundingClientRect();
  const context = assistedMask.getContext?.("2d");
  if (context === null || context === undefined || imageRect.width <= 0 || imageRect.height <= 0) {
    clearMaskPreview();
    return;
  }
  assistedMask.width = maskPreview.width;
  assistedMask.height = maskPreview.height;
  assistedMask.style.left = `${imageRect.left - stageRect.left}px`;
  assistedMask.style.top = `${imageRect.top - stageRect.top}px`;
  assistedMask.style.width = `${imageRect.width}px`;
  assistedMask.style.height = `${imageRect.height}px`;
  context.clearRect(0, 0, assistedMask.width, assistedMask.height);
  const computed = typeof window.getComputedStyle === "function"
    ? window.getComputedStyle(document.documentElement).getPropertyValue("--roi-mask-fill").trim()
    : "";
  context.fillStyle = computed || "rgba(49, 91, 182, 0.38)";
  maskPreview.rows.forEach((row, y) => {
    row.forEach(([start, end]) => context.fillRect(start, y, end - start, 1));
  });
  const outline = typeof window.getComputedStyle === "function"
    ? window.getComputedStyle(document.documentElement).getPropertyValue("--roi-mask-outline").trim()
    : "";
  context.fillStyle = outline || "#f0bc68";
  const outlineThickness = Math.min(64, Math.max(1, Math.ceil(maskPreview.width / imageRect.width * 2)));
  maskPreview.rows.forEach((row, y) => {
    row.forEach(([start, end], index) => {
      const previous = row[index - 1];
      const next = row[index + 1];
      const above = y === 0 ? [] : maskPreview.rows[y - 1];
      const below = y + 1 === maskPreview.height ? [] : maskPreview.rows[y + 1];
      drawExposedBoundary(context, start, end, above, y, outlineThickness, maskPreview.height);
      drawExposedBoundary(context, start, end, below, y, outlineThickness, maskPreview.height);
      if (previous === undefined || previous[1] !== start) {
        drawVerticalBoundary(context, start, y, outlineThickness, maskPreview.width);
      }
      if (next === undefined || next[0] !== end) {
        drawVerticalBoundary(context, end - 1, y, outlineThickness, maskPreview.width);
      }
    });
  });
  assistedMask.hidden = false;
}

function setMaskPreview(nextPreview) {
  maskPreview = nextPreview;
  roi.setAssistedPreviewActive?.(true);
  renderMaskPreview();
}

function showMarker(displayPoint) {
  const imageRect = selectedImage.getBoundingClientRect();
  const stageRect = document.querySelector("#roi-stage").getBoundingClientRect();
  assistedMarker.hidden = false;
  assistedMarker.style.left = `${displayPoint.x + imageRect.left - stageRect.left}px`;
  assistedMarker.style.top = `${displayPoint.y + imageRect.top - stageRect.top}px`;
}

function abortPending() {
  if (pending?.controller !== null && pending?.controller !== undefined) {
    pending.controller.abort();
  }
  pending = null;
}

function detachImage() {
  if (boundImage !== null && boundImageHandlers !== null) {
    boundImage.removeEventListener("load", boundImageHandlers.load);
    boundImage.removeEventListener("error", boundImageHandlers.error);
  }
  boundImage = null;
  boundImageHandlers = null;
}

function bindImage(image) {
  detachImage();
  boundImage = image;
  boundImageHandlers = {
    load: () => {
      updateControl();
      renderMaskPreview();
    },
    error: () => {
      if (image === selectedImage) {
        updateControl();
      }
    },
  };
  image.addEventListener("load", boundImageHandlers.load);
  image.addEventListener("error", boundImageHandlers.error);
  if (image.complete) {
    updateControl();
  }
}

function updateControl() {
  const canSuggest = selectedResourceId() !== null
    && selectedImage !== null
    && currentSourceSize() !== null
    && !capabilityUnavailable;
  assistedButton.disabled = !canSuggest;
  assistedButton.textContent = mode === "inactive" ? "Tap to suggest ROI" : "Cancel assisted selection";
  assistedButton.setAttribute("aria-pressed", String(mode !== "inactive"));
  assistedButton.setAttribute("aria-label", mode === "inactive" ? "Tap to suggest ROI" : "Cancel assisted selection");
}

function isCurrentRequest(requestGeneration, resourceId, image) {
  return pending?.generation === requestGeneration
    && generation === requestGeneration
    && selectedResourceId() === resourceId
    && selectedImage === image;
}

function finishCurrentRequest() {
  pending = null;
  hideMarker();
  updateControl();
}

function completeSuccess(nextRoi) {
  roi.replaceCommittedRoi(nextRoi.roi, "Suggested ROI received. Verify and adjust it.");
  setMaskPreview(nextRoi.maskPreview);
  mode = "inactive";
  setGuidance("Suggested ROI received. Verify it, adjust it manually, or request another suggestion.");
  finishCurrentRequest();
}

function handleRequestError(code) {
  mode = "inactive";
  capabilityUnavailable = code === "suggestion_unavailable";
  setGuidance(ERROR_MESSAGES[code]);
  setStatus(ERROR_MESSAGES[code], capabilityUnavailable ? "unavailable" : "error");
  finishCurrentRequest();
}

function handleRequestFailure(kind) {
  mode = "inactive";
  const message = kind === "cancelled" ? "Suggestion cancelled. The previous ROI remains." : ERROR_MESSAGES.suggestion_failure;
  setGuidance(message);
  setStatus(message, kind === "cancelled" ? "warning" : "error");
  finishCurrentRequest();
}

function handleInvalidResponse() {
  mode = "inactive";
  setGuidance("The response could not be applied safely. Try tapping the object again or draw the ROI manually.");
  setStatus("Suggested ROI could not be applied safely. Try tapping the object again or draw the ROI manually.", "error");
  finishCurrentRequest();
}

function requestSuggestion(point, displayPoint) {
  const resourceId = selectedResourceId();
  const sourceSize = currentSourceSize();
  if (resourceId === null || sourceSize === null || selectedImage === null) {
    return;
  }
  abortPending();
  clearMaskPreview();
  const requestGeneration = ++generation;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  pending = { controller, generation: requestGeneration };
  mode = "active";
  showMarker(displayPoint);
  setGuidance("Requesting an automatic ROI suggestion. The previous ROI remains until a valid result arrives.");
  setStatus("Requesting an automatic ROI suggestion…", "loading");
  void requestBoundary.requestSuggestion({
    controller,
    image: selectedImage,
    isCurrent: () => isCurrentRequest(requestGeneration, resourceId, selectedImage),
    onError: handleRequestError,
    onFailure: handleRequestFailure,
    onInvalid: handleInvalidResponse,
    onSuccess: completeSuccess,
    point,
    resourceId,
    sourceSize,
  });
}

function toggleMode() {
  if (assistedButton.disabled) {
    return;
  }
  if (mode !== "inactive") {
    generation += 1;
    abortPending();
    window.vigiVisionReferenceFrameAssistedPointer?.reset();
    clearPointer(null);
    hideMarker();
    clearMaskPreview();
    mode = "inactive";
    setGuidance("Assisted selection cancelled. Draw the ROI manually or tap to try again.");
    setStatus("Assisted selection cancelled. The previous ROI remains.", "warning");
  } else {
    mode = "active";
    setGuidance("Tap the object to request an automatic ROI suggestion.");
    setStatus("Tap the object to request an automatic ROI suggestion.", "active");
  }
  updateControl();
}

function setSelectedCandidate(candidate, image) {
  generation += 1;
  abortPending();
  window.vigiVisionReferenceFrameAssistedPointer?.reset();
  clearPointer(null);
  hideMarker();
  clearMaskPreview();
  selectedCandidate = candidate;
  selectedImage = image;
  bindImage(image);
  mode = "inactive";
  capabilityUnavailable = false;
  setGuidance("Select Tap to suggest ROI, then tap the object in the displayed image.");
  setStatus("Select Tap to suggest ROI to request an automatic suggestion.");
  updateControl();
}

function reset(message = "Select a candidate first.") {
  generation += 1;
  abortPending();
  window.vigiVisionReferenceFrameAssistedPointer?.reset();
  clearPointer(null);
  hideMarker();
  clearMaskPreview();
  detachImage();
  selectedCandidate = null;
  selectedImage = null;
  mode = "inactive";
  capabilityUnavailable = false;
  setGuidance("Select a usable candidate to enable automatic ROI suggestions.");
  updateControl();
  if (message) {
    setStatus(message);
  }
}

function cancelPending() {
  generation += 1;
  abortPending();
  window.vigiVisionReferenceFrameAssistedPointer?.reset();
  clearPointer(null);
  hideMarker();
  clearMaskPreview();
  mode = "inactive";
  setGuidance("ROI reset. Draw a new region when ready, or tap to request a suggestion.");
  updateControl();
}

function isBlockingManualPointerInput() {
  return mode !== "inactive" || armedPointerId !== null || pending !== null;
}

function getPointerContext() {
  const sourceSize = currentSourceSize();
  return mode === "inactive" || selectedImage === null || sourceSize === null
    ? null
    : { image: selectedImage, sourceSize };
}

function armPointer(pointerId, displayPoint) {
  armedPointerId = pointerId;
  showMarker(displayPoint);
  setStatus("Tap registered. Release to request an automatic ROI suggestion.", "active");
}

function clearPointer(pointerId) {
  if (pointerId === null || pointerId === armedPointerId) {
    armedPointerId = null;
    hideMarker();
  }
}

function cancelTap() {
  hideMarker();
  setStatus("Tap cancelled. Try again or draw the ROI manually.", "warning");
}

function requestTap(point, displayPoint) {
  requestSuggestion(point, displayPoint);
}

function getState() {
  return {
    active: mode !== "inactive",
    capabilityUnavailable,
    pending: pending === null ? null : { generation: pending.generation },
    armedPointerId,
    markerHidden: assistedMarker.hidden,
    maskPreview: maskPreview === null ? null : { width: maskPreview.width, height: maskPreview.height },
    maskHidden: assistedMask.hidden,
    selectedResourceId: selectedResourceId(),
  };
}

assistedButton.addEventListener("click", toggleMode);
window.addEventListener("resize", renderMaskPreview);
window.addEventListener("pagehide", () => reset(""));

window.vigiVisionReferenceFrameAssistedRoi = Object.freeze({
  getState,
  armPointer,
  cancelTap,
  cancelPending,
  clearPointer,
  getPointerContext,
  isBlockingManualPointerInput,
  requestTap,
  reset,
  clearMaskPreview,
  setMaskPreview,
  setSelectedCandidate,
});
updateControl();
})();
