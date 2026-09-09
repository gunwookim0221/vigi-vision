# ADR-0002: Bounded Recording Replay Extraction

## Status

Accepted

## Context

The system must acquire recorded video from the NVR to support analysis, investigation collection, and reference-frame decoding. RTSP replay streams from the NVR are not inherently bounded by standard file-transfer mechanisms; they stream continuously until the client disconnects or the recording ends.

If the application were to consume these streams indefinitely or fail to properly manage timeouts, it would risk resource exhaustion, hung processes, and leaving partial, corrupt MP4 files on disk.

## Decision

We extract NVR replays using an FFmpeg subprocess with a strict output duration cap (`-t`) and a client-side timeout. The `ReplayExtractor` enforces a bounded duration and explicitly removes any partial MP4 output if the extraction fails, times out, or encounters authentication/availability errors.

## Alternatives Considered

- **Retaining partial files on failure:** Keeping incomplete MP4s for debugging. Rejected because it violates the principle of leaving the filesystem clean after a failure; bounded progress diagnostics are used instead for timeout debugging.

## Consequences

- **Reliability:** The media pipeline remains strictly bounded while allowing a
  remote NVR to run slower than real time: the replay ceiling is the larger of
  `duration + 40s` and `duration × 3`, then Phase 7E clamps it to the remaining
  invocation budget and cleanup reserve. A 60-second request therefore has a
  180-second ceiling before that clamp.
- **Clean state:** The filesystem is not polluted with junk files after failures, as partial outputs are reliably cleaned up.
- **Media trust:** A timeout-owned partial MP4 is never treated as completed
  media; normal replay-process completion remains required before publication
  and downstream validation.
- **Error classification:** The extraction layer can cleanly distinguish between authentication failures, unavailability, and timeouts, returning safe domain errors rather than hanging.
