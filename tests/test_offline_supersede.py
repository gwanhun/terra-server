"""offline_monitor.scan_supersede_once — 교차 계정 superseded 판정·복구·해지 (Stage U).

왜 FakeSB: 행 상태 전이(superseded → 복구 → 해지)를 실제 UPDATE 결과로 확인해야 한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend import offline_monitor as om
from tests.fake_sb import FakeSB

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
OWNER_A = "aaaaaaaa-0000-0000-0000-000000000001"
OWNER_B = "bbbbbbbb-0000-0000-0000-000000000002"
HW = "30EDA0E22E80"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _cam(cid: str, owner: str, *, hw_id: str | None = HW, online: bool, last_seen: datetime | None,
         created: datetime | None = None, **extra) -> dict:
    return {
        "id": f"uuid-{cid}", "camera_id": cid, "owner_id": owner, "hw_id": hw_id,
        "is_online": online, "last_seen_at": _iso(last_seen) if last_seen else None,
        "created_at": _iso(created or (NOW - timedelta(days=30))),
        "unlinked_at": None, "superseded_at": None, "superseded_by": None,
        "supersede_suspect_at": None, "mqtt_revoked_at": None, **extra,
    }


@pytest.fixture(autouse=True)
def _registry_off(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(om.registry, "unregister_device", lambda cid: calls.append(cid) or True)
    return calls


def test_supersede_old_owner_row_after_24h_offline() -> None:
    """새 계정 행 온라인 + 이전 계정 행 25시간 오프라인 → 이전 행 superseded(숨김만, unlinked_at 그대로)."""
    sb = FakeSB(cameras=[
        _cam("p4cam-old", OWNER_A, online=False, last_seen=NOW - timedelta(hours=25)),
        _cam("p4cam-new", OWNER_B, online=True, last_seen=NOW - timedelta(seconds=10)),
    ])
    stats = om.scan_supersede_once(sb, NOW)
    assert stats["superseded"] == 1
    old = sb.one("cameras", camera_id="p4cam-old")
    assert old["superseded_at"] == _iso(NOW)
    assert old["superseded_by"] == "uuid-p4cam-new"
    assert old["unlinked_at"] is None                      # 해제 아님 — 행·설정·영상 유지
    assert sb.one("cameras", camera_id="p4cam-new")["superseded_at"] is None


def test_no_supersede_when_old_row_offline_less_than_24h() -> None:
    """정전·공유기 교체로 몇 시간 꺼진 보드는 건드리지 않는다."""
    sb = FakeSB(cameras=[
        _cam("p4cam-old", OWNER_A, online=False, last_seen=NOW - timedelta(hours=5)),
        _cam("p4cam-new", OWNER_B, online=True, last_seen=NOW),
    ])
    assert om.scan_supersede_once(sb, NOW)["superseded"] == 0
    assert sb.one("cameras", camera_id="p4cam-old")["superseded_at"] is None


def test_no_supersede_when_new_row_not_online() -> None:
    """남의 MAC 으로 페어링만 하고 접속 안 한 행은 승자가 못 된다."""
    sb = FakeSB(cameras=[
        _cam("p4cam-victim", OWNER_A, online=False, last_seen=NOW - timedelta(days=3)),
        _cam("p4cam-attacker", OWNER_B, online=False, last_seen=None),
    ])
    assert om.scan_supersede_once(sb, NOW)["superseded"] == 0


def test_never_seen_row_counts_offline_after_24h_from_creation() -> None:
    """피해자 보드가 온라인이면 남의 MAC 으로 만든 미접속 행이 거꾸로 숨겨진다(대칭 규칙)."""
    sb = FakeSB(cameras=[
        _cam("p4cam-victim", OWNER_A, online=True, last_seen=NOW),
        _cam("p4cam-junk", OWNER_B, online=False, last_seen=None, created=NOW - timedelta(hours=30)),
        _cam("p4cam-fresh", OWNER_B, online=False, last_seen=None, created=NOW - timedelta(hours=1)),
    ])
    assert om.scan_supersede_once(sb, NOW)["superseded"] == 1
    assert sb.one("cameras", camera_id="p4cam-junk")["superseded_at"] is not None
    assert sb.one("cameras", camera_id="p4cam-fresh")["superseded_at"] is None   # 아직 24시간 안 됨


def test_conflict_two_owners_online_is_held() -> None:
    """한 보드가 두 계정으로 동시 온라인 = 위조 의심 → 판정 보류."""
    sb = FakeSB(cameras=[
        _cam("p4cam-a", OWNER_A, online=True, last_seen=NOW),
        _cam("p4cam-b", OWNER_B, online=True, last_seen=NOW),
        _cam("p4cam-c", "cccccccc-0000-0000-0000-000000000003", online=False,
             last_seen=NOW - timedelta(days=2)),
    ])
    stats = om.scan_supersede_once(sb, NOW)
    assert stats["conflict"] == 1 and stats["superseded"] == 0


def test_same_owner_duplicates_and_null_hw_id_untouched() -> None:
    """같은 계정의 중복 행(구 펌웨어 잔재)과 hw_id 없는 행은 대상이 아니다."""
    sb = FakeSB(cameras=[
        _cam("p4cam-a1", OWNER_A, online=True, last_seen=NOW),
        _cam("p4cam-a2", OWNER_A, online=False, last_seen=NOW - timedelta(days=9)),
        _cam("p4cam-legacy", OWNER_B, hw_id=None, online=False, last_seen=NOW - timedelta(days=9)),
    ])
    assert om.scan_supersede_once(sb, NOW) == {"superseded": 0, "recovered": 0, "revoked": 0, "conflict": 0}


def test_recover_when_superseded_board_comes_back_and_flag_winner() -> None:
    """숨긴 행의 보드가 옛 자격증명으로 재접속(is_online=true) → superseded 해제 + 밀어낸 행에 의심 표식.
    같은 스캔에서 다시 superseded 되지 않는다(온라인이므로)."""
    sb = FakeSB(cameras=[
        _cam("p4cam-victim", OWNER_A, online=True, last_seen=NOW,
             superseded_at=_iso(NOW - timedelta(days=2)), superseded_by="uuid-p4cam-attacker"),
        _cam("p4cam-attacker", OWNER_B, online=True, last_seen=NOW),
    ])
    stats = om.scan_supersede_once(sb, NOW)
    assert stats["recovered"] == 1
    victim = sb.one("cameras", camera_id="p4cam-victim")
    assert victim["superseded_at"] is None and victim["superseded_by"] is None
    assert sb.one("cameras", camera_id="p4cam-attacker")["supersede_suspect_at"] == _iso(NOW)
    # 둘 다 온라인 → conflict 로 보류, victim 이 다시 숨겨지지 않음
    assert stats["conflict"] == 1 and stats["superseded"] == 0


def test_revoke_mqtt_after_30_days(_registry_off: list[str]) -> None:
    """superseded 30일 경과·재접속 없음 → 계정 해지 1회만(mqtt_revoked_at 기록)."""
    sb = FakeSB(cameras=[
        _cam("p4cam-gone", OWNER_A, online=False, last_seen=NOW - timedelta(days=40),
             superseded_at=_iso(NOW - timedelta(days=31)), superseded_by="uuid-p4cam-new"),
        _cam("p4cam-recent", OWNER_A, online=False, last_seen=NOW - timedelta(days=10),
             superseded_at=_iso(NOW - timedelta(days=9)), superseded_by="uuid-p4cam-new", hw_id="AA"),
        _cam("p4cam-new", OWNER_B, online=True, last_seen=NOW),
    ])
    stats = om.scan_supersede_once(sb, NOW)
    assert stats["revoked"] == 1
    assert _registry_off == ["p4cam-gone"]
    assert sb.one("cameras", camera_id="p4cam-gone")["mqtt_revoked_at"] == _iso(NOW)
    assert sb.one("cameras", camera_id="p4cam-recent")["mqtt_revoked_at"] is None

    # 두 번째 스캔: 이미 해지된 행은 다시 부르지 않는다
    stats2 = om.scan_supersede_once(sb, NOW + timedelta(minutes=10))
    assert stats2["revoked"] == 0 and _registry_off == ["p4cam-gone"]


def test_revoke_helper_failure_retries_next_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(om.registry, "unregister_device", lambda cid: False)
    sb = FakeSB(cameras=[
        _cam("p4cam-gone", OWNER_A, online=False, last_seen=NOW - timedelta(days=40),
             superseded_at=_iso(NOW - timedelta(days=31))),
    ])
    assert om.scan_supersede_once(sb, NOW)["revoked"] == 0
    assert sb.one("cameras", camera_id="p4cam-gone")["mqtt_revoked_at"] is None
