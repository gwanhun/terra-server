# Stage S — 온도 보정 분리: 공장 기본(-1.5℃, 서버만 앎) + 사용자 보정(앱이 보는 값)

> 상태: ✅ **서버 배포 완료 2026-11-05 12:59 UTC (main ce61eca, PR #49·#50)**. 실기 3항목 통과(아래), 앱팀 "배포 완료" 알림 → 신 앱 0.174.6 스위치 켬 대기. 서버만 수정, 펌웨어 변경 없음. 구현: 이관훈(Claude).
> 사용자 요청: "기본 온도보정 -1.5 는 앱에 안 보이게. 앱이 하는 보정은 기본에 더해져서 기기에 들어가게. 서버가 합산."
> 결정 경로: [docs/REVIEW_TEMP_OFFSET_BASE_2026-11-05.md](../docs/REVIEW_TEMP_OFFSET_BASE_2026-11-05.md)(검토, §5 결정 기록) → 백진우 결정 문서 `TEMP_OFFSET_BASE_DECISION_2026-11-05.md`(D1~D13, 레포 밖) → [docs/TEMP_OFFSET_BASE_DECISION_REPLY_2026-11-05.md](../docs/TEMP_OFFSET_BASE_DECISION_REPLY_2026-11-05.md)(회신, 발송 완료).
> 원래 Stage R 이었으나 PR #48(자동 환기·분무)이 R 을 먼저 써서 S.

## 왜 — 그리고 왜 "호환 방식"인가

- 기기는 보정값을 **하나**만 가진다 (NVS `terra/t_off`, 없으면 빌드 기본 `CONFIG_APP_TEMP_OFFSET_MDEG` = -1.5). `set_temp_offset` 은 덮어쓴다. telemetry 에 지금 적용 중인 **절대값** `temp_offset_c` 를 보고하고 서버가 `devices.temp_offset_c` 에 그대로 둔다 → 앱이 PostgREST/Realtime 으로 읽으면 **-1.5 가 보인다.**
- 11/02 회신 §1.2 는 "앱이 -1.5 를 상수로 들고 합산해라"였고, **앱 0.166.0 부터 그렇게 보내는 버전이 고객 APK(0.171.0·0.174.2)로 나갔다.** 그래서 기존 키 `offset_c`·컬럼 `temp_offset_c` 의 **의미는 못 바꾼다** — 바꾸면 서버 먼저 배포 시 구 앱 "끔(-1.5)"이 기기 -3.0 으로, 앱 먼저 배포 시 신 앱 "끔(0)"이 기기 0 으로 틀어진다.
- 결론: **기존 경로는 그대로 두고 새 키·새 컬럼을 추가한다.** 구 앱이 사라지면 기존 경로 정리(D13, 나중).

## 설계 (한 장)

| 구분 | 지금 | 바꾼 뒤 |
|---|---|---|
| 기기가 갖는 값 / 기기↔서버 MQTT | 절대값 1개 | **변경 없음** |
| `commands.payload.offset_c` | 합산값(기기에 갈 값) | **변경 없음** — 구 앱·콘솔용, 그대로 통과 |
| `commands.payload.user_offset_c` (신규) | — | **사용자 보정값.** dispatcher 가 발행 직전 `+ base` 해서 기기엔 `offset_c` 로 |
| `devices.temp_offset_c` | 기기 보고값(절대) | **변경 없음** |
| `devices.temp_offset_user_c` (신규, `numeric(4,1)`) | — | **사용자 보정값** = 기기 보고값 − base, 0.1 반올림. 미보고(구 펌웨어) NULL |
| -1.5 를 아는 곳 | 펌웨어 빌드 기본 + 앱 상수 | 펌웨어 빌드 기본 + **서버 env `TEMP_OFFSET_BASE_C`**. 신 앱은 상수 제거 |

```
신 앱 ──user_offset_c=+1.0──▶ commands 행(그대로 저장)
                                 │ dispatcher: offset_c = +1.0 + (-1.5) = -0.5, user_offset_c 는 발행 payload 에서 제거
                                 ▼
                              기기 (NVS -0.5) ──telemetry temp_offset_c=-0.5──▶ handlers:
                                 devices.temp_offset_c = -0.5  (그대로)
                                 devices.temp_offset_user_c = -0.5 − (-1.5) = +1.0   ← 같은 UPDATE
구 앱/콘솔 ──offset_c=-0.5──▶ 그대로 발행(변환 없음)
```

### 결정 요약 (D1~D13, 2026-11-05 확정)

| # | 결정 | 내용 |
|:---:|:---:|---|
| D1 | A | base = 서버 env `TEMP_OFFSET_BASE_C`(기본 -1.5). 전 기기 공통. nano·supermini·hub 빌드 기본 모두 -1500 확인. 코드는 `base_c(device_uuid)` 시그니처로 열어 두되 지금은 env 만. **파싱 실패·빈 값 → -1.5 + warning (0 으로 떨어지면 안 됨)** |
| D2 | B | `devices.temp_offset_user_c numeric(4,1)` 추가, `temp_offset_c` 유지. **두 컬럼 같은 UPDATE, 쌍으로만** |
| D3 | A | 합산은 dispatcher 발행 직전. 변환값을 commands 행에 쓰지 않음(mist 분할 선례) |
| D4 | B | 새 키 `user_offset_c`. `offset_c` 는 지금 의미 그대로 통과. **두 키 동시 = 거절** |
| D5 | A | 합산 결과 ±10 밖 → 발행 전 `rejected / offset_out_of_range`. 숫자 아님·bool·null → `rejected / invalid_payload`. **status 와 result 한 UPDATE**(앱 요청: result 없는 rejected 는 앱이 "응답 없음"으로 오인). API 는 400, 메시지의 허용 범위(`-10−base ~ 10−base` = -8.5 ~ +11.5)는 계산값 |
| D6 | 확인 | 신 앱 끔 = `user_offset_c: 0`. 구 앱 끔 = `offset_c: -1.5` 그대로. 11/02 §1.2 는 폐기 아니라 **구 앱 계약으로 유지** |
| D7 | A | ACK 때 확정 안 함. 두 컬럼 모두 telemetry 보고에서만 |
| D8 | 확인 | 구 펌웨어(미보고 3대) 두 컬럼 모두 NULL |
| D9 | 확인 | 과거 commands 이력 그대로(키로 구분됨). 콘솔 `🌡명령` 폴백은 키별로: `user_offset_c` 면 `+base` 해서 기기값으로, `offset_c` 면 그대로 |
| D10 | 확인 | 콘솔은 지금처럼 `offset_c` 로 보내고 `temp_offset_c` 표시 → **서버와 같은 배포 불필요.** 사용자값 괄호 `(사용자 +1.0)` 만 추가 |
| D11 | 확인 | 별도 플래그 없음. `TEMP_OFFSET_BASE_C=0` 은 `user_offset_c` 경로만 항등 — **신 앱 배포 뒤엔 kill switch 아님**(신 앱 사용자 기기가 1.5℃ 높게 읽힘). ENV.md 에 경고 |
| D12 | 확인 | 기기값(`temp_offset_c`, 발행 `offset_c`) 2자리 반올림 유지. 사용자값 컬럼 0.1 단위(앱이 0.1 단위로 저장 완료 비교) |
| D13 | 신규 | 구 앱 소멸 뒤 `offset_c` 경로·`temp_offset_c` 앱 노출 정리. 기준: 앱 최소 지원 버전 + 최근 N일 `offset_c` 앱 명령 0건. 지금 할 일 없음 |

### 앱팀 확정 사실 (근거)
- 보내는 경로: REST `POST /devices/{id}/commands` 만, `commands` 직접 INSERT 없음 → dispatcher 의 `invalid_payload` 는 방어용.
- 읽는 경로: `devices` PostgREST 직접 조회 + 기기 행 Realtime UPDATE(`newRecord['temp_offset_c']`). API 계산 필드는 앱에 안 닿음 → **DB 컬럼이어야 한다.**
- 신 앱: `user_offset_c` 만 전송, 끔 0, 표시·저장 완료 판정은 `temp_offset_user_c`, 앱 상수(`factoryDeviceValue`) 제거, 범위 -8.5 ~ +11.5. **서버 배포 완료 알림 뒤 배포.**
- **앱 구현 완료(2026-11-05 앱팀 회신)**: 0.174.6 main 에 새 계약 반영, **스위치로 꺼 둠** → 지금은 구 경로(`offset_c`·`temp_offset_c`)로 동작. 앱 계약 문서 머리가 "배포 완료" + 커밋 링크로 바뀌면 스위치를 켜고 실기기로 확인. → **서버가 병목.**

## In / Out

**In**
- `migrations/2026-11-05_devices_temp_offset_user.sql` — `ADD COLUMN temp_offset_user_c numeric(4,1)` + COMMENT. **코드 배포 전에 적용**(아래 함정 1).
- `backend/temp_offset.py` (신규, 변환 한 곳): `base_c(device_uuid=None) -> float`(env, 실패 시 -1.5+warning), `to_device(user_c) -> float`(2자리), `to_user(device_c) -> float`(1자리), `user_range() -> (lo, hi)`, `DEVICE_LIMIT_C = 10.0`.
- `backend/mqtt/dispatcher.py`: `_SERVER_ONLY_PAYLOAD_KEYS` 에 `user_offset_c` 추가. `action == "set_temp_offset"` 이고 payload 에 `user_offset_c` 가 있으면: 두 키 동시/숫자 아님 → `rejected/invalid_payload`, 합산 ±10 밖 → `rejected/offset_out_of_range`(둘 다 status·result 한 UPDATE + `_enqueue_failure`, `unsupported_action` 패턴), 정상 → `publish_payload["offset_c"] = to_device(user)`. `offset_c` 만 있으면 지금처럼 그대로.
- `backend/mqtt/handlers.py` `handle_telemetry`: `temp_offset_c` 가 ±10 통과하면 `dev_update` 에 `temp_offset_c` 와 `temp_offset_user_c = to_user(...)` 를 **함께**. `devices.temp_offset_user_c` 가 `_missing_columns` 캐시에 있으면 **둘 다 뺀다**(2차 방어).
- `backend/routers/commands.py` `_validate_generic`: `set_temp_offset` 은 `offset_c` 또는 `user_offset_c` **둘 중 하나**(둘 다·둘 다 없음 → 400). `user_offset_c` 는 숫자 + `to_device()` 가 ±10 안(400 메시지에 `user_range()` 표기).
- `backend/routers/devices.py` `DeviceOut` + `backend/routers/admin.py` `_DEVICE_COLUMNS`: `temp_offset_user_c` 추가(콘솔용. 앱은 PostgREST).
- 콘솔 `web/beta.html`·`web/index.html`: 전송·표시는 그대로(`offset_c`·`temp_offset_c`). 온도 셀/입력칸 옆에 `(사용자 +1.0)` 괄호. `index.html` `🌡명령` 폴백은 키별 처리(D9).
- 문서: `docs/MQTT.md` §1(`temp_offset_user_c` 저장 규칙)·§2(`set_temp_offset` 두 키·서버 합산), `docs/API.md` §4.13(두 키·범위·400), `docs/DATABASE.md` devices 행, `docs/ENV.md`(`TEMP_OFFSET_BASE_C` + D11 경고 + "펌웨어 빌드 기본과 같은 값" 규칙), `docs/BETA_ONBOARDING.md` 🌡보정, `MIGRATIONS_APPLIED.md`.
- 테스트: `tests/test_temp_offset.py`(신규), `test_mqtt_dispatcher.py`(합산·제거·거절 2종·offset_c 통과), `test_mqtt_handlers.py`(쌍 기록·캐시 때 둘 다 제외·구 펌웨어 NULL), `test_commands_api.py`(두 키·범위), `test_migration_coverage.py`(새 컬럼은 마이그레이션 파일에 있으면 자동 통과). **기존 기대값은 그대로.**

**Out**
- 펌웨어 변경 — 없음.
- 기기별 base(`device_calibration`) — 보드 달라질 때. 시그니처만 열어둠.
- ACK 확정, 과거 이력 재해석, 구 앱 경로 정리(D13) — 안 함.
- 앱 계약 문서 `docs/APP_TEMP_OFFSET_SERVER_BASE_2026-11-05.md` — 백진우 작성·전달(초안 완료). 이 레포에 사본을 둘지는 그쪽 결정.

## 함정 (구현·배포 때 반드시)

1. **마이그레이션이 코드보다 먼저.** `_write_heartbeat` 는 컬럼 누락(PGRST204/42703) 시 **그 컬럼만 빼고 즉시 재시도**한다 → 새 컬럼이 없으면 `temp_offset_c` 만 갱신돼 Realtime 에 두 값이 어긋난 이벤트가 나간다. 코드 쪽 쌍 처리(캐시 확인)는 첫 실패 1회를 못 막는 2차 방어.
2. **`TEMP_OFFSET_BASE_C` 는 펌웨어 빌드 기본과 같은 값.** 다르면 flash erase 뒤 기기가 보고한 -1.5 가 사용자값 0 이 아닌 다른 값으로 기록된다. 펌웨어 기본값을 바꾸면 env 도 함께.
3. **env 비어 있음/오타 → -1.5.** 0 으로 떨어지면 신 앱 사용자 기기가 1.5℃ 높게 읽혀 v4 29℃ 경고가 잘못 나간다.
4. **배포 완료 알림은 실기 4항목 뒤에.** 먼저 알리면 신 앱이 없는 컬럼을 조회하고 명령은 400.
5. 신 펌웨어 범위는 기기 절대값 ±10 → 사용자값 허용 -8.5 ~ +11.5. 앱 화면 범위(±4.0)는 안.

## 배포 순서

1. 마이그레이션 운영 적용 → `MIGRATIONS_APPLIED.md` 기록 (사전: 컬럼 없음 확인 / 사후: 타입 `numeric(4,1)`, NULL 허용, 기존 행 전부 NULL)
2. 서버 PR 머지 → Lightsail `git pull` → `.env` 에 `TEMP_OFFSET_BASE_C=-1.5` 명시 → **terra-api·terra-bridge 둘 다 재시작**
3. 실기 4항목
   - 구경로: 콘솔 `offset_c: -0.5` → 기기 -0.5, `temp_offset_c = -0.5`, `temp_offset_user_c = 1.0`
   - 신경로: API `user_offset_c: 1.0` → 기기 -0.5, LCD 온도 1.0 상승. `user_offset_c: 0` → 기기 -1.5, 사용자값 0.0
   - 거절: `user_offset_c: 12` → 400 / 두 키 동시 → 400 / (dispatcher 경로는 테스트로)
   - Realtime: 앱 구독에 두 컬럼이 같은 이벤트로 오는지
4. 앱 계약 문서 머리에 `배포 완료 2026-11-XX` 추가 → 앱팀 알림(이관훈) → 신 앱 배포

## 완료 조건

- [x] 마이그레이션 SQL 작성 `migrations/2026-11-05_devices_temp_offset_user.sql` (2026-11-05) → [x] 운영 적용·`MIGRATIONS_APPLIED.md` 기록 (2026-11-05, 이관훈 — 사후 count 0 확인)
- [x] `backend/temp_offset.py` + 테스트 (`to_device(1.0) == -0.5`, `to_user(-0.5) == 1.0`, `to_user(-1.5) == 0.0`, env 미설정/오타 → -1.5 + warning, `user_range() == (-8.5, 11.5)`)
- [x] dispatcher: `user_offset_c: 1.0` → publish `offset_c=-0.5`, payload 에 `user_offset_c` 없음, DB 행 그대로 / `offset_c: -0.5` → 그대로 통과 / `user_offset_c: 12` → `rejected offset_out_of_range` publish 0회, status·result 한 UPDATE / 두 키·문자열 → `invalid_payload`
- [x] handlers: `temp_offset_c -0.5` → `{temp_offset_c: -0.5, temp_offset_user_c: 1.0}` 한 UPDATE / `-1.5` → `0.0` / 42 → 둘 다 없음 / 캐시에 새 컬럼 누락 → 둘 다 없음 / 키 없음(구 펌웨어) → 둘 다 없음
- [x] API: `user_offset_c: 12` → 400(범위 -8.5~11.5 표기) / 두 키 → 400 / 둘 다 없음 → 400 / `offset_c: -1.5` → 201(기존 테스트 그대로)
- [x] `DeviceOut`·admin 응답 `temp_offset_user_c`, 콘솔 괄호 표기·`🌡명령` 키별 처리 — 콘솔은 base 를 모르므로 `🌡명령(사용자) +1.0` 으로 구분 표기(D9 의 "+base 기기값" 대신, 2026-11-05)
- [x] 문서 6종 + ENV.md D11 경고
- [x] 배포 (2026-11-05 12:59 UTC, `.env` `TEMP_OFFSET_BASE_C=-1.5`, api·bridge 재시작, health 200, 에러 0)
- [x] 실기 — 테스트 기기 `terra-d9feaaa2`(1.2.1), 2026-11-05 13:00 UTC, 기기는 기본값으로 복귀시킴
  - 신경로 `user_offset_c: 1.0` → acked ok, `devices` (-0.5, 1.0) ✅
  - 구경로 `offset_c: -2.0` → acked ok, (-2.0, -0.5) ✅ (변환 없이 통과)
  - 신경로 끔 `user_offset_c: 0` → acked ok, (-1.5, 0.0) ✅
  - 범위 밖 `user_offset_c: 12` → 발행 전 `rejected / offset_out_of_range` (status·result 한 UPDATE) ✅
  - 배포 직후 온라인 10대 전부 쌍 기록 확인(사용자값 = 기기값 + 1.5)
  - 미확인(앱 몫): Realtime 한 이벤트에 두 컬럼 — 신 앱 스위치 켤 때 앱팀이 확인 / LCD 온도 변화 육안
- [ ] 앱 계약 문서 머리 "배포 완료 2026-11-05 + ce61eca" → 앱팀 알림 (이관훈)

## 학습 노트

- "앱에 안 보이게" 는 컬럼을 숨기는 게 아니라 **저장하지 않거나 별도 컬럼으로 분리**하는 것. Supabase 컬럼 권한은 Realtime 이 무시하고 PostgREST `*` 도 깨질 수 있다.
- 변환은 **경계 한 곳**(브리지의 발행·수신)에서만. 앱·API·콘솔이 각자 더하고 빼면 이중 합산이 생긴다.
- **계약을 뒤집기 전에 앱 배포 상태를 먼저 확인.** 11/02 계약이 3일 만에 바뀌는 동안 앱이 고객에게 나갔고, 그래서 "의미 변경" 안이 "호환 추가" 안으로 바뀌었다. 외부 팀에 보낸 계약은 보낸 순간 배포됐다고 가정할 것.
