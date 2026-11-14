# Stage V — 카메라 네트워크 신호 약함 안내 Implementation Plan

> 상태: 📝 **계획 — 승인 전** (2026-11-13). 구현·배포 없음.
>
> **구현 방식 (CAOF):** 이 계획을 task 단위로 구현한다. Critical 트랙이라 Implementer 에이전트 분리(GATE 4). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 카메라 WiFi 신호가 −75dBm 이하로 10분 넘게 이어지면 그 카메라 화면 막대에 "네트워크 신호가 약하니 점검해 주세요"를 띄우고, −70dBm 이상으로 회복하면 내린다.

**Architecture:** 브리지가 카메라 heartbeat(15초)의 `wifi_rssi` 를 받을 때마다 카메라별 "약함 지속" 상태를 메모리에서 갱신한다. 조건이 차면 초점 안내와 같은 `app_notifications` 에 직접 INSERT 하고(푸시 outbox 를 타지 않음), 회복하면 안 읽은 행의 `read_at` 을 채운다. 24시간 제한은 DB 조회로 지켜서 브리지 재시작에도 유지된다.

**Tech Stack:** Python 3.12, paho-mqtt 브리지, supabase-py(service_role), pytest + `tests/fake_sb.py`

**Spec:** 사용자 결정(2026-11-13 대화) + 기준 공유문("WiFi 신호 약함 알림 기준"). 앱 계약(안): [docs/APP_CAMERA_WEAK_SIGNAL_2026-11-13.md](../docs/APP_CAMERA_WEAK_SIGNAL_2026-11-13.md). 선례: `docs/OPERATIONS.md` 1-3 초점 안내.

## 가정

- 확인:
  - 카메라 heartbeat 는 15초 주기, `wifi_rssi` 는 `_sys_state` 가 `sys_state["rssi"]`(int)로 파싱한다 (`backend/mqtt/handlers.py:690`, `docs/MQTT.md:112`).
  - 카메라 분기는 `handle_telemetry` 안 `handlers.py:984-1060`, `_write_heartbeat` 호출이 1032줄, `_ota_verify_from_heartbeat` 호출이 1037-1039줄.
  - 브리지는 프로세스 1개, MQTT 수신은 paho `loop_start` 단일 스레드라 heartbeat 는 직렬로 처리된다(`backend/mqtt/bridge.py`). 그래서 트래커에 락을 두지 않고, 조회 후 INSERT 사이 경합도 없다.
  - `app_notifications` 스키마(앱 레포 `supabase/migrations/20260915000000_fcm_notifications.sql`): `kind/category/title/body/route/source/source_event_id/dedupe_key` 모두 `TEXT NOT NULL`, `data` 는 object CHECK, `UNIQUE(user_id, dedupe_key)`. `kind`·`category`·`source` 에 CHECK 없음. Realtime publication 에 들어 있음.
  - 앱의 초점 안내는 이미 카메라 단위다(앱 `focus_notice_providers.dart`): 안 읽은 `camera.focus_needed` 행의 `data.camera_id` 가 가리키는 카메라 라이브 위에만 막대를 그리고, 그 카메라 것만 읽음 처리한다. 안 읽은 행 목록을 구독하므로 서버가 `read_at` 을 채우면 목록에서 빠진다.
  - 서버 코드 어디도 `app_notifications` 에 쓰지 않는다(grep). 초점 안내는 운영자 SQL.
  - `cameras.owner_id`, `cameras.superseded_at`, `cameras.unlinked_at` 컬럼이 있다.
  - `tests/fake_sb.py` 는 eq / is_ / in_ / gte / order / limit / insert / update 를 지원한다. JSON 경로 필터는 없다.
- 미확인:
  - 서버가 `read_at` 을 채웠을 때 앱 막대가 실제로 내려가는지(구조상 그럴 것으로 보이나 실기기 미확인). → 켜는 순서 3의 수동 확인.
  - 같은 `camera_id` 텍스트가 계정 이전 뒤 옛 행·새 행 양쪽에 활성으로 남는 동안 `_lookup_active_uuid` 가 어느 행을 잡는지. 옛 행이 잡히면 그 24시간 동안 옛 소유자에게 안내가 갈 수 있다(Stage U 의 supersede 가 정리).
  - 운영 DB 에서 지금 −75 이하인 카메라 수. → 드라이런 로그로 확인(검증 방법 3).

