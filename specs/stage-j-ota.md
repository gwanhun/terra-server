# Stage J — 펌웨어 원격 업데이트 (OTA)

**상태**: 🚧 승인됨 (2026-10-07, 방문 일정만 미정) · **대상**: 카메라(ESP32-P4) 먼저, 사육장 기기(ESP32-S3 **nano**) 다음. **supermini 제외**(owner 결정 2026-10-06)
**목표**: 베타 카메라 18대·nano 기기에 펌웨어를 **케이블 없이** 배포한다. 실패하면 **스스로 이전 버전으로 복귀**한다.

## 왜 지금

- 9/28 이후 한 달간 고친 것(WiFi 재연결·업로드 정체·인코더 스톨 워치독·에러 로그·코어덤프)이 **베타 기기에 거의 안 들어갔다**. 매 변경이 물리 리플래시라서다. `firebeetle2-p4-yr030/docs/IMPROVEMENTS.md` §14~§17 전부 "OTA 없음 → 한 빌드로 묶어 리플래시" 전제.
- C6 블루투스 고착 사태(`docs/C6_BLE_RECOVERY.md` "남은 위험") 결론: "원격 복구 경로가 없다. OTA 도입이 재발 위험에 대한 보험. 회수 때 OTA 를 같이 올리면 기기를 두 번 만지지 않는다."
- 지금 유일한 원격 수단은 `reboot` 명령 하나.

## 확인된 사실 (2026-10-06 조사)

| 항목 | 카메라 P4 (`firebeetle2-p4-yr030`) | 기기 S3 (`terra-iot-nano`) |
|---|---|---|
| 플래시 설정 | **2MB** (`ESPTOOLPY_FLASHSIZE_2MB`) | 4MB |
| 실제 칩 | **16MB** (C6_BLE_RECOVERY.md 실측. "8분의 1만 쓰는 중") | **4MB** — Geekble nano = `ESP32-S3FH4R2` (칩 내장 플래시 4MB, PSRAM 2MB). 나무위키 제품안내 페이지 사양표(2026-10-06 확인). 내장형이라 증설 불가 |
| 파티션 | 커스텀: nvs 0x9000/0x6000 · phy · **factory 1900K 단일** | `partitions_singleapp_large` (factory 3MB 단일) |
| 앱 크기 | 1,825,936 B (1.74MB, 파티션 여유 4%) | 1,326,224 B |
| OTA 코드 | 없음 (`esp_https_ota` 미사용) | 없음 |
| 롤백 | `BOOTLOADER_APP_ROLLBACK_ENABLE` 꺼짐 | 꺼짐 |
| 버전 식별 | `APP_FIRMWARE_VER` 매크로 → 페어링 + **heartbeat `fw`** → `cameras.firmware_ver` 갱신 (9/28+) | `firmware_ver` **페어링 때만**. heartbeat 에 없음 |
| TLS | crt bundle 켜짐 (서버 API), R2 는 `r2_ca.h` 핀 | crt bundle (`cloud_client.c:310`) |
| 명령 채널 | `esp32/{camera_id}/command` 직접 발행 (`camera_commands.py`, commands 테이블 없음) | `commands` 테이블 → dispatcher 발행, ack 로 상태 추적 |
| 명령 핸들러 | `main/app_mqtt.c handle_command` (reboot: ack 후 1.5초 재부팅) | `main/src/command_dispatch.c:270` (동일 패턴) |
| 명령 수신 버퍼 | (확인 필요) | `mqtt_app.c:283 payload[768]` |
| NVS 네임스페이스 (보존 대상) | `terra-cam`(creds·rotate) · `motion` · `wifi-prov` | `terra`(creds·보정) · `wifi` · `lcd` |
| C6 코프로세서 | esp-hosted slave 2.12.8, `esp_wifi_remote ==1.6.1` 고정 | 해당 없음 |

**결론**: 카메라 OTA 를 막는 건 공간이 아니라 `2MB` 설정 한 줄 + 단일 파티션. 기기는 4MB 로도 슬롯 2개(각 1.9MB)가 들어간다. 단, **두 쪽 모두 파티션 테이블이 바뀌므로 "마지막 물리 리플래시" 1회는 불가피.** 그 바닥 빌드에는 OTA + 실기 검증된 0.2.1 만 넣고, §15 미검증 항목은 첫 OTA 로 배포한다(리스크 A3).

## In / Out

**In**
- 서버: 릴리스 등록(업로드 스크립트) · 기기별 OTA 작업 트리거 API · 진행/결과 추적 · 콘솔 버튼
- 펌웨어(카메라): OTA 파티션 전환 · `ota_update` 명령 · 부팅 시 다운로드/적용 · 롤백 · 결과 보고
- 펌웨어(기기 nano): 동일 + heartbeat 에 `fw` 추가
- 운영: 한 대씩(canary) 순차 배포 절차

**Out (이번 아님)**
- **supermini 보드** — owner 결정(2026-10-06). `firmware_releases.target` 에 넣지 않고, supermini 트리에는 OTA 코드를 포팅하지 않는다. 두 트리 공통 로직 동일 유지 규칙의 **의도된 예외**로 기록
- 서명 이미지 / Secure Boot (TLS + 토큰 인증으로 대체. 다음 단계 후보)
- C6 slave 펌웨어 OTA (`~/project/esp32/c6-ota-work/host_ota` 예제가 있으나 P4 앱 OTA 와 별개. `esp_wifi_remote` 버전 고정으로 호환 유지)
- 앱(모바일) 노출 — 운영자 콘솔·스크립트만. 앱은 `firmware_ver` 표시 그대로
- 자동 전체 롤아웃 — v1 은 운영자가 대상·순서 지정

