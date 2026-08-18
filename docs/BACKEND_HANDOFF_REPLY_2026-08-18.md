# 백엔드/펌웨어 회신 — 카메라 키프레임 + 제어 계약 보강 (2026-08-18)

> **회신 대상**: 앱(Flutter) `backend-handoff-2026-08-18-webrtc-keyframe-and-contracts.md`
> **작성**: terra-server 백엔드 · P4 카메라 펌웨어 담당
> **성격**: 코드/스키마 실측 대조 + 구현. §2~§6 은 **구현·테스트 완료**(커밋/마이그레이션 포함), §1 은 컴포넌트 제약으로 결정 대기.

---

## 0. 요약

| # | 항목 | 판정 | 담당 | 상태 |
|---|---|---|---|---|
| §1 | 카메라 첫 프레임 18초 | 진단 정확. **esp_video 2.2.0 에 force-IDR 미지원** | P4 카메라 FW | ⛔ 결정 대기 (아래 §1) |
| §2 | `devices` 보드 타입 | 컬럼 없음 → `capabilities` JSONB 추가 | 백엔드 | ✅ 완료 |
| §3 | 구간 예약 서버 가드 | **B안 `pair_id`** 채택(짝 cascade 삭제) | 백엔드 | ✅ 완료 |
| §4 | `telemetry.led` 상태 | led 컬럼 + 펌웨어 publish | 백엔드 + nano FW | ✅ 완료 |
| §5 | `device_settings` setpoint | REST `/settings` + 단일 target 추가 | 백엔드 | ✅ 완료 |
| §6 | 예약 `*_toggle` 제거 | 화이트리스트 제거 + 400 | 백엔드 | ✅ 완료 |

> **마이그레이션 적용 필요**(Supabase SQL 에디터): `2026-08-18_devices_capabilities.sql`,
> `2026-08-18_device_settings_setpoint.sql`, `2026-08-18_schedules_pair_id.sql`,
> `2026-08-18_telemetry_led.sql`. 서버 배포는 `git push` → `uv sync` → `terra-api` 재시작.
> 펌웨어(§4)는 nano/relay 두 보드 빌드·플래시 필요.

---

## 0-1. 구현 완료 내역 (§2~§6)

| # | 커밋 | 마이그레이션 | 앱이 쓰는 계약 |
|---|---|---|---|
| §6 | `1adeddd` | 없음 | 예약에 `*_toggle` 넣으면 400. (앱은 이미 절대명령만 사용) |
| §2 | `8de77ef` | `2026-08-18_devices_capabilities.sql` | 페어링 시 `capabilities`(JSONB) 보고, `GET /devices`·`/devices/{id}` 응답에 노출. 예: `{"board":"mosfet","led_dimmable":true}` |
| §5 | `e4a5b41` | `2026-08-18_device_settings_setpoint.sql` | `GET/PATCH /devices/{id}/settings` — `target_temp_c`, `target_humidity_pct`, `target_temp_min/max`, `target_humid_min/max`. 서버가 범위(-20~60°C, 0~100%)·min≤max 검증 |
| §3 | `4847060` | `2026-08-18_schedules_pair_id.sql` | 구간 예약 = on/off 2행에 같은 `pair_id`(앱 생성 UUID) 부여. 한쪽 `DELETE` 시 서버가 짝도 삭제. `GET` 응답에 `pair_id` 노출(목록 묶기용) |
| §4 | server `850247b` / nano `bc9b57c` / relay `624bf87` | `2026-08-18_telemetry_led.sql` | telemetry 에 `led`(ON/OFF), MOSFET 은 `led_brightness`(0~100) 포함 |

**앱측 반영 포인트**:
- §2: `Device` 모델에 `capabilities` 추가 → `led_dimmable` 로 밝기 슬라이더 조건부 노출.
- §5: setpoint 하드코딩 제거 → `GET/PATCH /devices/{id}/settings`. 미설정 디바이스는 값이 전부 `null` 인 200 반환(404 아님).
- §3: `addSpan` 을 2행 POST(같은 `pair_id`) 로, 삭제는 한 행만 DELETE 하면 짝도 사라짐. 목록은 `pair_id` 로 구간 한 줄 묶기.
- §4: LED 로컬 추측(`_ledOn`) 제거 → telemetry `led`/`led_brightness` 사용.
- §6: 변경 없음(이미 절대명령만).

---

## §1 — 카메라 첫 프레임 18초 (P4 카메라 펌웨어) — ⛔ 컴포넌트 제약, 결정 대기

