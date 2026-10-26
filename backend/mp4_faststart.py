"""
MP4 faststart — `moov`(목차)를 `mdat`(프레임) 앞으로 옮긴다. qt-faststart 와 같은 일.

## 왜
카메라(0.4.14 이하, 운영 29대)가 올린 mp4 는 전부 [ftyp][mdat][moov] 라 iOS/Safari 가 파일을
거의 다 받은 뒤에야 재생을 시작한다(10Mbps 11초 vs 1.5초). 0.4.15 부터 카메라가 앞에 쓰지만,
이미 올라간 파일과 OTA 못 받는 구 빌드 카메라의 파일은 서버가 옮겨야 한다.

## 어떻게
재인코딩이 아니다. 최상위 박스를 훑어 moov 를 mdat 바로 앞으로 옮기고, moov 안의 샘플 오프셋
표(stco/co64)에 moov 크기만큼 더한다. 프레임 바이트(mdat)는 그대로 복사되고 파일 크기도 같다.
의존성 없음(ffmpeg 불필요) — 카메라 파일은 단일 H.264 트랙 15~20MB 라 메모리에서 처리한다.

## 입력 레이아웃
- 레거시           [ftyp][mdat][moov]        → [ftyp][moov][mdat]
- 0.4.15 폴백      [ftyp][free][mdat][moov]  → [ftyp][free][moov][mdat]
- 0.4.15 faststart [ftyp][moov][free][mdat]  → 그대로(None 반환)
"""

from __future__ import annotations

import struct

_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl"})


class NotMp4(ValueError):
    """박스 구조가 아니거나(moov/mdat 없음, 크기 깨짐) 다룰 수 없는 파일(압축 moov 등)."""


def iter_boxes(data: bytes | memoryview, start: int = 0, end: int | None = None):
    """[start, end) 구간의 ISO BMFF 박스를 (type, offset, header_len, size) 로 낸다.
    size==1 은 64비트 largesize, size==0 은 '끝까지'. 크기가 구간을 넘으면 NotMp4."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                raise NotMp4(f"largesize 헤더 잘림 @{pos}")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise NotMp4(f"박스 {kind!r} 크기 {size} 가 구간을 넘음 @{pos}")
        yield kind, pos, hdr, size
        pos += size


def top_level(data: bytes | memoryview) -> list[tuple[bytes, int, int, int]]:
    return list(iter_boxes(data))


def needs_faststart(head: bytes) -> bool | None:
    """파일 앞부분만 보고 판정. True=moov 가 mdat 뒤(옮겨야 함), False=이미 앞, None=이 길이로는 모름.
    레거시 파일은 ftyp(32B)+mdat 헤더(8B)만 있어도 True 가 나오므로 Range GET 수 KB 면 충분하다."""
    pos = 0
    end = len(head)
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", head, pos)
        if kind == b"moov":
            return False
        if kind == b"mdat":
            return True
        if size == 1:
            if pos + 16 > end:
                return None
            size = struct.unpack_from(">Q", head, pos + 8)[0]
        elif size == 0:
            return None
        if size < 8:
            return None
        pos += size
    return None


def _patch_offsets(moov: bytearray, start: int, end: int, delta: int) -> int:
    """moov 안의 stco/co64 엔트리를 delta 만큼 옮긴다(컨테이너 재귀). 고친 엔트리 수 반환."""
    patched = 0
    for kind, off, hdr, size in list(iter_boxes(moov, start, end)):
        if kind in _CONTAINERS:
            patched += _patch_offsets(moov, off + hdr, off + size, delta)
        elif kind in (b"stco", b"co64"):
            body = off + hdr            # version(1)+flags(3)
            count = struct.unpack_from(">I", moov, body + 4)[0]
            fmt, width = (">I", 4) if kind == b"stco" else (">Q", 8)
            p = body + 8
            if p + count * width > off + size:
                raise NotMp4(f"{kind!r} 엔트리 {count} 가 박스를 넘음")
            for _ in range(count):
                v = struct.unpack_from(fmt, moov, p)[0] + delta
                if kind == b"stco" and v > 0xFFFFFFFF:
                    raise NotMp4("stco 오프셋이 32비트를 넘음")
                struct.pack_into(fmt, moov, p, v)
                p += width
                patched += 1
        elif kind == b"cmov":
            raise NotMp4("압축 moov(cmov) 는 지원하지 않음")
    return patched


def faststart(data: bytes) -> bytes | None:
    """moov 를 mdat 앞으로 옮긴 새 바이트열. 이미 앞이면 None. 구조가 아니면 NotMp4.
    출력 길이는 입력과 같고 mdat 바이트는 그대로다."""
    boxes = top_level(data)
    kinds = [b[0] for b in boxes]
    if b"moov" not in kinds or b"mdat" not in kinds:
        raise NotMp4(f"moov/mdat 없음: {[k.decode('latin1') for k in kinds]}")
    moov_i = kinds.index(b"moov")
    mdat_i = kinds.index(b"mdat")
    if moov_i < mdat_i:
        return None
    _, moov_off, moov_hdr, moov_size = boxes[moov_i]
    _, mdat_off, _, _ = boxes[mdat_i]

    # 새 파일에서 옛 오프셋 x 의 위치: mdat_off ≤ x < moov_off 구간만 +moov_size.
    # (moov 앞 구간은 그대로, 옛 moov 뒤 구간은 -moov_size+moov_size 로 상쇄)
    moov = bytearray(data[moov_off:moov_off + moov_size])
    _patch_offsets(moov, moov_hdr, moov_size, moov_size)

    out = b"".join((
        data[:mdat_off],
        bytes(moov),
        data[mdat_off:moov_off],
        data[moov_off + moov_size:],
    ))
    assert len(out) == len(data)
    return out
