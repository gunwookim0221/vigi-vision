from pathlib import Path

import pytest
from tests.test_investigation_confirmation import build_context, build_request

from vigi_vision.investigation_confirmation_models import ConfirmationArtifactError
from vigi_vision.reference_frame_models import ReferenceFrameResourceNotFoundError
from vigi_vision.reference_frame_resources import (
    ReferenceFrameResourceMetadata,
    ReferenceFrameResourceStore,
)


def test_resource_disappearance_during_publication_cleans_invocation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    context = build_context(tmp_path)
    original = ReferenceFrameResourceStore.resolve_resource
    calls = 0

    def disappear(
        store: ReferenceFrameResourceStore, resource_id: str
    ) -> ReferenceFrameResourceMetadata:
        nonlocal calls
        calls += 1
        metadata = original(store, resource_id)
        if calls == 2:
            metadata.jpeg_path.unlink()
            raise ConfirmationArtifactError
        return metadata

    monkeypatch.setattr(ReferenceFrameResourceStore, "resolve_resource", disappear)

    # When / Then
    with pytest.raises(ConfirmationArtifactError):
        _ = context.service.confirm(build_request(context.resource_id))
    assert list(context.investigation_root.iterdir()) == []


def test_unknown_resource_is_rejected_by_the_trusted_store(tmp_path: Path) -> None:
    # Given
    context = build_context(tmp_path)

    # When / Then
    with pytest.raises(ReferenceFrameResourceNotFoundError):
        _ = context.service.confirm(build_request("unknown-resource"))
