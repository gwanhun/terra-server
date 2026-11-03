"""lcd_render 단위 테스트 — 비트맵 크기/포맷."""

from __future__ import annotations

import base64

from backend.lcd_render import (
    LCD_BAND_HEIGHT,
    LCD_WIDTH,
    build_lcd_payload,
    render_text_bitmap,
)


_EXPECTED_BYTES = LCD_WIDTH * LCD_BAND_HEIGHT // 8  # 128×24/8 = 384


def test_bitmap_size_fixed() -> None:
    """밴드 크기 1비트 비트맵 고정 (128×24 = 384 byte)."""
    assert len(render_text_bitmap("FEED 6PM")) == _EXPECTED_BYTES


def test_empty_text_still_full_band() -> None:
    """빈 텍스트도 밴드 크기 비트맵(전부 0)."""
    b = render_text_bitmap("")
    assert len(b) == _EXPECTED_BYTES
    assert b == bytes(_EXPECTED_BYTES)  # 전부 꺼짐


def test_nonempty_has_lit_pixels() -> None:
    """텍스트 있으면 켜진 픽셀(비트 1) 존재."""
    b = render_text_bitmap("A")
    assert any(byte != 0 for byte in b)


def test_payload_shape() -> None:
    p = build_lcd_payload("밥 6시")
    assert p["w"] == LCD_WIDTH and p["h"] == LCD_BAND_HEIGHT
    assert p["enc"] == "raw"
    assert len(base64.b64decode(p["data"])) == _EXPECTED_BYTES


# ---- Stage N: 화면보호기 컬러 이미지 (RGB565) ----

from io import BytesIO

import pytest
from PIL import Image

from backend.lcd_render import (
    LCD_IMAGE_BYTES,
    LCD_SCREEN_H,
    LCD_SCREEN_W,
    MAX_IMAGE_BYTES,
    InvalidImage,
    render_image_rgb565,
    rgb_to_rgb565,
)


def _png(size=(640, 480), mode="RGB", color=(255, 255, 255)) -> bytes:
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_rgb565_size_fixed() -> None:
    """어떤 원본 크기든 160×128×2 = 40,960 byte + 160×128 PNG."""
    for size in ((640, 480), (50, 300), (160, 128)):
        rgb, png = render_image_rgb565(_png(size))
        assert len(rgb) == LCD_IMAGE_BYTES == LCD_SCREEN_W * LCD_SCREEN_H * 2
        assert Image.open(BytesIO(png)).size == (LCD_SCREEN_W, LCD_SCREEN_H)


def test_rgb565_big_endian_pixels() -> None:
    """빨강 = 0xF800 → 바이트 F8 00 (ST7735 가 받는 순서). 흰색 FF FF, 검정 00 00."""
    rgb, _ = render_image_rgb565(_png(color=(255, 0, 0)))
    assert rgb[:2] == b"\xf8\x00" and rgb[-2:] == b"\xf8\x00"
    rgb, _ = render_image_rgb565(_png(color=(255, 255, 255)))
    assert rgb == b"\xff\xff" * (LCD_IMAGE_BYTES // 2)
    rgb, _ = render_image_rgb565(_png(color=(0, 0, 0)))
    assert rgb == bytes(LCD_IMAGE_BYTES)
    assert rgb_to_rgb565(0, 255, 0) == 0x07E0 and rgb_to_rgb565(0, 0, 255) == 0x001F


def test_preview_is_565_quantized() -> None:
    """미리보기는 기기와 같은 색 깊이 — (1,2,3) 같은 미세값은 0 으로 떨어진다."""
    _, png = render_image_rgb565(_png(color=(1, 2, 3)))
    assert Image.open(BytesIO(png)).convert("RGB").getpixel((0, 0)) == (0, 0, 0)
    _, png = render_image_rgb565(_png(color=(255, 0, 0)))
    assert Image.open(BytesIO(png)).convert("RGB").getpixel((5, 5)) == (255, 0, 0)


def test_transparent_png_composited_on_black() -> None:
    rgb, _ = render_image_rgb565(_png(mode="RGBA", color=(255, 255, 255, 0)))
    assert rgb == bytes(LCD_IMAGE_BYTES)


def test_image_too_large_rejected() -> None:
    with pytest.raises(InvalidImage):
        render_image_rgb565(b"\x00" * (MAX_IMAGE_BYTES + 1))


def test_not_an_image_rejected() -> None:
    with pytest.raises(InvalidImage):
        render_image_rgb565(b"hello, not an image")
