"""LCD 하단 vivanaut 로고 비트맵 생성 → 펌웨어 logo.h 갱신.

ST7735 160x128(가로) 화면에서 습도 줄(y=54~72) 아래 남는 영역(y=74~128)에
빨간 단색 로고를 1bit 비트맵으로 넣는다. 색은 draw 시 ST7735_RED 지정.

실행: .venv/bin/python scripts/gen_logo.py [원본 로고 PNG]
  - 원본 PNG(빨간 로고, 검정/투명 배경)를 주면 그걸 임계값 처리해 변환.
  - 없으면 Arial Rounded Bold 로 "vivanaut" 워드마크 + 원형 마크(근사)를 렌더.
→ supermini / nano / nano-relay 세 프로젝트의 main/include/logo.h 를 덮어쓴다.
"""
import sys
from PIL import Image, ImageDraw, ImageFont, ImageOps

LOGO_W, LOGO_H = 156, 38          # 160 폭에 좌우 2px 여백, 원본 비율 약 4.1:1
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf"
TARGETS = [
    "/Users/gwanhun/project/esp32/terra-iot-supermini/main/include/logo.h",
    "/Users/gwanhun/project/esp32/terra-iot-nano/main/include/logo.h",
    "/Users/gwanhun/project/esp32/terra-iot-nano-relay/main/include/logo.h",
]
PREVIEW = "/Users/gwanhun/project/terra-server/scripts/logo_preview.png"


def from_source(path: str) -> Image.Image:
    """원본 PNG → 1bit. 알파 있으면 알파, 없으면 밝기(빨강 채널)로 잉크 판정."""
    img = Image.open(path).convert("RGBA")
    a = img.getchannel("A")
    if a.getextrema() == (255, 255):            # 알파 없음 → 검정 배경 대비, 채널 최대값(빨강 로고)
        r, g, b, _ = img.split()
        from PIL import ImageChops
        ink = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > 90 else 0)
    else:
        ink = a.point(lambda v: 255 if v > 128 else 0)
    ink = ink.crop(ink.getbbox())
    ink = ImageOps.contain(ink, (LOGO_W, LOGO_H), Image.LANCZOS)
    return ink.point(lambda v: 1 if v > 128 else 0, "1")


def placeholder() -> Image.Image:
    """원본 없을 때: 원형 마크 + 워드마크 근사 렌더 (고해상도 → 축소)."""
    S = 8
    W, H = LOGO_W * S, LOGO_H * S
    im = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(im)
    # 원형 마크: 원 + 도마뱀 실루엣 근사(몸통/머리/꼬리/다리) 를 검정으로 파냄
    r = H // 2
    d.ellipse((0, 0, 2 * r, 2 * r), fill=255)
    cx, cy = r, r
    d.ellipse((cx - r*0.34, cy - r*0.22, cx + r*0.22, cy + r*0.40), fill=0)   # 몸통
    d.ellipse((cx - r*0.62, cy - r*0.62, cx - r*0.18, cy - r*0.18), fill=0)  # 머리
    d.line((cx + r*0.05, cy + r*0.30, cx + r*0.55, cy + r*0.70, cx + r*0.85, cy + r*0.45),
           fill=0, width=int(r*0.16))                                        # 꼬리
    for dx, dy in ((-0.55, 0.05), (0.15, -0.45), (-0.45, 0.60), (0.45, 0.05)):
        d.line((cx + r*dx*0.5, cy + r*dy*0.5, cx + r*dx, cy + r*dy), fill=0, width=int(r*0.12))
    # 워드마크
    text = "vivanaut"
    avail_w = W - (2 * r) - int(0.35 * H)
    size = H
    while size > 8:
        f = ImageFont.truetype(FONT_PATH, size)
        bb = d.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= avail_w and bb[3] - bb[1] <= H * 0.78:
            break
        size -= 4
    bb = d.textbbox((0, 0), text, font=f)
    tx = 2 * r + int(0.30 * H) - bb[0]
    ty = (H - (bb[3] - bb[1])) // 2 - bb[1]
    d.text((tx, ty), text, fill=255, font=f)
    small = im.resize((LOGO_W, LOGO_H), Image.LANCZOS)
    return small.point(lambda v: 1 if v > 110 else 0, "1")


def to_c(img: Image.Image) -> tuple[bytes, str]:
    canvas = Image.new("1", (LOGO_W, LOGO_H), 0)
    w, h = img.size
    canvas.paste(img, ((LOGO_W - w) // 2, (LOGO_H - h) // 2))
    data = canvas.tobytes()               # MSB-first, 156px → 20 byte/row
    rb = (LOGO_W + 7) // 8
    lines = [f"static const uint8_t LOGO_VIVANAUT[{len(data)}] = {{  /* {LOGO_W}x{LOGO_H} 1bit */"]
    for r in range(0, len(data), rb):
        lines.append("    " + ", ".join(f"0x{b:02X}" for b in data[r:r + rb]) + ",")
    lines.append("};")
    return data, "\n".join(lines)


src = sys.argv[1] if len(sys.argv) > 1 else None
img = from_source(src) if src else placeholder()
data, arr = to_c(img)
origin = f"원본 {src} 변환" if src else "원본 없음 — Arial Rounded 워드마크 + 원형 마크 근사 렌더"

rb = (LOGO_W + 7) // 8
for r in range(LOGO_H):
    bits = "".join(f"{b:08b}" for b in data[r * rb:(r + 1) * rb])[:LOGO_W]
    print("".join("#" if c == "1" else "." for c in bits))

canvas = Image.new("1", (LOGO_W, LOGO_H), 0)
canvas.paste(img, ((LOGO_W - img.size[0]) // 2, (LOGO_H - img.size[1]) // 2))
canvas.resize((LOGO_W * 4, LOGO_H * 4), Image.NEAREST).save(PREVIEW)

header = f"""#ifndef VIVANAUT_LOGO_H
#define VIVANAUT_LOGO_H
#include <stdint.h>

/* vivanaut 로고 비트맵 (1bit, MSB-first). LCD 습도 줄 아래(y=74~128)에 빨강으로 표시.
 * {origin}.
 * 재생성: terra-server/scripts/gen_logo.py [원본 PNG] */
#define LOGO_W {LOGO_W}
#define LOGO_H {LOGO_H}

{arr}

#endif // VIVANAUT_LOGO_H
"""
for t in TARGETS:
    open(t, "w").write(header)
    print("wrote", t)
