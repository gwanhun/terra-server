"""push_events — 이벤트 생성·적재·전송 워커 단위 테스트.

계약: docs/BACKEND_HANDOFF_REPLY_PUSH_2026-09-15.md
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from backend import push_events

CMD_ID = "5ec3b4d1-0000-4000-8000-222222222222"
DEVICE_UUID = "11111111-2222-3333-4444-555555555555"
OWNER = "4da7f48b-0000-4000-8000-111111111111"
SCHED = "6f7df1a2-0000-4000-8000-444444444444"

META = {"owner_id": OWNER, "name": "크레이 사육장", "enclosure_id": "enc-1"}


def _cmd(**over: Any) -> dict[str, Any]:
    row = {
        "id": CMD_ID,
        "device_id": DEVICE_UUID,
        "issued_by": OWNER,
        "action": "fan_on",
        "result": "ok",
        "source": "schedule",
        "source_id": SCHED,
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSH_EVENT_INGEST_URL", "https://example.test/functions/v1/ingest")
    monkeypatch.setenv("PUSH_EVENT_INGEST_SECRET", "s3cr3t")
    monkeypatch.delenv("PUSH_EVENT_SOURCES", raising=False)


# ---------- build_command_event ----------


def test_schedule_ok_builds_started_event() -> None:
    ev = push_events.build_command_event(_cmd(), "terra-a1b2c3d4", META)
    assert ev is not None
    assert ev["type"] == push_events.EVENT_STARTED
    assert ev["event_id"] == f"command:{CMD_ID}:started"
    assert ev["user_id"] == OWNER
    assert ev["schema_version"] == 1

    p = ev["payload"]
    assert p["execution_source"] == "schedule"
    assert p["execution_phase"] == "started"
    assert p["outcome"] == "succeeded"
    assert p["result"] == "ok"
    assert p["action"] == "fan_on"
    assert p["schedule_id"] == SCHED          # commands.source_id 매핑
    assert p["device_id"] == DEVICE_UUID       # UUID
    assert p["device_key"] == "terra-a1b2c3d4"  # MQTT client_id
    assert p["device_name"] == "크레이 사육장"
    assert p["enclosure_id"] == "enc-1"


@pytest.mark.parametrize("result", ["busy", "error", "unknown_action", "rejected_locked"])
def test_non_ok_result_is_failed(result: str) -> None:
    """status 는 항상 'acked' 라 못 쓴다 — result 로만 성패를 가른다."""
    ev = push_events.build_command_event(_cmd(result=result), "terra-a1", META)
    assert ev is not None
    assert ev["type"] == push_events.EVENT_FAILED
    assert ev["event_id"].endswith(":failed")
    assert ev["payload"]["outcome"] == "failed"
    assert ev["payload"]["result"] == result


def test_manual_source_not_published() -> None:
    """앱 요청: 사용자가 버튼 누른 즉시 제어는 제외."""
    assert push_events.build_command_event(_cmd(source="manual"), "terra-a1", META) is None


def test_guard_source_published() -> None:
    ev = push_events.build_command_event(_cmd(source="guard"), "terra-a1", META)
    assert ev is not None
    assert ev["payload"]["execution_source"] == "guard"


def test_sources_overridable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSH_EVENT_SOURCES", "manual")
    assert push_events.build_command_event(_cmd(source="schedule"), "terra-a1", META) is None
    assert push_events.build_command_event(_cmd(source="manual"), "terra-a1", META) is not None


def test_issued_by_null_falls_back_to_owner() -> None:
    """commands.issued_by 는 NULL 허용 → devices.owner_id 폴백."""
    ev = push_events.build_command_event(_cmd(issued_by=None), "terra-a1", META)
    assert ev is not None
    assert ev["user_id"] == OWNER


def test_no_user_id_anywhere_skips_event() -> None:
    ev = push_events.build_command_event(_cmd(issued_by=None), "terra-a1", {"name": "x"})
    assert ev is None


def test_missing_device_meta_tolerated() -> None:
    ev = push_events.build_command_event(_cmd(), "terra-a1", None)
    assert ev is not None
    assert ev["payload"]["device_name"] is None
    assert ev["payload"]["enclosure_id"] is None


def test_event_body_has_no_secret() -> None:
    ev = push_events.build_command_event(_cmd(), "terra-a1", META)
    assert "s3cr3t" not in str(ev)


# ---------- enqueue ----------


def _sb_with_insert(inserts: list[dict]) -> MagicMock:
    sb = MagicMock()
    t = MagicMock()
    t.insert.side_effect = lambda row: inserts.append(row) or t._ins
    t._ins = MagicMock()
    t._ins.execute.return_value.data = [{}]
    sb.table.return_value = t
    return sb


def test_enqueue_inserts_row(monkeypatch: pytest.MonkeyPatch) -> None:
    inserts: list[dict] = []
    monkeypatch.setattr(push_events, "get_supabase_client", lambda: _sb_with_insert(inserts))

    ev = push_events.build_command_event(_cmd(), "terra-a1", META)
    assert push_events.enqueue(ev) is True
    assert inserts[0]["event_id"] == f"command:{CMD_ID}:started"
    assert inserts[0]["event_type"] == push_events.EVENT_STARTED
    assert inserts[0]["body"] == ev


def test_enqueue_duplicate_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """같은 event_id 재적재는 정상 — UNIQUE 위반을 예외로 올리지 않는다."""
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = RuntimeError(
        'duplicate key value violates unique constraint (23505)'
    )
    monkeypatch.setattr(push_events, "get_supabase_client", lambda: sb)
    ev = push_events.build_command_event(_cmd(), "terra-a1", META)
    assert push_events.enqueue(ev) is False


def test_enqueue_noop_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUSH_EVENT_INGEST_SECRET", raising=False)
    called = MagicMock()
    monkeypatch.setattr(push_events, "get_supabase_client", called)
    ev = {"event_id": "x", "type": "y"}
    assert push_events.enqueue(ev) is False
    called.assert_not_called()


# ---------- send_pending ----------


def _sb_with_pending(rows: list[dict], updates: list[dict]) -> MagicMock:
    sb = MagicMock()
    t = MagicMock()
    (
        t.select.return_value.eq.return_value.lte.return_value
        .order.return_value.limit.return_value.execute.return_value.data
    ) = rows
    t.update.side_effect = lambda patch: updates.append(patch) or t._upd
    t._upd = MagicMock()
    t._upd.eq.return_value.execute.return_value.data = [{}]
    sb.table.return_value = t
    return sb


def _row(**over: Any) -> dict[str, Any]:
    r = {"id": "row-1", "event_id": "command:x:started", "body": {"a": 1}, "attempts": 0}
    r.update(over)
    return r


def test_send_pending_marks_sent_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[dict] = []
    monkeypatch.setattr(
        push_events, "get_supabase_client", lambda: _sb_with_pending([_row()], updates)
    )
    monkeypatch.setattr(
        push_events, "_post", lambda c, b: httpx.Response(202, request=httpx.Request("POST", "http://t"))
    )

    assert push_events.send_pending() == 1
    assert updates[0]["status"] == "sent"
    assert updates[0]["attempts"] == 1
    assert "sent_at" in updates[0]


def test_send_pending_4xx_is_permanent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """앱 계약: 4xx 는 인증/payload 문제라 무한 재시도하지 않는다."""
    updates: list[dict] = []
    monkeypatch.setattr(
        push_events, "get_supabase_client", lambda: _sb_with_pending([_row()], updates)
    )
    monkeypatch.setattr(
        push_events, "_post", lambda c, b: httpx.Response(401, request=httpx.Request("POST", "http://t"))
    )

    assert push_events.send_pending() == 0
    assert updates[0]["status"] == "failed"


def test_send_pending_5xx_retries_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[dict] = []
    monkeypatch.setattr(
        push_events, "get_supabase_client", lambda: _sb_with_pending([_row()], updates)
    )
    monkeypatch.setattr(
        push_events, "_post", lambda c, b: httpx.Response(503, request=httpx.Request("POST", "http://t"))
    )

    assert push_events.send_pending() == 0
    assert "status" not in updates[0]          # pending 유지
    assert updates[0]["attempts"] == 1
    assert "next_retry_at" in updates[0]


def test_send_pending_network_error_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[dict] = []
    monkeypatch.setattr(
        push_events, "get_supabase_client", lambda: _sb_with_pending([_row()], updates)
    )

    def _boom(_c: Any, _b: Any) -> Any:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(push_events, "_post", _boom)

    assert push_events.send_pending() == 0
    assert updates[0]["attempts"] == 1
    assert "next_retry_at" in updates[0]


def test_send_pending_abandons_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[dict] = []
    monkeypatch.setattr(
        push_events,
        "get_supabase_client",
        lambda: _sb_with_pending([_row(attempts=push_events.MAX_ATTEMPTS - 1)], updates),
    )
    monkeypatch.setattr(
        push_events, "_post", lambda c, b: httpx.Response(503, request=httpx.Request("POST", "http://t"))
    )

    assert push_events.send_pending() == 0
    assert updates[0]["status"] == "abandoned"


def test_send_pending_noop_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUSH_EVENT_INGEST_URL", raising=False)
    called = MagicMock()
    monkeypatch.setattr(push_events, "get_supabase_client", called)
    assert push_events.send_pending() == 0
    called.assert_not_called()


def test_backoff_is_bounded() -> None:
    assert push_events._backoff_sec(1) == 10
    assert push_events._backoff_sec(20) == push_events.MAX_BACKOFF_SEC


# ---------- 워커 ----------


def test_worker_does_not_start_when_not_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("PUSH_EVENT_INGEST_SECRET", raising=False)
    w = push_events.PushOutboxWorker()
    w.start()
    assert w._thread is None
    w.stop()
