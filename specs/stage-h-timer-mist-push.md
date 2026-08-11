# Stage H — 예약 타이머 · 물분무 펄스 · 푸시 알림

**진행**: H-1 백엔드 ✅ / H-2 ✅ (백엔드+마이그레이션+러너+테스트) / 웹 콘솔 테스트 UI ✅ / H-3 미착수 / firmware `mist` 핸들러 대기

**웹 콘솔** (`web/index.html`): 디바이스 행에 💦1s/2s/3s 물분무 버튼, "⏰ 예약 타이머" 패널(생성/목록/삭제).
mist·schedules REST 엔드포인트를 직접 호출 → 최근 명령 테이블에서 status(pending→sent→acked) 추적.

3개 기능 묶음. 공통 원칙: **기존 `commands` 파이프라인(멱등·TTL·ack·Realtime) 재활용**. 새로 발명 최소화.

권장 구현 순서: **H-1(물분무) → H-2(예약 타이머) → H-3(푸시)**
물분무가 타이머의 대표 유스케이스라 먼저 깔면 타이머 테스트가 쉬움.

## 확정된 결정 (사용자 합의)

- **앱 클라이언트**: Flutter / React Native → 푸시는 **FCM HTTP v1** (안드/iOS 통합)
- **물분무**: S3 제어 firmware 수정 가능 → **firmware 원샷 펄스** 방식 채택
- **예약 반복**: **daily(매일) + weekly(요일 지정)** 까지
- **하드웨어 2종**: A=릴레이, B=레벨시프터+MOSFET. **둘 다 on/off만** (세기 조절 없음).
  스위칭 소자 차이뿐 — 명령 계약 완전 통일, firmware 는 구동 핀만 다름.

---

## H-1 — 물분무 1/2/3초 (firmware 원샷 펄스)

### 개요
새 command action `mist`, payload `{duration_ms}`. 펌프 ON 후 **firmware 내부 one-shot 타이머로 정확히 duration 뒤 자동 OFF**. 단일 명령 자기완결 → OFF 유실로 인한 침수 리스크 0.

> ⚠️ `relay_toggle`(상태 토글) 재사용 금지. ON→(서버 지연)→OFF 2개 명령은 OFF 유실 시 펌프가 계속 돎.

### 하드웨어 2종 — 스위칭 소자만 다름
둘 다 **on/off 디지털 제어** (세기 조절 없음). A=릴레이, B=레벨시프터+MOSFET. firmware 는 구동 핀/방식만 다르고 로직 동일:
```c
void pump_set(bool on);   // A: gpio_set(relay_pin, on) / B: gpio_set(mosfet_gate_pin, on)
// mist 핸들러 공통: pump_set(true) → esp_timer once(duration_ms) → pump_set(false)
```
→ 앱·백엔드·스케줄러·계약 전부 하드웨어 무관. `actuator_type` 컬럼·`intensity` 파라미터 불필요.

### In
- Stage C(dispatch) 완료
- S3 제어 firmware (A: relay GPIO / B: MOSFET gate GPIO)

### 완료 조건
- [ ] **firmware**: `mist` action 핸들러 추가 (A/B 공통, 핀 config 만 분기) ← **남은 작업**
  - [ ] payload `duration_ms` 파싱 → `pump_set(true)` → `esp_timer` one-shot → `pump_set(false)`
  - [ ] 안전 상한 `MIST_MAX_MS = 5000` clamp
  - [ ] 실행 후 ack `"ok"` (기존 msg_id 멱등·TTL 로직 그대로)
- [x] **backend**: `POST /devices/{id}/mist` — `backend/routers/commands.py`, 검증은 `command_service.py`
  - [x] 허용 `duration_ms` ∈ {1000, 2000, 3000} clamp, 그 외 400
  - [x] dispatcher 변경 없음 (action passthrough)
- [x] **테스트**: `tests/test_commands_api.py` (ok/400/404)
- [ ] firmware 통합 테스트: A/B 각각 `mist {duration_ms:2000}` → 2초 후 자동 OFF → ack

### 설계 메모
- 앱 1/2/3초 버튼 → `duration_ms` 1000/2000/3000 매핑
- **안전 차이 (참고용, 계약엔 무영향)**:
  - B(MOSFET): firmware hang/부팅 시 게이트 float 방지 → **게이트 풀다운 저항(HW)** + duration 타이머가 강제 OFF
  - A(릴레이): 접점 마모 → H-2 예약에서 릴레이 디바이스는 1초 미만 잦은 반복 자제
- 카메라(P4) firmware 엔 펌프 없음 — 별도 센서/제어 디바이스 firmware 대상

---

## H-2 — 예약 타이머 (`schedules` 테이블 + bridge 스케줄러)

### 개요
스케줄러 루프가 due 된 예약을 `commands` 로 INSERT 만 하고, 발행은 기존 dispatcher 담당. daily/weekly 반복.

