(function () {
const roiStage = document.querySelector("#roi-stage");
const geometry = window.vigiVisionReferenceFrameRoiGeometry;
const assistant = window.vigiVisionReferenceFrameAssistedRoi;
const TAP_MOVE_TOLERANCE = 8;
let armedPointerId = null;
let armedPoint = null;
let armedDisplayPoint = null;
let armedStart = null;

function displayPointForClient(image, clientX, clientY) {
  const rect = image.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0 || clientX < rect.left || clientY < rect.top || clientX > rect.right || clientY > rect.bottom) {
    return null;
  }
  return { x: clientX - rect.left, y: clientY - rect.top };
}

function sourcePointForClient(image, sourceSize, clientX, clientY) {
  const displayPoint = displayPointForClient(image, clientX, clientY);
  if (displayPoint === null) {
    return null;
  }
  const point = geometry.pointToSource({ clientX, clientY }, image.getBoundingClientRect(), sourceSize.width, sourceSize.height);
  return {
    x: Math.min(point.x, sourceSize.width - 1),
    y: Math.min(point.y, sourceSize.height - 1),
  };
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

function clearLocalPointer() {
  const pointerId = armedPointerId;
  armedPointerId = null;
  armedPoint = null;
  armedDisplayPoint = null;
  armedStart = null;
  if (pointerId !== null) {
    releasePointerCapture(pointerId);
  }
  assistant.clearPointer(pointerId);
}

function beginPointer(event) {
  const context = assistant.getPointerContext();
  if (context === null || armedPointerId !== null) {
    return;
  }
  if (event.isPrimary === false || event.button !== 0 || !Number.isInteger(event.pointerId)) {
    return;
  }
  const displayPoint = displayPointForClient(context.image, event.clientX, event.clientY);
  const point = sourcePointForClient(context.image, context.sourceSize, event.clientX, event.clientY);
  if (displayPoint === null || point === null) {
    return;
  }
  event.preventDefault?.();
  roiStage.setPointerCapture?.(event.pointerId);
  armedPointerId = event.pointerId;
  armedPoint = point;
  armedDisplayPoint = displayPoint;
  armedStart = { x: event.clientX, y: event.clientY };
  assistant.armPointer(event.pointerId, displayPoint);
}

function updatePointer(event) {
  if (event.pointerId !== armedPointerId || armedStart === null) {
    return;
  }
  if (Math.max(Math.abs(event.clientX - armedStart.x), Math.abs(event.clientY - armedStart.y)) > TAP_MOVE_TOLERANCE) {
    clearLocalPointer();
    assistant.cancelTap();
  }
}

function finishPointer(event) {
  if (event.pointerId !== armedPointerId) {
    return;
  }
  event.preventDefault?.();
  const context = assistant.getPointerContext();
  const point = context === null ? null : sourcePointForClient(context.image, context.sourceSize, event.clientX, event.clientY);
  const displayPoint = context === null ? null : displayPointForClient(context.image, event.clientX, event.clientY);
  const validTap = point !== null && displayPoint !== null && armedPoint !== null && armedDisplayPoint !== null;
  clearLocalPointer();
  if (validTap) {
    assistant.requestTap(point, displayPoint);
    return;
  }
  assistant.cancelTap();
}

function cancelPointer(event) {
  if (event.pointerId !== armedPointerId) {
    return;
  }
  event.preventDefault?.();
  clearLocalPointer();
  assistant.cancelTap();
}

function reset() {
  clearLocalPointer();
}

roiStage.addEventListener("pointerdown", beginPointer);
roiStage.addEventListener("pointermove", updatePointer);
roiStage.addEventListener("pointerup", finishPointer);
roiStage.addEventListener("pointercancel", cancelPointer);
roiStage.addEventListener("lostpointercapture", cancelPointer);
window.vigiVisionReferenceFrameAssistedPointer = Object.freeze({ reset });
})();
