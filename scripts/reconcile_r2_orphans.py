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

역방향 정리 — 행은 있는데 R2 미디어가 없는 클립 (2026-11-05):
    uv run python scripts/reconcile_r2_orphans.py --prune-missing                 # 전체, dry-run
    uv run python scripts/reconcile_r2_orphans.py --prune-missing --camera p4cam-… # 한 카메라
    ... --prune-missing --apply                                                  # 행 삭제
  배경: R2 대시보드에서 카메라 폴더를 직접 지우면 DB 행이 남아 앱에 404 클립이 보인다
  (2026-11-05 확인 12,377행). 대상·결과는 CSV(--out, 기본 tmp/prune_missing_<날짜>.csv)로 남기고,
  --apply 때만 100건씩 DELETE. user_hidden_clips·gme_jobs 는 ON DELETE CASCADE. R2 는 건드리지 않는다.
  생성 10분 미만 행은 건너뛴다(방어적 — 행은 PUT 뒤에 생기지만 faststart 재기록 등과 겹치지 않게).
"""

from __future__ import annotations

import argparse
import csv
import re
import struct
import sys
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.r2_client import get_r2_bucket, get_r2_client
from backend.supabase_client import get_supabase_client

KST = ZoneInfo("Asia/Seoul")
DEFAULT_DURATION_SEC = 60.0
CAMERA_COLUMNS = "id,camera_id,owner_id,enclosure_id,unlinked_at"
DB_PAGE = 1000            # PostgREST 기본 max-rows — 넘기면 조용히 잘린다 → range 로 페이지
MIN_AGE_MIN_DEFAULT = 10  # 이보다 젊은 mp4 는 "업로드 진행 중" 으로 보고 건너뜀 (아래 split_in_flight)

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


def list_objects(client, bucket: str, prefix: str) -> dict[str, tuple[int, datetime]]:
    """prefix 아래 key → (size, LastModified) 매핑."""
    out: dict[str, tuple[int, datetime]] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = (obj["Size"], obj["LastModified"])
    return out


def split_in_flight(
    mp4s: dict[str, tuple[int, datetime]], now: datetime, min_age: timedelta,
) -> tuple[dict[str, int], list[str]]:
    """(충분히 오래된 key → size, 업로드 진행 중으로 볼 젊은 key 목록).

    펌웨어 순서는 mp4 PUT → jpg PUT → meta POST 라 몇 초~수십 초 사이엔 "R2 엔 있고 DB 엔 없음"
    이 정상이다. 그걸 등록해 버리면 뒤따르는 meta POST 가 23505 경로로 기존 행 id 만 받고
    썸네일·motion_score 가 영영 빠진다. 그래서 LastModified 가 min_age 보다 젊으면 다음 주기로.
    """
    old: dict[str, int] = {}
    young: list[str] = []
    for key, (size, last_modified) in mp4s.items():
        if now - last_modified < min_age:
            young.append(key)
        else:
            old[key] = size
    return old, young


def registered_started_at_keys(sb, camera_uuid: str, since: date, until: date) -> set[str]:
    """해당 카메라의 등록 클립 started_at 을 R2 키 형식(YYYY-MM-DD_HHMMSS, KST) 집합으로.

    range 로 페이지 — 한 카메라가 9일에 1800건이라 1000행 캡에 잘리면 2b 필터가 구멍 난다.
    """
    lo = datetime.combine(since, datetime.min.time(), tzinfo=KST)
    hi = datetime.combine(until + timedelta(days=1), datetime.min.time(), tzinfo=KST)
    out: set[str] = set()
    offset = 0
    while True:
        res = (
            sb.table("motion_clips")
            .select("started_at")
            .eq("camera_id", camera_uuid)
            .gte("started_at", lo.astimezone(timezone.utc).isoformat())
            .lt("started_at", hi.astimezone(timezone.utc).isoformat())
            .order("started_at")
            .range(offset, offset + DB_PAGE - 1)
            .execute()
        )
        rows = list(res.data or [])
        for row in rows:
            ts = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")).astimezone(KST)
            out.add(f"{ts:%Y-%m-%d}_{ts:%H%M%S}")
        if len(rows) < DB_PAGE:
            return out
        offset += DB_PAGE


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


def find_dangling(
    rows: list[dict[str, Any]], r2_keys: set[str], now: datetime, min_age: timedelta,
) -> list[dict[str, Any]]:
    """R2 에 mp4 가 없는 행. 생성 min_age 미만 행은 제외(방어적)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["r2_key"] in r2_keys:
            continue
        created = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        if now - created < min_age:
            continue
        out.append(r)
    return out


