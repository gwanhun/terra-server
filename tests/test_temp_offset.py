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
    with caplog.at_level(logging.WARNING):
        monkeypatch.setenv(temp_offset.ENV_VAR, "")          # .env 에 `TEMP_OFFSET_BASE_C=` 오타 — 경고 대상
        assert temp_offset.base_c() == -1.5
        monkeypatch.setenv(temp_offset.ENV_VAR, "abc")
        assert temp_offset.base_c() == -1.5
        assert temp_offset.base_c() == -1.5
    assert sum("숫자 아님" in r.message for r in caplog.records) == 1   # 경고는 프로세스당 한 번만


def test_base_unset_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert temp_offset.base_c() == -1.5
    assert not [r for r in caplog.records if "숫자 아님" in r.message]


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


# ---------- 보드별 base (Stage T, 2026-11-10) ----------

DEV = "11111111-1111-1111-1111-aaaaaaaaaaaa"


def test_base_per_device_from_cache_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """기기가 보고한 base 가 캐시에 있으면 그 값, 없으면(None 보고·조회 실패) env 폴백."""
    monkeypatch.setattr(temp_offset, "_lookup_base", lambda _uuid: None)
    assert temp_offset.base_c(DEV) == -1.5                      # 조회 None → env 기본
    temp_offset.remember_base(DEV, -3.0)
    assert temp_offset.base_c(DEV) == -3.0
    assert temp_offset.base_c() == -1.5                         # uuid 없는 호출은 여전히 env
    temp_offset.remember_base(DEV, None)                        # 구 펌웨어: 미보고도 캐시(DB 재조회 억제)
    assert temp_offset.base_c(DEV) == -1.5


def test_base_cache_expires_then_looks_up_db(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _lookup(uuid: str) -> float:
        calls.append(uuid)
        return -2.0

    monkeypatch.setattr(temp_offset, "_lookup_base", _lookup)
    assert temp_offset.base_c(DEV) == -2.0 and calls == [DEV]
    assert temp_offset.base_c(DEV) == -2.0 and calls == [DEV]   # TTL 안: 재조회 없음
    temp_offset._base_cache[DEV] = (-2.0, 0.0)                  # 만료
    assert temp_offset.base_c(DEV) == -2.0 and calls == [DEV, DEV]


def test_lookup_base_tolerates_db_failure_and_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 예외·컬럼 없음·NULL·범위 밖은 전부 None → env 폴백. 예외는 밖으로 안 나간다."""
    import backend.supabase_client as sc

    class _Boom:
        def table(self, _n):
            raise RuntimeError("down")

    monkeypatch.setattr(sc, "get_supabase_client", lambda: _Boom())
    assert temp_offset._lookup_base(DEV) is None

    from unittest.mock import MagicMock
    for data in ([{"temp_offset_base_c": -2.5}], [{"temp_offset_base_c": None}], [{"temp_offset_base_c": 42}], [], MagicMock()):
        sb = MagicMock()
        sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
        monkeypatch.setattr(sc, "get_supabase_client", lambda sb=sb: sb)
        expect = -2.5 if data and isinstance(data, list) and data[0].get("temp_offset_base_c") == -2.5 else None
        assert temp_offset._lookup_base(DEV) == expect, data


def test_to_user_with_reported_base_and_no_negative_zero() -> None:
    """같은 telemetry 의 base 를 넘기면 캐시·env 와 무관. -0.0 은 0.0 으로(앱 '-0.0' 표시 방지)."""
    assert temp_offset.to_user(-2.0, base=-3.0) == 1.0
    assert temp_offset.to_user(-3.0, base=-3.0) == 0.0
    assert str(temp_offset.to_user(-1.54)) == "0.0"
    temp_offset.remember_base(DEV, -3.0)
    assert temp_offset.to_user(-3.0, DEV) == 0.0
    assert temp_offset.to_device(1.0, DEV) == -2.0
    assert temp_offset.user_range(DEV) == (-7.0, 13.0)


def test_resolve_user_payload_uses_device_base() -> None:
    temp_offset.remember_base(DEV, -3.0)
    assert temp_offset.resolve_device_offset({"user_offset_c": 1.0}, DEV) == -2.0
    assert temp_offset.resolve_device_offset({"user_offset_c": 12.5}, DEV) == 9.5     # env 기준이면 거절될 값
    with pytest.raises(temp_offset.OffsetError) as ei:
        temp_offset.resolve_device_offset({"user_offset_c": -7.5}, DEV)
    assert "-7 ~ 13" in str(ei.value)


def test_resolve_base_payload() -> None:
    assert temp_offset.resolve_base_payload({"base_c": -3}) == -3.0
    assert temp_offset.resolve_base_payload({"base_c": -1.55}) == -1.55
    for bad in ({}, None, {"base_c": "-3"}, {"base_c": True}, {"base_c": None}):
        with pytest.raises(temp_offset.OffsetError) as ei:
            temp_offset.resolve_base_payload(bad)
        assert ei.value.result == "invalid_payload"
    with pytest.raises(temp_offset.OffsetError) as ei:
        temp_offset.resolve_base_payload({"base_c": 10.1})
    assert ei.value.result == "offset_out_of_range"
