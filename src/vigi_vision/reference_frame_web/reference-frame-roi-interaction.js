(function () {
const roiStage = document.querySelector("#roi-stage");
const resetButton = document.querySelector("#roi-reset");
const roi = window.vigiVisionReferenceFrameRoi;
const geometry = window.vigiVisionReferenceFrameRoiGeometry;
const MINIMUM_ROI_SIZE = 4;

function sourcePoint(event) {
  const state = roi.getState();
  return geometry.pointToSource(
    { clientX: event.clientX, clientY: event.clientY },
    state.selectedCandidate === null ? { left: 0, top: 0, width: 1, height: 1 } : document.querySelector("#selected-preview-image").getBoundingClientRect(),
    state.sourceSize.width,
    state.sourceSize.height,
  );
}

function handleHitRadius() {
  const state = roi.getState();
  const image = document.querySelector("#selected-preview-image");
  const displayRect = image.getBoundingClientRect();
  const suggestedRadius = Math.max(
    MINIMUM_ROI_SIZE,
    Math.ceil(12 * Math.max(state.sourceSize.width / displayRect.width, state.sourceSize.height / displayRect.height)),
  );
  if (state.committedRoi === null) {
    return suggestedRadius;
  }
  return Math.min(
    suggestedRadius,
    Math.max(MINIMUM_ROI_SIZE, Math.min(state.committedRoi.width, state.committedRoi.height) / 3),
  );
}

function setDraftFromPoint(point) {
  const state = roi.getState();
  if (state.activeEdit === null || state.sourceSize === null || state.interactionOriginPoint === null) {
    return;
  }
  const origin = state.interactionOriginPoint;
  const nextRoi = state.activeEdit.mode === "drawing"
    ? geometry.normalizeRoi(origin, point, state.sourceSize.width, state.sourceSize.height)
    : state.activeEdit.mode === "moving"
      ? geometry.moveRoi(state.interactionOriginRoi, { x: point.x - origin.x, y: point.y - origin.y })
      : geometry.resizeRoi(state.interactionOriginRoi, state.activeEdit.handle, point, MINIMUM_ROI_SIZE);
  roi.setDraftRoi(nextRoi);
}

function beginPointer(event) {
  const state = roi.getState();
  if (window.vigiVisionReferenceFrameAssistedRoi?.isBlockingManualPointerInput()) {
    return;
  }
  if (state.selectedCandidate === null || state.sourceSize === null || roiStage.hidden || state.activePointerId !== null) {
    return;
  }
  if ((event.pointerType === "mouse" && event.button !== 0) || !Number.isInteger(event.pointerId)) {
    return;
  }
  event.preventDefault?.();
  const point = sourcePoint(event);
  const handle = state.committedRoi === null ? null : geometry.resizeHandleAt(point, state.committedRoi, handleHitRadius());
  const mode = handle !== null ? "resizing" : state.committedRoi !== null && geometry.pointInRoi(point, state.committedRoi) ? "moving" : "drawing";
  window.vigiVisionReferenceFrameAssistedRoi?.cancelPending?.();
  try {
    roiStage.setPointerCapture?.(event.pointerId);
  } catch {
    roi.cancelInteraction("ROI edit could not start. Try again.", "error");
    return;
  }
  roiStage.focus?.({ preventScroll: true });
  roi.beginInteraction(event.pointerId, mode, handle, point);
  setDraftFromPoint(point);
  roi.setStatus(mode === "drawing" ? "Drawing a replacement ROI." : mode === "moving" ? "Moving ROI." : `Resizing ROI from the ${handle} handle.`, "active");
}

function updatePointer(event) {
  if (event.pointerId !== roi.getState().activePointerId) {
    return;
  }
  event.preventDefault?.();
  setDraftFromPoint(sourcePoint(event));
}

function finishPointer(event) {
  const state = roi.getState();
  if (event.pointerId !== state.activePointerId) {
    return;
  }
  event.preventDefault?.();
  const point = sourcePoint(event);
  const origin = state.interactionOriginPoint;
  if (origin !== null && Math.max(Math.abs(point.x - origin.x), Math.abs(point.y - origin.y)) < MINIMUM_ROI_SIZE) {
    roi.cancelInteraction("ROI edit was too small; the previous ROI remains.", "warning");
    return;
  }
  setDraftFromPoint(point);
  const next = roi.getState().draftRoi;
  const mode = state.activeEdit?.mode;
  if (next === null || next.width < MINIMUM_ROI_SIZE || next.height < MINIMUM_ROI_SIZE) {
    roi.cancelInteraction("ROI is too small; draw at least 4 by 4 source pixels.", "warning");
    return;
  }
  const message = mode === "drawing"
    ? "ROI committed in original-image pixels. Drag the box or handles to edit it."
    : mode === "moving"
      ? "ROI moved in original-image pixels."
      : "ROI resized in original-image pixels.";
  roi.commitInteraction(message);
}

function cancelPointer(event, message) {
  if (event.pointerId !== roi.getState().activePointerId) {
    return;
  }
  event.preventDefault?.();
  roi.cancelInteraction(message, "warning");
}

function keyboardRoi(event) {
  const state = roi.getState();
  if (event.key === "Escape" && state.activePointerId !== null) {
    event.preventDefault();
    roi.cancelInteraction("ROI edit cancelled; the previous ROI remains.", "warning");
    return;
  }
  if (state.committedRoi === null) {
    return;
  }
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    clearRoi("ROI reset. Draw a new region when ready.");
    return;
  }
  const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  const direction = directions[event.key];
  if (direction === undefined) {
    return;
  }
  event.preventDefault();
  const step = event.shiftKey ? 10 : 1;
  window.vigiVisionReferenceFrameAssistedRoi?.cancelPending?.();
  if (!event.altKey) {
    roi.replaceCommittedRoi(geometry.moveRoi(state.committedRoi, { x: direction[0] * step, y: direction[1] * step }), "ROI moved with the keyboard.");
    return;
  }
  const isHorizontal = direction[0] !== 0;
  const point = {
    x: isHorizontal ? state.committedRoi.x + state.committedRoi.width + direction[0] * step : state.committedRoi.x,
    y: isHorizontal ? state.committedRoi.y : state.committedRoi.y + state.committedRoi.height + direction[1] * step,
  };
  const handle = isHorizontal ? "e" : "s";
  roi.replaceCommittedRoi(geometry.resizeRoi(state.committedRoi, handle, point, MINIMUM_ROI_SIZE), "ROI resized with the keyboard.");
}

function clearRoi(message) {
  window.vigiVisionReferenceFrameAssistedRoi?.cancelPending?.();
  roi.clearRoi(message);
}

[
  ["pointerdown", beginPointer],
  ["pointermove", updatePointer],
  ["pointerup", finishPointer],
  ["pointercancel", (event) => cancelPointer(event, "ROI edit cancelled; the previous ROI remains.")],
  ["lostpointercapture", (event) => cancelPointer(event, "ROI edit was interrupted; the previous ROI remains.")],
].forEach(([name, handler]) => roiStage.addEventListener(name, handler));
roiStage.addEventListener("keydown", keyboardRoi);
resetButton.addEventListener("click", () => clearRoi("ROI reset. Draw a new region when ready."));
})();
