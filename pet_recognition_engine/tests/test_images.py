"""Tests of opening pictures and cutting animals out."""

import io

import pytest
from helpers import picture
from PIL import Image

from pet_engine.images import (
    MAX_CROP_SIDE,
    Box,
    UnreadableImageError,
    crop,
    encode_jpeg,
    open_image,
)


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_supported_formats_open_in_rgb(image_format: str) -> None:
    image = open_image(picture(image_format=image_format))
    assert (image.mode, image.size) == ("RGB", (320, 240))


def test_a_transparent_picture_opens_in_rgb() -> None:
    output = io.BytesIO()
    Image.new("RGBA", (8, 6), (12, 34, 56, 0)).save(output, format="PNG")
    assert open_image(output.getvalue()).mode == "RGB"


def test_the_exif_orientation_turns_the_picture_upright() -> None:
    # Stored on its side: the left of the stored picture is the top of the scene.
    stored = Image.new("RGB", (320, 240), (0, 0, 255))
    stored.paste((255, 0, 0), (0, 0, 160, 240))
    exif = Image.Exif()
    exif[0x0112] = 6
    output = io.BytesIO()
    stored.save(output, format="PNG", exif=exif.tobytes())
    image = open_image(output.getvalue())
    assert image.size == (240, 320)
    assert image.getpixel((120, 40)) == (255, 0, 0)
    assert image.getpixel((120, 280)) == (0, 0, 255)


def test_sixteen_bit_grey_keeps_its_levels() -> None:
    output = io.BytesIO()
    Image.new("I;16", (4, 4), 30000).save(output, format="PNG")
    red, green, blue = open_image(output.getvalue()).getpixel((0, 0))
    assert red == green == blue == 117


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a picture",
        picture()[:200],
        picture(image_format="GIF"),
        picture(image_format="BMP"),
    ],
)
def test_unreadable_data_is_refused(data: bytes) -> None:
    with pytest.raises(UnreadableImageError):
        open_image(data)


def test_a_picture_too_large_is_refused_before_decoding() -> None:
    data = picture(400, 300)
    assert open_image(data, max_pixels=400 * 300)
    with pytest.raises(UnreadableImageError, match="more than"):
        open_image(data, max_pixels=400 * 300 - 1)
    # Damaged pixel data is never reached: only the header is read.
    output = io.BytesIO()
    Image.new("1", (8000, 6000)).save(output, format="PNG")
    header = output.getvalue()[:64] + b"damaged" * 100
    with pytest.raises(UnreadableImageError, match="more than"):
        open_image(header)


def test_a_decompression_bomb_is_refused() -> None:
    # A tiny PNG file of 12 000 by 12 000 pixels.
    output = io.BytesIO()
    Image.new("1", (12_000, 12_000)).save(output, format="PNG")
    assert len(output.getvalue()) < 100_000
    with pytest.raises(UnreadableImageError):
        open_image(output.getvalue())


def test_boxes_measure_and_clip() -> None:
    box = Box(-10, 5, 30, 25)
    assert (box.width, box.height, box.area) == (40, 20, 800)
    assert box.clipped(20, 20) == Box(0, 5, 20, 20)
    assert Box(5, 5, 1, 1).area == 0


def test_a_crop_keeps_a_margin_inside_the_picture() -> None:
    image = Image.new("RGB", (100, 80))
    assert crop(image, Box(20, 20, 70, 60)).size == (58, 47)
    assert crop(image, Box(0, 0, 100, 80)).size == (100, 80)
    assert crop(image, Box(12, 12, 24, 24), margin=0).size == (12, 12)


def test_a_sliver_still_gives_a_pixel() -> None:
    image = Image.new("RGB", (100, 80))
    assert crop(image, Box(99.6, 40, 99.8, 40.1), margin=0).size == (1, 1)
    assert crop(image, Box(100, 80, 100, 80), margin=0).size == (1, 1)
    # A sliver that rounds down on its right still keeps a pixel.
    assert crop(image, Box(10.2, 10, 10.4, 10.6), margin=0).size == (1, 1)


def test_crops_are_encoded_as_jpeg() -> None:
    data = encode_jpeg(Image.new("RGB", (16, 16), (200, 10, 10)))
    with Image.open(io.BytesIO(data)) as image:
        assert (image.format, image.size) == ("JPEG", (16, 16))


def test_large_crops_are_shrunk_before_encoding() -> None:
    original = Image.new("RGB", (2000, 1000), (200, 10, 10))
    with Image.open(io.BytesIO(encode_jpeg(original))) as image:
        assert image.size == (MAX_CROP_SIDE, MAX_CROP_SIDE // 2)
    assert original.size == (2000, 1000)
    # Wider than JPEG allows, still fine once shrunk.
    with Image.open(io.BytesIO(encode_jpeg(Image.new("RGB", (70_000, 2))))) as image:
        assert image.width == MAX_CROP_SIDE
