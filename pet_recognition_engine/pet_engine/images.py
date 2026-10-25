"""Open pictures safely, and cut the animals out of them.

A picture comes from any image or camera entity, so it is never trusted: only
JPEG, PNG and WebP are read, the size is checked before the pixels are
decoded, and any failure of the decoder becomes `UnreadableImageError`, so that a
bad picture never stops the pictures after it.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps

FORMATS = ("JPEG", "PNG", "WEBP")
MAX_PIXELS = 40_000_000
# The longest side of the crops returned: enough to show an animal and to
# embed it again, as the embedder works on 224 pixels.
MAX_CROP_SIDE = 512


class UnreadableImageError(ValueError):
    """The data is not a picture the engine reads."""


@dataclass(frozen=True)
class Box:
    """A rectangle in a picture, in pixels."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        """The width, zero for an empty box."""
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        """The height, zero for an empty box."""
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        """The area in square pixels."""
        return self.width * self.height

    def clipped(self, width: int, height: int) -> Box:
        """The part of the box inside a picture of `width` by `height`."""
        return Box(
            min(max(self.x0, 0.0), width),
            min(max(self.y0, 0.0), height),
            min(max(self.x1, 0.0), width),
            min(max(self.y1, 0.0), height),
        )


def open_image(data: bytes, max_pixels: int = MAX_PIXELS) -> Image.Image:
    """The picture in `data`, in RGB and turned upright from its EXIF orientation.

    Raises UnreadableImageError for another format, a picture of more than
    `max_pixels` pixels, or data the decoder fails on.
    """
    try:
        with warnings.catch_warnings():
            # Pillow only warns about a picture it deems too large to be safe.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data), formats=FORMATS)
        with image:
            if image.width * image.height > max_pixels:
                raise UnreadableImageError(f"the picture has more than {max_pixels} pixels")
            return _rgb(ImageOps.exif_transpose(image))
    except UnreadableImageError:
        raise
    except Exception as err:
        # Pillow raises many kinds of errors on damaged data, not only OSError.
        raise UnreadableImageError(f"the picture cannot be read: {err}") from err


def _rgb(image: Image.Image) -> Image.Image:
    """`image` in 8-bit RGB. Pillow clips 16-bit grey levels to white, so they
    are scaled down to 8 bits first."""
    if image.mode.startswith("I;16"):
        levels = np.asarray(image, dtype=np.float64)
        image = Image.fromarray((levels * (255.0 / 65535.0)).round().astype(np.uint8))
    return image.convert("RGB")


def crop(image: Image.Image, box: Box, margin: float = 0.08) -> Image.Image:
    """The part of `image` in `box`, with `margin` of its size around it."""
    dx, dy = box.width * margin, box.height * margin
    area = Box(box.x0 - dx, box.y0 - dy, box.x1 + dx, box.y1 + dy).clipped(
        image.width, image.height
    )
    # At least one pixel each way, so that even a sliver can be embedded.
    left = min(int(area.x0), image.width - 1)
    top = min(int(area.y0), image.height - 1)
    right = max(round(area.x1), left + 1)
    bottom = max(round(area.y1), top + 1)
    return image.crop((left, top, right, bottom))


def encode_jpeg(image: Image.Image, side: int = MAX_CROP_SIDE, quality: int = 90) -> bytes:
    """`image` as JPEG data, shrunk to at most `side` pixels on its longest side."""
    if max(image.size) > side:
        image = image.copy()
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality)
    return output.getvalue()