## 선택지와 추천

| 결정 | 선택지 | 추천 |
|---|---|---|
| 전달 경로 | A. `app_notifications` 직접 INSERT / B. `push_outbox` 푸시 | **A** — "막대만, 푸시 없음" 결정. 초점 안내와 같은 경로 |
| 지속 판정 위치 | A. 브리지 메모리 / B. RSSI 이력 테이블 | **A** — 마이그레이션 없음. 재시작하면 10분을 다시 셀 뿐 |
| 24시간 제한 | A. 메모리 / B. 발송 직전 DB 조회 | **B** — 재시작 뒤에도 유지 |
| heartbeat 공백 | A. 공백이면 즉시 초기화 / B. 300초까지 이어진 것으로 봄(처음 120초 → 코드 리뷰 뒤 300초) | **B** — 신호가 아주 약한 카메라는 heartbeat 가 빠져서 A 면 제일 심한 집이 안내를 못 받는다 |
| 켜는 방식 | A. 스위치 1개 / B. 스위치 + 드라이런(기본 켜짐) | **B** — `AUTO_ENV_*` 와 같은 패턴. 대상 수를 보고 켠다 |

## 실패 시나리오

| 상황 | 결과 | 대응 |
|---|---|---|
| 카메라가 아예 오프라인 | RSSI 가 안 와서 안내 없음. 이미 뜬 막대는 누를 때까지 남음 | 범위 밖(오프라인 안내는 별개) |
| 9/28 이전 펌웨어 | `wifi_rssi` 없음 → 판정 안 함 | 없음 |
| 브리지 재시작 | 약함 지속 시간이 0부터 다시. 중복 발송은 DB 조회가 막음 | 재시작 뒤 카메라별 첫 "−70 이상" 샘플에서 해제를 1회 시도해 남은 막대를 내림 |
| INSERT 실패(제약 위반 등) | `failed` 로그, heartbeat 처리는 계속 | 10분 뒤 재시도 |
| 대상이 너무 많음 | 사용자 다수에게 막대 | 드라이런 로그로 먼저 확인, `NET_WEAK_NOTICE_ENABLED=false` 로 즉시 중단 |
| 안내가 뜬 카메라를 해제하거나 다른 계정이 가져감 | 그 행은 heartbeat 를 더 받지 않아 서버가 내릴 수 없음. 누르면 사라짐 | 범위 밖 |
| 해제 UPDATE 실패 | 막대가 남음 | 상태를 "모름"으로 되돌려 다음 −70 이상 샘플에서 다시 시도 |
| 앱 구버전 | 막대 없이 알림함에 "일반 알림" | 앱 확인 사항(요청서 3-5) |

## 유저 체험 시뮬레이션

1. 카메라가 공유기에서 멀어 신호가 10분 넘게 약하다.
2. 앱에서 그 카메라 화면을 열면 영상 위 막대(초점 안내 자리)에 "네트워크 신호가 약하니 점검해 주세요"가 보인다. 푸시는 오지 않는다. 다른 카메라 화면에는 안 보인다.
3. 카메라를 공유기 가까이 옮겨 신호가 −70 이상으로 올라오면 막대가 저절로 사라진다.
4. 옮기지 않고 막대를 눌러 닫았다면, 신호가 계속 약할 때 **안내가 처음 뜬 시각부터** 24시간이 지난 뒤 다시 뜬다(닫은 시각 기준이 아님).
5. 누르지도 고치지도 않으면 막대는 그대로 한 개만 남아 있다(쌓이지 않음).

## 근본 원인·전후 동작

없음 (신규 기능).

## 검증 방법

1. `uv run pytest tests/test_net_weak.py tests/test_mqtt_handlers.py -q` → 전부 통과.
2. `uv run pytest -q` → 기존 테스트 회귀 없음.
3. 운영(이관훈님 배포 뒤): `NET_WEAK_NOTICE_ENABLED=true` + 드라이런 상태로 2~3일. `journalctl -u terra-bridge | grep "net-weak intent"` 로 대상 카메라 수 확인. 드라이런은 행을 남기지 않아 브리지 재시작마다 같은 카메라가 다시 찍히므로 **카메라 중복을 빼고** 센다.
4. 실발송: 테스트 계정 카메라 1대로 막대 표시·자동 해제·24시간 제한 확인 — **사용자만 확인 가능**(앱 화면).

