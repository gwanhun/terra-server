# 백엔드 회신 — 기기 등록·그룹·기록 보존·온습도 계약 (2026-09-15)

> **회신 대상**: 앱(Flutter) `2026-09-15-lee-gwanhun-redesign-request.md` (비바노트 앱 디자인 개편)
> **작성**: terra-server 백엔드 담당
> **성격**: 코드·스키마 실측 대조 결과. 이 문서는 **판정과 근거**이며, 실제 구현은 아래 §7 순서로 별도 진행.
> **결론 먼저**: 요청 7개 영역 중 **2개는 기존 계약으로 지원 가능**, **5개는 서버 보완 필요**.
> 그중 §1-2(삭제 시 기록 보존)가 **다른 모든 항목의 선행 조건**이라 먼저 처리해야 합니다.

---

## 0. 요약

| # | 확인 요청 | 판정 | 비고 |
|---|---|---|---|
| §1-1 | 등록/claim/unlink endpoint·권한 | ⚠️ **부분 지원** | 등록은 있음. claim·unlink 개념 자체가 없음. **멱등성 없음** |
| §1-2 | 삭제 = 등록 해제, 기록 보존 | ⛔ **현재 계약과 충돌** | 삭제가 hard delete + cascade. **최우선 보완** |
| §2-1 | 그룹 구성·이름 중복·기본 이름 | ⛔ **서버 규칙 없음** | UNIQUE·길이·중복검사·자동그룹화 전부 미구현 |
| §2-2 | 개체↔카메라 연결 이력 공통 쓰기 경로 | ✅ **전제 맞음** + ⚠️ | `pets`·RPC 없음(앱팀 신설 맞음). 단 **트랜잭션 수단이 서버에 없음** |
| §3 | 유효 표본 수 기반 정확한 일평균 | ⚠️ **일부만 제공** | 평균/최소/최대 O, **지표별 유효 표본 수 X**, 과거 복원 불가 |
| §4 | 카메라 관측 coverage 원장 | ⛔ **제공 불가** | 촬영 성공/실패 원장 없음. 카메라 알림은 저장조차 안 됨 |
| §5 | 추가 개발 없는 항목 | ✅ **계약 일치** | 단 영상 삭제 버튼 숨김은 **반드시** 지켜야 함(§6) |

---

## 1. §1-1 — 기기 최초 등록·소유권 연결·등록 해제

### 1.1 현재 등록 계약 (지원됨)

**등록은 앱이 아니라 기기가 직접 호출합니다.** 앱은 BLE 로 사용자 JWT 를 기기에 넘기고, 기기가 Wi-Fi 연결 후 서버를 호출합니다.

| 항목 | 값 | 근거 |
|---|---|---|
| 엔드포인트 | `POST /devices/pair` (201) | `backend/routers/devices.py:106-157` |
| | `POST /cameras/pair` (201) | `backend/routers/cameras.py:172-231` |
| 인증 | **사용자 JWT** (`Authorization: Bearer <access_token>`) | `devices.py:115`, `cameras.py:181` |
| 요청 | `name`(필수, 1~64자), `enclosure_id`, `species`, `firmware_ver`, `capabilities` | `devices.py:41-52` |
| 응답 | `id`(UUID), `device_id`(MQTT client_id), `mqtt_token`(평문, **1회만**) | `devices.py:55-60` |
| 카메라 응답 추가 | `mqtt_broker_host` / `mqtt_broker_port` / `mqtt_use_tls` | `cameras.py:63-77` |

- 토큰은 평문을 DB 에 저장하지 않고 bcrypt 해시만 보관합니다 (`devices.py:130-151`, `backend/crypto.py:24-26`).
- BLE 프로토콜 계약은 `docs/FIRMWARE_INTEGRATION.md:59-99`, 앱측 의사코드는 `docs/APP_INTEGRATION.md:415-479` 가 정본입니다.

### 1.2 `WIFI_OK` 와 등록 완료 구분 → **이미 계약에 있습니다**

앱 문서가 우려한 부분은 기존 BLE 계약으로 해결됩니다.

- 펌웨어 TX notify 메시지에 **`PAIR_OK <device_id>`** 가 정의돼 있습니다 — `docs/APP_INTEGRATION.md:470-477`.
  `WIFI_OK` 는 Wi-Fi 연결 결과일 뿐이고, 서버 등록 성공은 `PAIR_OK` 로만 판정하세요.
- 서버측 교차 확인 수단 2가지:
  1. `GET /devices` / `GET /cameras` 재조회
  2. **Supabase Realtime INSERT 구독** — `devices` / `cameras` 모두 publication 에 포함됨
     (`migrations/2026-05-26_initial_schema.sql:242`, `migrations/2026-05-26_camera_schema.sql:165`)

