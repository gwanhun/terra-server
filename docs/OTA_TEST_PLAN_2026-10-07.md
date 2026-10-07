# OTA 실기 테스트 절차 (Stage J, 2026-10-07)

> 대상: OTA 레이아웃으로 리플래시한 dev 카메라 1대(+ nano 1대). 스펙·리스크: [specs/stage-j-ota.md](../specs/stage-j-ota.md). 계약: [API.md §4.10](API.md), [MQTT.md §2·§3-b](MQTT.md).
> 각 단계의 "확인" 은 `uv run python scripts/ota_status.py --watch` 화면과 서버 저널(`sudo journalctl -u terra-api -u terra-bridge -f`)로 본다.
> 실패하면 **그 단계에서 멈추고** 결과(로그·ota_status 출력)를 남긴다. 다음 단계로 억지로 넘어가지 않는다.

## 0. 전제 확인 (10분)

| # | 확인 | 방법 | 기대 |
|---|---|---|---|
| 0-1 | 보드가 새 펌웨어로 보고 | `ota_status.py` "OTA 지원 보드" 목록 | 카메라 `fw=fb2-p4 0.3.0-20261007`, `capabilities.ota=true`, 온라인 ● (리플래시 뒤 최대 30초) |
| 0-2 | 재페어링 없이 복귀 | 같은 `camera_id` 로 heartbeat, 콘솔 회전 설정 그대로 | NVS 보존 확인(리스크 A2). 아니면 `write_flash 0x9000 nvs_<id>.bin` 복원 |
| 0-3 | 서버 코드 배포 | 아래 §1 | `GET /firmware/releases` 가 401(인증 필요)로 응답 = 라우터 살아 있음 |
| 0-4 | 마이그레이션 적용 | Supabase SQL Editor 에 `migrations/2026-10-07_firmware_ota.sql` → `MIGRATIONS_APPLIED.md` 행 추가 | `select count(*) from ota_jobs` 가 0 으로 응답 |
| 0-5 | 두 번째 빌드 준비 | §2 | 보드에 올린 것과 **다른 버전** 이 있어야 OTA 를 보낼 수 있다(같은 버전은 409) |

2026-10-07 15:30 기준 DB 에는 아직 `capabilities.ota` 보고 보드가 없다 — 0-1 이 첫 관문.

## 1. 서버 배포 (한 번)

```bash
# 로컬: feat/stage-j-ota 에 main(Stage K 허브) 머지 후 테스트 통과 확인 → main 머지 → push
git checkout main && git merge --no-ff feat/stage-j-ota && git push
# 서버(Lightsail, 사용자 SSH)
cd ~/terra-server && git pull && uv sync
sudo systemctl restart terra-api terra-bridge
sudo journalctl -u terra-bridge -n 20 --no-pager      # "ota monitor 시작" 로그
```

`.env` 에 `API_PUBLIC_BASE_URL` 이 없으면 기본 `https://api.terra-server.uk` — 운영은 그대로 두면 된다.

## 2. 테스트용 릴리스 만들기 (카메라)

보드엔 `0.3.0-20261007` 이 있으니 **`0.3.1`** 을 만든다. 코드는 그대로여도 된다(버전만 다르면 OTA 대상).

```bash
cd ~/project/esp32/firebeetle2-p4-yr030
sed -i '' 's/^CONFIG_APP_PROJECT_VER=.*/CONFIG_APP_PROJECT_VER="fb2-p4 0.3.1-20261007"/' sdkconfig
idf.py build                                         # bin 과 elf 가 새 버전으로
cd ~/project/terra-server
uv run python scripts/upload_firmware.py --target camera_p4 \
    --bin ~/project/esp32/firebeetle2-p4-yr030/build/firebeetle2_p4_yr030.bin \
    --elf ~/project/esp32/firebeetle2-p4-yr030/build/firebeetle2_p4_yr030.elf \
    --notes "OTA 1차 실기(코드 동일, 버전만)"
uv run python scripts/upload_firmware.py --list
```

거절되면(dirty·비밀값·같은 버전) 메시지대로 고친다. `-dirty` 는 펌웨어 레포 작업 트리를 커밋하거나 `--allow-dirty`(테스트 한정).