## 아키텍처

```
[운영자 PC]  idf.py build → scripts/upload_firmware.py --target camera_p4 build/xxx.bin
                → sha256 계산 → R2 PUT firmware/camera_p4/fb2-p4 0.2.2-20261010.bin
                → firmware_releases INSERT (target, version, r2_key, size, sha256)

[콘솔/스크립트]  POST /cameras/{uuid}/ota {release_id}        (JWT, 소유자)
                → ota_jobs INSERT (status=pending)
                → MQTT esp32/{camera_id}/command
                   { msg_id, issued_at, ttl_sec:60, action:"ota_prepare",
                     job_id, version, size, sha256 }
                   (검증 완료 후) { ..., action:"ota_apply", job_id }   ← 전환+재부팅은 별도 명령

[카메라]  ack {result:"ok", state:"OTA"} → NVS terra-cam/ota_job 저장 → 1.5초 뒤 재부팅
          부팅: ota_job 있음 → 카메라 파이프라인 띄우지 않고 WiFi 만 →
                GET https://api…/firmware/{job_id}/bin  (Authorization: Bearer <camera_token>)
                → esp_https_ota 로 ota_1 에 기록 → 진행률을 ack 토픽으로 10% 단위 발행
                → 완료 → esp_ota_set_boot_partition → 재부팅
          새 펌웨어 부팅: MQTT 연결 + 첫 heartbeat 성공 → esp_ota_mark_app_valid_cancel_rollback
                         실패(연결 못 하고 N분) → esp_ota_mark_app_invalid_rollback_and_reboot → 구버전

[서버]   handle_ack: msg_id 로 ota_jobs 갱신 (sent → downloading(pct) → applied)
         handle_telemetry: heartbeat fw == release.version 이면 job=verified, 다르고 uptime 작으면 rolled_back
```

기기(S3)는 명령이 `commands` 테이블을 거친다는 점만 다르다: `POST /devices/{uuid}/ota` → `commands INSERT action=ota_update` → dispatcher 발행 → 표준 ack 흐름. 다운로드는 Device Token 으로 같은 엔드포인트.

## 설계 결정 (근거 포함)

1. **바이너리는 R2 직접이 아니라 terra-api 가 프록시**(`GET /firmware/{job_id}/bin`, Camera/Device Token 인증, 서버가 R2 에서 스트리밍).
   - R2 anycast IP 하나(172.64.66.1)가 가정 회선(KT)에서 불통이고 펌웨어에 DNS 폴백이 없다(2026-09-29 업로드 실패 원인). OTA 가 같은 함정에 빠지면 안 된다. Lightsail → 기기 경로는 하트비트·페어링으로 검증돼 있다.
   - 명령 페이로드가 짧아진다(presigned URL 400자 vs job_id). 기기 수신 버퍼 768B 안에서 안전.
   - 토큰 인증·다운로드 로그가 서버에 남는다. 이그레스는 2MB × 20대 = 40MB/릴리스로 무시 가능.
   - `API.md` 가 Device Token 용도로 "펌웨어 업데이트 등, 추후" 라고 미리 적어 둔 자리.
2. **다운로드·적용은 재부팅 직후 "깨끗한 상태" 에서.** 라이브 5~7회 뒤 내부 RAM 최대 블록이 31KB 까지 떨어지는 실측(IMPROVEMENTS §14) — 라이브·녹화와 경쟁하며 TLS 를 하나 더 열면 실패한다. `ota_update` 는 "예약" 만 하고(NVS 플래그), 재부팅 후 카메라 파이프라인을 띄우지 않은 상태에서 받는다. 부수 효과: 녹화 중단 시점이 명확하고, 실패해도 그냥 평소 부팅으로 이어진다.
   - **검토한 대안 — SD 카드에 받아 두고 적용(2026-10-06 질문, owner 확인: 전 카메라 SD 장착)**: v1 에서는 채택하지 않음. 이유 ① 비활성 OTA 슬롯(ota_1)이 이미 "스테이징 영역" 이다. `otadata` 는 다운로드·이미지 검증이 끝난 뒤에만 바뀌므로, 받다가 실패해도 구버전은 손상되지 않는다. SD 가 주는 "다 받고 검증한 뒤 플래시" 안전성은 이미 있는 것. ② **SD 가 지금 고장 이력이 가장 많은 부품**(0x107 카드 사망 fault 래치, 100MB 당 0x108 읽기 오류, C6 SDIO 간섭 의심 — IMPROVEMENTS §14·§16). SD 가 죽은 카메라야말로 OTA 로 고쳐야 할 대상인데, SD 경로만 있으면 그 카메라에 OTA 가 안 닿는다 → 직접 경로는 어차피 필요하고 SD 는 "있으면 쓰는 가속 경로" 이상이 될 수 없다. ③ RAM(B1)·부트로더(A1)·롤백 오판(C1) 리스크는 SD 와 무관. 남는 실익은 "운영 중 백그라운드 다운로드 → 다운타임 단축" 인데, 그건 SD 없이도 **유휴 시(라이브·업로드 없음) ota_1 로 직접 백그라운드 다운로드 → 조용한 시간에 전환 재부팅** 으로 얻을 수 있다. v1 실측에서 다운타임 2~3분이 문제가 되면 그 방식을 v1.1 로. SD 를 쓰게 되는 경우는 하나 — 다운로드 재개(resume)가 꼭 필요할 만큼 회선이 나쁜 집이 확인될 때. 그때도 SD 는 보조, 직접 경로가 기본.
