from __future__ import annotations

from vigi_vision.object_presence_comparator import (
    _LumaNormalization,
    _make_support_change_facts,
)
from vigi_vision.object_presence_models import BinaryMask
from vigi_vision.object_presence_support_change_diagnostics import (
    SUPPORT_CHANGE_FACT_FIELDS,
    SupportChangeFacts,
    baseline_mask_fingerprint,
)


def _facts(
    baseline: tuple[float, ...],
    raw_probe: tuple[float, ...],
    normalized_probe: tuple[float, ...],
    normalization: _LumaNormalization,
    *,
    dx: int = 0,
) -> SupportChangeFacts:
    facts = _make_support_change_facts(
        baseline,
        raw_probe,
        normalized_probe,
        normalization,
        "a" * 64,
        len(baseline),
        len(baseline),
        1,
        1,
        1,
        dx,
        0,
        0,
        1.0,
        "aligned" if dx else "not_required",
        0.0,
    )
    assert facts is not None
    return facts


def test_exact_changed_count_and_support_population_decomposition() -> None:
    baseline = (100.0,) * 9
    probe = (100.0, 75.0, 70.0, 69.0, 68.0, 67.0, 66.0, 65.0, 60.0)
    facts = _facts(
        baseline,
        probe,
        probe,
        _LumaNormalization(100.0, 100.0, 1.0, ()),
    )

    assert facts.baseline_support_pixel_count == 9
    assert facts.valid_support_pixel_count == 9
    assert facts.excluded_support_pixel_count == 0
    assert facts.changed_support_pixel_count == 4
    assert facts.unchanged_support_pixel_count == 5
    assert facts.valid_support_pixel_count == (
        facts.changed_support_pixel_count + facts.unchanged_support_pixel_count
    )
    assert facts.baseline_support_pixel_count == (
        facts.valid_support_pixel_count + facts.excluded_support_pixel_count
    )
    assert facts.support_change_ratio == 0.444444


def test_histogram_contract_has_strict_greater_than_32_boundary() -> None:
    differences = (24.0, 30.0, 31.0, 32.0, 33.0, 34.0, 35.0, 40.0, 41.0)
    facts = _facts(
        (100.0,) * len(differences),
        tuple(100.0 - item for item in differences),
        tuple(100.0 - item for item in differences),
        _LumaNormalization(100.0, 100.0, 1.0, ()),
    )

    assert facts.changed_support_pixel_count == 5
    assert facts.unchanged_support_pixel_count == 4
    assert (
        facts.difference_le_24_count,
        facts.difference_gt_24_le_30_count,
        facts.difference_gt_30_le_32_count,
        facts.difference_gt_32_le_34_count,
        facts.difference_gt_34_le_40_count,
        facts.difference_gt_40_count,
    ) == (1, 1, 2, 2, 2, 1)
    assert sum(getattr(facts, name) for name in _histogram_field_names()) == 9
    assert (
        facts.difference_gt_32_le_34_count
        + facts.difference_gt_34_le_40_count
        + (facts.difference_gt_40_count)
        == facts.changed_support_pixel_count
    )


def _histogram_field_names() -> tuple[str, ...]:
    return (
        "difference_le_24_count",
        "difference_gt_24_le_30_count",
        "difference_gt_30_le_32_count",
        "difference_gt_32_le_34_count",
        "difference_gt_34_le_40_count",
        "difference_gt_40_count",
    )


def test_normalization_stages_and_exact_clipping_intersections() -> None:
    baseline = (20.0, 100.0, 220.0, 50.0, 200.0, 50.0)
    raw_probe = (40.0, 110.0, 200.0, 0.0, 150.0, 95.0)
    preclip = tuple(2.0 * item - 100.0 for item in raw_probe)
    clipped = tuple(min(255.0, max(0.0, item)) for item in preclip)
    facts = _facts(
        baseline,
        raw_probe,
        clipped,
        _LumaNormalization(100.0, 100.0, 2.0, ()),
    )

    assert facts.raw_probe_changed_count == 3
    assert facts.normalized_preclip_changed_count == 4
    assert facts.normalized_postclip_changed_count == 3
    assert facts.normalized_postclip_changed_count == facts.changed_support_pixel_count
    assert (
        facts.clipped_low_count,
        facts.clipped_high_count,
        facts.non_clipped_count,
    ) == (2, 1, 3)
    assert (
        facts.changed_and_clipped_low_count,
        facts.changed_and_clipped_high_count,
        facts.changed_and_non_clipped_count,
    ) == (1, 1, 1)
    assert facts.changed_support_pixel_count == (
        facts.changed_and_clipped_low_count
        + facts.changed_and_clipped_high_count
        + facts.changed_and_non_clipped_count
    )
    assert facts.valid_support_pixel_count == (
        facts.clipped_low_count + facts.clipped_high_count + facts.non_clipped_count
    )
    assert facts.changed_and_clipped_low_count <= facts.clipped_low_count
    assert facts.changed_and_clipped_high_count <= facts.clipped_high_count
    assert facts.changed_and_non_clipped_count <= facts.non_clipped_count
    assert all(
        abs(base - clipped_value) <= abs(base - affine_value)
        for base, clipped_value, affine_value in zip(baseline, clipped, preclip, strict=True)
    )


def test_selected_transform_and_mask_fingerprint_are_bounded_and_deterministic() -> None:
    mask = BinaryMask.from_rows(((True, False, True), (False, True, False), (True, False, False)))
    same_mask = BinaryMask.from_rows(
        ((True, False, True), (False, True, False), (True, False, False))
    )
    changed_mask = BinaryMask.from_rows(
        ((True, False, True), (False, True, False), (True, False, True))
    )
    fingerprint = baseline_mask_fingerprint(mask)
    assert fingerprint == baseline_mask_fingerprint(same_mask)
    assert fingerprint != baseline_mask_fingerprint(changed_mask)

    facts = _facts(
        (100.0,) * 9,
        (100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 60.0),
        (100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 60.0),
        _LumaNormalization(100.0, 100.0, 1.0, ()),
        dx=2,
    )
    assert facts.alignment_dx == 2
    assert facts.support_mask_fingerprint == "a" * 64
    assert set(facts.to_payload()) == SUPPORT_CHANGE_FACT_FIELDS
    assert not {"mask", "mask_rows", "support_coordinates", "pixels"} & set(facts.to_payload())


def test_malformed_optional_numeric_facts_are_discarded() -> None:
    valid = _facts(
        (100.0,) * 9,
        (100.0,) * 9,
        (100.0,) * 9,
        _LumaNormalization(100.0, 100.0, 1.0, ()),
    ).to_payload()
    for invalid in (10**1000, float("nan"), float("inf"), float("-inf"), "wrong"):
        malformed = dict(valid, normalization_scale=invalid)
        assert SupportChangeFacts.from_payload(malformed) is None
    assert SupportChangeFacts.from_payload(dict(valid, changed_support_pixel_count=True)) is None