def prune_missing(
    sb, client, bucket: str, camera_text: str | None, *, apply: bool, out_csv: Path,
    min_age: timedelta = timedelta(minutes=MIN_AGE_MIN_DEFAULT),
) -> tuple[int, int]:
    """미디어 없는 motion_clips 행을 찾아 CSV 로 남기고, --apply 면 삭제. (대상 수, 삭제 수)."""
    prefix = f"terra-clips/clips/{camera_text}/" if camera_text else "terra-clips/clips/"
    r2_keys = set(list_objects(client, bucket, prefix))
    print(f"R2 스캔: {prefix} 아래 객체 {len(r2_keys)}건")

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        res = (
            sb.table("motion_clips")
            .select("id, camera_id, r2_key, thumbnail_key, started_at, created_at, file_size")
            .like("r2_key", prefix + "%")
            .order("id")
            .range(offset, offset + DB_PAGE - 1)
            .execute()
        )
        page = list(res.data or [])
        rows += page
        if len(page) < DB_PAGE:
            break
        offset += DB_PAGE
    dangling = find_dangling(rows, r2_keys, datetime.now(timezone.utc), min_age)
    print(f"motion_clips 행 {len(rows)}건 → 미디어 없음 {len(dangling)}건")
    if not dangling:
        return 0, 0

    by_cam: dict[str, int] = {}
    for r in dangling:
        cam = r["r2_key"].split("/")[2]
        by_cam[cam] = by_cam.get(cam, 0) + 1
    for cam, n in sorted(by_cam.items(), key=lambda x: -x[1]):
        print(f"  {cam}: {n}건")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["clip_id", "key_camera", "r2_key", "thumbnail_key", "file_size", "started_at", "row_created_at", "deleted"])
        for r in sorted(dangling, key=lambda r: r["r2_key"]):
            w.writerow([r["id"], r["r2_key"].split("/")[2], r["r2_key"], r.get("thumbnail_key") or "",
                        r.get("file_size"), r["started_at"], r["created_at"], "pending" if apply else "dry-run"])
    print(f"목록 저장: {out_csv}")

    if not apply:
        return len(dangling), 0

    # 배치 DELETE 는 한 행이라도 FK/트리거에 걸리면 전체 롤백된다(2026-11-05 실측: 앱팀 테이블
    # motion_clip_review_slots 가 RESTRICT FK, clip_python_evidence_runs 가 append-only 트리거).
    # 실패한 배치는 행 단위로 다시 시도해 막힌 행만 사유(테이블명)와 함께 남긴다.
    ids = [r["id"] for r in dangling]
    deleted: set[str] = set()
    blocked: dict[str, str] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        try:
            res = sb.table("motion_clips").delete().in_("id", chunk).execute()
            deleted.update(r["id"] for r in res.data or [])
            continue
        except Exception:  # noqa: BLE001 — 아래서 행 단위로 분리
            pass
        for clip_id in chunk:
            try:
                res = sb.table("motion_clips").delete().eq("id", clip_id).execute()
                if res.data:
                    deleted.add(clip_id)
            except Exception as exc:  # noqa: BLE001 — 막힌 행만 기록
                blocked[clip_id] = _blocker_name(exc)
    status = {cid: "deleted" for cid in deleted}
    status.update({cid: f"blocked:{why}" for cid, why in blocked.items()})
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["clip_id", "key_camera", "r2_key", "thumbnail_key", "file_size", "started_at", "row_created_at", "deleted"])
        for r in sorted(dangling, key=lambda r: r["r2_key"]):
            w.writerow([r["id"], r["r2_key"].split("/")[2], r["r2_key"], r.get("thumbnail_key") or "",
                        r.get("file_size"), r["started_at"], r["created_at"], status.get(r["id"], "unknown")])
    print(f"완료: {len(deleted)}/{len(ids)}건 삭제, 막힘 {len(blocked)}건 (결과 CSV deleted 열 참고)")
    if blocked:
        by_why: dict[str, int] = {}
        for why in blocked.values():
            by_why[why] = by_why.get(why, 0) + 1
        for why, n in sorted(by_why.items(), key=lambda x: -x[1]):
            print(f"  - {why}: {n}건")
        print("  → 막힌 행은 참조 테이블 소유자(앱팀)가 참조 행을 정리하거나 FK 를 CASCADE 로 바꿔야 지워진다")
    return len(dangling), len(deleted)


_FK_TABLE_RE = re.compile(r'on table "([^"]+)"')
_APPEND_ONLY_RE = re.compile(r"(\w+) is append-only")


def _blocker_name(exc: Exception) -> str:
    """PostgREST 오류 문자열에서 막은 테이블명을 뽑는다.

    23503 은 'on table "motion_clips" ... on table "<자식>"' 꼴이라 **마지막** 매치가 자식 테이블.
    append-only 트리거는 '<테이블> is append-only'. 못 찾으면 오류 첫 줄.
    """
    text = str(exc)
    fk = _FK_TABLE_RE.findall(text)
    if fk:
        return fk[-1]
    m = _APPEND_ONLY_RE.search(text)
    if m:
        return m.group(1)
    return text.splitlines()[0][:60]


