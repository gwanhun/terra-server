"""LCD 한글 라벨(온도/습도) 비트맵 생성 → 펌웨어 kr_labels.h 갱신.

40x18 1bit 셀에 글리프를 **왼쪽 정렬**(세로는 중앙)로 배치한다.
원래는 가로 중앙 배치였는데 라벨 앞 여백이 생겨서 왼쪽 정렬로 변경.

실행: .venv/bin/python scripts/gen_kr_labels.py
→ terra-iot-nano / terra-iot-nano-relay 두 프로젝트의 kr_labels.h 를 덮어쓴다.
"""

from PIL import Image, ImageDraw, ImageFont

CELL_W, CELL_H = 40, 18
FONT_SIZE = 18  # size2 숫자(14px 디지털)와 시각적으로 맞춘 크기 (기존과 동일)
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"

TARGETS = [
    "/Users/gwanhun/project/esp32/terra-iot-nano/main/include/kr_labels.h",
    "/Users/gwanhun/project/esp32/terra-iot-nano-relay/main/include/kr_labels.h",
]


def render_label(text: str) -> bytes:
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    # 넉넉한 캔버스에 그린 뒤 잉크 bbox 로 crop
    tmp = Image.new("1", (CELL_W * 3, CELL_H * 3), 0)
    d = ImageDraw.Draw(tmp)
    d.text((CELL_W, CELL_H), text, fill=1, font=font)
    ink = tmp.getbbox()
    glyph = tmp.crop(ink)
    gw, gh = glyph.size
    if gw > CELL_W or gh > CELL_H:
        glyph = glyph.resize((min(gw, CELL_W), min(gh, CELL_H)))
        gw, gh = glyph.size

    cell = Image.new("1", (CELL_W, CELL_H), 0)
    cell.paste(glyph, (0, (CELL_H - gh) // 2))  # 왼쪽 정렬 + 세로 중앙
    return cell.tobytes()  # MSB-first, 40px → 5 byte/row


def c_array(name: str, comment: str, data: bytes) -> str:
    lines = [f"static const uint8_t {name}[{len(data)}] = {{  /* {comment} */"]
    for r in range(0, len(data), 5):
        row = ", ".join(f"0x{b:02X}" for b in data[r : r + 5])
        lines.append(f"    {row},")
    lines.append("};")
    return "\n".join(lines)


def ascii_preview(data: bytes) -> str:
    out = []
    for r in range(CELL_H):
        row = data[r * 5 : r * 5 + 5]
        bits = "".join(f"{b:08b}" for b in row)
        out.append("".join("#" if c == "1" else "." for c in bits))
    return "\n".join(out)


temp = render_label("온도")
humid = render_label("습도")

print("온도:\n" + ascii_preview(temp))
print("습도:\n" + ascii_preview(humid))

header = f"""#ifndef KR_LABELS_H
#define KR_LABELS_H
#include <stdint.h>

/* 한글 라벨 비트맵 (40x18 1bit, MSB-first). Pillow(AppleSDGothic) 렌더 → 임베드.
 * LCD 폰트가 ASCII 전용이라 온도/습도 한글은 비트맵으로 그린다.
 * 잉크 bbox crop 후 40x18 셀에 왼쪽 정렬(세로 중앙) → 라벨 앞 여백 없음.
 * font18 → size2 숫자(디지털 14px)와 시각적으로 맞춤(한글 얇은 획 보정).
 * 재생성: terra-server/scripts/gen_kr_labels.py */
#define KR_LABEL_W {CELL_W}
#define KR_LABEL_H {CELL_H}

{c_array("KR_LABEL_TEMP", "온도 40x18 1bit", temp)}

{c_array("KR_LABEL_HUMID", "습도 40x18 1bit", humid)}

#endif // KR_LABELS_H
"""

for path in TARGETS:
    with open(path, "w") as f:
        f.write(header)
    print(f"wrote {path}")
