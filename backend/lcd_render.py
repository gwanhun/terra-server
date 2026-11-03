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
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

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


# ---- Stage N: 화면보호기 컬러 이미지 (전체 화면 160×128, RGB565) ----
#
# 서버가 사진을 160×128 RGB565(픽셀당 2바이트, big-endian) 로 깎아 R2 에 두고, 기기는 HTTP 로 받아
# 파티션 밖 raw 플래시에 저장한다(40,960B — nano NVS 24KB 엔 못 넣고, MQTT 2048 버퍼로도 못 보냄).
# big-endian 인 이유: ST7735 가 SPI 로 받는 바이트 순서 그대로라 펌웨어가 변환 없이 플래시→SPI 로 흘린다.
LCD_SCREEN_W = 160
LCD_SCREEN_H = 128
LCD_IMAGE_BYTES = LCD_SCREEN_W * LCD_SCREEN_H * 2   # 40960
MAX_IMAGE_BYTES = 5 * 1024 * 1024                    # 업로드 원본 상한 (폰 사진 2~4MB)


class InvalidImage(ValueError):
    """이미지 디코드/검증 실패 — 라우터에서 400 으로 변환."""


def _open_fitted(data: bytes) -> Image.Image:
    """업로드 원본 → EXIF 회전 반영 → 투명은 검정 위에 합성 → cover-fit(중앙 크롭) 160×128 RGB."""
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidImage(f"이미지가 너무 큼 ({len(data)} B > {MAX_IMAGE_BYTES} B)")
    try:
        img = Image.open(BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img) or img
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            base = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
            img = Image.alpha_composite(base, rgba).convert("RGB")
        else:
            img = img.convert("RGB")
        return ImageOps.fit(img, (LCD_SCREEN_W, LCD_SCREEN_H), Image.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
        raise InvalidImage(f"이미지를 읽을 수 없음: {e}") from e


def rgb_to_rgb565(r: int, g: int, b: int) -> int:
    """8비트 RGB → RGB565 (펌웨어 ST7735_RGB 매크로와 같은 비트 배치)."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def render_image_rgb565(data: bytes) -> tuple[bytes, bytes]:
    """업로드 원본 → (RGB565 big-endian 40,960B, 미리보기 PNG 160×128).

    미리보기는 RGB565 로 양자화한 뒤 다시 펼친 색이라 기기 화면과 같은 색 깊이다.
    """
    fitted = _open_fitted(data)
    out = bytearray(LCD_IMAGE_BYTES)
    preview = Image.new("RGB", (LCD_SCREEN_W, LCD_SCREEN_H))
    px = fitted.load()
    pv = preview.load()
    i = 0
    for y in range(LCD_SCREEN_H):
        for x in range(LCD_SCREEN_W):
            r, g, b = px[x, y]
            v = rgb_to_rgb565(r, g, b)
            out[i] = v >> 8          # big-endian
            out[i + 1] = v & 0xFF
            i += 2
            # 565 → 888 복원 (하위 비트 확장) — 기기에서 보이는 색
            r5, g6, b5 = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
            pv[x, y] = ((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2))
    buf = BytesIO()
    preview.save(buf, format="PNG")
    return bytes(out), buf.getvalue()


__all__ = [
    "InvalidImage",
    "LCD_BAND_HEIGHT",
    "LCD_IMAGE_BYTES",
    "LCD_SCREEN_H",
    "LCD_SCREEN_W",
    "LCD_WIDTH",
    "MAX_IMAGE_BYTES",
    "MAX_TEXT_LEN",
    "build_lcd_payload",
    "render_image_rgb565",
    "render_text_bitmap",
    "rgb_to_rgb565",
]
