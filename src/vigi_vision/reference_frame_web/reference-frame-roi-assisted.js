(function () {
const assistedButton = document.querySelector("#roi-assisted-button");
const assistedSpinner = document.querySelector("#roi-assisted-spinner");
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

function setRequestBusy(active) {
  assistedSpinner.hidden = !active;
  assistedSpinner.setAttribute("aria-hidden", String(!active));
}

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
  assistedButton.textContent = mode === "inactive" ? "ROI 자동 제안" : "ROI 자동 제안 취소";
  assistedButton.setAttribute("aria-pressed", String(mode !== "inactive"));
  assistedButton.setAttribute("aria-label", mode === "inactive" ? "ROI 자동 제안" : "ROI 자동 제안 취소");
}

function isCurrentRequest(requestGeneration, resourceId, image) {
  return pending?.generation === requestGeneration
    && generation === requestGeneration
    && selectedResourceId() === resourceId
    && selectedImage === image;
}

function finishCurrentRequest() {
  pending = null;
  setRequestBusy(false);
  hideMarker();
  updateControl();
}

function completeSuccess(nextRoi) {
  roi.replaceCommittedRoi(nextRoi.roi, "ROI 자동 제안을 받았습니다. 확인하고 수동 조정하세요.");
  setMaskPreview(nextRoi.maskPreview);
  mode = "inactive";
  setGuidance("ROI 자동 제안을 받았습니다. 확인하거나 수동 조정하고, 다른 제안을 요청할 수 있습니다.");
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
  const message = kind === "cancelled" ? "제안을 취소했습니다. 이전 ROI를 유지합니다." : ERROR_MESSAGES.suggestion_failure;
  setGuidance(message);
  setStatus(message, kind === "cancelled" ? "warning" : "error");
  finishCurrentRequest();
}

function handleInvalidResponse() {
  mode = "inactive";
  setGuidance("응답을 안전하게 적용할 수 없습니다. 대상을 다시 누르거나 ROI를 수동 조정하세요.");
  setStatus("ROI 자동 제안 응답을 안전하게 적용할 수 없습니다. 대상을 다시 누르거나 ROI를 수동 조정하세요.", "error");
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
  setRequestBusy(true);
  setGuidance("ROI 자동 제안을 요청하는 중입니다. 유효한 결과가 올 때까지 이전 ROI를 유지합니다.");
  setStatus("ROI 자동 제안을 요청하는 중입니다…", "loading");
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
    setRequestBusy(false);
    mode = "inactive";
    setGuidance("ROI 자동 제안을 취소했습니다. ROI를 수동 조정하거나 다시 눌러 보세요.");
    setStatus("ROI 자동 제안을 취소했습니다. 이전 ROI를 유지합니다.", "warning");
  } else {
    mode = "active";
    setGuidance("대상을 눌러 ROI 자동 제안을 요청하세요.");
    setStatus("대상을 눌러 ROI 자동 제안을 요청하세요.", "active");
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
  setRequestBusy(false);
  selectedCandidate = candidate;
  selectedImage = image;
  bindImage(image);
  mode = "inactive";
  capabilityUnavailable = false;
  setGuidance("ROI 자동 제안을 선택한 다음 표시된 이미지에서 대상을 누르세요.");
  setStatus("ROI 자동 제안을 선택하면 자동 제안을 요청할 수 있습니다.");
  updateControl();
}

function reset(message = "먼저 후보를 선택하세요.") {
  generation += 1;
  abortPending();
  window.vigiVisionReferenceFrameAssistedPointer?.reset();
  clearPointer(null);
  hideMarker();
  clearMaskPreview();
  setRequestBusy(false);
  detachImage();
  selectedCandidate = null;
  selectedImage = null;
  mode = "inactive";
  capabilityUnavailable = false;
  setGuidance("사용 가능한 후보를 선택하면 ROI 자동 제안을 사용할 수 있습니다.");
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
  setRequestBusy(false);
  mode = "inactive";
  setGuidance("ROI를 초기화했습니다. 준비되면 새 영역을 그리거나 눌러 제안을 요청하세요.");
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
  setStatus("탭을 등록했습니다. 손을 떼면 ROI 자동 제안을 요청합니다.", "active");
}

function clearPointer(pointerId) {
  if (pointerId === null || pointerId === armedPointerId) {
    armedPointerId = null;
    hideMarker();
  }
}

function cancelTap() {
  hideMarker();
  setStatus("탭을 취소했습니다. 다시 시도하거나 ROI를 수동 조정하세요.", "warning");
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
