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
let boundImage = null;
let boundImageHandlers = null;
function setStatus(message) {
  roiStatus.textContent = message;
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
  const displayRoi = roi === null ? null : roiGeometry.sourceRoiToDisplay(roi, displayRect);
  const positions = displayRoi === null ? {} : Object.fromEntries([
    ["nw", [displayRoi.left, displayRoi.top]], ["n", [displayRoi.left + displayRoi.width / 2, displayRoi.top]], ["ne", [displayRoi.left + displayRoi.width, displayRoi.top]], ["e", [displayRoi.left + displayRoi.width, displayRoi.top + displayRoi.height / 2]],
    ["se", [displayRoi.left + displayRoi.width, displayRoi.top + displayRoi.height]], ["s", [displayRoi.left + displayRoi.width / 2, displayRoi.top + displayRoi.height]], ["sw", [displayRoi.left, displayRoi.top + displayRoi.height]], ["w", [displayRoi.left, displayRoi.top + displayRoi.height / 2]],
  ]);
  Object.entries(handleElements).forEach(([handle, element]) => {
    const position = positions[handle];
    element.hidden = position === undefined;
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
    setStatus("Selected image dimensions are unavailable.");
    render();
    return;
  }
  setStatus(committedRoi === null
    ? "Ready. Drag on the image to draw one ROI."
    : "ROI committed. Drag again to replace it.");
  render();
}
function setSelectedCandidate(candidate, image) {
  reset("Loading selected candidate image.");
  roiSelectedCandidate = candidate;
  selectedImage = image;
  boundImage = image;
  boundImageHandlers = {
    load: () => activateImage(image),
    error: () => {
      if (image === selectedImage) {
        reset("Selected candidate image is unavailable.");
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
function reset(message = "Select a candidate first.") {
  clearInteractionState();
  detachImage();
  roiSelectedCandidate = selectedImage = sourceSize = committedRoi = null;
  render();
  setStatus(message);
}
function clearRoi(message = "ROI reset. Draw a new region when ready.") {
  clearInteractionState();
  committedRoi = null;
  render();
  setStatus(message);
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
function commitInteraction(message) {
  const nextRoi = copyRoi(draftRoi);
  clearInteractionState();
  if (nextRoi !== null && nextRoi.width >= MINIMUM_ROI_WIDTH && nextRoi.height >= MINIMUM_ROI_HEIGHT) {
    committedRoi = nextRoi;
    setStatus(message);
  }
  render();
}
function cancelInteraction(message) {
  clearInteractionState();
  setStatus(message);
  render();
}
function replaceCommittedRoi(nextRoi, message) {
  clearInteractionState();
  committedRoi = copyRoi(nextRoi);
  setStatus(message);
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
    return { activePointerId, activeEdit: activeEdit === null ? null : { ...activeEdit }, committedRoi: copyRoi(committedRoi), draftRoi: copyRoi(draftRoi), interactionOriginPoint: copyPoint(interactionOriginPoint), interactionOriginRoi: copyRoi(interactionOriginRoi), selectedCandidate: roiSelectedCandidate, sourceSize: sourceSize === null ? null : { ...sourceSize } };
  },
  moveRoi: roiGeometry.moveRoi, normalizeRoi: roiGeometry.normalizeRoi, pointToSource: roiGeometry.pointToSource,
  pointInRoi: roiGeometry.pointInRoi, beginInteraction, cancelInteraction, clearRoi, commitInteraction, getPhase6Snapshot,
  reset, replaceCommittedRoi, setDraftRoi, setStatus, setSelectedCandidate, resizeHandleAt: roiGeometry.resizeHandleAt,
  resizeRoi: roiGeometry.resizeRoi, sourceRoiToDisplay: roiGeometry.sourceRoiToDisplay,
});

window.vigiVisionReferenceFrameRoi = referenceFrameRoi;
