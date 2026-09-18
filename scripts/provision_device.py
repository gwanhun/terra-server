#!/usr/bin/env python3
"""물리 기기에 자격증명 NVS 주입 (USB, nRF/BLE 불필요).

웹 등록 패널이 발급한 device_id + mqtt_token 과 WiFi SSID/PW 를 ESP32 의 NVS 파티션에
직접 굽는다. 부팅하면 펌웨어가 이 값으로 WiFi + MQTT(device_id 소유 계정)에 바로 붙는다.

원리: 펌웨어 NVS 레이아웃
  - 네임스페이스 "terra": device_id, mqtt_token   (cloud_client.c)
  - 네임스페이스 "wifi":  ssid, pass               (wifi.c)
  - nvs 파티션: 오프셋 0x9000, 크기 0x6000         (partition table)

단건:
  uv run python scripts/provision_device.py \\
    --device-id terra-a1b2c3d4 --token <mqtt_token> --ssid MyWifi --wifi-pass MyPass --port /dev/tty.usbmodem1101

배치(여러 기기, 하나씩 꽂아가며): beta_devices CSV 를 읽어 순서대로 안내
  uv run python scripts/provision_device.py --csv storage/beta_devices_2026-09-18.csv --ssid MyWifi --wifi-pass MyPass

옵션:
  --dry-run    NVS 이미지(bin)만 만들고 플래시 안 함
  --erase-all  플래시 전 전체 지움(선택). 기본은 nvs 파티션만 덮어씀(앱 유지)
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IDF_PATH = Path(os.path.expanduser("~/.espressif/v6.0.1/esp-idf"))
NVS_GEN = IDF_PATH / "components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
IDF_PY = Path(os.path.expanduser("~/.espressif/python_env/idf6.0_py3.11_env/bin/python"))

NVS_OFFSET = "0x9000"
NVS_SIZE = "0x6000"
CHIP = "esp32s3"


def build_nvs_bin(device_id: str, token: str, ssid: str, wifi_pass: str, out_bin: Path) -> None:
    """자격증명 → NVS CSV → nvs_partition_gen → bin."""
    rows = [
        ["key", "type", "encoding", "value"],
        ["terra", "namespace", "", ""],
        ["device_id", "data", "string", device_id],
        ["mqtt_token", "data", "string", token],
        ["wifi", "namespace", "", ""],
        ["ssid", "data", "string", ssid],
        ["pass", "data", "string", wifi_pass],
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        csv.writer(f).writerows(rows)
        csv_path = f.name
    try:
        subprocess.run([str(IDF_PY), str(NVS_GEN), "generate", csv_path, str(out_bin), NVS_SIZE],
                       check=True, capture_output=True, text=True)
    finally:
        os.unlink(csv_path)


def flash_nvs(out_bin: Path, port: str | None, erase_all: bool) -> None:
    if erase_all:
        cmd = [str(IDF_PY), "-m", "esptool", "--chip", CHIP]
        if port:
            cmd += ["-p", port]
        subprocess.run(cmd + ["erase_flash"], check=True)
    cmd = [str(IDF_PY), "-m", "esptool", "--chip", CHIP]
    if port:
        cmd += ["-p", port]
    cmd += ["write_flash", NVS_OFFSET, str(out_bin)]
    subprocess.run(cmd, check=True)


def provision_one(device_id: str, token: str, ssid: str, wifi_pass: str,
                  port: str | None, dry_run: bool, erase_all: bool) -> None:
    out_bin = REPO_ROOT / "storage" / f"nvs_{device_id}.bin"
    out_bin.parent.mkdir(exist_ok=True)
    build_nvs_bin(device_id, token, ssid, wifi_pass, out_bin)
    print(f"  NVS 이미지 생성: {out_bin.relative_to(REPO_ROOT)} ({out_bin.stat().st_size} bytes)")
    if dry_run:
        print("  [dry-run] 플래시 생략")
        return
    flash_nvs(out_bin, port, erase_all)
    print(f"  ✅ {device_id} 주입 완료 (SSID={ssid})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device-id")
    p.add_argument("--token")
    p.add_argument("--csv", type=Path, help="beta_devices CSV (email,device_name,device_uuid,device_id,mqtt_token)")
    p.add_argument("--ssid", required=True)
    p.add_argument("--wifi-pass", required=True)
    p.add_argument("--port", help="예 /dev/tty.usbmodem1101 (생략 시 esptool 자동 탐지)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--erase-all", action="store_true")
    args = p.parse_args()

    if not NVS_GEN.exists():
        sys.exit(f"nvs_partition_gen.py 없음: {NVS_GEN}")

    if args.csv:
        rows = [r for r in csv.DictReader(args.csv.open(encoding="utf-8"))
                if r.get("device_id") and r.get("mqtt_token")]
        print(f"{args.csv.name}: 기기 {len(rows)}개. 하나씩 꽂아가며 진행.")
        for i, r in enumerate(rows, 1):
            print(f"\n[{i}/{len(rows)}] {r.get('email','')} → {r['device_id']}")
            if not args.dry_run:
                input(f"  이 기기를 USB 로 꽂고 Enter (port={args.port or '자동'}) …")
            provision_one(r["device_id"], r["mqtt_token"], args.ssid, args.wifi_pass,
                          args.port, args.dry_run, args.erase_all)
        return 0

    if not args.device_id or not args.token:
        sys.exit("--device-id 와 --token, 또는 --csv 필요")
    provision_one(args.device_id, args.token, args.ssid, args.wifi_pass,
                  args.port, args.dry_run, args.erase_all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
