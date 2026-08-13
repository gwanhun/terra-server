"""
LCD 커스텀 텍스트 렌더링 — Stage I.

사용자 텍스트(한글 포함)를 **서버(Pillow)가 1비트 비트맵으로 렌더**해서 펌웨어에 전송한다.
펌웨어는 폰트/유니코드 로직 없이 비트맵을 그대로 화면에 blit 만 하면 됨.

## 왜 서버 렌더?
ST7735 펌웨어 폰트는 5×7 ASCII 뿐 → 한글 불가. 폰트 임베드(2350자 + 조합 렌더)는 무겁다.
서버가 한글 TTF 로 렌더하면 모든 유니코드/이모지 + 예쁜 폰트, 펌웨어 부담 0.

## 출력 포맷
- 밴드 크기 고정 128×32 (1비트). row-major, MSB-first, 128px = 16 byte/row → 512 byte.
- PIL mode "1" 의 tobytes() 가 정확히 이 레이아웃 (bit=1 → 켜짐).

## 폰트
`LCD_FONT_PATH` 환경변수(TTF/TTC 경로)가 1순위. 없으면 흔한 한글 폰트 경로들을 탐색,
그래도 없으면 Pillow 기본 폰트(영문만). 프로덕션(Ubuntu)은 `apt install fonts-nanum` 후 경로 지정 권장.
"""

from __future__ import annotations

import base64
import logging
import os

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

LCD_WIDTH = 128
# 24px: 기존 상단 타이틀바(0~22) 자리를 커스텀 밴드로 쓰고, 센서(y=30~)와 안 겹침.
# → 펌웨어 센서 레이아웃 수정 불필요. 128×24 1비트 = 384 byte.
LCD_BAND_HEIGHT = 24
MAX_TEXT_LEN = 64  # 입력 길이 상한 (렌더는 폭 넘으면 자동 축소/트렁케이트)

# 한글 TTF 후보 (환경변수 다음 순위)
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",          # Ubuntu: fonts-nanum
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Noto CJK
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",               # macOS
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
]


def _load_font(size: int) -> ImageFont.ImageFont:
    """TTF 로드 (한글). 없으면 Pillow 기본(영문만)."""
    paths = []
    env = os.getenv("LCD_FONT_PATH")
    if env:
        paths.append(env)
    paths.extend(_FONT_CANDIDATES)
    for p in paths:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    logger.warning("한글 TTF 미발견 → Pillow 기본 폰트(영문만). LCD_FONT_PATH 설정 권장.")
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, width: int, height: int) -> ImageFont.ImageFont:
    """밴드에 들어가는 최대 폰트 크기 찾기 (폭·높이 둘 다 여유 안에서)."""
    for size in range(int(height * 0.9), 7, -1):
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= width - 2 and th <= height - 2:
            return font
    return _load_font(8)


def render_text_bitmap(
    text: str,
    width: int = LCD_WIDTH,
    height: int = LCD_BAND_HEIGHT,
) -> bytes:
    """텍스트 → 1비트 비트맵 (width*height/8 byte). 중앙 정렬."""
    img = Image.new("1", (width, height), 0)  # 0 = 꺼짐(검정 배경)
    draw = ImageDraw.Draw(img)

    text = (text or "").strip()[:MAX_TEXT_LEN]
    if text:
        font = _fit_font(draw, text, width, height)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = max(0, (width - tw) // 2 - bbox[0])
        y = max(0, (height - th) // 2 - bbox[1])
        draw.text((x, y), text, fill=1, font=font)  # 1 = 켜짐

    return img.tobytes()  # mode "1": MSB-first, row padded to byte (128px → 정확 16B/row)


def build_lcd_payload(text: str) -> dict:
    """command payload {w,h,enc,data(base64)} 생성. 현재 enc='raw'."""
    data = render_text_bitmap(text)
    return {
        "w": LCD_WIDTH,
        "h": LCD_BAND_HEIGHT,
        "enc": "raw",
        "data": base64.b64encode(data).decode("ascii"),
    }


__all__ = [
    "LCD_BAND_HEIGHT",
    "LCD_WIDTH",
    "MAX_TEXT_LEN",
    "build_lcd_payload",
    "render_text_bitmap",
]
