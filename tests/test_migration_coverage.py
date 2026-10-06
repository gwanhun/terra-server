"""브리지가 쓰는 컬럼이 전부 migrations/*.sql 에 있는지 검사.

9/21(hw_id)·9/17~28(image_state) 두 번 다 "코드는 새 컬럼에 쓰는데 마이그레이션 파일이 없음"
→ 운영에 적용이 누락돼 heartbeat UPDATE 가 통째로 실패한 사고였다. 런타임 fallback 은 증상만
막으므로, 파일이 빠진 채 배포되는 것 자체를 여기서 막는다.

실제 DB 적용 여부는 MIGRATIONS_APPLIED.md 가 SOT — 이 테스트는 "파일이 있는가"까지만 본다.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.mqtt import handlers

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

CAMERA_TEXT = "p4cam-aabbccdd"
CAMERA_UUID = "22222222-2222-2222-2222-bbbbbbbbbbbb"
DEVICE_TEXT = "terra-test01"
DEVICE_UUID = "11111111-1111-1111-1111-aaaaaaaaaaaa"

# 펌웨어가 보낼 수 있는 선택 필드를 전부 채운 payload — 새 필드를 추가하면 여기에도 넣는다.
FULL_CAMERA_PAYLOAD = {
    "ts": 1, "uptime_sec": 10, "free_heap": 1000, "reset": "POWERON",
    "rotate_180": False, "capabilities": {"rotate_180": True}, "hw_id": "30EDA0E22E80",
    "clips": {"rec": 1}, "img": {"exp": 120},
}
FULL_DEVICE_PAYLOAD = {
    "ts": 1_748_000_000, "dht22_a": {"ok": True, "t": 30, "h": 50},
    "dht22_b": {"ok": True, "t": 28, "h": 55}, "relay": "OFF", "fan": "ON", "fan2": "OFF",
    "heater": {"state": "OFF", "locked": False}, "led": "ON", "led_brightness": 75,
    "hw_id": "30EDA0E22E81", "capabilities": {"board": "mosfet"},
    "uptime_sec": 300, "free_heap": 190000, "reset": "SW:mqtt_reboot", "wifi_rssi": -40,
    "temp_offset_c": -1.5,
}


def _migration_columns(table: str) -> set[str]:
    """migrations/*.sql 의 CREATE TABLE 본문 + ALTER TABLE ... ADD COLUMN 에서 컬럼명 수집."""
    sql = "\n".join(p.read_text() for p in sorted(MIGRATIONS.glob("*.sql")))
    sql = re.sub(r"--[^\n]*", "", sql)
    cols: set[str] = set()
    create = re.search(
        rf"CREATE TABLE IF NOT EXISTS (?:public\.)?{table}\s*\((.*?)\n\);", sql, re.S | re.I)
    assert create, f"{table} CREATE TABLE 을 migrations 에서 못 찾음"
    for line in create.group(1).splitlines():
        m = re.match(r"\s*(\w+)\s+\w", line)
        if m and m.group(1).upper() not in {"PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK"}:
            cols.add(m.group(1))
    for stmt in re.finditer(rf"ALTER TABLE (?:ONLY )?(?:public\.)?{table}\b(.*?);", sql, re.S | re.I):
        cols |= set(re.findall(r"ADD COLUMN (?:IF NOT EXISTS )?(\w+)", stmt.group(1), re.I))
    return cols


def _capturing_sb(writes: dict[str, list[dict]]) -> MagicMock:
    """table 별 update/insert payload 를 모으는 supabase mock. 조회는 미페어링 없이 1행 반환."""
    def _table(name: str) -> MagicMock:
        t = MagicMock()
        uuid = CAMERA_UUID if name == "cameras" else DEVICE_UUID
        t.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"id": uuid, "rotate_180": False, "capabilities": None, "hw_id": None}
        ]
        t.update.side_effect = lambda p: writes.setdefault(name, []).append(p) or MagicMock()
        t.insert.side_effect = lambda p: writes.setdefault(name, []).append(p) or MagicMock()
        return t

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    captured: dict[str, list[dict]] = {}
    handlers.reset_device_cache()
    monkeypatch.setattr(handlers, "get_supabase_client", lambda: _capturing_sb(captured))
    monkeypatch.setattr(
        handlers, "_resolve_entity",
        lambda text: ("camera", CAMERA_UUID) if text == CAMERA_TEXT else ("device", DEVICE_UUID))
    handlers.set_command_publisher(lambda cid, payload: True)
    from backend import alerts
    monkeypatch.setattr(alerts, "evaluate_telemetry", lambda *a, **k: None)  # 실 Supabase 클라이언트 생성 차단
    yield captured
    handlers.set_command_publisher(None)
    handlers.reset_device_cache()


def test_migration_parser_sees_known_columns() -> None:
    """파서 자체가 망가져 전부 통과하는 일을 막는 sanity check."""
    cams = _migration_columns("cameras")
    assert {"last_seen_at", "is_online", "capabilities", "clip_stats", "image_state", "hw_id"} <= cams
    assert "nonexistent_column" not in cams


@pytest.mark.parametrize(
    ("entity_text", "payload"),
    [(CAMERA_TEXT, FULL_CAMERA_PAYLOAD), (DEVICE_TEXT, FULL_DEVICE_PAYLOAD)],
    ids=["camera", "device"],
)
def test_telemetry_writes_only_migrated_columns(
    writes: dict[str, list[dict]], entity_text: str, payload: dict
) -> None:
    handlers.handle_telemetry(entity_text, dict(payload))

    tables = {t for t in writes if t in {"cameras", "devices", "telemetry"}}
    assert tables, "쓰기가 하나도 잡히지 않음 — mock 이 핸들러 경로와 어긋남"
    for table in tables:
        written = {k for p in writes[table] for k in p}
        missing = written - _migration_columns(table)
        assert not missing, (
            f"{table} 에 쓰는 컬럼 {sorted(missing)} 의 마이그레이션 파일이 없음 — "
            f"migrations/YYYY-MM-DD_*.sql 추가 후 운영 적용 + MIGRATIONS_APPLIED.md 기록"
        )


def test_lcd_ack_writes_only_migrated_columns() -> None:
    """ACK 경로의 devices.lcd_text 확정도 마이그레이션 파일이 있는 컬럼만 쓴다."""
    captured: dict[str, list[dict]] = {}
    handlers._commit_lcd_text(
        _capturing_sb(captured),
        {"action": "lcd_bitmap", "result": "ok", "payload": {"lcd_text": "밥 6시"}},
        DEVICE_UUID,
    )
    written = {k for p in captured["devices"] for k in p}
    assert written and not written - _migration_columns("devices")