3. **롤백은 부트로더 기능 사용** (`BOOTLOADER_APP_ROLLBACK_ENABLE`). "정상" 판정은 **MQTT 연결 + heartbeat 1회 성공**. WiFi·MQTT·TLS 가 다 살아야 하므로 "켜지긴 하는데 서버에 안 붙는 빌드" 를 걸러낸다. 판정 전 타임아웃(예 5분) 안에 못 붙으면 롤백.
4. **버전 진실은 `APP_FIRMWARE_VER` 하나.** 릴리스 등록 때 `esp_app_desc` 의 `version` 과 일치하는지 스크립트가 검사(`CONFIG_APP_PROJECT_VER_FROM_CONFIG` 로 매크로를 `PROJECT_VER` 에 연결). 같은 버전이면 펌웨어가 다운로드를 건너뛴다(`esp_https_ota_get_img_desc` 비교). 서버 검증도 heartbeat `fw` 문자열 비교.
5. **NVS 오프셋·크기 불변** (0x9000/0x6000). 파티션 전환 리플래시 때 creds·회전·보정값이 살아남아야 재페어링이 없다. `idf.py erase-flash` 금지, 부트로더+파티션테이블+앱만 플래시.
6. **한 대씩.** 서버는 target 당 동시 job 1개, 전역 동시 N개(기본 1) 제한. 순서: dev cam(p4cam-79b5d844) → 테스트 A/B → 나머지. 기기는 nano 1대 → 나머지 nano.
7. **서명 없음은 알고 가는 부채.** TLS(서버 인증서) + 기기 토큰으로 "우리 서버가 준 파일" 은 보장되지만, 서버 침해 시 임의 펌웨어 주입이 가능. `sha256` 을 명령에 실어 두고 펌웨어가 기록 후 검증하는 것부터 넣고, 서명(Secure Boot v2 또는 앱 레벨 ed25519)은 다음 스테이지.

## 파티션 (안)

카메라 P4 — `partitions.csv` + `CONFIG_ESPTOOLPY_FLASHSIZE_16MB`
```
# Name,     Type, SubType,  Offset,    Size
nvs,        data, nvs,      0x9000,    0x6000     # 불변 (creds·rotate·motion ROI)
otadata,    data, ota,      0xf000,    0x2000
phy_init,   data, phy,      0x11000,   0x1000
ota_0,      app,  ota_0,    0x20000,   4M
ota_1,      app,  ota_1,    0x420000,  4M
coredump,   data, coredump, 0x820000,  64K        # IMPROVEMENTS §15 PANIC 원인 확보와 합침
```
기기 S3 nano (4MB 확정 — ESP32-S3FH4R2 내장 플래시)
```
nvs,        data, nvs,      0x9000,    0x6000     # 불변
otadata,    data, ota,      0xf000,    0x2000
phy_init,   data, phy,      0x11000,   0x1000
ota_0,      app,  ota_0,    0x20000,   0x1E0000   # 1920K (앱 1.3MB, 여유 600K)
ota_1,      app,  ota_1,    0x200000,  0x1E0000   # 끝 0x3E0000 < 4MB
```

## 서버 변경 — ✅ 구현 완료 (2026-10-07, 브랜치 `feat/stage-j-ota`, 운영 미배포·마이그레이션 미적용)

계약 정본: [docs/API.md §4.10](../docs/API.md), [docs/MQTT.md §2·§3-b](../docs/MQTT.md), [docs/DATABASE.md](../docs/DATABASE.md).

**마이그레이션** `migrations/2026-10-07_firmware_ota.sql` (적용 후 `MIGRATIONS_APPLIED.md` 기록)
- `firmware_releases(id, target, version, r2_key, elf_r2_key, size_bytes, sha256, project_name, idf_ver, notes, created_by, created_at)` — `UNIQUE(target, version)`, RLS 정책 없음(service_role).
- `ota_jobs(id, kind, target_uuid, release_id, status, pct, prepare_msg_id, apply_msg_id, prev_version, error, issued_by, forced, created_at, updated_at, applied_at, finished_at)` — status `pending → accepted → downloading → ready → applying → verified | rolled_back`, 언제든 `failed`, 시한 `timeout`. 부분 인덱스(진행 중 상태).

