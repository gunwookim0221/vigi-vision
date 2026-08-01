# VIGI Vision Design System

## 0. Research Log

- Embedded references: shortlisted `linear.app`, `notion`, and `sentry` for a
  local operations surface; selected the restrained density, clear status
  hierarchy, and cool neutral palette informed by `linear.app`.
- Lazyweb: skipped for this local Phase 4C-1 shell; external product
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

- **Structure:** labeled channel input, labeled local datetime input, KST hint,
  and native submit button.
- **States:** default, focus, disabled/loading, top-level error.
- **Accessibility:** native labels, required controls, keyboard submission,
  visible focus, and a live status region.
- **Layout:** intrinsic grid; document scroll is the sole scroll owner.

### Candidate result row

- **Structure:** ordered media area, offset heading, requested offset/time,
  textual succeeded/failed status, then created/reused outcome or fixed safe
  failure facts.
- **States:** succeeded with thumbnail, image unavailable, failed, partial-set
  context, all-failed context, and empty response.
- **Accessibility:** semantic list item; status words supplement color;
  meaningful image alternative text, controlled image failure status, and
  messages inserted as text, never HTML.
- **Layout:** responsive two-column media/details grid on wide screens and
  vertical stack on narrow screens. Server response order is authoritative.

## 6. Motion & Interaction

Only button hover/active and opacity transitions communicate affordance and
request-state change. They use 150ms ease-out and disable under
`prefers-reduced-motion`.

## 7. Depth & Surface

The surface strategy is tonal shift with a single low-contrast border. Cards
use `--surface-secondary` against `--surface-primary`; no decorative shadows
or glass effects are used.

## 8. Accessibility Constraints & Accepted Debt

- Target WCAG 2.2 AA: visible keyboard focus, labeled controls, semantic
  landmarks, live status/error announcements, and 4.5:1 body-text contrast.
- The form uses a local datetime value interpreted as KST by the Phase 4A API;
  the static helper text makes this explicit.
- Accepted debt: no real-browser/NVR validation has occurred yet. The exit is a
  user-run loopback browser check against a known recorded KST time.
