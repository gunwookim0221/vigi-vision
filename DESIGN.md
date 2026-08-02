# VIGI Vision Design System

## 0. Research Log

- Embedded references: shortlisted `linear.app`, `notion`, and `sentry` for a
  local operations surface; selected the restrained density, clear status
  hierarchy, and cool neutral palette informed by `linear.app`.
- Lazyweb: skipped for this local Phase 5 ROI slice; external product
  screenshots would not improve the bounded candidate-review workflow and are
  not copied into the product.
- Imagen drafts: skipped because the existing design tokens and evidence-led
  card layout are sufficient for this bounded thumbnail display; visual media
  is sourced from the existing safe API route.

## 1. Atmosphere & Identity

VIGI Vision's reference-frame shell is a quiet local review surface: focused,
factual, and calm under failure. Its signature is an ordered evidence ledger,
where requested candidate positions are easy to scan without implying that a
decoded image is an exact source-time capture.

## 2. Color

### Palette

| Role | Token | Light | Dark | Usage |
| --- | --- | --- | --- | --- |
| Surface primary | `--surface-primary` | `#f7f8fa` | `#101114` | Page background |
| Surface secondary | `--surface-secondary` | `#ffffff` | `#191a1e` | Form and result surfaces |
| Surface muted | `--surface-muted` | `#eef0f4` | `#23252b` | Input and row fill |
| Text primary | `--text-primary` | `#20242c` | `#f3f4f6` | Headings and body |
| Text secondary | `--text-secondary` | `#59616f` | `#b4bac5` | Supporting information |
| Border | `--border-default` | `#d9dde5` | `#393d47` | Controls and rows |
| Accent | `--accent-primary` | `#315bb6` | `#8da9ff` | Action and focus |
| Accent hover | `--accent-hover` | `#274b99` | `#b6c6ff` | Action hover |
| ROI mask fill | `--roi-mask-fill` | `rgb(49 91 182 / 38%)` | `rgb(141 169 255 / 42%)` | Transient assisted silhouette |
| ROI mask outline | `--roi-mask-outline` | `#f0bc68` | `#f0bc68` | Contrasting assisted selection edge |
| Success | `--status-success` | `#176b45` | `#72d1a6` | Succeeded candidate state |
| Warning | `--status-warning` | `#9a5d00` | `#f0bc68` | Partial result state |
| Error | `--status-error` | `#a92d3b` | `#ff9fab` | Request and candidate failure state |

Accent is reserved for the submit action, links, and focus. Status colors are
always paired with text so color is never the only state indicator.

## 3. Typography

| Level | Size | Weight | Line height | Usage |
| --- | --- | --- | --- | --- |
| H1 | 32px | 600 | 1.2 | Page title |
| H2 | 20px | 600 | 1.3 | Section headings |
| Candidate heading | 18px | 600 | 1.3 | Candidate offset label |
| Body | 16px | 400 | 1.5 | Default content |
| Body small | 14px | 400 | 1.5 | Helper text and rows |
| Label | 14px | 600 | 1.4 | Form labels and status |
| Mono | 13px | 500 | 1.4 | Requested UTC timestamps and offsets |