> 다만 주의: Mosquitto 계정 등록이 실패해도 서버는 **201 + 토큰을 그대로 반환**합니다
> (`backend/mqtt/registry.py:92`, 호출부 `devices.py:151` 이 반환값을 무시). 즉 `PAIR_OK` 를 받아도
> MQTT 접속은 실패할 수 있습니다. 앱은 "등록 완료"와 "온라인"을 별개로 표시하는 게 안전합니다.

### 1.3 claim / unlink → **개념 자체가 없습니다**

- `claim`, `unlink`, `unpair` 문자열이 레포 전체에 **0건**입니다.
  (`pair_id` 히트는 전부 예약 on/off 묶음용 `schedules.pair_id` 로 무관한 개념 — `migrations/2026-08-18_schedules_pair_id.sql:10`)
- 소유자는 페어링 시점 JWT 로 **INSERT 때 한 번 확정**되고, 이후 `owner_id` 를 UPDATE 하는 경로가 코드에 없습니다.
  `DeviceUpdate` / `CameraUpdate` 스키마에 `owner_id` 필드 자체가 없습니다 (`devices.py:63-66`, `cameras.py:80-92`).
- DB 레벨에서도 앱의 `devices` INSERT 정책이 없어 service_role(페어링 API)만 생성 가능합니다
  (`migrations/2026-05-26_initial_schema.sql:171`).
- QR 기반 2단계 claim(`POST /cameras/prepare-pair` + Pair Token)은 **문서상 미구현 명시** — `docs/API.md:60`.

**→ 기기 양도·가족 공유·소유권 이전은 신규 설계 대상입니다.** 현재 유일한 소유자 변경 수단은 "삭제 후 재페어링"이고, 그 삭제가 §1-2 의 문제를 일으킵니다.

### 1.4 ⚠️ 멱등성 없음 — 앱 §1-1-4 요구를 서버가 보장하지 못합니다

`pair_device`(`devices.py:113-157`) / `pair_camera`(`cameras.py:179-231`) 는 **순수 INSERT** 입니다.

- 기존 행 조회(SELECT) 없음, upsert 아님, Idempotency-Key 없음
- **`409 Conflict` 가 라우터 전체에 0건**입니다. 중복 페어링 계약이 정의돼 있지 않습니다.
- 기기 식별자를 서버가 매번 새로 만들기 때문에(`device_id = f"terra-{secrets.token_hex(4)}"` — `devices.py:130`)
  **같은 기기가 pair 를 두 번 호출하면 UNIQUE 충돌조차 나지 않고 별개 행 2개가 생깁니다.**
- 구 `device_id` 는 펌웨어가 더 이상 쓰지 않으므로 영원히 offline 유령 기기로 남고, 서버가 정리할 방법이 없습니다.

중복을 막는 유일한 장치는 **서버 밖**입니다 — 펌웨어 NVS 에 `device_id` 가 있으면 pair 를 건너뜁니다
(`docs/FIRMWARE_INTEGRATION.md:24-28, 89`). NVS 가 지워지거나 기기를 초기화하면 좀비 행이 생깁니다.

스펙도 이 문제를 인지하고 있으나 미구현입니다 — `specs/stage-b-device-pairing.md:45-46` ("DB UNIQUE 위반 시 재생성 → 최대 3회 재시도")에 해당하는 코드가 없습니다.

**보완안**: `devices` / `cameras` 에 하드웨어 고유 식별자 컬럼(예: `hw_id`, 칩 MAC 기반) UNIQUE 추가 + pair 를 upsert 로 전환.
현재 요청 스키마에 하드웨어 식별자 필드가 **하나도 없어서**(`devices.py:41-52`, `cameras.py:47-60`) **펌웨어 변경이 반드시 동반됩니다.** 이 항목만 리드타임이 깁니다.

### 1.5 BLE 식별자 ↔ 서버 ID 연결 → 경로 없음

- BLE MAC, 광고 이름(`Terra-XXXX` / `Terra-Cam-XXXX`), 시리얼 등을 받는 필드가 요청 스키마에 없습니다.
- 서버는 BLE 광고명과 발급한 `device_id` 를 매핑하지 않습니다. 연결고리는 펌웨어가 BLE TX 로 되돌려주는 `PAIR_OK <device_id>` 문자열뿐이며, 이는 서버 계약 밖입니다.

### 1.6 `viva-iot-…` 표기 / 접두사

식별자 접두사는 **서버가 생성**합니다.