**코드** (pytest 479 passed, OTA 관련 76건)
- `backend/ota_service.py` — 상태 전이 SOT. `gate_reasons`(사전 점검 ③), `create_job`, `apply_ack`(§3-b), `apply_heartbeat_fw`(verified/rolled_back), `scan_once` + `OtaMonitor`(단계별 시한·기기 commands 실패 반영, 브리지 스레드).
- `backend/mqtt/camera_commands.py` — `ota_prepare_command` / `ota_apply_command` / `ota_prepare_payload` (카메라·기기 공통 필드).
- `backend/auth_device.py` — 기기/카메라 토큰 bcrypt 검증 (`verify_entity_token`).
- `backend/routers/firmware.py` — `GET /firmware/releases`, `GET /firmware/jobs[/{id}]`(JWT, 본인 기기만), `GET /firmware/jobs/{id}/bin`(Bearer 기기 토큰, 작업 대상 기기의 토큰만, 상태 pending/accepted/downloading 만, R2 → `StreamingResponse` 프록시, 첫 바이트 전 `downloading` 표시).
- `backend/routers/cameras.py` — `POST /{uuid}/ota`(게이트 → job → `ota_prepare` 직접 발행, 발행 실패 시 `failed/publish_failed`), `POST /{uuid}/ota/{job}/apply`(`ready` 만).
- `backend/routers/commands.py` — 기기 동일 2개, `commands` 큐잉(action `ota_prepare`/`ota_apply`, ttl 60, reason 기록).
- `backend/mqtt/handlers.py` — `_route_ota_ack`(카메라: OTA 식별 가능한 ack 만, 기기: `ota` 블록 ack 는 commands 를 덮지 않음 D3 / 첫 ack 는 action 이 ota_* 일 때만), 기기 heartbeat `fw` → `devices.firmware_ver`(캐시, 다를 때만), `_ota_verify_from_heartbeat`(fw 변경 또는 **uptime 리셋** 시에만 조회 — 롤백은 fw 가 그대로라 재부팅 감지로 잡음).
- `backend/mqtt_bridge_main.py` — `OtaMonitor` 등록. `backend/main.py` — firmware 라우터.
- `scripts/upload_firmware.py` — `esp_app_desc` 파싱(버전·project_name·idf), chip_id↔target 교차 검증, `-dirty` 거절, 바이너리 자격증명 스캔(`p4cam-/terra-` id, bcrypt, `ChangeMe`), sha256, R2 PUT(bin+elf), INSERT, `--list`, `--dry-run`. **실측(2026-10-07)**: 현재 카메라 빌드의 `esp_app_desc.version` 은 `4cff056-dirty`(git describe) 라 `APP_FIRMWARE_VER` 와 다른 소스 → 펌웨어 항목 1(`APP_PROJECT_VER_FROM_CONFIG`) 필수.
- 콘솔 `web/index.html` — 카메라·기기 행 "OTA" 버튼 1개로 2단계(진행 중 없음 → 릴리스 선택 prepare, `ready` → apply 확인).
- ENV `API_PUBLIC_BASE_URL`(명령 `url`). 문서 4종 + `.env.example`.
- 테스트: `tests/test_ota_service.py`, `test_ota_api.py`, `test_ota_handlers.py`, `test_auth_device.py`, `tests/fake_sb.py`(인메모리 Supabase 흉내).

**아직**: Slack 경보(⑨)는 ota_jobs 종결 로그만. 배포 순서: 마이그레이션 적용 → terra-api·terra-bridge 재시작 → 릴리스 등록.

## 펌웨어 변경 — ✅ 빌드 통과 (2026-10-07, 실기 미검증)

| 레포 | 브랜치/커밋 | 산출물 | 비고 |
|---|---|---|---|
| `firebeetle2-p4-yr030` (카메라) | `feat/ota` `26354d1` (바닥 `sdcard` `984a4fb` = 0.2.1 커밋) | bin 1,807,600B / 슬롯 4MB · bootloader 23,200B / 24,576 | 버전 `fb2-p4 0.3.0-20261007`, 업로드 스크립트 dry-run 통과(비밀값 0건) |
| `terra-iot-nano` (기기) | `feat/ota` (바닥 `main` `6382ca1`) | bin 1,344,000B / 슬롯 1,966,080B (32% 여유) · bootloader 21,136B | 버전 `terra-fw 1.1.0-20261007`. supermini 미포팅(의도) |

구현 위치: 카메라 `main/app_ota.c`(+`app_mqtt.c`·`app_sys.c`·`main.c`), 기기 `main/src/ota.c`(+`command_dispatch.c`·`mqtt_app.c`·`sys_info.c`·`main.c`·`http_server.c`). 상세는 각 레포 `docs/OTA.md` / `docs/ota.md`.
기기 쪽 판정 차이: telemetry 가 QoS0 라 PUBACK 이 없어 **CONNACK + 첫 telemetry 발행 성공**으로 mark valid.
빌드 중 확인된 것: IDF 6.0 = mbedTLS 4.0 → `psa/crypto.h` 해시 사용. `ESP_IDF_VERSION` 환경변수는 `6.0` 이어야 esp_wifi_remote Kconfig 가 로드됨(memory `reference-env-gotchas`).

아래는 원래 계획(이력 보존).

## 펌웨어 변경 — 원 계획 (카메라 → 기기 순)

1. sdkconfig: `ESPTOOLPY_FLASHSIZE_16MB`(P4) · `BOOTLOADER_APP_ROLLBACK_ENABLE=y` · `APP_PROJECT_VER_FROM_CONFIG=y` + `APP_PROJECT_VER="<APP_FIRMWARE_VER>"` · `ESP_HTTPS_OTA_ALLOW_HTTP` 꺼짐 유지.
2. `partitions.csv` 위 안으로 교체.
3. `main/app_ota.c` (신규): `ota_schedule(job)` — NVS `terra-cam/ota_job`(job_id, version, size, sha256) 저장 → `app_sys_restart("ota")`. `ota_run_if_pending()` — 부팅 초기(WiFi 연결 직후, 카메라 init 전) 호출. `esp_https_ota_begin` → `perform` 루프(10% 마다 ack 발행) → `esp_https_ota_finish` → NVS 플래그 삭제 → `app_sys_restart("ota_apply")`. 실패 시 플래그 삭제 + 에러 ack + 정상 부팅 계속.
4. `app_mqtt.c handle_command`: `ota_update` 분기 — 녹화·업로드·라이브 진행 중이면 `result:"busy"`(서버가 재시도), 아니면 ack `ok` + `ota_schedule`.
5. 새 버전 첫 부팅: `esp_ota_get_state_partition == PENDING_VERIFY` 이면 MQTT 연결 + heartbeat 1회 성공 시 `esp_ota_mark_app_valid_cancel_rollback`, 5분 내 미달 시 `esp_ota_mark_app_invalid_rollback_and_reboot`. heartbeat 에 `ota:{"state":"verified"|"pending"}` 1회.
6. 기기 nano: `command_dispatch.c` 에 같은 분기, heartbeat 에 `fw` 추가(`payload[768]` 여유 확인), NVS 네임스페이스 `terra`. supermini 트리에는 넣지 않음.
7. ELF 를 `firmware_ver` 이름으로 보관(IMPROVEMENTS §15 요구와 동일) — `upload_firmware.py` 가 `.elf` 도 R2 `firmware/.../x.elf` 에 올려 두면 코어덤프 해석까지 연결.

