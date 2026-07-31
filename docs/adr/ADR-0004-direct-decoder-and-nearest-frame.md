# ADR-0004: Direct Reference-Frame Decoding and Nearest-Frame Selection

## Status

Accepted

## Context

Retrieving a specific reference frame from an NVR recording poses timing challenges. Requesting a replay for a specific UTC instant does not guarantee that the NVR's video stream contains a frame with that exact timestamp.

The original replay-based approach extracted a complete bounded MP4, probed it for all frame PTS (Presentation Time Stamp) values, and then invoked FFmpeg again to extract the nearest frame. This requires writing full video segments to disk just to extract a single image.

Furthermore, we need a deterministic rule for selecting a frame when the exact requested time falls between two decoded frames.

## Decision

We decode reference frames directly from the RTSP stream using an FFmpeg `tee` and `framemd5` pipeline (`FfmpegDirectReferenceFrameAcquirer`). This pipeline writes candidate JPEGs sequentially while simultaneously streaming timing metadata (`framemd5`) to `stdout`.

A background thread parses the timing data, computes the local PTS (`local_pts_seconds`), and evaluates candidates against the target offset.
- We select the frame nearest to the target PTS.
- In the event of a tie (e.g., the target is exactly halfway between two frames), we select the **earlier** frame.
- We report the timing precision as `measured_clip_relative`.

This implements the `gpv-2` (generation policy version 2) reference-frame policy.

## Alternatives Considered

- **Replay-based extraction (gpv-1):** Extracting a full MP4 first and then probing/extracting. While simpler to implement, it performs unnecessary disk I/O and takes longer for single-frame requests. (This is retained as a fallback/legacy option in the codebase).
- **Exact match only:** Failing the request if a frame doesn't exist at the exact requested millisecond. Rejected because video encoding (especially variable frame rate) rarely aligns perfectly with arbitrary requested instants.

## Consequences

- **Performance:** Faster single-frame retrieval without the overhead of saving full MP4 clips.
- **Complexity:** Introduces a complex streaming reader pipeline with background threads for stdout/stderr and careful synchronization to avoid deadlocks.
- **Deterministic semantics:** The selected frame's timing meaning is explicitly defined and recorded in the artifact manifest as `measured_clip_relative`.
