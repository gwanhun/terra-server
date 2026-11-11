#!/usr/bin/env python
"""운영 콘솔(JWT) 없이 카메라/허브 OTA — 서버 호스트에서 service role 로 작업 생성·ota_prepare 발행·apply.

API `POST /cameras/{id}/ota` → `.../apply` 와 같은 일을 routers/cameras.py 의 코드 경로 그대로 수행한다(ota_service.create_job →
ota_prepare_command → MQTT publish → ready 폴링 → ota_apply_command → mark_applying). 브로커 bridge 자격증명이 서버 .env 에만
있으므로 **프로덕션 호스트에서** 돌린다:

  ssh terra 'cd ~/terra-server && .venv/bin/python scripts/ota_push.py --camera p4hub-ff94f0b5 --release <release_id> --force --wait'

  --camera   cameras.camera_id(p4hub-…/p4cam-…) 또는 uuid
  --release  firmware_releases.id (scripts/ota_status.py 로 확인). 생략하면 그 target 의 최신 릴리스
  --force    사전 점검 게이트(just_booted·offline…) 무시 — 크래시 루프 중 기기에 필요
  --wait     ready 가 되면 apply 까지 발행하고 verified/rolled_back/failed/timeout 까지 기다린다(최대 20분)

로그 읽기는 `scripts/ota_status.py --logs <camera_id> [n]` (service role, 읽기 전용, 로컬에서도 됨).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from backend import ota_service  # noqa: E402
from backend.mqtt.camera_commands import ota_apply_command, ota_prepare_command  # noqa: E402
from backend.supabase_client import get_supabase_client  # noqa: E402
from backend.webrtc_signaling import MqttWebRTCSignaling  # noqa: E402

CAMERA_COLUMNS = ("id, owner_id, camera_id, model, unlinked_at, firmware_ver, capabilities, clip_stats, "
                  "last_seen_at, is_online, live_session_id")   # routers/cameras.py _OTA_CAMERA_COLUMNS 와 동일
TERMINAL = ("verified", "rolled_back", "failed", "timeout")


def load_camera(sb, key: str) -> dict:
    col = "camera_id" if not key.count("-") == 4 else "id"
    rows = sb.table("cameras").select(CAMERA_COLUMNS).eq(col, key).limit(1).execute().data or []
    if not rows or rows[0].get("unlinked_at"):
        raise SystemExit(f"camera not found or unlinked: {key}")
    return rows[0]


def latest_release(sb, target: str) -> str:
    rows = sb.table("firmware_releases").select("id, version").eq("target", target).is_("retired_at", "null") \
        .order("created_at", desc=True).limit(1).execute().data or []
    if not rows:
        raise SystemExit(f"no release for target {target}")
    print(f"최신 릴리스: {rows[0]['version']} {rows[0]['id']}")
    return rows[0]["id"]


def publish(cam: dict, cmd: dict) -> None:
    MqttWebRTCSignaling().publish(cam.get("camera_id", ""), cmd)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", required=True)
    p.add_argument("--release")
    p.add_argument("--force", action="store_true")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--poll", type=int, default=10, help="폴링 간격(초)")
    a = p.parse_args()

    sb = get_supabase_client()
    cam = load_camera(sb, a.camera)
    target = ota_service.target_for(ota_service.KIND_CAMERA, cam)
    release_id = a.release or latest_release(sb, target)
    gate = ota_service.gate_reasons(ota_service.KIND_CAMERA, cam) if a.force else []
    try:
        job, release, payload = ota_service.create_job(
            sb, kind=ota_service.KIND_CAMERA, entity=cam, release_id=release_id,
            issued_by=cam.get("owner_id"), force=a.force,
        )
    except ota_service.OtaError as exc:
        print(f"작업 생성 거절 HTTP {exc.status_code}: {exc.detail}")
        return 1
    cmd = ota_prepare_command(job_id=payload["job_id"], version=payload["version"], size_bytes=payload["size"],
                              sha256=payload["sha256"], url=payload["url"])
    ota_service.set_prepare_msg(sb, job["id"], cmd["msg_id"])
    try:
        publish(cam, cmd)
    except Exception as exc:  # noqa: BLE001
        ota_service.mark_failed(sb, job["id"], "publish_failed")
        print(f"ota_prepare 발행 실패: {exc}")
        return 1
    print(f"ota_prepare 발행: camera={cam['camera_id']} job={job['id']} version={release['version']} "
          f"force={a.force} gate_ignored={gate}")
    if not a.wait:
        print("ready 가 되면: --wait 로 다시 돌리거나 운영 콘솔에서 apply")
        return 0

    t0 = time.time()
    last = None
    applied = False
    while time.time() - t0 < 20 * 60:
        j = ota_service.get_job(sb, job["id"]) or {}
        st = (j.get("status"), j.get("pct"), j.get("error"))
        if st != last:
            print(f"  +{int(time.time() - t0):4}s {st[0]} {st[1] or 0}% {st[2] or ''}", flush=True)
            last = st
        if st[0] == "ready" and not applied:
            acmd = ota_apply_command(job_id=job["id"])
            try:
                publish(cam, acmd)
            except Exception as exc:  # noqa: BLE001
                print(f"ota_apply 발행 실패: {exc}")
                return 1
            ota_service.mark_applying(sb, job["id"], acmd["msg_id"])
            applied = True
            print(f"ota_apply 발행 msg_id={acmd['msg_id']}", flush=True)
        elif st[0] in TERMINAL:
            return 0 if st[0] == "verified" else 1
        time.sleep(a.poll)
    print("20분 초과")
    return 1


if __name__ == "__main__":
    sys.exit(main())