def reconcile_camera(
    sb, client, bucket: str, camera: dict[str, Any], since: date, until: date, *,
    apply: bool, min_age: timedelta = timedelta(minutes=MIN_AGE_MIN_DEFAULT),
) -> tuple[int, int]:
    """카메라 하나를 처리하고 (등록 대상 수, 실제 INSERT 수) 를 돌려준다."""
    camera_text = camera["camera_id"]

    # 1) R2 스캔
    objects: dict[str, tuple[int, datetime]] = {}
    for d in daterange(since, until):
        prefix = f"terra-clips/clips/{camera_text}/{d.isoformat()}/"
        objects.update(list_objects(client, bucket, prefix))
    all_mp4s = {k: v for k, v in objects.items() if k.endswith(".mp4")}
    jpgs = {k for k in objects if k.endswith(".jpg")}
    mp4s, in_flight = split_in_flight(all_mp4s, datetime.now(timezone.utc), min_age)
    print(f"R2 스캔: mp4 {len(all_mp4s)}건, jpg {len(jpgs)}건 ({since} ~ {until} KST)")
    if in_flight:
        print(f"업로드 진행 중일 수 있는 {len(in_flight)}건(LastModified < {min_age}) 은 다음 주기로")
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
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--camera", help="카메라 text id (예: p4cam-2d854b2f)")
    target.add_argument(
        "--all-linked", action="store_true",
        help="연결된(unlinked_at IS NULL) 카메라 전부 순회 — 주기 실행용",
    )
    ap.add_argument("--since", help="시작 날짜 (KST, YYYY-MM-DD). 생략 시 --days 로 계산")
    ap.add_argument("--until", help="끝 날짜 (KST, YYYY-MM-DD, 포함). 생략 시 오늘")
    ap.add_argument("--days", type=int, default=2, help="--since/--until 생략 시 최근 N일 (기본 2)")
    ap.add_argument(
        "--min-age-min", type=int, default=MIN_AGE_MIN_DEFAULT,
        help=f"이보다 젊은(분) mp4 는 업로드 진행 중으로 보고 건너뜀 (기본 {MIN_AGE_MIN_DEFAULT})",
    )
    ap.add_argument(
        "--prune-missing", action="store_true",
        help="역방향: R2 에 미디어가 없는 motion_clips 행 정리 (--camera 로 한정 가능, 기본 전체)",
    )
    ap.add_argument("--out", help="--prune-missing 대상 CSV 경로 (기본 tmp/prune_missing_<날짜>.csv)")
    ap.add_argument("--apply", action="store_true", help="실제 INSERT/DELETE (기본은 dry-run)")
    ap.add_argument(
        "--allow-unlinked", action="store_true",
        help="해제된 카메라도 진행 (enclosure_id NULL 로 등록돼 앱 목록엔 안 보임)",
    )
    args = ap.parse_args()

    if args.prune_missing:
        if args.all_linked or args.since or args.until:
            sys.exit("--prune-missing 은 --camera(선택) 만 받는다")
        out_csv = Path(args.out) if args.out else Path("tmp") / f"prune_missing_{datetime.now(KST):%Y-%m-%d}.csv"
        n, deleted = prune_missing(
            get_supabase_client(), get_r2_client(), get_r2_bucket(), args.camera,
            apply=args.apply, out_csv=out_csv,
            min_age=timedelta(minutes=max(args.min_age_min, 0)),
        )
        if not args.apply and n:
            print("dry-run 완료 — 실제 삭제는 --apply 로 재실행")
        return
    if not (args.camera or args.all_linked):
        ap.error("--camera 또는 --all-linked 중 하나 필요")

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
        # .single() 은 0행에 PGRST116 예외라 친절한 메시지가 못 나간다(backend/db_helpers.py) → limit(1)
        cam_rows = (
            sb.table("cameras").select(CAMERA_COLUMNS).eq("camera_id", args.camera).limit(1).execute().data
        ) or []
        if not cam_rows:
            sys.exit(f"cameras 에 없는 camera_id: {args.camera}")
        camera = cam_rows[0]
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
    min_age = timedelta(minutes=max(args.min_age_min, 0))
    total_candidates = total_inserted = 0
    failed: list[str] = []
    for camera in cameras:
        if len(cameras) > 1:
            print(f"\n== {camera['camera_id']}")
        try:
            c, i = reconcile_camera(
                sb, client, bucket, camera, since, until, apply=args.apply, min_age=min_age,
            )
        except Exception as exc:  # noqa: BLE001 — 한 카메라의 R2/Supabase 오류가 나머지를 막으면 안 된다(cron)
            failed.append(camera["camera_id"])
            print(f"  ! 실패 — 다음 카메라로: {exc!r}")
            continue
        total_candidates += c
        total_inserted += i

    if len(cameras) > 1:
        print(f"\n합계: 등록 대상 {total_candidates}건, INSERT {total_inserted}건, 실패 카메라 {len(failed)}대")
    if not args.apply and total_candidates:
        print("dry-run 완료 — 실제 등록은 --apply 로 재실행")
    if failed:
        sys.exit(f"실패 카메라: {', '.join(failed)} — 다음 주기에 같은 범위가 다시 스캔되게 --days 를 넉넉히")


if __name__ == "__main__":
    main()
