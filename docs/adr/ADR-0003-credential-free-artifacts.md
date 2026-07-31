# ADR-0003: Credential-Free Persisted Artifacts

## Status

Accepted

## Context

VIGI Vision generates durable artifacts (investigation packages, sampled recording packages, and reference frames) that are saved to the local filesystem. These artifacts are accompanied by manifests (`manifest.json`) that describe the context of the data (e.g., requested time, offsets, status, warnings).

The inputs to create these artifacts require sensitive information: NVR hostnames, usernames, passwords, and authenticated RTSP URLs. If this sensitive data were included in the persistent manifests, logs, or file paths, the generated artifacts could not be safely shared, inspected, or archived.

## Decision

We enforce a strict credential-free policy for all persisted artifacts, manifests, logs, and user-facing output. Usernames, passwords, hosts, authenticated RTSP URLs, FFmpeg arguments, and raw subprocess diagnostics are never stored in `manifest.json` or any artifact path.

Replay requests and artifacts retain only credential-free URLs and safe identifiers. Authenticated URLs are constructed in-memory just before invoking FFmpeg and are immediately discarded.

## Alternatives Considered

- **Storing full authenticated RTSP URLs:** Including the full URL in the manifest to make debugging extraction failures easier. Rejected because it violates fundamental security and privacy postures.
- **Storing raw FFmpeg commands/stderr:** Writing the exact subprocess invocation and stderr to the manifest for diagnostics. Rejected because the command line contains the authenticated URL, and stderr may leak network paths or hostnames.

## Consequences

- **Secure artifacts:** Artifact directories can be safely shared, zipped, or inspected without exposing NVR credentials or network topology.
- **Debugging constraints:** Debugging extraction or decoding failures requires recreating the request context rather than simply reading the failing command from a log.
- **Explicit redaction boundary:** API responses and exception handling must deliberately map raw exceptions to safe, fixed domain error codes to prevent credential leakage through stack traces.