Primary font: `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.
Mono font: `ui-monospace, "Cascadia Code", Consolas, monospace`.

## 4. Spacing & Layout

All spacing uses a 4px base: `--space-1` 4px, `--space-2` 8px,
`--space-3` 12px, `--space-4` 16px, `--space-5` 20px, `--space-6` 24px,
`--space-8` 32px, and `--space-12` 48px.

The page is a document-scrolling content limiter with a 960px maximum width.
The form is an intrinsic grid that becomes a single column below 640px. Result
rows preserve server order and use `overflow-wrap: anywhere` for long safe
messages.

## 5. Components

### Candidate form

- **Structure:** labeled channel input, local datetime input with seconds,
  explicit IANA timezone select, Apply date and time action, applied-value
  summary, and Generate candidates action.
- **States:** unapplied, ready to apply, applied, dirty/reapply-required,
  disabled/loading, top-level error, and safe warning.
- **Accessibility:** native labels, required controls, keyboard actions, visible
  focus, live status regions, an indeterminate progress element without numeric
  values, and reduced-motion-safe busy feedback.
- **Layout:** intrinsic grid; document scroll is the sole scroll owner.

The applied summary is application-owned and always uses
`YYYY-MM-DD HH:mm:ss` plus the selected IANA timezone. The browser-native picker
may use its own locale presentation; it is not overridden. Candidate generation
uses an indeterminate spinner/progress indicator because the backend exposes no
trustworthy incremental progress contract.

### Candidate result row

- **Structure:** ordered media area, offset heading, requested offset/time,
  native single-selection control for usable successes, textual succeeded/failed
  status, then created/reused outcome or fixed safe failure facts.
- **States:** succeeded with thumbnail, selected, image unavailable, failed,
  partial-set context, all-failed context, and empty response.
- **Accessibility:** semantic list item; status words supplement color;
  native radio keyboard interaction, meaningful image alternative text,
  controlled image failure status, visible focus distinct from selection, and
  messages inserted as text, never HTML.
- **Layout:** responsive two-column media/details grid on wide screens and
  vertical stack on narrow screens. Server response order is authoritative; the
  selected preview preserves the full image aspect ratio.

### Selected candidate preview

- **Structure:** one larger safe JPEG preview with backend-provided resource ID,
  requested time, offset, dimensions, timing precision, and warnings.
- **States:** no selection, selected, and selected image unavailable.
- **Persistence:** frontend memory only; a new request clears the selection.

### ROI workspace

- **Structure:** one selected-image pointer surface with a source-pixel summary.
- **States:** inactive until selection, image loading, draft ROI, committed ROI,
  moving, resizing, reset, rejected tiny drag, and interrupted edit.
- **Interaction:** mouse, touch, and pen use one Pointer Events path; only one
  pointer is active, and additional pointers are ignored. Interior dragging
  moves the box; eight handles resize it with deterministic minimum-size
  clamping. Escape restores the committed ROI.
- **Keyboard:** focus the ROI surface; Arrow keys move by one source pixel,
  Shift+Arrow keys by ten, and Alt+Arrow keys resize the east or south edge by
  one pixel (Alt+Shift by ten). Delete/Backspace resets the transient ROI.
- **Mobile:** `touch-action: none` is scoped to the image surface so document
  scrolling remains available outside it.
- **Geometry:** coordinates use loaded `naturalWidth`/`naturalHeight`, with
  endpoint `Math.round()` conversion, source-bound clamping, and a 4×4 source
  pixel minimum. The overlay is recalculated from canonical source pixels after
  image load and responsive resize.
- **Handoff:** `getPhase6Snapshot()` returns an immutable candidate-bound
  source-pixel snapshot with `coordinateSpace` and allowed `provenance` while a
  valid ROI is selected. Phase 6-2C sends those fields through the strict
  confirmation API; the server remains authoritative for the durable package.
- **Persistence:** the draft remains transient until confirmation. The inline
  Korean review/POST/GET flow locks the confirmed controls and displays only
  the safe relative artifact destination; see [Investigation Confirmation and
  Durable Persistence](docs/design/investigation-confirmation.md).

#### Assisted-selection integration

Phase 5-3A defines the optional **Tap to suggest ROI** interaction. Phase
5-3B-1 provides a disposable offline validation harness, Phase 5-3B-2 provides
the production backend `POST
/api/v1/reference-frames/{resource_id}/roi-suggestions`, and Phase 5-3B-3
connects the explicit button/tap flow to that endpoint. One source-space point
is sent only from the selected candidate image; a current, dimension-matched,
bounded response enters the existing committed ROI state and paints an exact
bounded source-row mask preview on a responsive canvas. The mask is primary
selection evidence and the rectangle is secondary compatibility state. The
previous ROI stays visible while pending, abort and generation/resource checks
reject stale work, and safe unavailable/failure categories preserve manual
editing. Assisted selections hide tiny resize handles; reset or manual editing
clears the transient mask. The single canonical ROI remains transient and
`getPhase6Snapshot()` remains the only handoff from ROI interaction; Phase 6-2C
adapts that snapshot into the strict confirmation request. Assisted selection
itself remains transient and does not add persistence. The source image is reserved for visual evidence; general
ROI workflow status is rendered in the single polite live region below the
image, with explicit non-color state markers and guidance before the controls.
Reset clears visual and textual selection state, while manual mouse, touch, pen,
and keyboard fallback remains authoritative. Localization is deferred to
Phase 5-4C. See [the routed assisted-ROI design](docs/design/assisted-roi-selection.md)
for the full API, lifecycle, deployment, accessibility, and security boundary.

## 6. Motion & Interaction

Only button hover/active, opacity, and the busy spinner communicate affordance
and request-state change. They use 150ms ease-out; the spinner stops under
`prefers-reduced-motion` while its visible status text remains.

## 7. Depth & Surface

The surface strategy is tonal shift with a single low-contrast border. Cards
use `--surface-secondary` against `--surface-primary`; no decorative shadows
or glass effects are used.

## 8. Accessibility Constraints & Accepted Debt

- Target WCAG 2.2 AA: visible keyboard focus, labeled controls, semantic
  landmarks, live status/error announcements, and 4.5:1 body-text contrast.
- The form requires explicit application of a local whole-second datetime and
  selected source timezone; the applied summary makes the 24-hour value
  authoritative for the UI.
- ROI keyboard movement/resizing and reset are implemented in Phase 5-2; the
  confirmation/persistence accessibility contract is implemented in Phase 6-2C;
  object-comparison semantics remain later-phase work.
- Tap-assisted selection and the Phase 5-3C transient silhouette preview are
  implemented but remain acceptance-gated for physical desktop/mobile use; the
  disposable harness, production backend, canvas preview, and explicit
  frontend control share one canonical source-space ROI.
- Accepted debt: the shell has fixture-backed desktop/mobile browser evidence;
  the disposable Phase 5-3B-1 harness has favorable initial real-CCTV evidence,
  the production assisted-ROI backend and frontend integration now exist, but
  physical touch-device validation and operator deployment smoke validation have
  not occurred.
