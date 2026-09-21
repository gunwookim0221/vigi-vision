from __future__ import annotations

from types import SimpleNamespace

import pytest

import vigi_vision.recording_search_successor_execution as execution_module
from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance
from vigi_vision.object_presence_models import BinaryMask, DecodedRgbImage
from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_7e_b4_process import (
    B4ProcessCancelled,
    B4ProcessTimeout,
    StaticMaskWorkerSpec,
)
from vigi_vision.recording_search_7e_public import approved_successor_object_presence_policy
from vigi_vision.recording_search_successor_classification import (
    SuccessorClassificationCancelledError,
    SuccessorClassificationError,
)


def _inputs() -> tuple[DecodedRgbImage, ConfirmationRoi, StaticMaskWorkerSpec]:
    image = DecodedRgbImage.from_rows(tuple(tuple((0, 0, 0) for _ in range(4)) for _ in range(4)))
    roi = ConfirmationRoi(
        x=0,
        y=0,
        width=4,
        height=4,
        coordinate_space="source_pixels",
        provenance=RoiProvenance.MANUAL,
    )
    mask = BinaryMask.from_rows(tuple(tuple(True for _ in range(4)) for _ in range(4)))
    return image, roi, StaticMaskWorkerSpec(mask, mask)


def test_successor_b4_uses_long_candidate_budget_and_keeps_startup_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image, roi, worker_spec = _inputs()
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(
            outcome=ClassificationOutcome.PRESENT,
            reason_code=None,
            comparison=None,
        )

    monkeypatch.setattr(execution_module, "run_b4_in_process", fake_run)
    classifier = execution_module.SuccessorB4Classifier(
        approved_successor_object_presence_policy(), worker_spec
    )

    result = classifier.classify_with_baseline_mask(
        image,
        image,
        4,
        4,
        roi,
        "timeout-policy-candidate",
        baseline_mask=worker_spec.baseline_mask,
    )

    assert result.outcome is ClassificationOutcome.PRESENT
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 60.0
    assert calls[0]["startup_timeout_seconds"] == 30.0
    assert calls[0]["baseline_mask"] == worker_spec.baseline_mask


def test_reference_preparation_keeps_existing_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image, roi, worker_spec = _inputs()
    calls: list[dict[str, object]] = []
    mask = worker_spec.baseline_mask

    def fake_run(**kwargs: object) -> object:
        calls.append(kwargs)
        return mask

    monkeypatch.setattr(execution_module, "run_b4_in_process", fake_run)
    classifier = execution_module.SuccessorB4Classifier(
        approved_successor_object_presence_policy(), worker_spec
    )

    assert classifier.prepare_reference(image, 4, 4, roi, "timeout-policy-reference") == mask
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 30.0
    assert calls[0]["startup_timeout_seconds"] == 30.0
    assert calls[0]["reference_only"] is True


def test_b4_timeout_preserves_fail_closed_result_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image, roi, worker_spec = _inputs()
    calls = 0

    def fake_run(**_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise B4ProcessTimeout(stage="inference")

    monkeypatch.setattr(execution_module, "run_b4_in_process", fake_run)
    classifier = execution_module.SuccessorB4Classifier(
        approved_successor_object_presence_policy(), worker_spec
    )

    with pytest.raises(SuccessorClassificationError) as raised:
        _ = classifier.classify(image, image, 4, 4, roi, "timeout-policy-timeout")

    assert str(raised.value) == "classifier_timeout"
    assert calls == 1


def test_successor_b4_propagates_existing_cancellation_and_does_not_map_it_to_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image, roi, worker_spec = _inputs()
    cancellation_calls = 0
    calls: list[dict[str, object]] = []

    def cancellation() -> bool:
        nonlocal cancellation_calls
        cancellation_calls += 1
        return True

    def fake_run(**kwargs: object) -> object:
        calls.append(kwargs)
        assert kwargs["cancellation"] is cancellation
        assert cancellation()
        raise B4ProcessCancelled

    monkeypatch.setattr(execution_module, "run_b4_in_process", fake_run)
    classifier = execution_module.SuccessorB4Classifier(
        approved_successor_object_presence_policy(), worker_spec
    )

    with pytest.raises(SuccessorClassificationCancelledError):
        _ = classifier.classify(
            image,
            image,
            4,
            4,
            roi,
            "cancellation-propagation",
            cancellation=cancellation,
        )

    assert calls
    assert cancellation_calls == 1


def test_successor_b4_normal_result_remains_unchanged_with_optional_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image, roi, worker_spec = _inputs()
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(
            outcome=ClassificationOutcome.PRESENT,
            reason_code=None,
            comparison=None,
        )

    monkeypatch.setattr(execution_module, "run_b4_in_process", fake_run)
    classifier = execution_module.SuccessorB4Classifier(
        approved_successor_object_presence_policy(), worker_spec
    )

    result = classifier.classify(image, image, 4, 4, roi, "normal-without-cancel")

    assert result.outcome is ClassificationOutcome.PRESENT
    assert calls[0]["cancellation"] is None
