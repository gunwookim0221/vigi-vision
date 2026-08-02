import pytest
from pydantic import ValidationError

from vigi_vision.investigation_confirmation_models import ConfirmationRoi, RoiProvenance


def _valid_roi() -> dict[str, bool | float | int | str]:
    return {
        "x": 4,
        "y": 4,
        "width": 8,
        "height": 8,
        "coordinate_space": "source_pixels",
        "provenance": "manual",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("width", 3), ("height", 3), ("x", -1), ("y", -1)],
)
def test_roi_model_rejects_invalid_geometry(field: str, value: int) -> None:
    # Given
    values = _valid_roi()
    values[field] = value

    # When / Then
    with pytest.raises(ValidationError) as error:
        _ = ConfirmationRoi.model_validate(values)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("value", [True, 4.0, "4"])
def test_roi_model_rejects_non_strict_integer_values(*, value: bool | float | str) -> None:
    # Given
    values = _valid_roi()
    values["x"] = value

    # When / Then
    with pytest.raises(ValidationError) as error:
        _ = ConfirmationRoi.model_validate(values)
    assert error.value.errors()[0]["loc"] == ("x",)


def test_valid_boundary_roi_is_accepted() -> None:
    # Given
    values = _valid_roi()
    values.update(x=1272, y=712, width=8, height=8)

    # When
    roi = ConfirmationRoi.model_validate(values)

    # Then
    assert roi.coordinate_space == "source_pixels"
    assert roi.x + roi.width == 1280
    assert roi.y + roi.height == 720


def test_invalid_coordinate_space_is_rejected_at_coordinate_space() -> None:
    # Given
    values = _valid_roi()
    values["coordinate_space"] = "normalized"

    # When / Then
    with pytest.raises(ValidationError) as error:
        _ = ConfirmationRoi.model_validate(values)
    assert error.value.errors()[0]["loc"] == ("coordinate_space",)


def test_invalid_half_open_ordering_is_rejected_at_width() -> None:
    # Given
    values = _valid_roi()
    values["width"] = 0

    # When / Then
    with pytest.raises(ValidationError) as error:
        _ = ConfirmationRoi.model_validate(values)
    assert error.value.errors()[0]["loc"] == ("width",)


@pytest.mark.parametrize("provenance", list(RoiProvenance))
def test_all_designed_roi_provenance_values_round_trip(provenance: RoiProvenance) -> None:
    # Given
    roi = ConfirmationRoi(
        x=0,
        y=0,
        width=4,
        height=4,
        coordinate_space="source_pixels",
        provenance=provenance,
    )

    # When / Then
    assert roi.provenance is provenance
