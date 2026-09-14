"""Production-shaped source-pixel fixtures for the successor support policy."""

from __future__ import annotations

from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.object_presence_comparator import ClassifierInput, ObjectPresenceClassifier
from vigi_vision.object_presence_models import BinaryMask, ClassificationResult, DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import ClassificationOutcome

WIDTH = 125
HEIGHT = 97
ROI = ConfirmationRoi(
    x=0,
    y=0,
    width=WIDTH,
    height=HEIGHT,
    coordinate_space="source_pixels",
    provenance=RoiProvenance.MANUAL,
)
OBJECT = (28, 22, 98, 75)
RgbPixel = tuple[int, int, int]


def _mask() -> BinaryMask:
    left, top, right, bottom = OBJECT
    return BinaryMask.from_rows(
        tuple(
            tuple(left <= x < right and top <= y < bottom for x in range(WIDTH))
            for y in range(HEIGHT)
        )
    )


def _image(
    *, shoe: bool = True, brightness: int = 0, person_patch: bool = False
) -> DecodedRgbImage:
    left, top, right, bottom = OBJECT
    rows: list[tuple[RgbPixel, ...]] = []
    for y in range(HEIGHT):
        row: list[RgbPixel] = []
        for x in range(WIDTH):
            background = 170 + ((x * 3 + y * 5) % 9) + brightness
            if person_patch and 4 <= x < 20 and 30 <= y < 82:
                background = 40 + ((x + y) % 7)
            if shoe and left <= x < right and top <= y < bottom:
                value = 35 + ((x * 11 + y * 7) % 35) + brightness
            else:
                value = background
            value = max(0, min(255, value))
            row.append((value, value, value))
        rows.append(tuple(row))
    return DecodedRgbImage.from_rows(tuple(rows))


def _classifier() -> ObjectPresenceClassifier:
    return ObjectPresenceClassifier(
        ObjectPresenceDecisionPolicy(
            classifier_policy_version="test-production-shaped-v3",
            classifier_preprocessing_version="test-production-shaped-v3",
            baseline_support_mode=True,
            baseline_support_alignment_mode=True,
            minimum_mask_overlap_for_comparison=0.1,
        )
    )


def _classify(probe: DecodedRgbImage, probe_mask: BinaryMask | None = None) -> ClassificationResult:
    mask = _mask()
    return _classifier().classify(ClassifierInput(_image(), probe, mask, probe_mask or mask, ROI))


def test_production_shaped_removed_object_is_absent_even_with_local_reveal_change() -> None:
    result = _classify(_image(shoe=False, person_patch=True))
    assert result.outcome is ClassificationOutcome.ABSENT
    comparison = result.comparison
    assert comparison.baseline_support_scene_stable is True
    assert comparison.baseline_support_stability_pixel_count == WIDTH * HEIGHT
    assert comparison.baseline_support_stability_valid_pixel_count is not None
    assert comparison.baseline_support_stability_changed_pixel_count is not None
    assert comparison.baseline_support_stability_excluded_pixel_count is not None
    assert comparison.baseline_support_alignment_state in {"aligned", "ambiguous", "not_required"}


def test_production_shaped_presence_survives_lighting_and_compression_variation() -> None:
    bright = _classify(_image(brightness=24))
    assert bright.outcome is ClassificationOutcome.PRESENT
    noisy = _image(brightness=3, person_patch=True)
    assert _classify(noisy).outcome is ClassificationOutcome.PRESENT


def test_production_shaped_occlusion_and_replacement_are_not_absent() -> None:
    occluded_rows: list[list[RgbPixel]] = [list(row) for row in _image().pixels]
    left, top, right, _bottom = OBJECT
    for y in range(top, top + 20):
        for x in range(left, right):
            occluded_rows[y][x] = (175, 175, 175)
    occluded = DecodedRgbImage.from_rows(tuple(tuple(row) for row in occluded_rows))
    assert _classify(occluded).outcome is ClassificationOutcome.INDETERMINATE

    replacement_rows: list[list[RgbPixel]] = [list(row) for row in _image().pixels]
    for y in range(OBJECT[1], OBJECT[3]):
        for x in range(OBJECT[0], OBJECT[2]):
            value = 80 + ((x + y) % 3)
            replacement_rows[y][x] = (value, value, value)
    replacement = DecodedRgbImage.from_rows(tuple(tuple(row) for row in replacement_rows))
    assert _classify(replacement).outcome is ClassificationOutcome.INDETERMINATE


def test_production_shaped_camera_translation_remains_indeterminate() -> None:
    baseline = _image()
    rows: list[tuple[RgbPixel, ...]] = []
    for y in range(HEIGHT):
        row: list[RgbPixel] = []
        for x in range(WIDTH):
            source_x, source_y = x - 8, y - 6
            if 0 <= source_x < WIDTH and 0 <= source_y < HEIGHT:
                row.append(baseline.pixels[source_y][source_x])
            else:
                row.append((170, 170, 170))
        rows.append(tuple(row))
    result = _classify(DecodedRgbImage.from_rows(tuple(rows)))
    assert result.outcome is ClassificationOutcome.INDETERMINATE
    assert result.comparison.baseline_support_alignment_state in {
        "aligned",
        "ambiguous",
        "no_valid_candidate",
    }


def test_probe_background_mask_does_not_change_baseline_identity() -> None:
    expanded = BinaryMask.from_rows(tuple(tuple(True for _ in range(WIDTH)) for _ in range(HEIGHT)))
    result = _classify(_image(shoe=False), expanded)
    assert result.outcome is ClassificationOutcome.ABSENT
