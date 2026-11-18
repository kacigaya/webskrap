"""Geometry and image metadata returned by the CDP driver."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class Viewport(TypedDict):
    """Geometry in CSS pixels, including the document's scroll offset."""

    width: int
    height: int
    scroll_x: float
    scroll_y: float


@dataclass(frozen=True)
class BrowserView:
    """One viewport PNG and its coordinate metadata, without filesystem writes."""

    image: bytes
    url: str
    title: str
    width: int
    height: int
    scroll_x: float
    scroll_y: float

    def metadata(self) -> dict[str, str | int | float]:
        """Describe the original image's one-to-one CSS coordinate mapping."""
        return {
            "url": self.url,
            "title": self.title,
            "width": self.width,
            "height": self.height,
            "scroll_x": self.scroll_x,
            "scroll_y": self.scroll_y,
            "coordinate_system": "viewport-css-pixels",
            "mime_type": "image/png",
        }
