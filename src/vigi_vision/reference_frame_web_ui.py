"""Install the static Phase 4C-2 reference-frame browser shell."""

from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_ASSET_ROOT: Final = Path(__file__).with_name("reference_frame_web")
_INDEX: Final = _ASSET_ROOT / "index.html"


def install_reference_frame_web_ui(app: FastAPI) -> None:
    """Mount the local UI root route and static assets on an application."""
    app.mount("/static", StaticFiles(directory=_ASSET_ROOT), name="static")

    async def get_web_ui() -> FileResponse:
        return FileResponse(_INDEX, media_type="text/html")

    app.add_api_route("/", get_web_ui, methods=["GET"], include_in_schema=False)
