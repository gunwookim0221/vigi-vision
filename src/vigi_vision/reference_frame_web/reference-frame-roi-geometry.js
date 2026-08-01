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

window.vigiVisionReferenceFrameRoiGeometry = Object.freeze({
  normalizeRoi,
  pointToSource,
  sourceRoiToDisplay,
});
