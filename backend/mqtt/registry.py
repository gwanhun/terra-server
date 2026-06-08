"""
Mosquitto 사용자/ACL 자동 등록 (Stage B 후반).

페어링 후 디바이스/카메라가 즉시 MQTT 연결 가능하도록 Mosquitto password 파일과
ACL 파일을 자동 갱신. 외부 헬퍼 스크립트 (sudoers 등록) 를 통해 실행.

## 흐름
1. `POST /devices/pair` 또는 `POST /cameras/pair` 성공
2. `register_device(device_id, plaintext_token)` 호출
3. 헬퍼 스크립트 실행 (`mosquitto_passwd -b ...`)
4. `regenerate_acl()` — DB 의 모든 active 디바이스/카메라로 ACL 파일 재생성
5. Mosquitto reload (SIGHUP)

## 보안 모델
- terra-api 는 ubuntu user 로 실행
- 헬퍼 스크립트만 sudoers NOPASSWD 등록 (mosquitto_passwd / acl 파일 / systemctl 만)
- 평문 토큰은 페어링 응답 1회 발급 시점에만 메모리에 존재 → 헬퍼로 전달 → 즉시 폐기
- 만약 헬퍼 호출 실패해도 페어링 자체는 성공 처리 (운영자가 수동 동기화 가능)

## 환경변수
- `MOSQUITTO_REGISTRY_ENABLED=true` — 활성화. 기본 false (로컬 개발 / pytest 비활성).
- `MOSQUITTO_HELPER_PATH=/usr/local/bin/terra-mosquitto-register.sh` — 헬퍼 경로
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from io import StringIO
from typing import Iterable

from backend.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


DEFAULT_HELPER_PATH = "/usr/local/bin/terra-mosquitto-register.sh"

# bridge 계정 — ACL 의 첫 entry (전 토픽 readwrite)
_BRIDGE_USER_ENV = "MQTT_BRIDGE_USERNAME"


def _enabled() -> bool:
    return os.getenv("MOSQUITTO_REGISTRY_ENABLED", "false").strip().lower() == "true"


def _helper() -> str:
    return os.getenv("MOSQUITTO_HELPER_PATH", DEFAULT_HELPER_PATH).strip()


def _run_helper(args: list[str], stdin: str | None = None, timeout: float = 5.0) -> bool:
    """헬퍼 스크립트 호출. 실패 시 경고 로그 + False 반환 (raise X)."""
    helper = _helper()
    if not shutil.which("sudo"):
        logger.error("mosquitto registry: sudo 명령을 찾을 수 없음")
        return False

    cmd = ["sudo", "-n", helper, *args]
    try:
        result = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("mosquitto registry: timeout (%ss) — %s", timeout, args)
        return False
    except FileNotFoundError as exc:
        logger.error("mosquitto registry: 헬퍼 누락 — %s (%s)", helper, exc)
        return False

    if result.returncode != 0:
        logger.error(
            "mosquitto registry: 실패 (rc=%d) %s\nstderr=%s",
            result.returncode, args, result.stderr.strip(),
        )
        return False
    return True


# ---------- 공개 API ----------


def register_device(device_id: str, plaintext_token: str) -> bool:
    """페어링 직후 호출. password 파일에 등록 + ACL 재생성 + reload.

    실패해도 raise X — 운영 안정성 우선. 로그만 남기고 False 반환.
    """
    if not _enabled():
        logger.debug("mosquitto registry 비활성 — register %s 무시", device_id)
        return True  # 비활성 시 호출자에게 성공으로 보이게

    ok = _run_helper(["register", device_id, plaintext_token])
    if not ok:
        return False
    return regenerate_acl()


def unregister_device(device_id: str) -> bool:
    """디바이스/카메라 삭제 시 호출."""
    if not _enabled():
        return True

    _run_helper(["unregister", device_id])
    return regenerate_acl()


def regenerate_acl() -> bool:
    """DB 의 모든 device + camera 로 ACL 파일 통째 재생성 + reload."""
    if not _enabled():
        return True

    content = _build_acl_content()
    ok_acl = _run_helper(["regen-acl"], stdin=content)
    if not ok_acl:
        return False
    return _run_helper(["reload"])


def _build_acl_content() -> str:
    """ACL 파일 전체 내용 생성. devices + cameras + bridge.

    포맷 ([docs/MQTT.md](../../docs/MQTT.md) §Mosquitto ACL 예시):
        user terra-bridge
        topic readwrite esp32/#

        user terra-a1b2c3d4
        topic write esp32/terra-a1b2c3d4/telemetry
        topic write esp32/terra-a1b2c3d4/ack
        topic write esp32/terra-a1b2c3d4/alert
        topic read  esp32/terra-a1b2c3d4/command
    """
    buf = StringIO()
    bridge_user = os.getenv(_BRIDGE_USER_ENV, "").strip()
    if bridge_user:
        buf.write(f"# bridge — 전 토픽\nuser {bridge_user}\ntopic readwrite esp32/#\n\n")

    sb = get_supabase_client()

    # 디바이스 (write telemetry/ack/alert, read command)
    dev_res = sb.table("devices").select("device_id").execute()
    for row in dev_res.data or []:
        did = row["device_id"]
        buf.write(_acl_block(did, ["telemetry", "ack", "alert"], ["command"]))

    # 카메라 (write motion_event/ack/alert, read command)
    cam_res = sb.table("cameras").select("camera_id").execute()
    for row in cam_res.data or []:
        cid = row["camera_id"]
        buf.write(_acl_block(cid, ["motion_event", "ack", "alert"], ["command"]))

    return buf.getvalue()


def _acl_block(client_id: str, writes: Iterable[str], reads: Iterable[str]) -> str:
    """한 디바이스/카메라의 ACL 블록 생성."""
    lines = [f"user {client_id}"]
    for w in writes:
        lines.append(f"topic write esp32/{client_id}/{w}")
    for r in reads:
        lines.append(f"topic read  esp32/{client_id}/{r}")
    return "\n".join(lines) + "\n\n"


__all__ = [
    "regenerate_acl",
    "register_device",
    "unregister_device",
]
