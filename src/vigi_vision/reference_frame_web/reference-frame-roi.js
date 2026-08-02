const roiStage = document.querySelector("#roi-stage");
const roiStatus = document.querySelector("#roi-status");
const committedOverlay = document.querySelector("#roi-committed-overlay");
const draftOverlay = document.querySelector("#roi-draft-overlay");
const roiSummary = document.querySelector("#roi-summary");
const resetButton = document.querySelector("#roi-reset");
const handleElements = Object.fromEntries(["nw", "n", "ne", "e", "se", "s", "sw", "w"].map((handle) => [handle, document.querySelector(`#roi-handle-${handle}`)]));
const summaryFields = { x: document.querySelector("#roi-summary-x"), y: document.querySelector("#roi-summary-y"), width: document.querySelector("#roi-summary-width"), height: document.querySelector("#roi-summary-height"), sourceWidth: document.querySelector("#roi-summary-source-width"), sourceHeight: document.querySelector("#roi-summary-source-height") };
const MINIMUM_ROI_WIDTH = 4;
const MINIMUM_ROI_HEIGHT = 4;
const roiGeometry = window.vigiVisionReferenceFrameRoiGeometry;
let roiSelectedCandidate = null;
let selectedImage = null;
let sourceSize = null;
let draftRoi = null;
let committedRoi = null;
let activePointerId = null;
let activeEdit = null;
let interactionOriginRoi = null;
let interactionOriginPoint = null;
let assistedPreviewActive = false;
let boundImage = null;
let boundImageHandlers = null;
const STATUS_STATES = new Set(["disabled", "ready", "active", "loading", "success", "warning", "error", "unavailable"]);
let statusState = "disabled";
function setStatus(message, state = "ready") {
  roiStatus.textContent = message;
  statusState = STATUS_STATES.has(state) ? state : "ready";
  roiStatus.dataset.state = statusState;
  roiStatus.setAttribute("aria-busy", String(statusState === "loading"));
}
function releasePointerCapture(pointerId) {
  if (typeof roiStage.releasePointerCapture !== "function") {
    return;
  }
  try {
    roiStage.releasePointerCapture(pointerId);
  } catch {
    return;
  }
}
function clearInteractionState() {
  const pointerId = activePointerId;
  activePointerId = activeEdit = interactionOriginRoi = interactionOriginPoint = draftRoi = null;
  if (pointerId !== null) {
    releasePointerCapture(pointerId);
  }
}
function copyRoi(roi) { return roi === null ? null : { ...roi }; }
function copyPoint(point) { return point === null ? null : { ...point }; }
function readSourceSize(image) {
  const width = image?.naturalWidth;
  const height = image?.naturalHeight;
  return Number.isInteger(width) && width > 0 && Number.isInteger(height) && height > 0 ? { width, height } : null;
}
function renderOverlay(overlay, roi, displayRect) {
  if (roi === null || displayRect.width <= 0 || displayRect.height <= 0) {
    overlay.hidden = true;
    return;
  }
  const displayRoi = roiGeometry.sourceRoiToDisplay(roi, displayRect);
  overlay.hidden = false;
  Object.assign(overlay.style, { left: `${displayRoi.left}px`, top: `${displayRoi.top}px`, width: `${displayRoi.width}px`, height: `${displayRoi.height}px` });
}
function renderSummary() {
  resetButton.disabled = committedRoi === null;
  if (committedRoi === null) {
    roiSummary.hidden = true;
    return;
  }
  roiSummary.hidden = false;
  Object.entries({ x: committedRoi.x, y: committedRoi.y, width: committedRoi.width, height: committedRoi.height, sourceWidth: committedRoi.source_width, sourceHeight: committedRoi.source_height }).forEach(([key, value]) => { summaryFields[key].textContent = String(value); });
}
function renderHandles(roi, displayRect) {
  const displayRoi = assistedPreviewActive || roi === null
    ? null
    : roiGeometry.sourceRoiToDisplay(roi, displayRect);
  const positions = displayRoi === null ? {} : Object.fromEntries([
    ["nw", [displayRoi.left, displayRoi.top]], ["n", [displayRoi.left + displayRoi.width / 2, displayRoi.top]], ["ne", [displayRoi.left + displayRoi.width, displayRoi.top]], ["e", [displayRoi.left + displayRoi.width, displayRoi.top + displayRoi.height / 2]],
    ["se", [displayRoi.left + displayRoi.width, displayRoi.top + displayRoi.height]], ["s", [displayRoi.left + displayRoi.width / 2, displayRoi.top + displayRoi.height]], ["sw", [displayRoi.left, displayRoi.top + displayRoi.height]], ["w", [displayRoi.left, displayRoi.top + displayRoi.height / 2]],
  ]);
  Object.entries(handleElements).forEach(([handle, element]) => {
    const position = positions[handle];
    element.hidden = position === undefined;
    element.setAttribute("aria-hidden", String(position === undefined));
    if (position !== undefined) Object.assign(element.style, { left: `${position[0]}px`, top: `${position[1]}px` });
  });
}
function render() {
  renderSummary();
  if (selectedImage === null || sourceSize === null) {
    [roiStage, committedOverlay, draftOverlay].forEach((element) => { element.hidden = true; });
    renderHandles(null, { width: 0, height: 0 });
    return;
  }
  roiStage.hidden = false;
  selectedImage.hidden = false;
  const displayRect = selectedImage.getBoundingClientRect();
  renderOverlay(committedOverlay, committedRoi, displayRect);
  renderOverlay(draftOverlay, draftRoi, displayRect);
  renderHandles(draftRoi ?? committedRoi, displayRect);
}
function detachImage() {
  if (boundImage !== null && boundImageHandlers !== null) {
    boundImage.removeEventListener("load", boundImageHandlers.load);
    boundImage.removeEventListener("error", boundImageHandlers.error);
  }
  boundImage = boundImageHandlers = null;
}
function activateImage(image) {
  if (image !== selectedImage) {
    return;
  }
  sourceSize = readSourceSize(image);
  if (sourceSize === null) {
    roiStage.hidden = true;
    setStatus("선택한 이미지의 크기를 확인할 수 없습니다.", "unavailable");
    render();
    return;
  }
  setStatus(committedRoi === null
    ? "준비되었습니다. 이미지에서 드래그하여 ROI 하나를 그리세요."
    : "ROI가 적용되었습니다. 다시 드래그하여 바꿀 수 있습니다.", committedRoi === null ? "ready" : "success");
  render();
}
function setSelectedCandidate(candidate, image) {
  reset("선택한 후보 이미지를 불러오는 중입니다.", "loading");
  roiSelectedCandidate = candidate;
  selectedImage = image;
  boundImage = image;
  boundImageHandlers = {
    load: () => activateImage(image),
    error: () => {
      if (image === selectedImage) {
        reset("선택한 후보 이미지를 사용할 수 없습니다.", "unavailable");
      }
    },
  };
  image.addEventListener("load", boundImageHandlers.load);
  image.addEventListener("error", boundImageHandlers.error);
  if (image.complete) {
    activateImage(image);
  }
  render();
}
function reset(message = "먼저 후보를 선택하세요.", state = "disabled") {
  clearInteractionState();
  detachImage();
  roiSelectedCandidate = selectedImage = sourceSize = committedRoi = null;
  assistedPreviewActive = false;
  render();
  setStatus(message, state);
}
function clearRoi(message = "ROI를 초기화했습니다. 준비되면 새 영역을 그리세요.", state = "ready") {
  clearInteractionState();
  committedRoi = null;
  assistedPreviewActive = false;
  render();
  setStatus(message, state);
}
function beginInteraction(pointerId, mode, handle, originPoint) {
  activePointerId = pointerId;
  activeEdit = { mode, handle };
  interactionOriginRoi = copyRoi(committedRoi);
  interactionOriginPoint = copyPoint(originPoint);
  draftRoi = copyRoi(committedRoi);
  render();
}
function setDraftRoi(nextRoi) {
  draftRoi = copyRoi(nextRoi);
  render();
}
function commitInteraction(message, state = "success") {
  const nextRoi = copyRoi(draftRoi);
  clearInteractionState();
  if (nextRoi !== null && nextRoi.width >= MINIMUM_ROI_WIDTH && nextRoi.height >= MINIMUM_ROI_HEIGHT) {
    committedRoi = nextRoi;
    setStatus(message, state);
  }
  render();
}
function cancelInteraction(message, state = "warning") {
  clearInteractionState();
  setStatus(message, state);
  render();
}
function replaceCommittedRoi(nextRoi, message, state = "success") {
  clearInteractionState();
  committedRoi = copyRoi(nextRoi);
  setStatus(message, state);
  render();
}
function setAssistedPreviewActive(active) {
  assistedPreviewActive = active;
  roiStage.dataset.assisted = String(active);
  render();
}
function deepFreeze(value) {
  Object.freeze(value);
  Object.values(value).forEach((child) => {
    if (child !== null && typeof child === "object" && !Object.isFrozen(child)) deepFreeze(child);
  });
  return value;
}
function getPhase6Snapshot() {
  const candidateId = roiSelectedCandidate?.reference_frame?.resource_id;
  if (typeof candidateId !== "string" || candidateId.length === 0 || committedRoi === null || sourceSize === null) {
    return null;
  }
  return deepFreeze({ candidateId, sourceWidth: sourceSize.width, sourceHeight: sourceSize.height, roi: { x: committedRoi.x, y: committedRoi.y, width: committedRoi.width, height: committedRoi.height } });
}