## 사용자가 정할 것

1. `body` 문구 — 추천: `공유기와 카메라 사이 거리나 장애물을 확인해 주세요`. 막대에는 `title` 만 보일 것으로 본다(앱 확인 중).
2. `kind` 이름 — 추천: `camera.weak_signal`. 앱과 합의 후 확정.
3. **약함 지속을 끊는 기준** — 지금 계획은 기준 원문대로 −74 이상 샘플이 하나만 와도 10분을 처음부터 다시 센다(`STREAK_BREAK_DBM = -75`). RSSI 는 보통 ±3dB 흔들려서 평균 −76~−77 인 카메라는 거의 발송되지 않고 실효 기준이 −80 근처가 될 수 있다. 추천: **−70 이상이 올 때만 끊기**(해제 기준과 같은 선). 상수 하나라 드라이런 결과를 보고 바꿔도 된다. → **2026-11-14 코드 리뷰 반영으로 추천안 적용**(`STREAK_BREAK_DBM = -71`, 맨 아래 "코드 리뷰 반영" 참고).

## Global Constraints

- 문구(title): `네트워크 신호가 약하니 점검해 주세요` — 글자 그대로.
- 기준: 약함 `rssi <= -75`, 지속 `600`초 이상("10분 넘게"를 10분째 샘플에서 발송으로 구현 — 15초 주기라 차이는 최대 15초), 해제 `rssi >= -70`, 반복 제한 카메라당 `86400`초에 1회.
- heartbeat 공백 `120`초 초과면 약함 지속을 처음부터 다시 센다.
- 대상은 카메라만. 사육장 기기(`devices`) 분기는 건드리지 않는다.
- 푸시(`push_outbox`, `notification_events`)로 보내지 않는다.
- `service_role` 조회·수정에는 `user_id`(= `cameras.owner_id`) 필터를 명시한다.
- 스위치: `NET_WEAK_NOTICE_ENABLED` 기본 꺼짐(`true/1/yes/on` 만 켬), `NET_WEAK_NOTICE_DRY_RUN` 기본 켜짐(`0/false/no/off` 만 끔).
- 안내 처리 중 예외가 heartbeat 처리(온라인 표시)를 막으면 안 된다.
- 의존성 추가 없음. 마이그레이션 없음.
- 건드리지 않는 것: `backend/push_events.py`, `backend/alerts.py`, `backend/ota_service.py` 의 −75 게이트, `_sys_state` 파싱, 앱의 기존 1분 화면 안내, 초점 안내 운영 절차.

## Review Focus

1. **브리지 재시작 뒤 회복** — 안 읽은 안내가 남은 채 재시작하고 신호가 회복되면 막대가 내려가야 한다. → Task 1 `test_unknown_state_clears_once_on_good_sample`.
2. **카메라 2대 소유자** — A 카메라 안내가 B 카메라 발송을 막거나, A 회복이 B 막대를 내리면 안 된다. → Task 2 `test_other_camera_not_blocked_nor_cleared`.
3. **heartbeat 가 빠지는 약한 카메라** — 30~120초 공백이 섞여도 10분이 이어져야 한다. → Task 1 `test_gap_within_120s_keeps_streak`.
4. **해제 UPDATE 실패** — 회복 시 DB 오류가 나면 막대가 영영 남으면 안 된다. 다음 회복 샘플에서 다시 시도해야 한다. → Task 3 `test_clear_failure_retries_on_next_good_sample`.
5. **INSERT 실패** — 실패해도 heartbeat UPDATE 는 이미 끝나 있어야 하고 예외가 밖으로 나가면 안 된다. → Task 3 `test_notice_error_does_not_break_heartbeat`.

---

### Task 1: 약함 지속 판정 `WeakTracker`

**Files:**
- Create: `backend/net_weak.py`
- Test: `tests/test_net_weak.py`