| 종류 | 형식 | 근거 |
|---|---|---|
| 디바이스 | `terra-` + 8 hex | `devices.py:130` |
| 카메라(esp32-p4) | `p4cam-` + 8 hex | `cameras.py:199-200` |
| 카메라(그 외 전부, `ip-camera` 포함) | `picam-` + 8 hex | `cameras.py:199` |

- 이 문자열이 곧 **MQTT client_id = username = ACL user** 라서, 접두사를 바꾸면 기존 기기의 브로커 계정·펌웨어 NVS 와 어긋납니다. **변경한다면 신규 기기부터만 적용 가능**합니다.
- BLE 광고 이름은 펌웨어 소관이며 서버와 무관합니다.
- **앱은 접두사로 기종을 판단하지 마세요.** `picam-` 은 RPi 전용이 아니라 "esp32-p4 가 아닌 모든 모델"에 붙습니다. 기종 판별은 `cameras.model` 을 쓰세요.

### 1.7 사전 등록 베타 vs 미등록 판매 기기

서버는 둘을 구분하지 않습니다. 등록 흐름은 §1.1 하나뿐이며, 사전 인입(pre-provisioning) 개념이 스키마·코드에 없습니다.

---

## 2. §1-2 — 삭제 버튼의 실제 동작과 기록 보존 ⛔

### 2.1 현재 동작: hard delete + cascade

앱 문서는 "삭제 = 등록 해제 및 그룹 연결 해제이며, R2 원본이나 과거 통계 원본을 삭제하는 기능이 아니다"라고 정의했습니다.
**현재 서버 구현은 그 반대입니다.**

`DELETE /devices/{id}` (`backend/routers/devices.py:257-280`) 는 DB 행을 실제로 삭제하고, FK cascade 로 아래가 **전부** 사라집니다.

| 동반 삭제 테이블 | FK 근거 |
|---|---|
| `device_settings` | `migrations/2026-05-26_initial_schema.sql:47` |
| `telemetry` (원본 센서값) | `initial_schema.sql:68` |
| `telemetry_1m` | `initial_schema.sql:92` |
| **`telemetry_30m` (장기 통계 정본)** | `migrations/2026-06-30_telemetry_30m_pgcron.sql:24` |
| `commands` (제어 이력) | `initial_schema.sql:117` |
| `alerts` (알림 이력) | `initial_schema.sql:146` |
| `schedules` (예약) | `migrations/2026-08-10_schedules.sql:12` |

`DELETE /cameras/{id}` (`cameras.py:347-370`) 는 **`motion_clips` 행 전체**를 cascade 삭제합니다 (`migrations/2026-05-26_camera_schema.sql:99`).

> 라우터 docstring(`devices.py:267`)과 `docs/API.md:204` 는 `telemetry_1m`·`telemetry_30m`·`schedules` 누락 상태입니다. 문서도 함께 고치겠습니다.

**R2 원본 파일은 남지만 DB 행이 없어져 접근 경로가 끊깁니다.** 실제로 이 상태(R2 에 mp4 있음 + DB 행 없음)가 발생한 적이 있고, 복구용으로 `scripts/reconcile_r2_orphans.py` 를 운영 중입니다. 또한 lifecycle rule 실측이 미확정이라(`scripts/check_r2_lifecycle.py`, `docs/BACKEND_HANDOFF_REPLY_2026-08-31.md:52-56`) 파일이 무기한 남을 수도 있습니다.

`clip_favorites` 는 FK 가 없어(`migrations/2026-07-08_clip_favorites.sql:19`) 카메라 삭제 후 고아 행으로 남습니다.

### 2.2 ✅ 좋은 소식 — 소유권 격리는 이미 요구대로입니다 (영상 한정)

- `motion_clips.owner_id` 는 **촬영 시점에 행에 박히는 독립 컬럼**이고 카메라 소유자를 따라가지 않습니다
  (`migrations/2026-05-26_camera_schema.sql:101`, 저장 지점 `backend/routers/clips.py:369`).
- RLS 도 `auth.uid() = owner_id` 기준입니다 (`camera_schema.sql:151-154`).
- **→ 카메라를 다른 계정에 재등록해도 이전 소유자의 과거 영상이 새 소유자에게 노출되지 않습니다.** 앱 요구와 일치합니다.
- 클립의 그룹 귀속도 촬영 시점 스냅샷입니다 (`clips.py:369`). 사후에 카메라를 다른 그룹으로 옮겨도 과거 클립 귀속이 바뀌지 않습니다 — §2-2 이력의 부분적 대용으로 쓸 수 있습니다.

### 2.3 ⚠️ 반대로 텔레메트리는 위험합니다

`telemetry` / `telemetry_30m` 에는 **소유자 컬럼이 없고** RLS 가 `devices.owner_id` 를 따라갑니다
(`initial_schema.sql:190-203`, `2026-06-30_telemetry_30m_pgcron.sql:41-47`).

