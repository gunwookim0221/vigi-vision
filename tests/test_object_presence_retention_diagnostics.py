"""Bounded foreground-retention aggregates remain outside comparison authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typing_extensions import Self

from test_object_presence_classification import (
    _shift_image,
    _support_classifier,
    _support_input,
    _support_scene,
)
from vigi_vision.object_presence_comparator import _support_luma_metrics
from vigi_vision.object_presence_evidence import ClassificationResult
from vigi_vision.object_presence_retention_diagnostics import ForegroundRetentionFacts
from vigi_vision.object_presence_values import ClassificationOutcome, DecodedRgbImage
from vigi_vision.recording_search_7e_b4_process import (
    PROTOCOL_VERSION,
    B4ProcessError,
    StaticMaskWorkerSpec,
    _decode_result,
    run_b4_in_process,
)
from vigi_vision.recording_search_successor_diagnostics import (
    SuccessorDiagnosticRepository,
    diagnostic_scope,
    persist_foreground_retention,
)

_INVESTIGATION_ID = "object-disappearance-v3-ch1-20260904T051732Z"
_RUN_ID = "search-run-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_OBSERVATION_ID = f"successor-observation-v1-{'f' * 64}"


def _metrics_with_facts(
    baseline: tuple[float, ...],
    probe: tuple[float, ...],
    baseline_background: tuple[float, ...],
    probe_background: tuple[float, ...],
) -> tuple[tuple[object, ...], ForegroundRetentionFacts]:
    facts: list[ForegroundRetentionFacts] = []
    result = _support_luma_metrics(
        baseline,
        probe,
        baseline_background,
        probe_background,
        (tuple(range(len(baseline_background))), len(baseline_background)),
        retention_sink=facts.append,
        baseline_support_pixel_count=len(baseline),
    )
    assert len(facts) == 1
    return result, facts[0]


def _authoritative_result() -> ClassificationResult:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    return classifier.classify(_support_input(_support_scene()))


def _worker_result_payload(result: object, facts: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "correlation_id": "retention-diagnostic-decode-test",
            "kind": "result",
            "result": result.model_dump(mode="json"),
            "retention_diagnostic": facts,
        }
    ).encode("utf-8")


def _valid_facts_payload() -> dict[str, object]:
    return _metrics_with_facts(
        (60.0, 80.0, 90.0, 120.0, 240.0),
        (50.0, 80.0, 60.0, 60.0, 80.0),
        (40.0,) * 5,
        (40.0,) * 5,
    )[1].to_payload()


def test_retention_counts_decompose_exactly() -> None:
    _, facts = _metrics_with_facts(
        (60.0, 80.0, 90.0, 120.0, 240.0),
        (50.0, 80.0, 60.0, 60.0, 80.0),
        (40.0,) * 5,
        (40.0,) * 5,
    )

    assert facts.baseline_support_pixel_count == 5
    assert facts.retention_support_population_pixel_count == 5
    assert facts.eligible_baseline_foreground_pixel_count == 4
    assert facts.retained_foreground_pixel_count == 1
    assert facts.absolute_floor_rejection_count == 2
    assert facts.relative_retention_rejection_count == 1
    assert facts.eligible_baseline_foreground_pixel_count == (
        facts.retained_foreground_pixel_count
        + facts.absolute_floor_rejection_count
        + facts.relative_retention_rejection_count
    )


def test_contrast_histograms_use_fixed_absolute_contrast_buckets() -> None:
    _, facts = _metrics_with_facts(
        (60.0, 80.0, 90.0, 120.0, 240.0),
        (50.0, 80.0, 60.0, 60.0, 80.0),
        (40.0,) * 5,
        (40.0,) * 5,
    )

    assert facts.baseline_contrast_histogram == (1, 0, 1, 1, 2)
    assert facts.probe_contrast_histogram == (3, 0, 2, 0, 0)
    assert sum(facts.baseline_contrast_histogram) == 5
    assert sum(facts.probe_contrast_histogram) == 5


def test_normalization_medians_scale_offset_and_clipping_are_recorded() -> None:
    _, facts = _metrics_with_facts(
        (40.0, 160.0),
        (0.0, 255.0),
        (40.0, 70.0, 100.0, 130.0, 160.0),
        (70.0, 85.0, 100.0, 115.0, 130.0),
    )

    assert facts.baseline_background_pixel_count == 5
    assert facts.probe_background_pixel_count == 5
    assert facts.baseline_background_median == 100.0
    assert facts.probe_background_median == 100.0
    assert facts.normalization_scale == 2.0
    assert facts.normalization_offset == -100.0
    assert facts.normalization_clipped_low_count == 1
    assert facts.normalization_clipped_high_count == 1


def test_diagnostic_instrumentation_preserves_public_results_and_metrics() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    baseline = _support_scene()
    occluded_rows = [list(row) for row in baseline.pixels]
    for y in range(7, 11):
        for x in range(7, 15):
            occluded_rows[y][x] = (180, 180, 180)
    occluded = DecodedRgbImage.from_rows(tuple(tuple(row) for row in occluded_rows))
    replacement_rows = [list(row) for row in baseline.pixels]
    for y in range(7, 15):
        for x in range(7, 15):
            value = 70 + ((x + y) % 3)
            replacement_rows[y][x] = (value, value, value)
    replacement = DecodedRgbImage.from_rows(tuple(tuple(row) for row in replacement_rows))
    cases = (
        (_support_input(baseline), ClassificationOutcome.PRESENT),
        (_support_input(_support_scene(shoe=False)), ClassificationOutcome.ABSENT),
        (_support_input(occluded), ClassificationOutcome.INDETERMINATE),
        (_support_input(replacement), ClassificationOutcome.INDETERMINATE),
        (_support_input(_shift_image(baseline, 5, 5)), ClassificationOutcome.INDETERMINATE),
    )

    for values, expected_outcome in cases:
        before = classifier.classify(values)
        diagnostics: list[ForegroundRetentionFacts] = []
        after = classifier.classify(values, retention_sink=diagnostics.append)
        assert before.outcome is expected_outcome
        assert after.model_dump(mode="json") == before.model_dump(mode="json")
        assert after.comparison == before.comparison
        assert diagnostics


def test_broken_retention_sink_does_not_change_comparison() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    values = _support_input(_support_scene())

    def broken_sink(_facts: ForegroundRetentionFacts) -> None:
        raise RuntimeError

    before = classifier.classify(values)
    after = classifier.classify(values, retention_sink=broken_sink)
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_spawned_b4_returns_retention_facts_outside_classification_result() -> None:
    classifier = _support_classifier(
        classifier_policy_version="test-baseline-support-v3",
        classifier_preprocessing_version="test-baseline-support-v3",
        baseline_support_alignment_mode=True,
    )
    values = _support_input(_support_scene())
    expected = classifier.classify(values)
    diagnostics: list[ForegroundRetentionFacts] = []

    actual = run_b4_in_process(
        baseline_image=values.baseline_image,
        probe_image=values.probe_image,
        source_width=values.baseline_image.width,
        source_height=values.baseline_image.height,
        roi=values.roi,
        policy=classifier.policy,
        worker_spec=StaticMaskWorkerSpec(values.baseline_mask, values.probe_mask),
        correlation_id="retention-diagnostic-worker-test",
        timeout_seconds=3.0,
        retention_sink=diagnostics.append,
    )

    assert actual == expected
    assert len(diagnostics) == 1
    assert diagnostics[0].baseline_support_pixel_count == sum(
        cell for row in values.baseline_mask.rows for cell in row
    )


def test_huge_integer_in_optional_float_discards_facts_and_returns_result() -> None:
    result = _authoritative_result()
    facts = _valid_facts_payload()
    facts["baseline_background_median"] = 10**1000
    diagnostics: list[ForegroundRetentionFacts] = []

    decoded = _decode_result(
        _worker_result_payload(result, facts),
        "retention-diagnostic-decode-test",
        retention_sink=diagnostics.append,
    )

    assert decoded == result
    assert diagnostics == []


@pytest.mark.parametrize("invalid", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_optional_numeric_discards_facts_and_returns_result(invalid: float) -> None:
    result = _authoritative_result()
    facts = _valid_facts_payload()
    facts["normalization_offset"] = invalid
    diagnostics: list[ForegroundRetentionFacts] = []

    decoded = _decode_result(
        _worker_result_payload(result, facts),
        "retention-diagnostic-decode-test",
        retention_sink=diagnostics.append,
    )

    assert decoded == result
    assert diagnostics == []


@pytest.mark.parametrize("invalid", ["100.0", {"value": 100.0}])
def test_invalid_optional_numeric_type_discards_facts_and_returns_result(
    invalid: object,
) -> None:
    result = _authoritative_result()
    facts = _valid_facts_payload()
    facts["normalization_scale"] = invalid
    diagnostics: list[ForegroundRetentionFacts] = []

    decoded = _decode_result(
        _worker_result_payload(result, facts),
        "retention-diagnostic-decode-test",
        retention_sink=diagnostics.append,
    )

    assert decoded == result
    assert diagnostics == []


@pytest.mark.parametrize("error_type", [OverflowError, ValueError, TypeError])
def test_numeric_conversion_errors_discard_optional_fact_set(error_type: type[Exception]) -> None:
    class ConversionErrorInt(int):
        failure_type: type[Exception]

        def __new__(cls, value: int, failure_type: type[Exception]) -> Self:
            instance = super().__new__(cls, value)
            instance.failure_type = failure_type
            return instance

        def __float__(self) -> float:
            raise self.failure_type()

    facts = _valid_facts_payload()
    facts["baseline_background_median"] = ConversionErrorInt(100, error_type)

    assert ForegroundRetentionFacts.from_payload(facts) is None


def test_valid_optional_facts_decode_and_persist_without_changing_result(
    tmp_path: Path,
) -> None:
    result = _authoritative_result()
    facts = _valid_facts_payload()
    diagnostics: list[ForegroundRetentionFacts] = []
    decoded = _decode_result(
        _worker_result_payload(result, facts),
        "retention-diagnostic-decode-test",
        retention_sink=diagnostics.append,
    )

    assert decoded == result
    assert len(diagnostics) == 1
    assert diagnostics[0].to_payload() == facts
    repository = SuccessorDiagnosticRepository(tmp_path)
    with diagnostic_scope(repository, _INVESTIGATION_ID, _RUN_ID):
        persist_foreground_retention(
            plan_id="successor-plan-decoder-test",
            target_id="successor-target-decoder-test",
            observation_id=_OBSERVATION_ID,
            requested_time_utc="2026-09-04T05:18:00Z",
            reference_identity=f"successor-authority-v1-{'a' * 64}",
            reference_frame_resource_id="NVR_Channel_01-Reference-Frame",
            roi_identity=f"successor-roi-v1-{'b' * 64}",
            classifier_policy_identity=f"policy-{'c' * 64}",
            facts=diagnostics[0],
        )

    persisted = repository.read_retention(_INVESTIGATION_ID, _RUN_ID, _OBSERVATION_ID)
    assert persisted is not None
    assert {key: persisted[key] for key in facts} == facts


def test_invalid_authoritative_result_still_fails_with_valid_diagnostic() -> None:
    result = _authoritative_result()
    facts = _valid_facts_payload()
    payload = json.loads(_worker_result_payload(result, facts))
    payload["result"]["outcome"] = "NOT_A_CLASSIFICATION_OUTCOME"
    diagnostics: list[ForegroundRetentionFacts] = []

    with pytest.raises(B4ProcessError, match="invalid_classifier_output"):
        _decode_result(
            json.dumps(payload).encode("utf-8"),
            "retention-diagnostic-decode-test",
            retention_sink=diagnostics.append,
        )
    assert diagnostics == []
