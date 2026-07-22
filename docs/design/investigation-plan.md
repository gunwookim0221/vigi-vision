# Investigation Plan

## Purpose

The Investigation Plan is a pure, deterministic contract for turning one
investigation anchor into ordered per-camera recording requests. It performs no
NVR authentication, recording search, replay extraction, filesystem work, or
AI analysis.

## Boundaries

- The input boundary parses `YYYY-MM-DD HH:MM:SS` as `Asia/Seoul`, converts it
  once to a whole-second UTC `AnchorTime`, and retains the source timezone for
  traceability.
- The composition boundary calls `validate_scenario_profiles` to resolve every
  `ScenarioCameraRule.profile_id` through the existing profile registry.
- `InvestigationPlanner` expands validated scenario rules over
  `CameraAssignment` values and returns an `InvestigationPlan`.
- Existing `RecordingPlanner` remains downstream. It later decides whether an
  individual planned `RecordingWindow` has NVR coverage.

## Domain contract

- `CameraRole` is a lowercase kebab-case semantic label separate from the NVR
  channel identity.
- `Scenario` contains ordered, unique role rules. Each rule names one profile,
  a relative whole-second window, and whether the role is required.
- `CameraAssignment` maps one positive NVR channel ID to one role.
- `InvestigationItem` contains a safe deterministic ID, channel ID, role,
  profile ID, and the existing UTC `RecordingWindow`.
- `InvestigationPlan` retains the scenario ID, anchor, and ordered items.

Items are ordered by scenario-rule order and then ascending channel ID. Missing
required roles fail planning; missing optional roles are omitted. Unrelated
camera assignments are ignored. More than one assignment for the same channel
is rejected.

The planner derives recording windows only from the canonical UTC anchor and
relative offsets. Its item IDs contain scenario, channel, role, and UTC bounds;
they never contain NVR hosts, credentials, or RTSP/replay URLs.

## Current restaurant fixture

The implementation does not hardcode a restaurant. A caller may provide the
following assignments and scenario data:

```text
channel 1 → counter
channel 2 → entrance
channel 3 → dining

counter:  profile counter,  -60s to +60s, required
entrance: profile entrance, -30s to +480s, required
dining:   profile dining,  -300s to +300s, optional
```

## Deferred work

The plan neither claims recording availability nor creates artifacts. A later
collection boundary may process each item with the existing recording retrieval
layer and report per-item partial success. That boundary owns replay clips,
artifact manifests, and cleanup; analysis remains downstream of collection.