**→ 소유권 이전(§1-3)을 구현하면 과거 센서 기록 전체가 새 소유자에게 그대로 보입니다.** 이전 기능을 만들 거면 이 부분 설계가 필수입니다.

### 2.4 ✅ 그룹 삭제는 안전합니다 (cascade 아님)

`DELETE /enclosures/{id}`(`backend/routers/enclosures.py:278-298`)는 구성원을 지우지 않고 연결만 끊습니다 — FK 가 전부 `ON DELETE SET NULL` 입니다 (`camera_schema.sql:44, 62, 100`).
앱 문서의 "그룹 삭제가 기기·영상·통계 연쇄 삭제로 이어지면 안 된다"는 이미 충족됩니다.

단, `motion_clips.enclosure_id` 도 NULL 이 되어 **과거 클립의 그룹 귀속이 사라집니다.** 이력 테이블(§2-2)이 그 손실을 메워야 합니다.

### 2.5 제안하는 보완 방향

1. **소프트 해제 신설.** `devices` / `cameras` 에 `unlinked_at TIMESTAMPTZ` 추가. `DELETE` 를 소프트 해제로 바꾸거나 `POST /devices/{id}/unlink` 를 별도 신설하고, 목록 조회에서 해제된 기기를 제외.
2. 해제된 기기의 MQTT 계정은 `registry.unregister_device()` 로 즉시 회수 (기존 로직 재사용, `devices.py:280`).
3. 과거 데이터 접근은 소유자 기준으로 유지 — 영상은 이미 맞고(§2.2), 텔레메트리는 소유자 스냅샷 컬럼 추가 검토.
4. **hard delete 는 남기되 앱 화면에서 노출하지 않기** — 운영/탈퇴 처리용으로만.

앱 §6 검증 시나리오 중 **"기기 해제 후 다른 계정에 등록 → 원본 유지"** 는 이 작업 전에는 통과할 수 없습니다.

---

## 3. §2-1 — 그룹·이름 규칙 ⛔

그룹(`enclosures`) 정의는 `migrations/2026-05-26_camera_schema.sql:20-37` 한 곳이며, **요청서가 전제한 규칙이 서버에 하나도 없습니다.**

| 앱 기획 | 서버 현재 | 근거 |
|---|---|---|
| 그룹 이름 같은 계정 내 중복 금지 | ❌ UNIQUE 제약 없음, 라우터 검사 없음 | 마이그레이션 전체 UNIQUE 는 `device_id`/`camera_id` 뿐 |
| 이름 최대 10자 | ❌ `max_length=64`, DB 는 `TEXT` 무제한 | `enclosures.py:32`, `devices.py:45`, `cameras.py:51` |
| 기본 이름 `사육 환경 1` / `사육장 1` 자동 부여 | ❌ 자동 생성 로직 없음 | — |
| 그룹당 사육장 1 + 카메라 1 + 개체 1 상한 | ❌ 상한 없음 (N:1 무제한) | — |
| 마지막 구성원 이탈 시 빈 그룹 제거 | ❌ 자동 삭제 없음 | `enclosures.py:278-298` 은 명시 호출만 |
| 같은 흐름 등록 시 자동 그룹화 | ❌ 없음. 전달받은 id 소유권만 확인 | `devices.py:127-128`, `cameras.py:195-196` |
| 개체 이름 중복 금지·10자 | ❌ `pets` 테이블 자체가 없음 (§4) | — |

### 3.1 동시 등록 원자성 → DB 제약이 유일한 해법

요청서의 "앱 사전 중복 검사만으로는 동시 등록을 막을 수 없다"는 정확한 지적입니다.
서버에 부분 유니크 인덱스를 걸어야만 보장됩니다.

```sql
-- 제안 (공백 정규화·대소문자 무시 기준은 앱과 합의 후 확정)
CREATE UNIQUE INDEX IF NOT EXISTS uq_enclosures_owner_name
    ON public.enclosures (owner_id, lower(btrim(name)));
```

- 라우터에서 위반 시 **409 Conflict** 반환으로 계약 신설 (현재 409 는 전체 0건).
- 기기·카메라 이름도 "사육장·카메라 전체를 대상으로 중복 확인"이라 **두 테이블에 걸친 제약**이 필요합니다. 단일 UNIQUE 인덱스로는 불가능하므로, 공용 이름 테이블을 두거나 트랜잭션 함수 안에서 검사해야 합니다. 이 부분은 §2-2 의 RPC 설계와 같이 가는 게 자연스럽습니다.
- **공백·허용 문자·비교 방식(대소문자, 유니코드 정규화)은 앱과 서버가 같은 규칙을 써야 합니다.** 앱 문서 제안대로 맞추겠습니다.

