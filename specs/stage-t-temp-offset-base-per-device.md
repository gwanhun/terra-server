# Stage T — 기기별 공장 기본 온도 보정(base): 기기가 NVS 에 갖고 보고, 서버는 보드별로 뺀다

> 상태: 🔧 **서버 PR #59 (stage-t/temp-offset-base-per-device, 테스트 1183 통과) · 펌웨어 nano `feat/temp-base` 1.3.0-20261110 빌드 통과 — 2026-11-10**. 마이그레이션 미적용, 미배포, 미플래시, 릴리스 미등록. Stage S([stage-s-temp-offset-base.md](stage-s-temp-offset-base.md)) 의 다음 단계.
> 사용자 요청(2026-11-09): "보드마다 공장 초기 보정값이 -2, -3 으로 다를 수 있고 나중에 다시 바꿀 수도 있다. 그래도 앱에는 사용자가 바꾼 양만(처음엔 0) 보여야 한다."
> 비유: 영점 다이얼 두 개. **안쪽(공장용, 운영자만)** + **바깥(사용자용, 앱이 보는 것)**. 기기 실제 보정 = 둘의 합.

## 왜

- Stage S 는 base 를 서버 env `TEMP_OFFSET_BASE_C` **하나(-1.5)** 로 전 기기 공통 처리한다. 운영자가 어떤 보드를 콘솔에서 -3 으로 보정하면 그 보드 사용자 앱엔 -1.5 가 보인다 — 운영자 보정이 사용자 눈에 새어 나감.
- 보드마다 base 가 다르고 나중에 바뀔 수도 있으므로, base 는 **기기 자신이 NVS 에 갖고 telemetry 로 보고**해야 한다. 빌드 설정(Kconfig)에 두면 보드마다 다른 빌드가 필요하고 OTA 로 바뀌면 사용자값이 튄다 — 채택 안 함.
- 서버 `temp_offset.base_c(device_uuid)` 는 Stage S D1 에서 이미 보드별 분기를 염두에 두고 시그니처를 열어 둠.

## 설계 (한 장)

```
기기 NVS:  terra/t_off  = 적용 절대값(기존)        terra/t_base = 공장 기본(신규, 없으면 Kconfig -1.5)
           실제 보정 = t_off 하나로 적용(기존 그대로). 사용자값은 저장 안 함 — 서버가 t_off − t_base 로 계산.

telemetry: { "temp_offset_c": -2.0, "temp_offset_base_c": -3.0, ... }
            └ handlers: devices.temp_offset_c = -2.0, temp_offset_base_c = -3.0,
                        temp_offset_user_c = -2.0 − (-3.0) = +1.0   (같은 UPDATE, 셋 쌍으로)

명령 1 (사용자, 기존): set_temp_offset {user_offset_c: +1.0}
            └ dispatcher: offset_c = user + base(그 보드, DB) = +1.0 + (-3.0) = -2.0 → 기기 t_off = -2.0

명령 2 (운영자, 신규): set_temp_base {base_c: -3.0}
            └ 기기: t_off = t_off − 옛 t_base + 새 t_base ; t_base = 새 값   (사용자값 불변)
               예) t_off -0.5, t_base -1.5, 사용자 +1 → set_temp_base -3 → t_off -2.0, t_base -3.0, 사용자 여전히 +1
```

### 결정 요약 (T1~T8, 2026-11-09 제안)

| # | 결정 | 내용 |
|---|------|------|
| T1 | base 저장 위치 | 기기 NVS `terra/t_base`(밀리℃ int32). 없으면 Kconfig `CONFIG_APP_TEMP_OFFSET_MDEG` 로 폴백(기존 보드 호환) |
| T2 | base 주입 | MQTT 명령 `set_temp_base {base_c}` 하나. 콘솔 베타 화면에서 운영자가 입력. 시리얼 주입은 안 함(명령이 재보정에도 쓰임) |
| T3 | base 변경 시 적용값 | 기기가 `t_off += 새 base − 옛 base` 로 **같이 옮긴다**. 사용자값이 안 움직이는 게 요구사항의 핵심 |
| T4 | 범위 | base ±10, 옮긴 t_off 도 ±10 안이어야 함. 밖이면 기기가 거절(ACK result `offset_out_of_range`), 둘 다 안 바꿈 |
| T5 | 서버 base 조회 | `base_c(device_uuid)`: devices.temp_offset_base_c(프로세스 캐시) → NULL/미보고면 env -1.5 폴백. 구 펌웨어는 Stage S 와 동일 동작 |
| T6 | 사용자값 계산 | handlers 는 **같은 telemetry 안의** base 로 계산(DB 왕복 없음). base 키 없는 구 펌웨어는 env 폴백 |
| T7 | 앱 | 변경 없음. `user_offset_c` 보내고 `temp_offset_user_c` 읽는 것 그대로. 범위 문구 하드코딩(-8.5~11.5)이 있으면 보드별로 달라질 수 있다고 앱팀에 통지 |
| T8 | 콘솔 기존 절대값 칸 | 유지(디버그용). 옆에 **base 칸** 신설. 보드 보정은 base 칸으로 하라는 안내문 |

