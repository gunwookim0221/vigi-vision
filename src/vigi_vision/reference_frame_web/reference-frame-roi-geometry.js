function clamp(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), maximum);
}

function pointToSource(point, displayRect, sourceWidth, sourceHeight) {
  const width = Math.max(displayRect.width, 1);
  const height = Math.max(displayRect.height, 1);
  const relativeX = clamp((point.clientX - displayRect.left) / width, 0, 1);
  const relativeY = clamp((point.clientY - displayRect.top) / height, 0, 1);
  return {
    x: clamp(Math.round(relativeX * sourceWidth), 0, sourceWidth),
    y: clamp(Math.round(relativeY * sourceHeight), 0, sourceHeight),
  };
}

function normalizeRoi(start, end, sourceWidth, sourceHeight) {
  const left = clamp(Math.min(start.x, end.x), 0, sourceWidth);
  const right = clamp(Math.max(start.x, end.x), 0, sourceWidth);
  const top = clamp(Math.min(start.y, end.y), 0, sourceHeight);
  const bottom = clamp(Math.max(start.y, end.y), 0, sourceHeight);
  return {
    source_width: sourceWidth,
    source_height: sourceHeight,
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
  };
}

function sourceRoiToDisplay(roi, displayRect) {
  return {
    left: (roi.x / roi.source_width) * displayRect.width,
    top: (roi.y / roi.source_height) * displayRect.height,
    width: (roi.width / roi.source_width) * displayRect.width,
    height: (roi.height / roi.source_height) * displayRect.height,
  };
}

const HANDLE_AXES = Object.freeze({
  nw: { left: true, top: true },
  n: { top: true },
  ne: { right: true, top: true },
  e: { right: true },
  se: { right: true, bottom: true },
  s: { bottom: true },
  sw: { left: true, bottom: true },
  w: { left: true },
});

function moveRoi(roi, delta) {
  return {
    ...roi,
    x: clamp(Math.round(roi.x + delta.x), 0, roi.source_width - roi.width),
    y: clamp(Math.round(roi.y + delta.y), 0, roi.source_height - roi.height),
  };
}

function pointInRoi(point, roi) {
  return point.x >= roi.x
    && point.x <= roi.x + roi.width
    && point.y >= roi.y
    && point.y <= roi.y + roi.height;
}

function resizeHandleAt(point, roi, hitRadius) {
  const left = roi.x;
  const right = roi.x + roi.width;
  const top = roi.y;
  const bottom = roi.y + roi.height;
  const nearLeft = Math.abs(point.x - left) <= hitRadius;
  const nearRight = Math.abs(point.x - right) <= hitRadius;
  const nearTop = Math.abs(point.y - top) <= hitRadius;
  const nearBottom = Math.abs(point.y - bottom) <= hitRadius;
  if (nearLeft && nearTop) return "nw";
  if (nearRight && nearTop) return "ne";
  if (nearRight && nearBottom) return "se";
  if (nearLeft && nearBottom) return "sw";
  if (nearTop && point.x > left && point.x < right) return "n";
  if (nearRight && point.y > top && point.y < bottom) return "e";
  if (nearBottom && point.x > left && point.x < right) return "s";
  if (nearLeft && point.y > top && point.y < bottom) return "w";
  return null;
}

function resizeRoi(roi, handle, point, minimumSize = 4) {
  const axes = HANDLE_AXES[handle] ?? {};
  const minimumWidth = Math.min(minimumSize, roi.source_width);
  const minimumHeight = Math.min(minimumSize, roi.source_height);
  let left = roi.x;
  let right = roi.x + roi.width;
  let top = roi.y;
  let bottom = roi.y + roi.height;
  if (axes.left) {
    left = clamp(Math.round(point.x), 0, right - minimumWidth);
  }
  if (axes.right) {
    right = clamp(Math.round(point.x), left + minimumWidth, roi.source_width);
  }
  if (axes.top) {
    top = clamp(Math.round(point.y), 0, bottom - minimumHeight);
  }
  if (axes.bottom) {
    bottom = clamp(Math.round(point.y), top + minimumHeight, roi.source_height);
  }
  return {
    source_width: roi.source_width,
    source_height: roi.source_height,
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
  };
}

window.vigiVisionReferenceFrameRoiGeometry = Object.freeze({
  moveRoi,
  normalizeRoi,
  pointToSource,
  pointInRoi,
  resizeHandleAt,
  resizeRoi,
  sourceRoiToDisplay,
});