## 완료 조건

- [x] dev cam 1대: 물리 리플래시(OTA 레이아웃) 후 **재페어링 없이** 기존 camera_id 로 heartbeat 복귀 (NVS 보존 검증) — **2026-10-07 p4cam-06461c21**, 0.3.0 → 0.3.3 모두 `idf.py flash`/`write-flash 0x20000` 만으로 같은 camera_id·WiFi·회전 유지
- [x] 콘솔에서 릴리스 선택 → 카메라 다운로드 → 재부팅 → `firmware_ver` 갱신 → job `verified`. 전 과정 **케이블 없이** — **2026-10-07** job `85ffadaf…` 0.3.3 → 0.3.4: accepted → downloading(1.86MB 약 10초) → ready → applying → verified(apply 후 32초). 1차(0.3.0 → 0.3.2, job `79d37c3d…`)는 prepare 가 main_task 스택 초과로 패닉 → `failed(max_tries)` 로 정상 실패 처리됨(0.3.3 에서 수정, 펌웨어 docs/OTA.md 실기 기록)
- [ ] 롤백: MQTT 비밀번호를 틀리게 빌드한 "불량 릴리스" 배포 → 5분 내 자동 복귀 → job `rolled_back`, heartbeat `fw` 는 이전 버전
- [ ] 다운로드 중 WiFi 차단 → `failed` 기록 후 구버전으로 정상 부팅 (벽돌 없음)
- [ ] 라이브 중 `ota_update` → `busy` 거절 → 서버 job `failed(busy)` 로 표시
- [ ] 구 펌웨어(0.2.1)에 `ota_update` → `unknown_action` → job `failed(old_firmware)`
- [ ] 기기 nano 1대 동일 시나리오(다운로드·verified·롤백)
- [ ] nano 앱 바이너리 ≤ 1.7MB (슬롯 1920K 대비 여유). 빌드 스크립트가 초과 시 실패
- [ ] 베타 18대 리플래시 계획에 OTA 레이아웃 포함 → 이후 1회 OTA 로 전체 갱신 성공, `cameras.firmware_ver` 로 집계
- [ ] 문서 4종 갱신 + pytest(핸들러 ack/telemetry job 전이, 라우터 권한, 토큰 타입 교차 거절)

## 착수 전 확인 (owner / 실기)

- [x] nano 실제 플래시 크기 — 4MB 확정(ESP32-S3FH4R2, 사양표). 슬롯 2×1920K 로 고정, 앱 1.3MB → 여유 600K. 앱이 1.8MB 를 넘기 시작하면 OTA 불가해지므로 **nano 앱 크기 상한 1.7MB** 를 완료 조건에 추가
- [x] 카메라 `handle_command` 수신 버퍼 크기 — 실기에서 `ota_prepare`(url+sha256+size 포함) 수신·파싱 정상 (2026-10-07)
- [x] 바닥 빌드 = OTA + 검증된 0.2.1 만, §15 미검증 항목은 첫 OTA 로 — **승인 (2026-10-07)**
- [ ] 베타 회수/방문 일정 — 이 1회가 마지막 물리 접근이 되도록 (**미정**, owner)
- [x] 다운로드 프록시 vs R2 직접 — **프록시 승인 (2026-10-07)**
- [x] 2단계 OTA(`ota_prepare` / `ota_apply`) — **승인 (2026-10-07)**

## 리스크와 대응 (2026-10-06)

심각도: 🔴 벽돌/재방문 유발 · 🟠 배포 실패·오판 · 🟡 운영 불편

### A. 전환 리플래시 (마지막 물리 접근)

| # | 리스크 | 근거 | 대응 |
|---|---|---|---|
| A1 ✅ | ~~P4 부트로더 공간 1.5KB~~ **실측으로 해소(2026-10-06)**. 카메라와 같은 부트로더 설정의 최소 프로젝트로 P4 부트로더 3종 빌드: 현재 설정 **23,552B** / 롤백 켬 **23,680B (+128B)** / 롤백 켬+로그 NONE **16,160B**. 상한 24,576B. 롤백만 켜도 896B 여유, 로그를 줄이면 8.4KB 여유 | 스크래치 `blsize/` 빌드 로그 | 롤백 켜고 `LOG_LEVEL` 은 INFO 유지. 이후 부트로더 옵션을 더 켤 일이 생기면 그때 WARN/NONE. 파티션 테이블 오프셋(0x8000)·NVS(0x9000) 불변 확정 |
| A2 🔴 | 리플래시 때 NVS 소실 → 현장에서 재페어링 | `erase-flash`, 또는 Kconfig 에 다른 카메라 creds 가 채워진 채 플래시(BETA_ONBOARDING §3-2 경고) | 절차서에 `idf.py flash` 만(erase 금지), Kconfig creds 비움 체크. dev cam 으로 "리플래시 후 같은 camera_id 로 heartbeat 복귀" 를 **첫 완료 조건**으로 |
| A3 🟠 | "바닥(floor)" 빌드가 OTA 자체에 버그가 있으면 다시 물리 접근 | 롤백은 구버전으로 돌아갈 뿐, 구버전 OTA 가 망가져 있으면 끝 | **바닥 빌드는 OTA + 이미 실기 검증된 0.2.1 만.** §15 의 미검증 항목(DNS 폴백·last_err.detail·코어덤프)은 바닥에 넣지 않고 **첫 OTA 로 배포** — 그게 OTA 의 첫 실전 검증이기도 함. 바닥은 dev cam + A/B 에서 최소 3일 운용 후 전체 |
| A4 🟠 | esp-hosted(C6 slave 2.12.8) 와 P4 앱 버전 불일치 → WiFi 자체 불가 | `esp_wifi_remote ==1.6.1` 고정이지만 IDF/컴포넌트 락 재해결 시 올라갈 수 있음 | 락 파일 고정 유지. 릴리스 스크립트가 `dependencies.lock` 의 esp_hosted 버전을 릴리스 메타에 기록, 바뀌면 경고. 바뀌어야 하면 C6 OTA(`c6-ota-work/host_ota`)를 별 스테이지로 |