**Context:**
- Depends on: 없음
- Consumes: 없음
- Produces:
  - 상수 `WEAK_DBM = -75`, `RECOVER_DBM = -70`, `HOLD_SEC = 600`, `GAP_RESET_SEC = 120`, `RETRY_SEC = 600`, `REPEAT_SEC = 86400`
  - 상수 `STREAK_BREAK_DBM = -75` (이 값 **초과** 샘플이 약함 지속을 끊는다. "사용자가 정할 것" 3번에 따라 `-70` 이상으로 바뀔 수 있음)
  - `class WeakTracker` — `observe(camera_uuid: str, rssi: int, now: float) -> Literal["send", "clear"] | None`, `mark_sent(camera_uuid: str, now: float) -> None`, `set_active(camera_uuid: str, value: bool | None) -> None`, `reset() -> None`
- Must know:
  - `now` 는 호출자가 넘기는 단조 시각(초). 클래스 안에서 시계를 읽지 않는다(테스트가 시간을 직접 준다).
  - 카메라별 상태 4개: `weak_since`(약함 시작), `last_weak_at`(마지막 약함 샘플), `next_try`(다음 발송 시도 가능 시각, 초기 0), `active`(`None`=모름, `True`=안내 떠 있음, `False`=없음).
  - 규칙:
    - `rssi <= -75`: 진행 중인 약함이 없거나 `now - last_weak_at > 120` 이면 `weak_since = now`. `last_weak_at = now`. `now - weak_since >= 600` 이고 `now >= next_try` 면 `next_try = now + 600` 으로 두고 `"send"`.
    - `rssi > -75`: `weak_since = None`. 추가로 `rssi >= -70` 이고 `active` 가 `False` 가 아니면 `active = False` 로 두고 `"clear"`.
    - `mark_sent`: `active = True`, `next_try = now + 86400`.
    - `set_active`: `active` 만 바꾼다(`next_try` 는 그대로). 호출자가 "안 읽은 안내가 있다"(`True`)·"해제 실패, 다시 시도"(`None`)를 알릴 때 쓴다.
  - −74~−71 은 약함 지속을 끊지만 해제는 아니다(기준 원문 그대로).
  - 락 없음(가정: 수신 스레드 1개).
- Acceptance: `uv run pytest tests/test_net_weak.py -q` 전부 통과.

- [x] **Step 1: 실패하는 테스트 작성** — `tests/test_net_weak.py`

```python
CAM = "cam-1"

def _feed(t, rssi, start, end, step=15):
    out = []
    for now in range(start, end + 1, step):
        out.append(t.observe(CAM, rssi, float(now)))
    return out

def test_tracker_sends_after_600s_of_weak():
    t = WeakTracker()
    assert set(_feed(t, -75, 0, 585)) == {None}
    assert t.observe(CAM, -80, 600.0) == "send"

def test_tracker_no_repeat_within_retry_window():
    t = WeakTracker()
    _feed(t, -80, 0, 600)
    assert set(_feed(t, -80, 615, 1185)) == {None}
    assert t.observe(CAM, -80, 1200.0) == "send"      # RETRY_SEC 뒤 재시도

def test_tracker_mark_sent_blocks_for_24h():
    t = WeakTracker()
    _feed(t, -80, 0, 600)
    t.mark_sent(CAM, 600.0)
    assert set(_feed(t, -80, 615, 86985)) == {None}    # 600 + 86400 = 87000 전까지
    assert t.observe(CAM, -80, 87000.0) == "send"

def test_tracker_minus_74_resets_streak_but_does_not_clear():
    t = WeakTracker()
    _feed(t, -80, 0, 300)
    assert t.observe(CAM, -74, 315.0) is None          # 해제 아님(-70 미만)
    assert set(_feed(t, -80, 330, 915)) == {None}      # 330 부터 다시 600초
    assert t.observe(CAM, -80, 930.0) == "send"

def test_tracker_clears_at_minus_70_once():
    t = WeakTracker()
    t.mark_sent(CAM, 0.0)
    assert t.observe(CAM, -71, 15.0) is None
    assert t.observe(CAM, -70, 30.0) == "clear"
    assert t.observe(CAM, -60, 45.0) is None

def test_unknown_state_clears_once_on_good_sample():
    t = WeakTracker()                                  # 재시작 직후
    assert t.observe(CAM, -55, 0.0) == "clear"
    assert t.observe(CAM, -55, 15.0) is None

def test_set_active_none_makes_clear_fire_again():
    t = WeakTracker()
    assert t.observe(CAM, -60, 0.0) == "clear"
    t.set_active(CAM, None)                            # 해제 실패를 알림
    assert t.observe(CAM, -60, 15.0) == "clear"

def test_gap_within_120s_keeps_streak():
    t = WeakTracker()
    t.observe(CAM, -80, 0.0)
    for now in (120.0, 240.0, 360.0, 480.0):
        assert t.observe(CAM, -80, now) is None
    assert t.observe(CAM, -80, 600.0) == "send"

def test_gap_over_120s_restarts_streak():
    t = WeakTracker()
    _feed(t, -80, 0, 585)
    assert t.observe(CAM, -80, 706.0) is None          # 585 → 706 = 121초 공백, 706 부터 다시
    assert set(_feed(t, -80, 721, 1291)) == {None}
    assert t.observe(CAM, -80, 1306.0) == "send"
```