**진단은 정확하나, 권장했던 ①②안이 현재 컴포넌트 스택에선 불가함을 확인.**

### 코드 실측
- `on_peer_state()` `CONNECTED` 시 `s_connected=true`만, **키프레임 강제 없음** (`app_webrtc.c:62-64`).
- H.264 인코더 = esp_video **2.2.0** V4L2 HW. GOP = `V4L2_CID_MPEG_VIDEO_H264_I_PERIOD`(=`CONFIG_APP_H264_GOP`, ~30) (`main.c:380`).
- **핵심 발견**: esp_video H.264 드라이버(`esp_video_h264_device.c`)가 처리하는 컨트롤은 **I_PERIOD / MIN_QP / MAX_QP / BITRATE 4개뿐**. `V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME` 은 헤더에 매크로만 있고 **드라이버 미구현** → set 하면 에러(no-op).
- esp_peer 의 `pli_send_interval`(`esp_peer_default.h:34`)은 **수신측이 PLI를 보내는** 설정이라 송신(카메라)엔 무관. 설령 카메라가 PLI를 수신해도 인코더에 force-IDR 수단이 없어 대응 불가.
- **18초 원인**: I_PERIOD 는 30**프레임**인데 라이브 경로 fps 가 낮아 시간상 키프레임 간격이 큼(30 ÷ 저fps ≈ 18초). 클립 경로(`CONFIG_APP_H264_FPS`)는 fps 가 높아 같은 30프레임이 ~3초.

### 결론 — ①②안 불가, 선택지 3가지
| 안 | 내용 | 평가 |
|---|---|---|
| **A. esp_video 업그레이드** | FORCE_KEY_FRAME 을 구현한 상위 버전으로 올려 ①(IDR-on-connect) 구현 | 가장 깔끔. **버전별 지원 여부 확인 필요**(2.2.x 엔 없음). API 회귀 리스크 검토 |
| **B. I_PERIOD 축소** | `CONFIG_APP_H264_GOP` 를 30→~5-8 로. 지원되는 유일한 레버 | 첫 프레임 대폭 단축. 단 **녹화 클립도 인코더 공유** → 클립 크기↑(키프레임 잦음). 썸네일/프리롤은 유지됨(여전히 키프레임 시작). 즉시 가능한 스톱갭 |
| C. 라이브 전용 인코더 분리 | 녹화와 별도 인코더로 라이브만 짧은 GOP | 근본적이나 대규모 리팩터 |

**권장**: 우선 **B(I_PERIOD 축소)** 로 즉시 완화 → 병행해서 **A(esp_video 업그레이드)** 로 FORCE_KEY_FRAME 지원 여부 확인. 하드웨어에서 `firstFrame − connected` 계측하며 GOP 값 튜닝.

**결정 필요**: B의 GOP 목표값(대역폭 vs 지연 vs 클립 크기 트레이드오프), A 업그레이드 착수 여부.

**계약 문구(APP_WEBRTC.md)**: force-IDR 수단 확보(A) 시 "peer 연결 완료 후 1초 이내 IDR 송출" 조항 추가. 그 전까진 B로 "키프레임 간격 단축(best-effort)" 수준.

**검증 기준**: 앱 `[webrtc-timing]` 로그 `firstFrame − connected` 감소치.

---

## §2 — `devices` 보드 타입 (백엔드)

**판정: 컬럼 없음 확인.** `devices`: id / owner_id / device_id / token_hash / name / species / firmware_ver / created_at / updated_at / last_seen_at / is_online (`migrations/2026-05-26_initial_schema.sql:19-31`). 보드 타입/capability 컬럼 없음.

**권장: `capabilities` JSONB.** 앱 제안대로 히터 미장착 보드까지 한 필드로 커버 가능.

```sql
ALTER TABLE public.devices
    ADD COLUMN IF NOT EXISTS capabilities JSONB;
-- 예: {"led_dimmable": true, "heater": false, "board": "mosfet"}
-- 기존 행 백필: 릴레이 보드 기본값으로 UPDATE, 이후 펌웨어가 페어링 시 보고.
```

**필요 작업**: 마이그레이션 → 페어링(`DevicePairRequest`)에서 수신 또는 펌웨어 최초 보고 → `DeviceOut`에 노출. 앱은 `Device` 모델 필드 1개 추가로 밝기 슬라이더 조건부 복원.

---

## §3 — 구간 예약 서버 가드 (백엔드)

