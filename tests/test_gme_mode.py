"""GmeMode 해석·금지 모드 테스트."""

from __future__ import annotations

import pytest

from backend.gme.mode import (
    DEFAULT_MODE,
    GmeMode,
    GmeModeForbidden,
    assert_mode_allowed,
    resolve_mode,
    writes_to_r2,
)


def test_default_is_off() -> None:
    assert DEFAULT_MODE is GmeMode.OFF
    assert resolve_mode({}) is GmeMode.OFF          # 미설정 → off
    assert resolve_mode({"GME_MODE": ""}) is GmeMode.OFF


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("off", GmeMode.OFF),
        ("dry_run", GmeMode.DRY_RUN),
        ("test_copy", GmeMode.TEST_COPY),
        ("production_copy", GmeMode.PRODUCTION_COPY),
        ("DRY_RUN", GmeMode.DRY_RUN),               # 대소문자 무시
        ("garbage", GmeMode.OFF),                    # 미지값 → off (fail-safe)
    ],
)
def test_resolve_mode(raw: str, expected: GmeMode) -> None:
    assert resolve_mode({"GME_MODE": raw}) is expected


def test_production_copy_is_forbidden() -> None:
    with pytest.raises(GmeModeForbidden):
        assert_mode_allowed(GmeMode.PRODUCTION_COPY)


@pytest.mark.parametrize("mode", [GmeMode.OFF, GmeMode.DRY_RUN, GmeMode.TEST_COPY])
def test_allowed_modes_pass(mode: GmeMode) -> None:
    assert_mode_allowed(mode)  # 예외 없어야


def test_only_test_copy_writes_r2() -> None:
    assert writes_to_r2(GmeMode.TEST_COPY) is True
    for m in (GmeMode.OFF, GmeMode.DRY_RUN, GmeMode.PRODUCTION_COPY):
        assert writes_to_r2(m) is False