- [x] **Step 2: 실패 확인** — `uv run pytest tests/test_net_weak.py -q` → `ModuleNotFoundError: backend.net_weak`
- [x] **Step 3: `backend/net_weak.py` 에 상수와 `WeakTracker` 구현** (위 Must know 규칙 그대로)
- [x] **Step 4: 통과 확인** — `uv run pytest tests/test_net_weak.py -q`
- [ ] **Step 5: Commit** — `net_weak: 카메라 신호 약함 지속 판정(WeakTracker)`

### Task 2: 안내 넣기·내리기 (`app_notifications`)

**Files:**
- Modify: `backend/net_weak.py`
- Test: `tests/test_net_weak.py`

**Context:**
- Depends on: Task 1
- Consumes: Task 1 상수
- Produces:
  - 상수 `KIND = "camera.weak_signal"`, `TITLE = "네트워크 신호가 약하니 점검해 주세요"`, `BODY = "공유기와 카메라 사이 거리나 장애물을 확인해 주세요"`
  - `enabled() -> bool`, `dry_run() -> bool`
  - `send_notice(sb, camera_uuid: str, rssi: int) -> Literal["queued", "recent", "unread", "no_owner", "dry_run", "failed"]`
  - `clear_notices(sb, camera_uuid: str) -> int | None` (읽음 처리한 행 수. DB 오류면 `None`)
- Must know:
  - 소유자 조회: `cameras` 에서 `owner_id, superseded_at, unlinked_at` 을 `id = camera_uuid` 로. 행이 없거나 `superseded_at`·`unlinked_at` 중 하나라도 값이 있으면 `"no_owner"`.
  - 중복 판정은 조회 2번, 둘 다 `user_id = owner`, `kind = KIND` 필터에 **limit 없이**: ① `read_at IS NULL` ② `created_at >= 지금 − 24시간`. 각각 **Python 에서** `data["camera_id"] == camera_uuid` 로 거른다(FakeSB·PostgREST 양쪽에서 같은 코드가 돌도록 JSON 경로 필터를 쓰지 않는다). ①에 행이 있으면 `"unread"`, 아니고 ②에 있으면 `"recent"`.
  - 드라이런이면 위 판정까지 한 뒤 INSERT 대신 `logger.info("net-weak intent camera=%s rssi=%s", ...)` 를 남기고 `"dry_run"`.
  - INSERT 값(전문 — 앱 계약):

    | 컬럼 | 값 |
    |---|---|
    | `user_id` | `cameras.owner_id` |
    | `kind` | `camera.weak_signal` |
    | `category` | `camera` |
    | `title` | `TITLE` |
    | `body` | `BODY` |
    | `route` | `/crecam` |
    | `data` | `{"kind": "camera.weak_signal", "source": "external", "camera_id": <camera_uuid>, "rssi": <rssi>}` |
    | `source` | `external` |
    | `source_event_id` | `str(uuid4())` |
    | `dedupe_key` | `"net-weak:" + camera_uuid + ":" + 같은 uuid` |

  - `clear_notices`: 드라이런이면 DB 를 건드리지 않고 0. 아니면 같은 방식으로 소유자의 `kind = KIND`, `read_at IS NULL` 행을 읽어 Python 에서 카메라로 거른 뒤 `id` 목록으로 `read_at = 지금(UTC ISO)` UPDATE. 조회·UPDATE 가 실패하면 `None`.
  - DB 예외는 `postgrest.exceptions.APIError`, `httpx.HTTPError` 만 잡아 `send_notice` 는 `"failed"`, `clear_notices` 는 `None` 을 돌려준다(bare except 금지). 그 밖의 예외는 Task 3 의 handlers 쪽 바깥 가드가 받는다.
