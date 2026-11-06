# 앱 전달 — 온도 보정 새 계약 (`user_offset_c` · `temp_offset_user_c`)

> **대상**: 앱 담당
> **작성**: terra-server 백엔드 담당, 2026-11-05
> **성격**: 공장 보정 −1.5℃를 서버가 맡습니다. 새 앱은 **사용자 보정값만** 보내고 읽습니다. 기존 키·컬럼은 의미를 그대로 두니 **배포된 앱(0.166.0~0.174.x)은 바꾸지 않아도 됩니다.**
> **배포 완료 2026-11-05 · `ce61eca`** (12:59 UTC, PR #49·#50). 실기(terra-d9feaaa2): 새 경로 `user_offset_c`·옛 경로 `offset_c`·범위 밖 거절 통과, 배포 직후 온라인 10대 쌍 기록(사용자값 = 기기값 + 1.5) 확인.
> **서버 상태**: **배포 완료**. 새 앱(0.174.6) 계약 스위치를 켜도 됩니다. 켤 때 `devices` Realtime UPDATE 한 이벤트에 `temp_offset_c`·`temp_offset_user_c` 가 같이 오는지 한 번 확인 부탁드립니다(서버는 코드·테스트로만 보장).
> **관련**: [BACKEND_HANDOFF_REPLY_ENV_ALERTS_V4_2026-11-02.md](../reports/BACKEND_HANDOFF_REPLY_ENV_ALERTS_V4_2026-11-02.md) §1(옛 앱 계약 — 유지), [../reports/APP_TEMP_OFFSET_PAYLOAD_CHECK_2026-11-05.md](../reports/APP_TEMP_OFFSET_PAYLOAD_CHECK_2026-11-05.md)(질문서), [API.md](API.md) §4.13, [MQTT.md](MQTT.md) §1·§2
> **답하는 것**: 앱 회신(2026-11-05) 1·2차 — 요청 사항 전부 반영했습니다. 백엔드 확인(2026-11-05)까지 끝난 최종본입니다.

## 0. 요약

| | 옛 앱 (0.166.0~0.174.x) | 새 앱 |
|---|---|---|
| 보내는 키 | `offset_c` = **합산값**(공장 −1.5 포함) | `user_offset_c` = **사용자값** |
| 보정 끔 | `offset_c: -1.5` | `user_offset_c: 0` |
| 읽는 컬럼 | `devices.temp_offset_c` (기기에 걸린 합산값) | `devices.temp_offset_user_c` (사용자값) |
| 공장 보정 −1.5 계산 | 앱 | **서버** — 앱은 `factoryDeviceValue` 제거 |
| 서버 배포 뒤 변화 | **없음** | — |

공장 보정값은 서버 설정 하나(지금 −1.5℃, 전 기기 공통)입니다. 나중에 보드마다 달라져도 서버가 `temp_offset_user_c` 를 맞게 계산하므로 앱은 바뀌지 않습니다.

## 1. 보내기 — `POST /devices/{id}/commands`

```json
{ "action": "set_temp_offset", "payload": { "user_offset_c": 1.0 } }
```

| 사용자 보정 | `user_offset_c` | 기기에 걸리는 값 |
|---|---|---|
| 끔 | `0` | −1.5 |
| 더하기 1.0 | `1.0` | −0.5 |
| 빼기 2.0 | `-2.0` | −3.5 |
| 화면 끝값 ±4.0 | `±4.0` | −5.5 / +2.5 |

- 숫자(0.1 단위)만 보내 주세요. 지금처럼 앱에서 반올림하면 됩니다.
- 허용 범위는 **−8.5 ~ +11.5** 입니다(공장 보정을 더해 기기 허용 ±10℃ 안). 앱 화면 범위 ±4.0은 그 안에 있습니다.
- **`offset_c` 와 `user_offset_c` 를 한 명령에 같이 보내지 마세요.** 400 으로 거절합니다.
- 응답·상태 흐름은 지금과 같습니다(201 + `id`, 이후 `commands.status` 가 `pending → sent → acked`). `commands.payload` 에는 보낸 그대로 `{"user_offset_c": 1.0}` 가 남습니다.

## 2. 읽기 — `devices.temp_offset_user_c`

| 기기 보고(`temp_offset_c`) | `temp_offset_user_c` | 앱 표시 |
|---|---|---|
| −1.5 | `0.0` | 보정 꺼짐 |
| −0.5 | `1.0` | +1.0 |
| −3.5 | `-2.0` | −2.0 |
| 보고 없음(구 펌웨어) | `null` | 펌웨어 업데이트 필요 |

앱 요청 3가지를 이렇게 지킵니다.

1. **같은 UPDATE** — `temp_offset_c` 와 `temp_offset_user_c` 는 telemetry 수신 때 한 번의 UPDATE 로 함께 씁니다. Realtime `newRecord` 에서 두 값은 항상 같은 시점 값입니다.
2. **0.1 단위** — 컬럼 타입 `numeric(4,1)`. 서버가 0.1 단위로 반올림해 저장합니다.
3. **미보고는 `null`** — `temp_offset_c` 와 같은 규칙입니다. 기기가 보정값을 보고하지 않으면 두 컬럼 모두 바뀌지 않습니다.

- 읽는 경로는 지금처럼 PostgREST 직접 조회(`select('temp_offset_user_c')`)와 그 기기 행 Realtime UPDATE 그대로 쓰시면 됩니다. Realtime 구독은 테이블 단위라 새 컬럼도 따로 설정하지 않아도 `newRecord` 에 실립니다.
- 값은 **기기 보고로만** 바뀝니다. 명령 ack 때 서버가 미리 채우지 않습니다(10/01 원칙 그대로).
- 기존 배포 기기는 서버 배포 뒤 다음 telemetry(약 3초)부터 값이 채워집니다. 그 전까지는 `null` 입니다.
- 읽기 허용 범위는 −8.5 ~ +11.5 로 맞춰 주세요.

## 3. 저장 완료 판정

- **신 펌웨어**: 보낸 `user_offset_c` 와 `temp_offset_user_c` 가 0.1 단위로 같아지면 완료.
- **구 펌웨어(`temp_offset_user_c = null`)**: 지금과 같이 명령 `status = acked`, `result = ok` 로 판정.

## 4. 거절

| 어디서 | 언제 | 앱이 보는 것 |
|---|---|---|
| REST 400 | `user_offset_c` 가 숫자가 아님 / −8.5 ~ +11.5 밖 / `offset_c` 와 같이 옴 | 400 + 이유 문구 → "보정값을 저장하지 못했어요" (지금 그대로) |
| 명령 행 | 서버가 발행 전에 거절(REST 를 거치지 않은 명령 방어용) | `status = 'rejected'`, `result = 'invalid_payload'` 또는 `'offset_out_of_range'` |
| 기기 | 기기 허용 범위(±10℃) 밖 값을 받음 — 서버가 먼저 거르므로 실제로는 안 나옴 | `status = 'acked'`, `result = 'bad_request'` (지금과 같음) |

- 명령 행 거절은 `status` 와 `result` 를 **한 번의 UPDATE 로** 씁니다(요청하신 조건). 기존 `unsupported_action` 거절과 같은 방식입니다.
- 앱은 REST 만 쓰므로 명령 행 거절은 실제로 볼 일이 거의 없습니다. 문구("기기가 받지 못했어요")는 지금 그대로 두셔도 됩니다.
- 구 펌웨어(보정값 미보고 기기)도 `set_temp_offset` 은 알고 있어 `unknown_action` 이 아니라 정상 처리합니다.
- `result` 를 정해진 목록으로 다루신다면 `ok` · `bad_request` · `invalid_payload` · `offset_out_of_range` 가 빠지지 않게 해 주세요. 성공은 지금처럼 `result == 'ok'` 입니다.

## 5. 바뀌지 않는 것

- `offset_c` 의미(합산값)와 검사(±10℃) — 옛 앱·운영 콘솔이 계속 씁니다.
- `devices.temp_offset_c` 의미(기기에 걸린 값).
- 실시간 판정·일일·주간 분석이 쓰는 온도 — 기기가 보정한 값 그대로입니다.
- MQTT, 펌웨어.

## 6. 배포 순서

1. **서버 배포** (DB 컬럼 추가 → 서버 코드) — 배포 담당(이관훈)이 합니다. 실기 확인 4가지(옛 경로·새 경로·거절·Realtime)를 끝낸 직후 이 문서 맨 위에 `배포 완료 2026-11-XX` 를 적고, 그 커밋 링크를 앱팀에 보내 드립니다. 실기 확인 전에는 알리지 않습니다.
2. **새 앱 배포** — 1번 알림 뒤에 해 주세요.
   - 서버보다 먼저 나가면 `select('temp_offset_user_c')` 가 없는 컬럼이라 조회가 실패하고, `user_offset_c` 명령도 400 으로 거절됩니다(잘못된 온도는 안 걸리지만 보정 화면이 동작하지 않습니다).
3. 옛 앱은 그대로 둬도 됩니다. 서버 배포 전후로 동작이 같습니다.

## 7. 나중에 (지금 할 일 없음)

옛 앱 사용자가 모두 새 앱으로 넘어가면 `offset_c` 경로와 앱에서의 `temp_offset_c` 사용을 정리할 예정입니다. 그 전에 앱 최소 지원 버전을 함께 정하고 따로 공지하겠습니다.
