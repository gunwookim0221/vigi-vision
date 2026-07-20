# Recording Retrieval

## Purpose

The recording retrieval layer obtains one bounded temporary MP4 from an NVR.
It does not invoke OpenAI, inspect video content, generate reports, or expose a
CLI command.

## Public interfaces

- `RecordingWindow` is a positive, whole-second UTC interval for one channel.
- `RecordingSegment` preserves the source NVR-local recording date, raw epoch
  seconds, converted UTC endpoints, and duration.
- `RecordingPlanner.plan(window)` uses the public SDK to find an overlapping
  segment and returns a credential-free `ReplayRequest`.
- `ReplayExtractor.extract(request)` supplies RTSP credentials only to ffmpeg
  and returns a removable `ReplayClip` after a successful video-only MP4
  extraction.
- `ReplayClip.remove()` deletes the temporary MP4 after consumption.

## NVR contract

Recording-day search uses the NVR-local calendar. The configured NVR is
UTC+09:00. Recording-result timestamps are Unix epoch seconds and are converted
to UTC before replay planning. Replay URLs use lowercase UTC
`YYYYMMDDtHHMMSSz` values and documented stream `1`.

The planner verifies segment overlap before building a replay request. The
extractor always uses RTSP-over-TCP and applies `-t <window duration>` because
the NVR replay `endtime` is not a reliable ffmpeg EOF boundary.

## Security and failure behavior

`ReplayRequest` and `ReplayClip` retain credential-free URLs only. The ffmpeg
command receives encoded credentials in memory; stderr and command arguments
are not stored in returned values or exceptions. Partial MP4 files are removed
for timeouts and non-zero ffmpeg exits.

The layer distinguishes no matching SDK segment, RTSP 401 authentication,
RTSP 454 recording unavailable, ffmpeg timeout, and other extraction failures.

## Session 7 integration point

Session 7 should pass `ReplayClip.temporary_mp4_path` into the existing local
`analyze-video` service, then call `ReplayClip.remove()` in a `finally` block.
The retrieval layer must remain unaware of analysis profiles, OpenAI, and
business reports.