### 3.2 기기 ↔ 그룹 연결 (지원됨)

전용 배정 API 는 없고, 기기/카메라 쪽 PATCH 의 필드로 처리합니다. 계약 정본은 `docs/ENCLOSURE_MATCHING_API.md` 입니다.

| 경로 | 근거 |
|---|---|
| `PATCH /devices/{uuid}` `{"enclosure_id": "<uuid>"}` | `devices.py:213-254` |
| `PATCH /cameras/{uuid}` `{"enclosure_id": "<uuid>"}` | `cameras.py:288-324` |
| 페어링 시 동시 배정 | `devices.py:136`, `cameras.py:207` |

- 남의 그룹 id 면 **400** (`devices.py:87-101`, `cameras.py:158-169`).
- **해제하려면 `{"enclosure_id": null}` 을 명시 전송**해야 합니다. 필드 생략은 무변경입니다 (`exclude_unset` — `devices.py:235`, `cameras.py:303`).
- 그룹 쪽에서 멤버를 넣는 API(`POST /enclosures/{id}/devices` 같은 것)는 **없습니다.**

### 3.3 드롭다운용 조회는 이미 충분합니다

- `GET /enclosures` → 각 그룹에 `camera_count` / `device_count` 포함 (`enclosures.py:139-175`)
- `GET /enclosures/{id}` → 소속 `cameras[]` / `devices[]` nested (`enclosures.py:178-233`)
- 이름 변경·소속 이동·해제 후 최신 관계는 위 두 엔드포인트 재조회 또는 `enclosures` Realtime 구독으로 받습니다 (`camera_schema.sql:164`).

### 3.4 LCD 표시 문구 — 기기 이름과 완전히 분리돼 있습니다

- `devices` 에 LCD 컬럼이 **없습니다.** 마이그레이션 전체에 `lcd` 문자열 0건.
- 문구는 컬럼이 아니라 **명령**으로 전달됩니다 — `POST /devices/{id}/lcd`, `POST /devices/{id}/lcd/clear` (`backend/routers/lcd.py:61-98`).
- 서버 상한은 **64자**(`backend/lcd_render.py:34`)이고 렌더 시 폭 초과분은 자동 절단됩니다. 앱이 20자로 좁히는 건 문제없습니다.
- **현재 문구를 되읽는 GET 은 없습니다.** 영속은 펌웨어 NVS 쪽입니다. 앱이 현재값을 표시하려면 로컬 캐시가 필요합니다.
- 기기 이름이 LCD 에 자동 반영되는 경로는 없습니다. 요청서의 "별개" 전제와 일치합니다.

---

## 4. §2-2 — 개체↔카메라 연결 이력

### 4.1 ✅ 전제 확인: 앱 팀이 새로 설계하는 게 맞습니다

| 확인 항목 | 결과 |
|---|---|
| `pets` 테이블·라우터 | **없음** (서면 재확인: `docs/BACKEND_HANDOFF_REPLY_2026-08-31.md:34`) |
| `assign_pet_to_enclosure` | **없음** |
| `pet_camera_assignments` 등 이력 테이블 | **없음** |
| Supabase RPC 사용 | **레포 전체 `.rpc()` 호출 0건** |
| 마이그레이션의 `CREATE FUNCTION` | `public.set_updated_at()` **단 하나** (`initial_schema.sql:249`) |

현재 "개체"는 `enclosures.species` / `devices.species` 라는 TEXT 필드로만 표현됩니다. 1급 도메인 개념이 없습니다.

### 4.2 관계를 바꾸는 **모든 쓰기 경로** (요청 §2-2-1)

| # | 주체 | 경로 |
|---|---|---|
| 1 | 서버 | `PATCH /devices/{uuid}` — `devices.py:213-254` |
| 2 | 서버 | `PATCH /cameras/{uuid}` — `cameras.py:288-324` |
| 3 | 서버 | `POST /devices/pair` — `devices.py:136` |
| 4 | 서버 | `POST /cameras/pair` — `cameras.py:207` |
| 5 | 웹 콘솔 | `assignEnclosure()` — `web/index.html:613-622` (위 1·2 를 호출) |

전부 `enclosure_id` 단일 필드만 건드립니다. 앱은 1·2 만 쓰면 됩니다.

### 4.3 ⚠️ 트랜잭션 수단이 서버에 없습니다 (요청 §2-2-3)

terra-server 는 supabase-py 로 **단건 호출만** 합니다. 다중 테이블 원자 갱신 수단이 코드에 없습니다.

