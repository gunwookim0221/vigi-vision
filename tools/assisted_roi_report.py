from __future__ import annotations

from dataclasses import dataclass

from tools.assisted_roi_session import (
    ChannelSummary,
    MetricSummary,
    SessionDocument,
    SessionItem,
    channel_metrics_for,
)


@dataclass(frozen=True, slots=True)
class EnvironmentInfo:
    python_version: str
    platform: str
    device: str


def _format_optional(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def _format_score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _channel_rows(rows: tuple[ChannelSummary, ...]) -> str:
    if not rows:
        return "| n/a | 0 | 0 | 0 | 0 | 0 | 0.00% |"
    return "\n".join(
        f"| {row.channel_id} | {row.total} | {row.evaluated} | {row.success} | "
        f"{row.partial} | {row.failure} | {row.success_rate:.2f}% |"
        for row in rows
    )


def _bbox_text(item: SessionItem) -> str:
    if item.bbox is None:
        return "n/a"
    box = item.bbox
    return f"({box.x}, {box.y}, {box.width}, {box.height})"


def _item_rows(document: SessionDocument) -> str:
    if not document.items:
        return "| n/a | n/a | n/a | n/a | n/a | n/a | n/a |"
    return "\n".join(
        f"| {item.source_path} | {item.channel_id or 'n/a'} | {item.classification} | "
        f"{item.mask_pixel_count if item.mask_pixel_count is not None else 'n/a'} | "
        f"{_format_percent(item.mask_coverage_percent)} | {_bbox_text(item)} | "
        f"{_format_score(item.selected_score)} | {item.failure_reason or 'n/a'} |"
        for item in document.items
    )


def render_summary(
    document: SessionDocument,
    metrics: MetricSummary,
    environment: EnvironmentInfo,
    output_directory: str,
) -> str:
    channels = _channel_rows(channel_metrics_for(document.items))
    items = _item_rows(document)
    return (
        "# Assisted ROI Validation\n\n"
        "This report is disposable validation evidence only. It does not change\n"
        "reference-frame artifacts, manifests, or production ROI state.\n\n"
        "## Decision\n\n"
        f"- Recommendation: `{metrics.recommendation}`\n"
        f"- Total discovered: {metrics.total}\n"
        f"- Evaluated: {metrics.evaluated}\n"
        f"- Success: {metrics.success}\n"
        f"- Partial: {metrics.partial}\n"
        f"- Failure: {metrics.failure}\n"
        f"- Skipped: {metrics.skipped}\n"
        f"- Success rate: {metrics.success_rate:.2f}%\n"
        "- Threshold: at least 20 evaluated images; partial results are not successes.\n\n"
        "## Timing\n\n"
        f"- Average inference: {_format_optional(metrics.average_inference_ms)} ms\n"
        f"- Median inference: {_format_optional(metrics.median_inference_ms)} ms\n"
        f"- P95 inference: {_format_optional(metrics.p95_inference_ms)} ms\n\n"
        "## Per-channel results\n\n"
        "| Channel | Total | Evaluated | Success | Partial | Failure | Success rate |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |\n"
        f"{channels}\n\n"
        "## Per-item evidence\n\n"
        "| Source | Channel | Classification | Mask pixels | Coverage | Bbox | Score | Failure |\n"
        "| --- | ---: | --- | ---: | ---: | --- | ---: | --- |\n"
        f"{items}\n\n"
        "## Environment\n\n"
        f"- Python: `{environment.python_version}`\n"
        f"- Platform: `{environment.platform}`\n"
        f"- Device: `{environment.device}`\n"
        f"- Checkpoint: `{document.checkpoint_name}`\n"
        f"- Expected SHA-256: `{document.expected_sha256}`\n"
        f"- Verified SHA-256: `{document.actual_sha256 or 'not requested'}`\n"
        f"- Validation output: `{output_directory}`\n\n"
        "## Classification guide\n\n"
        "- `success`: the mask is a useful editable whole-object starting ROI.\n"
        "- `partial`: the mask is usable only after substantial manual correction.\n"
        "- `failure`: inference completed but the suggestion is unusable.\n"
        "- `skip`: not evaluated or intentionally omitted.\n"
    )