## In / Out

### In

**펌웨어 (terra-iot-nano 만, 규칙: nano 수정 → supermini 확인)**
- `main/src/calib.c`: `t_base` NVS 읽기/쓰기, `calib_temp_base_get()`, `calib_temp_base_set(new)` — T3·T4 의 동시 이동 포함. `calib_init` 에서 두 키 모두 로드
- `main/src/command_dispatch.c`(`set_temp_offset` 분기 옆, 413행 부근): `set_temp_base` 분기 추가. payload `base_c` 숫자·범위 검증, ACK 는 기존 형식
- `main/src/mqtt_app.c`(telemetry 조립, 329행 부근): `"temp_offset_base_c":%.2f` 추가
- 버전 범프 + 릴리스 등록(Stage J OTA)

**마이그레이션**
- `migrations/2026-11-XX_devices_temp_offset_base.sql`: `devices.temp_offset_base_c numeric(4,1) null` + 컬럼 코멘트. 적용 뒤 `MIGRATIONS_APPLIED.md` 기록

**서버**
- `backend/temp_offset.py`: `base_c(device_uuid)` 가 캐시 → env 순. 캐시 갱신 함수(handlers 가 base 보고받을 때 호출). `user_range(device_uuid)` 는 그대로 base 따라감
- `backend/mqtt/handlers.py` `handle_telemetry`: `temp_offset_base_c`(±10 통과) 저장 + 캐시 갱신. 사용자값은 payload base 우선(T6). 새 컬럼 누락 시 `_missing_columns` 로 **셋 다 뺀다**(Stage S 함정 1 패턴)
- `backend/mqtt/dispatcher.py`: `set_temp_base` 를 허용 action 에 추가(payload 그대로 통과, 서버 변환 없음). `set_temp_offset` 경로는 이미 `device_uuid` 를 넘기므로 코드 변경 없음
- `backend/routers/commands.py`: `GENERIC_ACTIONS` 에 `set_temp_base` 추가 + `base_c` 검증. **`resolve_device_offset(body.payload, device_uuid)` 로 기기 uuid 전달**(지금 안 넘겨서 사전 검증이 env base 로 됨 — dispatcher 판정과 어긋나는 기존 결함)
- `backend/routers/devices.py` `DeviceOut`·`backend/routers/admin.py` `_DEVICE_COLUMNS`: `temp_offset_base_c` 노출
- `docs/MQTT.md`(telemetry 키·명령 표·변경 이력), `docs/DATABASE.md`, `docs/ENV.md`(env 는 이제 폴백용이라고 명시)

**콘솔**
- `web/beta.html` 기기 줄(504행 부근): base 표시 + 입력칸 + `set_temp_base` 전송. 기존 절대값 칸엔 "디버그용, 보드 보정은 base 로" 안내
- `web/index.html`: 기기 셀에 `(base -3.0 · 사용자 +1.0)` 표기

**테스트**
- `tests/test_temp_offset.py`: 보드별 base 캐시·폴백·범위
- handlers: base 있는/없는 telemetry 각각 사용자값 계산
- commands API: `set_temp_base` 검증, `set_temp_offset` 사전 검증이 보드 base 를 쓰는지

### Out (안 함)
- 펌웨어가 사용자값을 따로 저장하는 구조(4번안) — 구 앱 `offset_c` 경로를 깨고 변경이 큼
- 과거 telemetry 재해석 — 저장된 온도는 그 시점 보정 그대로(Stage S D 와 동일)
- 시리얼/페어링 시 base 주입
- supermini·hub 펌웨어(hub 동결, supermini 는 확인만)

