"""R2 orphan mp4 → motion_clips 역등록 (펌웨어 업로드 중단 복구).

배경: "R2 엔 mp4 있음 + DB 엔 row 없음" 상태는 두 경로로 생긴다.
  - 레거시 펌웨어가 썸네일 PUT 실패를 클립 전체 실패로 처리 (p4cam-2d854b2f 8/30~31)
  - R2 PUT 뒤 meta POST 가 서버에 닿지 못함 — 카메라 쪽 네트워크·TLS 끊김. 서버 로그엔 흔적이
    없고 펌웨어 기본값은 실패 클립 즉시 폐기(재시도 2초×3 뿐) (2026-11-04 6건 대조로 확인)
이 스크립트는 R2 를 스캔해 DB 에 없는 mp4 를 motion_clips 에 등록한다.
썸네일 jpg 가 R2 에 있으면 함께 연결, 없으면 NULL(앱은 아이콘 폴백).

로컬 .env 에 운영 R2·Supabase 키가 있으면 로컬에서도 돌아간다. 없으면 프로덕션 서버에서:
    uv run python scripts/reconcile_r2_orphans.py --camera p4cam-2d854b2f \\
        --since 2026-08-30 --until 2026-08-31          # dry-run (기본)
    ... --apply                                        # 실제 INSERT

주기 실행(2026-11-04, 재발 방지 — docs/OPERATIONS.md 1-2):
    uv run python scripts/reconcile_r2_orphans.py --all-linked --days 2 --apply
  → 연결된(unlinked_at IS NULL) 카메라 전부를 최근 N일(KST, 오늘 포함) 범위로 순회.

- 멱등: clip_id(=파일명 UUID)가 이미 DB 에 있으면 스킵.
- duration 은 mp4 의 mvhd box 에서 추출 (Range GET, 파일 전체 다운로드 안 함).
  못 찾으면 기본 60.0s 로 등록하고 표시.
- 같은 클립이 SD 재시도로 여러 clip_id 에 중복 업로드됐을 수 있다 →
  started_at(파일명 HHMMSS) 이 같은 orphan 은 가장 큰 파일 1개만 등록.
- [2026-11-04] 이미 등록된 클립과 같은 시각(KST 초 단위)인 orphan 은 제외 — 10/21 멱등 수정 전
  meta 재시도 500 → 전체 재업로드로 생긴 "중복 재업로드" 고아(149건)를 새 행으로 또 등록하지
  않기 위해. 같은 초에 두 모션이 시작할 수는 없으니 시각 일치 = 같은 클립.
- [2026-11-04] 해제된(unlinked_at) 카메라는 enclosure_id 가 NULL 이라 등록해도 앱 목록
  (GET /enclosures/{id}/clips)에 안 보인다. 기본 거부, --allow-unlinked 로만 진행(연구용 등).
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.r2_client import get_r2_bucket, get_r2_client
from backend.supabase_client import get_supabase_client

KST = ZoneInfo("Asia/Seoul")
DEFAULT_DURATION_SEC = 60.0
CAMERA_COLUMNS = "id,camera_id,owner_id,enclosure_id,unlinked_at"

# backend/routers/clips.py 의 _KEY_RE 와 동일 계약
KEY_RE = re.compile(
    r"^terra-clips/clips/(?P<camera>[^/]+)/(?P<date>\d{4}-\d{2}-\d{2})/"
    r"(?P<time>\d{6})_(?P<clip_id>[0-9a-f-]{36})\.(?P<ext>mp4|jpg)$"
)


def daterange(since: date, until: date):
    d = since
    while d <= until:
        yield d
        d += timedelta(days=1)


def default_range(days: int, today: date | None = None) -> tuple[date, date]:
    """--since/--until 생략 시: 오늘(KST) 포함 최근 `days` 일."""
    until = today or datetime.now(KST).date()
    return until - timedelta(days=max(days, 1) - 1), until


def list_objects(client, bucket: str, prefix: str) -> dict[str, int]:
    """prefix 아래 key → size 매핑."""
    out: dict[str, int] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = obj["Size"]
    return out


def registered_started_at_keys(sb, camera_uuid: str, since: date, until: date) -> set[str]:
    """해당 카메라의 등록 클립 started_at 을 R2 키 형식(YYYY-MM-DD_HHMMSS, KST) 집합으로."""
    lo = datetime.combine(since, datetime.min.time(), tzinfo=KST)
    hi = datetime.combine(until + timedelta(days=1), datetime.min.time(), tzinfo=KST)
    res = (
        sb.table("motion_clips")
        .select("started_at")
        .eq("camera_id", camera_uuid)
        .gte("started_at", lo.astimezone(timezone.utc).isoformat())
        .lt("started_at", hi.astimezone(timezone.utc).isoformat())
        .execute()
    )
    out: set[str] = set()
    for row in res.data or []:
        ts = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")).astimezone(KST)
        out.add(f"{ts:%Y-%m-%d}_{ts:%H%M%S}")
    return out


def mvhd_duration_sec(client, bucket: str, key: str, size: int) -> float | None:
    """mp4 mvhd box 에서 duration 추출. moov 는 보통 파일 끝(또는 앞)에 있어
    양끝 512KB Range GET 두 번으로 대부분 커버된다."""
    span = min(size, 512 * 1024)
    ranges = [f"bytes=-{span}"]
    if size > span:
        ranges.append(f"bytes=0-{span - 1}")
    for rng in ranges:
        try:
            body = client.get_object(Bucket=bucket, Key=key, Range=rng)["Body"].read()
        except Exception:
            return None
        idx = body.find(b"mvhd")
        if idx < 0 or idx + 24 > len(body):
            continue
        version = body[idx + 4]
        try:
            if version == 1:
                timescale, duration = struct.unpack(
                    ">IQ", body[idx + 8 + 16 : idx + 8 + 16 + 12]
                )
            else:
                timescale, duration = struct.unpack(
                    ">II", body[idx + 8 + 8 : idx + 8 + 8 + 8]
                )
        except struct.error:
            return None
        if timescale > 0 and 0 < duration / timescale < 24 * 3600:
            return round(duration / timescale, 3)
    return None


def reconcile_camera(
    sb, client, bucket: str, camera: dict[str, Any], since: date, until: date, *, apply: bool,
) -> tuple[int, int]:
    """카메라 하나를 처리하고 (등록 대상 수, 실제 INSERT 수) 를 돌려준다."""
    camera_text = camera["camera_id"]

    # 1) R2 스캔
    objects: dict[str, int] = {}
    for d in daterange(since, until):
        prefix = f"terra-clips/clips/{camera_text}/{d.isoformat()}/"
        objects.update(list_objects(client, bucket, prefix))
    mp4s = {k: v for k, v in objects.items() if k.endswith(".mp4")}
    jpgs = {k for k in objects if k.endswith(".jpg")}
    print(f"R2 스캔: mp4 {len(mp4s)}건, jpg {len(jpgs)}건 ({since} ~ {until} KST)")
    if not mp4s:
        return 0, 0

    # 2) DB 에 이미 있는 clip_id 제외
    parsed = []
    for key, size in sorted(mp4s.items()):
        m = KEY_RE.match(key)
        if not m:
            print(f"  ! key 형식 불일치 스킵: {key}")
            continue
        parsed.append((m, key, size))
    ids = [m.group("clip_id") for m, _, _ in parsed]
    existing: set[str] = set()
    for i in range(0, len(ids), 100):
        res = sb.table("motion_clips").select("id").in_("id", ids[i : i + 100]).execute()
        existing.update(row["id"] for row in res.data or [])
    orphans = [(m, key, size) for m, key, size in parsed if m.group("clip_id") not in existing]
    print(f"DB 등록됨 {len(existing)}건 → orphan {len(orphans)}건")
    if not orphans:
        return 0, 0

    # 2b) 등록된 클립과 같은 시각(KST 초)인 orphan 은 중복 재업로드 → 제외
    registered_times = registered_started_at_keys(sb, camera["id"], since, until)
    dup_of_registered = [
        (m, key, size) for m, key, size in orphans
        if f"{m.group('date')}_{m.group('time')}" in registered_times
    ]
    if dup_of_registered:
        print(f"등록 클립과 같은 시각 {len(dup_of_registered)}건 제외(중복 재업로드):")
        for _, key, _ in dup_of_registered:
            print(f"  - {key}")
        orphans = [o for o in orphans if o not in dup_of_registered]

    # 3) SD 재시도 중복 제거: 같은 started_at(HHMMSS) 은 가장 큰 파일 1건만
    by_time: dict[str, tuple] = {}
    for m, key, size in orphans:
        t = f"{m.group('date')}_{m.group('time')}"
        if t not in by_time or size > by_time[t][2]:
            by_time[t] = (m, key, size)
    dupes = len(orphans) - len(by_time)
    if dupes:
        print(f"중복(같은 시각) {dupes}건 제외 → 등록 대상 {len(by_time)}건")

    # 4) 등록
    inserted = 0
    for m, key, size in sorted(by_time.values(), key=lambda x: x[1]):
        clip_id = m.group("clip_id")
        started_kst = datetime.strptime(
            f"{m.group('date')} {m.group('time')}", "%Y-%m-%d %H%M%S"
        ).replace(tzinfo=KST)
        thumb_key = key[:-4] + ".jpg"
        thumbnail_key = thumb_key if thumb_key in jpgs else None
        duration = mvhd_duration_sec(client, bucket, key, size)
        dur_note = "" if duration else f" (mvhd 미검출 → {DEFAULT_DURATION_SEC}s 기본값)"

        payload = {
            "id": clip_id,
            "camera_id": camera["id"],
            "enclosure_id": camera.get("enclosure_id"),
            "owner_id": camera["owner_id"],
            "started_at": started_kst.astimezone(timezone.utc).isoformat(),
            "duration_sec": duration or DEFAULT_DURATION_SEC,
            "r2_key": key,
            "clip_purpose": "production",
            "thumbnail_key": thumbnail_key,
            "file_size": size,
            "codec": "h264",
            "container": "mp4",
        }
        tag = "INSERT" if apply else "DRY-RUN"
        print(
            f"  [{tag}] {started_kst:%m-%d %H:%M:%S} dur={payload['duration_sec']}"
            f" size={size} thumb={'있음' if thumbnail_key else 'NULL'}{dur_note}"
        )
        if apply:
            res = sb.table("motion_clips").insert(payload).execute()
            if res.data:
                inserted += 1
            else:
                print(f"  ! INSERT 실패: {clip_id}")

    if apply:
        print(f"완료: {inserted}/{len(by_time)}건 등록")
    return len(by_time), inserted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--camera", help="카메라 text id (예: p4cam-2d854b2f)")
    target.add_argument(
        "--all-linked", action="store_true",
        help="연결된(unlinked_at IS NULL) 카메라 전부 순회 — 주기 실행용",
    )
    ap.add_argument("--since", help="시작 날짜 (KST, YYYY-MM-DD). 생략 시 --days 로 계산")
    ap.add_argument("--until", help="끝 날짜 (KST, YYYY-MM-DD, 포함). 생략 시 오늘")
    ap.add_argument("--days", type=int, default=2, help="--since/--until 생략 시 최근 N일 (기본 2)")
    ap.add_argument("--apply", action="store_true", help="실제 INSERT (기본은 dry-run)")
    ap.add_argument(
        "--allow-unlinked", action="store_true",
        help="해제된 카메라도 진행 (enclosure_id NULL 로 등록돼 앱 목록엔 안 보임)",
    )
    args = ap.parse_args()

    if args.since or args.until:
        if not (args.since and args.until):
            sys.exit("--since 와 --until 은 같이 지정")
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until)
    else:
        since, until = default_range(args.days)
    if since > until:
        sys.exit("--since 가 --until 보다 뒤임")

    sb = get_supabase_client()
    if args.camera:
        cam_res = (
            sb.table("cameras").select(CAMERA_COLUMNS).eq("camera_id", args.camera).single().execute()
        )
        if not cam_res.data:
            sys.exit(f"cameras 에 없는 camera_id: {args.camera}")
        camera = cam_res.data
        if camera.get("unlinked_at"):
            print(f"! 해제된 카메라 (unlinked_at={camera['unlinked_at']}, enclosure_id="
                  f"{camera.get('enclosure_id')}) — 등록해도 앱 목록엔 안 보임")
            if not args.allow_unlinked:
                sys.exit("  중단. 그래도 등록하려면 --allow-unlinked")
        cameras = [camera]
    else:
        res = (
            sb.table("cameras").select(CAMERA_COLUMNS).is_("unlinked_at", "null")
            .order("camera_id").execute()
        )
        cameras = list(res.data or [])
        print(f"연결된 카메라 {len(cameras)}대 ({since} ~ {until} KST)")

    client = get_r2_client()
    bucket = get_r2_bucket()
    total_candidates = total_inserted = 0
    for camera in cameras:
        if len(cameras) > 1:
            print(f"\n== {camera['camera_id']}")
        c, i = reconcile_camera(sb, client, bucket, camera, since, until, apply=args.apply)
        total_candidates += c
        total_inserted += i

    if len(cameras) > 1:
        print(f"\n합계: 등록 대상 {total_candidates}건, INSERT {total_inserted}건")
    if not args.apply and total_candidates:
        print("dry-run 완료 — 실제 등록은 --apply 로 재실행")


if __name__ == "__main__":
    main()