### In
- H-1 (물분무가 대표 예약 액션)
- dispatcher (단일 인스턴스 가정 — race 없음)

### 완료 조건
- [x] **migration** `migrations/2026-08-10_schedules.sql` — 테이블 + RLS(own schedules all) + Realtime + weekly CHECK
- [x] **backend** `backend/schedule_runner.py` (OfflineMonitor 패턴, 30초 폴링)
  - [x] `next_run_at <= now() AND enabled` 조회 → advance 먼저 → `commands` INSERT
  - [x] KST → UTC 환산 (`backend/scheduling.py`, `Asia/Seoul` ZoneInfo)
- [x] **backend** `/devices/{id}/schedules` + `/schedules/{id}` REST 라우터 (`backend/routers/schedules.py`, CRUD+검증)
- [x] bridge 프로세스에 `ScheduleRunner.start()` (`mqtt_bridge_main.py`)
- [x] **테스트** `tests/test_scheduling.py`(KST 계산), `tests/test_schedule_runner.py`(발화)
- [ ] **마이그레이션 적용** (Supabase SQL Editor 에서 실행) ← **남은 작업**
- [ ] E2E: 예약 등록 → next_run_at 도달 → commands INSERT → 발행 → ack

### 스키마
```sql
CREATE TABLE public.schedules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    owner_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    action       TEXT NOT NULL,            -- 'mist' | 'led_on' | 'fan_toggle' ...
    payload      JSONB,                    -- {duration_ms:2000} 등
    kind         TEXT NOT NULL,            -- 'daily' | 'weekly'
    time_of_day  TIME NOT NULL,            -- KST 기준 08:00
    days_of_week INT[],                    -- weekly: [1,3,5] (월수금, 1=월). daily 는 NULL
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at  TIMESTAMPTZ NOT NULL,     -- UTC 로 계산 저장
    last_run_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_schedules_due ON schedules(next_run_at) WHERE enabled;
```

### 설계 메모
- **KST 함정**: 클립 날짜 폴더 버그와 동일. `time_of_day` 는 KST 저장, `next_run_at`(UTC) 환산 폴링. 일관되게 `Asia/Seoul`.
- **멱등**: `next_run_at` 을 먼저 advance 한 뒤 commands INSERT (중복 발화 방지). 단일 인스턴스라 lock 불필요.
- pg_cron per-schedule 방식은 동적 관리 지옥 → 테이블+폴링이 단순.

---

## H-3 — 푸시 알림 (FCM HTTP v1, 그린필드)

### 개요
(a) 푸시 토큰 저장 + (b) `alerts` INSERT 직후 FCM 발송. 알림 dedup 은 기존 로직 재사용.

### In
- alerts 파이프라인 (Stage D — 이미 dedup 완료: `alerts.py`, `handlers.handle_alert`)
- Firebase 프로젝트 + 서비스계정 JSON

### 완료 조건
- [ ] **migration** `push_tokens` 테이블 + RLS (own tokens all)
- [ ] **backend** `/push/tokens` 라우터 (앱 로그인 시 토큰 등록/삭제)
- [ ] **backend** `backend/push.py` — FCM HTTP v1 발송 (`httpx + google-auth`, 서비스계정)
- [ ] **트리거**: alert INSERT 직후 push 발송 한 줄 (`alerts.py` / `handlers.handle_alert`)
  - [ ] `owner_id` 로 `push_tokens` 조회 (**`.eq("owner_id", ...)` 명시 필터** — RLS 바이패스 주의)
- [ ] env `FCM_PROJECT_ID`, `FCM_CREDENTIALS_PATH` 추가 (docs/ENV.md)
- [ ] 의존성 `uv add httpx google-auth` (또는 `firebase-admin`)
- [ ] 통합 테스트: 임계 초과 telemetry → alert INSERT → FCM 발송 mock 확인

### 스키마
```sql
CREATE TABLE public.push_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL,
    platform   TEXT NOT NULL,          -- 'android' | 'ios'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, token)
);
```

### 설계 메모
- bridge 는 service_role → owner 토큰 조회 자유. 단 명시 필터 필수 (CLAUDE.md 규칙).
- 확장 여지: `motion_clips` INSERT(모션 감지)나 예약 실행 완료도 동일 발송 함수 재사용.
- 앱 측: `firebase_messaging`(Flutter) / `@react-native-firebase/messaging`(RN) 로 토큰 획득 → `/push/tokens` POST.

## Out (이번 범위 아님)
- 푸시 알림 사용자별 on/off 세분화 설정 (알림 종류별)
- 푸시 재시도 큐 / 실패 토큰 정리(GC)
- 명령 스케줄러 우선순위 / 충돌 해소

## 참고
- [docs/MQTT.md](../docs/MQTT.md) — command/ack 페이로드
- [backend/mqtt/dispatcher.py](../backend/mqtt/dispatcher.py) — 재사용할 발행 파이프라인
- [backend/alerts.py](../backend/alerts.py) — 푸시 트리거 지점
