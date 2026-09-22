# 백엔드 회신 — 카메라 라이브 안정성 요청 2건 + PT409 (2026-09-23)

> **회신 대상**: 앱(Flutter) `docs/handoffs/2026-09-22-camera-live-stability-server-requests.md`
> **작성**: terra-server 백엔드 담당
> **성격**: 요청 1은 **완료·배포**, 요청 2는 **설계 확인 후 결정 대기**. 선택 3건 중 2건은 **이미 해결돼 있거나 진단이 어긋나 있습니다** — 그 근거를 코드 위치와 함께 적었습니다.
> **관련 커밋**: `f158db2` (main)
> **계약 정본**: [APP_WEBRTC.md](APP_WEBRTC.md) — 이번에 §4.2.1 추가

---

## 0. 요청 → 처리 대조

| # | 요청 | 서버 처리 | 앱이 할 것 |
|:---:|---|---|---|
| 1 | 연결 결과 로그 테이블 | ✅ `webrtc_connect_logs` **적용 완료** (2026-09-23, cron jobid=10). 제안 DDL 에서 5곳 조정 | §1.2 조정 내용 확인 후 INSERT 시작 |
| 1+ | (서버 추가 제안) | ✅ `POST /webrtc/offer` 응답에 `offer_attempts`·`answer_ms` 추가 | **두 값을 로그 행에 넣어주세요** — 실패 원인 분리의 핵심 |
| 2 | TURN 서버 배포 | ⏸️ **인스턴스 결정 대기**. 서버 코드는 HMAC 생성만 추가하면 됨 | 없음 (앱 수정 불필요 확인) |
| 2-q | 펌웨어가 relay 로 check 보낼 수 있나 | ✅ **예** — 근거 §2.1 | — |
| 선택 1 | 새 세션 IDR 즉시 전송 | ⚠️ **진단이 어긋남** — 18초는 IDR 대기가 아닙니다 (§3) | §3 읽고 방향 재검토 |
| 선택 2 | `/close` 유실 세션이 다음 연결 차단 | ✅ **이미 방어돼 있음** (§4) | 실제 겪었다면 시각+camera_id 주세요 |
| 선택 3 | 9/7 mist-blackout 핸드오프 원문 | ❌ **이 레포에 없음** (§5) | 앱 레포 쪽 확인 부탁 |
| 별건 | PT409 전환 | ✅ 서버 영향 없음 확인 + 되돌림 방지 조치 (§6) | §6.2 질문 2건 답변 부탁 |

---

## 1. 요청 1 — 연결 결과 로그 테이블 ✅ 적용 완료

### 1.1 적용 상태

`migrations/2026-09-22_webrtc_connect_logs.sql` — **2026-09-23 운영 DB 적용 완료.**
보존 cron `cleanup-webrtc-connect-logs-90d` 등록 확인 (jobid=10, 매일 04:23 UTC).

수집 경로는 제안하신 대로 **앱이 Supabase 로 직접 INSERT** 입니다. terra-server REST 경유는
일부러 택하지 않았습니다 — 연결이 실패하는 순간은 네트워크가 나쁜 순간이라, 저희 API 를
한 단계 더 태우면 정작 기록해야 할 실패 케이스에서 로그 자체가 유실됩니다.

조회는 `service_role` 전용입니다(SELECT 정책 없음). 앱은 자기 행도 못 읽습니다 — 의도된 설계입니다.

### 1.2 제안 DDL 에서 바꾼 5곳

