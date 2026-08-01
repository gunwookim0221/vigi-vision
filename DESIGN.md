# VIGI Vision Design System

## 0. Research Log

- Embedded references: shortlisted `linear.app`, `notion`, and `sentry` for a
  local operations surface; selected the restrained density, clear status
  hierarchy, and cool neutral palette informed by `linear.app`.
- Lazyweb: skipped for this local Phase 5-1 ROI slice; external product
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
  rejected tiny drag, and interrupted drag.
- **Interaction:** mouse, touch, and pen use one Pointer Events path; only one
  pointer is active, and additional pointers are ignored.
- **Mobile:** `touch-action: none` is scoped to the image surface so document
  scrolling remains available outside it.
- **Geometry:** coordinates use loaded `naturalWidth`/`naturalHeight`, with
  endpoint `Math.round()` conversion, source-bound clamping, and a 4×4 source
  pixel minimum. The overlay is recalculated from canonical source pixels after
  image load and responsive resize.
- **Persistence:** the one ROI is transient frontend state. Persistence and
  confirmation remain deferred to Phase 6.

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
- ROI pointer drawing is intentionally limited to pointer input in Phase 5-1;
  keyboard creation, movement, resize handles, and full keyboard accessibility
  are deferred to Phase 5-2.
- Accepted debt: no real-browser/NVR validation has occurred yet. The exit is a
  user-run loopback browser check against a known recorded KST time.