### B. 다운로드

| # | 리스크 | 근거 | 대응 |
|---|---|---|---|
| B1 🟠 | 내부 RAM 부족으로 TLS/OTA 버퍼 실패 | **2026-10-06 운영 실측(하트비트 `sys`)**: 온라인 카메라 내부 RAM 여유 60~128KB, 최대 블록 31~42KB (총 내부 RAM 의 85~90% 사용). PSRAM 은 18~21MB 여유(32MB 중) → 넉넉. 설정상 mbedTLS 버퍼는 PSRAM(`MBEDTLS_EXTERNAL_MEM_ALLOC=y`), 16KB 미만 할당은 전부 내부(`SPIRAM_MALLOC_ALWAYSINTERNAL=16384`) | 재부팅 직후·카메라 파이프라인 전 다운로드(결정 2 — 그 시점엔 ISP/H.264/클립 버퍼가 아직 없어 내부 RAM 이 가장 넉넉). OTA 태스크 스택은 PSRAM(`xTaskCreateWithCaps`, `ALLOW_STACK_EXTERNAL_MEMORY` 이미 on). `esp_https_ota` 버퍼는 KB 단위라 31KB 블록으로 충분. OTA 시작·완료 시 `int_free/int_largest` 로그. `ALWAYSINTERNAL` 16K→4K 하향은 내부 RAM 을 크게 풀어 주지만 전역 영향이라 **바닥 빌드에 넣지 않고** 별도 실험 |
| B2 🔴 | 다운로드 실패 → 재부팅 → 다시 시도 → 실패… **부팅 루프** | NVS `ota_job` 플래그가 남으면 매 부팅마다 시도 | 시도 횟수를 NVS 에 함께 저장, **최대 2회** 후 플래그 삭제·`failed` 보고·정상 부팅. 시도 시작 시점에 카운터 증가(전원 차단 대비) |
| B3 🟠 | 플래시 쓰기 중 캐시 정지로 SDIO(C6) 프레임 유실 → WiFi 끊김 → 다운로드 실패 | P4 는 XIP, 플래시 erase/write 중 캐시 비활성. 4MB 슬롯 전체 erase 는 수 초 | `esp_https_ota` 에 `image_size` 전달(필요 영역만 erase). 실측으로 끊김 여부 확인, 끊기면 청크 사이 `vTaskDelay` |
| B4 🟡 | 서버 프록시 스레드 고갈 | `def` 스트리밍은 스레드풀(기본 40) 점유 | 동시 job 1개 정책(결정 6) + 다운로드 타임아웃 60초 |
| B5 🟠 | 기기(nano)용 **Device Token 검증기가 서버에 없음** | `auth_camera.py` 만 존재. `devices.token_hash` 는 MQTT 비밀번호 해시 | `auth_device.py` 신규(카메라와 동일 bcrypt 검증). MQTT 자격증명을 HTTPS Bearer 로 재사용하는 것은 의도된 결정으로 문서화 |
| B6 🟡 | 사용자 눈에 2~3분 "먹통"(라이브 안 됨), 그 사이 모션 손실 | 앱에 OTA 상태 노출 없음 | v1 은 야간 시간대 수동 배포. 앱 노출은 Out. `cameras` Realtime 에 `ota_state` 한 컬럼 추가는 후속 후보 |

### C. 적용·검증·롤백

