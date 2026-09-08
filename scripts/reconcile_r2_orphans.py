"""R2 orphan mp4 → motion_clips 역등록 (펌웨어 업로드 중단 복구).

배경: 레거시 펌웨어가 썸네일 PUT 실패를 클립 전체 실패로 처리해서
"R2 엔 mp4 있음 + DB 엔 row 없음" 상태가 생겼다 (p4cam-2d854b2f 8/30~31).
이 스크립트는 R2 를 스캔해 DB 에 없는 mp4 를 motion_clips 에 등록한다.
썸네일 jpg 가 R2 에 있으면 함께 연결, 없으면 NULL(앱은 아이콘 폴백).

로컬 .env 는 R2 키가 placeholder 라 프로덕션 서버에서 실행:
    uv run python scripts/reconcile_r2_orphans.py --camera p4cam-2d854b2f \
        --since 2026-08-30 --until 2026-08-31          # dry-run (기본)
    ... --apply                                        # 실제 INSERT

- 멱등: clip_id(=파일명 UUID)가 이미 DB 에 있으면 스킵.
- duration 은 mp4 의 mvhd box 에서 추출 (Range GET, 파일 전체 다운로드 안 함).
  못 찾으면 기본 60.0s 로 등록하고 표시.
- 같은 클립이 SD 재시도로 여러 clip_id 에 중복 업로드됐을 수 있다 →
  started_at(파일명 HHMMSS) 이 같은 orphan 은 가장 큰 파일 1개만 등록.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.r2_client import get_r2_bucket, get_r2_client
from backend.supabase_client import get_supabase_client

KST = ZoneInfo("Asia/Seoul")
DEFAULT_DURATION_SEC = 60.0

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


def list_objects(client, bucket: str, prefix: str) -> dict[str, int]:
    """prefix 아래 key → size 매핑."""
    out: dict[str, int] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = obj["Size"]
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", required=True, help="카메라 text id (예: p4cam-2d854b2f)")
    ap.add_argument("--since", required=True, help="시작 날짜 (KST, YYYY-MM-DD)")
    ap.add_argument("--until", required=True, help="끝 날짜 (KST, YYYY-MM-DD, 포함)")
    ap.add_argument("--apply", action="store_true", help="실제 INSERT (기본은 dry-run)")
    args = ap.parse_args()

    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until)
    if since > until:
        sys.exit("--since 가 --until 보다 뒤임")

    sb = get_supabase_client()
    cam_res = (
        sb.table("cameras")
        .select("id,camera_id,owner_id,enclosure_id")
        .eq("camera_id", args.camera)
        .single()
        .execute()
    )
    camera = cam_res.data
    if not camera:
        sys.exit(f"cameras 에 없는 camera_id: {args.camera}")

    client = get_r2_client()
    bucket = get_r2_bucket()

    # 1) R2 스캔
    objects: dict[str, int] = {}
    for d in daterange(since, until):
        prefix = f"terra-clips/clips/{args.camera}/{d.isoformat()}/"
        objects.update(list_objects(client, bucket, prefix))
    mp4s = {k: v for k, v in objects.items() if k.endswith(".mp4")}
    jpgs = {k for k in objects if k.endswith(".jpg")}
    print(f"R2 스캔: mp4 {len(mp4s)}건, jpg {len(jpgs)}건 ({since} ~ {until} KST)")
    if not mp4s:
        return

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
        tag = "INSERT" if args.apply else "DRY-RUN"
        print(
            f"  [{tag}] {started_kst:%m-%d %H:%M:%S} dur={payload['duration_sec']}"
            f" size={size} thumb={'있음' if thumbnail_key else 'NULL'}{dur_note}"
        )
        if args.apply:
            res = sb.table("motion_clips").insert(payload).execute()
            if res.data:
                inserted += 1
            else:
                print(f"  ! INSERT 실패: {clip_id}")

    if args.apply:
        print(f"완료: {inserted}/{len(by_time)}건 등록")
    else:
        print("dry-run 완료 — 실제 등록은 --apply 로 재실행")


if __name__ == "__main__":
    main()