## 3. 정상 경로: prepare → ready → apply → verified (카메라)

| 단계 | 할 일 | 기대 (ota_status / 저널 / 시리얼) | 시한 |
|---|---|---|---|
| 3-1 | 콘솔 카메라 행 **OTA** → `1` (0.3.1 선택) | 응답 `published=true`, 작업 `pending`. 저널 `ota_prepare 발행 camera=… job=…` | 즉시 |
| 3-2 | 카메라 ack | 작업 `accepted` (저널 `camera ack … result=ok`). 시리얼 `ota_prepare 예약 … 1.5초 뒤 재부팅` → `SW:ota_prepare` | 5초 |
| 3-3 | 재부팅 후 다운로드 | 시리얼 `OTA prepare 1/2: job=…` → `다운로드 10%…100%` → `OTA prepare 완료: … 슬롯 ota_1`. 저널 `firmware download start`. 작업 `downloading` → MQTT 연결 뒤 `ready 100%` | 2분 |
| 3-4 | 카메라 정상 복귀 확인 | heartbeat 재개, `fw` 는 아직 **0.3.0**(전환 전), 라이브·녹화 정상 | — |
| 3-5 | 콘솔 **OTA** 다시 → apply 확인 | 작업 `applying`. 시리얼 `ota_apply: … ota_1 로 전환 → 재부팅` → `SW:ota_apply` | 5초 |
| 3-6 | 새 펌웨어 부팅 | 시리얼 `새 펌웨어 fb2-p4 0.3.1… PENDING_VERIFY — MQTT PUBACK 까지 5분` → MQTT 연결 → `정상 확정 (롤백 취소)` | 30초 |
| 3-7 | 서버 판정 | heartbeat `fw=fb2-p4 0.3.1…` → 작업 **`verified`**. `cameras.firmware_ver` 갱신. heartbeat `ota.state` 가 `pending_verify` → `valid` | 1분 |
| 3-8 | 사후 | 회전·ROI·자격증명 그대로(NVS). 콘솔 OTA 버튼 다시 누르면 "같은 버전" 409 | — |

실패 분기:
- 3-1 에서 409 `precheck failed: …` → 사유대로(offline/weak_wifi/low_internal_ram/just_booted/uploading/live_active/no_ota_capability). 개발 중엔 콘솔 입력에 `1 force`.
- 3-2 에서 `busy` → 녹화·업로드·라이브 중. 끝난 뒤 재시도.
- 3-3 에서 `failed` + error → [펌웨어 docs/OTA.md 실패 사유 표](../../esp32/firebeetle2-p4-yr030/docs/OTA.md). `connect`/`tls` 면 서버 URL·인증서, `http_401` 이면 토큰, `http_409` 면 작업 상태.
- 3-7 에서 10분 무소식 → `timeout`. 시리얼로 어디서 멈췄는지 확인.

### 3-x. 1차 실기 결과 (2026-10-07, p4cam-06461c21, 0.3.0 → 0.3.2)

- 3-1~3-2 통과 (`1 force`, 작업 `accepted`, `SW:ota_prepare` 재부팅).
- 3-3 실패: 다운로드 진입 직후 `Stack protection fault, task "main"` 2회 → `failed(max_tries)` ack → 서버 작업 `failed`. 카메라는 정상 복귀 (boot 파티션 미변경이라 서비스 영향 없음).
- 원인/수정: 펌웨어 0.3.3 — prepare 다운로드를 16KB 전용 태스크로 이동 ([펌웨어 docs/OTA.md 실기 기록](../../esp32/firebeetle2-p4-yr030/docs/OTA.md)).
- 재시도 절차: 보드에 **0.3.3 을 시리얼로** 올리고(`esptool write-flash 0x20000 <0.3.3 bin>` 또는 `idf.py app-flash`, erase 금지), 릴리스 **0.3.4**(코드 동일) 로 §3 을 다시 돈다. 콘솔 OTA 입력 `1` = 목록 첫 번째(최신).

## 4. 롤백 매트릭스 (카메라, 불량 빌드 3종)

각 케이스마다 §2 처럼 버전을 올려 릴리스 등록 → §3 의 3-1~3-5 → 아래 기대. **끝나면 반드시 정상 빌드(0.3.1)가 다시 verified 인지 확인**하고 다음 케이스.

