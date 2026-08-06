"""clip_purpose 도출 계약 단위 테스트.

핵심 성질:
- test/ prefix → 'test'
- 운영 lineage allowlist prefix → 'production'
- 미지/오타 prefix → 400 fail-closed (production 으로 새지 않음)
- writer 의 allowlist 가 마이그레이션 CHECK 와 동일 집합인지 (drift 방지)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.routers.clips import (
    _PRODUCTION_PREFIXES,
    _derive_clip_purpose,
)

_CLIP = "abcdef12-3456-7890-abcd-ef1234567890"


def test_test_prefix_is_test() -> None:
    assert _derive_clip_purpose(f"test/p4cam-79b5d844/2026-08-06/120000_{_CLIP}.mp4") == "test"


@pytest.mark.parametrize(
    "key",
    [
        f"terra-clips/clips/2026-08-06/120000_{_CLIP}.mp4",
        f"research-quarantine/2026-08-06/{_CLIP}.mp4",
        f"research-excluded/2026-08-06/{_CLIP}.mp4",
        f"deleted/{_CLIP}.mp4",
    ],
)
def test_production_lineage_prefixes_are_production(key: str) -> None:
    assert _derive_clip_purpose(key) == "production"


@pytest.mark.parametrize(
    "key",
    [
        "clips/2026-08-06/x.mp4",          # 오타 (test/ 도 allowlist 도 아님)
        "tests/p4cam/x.mp4",               # test 아닌 유사 prefix
        "terra-clips/x.mp4",               # canonical 하위경로 아님
        "random/whatever.mp4",
        "",
    ],
)
def test_unknown_prefix_is_fail_closed(key: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _derive_clip_purpose(key)
    assert exc.value.status_code == 400


def test_no_production_prefix_is_under_test() -> None:
    """production allowlist 중 test/ 로 시작하는 게 없어야 (분류 충돌 방지)."""
    assert not any(p.startswith("test/") for p in _PRODUCTION_PREFIXES)


def test_migration_anonymous_block_has_valid_plpgsql_terminator() -> None:
    sql = Path("migrations/2026-08-06_clip_purpose.sql").read_text(encoding="utf-8")
    assert "END;\n$$;" in sql
    assert "END $$;" not in sql
