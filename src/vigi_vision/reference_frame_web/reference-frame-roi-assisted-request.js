(function () {
const MINIMUM_ROI_SIZE = 4;
const MAX_MASK_PREVIEW_RUNS = 50000;
const ERROR_MESSAGES = Object.freeze({
  invalid_point: "A suggestion could not be created for that point. Try tapping the object again.",
  no_valid_suggestion: "No valid suggestion was available for that tap. Try tapping the object again.",
  suggestion_timeout: "The suggestion took too long. Try again or draw the ROI manually.",
  suggestion_unavailable: "Automatic suggestion is unavailable. Continue with manual ROI selection.",
  suggestion_failure: "The automatic suggestion failed safely. Try again or draw the ROI manually.",
});

function responseErrorCode(payload) {
  const code = payload?.error?.code;
  return typeof code === "string" && Object.prototype.hasOwnProperty.call(ERROR_MESSAGES, code)
    ? code
    : "suggestion_failure";
}

function safeResponsePayload(response) {
  return response.json().catch(() => null);
}

function validateMaskPreview(payload, sourceSize, box) {
  if (payload === null || typeof payload !== "object"
    || payload.width !== sourceSize.width || payload.height !== sourceSize.height
    || !Array.isArray(payload.rows) || payload.rows.length !== sourceSize.height) {
    return null;
  }
  let runCount = 0;
  let left = null;
  let top = null;
  let right = null;
  let bottom = null;
  const rows = payload.rows.map((rawRow, y) => {
    if (!Array.isArray(rawRow)) {
      return null;
    }
    let previousEnd = 0;
    const row = rawRow.map((rawRun) => {
      if (!Array.isArray(rawRun) || rawRun.length !== 2
        || !Number.isInteger(rawRun[0]) || !Number.isInteger(rawRun[1])
        || rawRun[0] < 0 || rawRun[0] >= rawRun[1] || rawRun[1] > sourceSize.width) {
        return null;
      }
      if (rawRun[0] < previousEnd) {
        return null;
      }
      previousEnd = rawRun[1];
      runCount += 1;
      left = left === null ? rawRun[0] : Math.min(left, rawRun[0]);
      top = top === null ? y : Math.min(top, y);
      right = right === null ? rawRun[1] : Math.max(right, rawRun[1]);
      bottom = bottom === null ? y + 1 : Math.max(bottom, y + 1);
      return [rawRun[0], rawRun[1]];
    });
    return row.some((run) => run === null) ? null : row;
  });
  if (rows.some((row) => row === null) || runCount > MAX_MASK_PREVIEW_RUNS || left === null) {
    return null;
  }
  if (left !== box.x || top !== box.y || right - left !== box.width || bottom - top !== box.height) {
    return null;
  }
  return { width: sourceSize.width, height: sourceSize.height, rows };
}

function validateSuggestion(payload, resourceId, sourceSize, point) {
  if (payload === null || typeof payload !== "object" || payload.resource_id !== resourceId) {
    return null;
  }
  if (payload.source_width !== sourceSize.width || payload.source_height !== sourceSize.height) {
    return null;
  }
  const box = payload.bbox;
  if (box === null || typeof box !== "object") {
    return null;
  }
  const values = [box.x, box.y, box.width, box.height];
  if (!values.every((value) => Number.isInteger(value))) {
    return null;
  }
  if (box.width < MINIMUM_ROI_SIZE || box.height < MINIMUM_ROI_SIZE || box.x < 0 || box.y < 0) {
    return null;
  }
  if (box.x + box.width > sourceSize.width || box.y + box.height > sourceSize.height) {
    return null;
  }
  if (point.x < box.x || point.x >= box.x + box.width || point.y < box.y || point.y >= box.y + box.height) {
    return null;
  }
  const maskPreview = validateMaskPreview(payload.mask_preview, sourceSize, box);
  if (maskPreview === null) {
    return null;
  }
  return {
    roi: {
      source_width: sourceSize.width,
      source_height: sourceSize.height,
      x: box.x,
      y: box.y,
      width: box.width,
      height: box.height,
    },
    maskPreview,
  };
}

async function requestSuggestion(options) {
  try {
    const response = await fetch(`/api/v1/reference-frames/${encodeURIComponent(options.resourceId)}/roi-suggestions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ point: options.point }),
      signal: options.controller?.signal,
    });
    const payload = await safeResponsePayload(response);
    if (!options.isCurrent()) {
      return;
    }
    if (!response.ok) {
      options.onError(responseErrorCode(payload));
      return;
    }
    const nextRoi = validateSuggestion(payload, options.resourceId, options.sourceSize, options.point);
    if (nextRoi === null) {
      options.onInvalid();
      return;
    }
    options.onSuccess(nextRoi);
  } catch (error) {
    if (!options.isCurrent()) {
      return;
    }
    options.onFailure(options.controller?.signal?.aborted || error?.name === "AbortError" ? "cancelled" : "failure");
  }
}

window.vigiVisionReferenceFrameAssistedRequest = Object.freeze({ ERROR_MESSAGES, requestSuggestion });
})();
