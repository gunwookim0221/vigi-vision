const roiStage = document.querySelector("#roi-stage");
const roiStatus = document.querySelector("#roi-status");
const committedOverlay = document.querySelector("#roi-committed-overlay");
const draftOverlay = document.querySelector("#roi-draft-overlay");
const roiSummary = document.querySelector("#roi-summary");
const summaryFields = {
  x: document.querySelector("#roi-summary-x"),
  y: document.querySelector("#roi-summary-y"),
  width: document.querySelector("#roi-summary-width"),
  height: document.querySelector("#roi-summary-height"),
  sourceWidth: document.querySelector("#roi-summary-source-width"),
  sourceHeight: document.querySelector("#roi-summary-source-height"),
};
const MINIMUM_ROI_WIDTH = 4;
const MINIMUM_ROI_HEIGHT = 4;
const roiGeometry = window.vigiVisionReferenceFrameRoiGeometry;
let roiSelectedCandidate = null;
let selectedImage = null;
let sourceSize = null;
let draftEndpoints = null;
let committedRoi = null;
let activePointerId = null;
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
function clearPointerState() {
  const pointerId = activePointerId;
  activePointerId = null;
  draftEndpoints = null;
  if (pointerId !== null) {
    releasePointerCapture(pointerId);
  }
}
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
  Object.assign(overlay.style, {
    left: `${displayRoi.left}px`,
    top: `${displayRoi.top}px`,
    width: `${displayRoi.width}px`,
    height: `${displayRoi.height}px`,
  });
}
function renderSummary() {
  if (committedRoi === null) {
    roiSummary.hidden = true;
    return;
  }
  roiSummary.hidden = false;
  Object.entries({
    x: committedRoi.x,
    y: committedRoi.y,
    width: committedRoi.width,
    height: committedRoi.height,
    sourceWidth: committedRoi.source_width,
    sourceHeight: committedRoi.source_height,
  }).forEach(([key, value]) => {
    summaryFields[key].textContent = String(value);
  });
}
function render() {
  renderSummary();
  if (selectedImage === null || sourceSize === null) {
    [roiStage, committedOverlay, draftOverlay].forEach((element) => { element.hidden = true; });
    return;
  }
  roiStage.hidden = false;
  selectedImage.hidden = false;
  const displayRect = selectedImage.getBoundingClientRect();
  renderOverlay(committedOverlay, committedRoi, displayRect);
  const draftRoi = draftEndpoints === null
    ? null
    : roiGeometry.normalizeRoi(draftEndpoints.start, draftEndpoints.end, sourceSize.width, sourceSize.height);
  renderOverlay(draftOverlay, draftRoi, displayRect);
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
  clearPointerState();
  detachImage();
  roiSelectedCandidate = selectedImage = sourceSize = committedRoi = null;
  render();
  setStatus(message);
}
function pointerSourcePoint(event) {
  return roiGeometry.pointToSource({ clientX: event.clientX, clientY: event.clientY }, selectedImage.getBoundingClientRect(), sourceSize.width, sourceSize.height);
}
function updateDraft(event) {
  if (draftEndpoints !== null) {
    draftEndpoints.end = pointerSourcePoint(event);
  }
}
function handlePointerDown(event) {
  if (selectedImage === null || sourceSize === null || roiStage.hidden || activePointerId !== null) {
    return;
  }
  if ((event.pointerType === "mouse" && event.button !== 0) || !Number.isInteger(event.pointerId)) {
    return;
  }
  event.preventDefault?.();
  activePointerId = event.pointerId;
  const point = pointerSourcePoint(event);
  draftEndpoints = { start: point, end: point };
  try {
    roiStage.setPointerCapture?.(event.pointerId);
  } catch {
    clearPointerState();
    setStatus("ROI drawing could not start. Try again.");
    return;
  }
  setStatus("Drawing ROI. Release to commit the region.");
  render();
}
function handlePointerMove(event) {
  if (event.pointerId !== activePointerId) {
    return;
  }
  event.preventDefault?.();
  updateDraft(event); render();
}
function handlePointerUp(event) {
  if (event.pointerId !== activePointerId || draftEndpoints === null || sourceSize === null) {
    return;
  }
  event.preventDefault?.();
  updateDraft(event);
  const nextRoi = roiGeometry.normalizeRoi(draftEndpoints.start, draftEndpoints.end, sourceSize.width, sourceSize.height);
  clearPointerState();
  if (nextRoi.width < MINIMUM_ROI_WIDTH || nextRoi.height < MINIMUM_ROI_HEIGHT) {
    setStatus("ROI is too small; draw at least 4 by 4 source pixels.");
    render();
    return;
  }
  committedRoi = nextRoi;
  setStatus("ROI committed in original-image pixels. Drag again to replace it.");
  render();
}
function handlePointerCancel(event) {
  if (event.pointerId !== activePointerId) {
    return;
  }
  event.preventDefault?.();
  clearPointerState();
  setStatus(committedRoi === null ? "ROI drawing was cancelled." : "ROI drawing was cancelled; the previous ROI remains."); render();
}
function handleLostPointerCapture(event) {
  if (event.pointerId !== activePointerId) {
    return;
  }
  clearPointerState();
  setStatus(committedRoi === null ? "ROI drawing was interrupted." : "ROI drawing was interrupted; the previous ROI remains."); render();
}
[
  ["pointerdown", handlePointerDown],
  ["pointermove", handlePointerMove],
  ["pointerup", handlePointerUp],
  ["pointercancel", handlePointerCancel],
  ["lostpointercapture", handleLostPointerCapture],
].forEach(([name, handler]) => roiStage.addEventListener(name, handler));

if (typeof window.addEventListener === "function") {
  window.addEventListener("resize", render);
}
if (typeof window.ResizeObserver === "function") {
  const observer = new window.ResizeObserver(render);
  observer.observe(roiStage);
}

const referenceFrameRoi = Object.freeze({
  getState() {
    return {
      activePointerId,
      committedRoi: committedRoi === null ? null : { ...committedRoi },
      draftRoi: draftEndpoints === null || sourceSize === null
        ? null
        : roiGeometry.normalizeRoi(draftEndpoints.start, draftEndpoints.end, sourceSize.width, sourceSize.height),
      selectedCandidate: roiSelectedCandidate,
      sourceSize: sourceSize === null ? null : { ...sourceSize },
    };
  },
  normalizeRoi: roiGeometry.normalizeRoi,
  pointToSource: roiGeometry.pointToSource,
  reset,
  setSelectedCandidate,
  sourceRoiToDisplay: roiGeometry.sourceRoiToDisplay,
});

window.vigiVisionReferenceFrameRoi = referenceFrameRoi;