**→ "그룹 이동 + 이력 종료 + 이력 시작"을 한 번에 처리하려면 Postgres 함수(RPC)로 감싸는 방법뿐입니다.**
앱 팀이 SQL/RPC 초안을 작성하고 terra-server·웹 호출부를 거기에 맞추는 분장에 동의합니다.

### 4.4 ⚠️ 우회 경로 차단은 RLS 변경이 필요합니다 (요청 §2-2-4)

현재 RLS 는 기기 행의 **모든 컬럼 수정**을 허용합니다 (`initial_schema.sql:170-171` "own devices update", 카메라 동일 `camera_schema.sql:142-143`).
즉 앱이 RPC 를 안 거치고 `enclosure_id` 를 직접 UPDATE 할 수 있습니다.

강제하려면 둘 중 하나가 필요합니다.
- (a) `enclosure_id` UPDATE 를 RLS/트리거로 막고 RPC(SECURITY DEFINER)만 허용
- (b) 관계 변경 시 이력을 갱신하는 **DB 트리거**를 걸어 경로와 무관하게 보장

terra-server 는 service_role 로 접속하므로 (a) 만으로는 서버 경로를 못 막습니다. **(b) 트리거 방식을 권장**합니다. 서버·웹·앱 어느 경로로 바뀌어도 이력이 남습니다.

### 4.5 참고: 현재 있는 "이력 비슷한 것"

클립 생성 시 카메라의 그룹을 스냅샷 저장합니다 (`clips.py:369`). 과거 클립의 귀속은 사후 이동에 영향받지 않습니다. 새 이력 테이블 설계 시 이 값과 충돌하지 않게 봐주세요.

---

## 5. §3 — 정확한 과거 온습도 일평균

### 5.1 현재 제공되는 것

장기 추이 정본은 **`telemetry_30m`** 입니다 (`migrations/2026-06-30_telemetry_30m_pgcron.sql:26-38`).
REST 엔드포인트는 없고 **Supabase PostgREST 직접 SELECT** 가 계약입니다 (`docs/APP_TIMESERIES_CHART.md` 정본).

| 요청 항목 | 제공 | 컬럼 |
|---|---|---|
| 온도 평균/최소/최대 | ✅ | `t_a_avg` / `t_a_min` / `t_a_max` (B 센서는 `t_b_*`) |
| 습도 평균/최소/최대 | ✅ | `h_a_avg` / `h_a_min` / `h_a_max` (`h_b_*`) |
| **지표별 유효 표본 수** | ❌ | 없음 |
| 버킷 시각 | ✅ | `bucket` |

### 5.2 ⚠️ `sample_count` 는 유효 표본 수가 아닙니다

집계 쿼리의 해당 항은 `count(*)` 입니다 — `2026-06-30_telemetry_30m_pgcron.sql:73`. 즉 **버킷 안의 원본 행 수**입니다.

- 온도 센서만 죽어 `t_a` 가 전부 NULL 이어도 `sample_count` 는 그대로입니다.
- A 센서만 고장난 구간도 값이 동일합니다.
- **`sample_count` 를 가중치로 쓴 가중평균은 지표별로 부정확합니다.** 요청서의 `(20×1 + 30×3) / 4 = 27.5` 계산은 "지표별 유효 표본 수"를 전제하는데, 그 값이 현재 없습니다.

### 5.3 결측·이상치 규칙

- **행 단위로 버리지 않습니다.** WHERE 절은 기간 조건뿐이고(`:86`), Postgres `avg`/`min`/`max` 가 NULL 을 자동 무시합니다.
  **→ 온도·습도, A·B 센서는 각각 독립적으로 집계됩니다.** 요청 §3-3 의 전제는 맞습니다.
- **⚠️ 센서 fault 값이 필터 없이 섞입니다.** `docs/MQTT.md:61` 은 "fault 시 `ok:false`, `t/h` 값은 무의미"라고 정의하는데, 수집부가 `ok` 와 무관하게 값을 그대로 INSERT 하고(`backend/mqtt/handlers.py:333-338`) 롤업이 `a_ok` 를 보지 않습니다. 이건 별개의 실질 버그로 같이 고치겠습니다.
- 범위 검증·스파이크 제거 같은 이상치 로직은 없습니다.

### 5.4 버킷 경계와 타임존

- 경계는 **UTC epoch 기준 정시/30분** 입니다 (`:76`). KST 는 오프셋 고정이라 벽시계로도 정시에 떨어집니다.
- **⚠️ "일 단위"로 묶을 때만 문제입니다.** 그대로 날짜 절단하면 UTC 자정 기준이 되어 KST 와 9시간 어긋납니다.
  **KST 일평균은 앱에서 `bucket + 9h` 로 날짜를 만든 뒤 묶어주세요.**