- Acceptance: `uv run pytest tests/test_net_weak.py -q` 전부 통과.

- [x] **Step 1: 실패하는 테스트 작성** — `FakeSB` 에 `cameras` 1~2행을 넣고 검증. 테스트 이름과 단언:
  - `test_send_inserts_contract_row` — 결과 `"queued"`, 행의 `kind == "camera.weak_signal"`, `category == "camera"`, `title == "네트워크 신호가 약하니 점검해 주세요"`, `route == "/crecam"`, `source == "external"`, `data == {"kind": "camera.weak_signal", "source": "external", "camera_id": CAM, "rssi": -78}`, `dedupe_key.startswith("net-weak:" + CAM + ":")`, `read_at` 없음/None, `user_id == OWNER`.
  - `test_send_blocked_when_unread_exists` — 안 읽은 행(25시간 전 생성)이 있으면 `"unread"`, 행 수 그대로.
  - `test_send_blocked_within_24h_of_read_notice` — 읽은 행(23시간 59분 전)이 있으면 `"recent"`.
  - `test_send_allowed_after_24h` — 읽은 행(24시간 1분 전)만 있으면 `"queued"`.
  - `test_many_other_camera_rows_do_not_hide_unread` — 같은 소유자의 카메라 B 행 60건(더 최근) 뒤에 카메라 A 의 안 읽은 행(3일 전)이 있어도 A 는 `"unread"`.
  - `test_other_camera_not_blocked_nor_cleared` — 같은 소유자의 카메라 B 에 안 읽은 행이 있어도 A 는 `"queued"`. `clear_notices(sb, A)` 뒤 B 행의 `read_at` 은 여전히 None.
  - `test_unlinked_or_superseded_camera_not_sent` — `unlinked_at` 또는 `superseded_at` 값이 있으면 `"no_owner"`, INSERT 0.
  - `test_dry_run_logs_and_inserts_nothing` — `NET_WEAK_NOTICE_DRY_RUN` 미설정 → `"dry_run"`, 행 0, caplog 에 `net-weak intent`.
  - `test_clear_marks_only_unread_of_this_camera` — 반환값 1, 해당 행 `read_at` 채워짐, 이미 읽은 행의 `read_at` 은 안 바뀜.
  - `test_flags_default` — 환경변수 없으면 `enabled() is False`, `dry_run() is True`; `NET_WEAK_NOTICE_ENABLED=on` → True; `NET_WEAK_NOTICE_DRY_RUN=off` → False; `NET_WEAK_NOTICE_DRY_RUN=flase`(오타) → True.
- [x] **Step 2: 실패 확인** — `uv run pytest tests/test_net_weak.py -q`
- [x] **Step 3: 구현** — 위 시그니처·규칙대로
- [x] **Step 4: 통과 확인**
- [ ] **Step 5: Commit** — `net_weak: app_notifications 안내 넣기·내리기 + 스위치`

### Task 3: heartbeat 연결 + 문서

**Files:**
- Modify: `backend/net_weak.py` (연결 함수)
- Modify: `backend/mqtt/handlers.py` — 카메라 분기, `_ota_verify_from_heartbeat(...)` 호출 다음(현재 1037-1039줄 뒤)
- Modify: `backend/mqtt/handlers.py` `reset_device_cache()` — 트래커도 비움(테스트 격리)
- Test: `tests/test_net_weak.py`, `tests/test_mqtt_handlers.py`
- Modify: `docs/ENV.md`(스위치 2개), `docs/OPERATIONS.md`(1-4 절: 켜는 순서·보낸 기록 SQL·끄기), `specs/README.md`(Stage V 줄)