| 케이스 | 불량 빌드 만드는 법 | 기대 |
|---|---|---|
| 4-a 부팅 불가 | 정상 bin 의 끝에서 1KB 앞 바이트 1개를 바꾼 파일을 **그 파일 기준 sha256 으로** 등록(스크립트가 bin 을 읽어 해시하므로 그냥 등록하면 된다). 이미지 체크섬이 깨진다 | prepare 단계에서 `esp_ota_end` 가 `image_invalid` → 작업 `failed(image_invalid)`. apply 까지 못 간다. (부트로더 롤백은 발동할 일 없음 — prepare 가 막음) |
| 4-b WiFi 불가 | `sdkconfig` 의 `CONFIG_APP_WIFI_SSID` 를 틀리게 + NVS 자격증명을 쓰지 않도록… **현실적으로 어려움** → 대신 apply 직후 공유기 전원을 끈다 | PENDING_VERIFY 5분 뒤 시리얼 `5분 안에 MQTT 확인 못 함 → 부트로더 롤백` → 0.3.1 로 부팅 → heartbeat `fw=0.3.1` → 작업 **`rolled_back`** (`prev_version` 일치) |
| 4-c MQTT 인증 실패 | `main/app_mqtt.c` 에서 password 에 `"x"` 를 덧붙인 빌드(`.credentials.authentication.password`) | WiFi 는 붙지만 CONNACK 거절 → PUBACK 없음 → 5분 뒤 롤백 → `rolled_back` |
| 4-d 검증 창 보호 | 4-b 진행 중(PENDING_VERIFY, 공유기 OFF) 시리얼에서 `net_wd` 가 발동해야 할 90초 시점 | `재부팅(net_wd) 보류 — 새 펌웨어 검증 중` 로그만 찍히고 재부팅 안 함. 5분 뒤 롤백 타이머가 처리 |

## 5. 다운로드 중단·재시도 상한

| 케이스 | 방법 | 기대 |
|---|---|---|
| 5-a 다운로드 중 WiFi 차단 | 3-3 `다운로드 30%` 쯤 공유기 OFF → 20초 뒤 ON | 시리얼 `OTA prepare 실패: read` → 5초 뒤 `OTA prepare 2/2` → 성공하면 `ready`, 또 실패하면 `failed`. **부팅 루프 없음**, 카메라는 정상 부팅 |
| 5-b 다운로드 중 전원 차단 | 3-3 중 전원 OFF/ON ×2 | 2회째 부팅에서 `prepare 포기: 시도 2회 초과` → 작업 `failed(max_tries)`. 정상 부팅 |
| 5-c 서버 시한 | 5-b 뒤 서버 | OtaMonitor 가 15분 지나도 ack 없으면 `timeout` (max_tries ack 가 먼저 오면 `failed`) |

## 6. nano 동일 절차

- 릴리스: `upload_firmware.py --target device_nano --bin ~/project/esp32/terra-iot-nano/build/terra-iot.bin --elf …/terra-iot.elf` (버전 `terra-fw 1.1.1-…` 으로 올려서).
- 콘솔 기기 행 **OTA**. 차이: 명령이 `commands` 테이블을 거치므로 콘솔 명령 패널에 `ota_prepare` 행이 `pending → sent → acked` 로 보인다. 정상 판정은 CONNACK + 첫 telemetry(QoS0) 라 3-6 의 "PUBACK" 대신 `MQTT CONNECTED` 직후.
- busy 거절 없음(액추에이터는 부팅 초기 OFF). 분무 중이면 끊긴다 — 테스트 중엔 분무 안 할 때.

## 7. 기록

각 단계 결과를 `specs/stage-j-ota.md` 완료 조건 체크박스에 날짜와 함께 적고, 실패 케이스는 `firebeetle2-p4-yr030/docs/IMPROVEMENTS.md` §18 에 시리얼 로그 요지를 남긴다. 베타 18대 리플래시는 **§3·§4·§5 가 dev cam 에서 전부 통과한 뒤** 스펙의 단계적 배포 순서(dev → A/B → 5대 → 나머지)로.