- 수집 시각 정규화는 `backend/mqtt/handlers.py:243-257` (epoch ms/s 판별, 불명확하면 서버 시각 폴백).

### 5.5 ⛔ 과거 지원 범위 — 원본은 7일만 보존됩니다

| 대상 | 보존 | 근거 |
|---|---|---|
| `telemetry` (3초 원본) | **7일** 후 자동 삭제 | pg_cron `cleanup-telemetry-7d`, `2026-06-30_...sql:105-112` |
| `telemetry_30m` | 사실상 영구 (삭제 cron 없음) | `2026-06-30_...sql:21-23` |
| `telemetry_1m` | 무의미 — **채우는 작업이 없어 영구히 빈 테이블** | `specs/stage-e-timeseries-downsample.md:1` (보류) |

**→ 지표별 유효 표본 수는 과거 복원이 불가능합니다.** 요청서 방침대로 **null/미지원**으로 남기고 앱은 `--` 처리해 주세요.

### 5.6 보완안 (작업량 작음)

`telemetry_30m` 에 지표별 카운트를 추가하고 집계를 고치면 됩니다.

```sql
ALTER TABLE public.telemetry_30m
    ADD COLUMN IF NOT EXISTS t_a_count INT,
    ADD COLUMN IF NOT EXISTS h_a_count INT,
    ADD COLUMN IF NOT EXISTS t_b_count INT,
    ADD COLUMN IF NOT EXISTS h_b_count INT;
-- cron 본문: count(*) → count(t_a) FILTER (WHERE a_ok) 형태로 지표별 분리
```

- 마이그레이션 1건 + cron 재등록 규모입니다.
- **적용 시점 이후 버킷부터만 유효**합니다. 소급 불가(§5.5).
- 기존 `sample_count` 는 호환을 위해 유지합니다.

### 5.7 ⚠️ 기존 앱 계약 정정 필요

- `docs/APP_TIMESERIES_CHART.md` 가 **`sample_count < 300` 이면 부분 데이터**로 표시하라고 약속했는데, §5.2·§5.3 때문에 실제 신뢰도와 어긋납니다. A 센서가 통째로 fault 여도 앱은 "정상"으로 그립니다. 이 규약은 지표별 카운트 도입 후로 미뤄주세요.
- **`docs/API.md:385-397`, `docs/DATABASE.md:83`, `docs/ARCHITECTURE.md:88` 이 아직 `telemetry_1m` 을 "1분 평균 1년 보관"으로 안내합니다.** 실제로는 영구 빈 테이블이라 붙으면 빈 차트가 나옵니다. 이 세 문서는 저희가 정정하겠습니다. **앱은 `telemetry_30m` 만 쓰세요.**

### 5.8 ✅ 제어 기록 화면 관련

요청서가 확인한 대로, 제어 시점 스냅샷 저장이나 상태 변화량 기능은 이번 범위 밖으로 두는 데 동의합니다. 기존 30분 평균 근사치 사용에 서버측 변경은 없습니다.

---

## 6. §4·§5 — coverage 원장, 추가 개발 없는 항목

### 6.1 §4 카메라 관측 coverage ⛔ 제공 불가

- **촬영 성공/실패 원장이 없습니다.** `motion_clips` 는 업로드에 성공한 것만 기록합니다 (`backend/routers/clips.py:345-402`).
- `cameras.last_seen_at` / `is_online` 은 **현재 상태만** 있고 이력 테이블이 없습니다.
- **⚠️ 카메라가 보낸 알림은 저장조차 되지 않습니다.**
  `alerts.device_id` 가 `devices` 만 참조하고(`initial_schema.sql:141`), `handle_alert` 가 카메라 식별자를 풀지 못해 "미페어링"으로 폐기합니다 (`backend/mqtt/handlers.py:443-459`).
  브로커 ACL 에는 카메라 alert 쓰기 권한이 있어서(`backend/mqtt/registry.py:158`) 카메라는 보내고 있는데 서버가 버리는 상태입니다.
- 오프라인 판정은 3분 무응답 기준으로 상태만 갱신하고 카메라용 alert 는 만들지 않습니다 (`backend/offline_monitor.py:5-6, 39-47`).

**→ 과거 날짜의 관측 커버리지를 terra-server 가 제공할 수 없습니다.** petcam-lab 과 합의하시는 방향에 동의하며, 필요하면 카메라 alert 저장 경로 신설을 별도 과제로 잡겠습니다.

### 6.2 §5 항목별 확인 — 계약 일치

