"""backend/clip_faststart.py — R2 get/put 을 가짜로 바꿔 판정·변환·멱등을 검증."""

from __future__ import annotations

from botocore.exceptions import ClientError

from backend.clip_faststart import HEAD_BYTES, ensure_faststart, ensure_faststart_safe
from backend.mp4_faststart import faststart
from tests.test_mp4_faststart import legacy_file


class FakeR2:
    def __init__(self, data: bytes) -> None:
        self.objects = {"k.mp4": data}
        self.gets: list[tuple[int, int] | None] = []
        self.puts: list[tuple[str, bytes, str]] = []

    def get(self, key: str, byte_range: tuple[int, int] | None = None) -> bytes:
        self.gets.append(byte_range)
        d = self.objects[key]
        return d[byte_range[0]:byte_range[1] + 1] if byte_range else d

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.puts.append((key, data, content_type))
        self.objects[key] = data


def test_legacy_is_converted_and_overwritten() -> None:
    src, _ = legacy_file()
    r2 = FakeR2(src)
    assert ensure_faststart("k.mp4", get_fn=r2.get, put_fn=r2.put) == "converted"
    assert r2.gets == [(0, HEAD_BYTES - 1), None]
    assert r2.puts[0][0] == "k.mp4" and r2.puts[0][2] == "video/mp4"
    assert r2.objects["k.mp4"] == faststart(src)
    # 두 번째는 앞 4KB 만 읽고 끝(멱등)
    assert ensure_faststart("k.mp4", get_fn=r2.get, put_fn=r2.put) == "already"
    assert r2.gets[-1] == (0, HEAD_BYTES - 1) and len(r2.puts) == 1


def test_dry_run_does_not_write() -> None:
    src, _ = legacy_file()
    r2 = FakeR2(src)
    assert ensure_faststart("k.mp4", get_fn=r2.get, put_fn=r2.put, dry_run=True) == "would_convert"
    assert r2.puts == []


def test_non_mp4_is_skipped() -> None:
    r2 = FakeR2(b"not an mp4 at all" * 10)
    assert ensure_faststart("k.mp4", get_fn=r2.get, put_fn=r2.put) == "skipped"
    assert r2.puts == []


def test_safe_wrapper_swallows_r2_errors(monkeypatch) -> None:
    def boom(key: str, byte_range=None) -> bytes:
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    monkeypatch.setattr("backend.clip_faststart.get_object_bytes", boom)
    ensure_faststart_safe("missing.mp4")   # 예외가 밖으로 나오면 실패
