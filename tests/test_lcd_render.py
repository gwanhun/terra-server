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
