#!/usr/bin/env python
"""
OTA 릴리스 등록 — 빌드 산출물(.bin, 선택 .elf)을 R2 에 올리고 firmware_releases 에 1행 INSERT.

    uv run python scripts/upload_firmware.py --target camera_p4 \
        --bin ~/project/esp32/firebeetle2-p4-yr030/build/firebeetle2_p4_yr030.bin \
        --elf ~/project/esp32/firebeetle2-p4-yr030/build/firebeetle2_p4_yr030.elf \
        --notes "DNS 폴백 + last_err.detail"

    uv run python scripts/upload_firmware.py --list [--target device_nano]

    uv run python scripts/upload_firmware.py --delete <release_id> --reason "prepare 스택 버그" [--yes]

--delete: R2 의 bin/elf 를 지우고, 작업 이력(ota_jobs)이 없으면 행을 DELETE, 있으면 FK 때문에
retired_at/retired_reason 만 채운다(퇴역 — 목록·OTA 대상·다운로드에서 제외, 이력은 유지).
진행 중(pending/accepted/downloading/ready/applying) 작업이 참조하면 거절.

등록 전에 거절하는 것(specs/stage-j-ota.md 리스크 A4·C6·C7·D1):
- 이미지 헤더의 chip_id 가 target 과 다름 (카메라 bin 을 nano 로 등록 등)
- esp_app_desc.version 이 비었거나 `-dirty` (--allow-dirty 로만 허용) — 같은 문자열의 다른 빌드 차단
- 같은 (target, version) 이 이미 있음
- 바이너리에 자격증명/기본 비밀번호 흔적: `p4cam-xxxxxxxx` / `terra-xxxxxxxx` 식 id, bcrypt `$2b$`,
  "ChangeMe", --deny-string 추가분

버전 문자열은 사람이 넘기지 않는다 — 바이너리 안의 esp_app_desc 에서 읽는다
(펌웨어 `APP_PROJECT_VER_FROM_CONFIG` 로 APP_FIRMWARE_VER 와 한 소스).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

# ESP-IDF 이미지: [esp_image_header_t 24B][esp_image_segment_header_t 8B][esp_app_desc_t ...]
_IMAGE_MAGIC = 0xE9
_APP_DESC_OFFSET = 24 + 8
_APP_DESC_MAGIC = 0xABCD5432
# esp_app_desc_t: magic(4) secure_version(4) reserv1(8) version(32) project_name(32) time(16) date(16) idf_ver(32) app_elf_sha256(32)
_APP_DESC_FMT = "<II8s32s32s16s16s32s32s"
# esp_chip_id_t
CHIP_ID_BY_TARGET: dict[str, int] = {"camera_p4": 0x0012, "device_nano": 0x0009}
CHIP_NAME: dict[int, str] = {0x0012: "esp32p4", 0x0009: "esp32s3", 0x0000: "esp32", 0x0002: "esp32s2",
                             0x0005: "esp32c3", 0x000D: "esp32c6"}

_DEFAULT_DENY = [
    r"p4cam-[0-9a-f]{8}",        # 구운 camera_id (Kconfig creds 비우지 않고 빌드)
    r"p4hub-[0-9a-f]{8}",
    r"terra-[0-9a-f]{8}",        # 구운 device_id
    r"\$2[aby]\$\d\d\$",         # bcrypt 해시
    r"ChangeMe",                  # Kconfig 기본 비밀번호류
]


@dataclass
class AppDesc:
    version: str
    project_name: str
    build_time: str
    build_date: str
    idf_ver: str
    elf_sha256: str


def _cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("utf-8", "replace")


def parse_image(data: bytes) -> tuple[int, AppDesc]:
    """(chip_id, app_desc). 헤더가 아니면 ValueError."""
    if len(data) < _APP_DESC_OFFSET + struct.calcsize(_APP_DESC_FMT):
        raise ValueError("파일이 너무 작음 — ESP-IDF 앱 이미지가 아님")
    if data[0] != _IMAGE_MAGIC:
        raise ValueError(f"이미지 매직 불일치 (0x{data[0]:02X} != 0xE9) — 앱 .bin 이 아님(부트로더/병합 이미지?)")
    chip_id = struct.unpack_from("<H", data, 12)[0]
    fields = struct.unpack_from(_APP_DESC_FMT, data, _APP_DESC_OFFSET)
    if fields[0] != _APP_DESC_MAGIC:
        raise ValueError("esp_app_desc 매직 불일치 — IDF 앱 이미지가 아니거나 레이아웃이 다름")
    return chip_id, AppDesc(
        version=_cstr(fields[3]), project_name=_cstr(fields[4]), build_time=_cstr(fields[5]),
        build_date=_cstr(fields[6]), idf_ver=_cstr(fields[7]), elf_sha256=fields[8].hex(),
    )


def scan_secrets(data: bytes, extra: list[str]) -> list[str]:
    """바이너리 안의 printable 문자열에서 금지 패턴을 찾는다. 걸린 (패턴, 일부) 목록."""
    hits: list[str] = []
    text = data.decode("latin-1")
    for pat in _DEFAULT_DENY + extra:
        m = re.search(pat, text)
        if m:
            s = max(0, m.start() - 8)
            hits.append(f"{pat!r} → …{text[s:m.end() + 8]!r}…")
    return hits


def r2_key(target: str, version: str, ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", version)
    return f"firmware/{target}/{slug}.{ext}"


def _put(client, bucket: str, key: str, body: bytes, content_type: str) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def cmd_list(args: argparse.Namespace) -> int:
    from backend.supabase_client import get_supabase_client
    sb = get_supabase_client()
    q = sb.table("firmware_releases").select("*").order("created_at", desc=True).limit(50)
    if args.target:
        q = q.eq("target", args.target)
    rows = q.execute().data or []
    print(f"{'created':20} {'target':12} {'version':28} {'size':>9}  id")
    for r in rows:
        retired = f"  [퇴역 {r['retired_at'][:10]}: {r.get('retired_reason') or '-'}]" if r.get("retired_at") else ""
        print(f"{r['created_at'][:19]:20} {r['target']:12} {r['version']:28} {r['size_bytes']:>9}  {r['id']}"
              + retired + (f"  — {r['notes']}" if r.get("notes") else ""))
    return 0


_ACTIVE_JOB_STATUSES = frozenset({"pending", "accepted", "downloading", "ready", "applying"})


def cmd_delete(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from backend.r2_client import get_r2_bucket, get_r2_client
    from backend.supabase_client import get_supabase_client

    sb = get_supabase_client()
    rel = (sb.table("firmware_releases").select("*").eq("id", args.delete).limit(1).execute().data or [None])[0]
    if not rel:
        print(f"없음: release_id={args.delete}", file=sys.stderr)
        return 2

    jobs = sb.table("ota_jobs").select("id, status, kind").eq("release_id", rel["id"]).execute().data or []
    active = [j for j in jobs if j["status"] in _ACTIVE_JOB_STATUSES]
    if active:
        print("거절: 진행 중 작업이 이 릴리스를 참조함 — 끝난 뒤 다시:", file=sys.stderr)
        for j in active:
            print(f"  - {j['id']} {j['kind']} {j['status']}", file=sys.stderr)
        return 3

    running: list[str] = []
    for table, idcol in (("cameras", "camera_id"), ("devices", "device_id")):
        rows = sb.table(table).select(idcol).eq("firmware_ver", rel["version"]).is_("unlinked_at", "null") \
            .execute().data or []
        running += [r[idcol] for r in rows]

    mode = "퇴역(retired_at 설정, 행 유지)" if jobs else "행 DELETE"
    print(f"release      : {rel['id']}")
    print(f"target       : {rel['target']}   version: {rel['version']}   size: {rel['size_bytes']:,} B")
    print(f"r2           : {rel['r2_key']}" + (f"  (+ {rel['elf_r2_key']})" if rel.get("elf_r2_key") else ""))
    print(f"작업 이력     : {len(jobs)}건 → {mode}")
    if running:
        print(f"경고         : 이 버전으로 돌고 있는 보드 {len(running)}대 — {', '.join(running)} "
              "(보드 동작엔 영향 없음. 바이너리만 더는 받을 수 없게 됨)")
    if rel.get("retired_at"):
        print(f"이미 퇴역됨   : {rel['retired_at']} ({rel.get('retired_reason') or '-'}) — R2 삭제만 다시 시도")
    if not args.yes:
        ans = input("진행? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("취소")
            return 1

    # DB 먼저, R2 는 그 다음 — 컬럼 미적용 등으로 DB 가 실패하면 바이너리는 손대지 않는다
    # (R2 만 지워지고 행이 살아 있으면 "고를 수 있는데 받을 수 없는" 릴리스가 된다).
    if jobs:
        patch = {"retired_at": datetime.now(UTC).isoformat(), "retired_reason": args.reason or "deleted"}
        try:
            sb.table("firmware_releases").update(patch).eq("id", rel["id"]).execute()
        except Exception as e:  # noqa: BLE001 — PostgREST 오류 메시지를 그대로 보여주는 게 목적
            print(f"퇴역 표시 실패: {e}\n→ migrations/2026-10-08_firmware_releases_retired.sql 적용 여부 확인 "
                  "(R2 는 건드리지 않았음)", file=sys.stderr)
            return 4
        print(f"퇴역 완료     : release_id={rel['id']} ({patch['retired_reason']})")
    else:
        sb.table("firmware_releases").delete().eq("id", rel["id"]).execute()
        print(f"삭제 완료     : release_id={rel['id']}")

    client, bucket = get_r2_client(), get_r2_bucket()
    for key in (rel["r2_key"], rel.get("elf_r2_key")):
        if key:
            client.delete_object(Bucket=bucket, Key=key)   # S3 삭제는 없는 키도 204 — 멱등
            print(f"r2 삭제      : {key}")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    bin_path = Path(args.bin).expanduser()
    data = bin_path.read_bytes()
    chip_id, desc = parse_image(data)

    want = CHIP_ID_BY_TARGET[args.target]
    if chip_id != want:
        print(f"거절: 이미지 chip_id={CHIP_NAME.get(chip_id, hex(chip_id))} 인데 target {args.target} 은 "
              f"{CHIP_NAME[want]} 용", file=sys.stderr)
        return 2
    if not desc.version:
        print("거절: esp_app_desc.version 이 비어 있음 — APP_PROJECT_VER 설정 확인", file=sys.stderr)
        return 2
    if "-dirty" in desc.version and not args.allow_dirty:
        print(f"거절: dirty 빌드 {desc.version!r} — 커밋 후 다시 빌드하거나 --allow-dirty", file=sys.stderr)
        return 2
    hits = scan_secrets(data, args.deny_string)
    if hits:
        print("거절: 바이너리에 자격증명/기본 비밀번호 흔적:", file=sys.stderr)
        for h in hits:
            print("  -", h, file=sys.stderr)
        print("Kconfig creds 를 비우고(BETA_ONBOARDING.md §3) 다시 빌드. 오탐이면 --deny-string 조정",
              file=sys.stderr)
        return 2

    sha = hashlib.sha256(data).hexdigest()
    key_bin = r2_key(args.target, desc.version, "bin")
    elf_path = Path(args.elf).expanduser() if args.elf else None
    key_elf = r2_key(args.target, desc.version, "elf") if elf_path else None

    print(f"target       : {args.target} ({CHIP_NAME[want]})")
    print(f"version      : {desc.version}")
    print(f"project_name : {desc.project_name}   idf: {desc.idf_ver}   built: {desc.build_date} {desc.build_time}")
    print(f"size         : {len(data):,} B   sha256: {sha}")
    print(f"r2 key       : {key_bin}" + (f"  (+ {key_elf})" if key_elf else ""))
    if args.dry_run:
        print("dry-run: 업로드·등록 안 함")
        return 0

    from backend.r2_client import get_r2_bucket, get_r2_client
    from backend.supabase_client import get_supabase_client

    sb = get_supabase_client()
    dup = sb.table("firmware_releases").select("id").eq("target", args.target) \
        .eq("version", desc.version).limit(1).execute().data
    if dup:
        print(f"거절: (target={args.target}, version={desc.version!r}) 이미 등록됨 id={dup[0]['id']}. "
              f"APP_FIRMWARE_VER 를 올려 다시 빌드", file=sys.stderr)
        return 3

    client, bucket = get_r2_client(), get_r2_bucket()
    _put(client, bucket, key_bin, data, "application/octet-stream")
    if elf_path and key_elf:
        _put(client, bucket, key_elf, elf_path.read_bytes(), "application/octet-stream")

    row = {
        "target": args.target, "version": desc.version, "r2_key": key_bin, "elf_r2_key": key_elf,
        "size_bytes": len(data), "sha256": sha, "project_name": desc.project_name or None,
        "idf_ver": desc.idf_ver or None, "notes": args.notes,
    }
    res = sb.table("firmware_releases").insert(row).execute()
    rel = (res.data or [None])[0]
    if not rel:
        print("등록 실패: firmware_releases INSERT 가 행을 돌려주지 않음", file=sys.stderr)
        return 4
    print(f"등록 완료: release_id={rel['id']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=sorted(CHIP_ID_BY_TARGET), help="camera_p4 | device_nano")
    p.add_argument("--bin", help="idf.py build 산출 앱 .bin")
    p.add_argument("--elf", help="같은 빌드의 .elf (코어덤프 해석용, 선택)")
    p.add_argument("--notes", help="릴리스 메모")
    p.add_argument("--allow-dirty", action="store_true", help="-dirty 버전 허용(개발용)")
    p.add_argument("--deny-string", action="append", default=[], help="추가 금지 정규식 (반복 가능)")
    p.add_argument("--dry-run", action="store_true", help="검사·요약만, 업로드·등록 안 함")
    p.add_argument("--list", action="store_true", help="등록된 릴리스 목록")
    p.add_argument("--delete", metavar="RELEASE_ID", help="릴리스 삭제/퇴역 (R2 bin·elf 삭제 포함)")
    p.add_argument("--reason", help="--delete 사유 (retired_reason)")
    p.add_argument("--yes", action="store_true", help="--delete 확인 프롬프트 생략")
    args = p.parse_args(argv)

    if args.list:
        return cmd_list(args)
    if args.delete:
        return cmd_delete(args)
    if not args.target or not args.bin:
        p.error("--target 과 --bin 이 필요 (또는 --list / --delete)")
    return cmd_upload(args)


if __name__ == "__main__":
    sys.exit(main())