## 함정 (구현·배포 때 반드시)

1. **마이그레이션이 서버 코드보다 먼저**. 컬럼 없이 코드가 가면 `_missing_columns` 1회 어긋남(Stage S 함정 1 재현).
2. **서버가 펌웨어보다 먼저**. 구 펌웨어는 base 키를 안 보내니 env 폴백으로 Stage S 와 동일 — 순서 거꾸로 하면 기기가 보낸 base 를 서버가 버려서 사용자값이 틀어짐.
3. **T3 동시 이동은 기기 안에서만**. 서버가 `set_temp_base` 뒤에 `set_temp_offset` 을 따로 보내는 식으로 하면 두 명령 사이에 사용자값이 튀고, 하나만 성공하면 영구히 어긋남.
4. **telemetry 쌍 기록**. `temp_offset_c`·`temp_offset_base_c`·`temp_offset_user_c` 는 한 UPDATE. 앱이 Realtime 한 이벤트에서 셋을 같이 봄.
5. **dispatcher base 캐시 stale**. `set_temp_base` 직후 사용자가 바로 `set_temp_offset` 하면 다음 telemetry 전까지 서버 캐시는 옛 base → 기기값이 옛 base 기준으로 계산됨. 다음 telemetry 에서 사용자값이 (새 base − 옛 base) 만큼 튄다. 완화: `set_temp_base` ACK(success) 수신 시 캐시·DB 를 즉시 갱신. 사용자가 동시에 만질 확률은 낮지만 완화는 넣는다.
6. **보드 교체(같은 device_id)**: 새 보드는 NVS 비어 있어 Kconfig -1.5 를 base 로 보고 → 자동으로 맞춰짐. 운영자가 base 재입력만 하면 됨.
7. **앱 범위 문구**: 보드마다 사용자 허용 범위가 달라짐(-1.5 보드 -8.5~11.5, -3 보드 -7~13). 앱이 서버 거절(`offset_out_of_range`)만 처리하면 문제 없음. 앱팀 확인.

## 배포 순서

1. 마이그레이션 적용 → `MIGRATIONS_APPLIED.md`
2. 서버 PR 머지 → API·브리지 **둘 다** 재시작(프로세스 분리 함정) → 구 펌웨어 기기 사용자값이 Stage S 와 같은지 확인
3. nano 펌웨어 릴리스 등록 → 실기 1대 OTA → telemetry 에 `temp_offset_base_c: -1.5` 찍히고 앱 0.0 유지 확인
4. 콘솔에서 그 보드 `set_temp_base -3` → telemetry `temp_offset_c -3.0 / base -3.0 / user 0.0`, 앱 0.0 그대로
5. 앱에서 +1 → 기기 -2.0, 앱 +1.0. 콘솔에서 base -2 → 기기 -1.0, 앱 **여전히 +1.0** (T3 검증)
6. 앱팀에 "범위 보드별" 통지

## 완료 조건

- [x] 펌웨어: `t_base` NVS, `set_temp_base` 명령(동시 이동·범위 거절), telemetry base 보고, 버전 1.3.0-20261110 — nano `feat/temp-base` 2bd5fdc, 빌드 통과(31% 여유). 릴리스 등록·실기 전
- [ ] 마이그레이션 적용·기록 (파일: `migrations/2026-11-10_devices_temp_offset_base.sql`)
- [x] 서버: base 캐시·폴백, handlers 쌍 기록, dispatcher/API `set_temp_base`, API `resolve_device_offset` 에 uuid 전달 (2026-11-10)
- [x] 콘솔: base 칸(🏭)·표기 — index.html·beta.html (2026-11-10)
- [x] 테스트 통과 — 1183 (신규 17: temp_offset 6, handlers 7, API 2, dispatcher 2)
- [ ] 실기: 배포 순서 3~5 통과
- [x] 문서: MQTT/DATABASE/ENV/API 갱신 (2026-11-10)

## 학습 노트

- "운영자 보정"과 "사용자 보정"은 같은 다이얼이 아니다. 하나로 두면 운영자 작업이 사용자 화면에 새어 나간다.
- base 가 바뀔 때 사용자값을 고정하려면 **적용값을 같은 트랜잭션에서 같이 옮겨야** 한다. 기기 안에서 하는 게 유일하게 원자적이다.
