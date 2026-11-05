# 회신 — 온도 보정 분리 결정 문서(`TEMP_OFFSET_BASE_DECISION_2026-11-05.md`) + PR #48 펌웨어 질문

> **작성**: 이관훈, 2026-11-05
> **답하는 것**: 결정 문서 §7 확인 요청 1~5, 그리고 PR #48(자동 환기·분무) 메시지의 펌웨어 질문 ①~③
> **관련**: [docs/REVIEW_TEMP_OFFSET_BASE_2026-11-05.md](REVIEW_TEMP_OFFSET_BASE_2026-11-05.md)(§5 결정 기록 채움), [specs/stage-s-temp-offset-base.md](../specs/stage-s-temp-offset-base.md)

---

## A. 온도 보정 결정 — §7 확인

| # | 확인 | 답 |
|:---:|---|---|
| 1 | D2·D4 를 A → B(호환 방식)로 | **동의.** 합산값 앱이 고객 APK 로 나가 있는 이상 의미 변경은 못 한다. 결정 문서 §1 표(서버 먼저/앱 먼저 둘 다 틀어짐)가 맞다 |
| 2 | D9·D10·D12 변경 | **동의.** D10 보강 하나 아래 |
| 3 | §3 구현 조건 8개 | **동의.** 1·2·4·8 에 보강 아래 |
| 4 | 앱 계약 초안 §6 | **이대로.** 추가 한 줄만 아래 |
| 5 | "배포 완료" 알림 주체·시점 | 아래 제안 |

### A-1. 보강 — 구현 조건

**조건 1·2 (마이그레이션 먼저, 두 컬럼 쌍으로)** — 코드로는 100% 못 막는다는 걸 전제로.
- `_write_heartbeat`(`backend/mqtt/handlers.py:587`)는 컬럼 누락(PGRST204/42703)을 만나면 **그 컬럼만 빼고 즉시 재시도**하고, `_missing_columns` 캐시에 TTL 로 기억해 이후 heartbeat 에서 뺀다.
- 쌍 보장은 `handle_telemetry` 에서 "`devices.temp_offset_user_c` 가 캐시에 있으면 `temp_offset_c` 도 함께 뺀다"로 넣을 수 있다. 하지만 **첫 실패 1회**는 재시도 루프가 `user_c` 만 빼고 `temp_offset_c` 를 쓰므로 Realtime 에 한 번 어긋난 이벤트가 나간다. 그래서 진짜 방어는 **마이그레이션 선적용**(§5 순서 1). 코드 쪽 쌍 처리는 "그 뒤 10분을 막는 2차 방어"로만 쓴다.

**조건 4 (`user_offset_c` 를 발행 payload 에서 제거)** — 기존 메커니즘이 있다. `dispatcher.py` 의 `_SERVER_ONLY_PAYLOAD_KEYS`(지금 `lcd_text` 하나)에 `user_offset_c` 를 넣으면 병합 단계에서 자동으로 빠진다. 그 다음 `set_temp_offset` 분기에서 `publish_payload["offset_c"] = to_device(user)` 를 세팅. mist 분할(`burst_ms`)과 같은 자리.

**조건 8 (Realtime 에 새 컬럼)** — Supabase `supabase_realtime` publication 은 **테이블 단위**(컬럼 목록 없음)라 `ADD COLUMN` 뒤 별도 조치 없이 UPDATE 이벤트에 실린다. 실기 때 앱 구독으로 한 번 보면 끝.

**추가 1 — API 검증.** `routers/commands.py` `_validate_generic` 이 지금 `set_temp_offset` 에 `offset_c` 를 **필수**로 본다. `user_offset_c` 분기(숫자·합산 범위) + 두 키 동시 400 을 넣어야 한다. 영향 범위 표에 이미 있지만 "필수 → 둘 중 하나" 로 바뀐다는 점만 명시.

**추가 2 — env 파싱 실패.** `TEMP_OFFSET_BASE_C` 가 비어 있거나 숫자가 아니면 **기본 -1.5 + warning**. 0 으로 떨어지면 안 된다(D11 경고와 같은 이유 — 신 앱 사용자 기기가 1.5℃ 높게 읽힘).

**추가 3 — `tests/test_migration_coverage.py`.** handlers 가 쓰는 컬럼이 마이그레이션 SQL 에 있는지 대조하는 테스트라, 새 컬럼이 `migrations/2026-11-05_devices_temp_offset_user.sql` 에 들어가야 통과한다. 파일명 그대로 가면 됨.

### A-2. 보강 — D10 콘솔

- `web/index.html` 온도 셀은 `GET /devices`(사용자 API, `DeviceOut`)를 쓴다. 괄호 사용자값을 붙이려면 **`DeviceOut` 에 `temp_offset_user_c` 추가**가 필요하다(영향 범위 표의 `devices.py` 항목이 이것). 앱은 PostgREST 로 읽으니 API 응답 추가는 콘솔용.
- `web/beta.html` 은 `/admin` 응답(`_DEVICE_COLUMNS`)에 같은 컬럼 추가.

