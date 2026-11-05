"""backend/temp_offset — 공장 기본(base)·사용자 보정값 변환 (Stage S)."""

from __future__ import annotations

import logging

import pytest

from backend import temp_offset


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(temp_offset.ENV_VAR, raising=False)
    monkeypatch.setattr(temp_offset, "_warned_bad_env", False)
    yield


def test_base_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert temp_offset.base_c() == -1.5
    monkeypatch.setenv(temp_offset.ENV_VAR, "-2.0")
    assert temp_offset.base_c() == -2.0


def test_base_bad_env_falls_back_to_default_not_zero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """빈 값·오타는 0 이 아니라 기본 -1.5 (0 이면 신 앱 기기가 1.5℃ 높게 읽혀 오경보)."""
    monkeypatch.setenv(temp_offset.ENV_VAR, "")
    assert temp_offset.base_c() == -1.5
    monkeypatch.setenv(temp_offset.ENV_VAR, "abc")
    with caplog.at_level(logging.WARNING):
        assert temp_offset.base_c() == -1.5
        assert temp_offset.base_c() == -1.5
    assert sum("숫자 아님" in r.message for r in caplog.records) == 1   # 경고는 한 번만


def test_conversions_round_trip() -> None:
    assert temp_offset.to_device(1.0) == -0.5
    assert temp_offset.to_device(0) == -1.5
    assert temp_offset.to_device(1.1) == -0.4          # 부동소수 -0.3999… 방지
    assert temp_offset.to_user(-0.5) == 1.0
    assert temp_offset.to_user(-1.5) == 0.0
    assert temp_offset.to_user(-3.0) == -1.5
    assert temp_offset.to_user(-0.55) == 0.9            # 사용자값은 0.1 단위(numeric(4,1))


def test_user_range_follows_base(monkeypatch: pytest.MonkeyPatch) -> None:
    assert temp_offset.user_range() == (-8.5, 11.5)
    monkeypatch.setenv(temp_offset.ENV_VAR, "0")
    assert temp_offset.user_range() == (-10.0, 10.0)


def test_resolve_legacy_payload_untouched() -> None:
    """user_offset_c 없으면 None — 구 앱·콘솔의 offset_c 는 호출자가 그대로 통과시킨다."""
    assert temp_offset.resolve_device_offset({"offset_c": -1.5}) is None
    assert temp_offset.resolve_device_offset(None) is None
    assert temp_offset.resolve_device_offset({}) is None
    assert temp_offset.resolve_device_offset("junk") is None


def test_resolve_user_payload() -> None:
    assert temp_offset.resolve_device_offset({"user_offset_c": 1.0}) == -0.5
    assert temp_offset.resolve_device_offset({"user_offset_c": 0}) == -1.5
    assert temp_offset.resolve_device_offset({"user_offset_c": 11.5}) == 10.0
    assert temp_offset.resolve_device_offset({"user_offset_c": -8.5}) == -10.0


@pytest.mark.parametrize("payload", [
    {"user_offset_c": 1.0, "offset_c": -0.5},   # 두 키 동시
    {"user_offset_c": "1.0"},
    {"user_offset_c": True},
    {"user_offset_c": None},
])
def test_resolve_invalid_payload(payload: dict) -> None:
    with pytest.raises(temp_offset.OffsetError) as ei:
        temp_offset.resolve_device_offset(payload)
    assert ei.value.result == "invalid_payload"


@pytest.mark.parametrize("v", [11.6, -8.6, 100])
def test_resolve_out_of_range(v: float) -> None:
    with pytest.raises(temp_offset.OffsetError) as ei:
        temp_offset.resolve_device_offset({"user_offset_c": v})
    assert ei.value.result == "offset_out_of_range"
    assert "-8.5 ~ 11.5" in str(ei.value)