| 항목 | 확인 |
|---|---|
| 냉각팬 `fan2` | ✅ `docs/APP_FAN2_2026-09-07.md` 계약 그대로. 구기기 응답 차이 없음 |
| LED 밝기 20~100%·10% 단위 | ⚠️ **서버는 0~100 을 그대로 통과시킵니다.** 하한 20% 는 펌웨어 정책이라 서버가 20 미만을 거부하지 않습니다. 앱 UI 에서 제한해 주세요 |
| 분무 3초 | ✅ `duration_ms` 화이트리스트가 1000/2000/3000 (`backend/command_service.py:36-42`) |
| LCD 20자 | ✅ 서버 상한 64자 이내라 무방 (§3.4) |
| 전원 버튼 ON 고정 | ✅ 서버에 전원 제어 action 이 없습니다. 맞습니다 |
| 즐겨찾기 `clip_favorites` | ✅ 존재 (`migrations/2026-07-08_clip_favorites.sql`) |
| **영상 삭제 버튼 앱측 숨김** | ⚠️ **반드시 지켜주세요.** `DELETE /clips/{id}` 는 R2 객체와 DB 행을 **실제로** 삭제합니다 (`clips.py:654`). 사용자별 숨김용으로 호출하면 안 됩니다 |
| 영상 메모 로컬 저장 | ✅ 서버 변경 없음 |
| FCM 요약·타이머 ACK | ✅ 기존 문서 계약 유지 |

### 6.3 §6 검증 시나리오 현재 판정

| 시나리오 | 현재 서버 | 통과 조건 |
|---|---|---|
| Wi-Fi 성공 후 claim 실패 → 재시도 | ⛔ 재시도 시 좀비 행 생성 | §1.4 멱등성 |
| 둘 중 하나만 등록 성공 → 중복 없음 | ⛔ 보장 불가 | §1.4 |
| 동시 등록 시 이름 중복 방지·그룹 상한 | ⛔ 제약 없음 | §3.1 |
| 그룹 이동 중 오류·동일 요청 재시도 | ⛔ 트랜잭션 수단 없음 | §4.3 |
| 카메라 교체·기존 사용자 승계 | ⚠️ 이력 테이블 신설 후 가능 | §4 |
| 기기 해제 후 타 계정 등록 → 원본 유지 | ⛔ 해제가 곧 삭제 | **§2.5 (최우선)** |
| 가중평균 27.5 · 온습도 독립 | ⚠️ 독립 집계는 OK, 유효 표본 수 없음 | §5.6 |

---

## 7. 우선순위와 담당

영향도 순입니다. 위에서부터 처리하겠습니다.

| # | 작업 | 담당 | 규모 | 비고 |
|---|---|---|---|---|
| 1 | **소프트 해제 도입** (§2.5) | terra-server | 중 | 다른 항목의 선행 조건 |
| 2 | **이름 UNIQUE 제약 + 409** (§3.1) | terra-server | 소 | 비교 규칙만 앱과 합의 |
| 3 | **롤업 지표별 표본 수 + fault 필터** (§5.6) | terra-server | 소 | 소급 불가 |
| 4 | **개체·이력 스키마/RPC/트리거** (§4) | 앱 팀 작성 → 백엔드 검토·적용 | 중 | 트리거 방식 권장 |
| 5 | **페어링 멱등성** (§1.4) | terra-server + 펌웨어 | 대 | 하드웨어 식별자 필요, 리드타임 김 |
| 6 | 카메라 alert 저장 경로 (§6.1) | terra-server | 중 | coverage 는 petcam-lab 과 별도 합의 |
| 7 | 문서 정정 (`API.md`/`DATABASE.md`/`ARCHITECTURE.md`/`APP_TIMESERIES_CHART.md`) | terra-server | 소 | §5.7 |

### 앱 팀에 부탁드리는 것

1. **이름 비교 규칙 확정** — 공백 처리(trim/중간공백), 대소문자, 유니코드 정규화. 서버 인덱스를 거기에 맞춥니다.
2. **기기 이름 중복 범위 확인** — "사육장·카메라 전체 대상"이면 두 테이블에 걸친 제약이라 RPC 안에서 검사해야 합니다 (§3.1).
3. **`telemetry_1m` 을 쓰지 않기** — 영구 빈 테이블입니다 (§5.7).
4. **영상 삭제 버튼에서 `DELETE /clips/{id}` 를 호출하지 않기** (§6.2).
5. **`PAIR_OK` 로 등록 완료 판정** — `WIFI_OK` 와 구분 (§1.2).

### 참고 (오류 코드 계약 정정)

`docs/API.md:665` 는 403 을 "권한 없음"으로 안내하지만, **실제 구현은 소유권 위반을 전부 404(조회·수정·삭제) 또는 400(그룹 배정)으로 응답**합니다. 라우터에 403 이 0건입니다. 앱이 403 을 기대하지 마세요. 문서는 저희가 고치겠습니다.