### A-3. 앱 계약 초안 §6 — 추가 한 줄

- 거절 표에 **기기 측 결과가 그대로 남는다**는 줄: 구 펌웨어(`temp_offset_c` 미보고 3대)는 `unknown_action` 이 아니라 `set_temp_offset` 을 알고 있으므로 정상 처리되고, 기기 범위 밖은 `bad_request` 로 돌아온다(서버가 먼저 걸러서 실제로는 안 나옴). 앱이 `result` 문자열을 화이트리스트로 보면 빠진 값 없게.
- 나머지(두 키 금지·0.1 단위·`temp_offset_user_c` 기준 저장 완료 판정·`-8.5 ~ +11.5`)는 그대로.

### A-4. "배포 완료" 알림

- **서버를 배포하는 사람이**, §5 실기 확인 4항목(구경로·신경로·거절·Realtime)을 끝낸 **직후** 앱 계약 문서 머리에 `배포 완료 2026-11-XX` 한 줄을 넣고 그 커밋 링크를 앱팀에 보낸다. 실기 전에 알리면 앱이 먼저 나가 없는 컬럼을 조회하는 사고가 난다.
- 배포는 내가 하니(Lightsail) 알림도 내가. 결정 문서 §5-3 에 그렇게 적어 두면 된다.

### A-5. 함정 5 해소 — 펌웨어 3트리 기본값

소스로 확인했다. 전부 -1.5.

| 트리 | 설정 | 값 |
|---|---|---|
| terra-iot-nano | `CONFIG_APP_TEMP_OFFSET_MDEG` | -1500 |
| terra-iot-supermini | `CONFIG_APP_TEMP_OFFSET_MDEG` | -1500 |
| terra-hub-p4 (동결) | `CONFIG_APP_IOT_TEMP_OFFSET_MDEG` | -1500 |

→ D1-A `TEMP_OFFSET_BASE_C=-1.5` 전 기기 일괄 적용 OK.

### A-6. 스테이지 번호

- 내 스펙은 **Stage S** 로 바꿨다(`specs/stage-s-temp-offset-base.md`). PR #48 이 `stage-r-auto-env.md` 로 Stage R 을 먼저 썼기 때문. 결정 문서에서 `stage-r-temp-offset-base.md` 를 가리키는 곳은 `stage-s-…` 로 읽어 달라. 스펙 본문은 아직 A 안 기준이라 머리에 "B 로 개정 필요" 만 적어 뒀고, 구현 PR 에서 함께 고친다.

---

## B. PR #48 (자동 환기·분무) — 펌웨어 질문 ①~③

`terra-iot-nano` 소스(`main/src/command_dispatch.c`, `main/src/actuators/relay.c`) 기준. supermini 도 같은 로직 확인. 팬 타이머는 초기 커밋부터 있어서 **베타 기기 전부(구 펌웨어 3대 포함) 같은 동작**.

| # | 질문 | 답 | 근거 |
|:---:|---|---|---|
| ① | 팬 타이머 도는 중 `fan_on{duration_ms}` 재전송 → `busy`? | **맞다.** ack `result="busy"`, `state="busy"`. 기존 타이머는 그대로 돈다(연장 아님) | `relay_pulse()` 가 `h->pulsing` 이면 `ESP_ERR_INVALID_STATE` → dispatch 가 `busy` 로 변환 |
| ② | 타이머 도는 중 일반 `fan_on`(duration 없음) → 자동 OFF 취소? | **취소된다.** 팬은 계속 ON 으로 남고, 예약된 auto-off 는 사라진다 | `relay_on()` → `relay_set()` 이 `relay_cancel_pulse()` 호출(`esp_timer_stop` + `pulsing=false`) 뒤 ON |
| ③ | `fan_off` 가 fan 도 fan2 처럼 타이머를 취소하나? | **같다.** fan·fan2 모두 `relay_off()` → `relay_set()` 경로라 타이머 취소 후 OFF | 같은 `relay_set()`. `fan_toggle`·`fan2_toggle` 도 동일 |

자동 제어 설계에 걸리는 것 두 가지:
- **②가 함정.** 사용자가 앱에서 수동 `fan_on` 을 누르면 자동 환기의 타이머가 **조용히 사라져** 팬이 계속 돈다(ack 는 `ON`, 에러 아님). 서버가 "자동으로 켠 팬" 을 추적하다 수동 ON 이 끼면 그 추적을 끊거나, 자동 OFF 를 서버 쪽에서 한 번 더 보내는 보호가 필요하다.
- **①의 `busy` 는 연장이 아니다.** 자동 환기가 "아직 습도 높음 → 10분 더" 로 `fan_on{duration_ms}` 를 다시 보내면 거절되고 기존 타이머가 끝나는 순간 꺼진다. 연장하려면 `fan_off` → `fan_on{duration_ms}` 순서(두 명령) 또는 타이머 종료 뒤 재발행이 필요. `relay_set` 의 `min_toggle_ms` 가드(팬 릴레이 **200ms**, `main.c`)도 있어 `fan_off` 직후 200ms 안의 `fan_on` 은 `ESP_ERR_INVALID_STATE` 로 거절된다 — 디스패처 폴링이 1초라 보통은 안 걸리지만 두 명령을 같은 폴링에 넣지 말 것(mist 분할의 `MIST_SPLIT_GAP_SEC=1.5` 와 같은 이유).
- 참고: `FAN_TIMER_MAX_MS` = 2시간 clamp. 그 이상을 보내면 조용히 2시간으로 잘린다(mist 상한 clamp 와 같은 패턴).