| # | 변경 | 이유 | 앱 영향 |
|:---:|---|---|:---:|
| 1 | `user_id` FK 에 `ON DELETE CASCADE` 추가 | 이 레포 전 테이블의 owner FK 컨벤션. 빠뜨리면 **계정 삭제가 FK 로 막힙니다**. `auth.users` 하드 삭제는 이미 트리거 문제로 전역 실패 중이라 FK 를 하나라도 더 걸면 안 됩니다 | 없음 |
| 2 | `local_cand`/`remote_cand` 에 `CHECK` 추가 | W3C `RTCIceCandidateType` 전체 집합(`host/srflx/prflx/relay`)이라 늘어날 일이 없습니다 | **이 넷 외 값은 거부됩니다** |
| 3 | **`outcome` 에는 CHECK 를 걸지 않음** | 제안서 주석의 6개 값을 CHECK 로 만들까 고민했지만 뺐습니다. 이 테이블의 목적은 측정인데, CHECK 가 거부하면 **정작 측정하려던 실패 행이 통째로 날아갑니다**(앱은 23514 예외를 가장 네트워크가 나쁜 순간에 받습니다). 더러운 값은 `GROUP BY` 로 드러나 나중에 정리할 수 있지만, 없는 행은 복구가 안 됩니다 | **값이 늘어나도 안전** (`no_answer`, `timeout` 등 자유롭게) |
| 4 | `(created_at DESC)` 인덱스 추가 | "최근 24시간 전체 실패율"이 주 용도인데 `(camera_id, created_at)` 선두 인덱스로는 못 탑니다. 보존 cron 의 범위 DELETE 도 이걸 씁니다 | 없음 |
| 5 | `offer_attempts`·`answer_ms` 컬럼 추가 | §1.3 참고 | **채워주세요** |

> 보존 90일은 제안대로입니다. 다만 제안서에 "90일이면 충분"만 있고 수단이 없어서 `pg_cron` 잡을 같이 넣었습니다.

### 1.3 ⭐ `offer_attempts` / `answer_ms` — 요청 1의 목적을 실제로 달성하는 부분

요청하신 목적이 **"실패가 앱·펌웨어·NAT 중 어디서 생기는지 비율"** 인데, 제안 스키마만으로는
이 셋이 갈리지 않습니다. 앱이 볼 수 없는 정보가 하나 있기 때문입니다.

서버는 카메라가 offer 에 답하지 않으면 **새 `msg_id` 로 최대 3회, 회당 7초까지 재발행**합니다
([`backend/routers/webrtc.py`](../backend/routers/webrtc.py) `attempts = 3`, `per_timeout = 7.0`).
카메라 `esp_peer_open()` 이 PSRAM 경합으로 간헐 실패하는 걸 넘기려는 완화책입니다.

앱 입장에서는 이게 **"느린 한 번의 호출"로만 보입니다.** 그래서 서버가 알려드립니다.

```jsonc
POST /cameras/{camera_uuid}/webrtc/offer
→ 200
{
  "session_id": "...",
  "type": "answer",
  "sdp": "...",
  "raw": { ... },
  "offer_attempts": 1,     // ← 추가
  "answer_ms": 820         // ← 추가
}
```

| 값 | 해석 | 범인 |
|---|---|---|
| `offer_attempts == 1` | 카메라가 첫 offer 에 바로 답했다. 이후 연결이 실패했다면 SDP 교환 **이후** 단계 | **ICE / NAT** |
| `offer_attempts > 1` | 카메라가 첫 offer 에 답을 못 했다 | **펌웨어** (`esp_peer_open` PSRAM) |
| `504` 응답 | 3회 모두 무응답. `offer_attempts` 는 정의상 3 | **펌웨어** (확정) |

두 값을 그대로 `webrtc_connect_logs` 의 동명 컬럼에 넣어주시면 이 집계가 바로 나옵니다:

```sql
SELECT offer_attempts, outcome, count(*)
FROM public.webrtc_connect_logs
WHERE created_at > now() - interval '7 days'
GROUP BY 1, 2 ORDER BY 1, 3 DESC;
```

**순수 추가 필드라 현재 앱(0.129.0)은 수정 없이 그대로 동작합니다.** 안 읽으면 컬럼이 NULL 로 남을 뿐입니다.

