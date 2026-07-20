# Recording Retrieval

## Purpose

The recording retrieval layer obtains one bounded temporary MP4 from an NVR.
It does not invoke OpenAI, inspect video content, or generate reports. The
application CLI composes it with the existing local-video analysis workflow.

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
the NVR replay `endtime` is not a reliable ffmpeg EOF boundary. Its subprocess
timeout is bounded to `requested duration + 30 seconds startup allowance + 10
seconds finalization margin`: the startup term covers the observed roughly
5.56-second RTSP connection latency, while the explicit finalization term gives
`+faststart` time to finish the MP4 container without allowing an unbounded
process.

## Security and failure behavior

`ReplayRequest` and `ReplayClip` retain credential-free URLs only. The ffmpeg
command receives encoded credentials in memory; stderr and command arguments
are not stored in returned values or exceptions. Partial MP4 files are removed
for timeouts and non-zero ffmpeg exits.

The layer distinguishes no matching SDK segment, RTSP 401 authentication,
RTSP 454 recording unavailable, ffmpeg timeout, and other extraction failures.

## Session 7 integration point

`vigi-vision analyze-recording` accepts an NVR channel, a UTC start timestamp,
a positive whole-second duration such as `30s`, and an analysis profile. It
plans and extracts the replay, passes `ReplayClip.temporary_mp4_path` to the
same internal service used by `analyze-video`, renders the existing temporal
business report, and calls `ReplayClip.remove()` in a `finally` block.

The retrieval layer remains unaware of analysis profiles, OpenAI, and business
reports. The shared video-analysis service retains its own temporary-frame
cleanup behavior.