| # | 리스크 | 근거 | 대응 |
|---|---|---|---|
| C1 🟠 | **정상 빌드를 불량으로 오판해 롤백.** PENDING_VERIFY 상태에서 앱이 어떤 이유로든 재부팅하면 부트로더가 즉시 롤백한다. 공유기 순단 → `net_wd`(90초) 재부팅 → 롤백 | 자가 재부팅 경로 9종(IMPROVEMENTS §13) 전부 해당 | "정상" 판정을 **MQTT CONNACK + heartbeat 1회** 로 최대한 빨리(실측 12초). 판정 전까지 `net_wd` 등 자가 재부팅은 **지연**(판정 또는 5분 타임아웃까지). 롤백되면 서버가 `rolled_back` 으로 표시하고 운영자가 재시도 |
| C2 🟠 | 반대로 판정이 일러서 **카메라 파이프라인이 죽는 빌드를 정상 확정** | MQTT 는 되는데 인코더·CSI 가 안 되는 빌드 | 받아들임. 그 뒤는 기존 워치독(`enc_stall`/`cam_stall`)과 **다음 OTA** 가 담당 — 그게 OTA 의 존재 이유. 단 heartbeat 에 `clips` 블록이 있어야 "카메라 init 통과" 로 보고, 서버 `verified` 조건에 포함 |
| C3 🔴 | 롤백 타이머가 WiFi 에 의존하면, WiFi 가 죽는 빌드에서 영원히 PENDING | — | 타이머는 부팅 직후 무조건 시작. 5분 뒤 `esp_ota_mark_app_invalid_rollback_and_reboot` |
| C4 🟠 | NVS 스키마 비호환: 신버전이 바꾼 NVS 를 구버전이 읽음(롤백 뒤) | §16 `rotbase` 마커 + ROI 1회 변환이 정확히 이 케이스. 롤백 창 동안 ROI 가 뒤집힘 | 규칙: **NVS 변경은 추가만, 파괴·의미 변경 금지.** 의미 변경이 불가피하면 새 키로 쓰고 구 키 유지 |
| C5 🟡 | `esp_reset_reason()` 이 P4+IDF 6.0.1 에서 SW 재부팅을 WDT 로 보고(§16) → 서버의 롤백 분류가 흔들림 | — | 부트로더 롤백은 reset reason 과 무관(otadata 상태만 봄). 서버 분류는 **heartbeat `fw` 문자열 + uptime 감소** 로만 |
| C6 🟠 | 잘못된 타겟(카메라 bin → nano) 배포 | 운영 실수 | 서버: `release.target` 과 `job.kind` 불일치 거절. 펌웨어: 이미지 헤더 `chip_id` 검증(`esp_https_ota` 기본) + `esp_app_desc.project_name` 비교 |
| C7 🟠 | 버전 문자열 불일치로 `verified` 가 영원히 안 됨, 또는 **다른 빌드가 같은 버전 문자열**을 씀 | `APP_FIRMWARE_VER` 와 `esp_app_desc.version` 이 다른 경로. 실제로 10/1 errlog 빌드가 9/28 빌드와 같은 `0.2.0-20260928` 을 달고 배포돼 서버에서 구분 불가(2026-10-06 DB 확인) | `APP_PROJECT_VER_FROM_CONFIG` 로 한 소스. 업로드 스크립트가 bin 의 app_desc 를 파싱해 입력 버전과 대조, 다르면 거절 |

### D. 보안·서버·운영

| # | 리스크 | 근거 | 대응 |
|---|---|---|---|
| D1 🟠 | **바이너리에 비밀값 포함.** Kconfig 기본값에 `"admin"`/`"ChangeMe!StrongPass2026"` 류가 있고, 개발용 creds 를 채운 채 빌드할 수 있음. **실측(2026-10-07)**: 현재 카메라 빌드(`4cff056-dirty`)에 `ChangeMe!StrongP…` 가 그대로 들어 있어 `upload_firmware.py` 가 거절함 — 펌웨어에서 해당 Kconfig 기본값을 비우거나 릴리스 빌드에서 제거해야 등록 가능 | `main/Kconfig.projbuild` 기본값, BETA_ONBOARDING 경고 | 업로드 스크립트가 `strings` 로 알려진 패턴(토큰 prefix, WiFi 비번, 기본 비번) 검사 후 거절. 릴리스 빌드는 creds Kconfig 전부 빈 값 |
| D2 🟠 | 서명 없음: 서버 침해 시 임의 펌웨어 주입 | 결정 7 | 명령의 `sha256` 을 펌웨어가 기록 후 검증(파티션 범위 해시). Secure Boot v2 / 앱 서명은 다음 스테이지. R2 버킷 쓰기 권한은 서버 키만 |
| D3 🟡 | ack 중복 처리: 기기는 msg_id = `commands.id` 라 진행률 ack 가 같은 msg_id 로 여러 번 옴 → `commands` 행을 재갱신 | `handle_ack` 현재 구조 | `state.ota` 가 있는 ack 는 `ota_jobs` 로만 라우팅, `commands` 는 첫 ack 만 |
| D4 🟡 | nano/supermini 트리 분기 → 이후 공통 수정 diff 에 OTA 노이즈 | supermini 제외 결정 | memory `project-firmware-trees-synced` 에 예외 기록 완료. `diff` 시 `app_ota.c`·`partitions.csv` 제외 |
| D5 🟡 | ELF 미보관 → 코어덤프 해석 불가 | §15 | 업로드 스크립트가 `.elf` 도 함께 R2 에 |
| D6 🟡 | 리플래시 방문 일정과 C6 회수 계획 충돌 | C6_BLE_RECOVERY "회수 때 OTA 를 같이" | 같은 방문에서 처리. 방문 전 바닥 빌드가 A3 기준을 통과해야 함 |

### 리스크 저감 계획 (2026-10-06 추가 — 설계에 반영할 것)

**① 2단계 OTA: `ota_prepare` 와 `ota_apply` 분리** (B1·B3·B6·C1 완화, 가장 효과 큼)
- `ota_prepare`: 다운로드 → ota_1 기록 → sha256 검증까지만. **부팅 파티션은 바꾸지 않음.** 실패해도 아무 일도 안 일어난다.
- `ota_apply`: 서버가 prepare 성공을 확인한 뒤 보내는 "전환 + 재부팅". 조용한 시간대에 몰아서.
- 효과: 18대 전체에 prepare 만 먼저 보내 **다운로드 경로를 무위험으로 검증**(집집마다 회선·RAM 상태 확인). 적용 시점을 운영자가 고를 수 있어 사용자 체감 다운타임이 재부팅 30초로 줄고, 실패한 집만 골라 재시도. 스펙 §아키텍처의 "예약 후 재부팅 → 부팅 시 다운로드" 는 prepare 의 구현 방식으로 남긴다(RAM 가장 넉넉한 시점).