> 📌 **구현 시 보실 곳**: [APP_WEBRTC.md §4.2.1](APP_WEBRTC.md#421-offer_attempts--answer_ms--실패-원인-분리용-2026-09-22-추가)
> — 응답 예시와 해석표가 계약 정본에 들어가 있습니다. 이 회신 문서가 아니라 그쪽이 정본입니다.

### 1.4 배포 상태

커밋 `f158db2`, main 반영. 운영 서버 재시작 후 유효합니다.
테스트 2건 추가 — 단일 시도(`=1`), 재시도 후 성공(`=2`), 재발행마다 `msg_id` 가 달라지는지 검증.

---

## 2. 요청 2 — TURN 서버 ⏸️ 인스턴스 결정 대기

### 2.1 질문 답: 펌웨어가 relay 주소로 connectivity check 를 보낼 수 있나 → **예**

카메라 펌웨어는 `ice_trans_policy = ESP_PEER_ICE_TRANS_POLICY_ALL` 입니다
(`firebeetle2-p4-yr030/main/app_webrtc.c`). host/srflx/relay 를 모두 시도하므로
원격 relay 후보로 check 를 보냅니다.

게다가 **relay 경로는 이미 한 번 돌려본 적이 있습니다.** 같은 파일 주석:

> `RELAY-only 로 강제했더니 relay 경로가 불안정할 때(keepalive fail/DTLS timeout) 폴백이 없어
> 영상이 아예 안 뜨는 회귀 발생 → ALL 로 복구.`

즉 relay 자체는 동작했고 **안정성**이 문제였습니다. NACK 재전송 버퍼도 "relay RTT 창"에 맞춰
이미 키워둔 상태입니다(send pool 400kB→1MB, queue 256→1024, resend 3→5).

### 2.2 좋은 소식 — 앱 전용 TURN 이면 펌웨어 수정 불필요

한쪽만 relay 후보를 가져도 대칭 NAT 양쪽 케이스는 풀립니다. 카메라가 앱의 relay 주소로 보내면
TURN 이 중계하기 때문입니다. 주신 완료 기준 `local_cand=relay` 도 앱 관점이라 이걸로 충족됩니다.

**→ 1단계는 앱 전용 TURN 으로 갑니다. 펌웨어 변경 없음, 앱 변경 없음.**

### 2.3 ⚠️ 단기 자격증명은 카메라에 닿지 않습니다 (2단계 과제)

펌웨어의 TURN 자격증명은 **컴파일타임 Kconfig** 입니다
(`APP_WEBRTC_TURN_URL` / `_USER` / `_PASS`). 6~24시간 만료 HMAC 자격증명을 펌웨어 이미지에
박을 수는 없습니다. 앱은 재연결마다 `/config` 를 새로 받으니 문제없지만, 카메라는 경로가 없습니다.

나중에 카메라도 자기 relay 후보를 만들게 하려면 **`webrtc_offer` MQTT 명령 payload 에
`ice_servers` 를 실어 보내는 것**이 가장 깔끔합니다. 서버가 이미 그 명령을 만들고 자격증명도
알고 있어서 서버 변경은 작고, 펌웨어는 세션마다 받은 값을 쓰면 됩니다. 1단계 성과를 보고 판단하겠습니다.

### 2.4 서버 코드 — HMAC 생성 추가 필요

현재 `_ice_servers_from_env()` 는 환경변수 문자열을 그대로 돌려줄 뿐이라 `use-auth-secret`
방식의 `username=<만료ts>:<user_id>` / `credential=HMAC-SHA1` 생성 코드가 없습니다.
20줄 정도의 추가이고, coturn 배포가 정해지면 같이 올리겠습니다.

**주신 대로 `/cameras/webrtc/config` 응답 `iceServers` 에 넣으면 앱 수정 없이 적용됩니다.**
자동 재연결마다 config 를 새로 받는다는 점도 확인했습니다 — 만료 갱신이 자연히 됩니다.

### 2.5 결정이 필요한 것 — 인스턴스 분리 여부

| 항목 | 내용 |
|---|---|
| 대역폭 | 카메라가 **고정 2.5Mbps** (`CONFIG_APP_H264_BITRATE`, 800x800@10fps). TURN 은 in/out 을 둘 다 태우므로 **시청 1시간 ≈ 2.2GB** |
| 현재 서버 | Lightsail $7 플랜 (1GB / 2 vCPU / **2TB transfer**) — API 트래픽과 **같은 할당량** |
| 비용 | 별도 인스턴스 = **월 +$7 내외** (같은 Lightsail 등급 기준). 기존 서버에 얹으면 추가 비용 0 이지만 2TB 를 API 와 나눠 씁니다 |
| 한도 감각 | 2TB 를 라이브가 다 쓴다고 가정하면 **월 약 900시간 시청**. 베타 규모에선 여유가 있으나, API 트래픽이 같은 통에 있는 게 문제입니다 |
| 권고 | **coturn 별도 인스턴스.** 라이브가 API 를 굶기는 걸 막고 요금도 분리됩니다 |
| 추가 | `turns:…:443` 은 **별도 인증서** 필요 (api.terra-server.uk 와 다른 도메인) |

리전은 서울로 맞추겠습니다. 인스턴스 방침만 정해지면 coturn 설정 파일까지 준비하겠습니다.

---

## 3. 선택 1 — "새 세션 IDR 즉시 전송" ⚠️ 진단이 어긋나 있습니다

### 3.1 펌웨어는 이미 IDR 을 기다렸다 보냅니다

`app_webrtc.c` 에 `s_wait_idr` 플래그가 있고, 새 세션에서는 **IDR 이 올 때까지 프레임을
의도적으로 드롭**합니다. 주석 그대로 *"P-frame 부터 보내면 두 번째 뷰어가 검은 화면으로 정지"* 하기 때문입니다.

### 3.2 그 대기 비용은 최대 1.5초입니다

```
CONFIG_APP_H264_GOP = 15
CONFIG_APP_H264_FPS = 10
→ IDR 주기 = 15 / 10 = 1.5초
```

**18초를 설명하지 못합니다.**

### 3.3 18초는 offer 재시도로 설명됩니다

```
 7초   1차 offer 무응답 (per_timeout)
 7초   2차 offer 무응답
 2초   ICE gathering (계약 권장값)
 +α    DTLS 핸드셰이크
1.5초  IDR 대기
────────────────────────────
≈ 18초
```

원인은 이미 문서화돼 있습니다 — `firebeetle2-p4-yr030/LIVE_WEBRTC_NO_ANSWER.md`:
카메라 `esp_peer_open()` 이 **PSRAM 부족**으로 실패. 모션 클립이 쌓이면 재현됩니다.

### 3.4 결론

force-IDR(`V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME`)을 넣어도 **1.5초밖에 줄지 않습니다.**
진짜 수확은 첫 offer 가 왜 무응답인지를 잡는 것이고, 그게 바로 §1.3 의 `offer_attempts` 가
증명해 줄 값입니다.

> **선택 1과 요청 1은 같은 문제입니다.** 로그가 쌓이면 `offer_attempts > 1` 의 비율로
> PSRAM 가설이 바로 검증됩니다. 그 결과를 보고 펌웨어 쪽 대응(클립 업로드 중 라이브 거절,
> PSRAM 선점 등)을 정하는 게 순서라고 봅니다.

---

## 4. 선택 2 — `/close` 유실 세션 ✅ 이미 방어돼 있습니다

`app_webrtc.c` 에 4중으로 들어가 있습니다.

| 방어 | 임계 | 동작 |
|---|---|---|
| **새 offer 도착** | 즉시 | 이전 세션을 정리하고 진행. 주석 그대로 *"새 offer 자체가 이전 세션을 대체하므로 close command 유실에도 복구된다"* |
| connect timeout | **12초** | 연결 못 하면 peer close |
| video stall | **20초** | watchdog 이 peer 폐기 |
| loop stall | **30초** | 보드 재부팅 |

미완료 세션 뒤에 새 offer 가 오면 peer 객체를 **통째로 교체**하는 경로도 따로 있습니다
(`offer-retry-after-no-connect`) — ICE/DTLS 내부 상태가 막힌 경우 대비입니다.

서버 쪽도 `stream_until = now + 5분` 으로 만료를 겁니다.

**→ 이론상 "이전 세션이 다음 연결을 막는" 경로는 없습니다.**
그래도 실제로 겪으셨다면 다른 원인이고, 그것도 요청 1 로그가 있어야 잡힙니다.
**겪은 케이스의 시각(UTC)과 `camera_id` 를 주시면 서버 로그와 맞춰보겠습니다.**

---

## 5. 선택 3 — 9/7 mist-blackout 핸드오프 ❌ 이 레포에 없습니다

`backend-handoff-2026-09-07-mist-blackout.md` 를 `docs/`, `specs/`, **git 히스토리 전체**에서
검색했지만 없습니다. 파일명이 앱 레포의 `docs/handoffs/` 형식이라 그쪽이나 다른 채널에 있을 것 같습니다.

저희 쪽 9/7 자료는 [APP_FAN2_2026-09-07.md](APP_FAN2_2026-09-07.md)(냉각팬)뿐이고 내용이 다릅니다.

> 참고: 현재 펌웨어의 stall 방어는 20~30**초** 단위라 "라이브 ~20분 뒤 끊김"과는 시간대가
> 맞지 않습니다. 별도 원인일 가능성이 높습니다. 원문 찾으시면 공유 부탁드립니다.

---

## 6. 별건 — PT409 전환 (9/22 공유분)

### 6.1 서버 영향: 없음 ✅

`backend/`, `web/` 전체에서 `redesign_*` RPC 호출도 `40001`/`40P01` 분기도 없습니다.
서버는 이 RPC 들을 쓰지 않고 Supabase 테이블에 직접 붙습니다. **PT409 대응 코드를 넣을 곳이 없습니다.**

유일한 호출처는 `scripts/seed_test_pets.py` 의 `redesign_save_pet_v1` 인데, 40001 분기가 없어
깨지지 않습니다. 오히려 **개선됐습니다** — 예전엔 PostgREST 무한 재시도에 걸려 멈췄을 스크립트가
이제 409 로 바로 떨어지고 다음 계정으로 넘어갑니다.

### 6.2 ⚠️ 답변 부탁드릴 것 2건

**(1) `redesign_save_pet_v1` 의 나머지 40001 둘도 바꾸셨나요?**

저희가 가진 사본 기준으로 그 함수 안에 `40001` RAISE 가 셋인데, '구성 변경'은 하나뿐입니다.
나머지 둘도 **재시도해봐야 조건이 안 바뀌는 영구 오류**라 같은 무한 재시도 함정이 남습니다:

| 메시지 | 상황 |
|---|---|
| `deleted pet cannot be edited or resurrected` | 툼스톤된 개체 수정 시도 |
| `group already has a pet` | 그룹에 이미 개체 있음 |

PostgREST 재시도는 메시지가 아니라 **SQLSTATE 만 보고** 돕니다. 특히 `group already has a pet` 은
이번 사고 원인(기기 추가 → 기존 환경 합류)과 인접해 보여 위험도가 비슷합니다.

**(2) `20260922_redesign_conflict_errcode.sql` 원문을 공유해 주실 수 있나요?**

저희 레포에 `_02_redesign_groups.sql` / `_04_redesign_pets.sql` 사본이 있는데 **아직 40001** 이고,
전부 `CREATE OR REPLACE FUNCTION` 이라 **재적용하면 PT409 수정이 조용히 되돌아갑니다.**

지금은 각 파일 상단에 경고 헤더를 박고 `MIGRATIONS_APPLIED.md` 에 ⚠️ 행으로 기록해
**되돌림 사고만 막아둔 상태**입니다. 추측으로 고치면 새 drift 가 생기므로 원문을 받은 뒤
동기화하겠습니다.

### 6.3 저희가 채택한 규칙

`MIGRATIONS_APPLIED.md` 에 두 줄 추가했습니다.

- 앱팀이 운영 DB 를 직접 고친 경우에도 ⚠️ 행으로 기록한다 (파일이 이 레포에 없어도)
- **새 RPC 에서 충돌·구성 변경류 오류는 `40001`/`40P01` 대신 `PT409` 를 쓴다**

---

## 7. 정리 — 앱측 조치

| 우선 | 조치 |
|:---:|---|
| 1 | `webrtc_connect_logs` INSERT 시작. **`offer_attempts`·`answer_ms` 를 `POST /webrtc/offer` 응답에서 받아 채워주세요** (§1.3, 정본 [APP_WEBRTC.md §4.2.1](APP_WEBRTC.md)) |
| 2 | `local_cand`/`remote_cand` 는 `host/srflx/prflx/relay` 만 허용 — 그 외 값은 INSERT 거부 (§1.2) |
| 3 | `outcome` 은 제약 없음 — 어휘가 늘어도 안전 (§1.2) |
| 4 | IDR 관련 작업 착수 전 §3 확인 |
| 5 | `/close` 유실 케이스를 실제로 겪었다면 **시각(UTC) + camera_id** 공유 (§4) |

### 저희에게 답을 주실 것

1. `redesign_save_pet_v1` 의 나머지 40001 둘 처리 여부 (§6.2-1)
2. `20260922_redesign_conflict_errcode.sql` 원문 (§6.2-2)
3. 9/7 mist-blackout 핸드오프 원문 (§5)

### 저희가 다음에 할 것

- coturn 배포 (인스턴스 방침 확정 후) + 서버 HMAC 생성 코드
- 로그 1~2주 쌓인 뒤 `offer_attempts` 분포로 PSRAM 가설 검증 → 펌웨어 대응 결정
