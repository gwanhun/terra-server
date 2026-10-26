"""backend/mp4_faststart.py — 카메라 파일 레이아웃 3종을 합성해 검증(ffmpeg 불필요)."""

from __future__ import annotations

import struct

import pytest

from backend.mp4_faststart import NotMp4, faststart, needs_faststart, top_level


def box(kind: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def fullbox(kind: bytes, *payload: bytes) -> bytes:
    return box(kind, b"\x00\x00\x00\x00", *payload)


FTYP = box(b"ftyp", b"isom", struct.pack(">I", 0x200), b"isom", b"iso2", b"avc1", b"mp41")
assert len(FTYP) == 32
PAYLOAD = bytes(range(256)) * 40          # 10240B "프레임"


def moov_with(chunk_offsets: list[int], co64: bool = False, extra: bytes = b"") -> bytes:
    """카메라 moov 와 같은 중첩: moov>trak>mdia>minf>stbl>(stsd,stco|co64). 다른 박스는 더미."""
    if co64:
        table = fullbox(b"co64", struct.pack(">I", len(chunk_offsets)),
                        b"".join(struct.pack(">Q", o) for o in chunk_offsets))
    else:
        table = fullbox(b"stco", struct.pack(">I", len(chunk_offsets)),
                        b"".join(struct.pack(">I", o) for o in chunk_offsets))
    stbl = box(b"stbl", fullbox(b"stsd", b"\x00" * 20), table)
    minf = box(b"minf", fullbox(b"vmhd", b"\x00" * 8), stbl)
    mdia = box(b"mdia", fullbox(b"mdhd", b"\x00" * 20), minf)
    trak = box(b"trak", fullbox(b"tkhd", b"\x00" * 80), mdia)
    return box(b"moov", fullbox(b"mvhd", b"\x00" * 96), trak, extra)


def legacy_file(free_before_mdat: int = 0) -> tuple[bytes, int]:
    """[ftyp][free?][mdat][moov] — 0.4.14 (free=0) / 0.4.15 폴백(free>0). (파일, 페이로드 오프셋)"""
    pre = FTYP + (box(b"free", b"\x00" * (free_before_mdat - 8)) if free_before_mdat else b"")
    payload_off = len(pre) + 8
    mdat = box(b"mdat", PAYLOAD)
    moov = moov_with([payload_off, payload_off + 4096])
    return pre + mdat + moov, payload_off


def kinds(data: bytes) -> list[bytes]:
    return [k for k, *_ in top_level(data)]


def stco_entries(data: bytes) -> list[int]:
    i = data.find(b"stco")
    count = struct.unpack_from(">I", data, i + 8)[0]
    return list(struct.unpack_from(f">{count}I", data, i + 12))


def test_legacy_moves_moov_before_mdat_and_shifts_offsets() -> None:
    src, payload_off = legacy_file()
    out = faststart(src)
    assert out is not None
    assert kinds(src) == [b"ftyp", b"mdat", b"moov"]
    assert kinds(out) == [b"ftyp", b"moov", b"mdat"]
    assert len(out) == len(src)
    moov_len = len(moov_with([0, 0]))
    assert stco_entries(out) == [payload_off + moov_len, payload_off + 4096 + moov_len]
    # 옮긴 오프셋이 실제로 같은 바이트를 가리킨다
    for old, new in zip(stco_entries(src), stco_entries(out)):
        assert src[old:old + 64] == out[new:new + 64]
    # mdat 바이트 동일
    assert out.find(b"mdat") >= 0 and PAYLOAD in out


def test_fallback_layout_keeps_free_and_moves_moov() -> None:
    src, payload_off = legacy_file(free_before_mdat=6944)
    out = faststart(src)
    assert out is not None
    assert kinds(out) == [b"ftyp", b"free", b"moov", b"mdat"]
    assert stco_entries(out)[0] == payload_off + len(moov_with([0, 0]))


def test_already_faststart_is_noop_and_idempotent() -> None:
    src, _ = legacy_file()
    out = faststart(src)
    assert out is not None
    assert faststart(out) is None
    # 0.4.15 카메라 산출물 모양: [ftyp][moov][free][mdat]
    cam = FTYP + moov_with([100]) + box(b"free", b"\x00" * 16) + box(b"mdat", PAYLOAD)
    assert faststart(cam) is None


def test_co64_offsets_are_shifted() -> None:
    mdat = box(b"mdat", PAYLOAD)
    moov = moov_with([40], co64=True)
    out = faststart(FTYP + mdat + moov)
    assert out is not None
    i = out.find(b"co64")
    assert struct.unpack_from(">Q", out, i + 12)[0] == 40 + len(moov)


def test_needs_faststart_from_head() -> None:
    src, _ = legacy_file()
    assert needs_faststart(src[:40]) is True           # ftyp + mdat 헤더만으로 판정
    out = faststart(src)
    assert out is not None
    assert needs_faststart(out[:40]) is False          # ftyp 다음이 moov
    assert needs_faststart(src[:32]) is None           # ftyp 만 — 모름
    assert needs_faststart(b"") is None


def test_not_mp4_raises() -> None:
    with pytest.raises(NotMp4):
        faststart(FTYP + box(b"mdat", b"xx"))          # moov 없음
    with pytest.raises(NotMp4):
        faststart(b"\x00\x00\x10\x00ftyp")             # 크기가 데이터를 넘음
    with pytest.raises(NotMp4):
        faststart(FTYP + box(b"mdat", b"x") + box(b"moov", box(b"cmov", b"zzzz")))