위 ①②는 PR #48 설계와 대조하면: ②는 `on_other_ack`(수동 조작 ACK 가 오면 소유 해제)로 이미 받아내고 있고, ①은 자동 환기가 연장을 시도하지 않고(telemetry `fan=="OFF"` 확인 뒤에만 시작, `REFIRE_MIN_SEC` 300초) 타이머 종료를 기다리는 구조라 걸리지 않는다. 즉 **펌웨어 동작 3가지 모두 PR 의 가정과 맞다.**

---

## C. PR #48 코드 리뷰 (`origin/feat/auto-env` vs `origin/main`, 2026-11-05)

테스트: 별도 워크트리에서 `uv run pytest -q` → **1143 passed, 0 failed** (경고 6건은 기존 supabase-py deprecation).

**머지 차단급 — 없음.** 저자 주장 세 가지를 코드로 추적해 확인:
- 플래그 OFF 무변화: `alerts.py` `_thresholds` 가 `auto_env_enabled()` 거짓이면 `device_settings` select 에 auto 컬럼을 넣지 않고, v4 뒤 자동 판정 블록은 `auto_env_store.step` 을 호출하지 않는다(DB 쓰기 0). `dispatcher.py` 재확인은 `source=="auto"` 행에만 걸리는데 마이그레이션 전엔 CHECK 제약이 `'auto'` 를 막아 그런 행이 없다. `test_auto_env_off_by_default` 가 commands/schedules 쓰기 0회를 검증.
- 훅 3곳(v4 뒤·ACK 뒤·발행 직전) 모두 `except Exception` 격리. 훅이 터져도 v4/ACK 가 끝까지 가는 테스트 있음.
- 폭주 없음: `fan_on` 은 telemetry `fan=="OFF"` 뒤에만, 소유 중엔 `REFIRE_MIN_SEC`·일일 상한·쉼으로 재발행 차단. 분무는 펌웨어 one-shot 타이머가 끄는 계약이라 서버가 펌프를 켜둔 채 남기는 경로 없음.

**머지 전 고치면 좋은 것**
| # | 위치 | 내용 |
|:---:|---|---|
| W-1 | `backend/auto_env.py:470, 496` | `assert` 로 프로덕션 불변식 보증. 깨지면 `AssertionError` 가 상위 `except Exception` 에 먹혀 그 틱만 조용히 스킵되고, `-O` 로 돌리면 다른 예외로 바뀜. 명시적 `if … is None: logger.warning; return` 으로 |
| W-2 | `dispatcher.py:117`, `auto_env_store.py:63` | `AUTO_SOURCE = "auto"` 중복 정의. dispatcher 가 `auto_env_store` 에서 import 하면 됨(순환 없음) |

**참고**
- I-2 경합: 자동 `fan_on` ACK 대기 중(`phase=="requested"`)에 수동 `fan_toggle` ACK 가 먼저 오면 `on_other_ack` 가 무반응이고, 뒤늦은 자동 ACK 가 소유를 확정하는 짧은 창. 저자가 스펙(`stage-r-auto-env.md:74`)에 "수백 ms 경합 잔존" 으로 이미 적어 둔 것이고 telemetry 로 자가 교정됨. 새 결함 아님.
- I-3 전역 킬스위치 `AUTO_ENV_ENABLED=false` 는 즉시 `fan_off` 를 보내지 않고 펌웨어 1시간 타이머에 맡긴다(ENV.md 에 명시, 의도). 기기별 토글 `auto_vent_enabled=false` 는 소유 중이면 실제 `fan_off` 발행 — 두 경로 차이를 운영자가 알아야 함.
- 잘된 점: 플래그 OFF·예외·컬럼 없음 세 안전 경계가 전부 테스트로 커버. `auto_env.py` 가 DB·시계 모르는 순수 상태 기계(`humid_cycle_store` 패턴)라 시간 축만 조작해 결정론적 테스트. 마이그레이션 적용 기록이 CLAUDE.md 규칙대로.

**결론**: W-1·W-2 는 머지를 막을 정도는 아니다. 그대로 머지해도 되고, 고칠 거면 같은 PR 에 한 커밋 추가 뒤 머지. 배포 뒤 동작 변화는 없음(플래그 기본 꺼짐) — 켜는 순서(섀도 2~3일 → 1대 → 전체)는 제안대로.