**② NVS 백업·복원 절차** (A2 를 "재방문" 에서 "복구 가능" 으로)
- 리플래시 스크립트 `scripts/flash_ota_layout.sh`: ① `esptool read_flash 0x9000 0x6000 nvs_<camera_id>.bin` 백업 → ② `erase_flash` 금지, bootloader+partition-table+app(ota_0) 만 `write_flash` → ③ NVS 영역 재읽기 → 백업과 해시 비교 → ④ 부팅 후 같은 camera_id heartbeat 확인. 불일치면 `write_flash 0x9000 nvs_<camera_id>.bin` 으로 복원.
- 스크립트가 Kconfig creds 비어 있음·`APP_FIRMWARE_VER` 유일성·바이너리 비밀값 스캔을 플래시 전에 강제.

**③ 서버 사전 점검 게이트** (실패 확률 자체를 낮춤)
- `POST /{..}/ota` 가 거절하는 조건: 오프라인, `rssi < -75`, `int_largest < 40KB`, SD fault 래치 중, 녹화·업로드·라이브 진행 중, `uptime < 5분`, 진행 중 job 있음, `release.target ≠ job.kind`. 거절 사유를 응답에.

**④ 불량 빌드 3종 롤백 매트릭스** (베타 전 실험실에서)
- (a) 부팅 안 됨(이미지 손상) → 부트로더가 즉시 구버전. (b) 부팅되나 WiFi 불가 → 5분 타이머 롤백(C3). (c) WiFi 되나 MQTT 인증 실패 → 판정 실패 롤백(C1). 세 경우 모두 서버 job 이 `rolled_back` 으로 끝나는지.

**⑤ 버전 문자열 자동 생성** (C7 근본 해결)
- `APP_FIRMWARE_VER` 를 손으로 쓰지 않고 CMake 가 `git describe --always --dirty` + 빌드 날짜로 생성. 같은 문자열의 다른 빌드가 원천적으로 불가. 업로드 스크립트는 `-dirty` 빌드를 릴리스로 거절.

**⑥ 판정 창 보호** (C1)
- PENDING_VERIFY 동안 `net_wd`·`upload_stuck`·`enc_stall` 등 자가 재부팅을 **지연**(판정 또는 5분까지). 판정 조건에 heartbeat `clips` 블록 포함(카메라 init 통과 확인).

**⑦ 단계적 배포 + 숙성 시간**
- dev cam 3일 → A/B 3일 → 5대 → 나머지. 각 단계에서 `rolled_back`/`failed` 0건이어야 다음 단계.
- 적용 시간대는 카메라별 클립 이력에서 모션이 가장 적은 시간으로.

**⑧ 물리 복구 경로 문서화 + 예비 1대**
- UART 리플래시 절차(C6_BLE_RECOVERY.md 의 포트 확인·복구 절차 재사용)를 운영 문서에. 예비 카메라 1대를 항상 바닥 빌드 상태로 보관해 현장 교체 가능하게.

**⑨ 관측·경보**
- `ota_jobs` 상태 전이를 Slack 경보(`failed`/`rolled_back`/`timeout` 즉시). 릴리스마다 ELF 보관. 릴리스 메타에 `dependencies.lock` 의 esp_hosted 버전 기록, 바뀌면 경고.

### 먼저 할 실험 (리스크 순)

1. ~~**A1** 롤백 켠 부트로더 크기~~ ✅ 23,680B / 24,576B (2026-10-06 실측).
2. **A2** dev cam 리플래시(②절차) → NVS 해시 일치 + 재페어링 없이 복귀.
3. **④** 불량 빌드 3종 → 롤백 매트릭스 통과.
4. **①** `ota_prepare` 만 dev cam 에 → 다운로드 중 `int_largest`·MQTT 끊김 로그(B1·B3) → 이어서 `ota_apply`.
5. **B2** 다운로드 중 WiFi 차단 → 2회 후 포기, 부팅 루프 없음.

## 학습 노트 (왜 이렇게?)

- **OTA 슬롯이 2개인 이유**: 실행 중인 파티션에는 쓸 수 없다. 새 이미지를 빈 슬롯에 받아 두고 부트로더에 "다음엔 저쪽" 이라고 표시(`otadata`)한 뒤 재부팅한다. 그래서 앱 크기의 2배 + 8KB 가 필요하다.
- **롤백이 공짜인 이유**: ESP-IDF 부트로더가 새 슬롯을 `PENDING_VERIFY` 로 켜 주고, 앱이 "나 정상" 이라고 표시하지 않은 채 재부팅되면 자동으로 이전 슬롯으로 돌아간다. 우리가 할 일은 "정상" 의 기준(MQTT 연결)을 정하는 것뿐.
- **NVS 를 살리는 조건**: 파티션 테이블에서 nvs 의 offset/size 가 같고, 그 영역을 지우지 않으면 된다. 테이블 자체는 0x8000 에 있어서 nvs(0x9000) 와 겹치지 않는다.
- **esp_https_ota 가 하는 일**: HTTP(S) 로 받으며 이미지 헤더 검증 → 빈 슬롯에 순차 기록 → 끝나면 `esp_ota_set_boot_partition`. 진행률은 `esp_https_ota_get_image_len_read` 로 읽는다.

## 관련

- 펌웨어 근거: `firebeetle2-p4-yr030/docs/IMPROVEMENTS.md` §14~§17, `docs/C6_BLE_RECOVERY.md` "남은 위험과 다음 단계"
- 서버 재사용: `backend/mqtt/camera_commands.py`(명령 빌더), `routers/cameras.py reboot_camera`(best-effort 발행 패턴), `auth_camera.get_authed_camera`, `r2_client.generate_presigned_get_url`, `handlers.py:703`(heartbeat fw → firmware_ver)
- 함정: R2 anycast 블랙홀(memory `project_r2_anycast_blackhole_2026-09-29`). 두 펌웨어 트리 공통 로직 동일 유지(memory `project_firmware_trees_synced`)는 이 스테이지에서 supermini 제외로 **예외**