**Context:**
- Depends on: Task 1, 2
- Consumes: `WeakTracker.observe/mark_sent/set_active/reset`, `enabled()`, `send_notice()`, `clear_notices()`
- Produces: `on_camera_rssi(sb, camera_uuid: str, camera_id_text: str, rssi: int) -> None`, 모듈 전역 `tracker = WeakTracker()`
- Must know:
  - `on_camera_rssi`: `enabled()` 가 False 면 바로 반환(트래커도 안 건드림). `tracker.observe(camera_uuid, rssi, time.monotonic())` 결과가 `"send"` 면 `send_notice`, 결과가 `"queued"`·`"dry_run"` 이면 `tracker.mark_sent`(= 24시간 동안 다시 조회하지 않음). `"recent"`·`"unread"`·`"failed"`·`"no_owner"` 는 `mark_sent` 없이 둬서 10분 뒤 다시 조회한다(`mark_sent` 를 하면 직전 안내의 24시간이 끝난 뒤에도 최대 24시간 더 막힌다). `"unread"` 면 `tracker.set_active(camera_uuid, True)`(회복 때 내릴 수 있게). `"clear"` 면 `clear_notices`, 반환이 `None`(실패)이면 `tracker.set_active(camera_uuid, None)` 으로 다음 회복 샘플에서 다시 시도.
  - `"queued"` 일 때 `logger.info("net-weak sent camera=%s rssi=%s", camera_id_text, rssi)`, 해제 행 수가 1 이상이면 `logger.info("net-weak cleared camera=%s n=%d", ...)`.
  - handlers 쪽 호출 조건은 `sys_state is not None and "rssi" in sys_state`(`_sys_state` 는 `uptime_sec` 가 없으면 None 을 돌려준다). 기존 `rotate_180`·`errs` 블록과 같은 방식으로 `try/except Exception  # noqa: BLE001` + `logger.exception` 으로 감싼다(heartbeat 를 막지 않기 위해, 이 파일의 기존 관례).
  - `_write_heartbeat` **뒤에** 둔다 — 안내 처리가 느리거나 실패해도 온라인 표시는 이미 써져 있어야 한다.
- Acceptance: `uv run pytest -q` 전부 통과.

- [x] **Step 1: 실패하는 테스트 작성**
  - `tests/test_net_weak.py::test_on_camera_rssi_disabled_does_nothing` — 스위치 꺼짐 → `send_notice` 호출 0, 트래커 상태 없음.
  - 아래 `on_camera_rssi` 테스트 3개의 전제: `NET_WEAK_NOTICE_ENABLED=true`, `NET_WEAK_NOTICE_DRY_RUN=false` 를 monkeypatch, `time.monotonic` 도 monkeypatch 해 **15초 간격**으로 호출한다.
  - `tests/test_net_weak.py::test_on_camera_rssi_sends_once_then_waits_24h` — 0~600초 `-80` → INSERT 1건, 이후 23시간 동안 15초 간격으로 약함이 이어져도 1건.
  - `tests/test_net_weak.py::test_on_camera_rssi_clears_on_recovery` — 발송 뒤 `-70` → 행 `read_at` 채워짐.
  - `tests/test_net_weak.py::test_clear_failure_retries_on_next_good_sample` — `clear_notices` 가 첫 호출에 `None`, 둘째에 1 을 돌려주게 monkeypatch → `-60` 샘플 2번에 `clear_notices` 2회 호출, 셋째 `-60` 에는 호출 없음.
  - `tests/test_net_weak.py::test_unread_result_arms_clear` — 재시작 상황 재현: 안 읽은 행이 DB 에 있고 트래커는 `set_active(CAM, False)` 상태 → 600초 약함 뒤 `"unread"` → 이어서 `-60` → 행 `read_at` 채워짐.
  - `tests/test_mqtt_handlers.py::test_camera_heartbeat_feeds_net_weak` — `net_weak.on_camera_rssi` 를 monkeypatch, `{"uptime_sec": 300, "wifi_rssi": -78, "clips": {...}}` heartbeat → `(fake_sb, camera_uuid, CAMERA_TEXT, -78)` 로 1회 호출.
  - `tests/test_mqtt_handlers.py::test_camera_heartbeat_without_rssi_skips_net_weak` — `wifi_rssi` 없으면 호출 0.
  - `tests/test_mqtt_handlers.py::test_notice_error_does_not_break_heartbeat` — `on_camera_rssi` 가 `RuntimeError` 를 던져도 `handle_telemetry` 가 예외 없이 끝나고 `last_seen_at` UPDATE 가 기록돼 있다.
- [x] **Step 2: 실패 확인**
- [x] **Step 3: 구현**
- [x] **Step 4: 전체 테스트** — `uv run pytest -q`
- [x] **Step 5: 문서 3곳 갱신** — `docs/OPERATIONS.md` 1-4 에는 켜는 순서(아래)와 보낸 기록 SQL(`kind = 'camera.weak_signal'`, 초점 안내 절의 SQL 과 같은 형태)을 적는다.
- [ ] **Step 6: Commit** — `카메라 신호 약함 안내: heartbeat 연결 + 문서 (Stage V)`

