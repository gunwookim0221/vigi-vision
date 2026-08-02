from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from vigi_vision.assisted_roi_service import RoiSuggestion


class RoiSuggestionPointBody(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    x: StrictInt = Field(ge=0)
    y: StrictInt = Field(ge=0)


class RoiSuggestionBody(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    point: RoiSuggestionPointBody


class RoiSuggestionBoxResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    x: StrictInt = Field(ge=0)
    y: StrictInt = Field(ge=0)
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)


class RoiMaskPreviewResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    rows: tuple[tuple[tuple[StrictInt, StrictInt], ...], ...]


class RoiSuggestionResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    resource_id: str
    source_width: StrictInt = Field(gt=0)
    source_height: StrictInt = Field(gt=0)
    bbox: RoiSuggestionBoxResponse
    mask_preview: RoiMaskPreviewResponse


def roi_suggestion_response(result: RoiSuggestion) -> RoiSuggestionResponse:
    return RoiSuggestionResponse(
        resource_id=result.resource_id,
        source_width=result.source_width,
        source_height=result.source_height,
        bbox=RoiSuggestionBoxResponse(
            x=result.bbox.x,
            y=result.bbox.y,
            width=result.bbox.width,
            height=result.bbox.height,
        ),
        mask_preview=RoiMaskPreviewResponse(
            width=result.mask_preview.width,
            height=result.mask_preview.height,
            rows=result.mask_preview.rows,
        ),
    )