if (typeof window.addEventListener === "function") {
  window.addEventListener("resize", render);
}
if (typeof window.ResizeObserver === "function") {
  const observer = new window.ResizeObserver(render);
  observer.observe(roiStage);
}

const referenceFrameRoi = Object.freeze({
  getState() {
    return { activePointerId, activeEdit: activeEdit === null ? null : { ...activeEdit }, assistedPreviewActive, committedRoi: copyRoi(committedRoi), draftRoi: copyRoi(draftRoi), interactionOriginPoint: copyPoint(interactionOriginPoint), interactionOriginRoi: copyRoi(interactionOriginRoi), selectedCandidate: roiSelectedCandidate, sourceSize: sourceSize === null ? null : { ...sourceSize }, statusState };
  },
  moveRoi: roiGeometry.moveRoi, normalizeRoi: roiGeometry.normalizeRoi, pointToSource: roiGeometry.pointToSource,
  pointInRoi: roiGeometry.pointInRoi, beginInteraction, cancelInteraction, clearRoi, commitInteraction, getPhase6Snapshot,
  reset, replaceCommittedRoi, setAssistedPreviewActive, setDraftRoi, setStatus, setSelectedCandidate, resizeHandleAt: roiGeometry.resizeHandleAt,
  resizeRoi: roiGeometry.resizeRoi, sourceRoiToDisplay: roiGeometry.sourceRoiToDisplay,
});

window.vigiVisionReferenceFrameRoi = referenceFrameRoi;