## 켜는 순서 (배포·스위치는 이관훈님)

1. PR 머지·배포 — 스위치 꺼짐이라 동작 변화 없음.
2. `NET_WEAK_NOTICE_ENABLED=true` (드라이런 기본 켜짐) + 브리지 재시작 → 2~3일 `net-weak intent` 로그로 대상 수 확인.
3. `NET_WEAK_NOTICE_DRY_RUN=false` 는 아래 **셋이 모두** 끝난 뒤에만:
   - 앱 답변: 이 `kind` 의 알림함 노출 처리(요청서 3-1)와 기존 1분 화면 안내와의 관계(3-2).
   - 수동 확인: 테스트 계정에 계약 행을 SQL 로 1건 넣어 그 카메라 화면에만 막대가 뜨는지, `read_at` 을 SQL 로 채우면 막대가 내려가는지 본다(FakeSB 는 실제 제약·Realtime 을 검증하지 못한다).
   - 드라이런 대상 수 합의.
4. 롤백: `NET_WEAK_NOTICE_ENABLED=false` + 브리지 재시작. 이미 뜬 막대는 `docs/OPERATIONS.md` 1-4 의 일괄 읽음 처리 SQL 로 내린다.

## Out (안 함)

- 사육장 기기(`devices.sys_state.rssi`) 안내
- 푸시 발송, 알림함 전용 표시
- RSSI 이력 저장, 끊김 횟수(`net.disc`·`flap`) 기반 판정
- 오프라인 안내, 오프라인이 된 카메라의 막대 자동 내리기
- 해제·이전된 카메라의 남은 막대 정리
- 기준을 −80 으로 낮추는 안(별도 결정)
- 앱의 기존 1분 화면 안내 변경

## 코드 리뷰 반영 (2026-11-14)

`/code-review` 지적 8건을 반영했다. 위 Task 본문·테스트 전문은 승인 당시 기록이라 그대로 두고, 달라진 것만 여기 적는다. 현재 동작의 기준은 `backend/net_weak.py` 와 `tests/test_net_weak.py` 다.

| # | 지적 | 반영 |
|---|---|---|
| 1 | 해제 중 DB 오류가 아닌 예외가 나면 재시도가 없다 | `on_camera_rssi` 가 예외를 잡아 `tracker.clear_failed()` 뒤 다시 던진다. `test_clear_unexpected_error_still_retries` |
| 2 | RSSI 0·양수가 회복으로 처리된다 | −120 ~ −1 밖의 값은 무시(`RSSI_MIN_DBM`/`RSSI_MAX_DBM`). `test_out_of_range_rssi_is_ignored` |
| 3 | 자주 끊기거나 −74 가 섞이는 카메라는 10분을 못 채운다 | `STREAK_BREAK_DBM` −75 → **−71**(−70 이상 샘플만 지속을 끊는다), `GAP_RESET_SEC` 120 → **300**. 발송(−75 이하)·해제(−70 이상)·10분·24시간 기준은 그대로 |
| 4 | 실발송 뒤 드라이런으로 되돌리면 막대가 자동으로 안 내려간다 | 코드 변경 없음. OPERATIONS.md 1-4 롤백 절에 명시 |
| 5 | 해제 재시도에 간격 제한이 없다 | 실패 뒤 60초(`CLEAR_RETRY_SEC`) 동안 다시 시도하지 않는다. `set_active(None)` 대신 `clear_failed(camera_uuid, now)` |
| 6 | 테스트 누락 | `test_camera_heartbeat_without_uptime_skips_net_weak`, 위 1·2·5 테스트 추가 |
| 7 | 앱 요청서의 "서버 미구현" 문구 | 구현 상태로 갱신 |
| 8 | 모듈 전역 `tracker` | `@lru_cache` 싱글톤 `get_tracker()` 로 변경 |

- 3번은 "사용자가 정할 것" 3번의 추천안을 적용한 것이다. 둘 다 상수라 드라이런 대상 수를 보고 되돌릴 수 있다.
- 검증: `uv run pytest -q` → 1241 passed.