**판정: 없음 확인.** `schedules`에 `end_time_of_day`도 `pair_id`도 없다. 현재 on/off는 낱개 2행으로만 저장 → 한쪽만 삭제 시 고아 예약 발생.

**권장: A안 `end_time_of_day`.** 1행=1구간. 목록도 구간 한 줄로 그려지고 삭제/토글이 원자적.

- 마이그레이션: `schedules.end_time_of_day TEXT`(HH:MM, nullable) 추가.
- 러너: `end_time_of_day` 있으면 시작 시각에 `*_on`, 종료 시각에 대응 `*_off` 발행. weekly 자정 넘김이면 off를 하루 밀어 계산.
- POST/PATCH 확장 + `next_run_at`을 시작/종료 각각 관리(또는 파생).
- **중간~큰 작업**(러너 발행 로직이 핵심). §6(off+guard 거부, 이미 완료)과 정합.

---

## §4 — `telemetry.led` 상태 피드백 (백엔드 + nano 펌웨어)

**판정: led 컬럼 없음 확인.** `telemetry`: relay / fan / heater_state / heater_locked (+센서값). led 상태 없음 (`migrations/2026-05-26_initial_schema.sql:74-77`). `acked`는 "수신"이지 "현재 켜짐"이 아니라는 앱 지적 타당.

**필요 작업**:
1. 마이그레이션 `telemetry.led TEXT`(ON/OFF), MOSFET면 `led_brightness SMALLINT`(0~100) 추가. `telemetry_1m` 롤업도 동반 검토.
2. nano 펌웨어 telemetry publish 페이로드에 led 상태 추가.
3. 브리지 telemetry 핸들러 파싱/INSERT 확장.
4. (선택) 명령 `acked`의 `result`에 적용 후 상태 스냅샷(`payload_result`) 포함 → 앱이 다음 텔레메트리(30초) 안 기다리고 즉시 반영.

---

## §5 — `device_settings` setpoint (백엔드)

**판정: 테이블 존재하나 계약 불충분.**

- 테이블 있음 (`migrations/2026-05-26_initial_schema.sql:44-56`): `target_temp_min/max`, `target_humid_min/max`, `alert_temp_high/low`, `alert_humid_low`, `schedule`.
- **문제 1**: 앱이 쓰려는 **단일 목표값 `target_temp_c` / `target_humidity_pct`가 없음** — 지금은 min/max 범위만.
- **문제 2**: **REST `/settings` 라우터 없음.** `alerts.py`가 내부에서 `alert_*`만 캐시 조회할 뿐(`backend/alerts.py:47-63`), 앱이 읽고 쓸 경로가 없다.

**필요 작업 (결정 포함)**:
- 단일 목표값을 **(a) 새 컬럼 `target_temp_c`/`target_humidity_pct` 추가** vs **(b) min/max 중앙값 파생** 중 택1. 유지형 가드(목표 도달 시 OFF)가 단일값을 기대하면 (a) 권장.
- REST `GET/PATCH /devices/{id}/settings` 신규 라우터(서버가 값 범위 검증 후 저장 — RLS만 쓰는 직결보다 안전).
- 초기값: 페어링/등록 시 종(species) care_info로 기본값 채우면 앱과 동일.

---

## §6 — 예약 화이트리스트 `*_toggle` 제거 (백엔드, 방어)

**판정: 4개 다 허용 중.** `SCHEDULABLE_ACTIONS`에 `relay_toggle / fan_toggle / heater_toggle / led_toggle` 포함 (`backend/routers/schedules.py:47-50`).

**권장**: 화이트리스트에서 toggle 4개 제거 → 예약 생성/수정 시 400 거부. §4-3(off+guard 거부, 2026-08-18 커밋 완료)과 동일 패턴, 마이그레이션 불필요. **저비용, 즉시 가능.**

- **선행 확인**: 기존 DB에 toggle 예약이 있으면 앱 목록이 깨지지 않게 처리 필요 → Supabase에서
  `SELECT id, action FROM schedules WHERE action LIKE '%\_toggle';` 조회 요청.

---

## 이미 반영된 항목 (참고)

- **off action + guard 400 거부** (앱 2026-08-14 §4-3): 구현·테스트 완료. `*_off` 예약에 skip 가드 부착 시 400 (생성/수정 양쪽). `{"guard": null}` 해제는 정상 동작 유지.
- nano 물분무(1/2/3s)·팬 타이머(자동 OFF)·LCD 한글 렌더는 커밋 완료(양쪽 보드).

---
항목별 착수 합의 주시면 마이그레이션/라우터/펌웨어 순으로 진행하겠습니다.
