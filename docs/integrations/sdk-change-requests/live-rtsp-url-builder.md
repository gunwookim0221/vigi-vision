# SDK Change Request: Public Live RTSP URL Builder

## Status

Complete. The NVR builder is implemented in the SDK at commit `c559ffa`; the
standalone IPC builder is implemented at commit `5dcc8a7`. VIGI Vision live
validation succeeded through the documented public IPC builder, one-frame
ffmpeg extraction, and one structured OpenAI analysis. No RTSP URL,
credentials, frame contents, or model-response details are recorded here.

## Problem and evidence

VIGI Vision's First Working Slice needs one current camera frame for image
analysis. The SDK already provides the public NVR authentication and inventory
path:

- `VigiClient.login()` authenticates an NVR session.
- `VigiClient.devices.list_added_devices()` returns `AddedDevice` entries.
- `AddedDevice.channel_id` is the documented NVR `id` field and is the channel
  identifier used by existing SDK stream and recording APIs.

The SDK now provides the requested public live builder. The documented SDK
contract uses `StreamType.MAIN` for stream `1` and `StreamType.MINOR` for
stream `2`, returns a credential-free URL, and leaves RTSP Digest
authentication to the external media client.

VIGI Vision must not derive an undocumented live URL, use SDK internals, or add
media processing to the SDK.

## Requested public capability

Add a documented, capability-gated public live URL builder:

```python
from vigi import StreamService, StreamType

url = StreamService().build_live_url(
    host="nvr.example.invalid",
    channel_id=1,
    stream=StreamType.MAIN,
)
```

Proposed signature:

```python
def build_live_url(
    self,
    host: str,
    channel_id: int,
    stream: StreamType = StreamType.MAIN,
) -> str: ...
```

The method must construct only the official, documented NVR live RTSP URL. It
must not open RTSP, extract media, save files, or add ffmpeg as an SDK
dependency. It should raise the existing public `CapabilityError` when
`CapabilityName.STREAM_LIVE_RTSP` is unavailable and the existing public
`ValidationError` for invalid inputs.

## Channel and stream contract

- `channel_id` is a positive integer returned as `AddedDevice.channel_id` from
  `client.devices.list_added_devices()`; it is not `AddedDevice.device_id`.
- The builder must accept only stream selectors documented for NVR live RTSP.
  The SDK's existing public `StreamType` expresses `MAIN` (`"1"`) and `MINOR`
  (`"2"`); the implementation and documentation must confirm which of these
  the NVR live URL supports before promising them. Unsupported values must
  fail clearly.
- The returned URL must contain the supplied channel and selected stream, with
  no invented stable device identifier.

## RTSP authentication contract

The returned URL must not embed an NVR password, access token, refresh token,
`stok`, Digest nonce, or Digest response. NVR HTTPS Bearer authentication is
not RTSP authentication. The SDK documentation must state that an external
RTSP client performs the RTSP Digest challenge/response using separately
supplied credentials and must specify the verified credential scope for an NVR
live stream.

The SDK must not imply that `VigiClient.login()` authenticates an RTSP session
or that a Bearer token can be reused for RTSP.

## Security requirements

- Reject hosts containing a scheme, port, credentials, path, query, or
  fragment, consistent with the replay URL builder.
- Do not include credentials or tokens in URL output, exception messages,
  `repr`, examples, or tests.
- Document the generated URL as sensitive connection metadata. Callers should
  avoid logs and should not persist it with camera artifacts.
- Add redaction tests for malformed host input and generated URL output.

## Backward compatibility

This is additive. It must not change `build_replay_url(...)`, its replay-only
stream-`1` rule, existing URL format, authentication behavior, or public
exception hierarchy. Existing consumers must not need to opt into live RTSP.

## Expected VIGI Vision boundary

After this capability is released, VIGI Vision will:

1. authenticate and enumerate channels through the SDK;
2. select a channel using `AddedDevice.channel_id`;
3. obtain the live URL through the new public builder;
4. supply the URL and separately configured RTSP Digest credentials to an
   external ffmpeg process;
5. extract one frame into Vision's git-ignored runtime artifacts directory;
6. pass that image to OpenAI for analysis.

VIGI Vision will not modify the SDK, construct a live RTSP URL itself, use a
private endpoint, or move frame extraction, image persistence, or OpenAI code
into the SDK.

## Acceptance evidence requested from the SDK

- Public import, construction, and unit tests for `build_live_url`.
- Tests for documented channel and live-stream selectors, invalid input, and
  `STREAM_LIVE_RTSP` capability gating.
- Tests proving URL/error redaction.
- An explicitly opt-in real-NVR test showing that a documented live URL can be
  opened by a standard RTSP Digest client without exposing credentials in test
  output.
