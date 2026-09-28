# 앱 전달 — 카메라 원격 재부팅 API + 상태 진단 필드 (rssi · last_err · firmware_ver)

> **대상**: 앱 개발 담당
> **작성**: terra-server 백엔드 담당, 2026-09-28
> **성격**: 베타 카메라 "멈춤"(연결 끊김 / 온라인인데 영상 없음) 대응으로 펌웨어를 고치면서, 앱이 쓸 수 있는 **새 API 1개**와 **`cameras` 응답 필드 3개**가 생겼습니다. 필수 작업은 없고, 재부팅 버튼은 **권장**입니다.
> **관련 커밋**: `88ab198` (reboot API·rssi/fw 저장, 운영 반영 완료) · petcam PR #4·#5 머지 · 회신 반영 커밋(§6). 펌웨어는 카메라 리플래시 후 유효 (§6)
> **개정 이력**: 9/28 오후 — petcam 회신 반영(rssi 경계 −75 이하=약함, REST 포함 시점, 상대 시간 계산, 정본 링크). 회신 문서: [BACKEND_HANDOFF_REPLY_CAMERA_REBOOT_2026-09-28.md](BACKEND_HANDOFF_REPLY_CAMERA_REBOOT_2026-09-28.md)

---

## 0. 요약

| # | 항목 | 앱에서 할 일 | 권장도 |
|:---:|---|---|:---:|
| ① | `POST /cameras/{id}/reboot` — 원격 재부팅 | 카메라 상세에 "카메라 재시작" 버튼 (§2) | 권장 |
| ② | `cameras.firmware_ver` 가 이제 **실제로 바뀜** | 구/신 펌웨어 분기 기준으로 사용 (§3) | 권장 |
| ③ | `clip_stats.sys.rssi` — WiFi 신호(dBm) | **−75 이하 = 약함** 일 때만 설치 위치 안내 (§4, petcam 결정) | 선택 |
| ④ | `clip_stats.last_err` — 마지막 업로드 실패 | 지원 화면에 표시하거나 무시 (§4) | 선택 |
| ⑤ | `clip_stats.sys.reset` 새 값 3개 | 앱 동작 변화 없음, 참고 (§4) | — |

**모든 새 필드는 신 펌웨어부터 옵니다. 리플래시 전에는 `null`/키 없음이 정상이라 앱은 없음을 기본으로 처리해 주세요.**

---

## 1. 배경 — 무엇을 고쳤나

베타 카메라가 "이틀 잘 되다 끊김" 또는 "앱에선 온라인인데 영상이 안 옴" 상태가 되는 원인은 펌웨어 결함 2건이었습니다.

- **WiFi 재연결 포기**: 공유기가 20초만 끊겨도 영영 재연결하지 않음 → 무한 재연결 + 90초 IP 없으면 자가 재부팅 + 부팅 시 공유기 꺼져 있어도 30초마다 재시도.
- **업로드 무한루프**: 소켓이 멈추면 업로드 태스크가 영원히 돌며 녹화 슬롯을 잡음 → 즉시 실패 처리 + 15분 이상 정체 시 자가 재부팅.

여기에 "하트비트는 살아 있는데 녹화가 멈춘" 카메라를 사용자가 전원을 뽑지 않고 살릴 수 있도록 **원격 재부팅 명령**을 넣었고, 왜 실패하는지 서버에서 볼 수 있게 **rssi / last_err** 계측을 추가했습니다.

---

## 2. `POST /cameras/{camera_uuid}/reboot`

### 2.1 계약

```
POST /cameras/{camera_uuid}/reboot
Authorization: Bearer <사용자 JWT>
(본문 없음)

→ 200 { "published": true,  "msg_id": "8f1c…" }   명령 발행됨
→ 200 { "published": false, "msg_id": null }      브로커 발행 실패 (일시 장애) → 잠시 후 재시도 버튼
→ 404 { "detail": "camera not found" }            타인 카메라 / 등록 해제된 카메라 (다른 카메라 API 와 동일 규칙)
```

- 발행은 **best-effort 1회**. 발행 실패를 5xx 로 주지 않고 `published=false` 로 알립니다.
- `msg_id` 는 서버 로그 대조용입니다. 앱이 저장할 필요는 없습니다. 서버는 발행 시 `reboot 발행 camera=… msg_id=…`, 카메라 ack 수신 시 `camera ack camera=… msg_id=… result=…` 를 남겨 같은 `msg_id` 로 짝을 맞춥니다(구 펌웨어는 `result=rejected_unknown_action`).

### 2.2 카메라 쪽 동작

| 상황 | 결과 |
|---|---|
| 온라인 + 신 펌웨어 | ack 후 **1.5초 뒤 재부팅**. **실측(9/28, p4cam-0d1b47b4)**: 재부팅 → WiFi → MQTT 재접속 **약 12초**, 첫 하트비트(`reset=SW:mqtt_reboot`)까지 최대 약 27초(15초 주기) |
| 온라인 + 구 펌웨어 | 명령을 모르는 액션으로 거부 → **아무 일도 일어나지 않음** |
| 오프라인 | 명령 TTL 60초 안에 카메라가 없으면 **유실**. 재연결해도 나중에 실행되지 않음 |

- 앱은 명령 ack 를 직접 받지 못합니다(카메라 명령은 `commands` 테이블을 거치지 않음).
- **재부팅 완료 판정**: `cameras` Realtime UPDATE 에서 `clip_stats.sys.uptime_s` 가 작아지고 `clip_stats.sys.reset == "SW:mqtt_reboot"` 이면 재부팅이 끝나고 첫 하트비트가 온 것입니다.
- 정상 재부팅이면 3분 안에 하트비트가 돌아오므로 `is_online` 은 보통 `false` 로 뒤집히지 않습니다(오프라인 판정 임계 180초).

### 2.3 UX 권장

- 노출 조건: `is_online == true` **이고** 신 펌웨어(§3). 구 펌웨어에서는 눌러도 반응이 없어 사용자가 혼란스럽습니다.
- 확인 다이얼로그: "카메라가 약 30초 동안 꺼졌다 켜집니다. 라이브 보기와 녹화가 잠시 중단됩니다." (실측 12~27초라 "약 30초" 문구 그대로 써도 됩니다)
- 연타 방지: 발행 성공 후 60초 비활성(명령 TTL 과 동일).
- `published=false` 면 "잠시 후 다시 시도" 안내. 서버 장애가 아니라 브로커 순간 끊김입니다.
- 라이브 시청 중이면 WebRTC 세션이 끊깁니다. 재부팅 후 다시 열어야 합니다.

---

## 3. `firmware_ver` — 이제 리플래시하면 바뀝니다

지금까지는 페어링 때 한 번만 저장돼 모든 카메라가 `"fb2-p4 0.1.0"` 고정이었습니다. 신 펌웨어는 15초 하트비트에도 버전을 실어 서버가 값이 다를 때만 갱신합니다(Realtime 잡음 없음).

| 값 | 의미 |
|---|---|
| `null` 또는 `"fb2-p4 0.1.0"` | 구 펌웨어 — reboot 무반응, rssi/last_err 없음 |
| `"fb2-p4 0.2.0-20260928"` 이후 | 신 펌웨어 |

판별은 문자열 비교보다 **버전 파싱** 을 권장합니다: `"fb2-p4 <major.minor.patch>[-<build>]"` 에서 `0.2.0` 이상이면 신 펌웨어. 앞으로 빌드마다 올라갑니다.

---

## 4. `cameras.clip_stats` 구조 (앱이 쓸 만한 것만)

Realtime `cameras` 행에는 항상 포함됩니다. **REST `GET /cameras`·`GET /cameras/{id}` 에 `clip_stats`·`clip_stats_at`·`capabilities`·`rotate_180`·`image_state` 가 실리는 것은 petcam PR #5(`80ebf6d`) 배포부터**입니다 — 그전엔 select 누락으로 항상 `null`/`false` 였습니다(§6 반영 표 참고). 15초 하트비트마다 UPDATE 되는 건 9/16 부터 이미 그랬고, 필드가 늘어도 빈도는 같습니다.

**상대 시간 필드 주의**: `last_rec_s`, `last_err.age_s`, `sys.uptime_s` 는 **하트비트 시점(`clip_stats_at`) 기준**입니다. 화면의 "N분 전" 은 `last_rec_s + (now − clip_stats_at)` 로 계산하세요. 카메라가 오프라인이면 `clip_stats` 는 마지막 값이 그대로 남고 `clip_stats_at` 만 멈추므로, `is_online == false` 면 이 값들을 "마지막 보고 기준" 으로 표시하거나 숨기는 게 맞습니다.

```json
"clip_stats": {
  "rec": 116, "skip": 15, "skip_lock": 0,
  "up_ok": 38, "up_fail": 78, "sd_ok": 0, "sd_fail": 0, "sd_backlog": 0,
  "last_rec_s": 412, "up_busy_s": -1,
  "last_err": { "stage": 2, "err": -1, "http": 0, "age_s": 300 },
  "sys": { "uptime_s": 61831, "reset": "SW:net_wd", "heap": 123456, "rssi": -68 }
},
"clip_stats_at": "2026-09-28T03:25:00+00:00"
```

| 필드 | 단위 | 앱 활용 | 펌웨어 |
|---|---|---|---|
| `sys.rssi` | dBm (음수, 0 에 가까울수록 좋음) | **−75 이하 = 약함**(웹 콘솔 빨강과 동일 경계). 참고 구간: −60 이상 좋음 / −61~−74 보통 / −75 이하 약함. 약함일 때만 "공유기와 가까운 곳에 설치" 안내(petcam 결정) | 신 |
| `sys.uptime_s` | 초 | 재부팅 판정(§2.2), "가동 N일" 표시 | 9/17~ |
| `sys.reset` | 문자열 | 마지막 재부팅 사유. 사용자 노출은 권장하지 않고 지원 화면 정도 | 9/17~ |
| `last_rec_s` | 초, `-1` = 부팅 후 없음, 하트비트 시점 기준 | "마지막 움직임 N분 전" = `last_rec_s + (now − clip_stats_at)` | 9/16~ |
| `last_err` | 객체, 없으면 부팅 후 실패 없음 | `http == 0` 이면 전송 실패(=WiFi 문제), `4xx/5xx` 면 서버 거절. `stage` 1=업로드 URL 발급 2=영상 업로드 3=메타 등록, `age_s` 경과 초. 지원 화면용 | 신 |
| `up_fail` / `up_ok` | 누적 건수(부팅 후) | 실패 비율이 높으면 "영상 저장 실패가 잦음" 배지 정도 | 9/16~ |
| `up_busy_s` | 초, `-1` = 업로드 없음 | 무시해도 됨(서버 감시용) | 9/16~ |

`sys.reset` 값 목록 (참고):

| 값 | 뜻 |
|---|---|
| `POWERON` | 전원 투입 |
| `SW:mqtt_reboot` | 앱/서버의 원격 재부팅 (§2) |
| `SW:net_wd` | WiFi 90초 단절 → 자가 재부팅 |
| `SW:boot_net_wd` | 부팅 후 20분간 WiFi 못 붙음 → 자가 재부팅 (신) |
| `SW:upload_stuck` | 업로드 15분 정체 → 자가 재부팅 (신) |
| `SW:rotate` / `SW:ble_reprov` | 회전 적용 / BLE WiFi 재설정 |
| `BROWNOUT` | 전원 부족 (어댑터·케이블 문제 신호) |
| `PANIC` / `WDT` / `INT_WDT` / `TASK_WDT` | 크래시 |

---

## 5. 앱 계약 변경 없음 확인

- 기존 필드·응답 모양은 그대로입니다. `clip_stats`, `firmware_ver` 는 원래 있던 필드에 값이 채워지는 것뿐입니다.
- `GET /cameras` 목록 크기: `clip_stats` 에 `last_err`(약 50B)·`rssi`(약 12B)가 늘어납니다. 무시해도 되는 수준입니다.
- 푸시/알림 파이프라인 변화 없음. 카메라 오프라인 알림은 여전히 없습니다.

---

## 6. 반영 상태와 확인 방법

| 구분 | 상태 |
|---|---|
| 서버 코드 | `main` 반영(`88ab198`), pytest 306 passed |
| 서버 운영 (reboot API·rssi/fw) | **반영 완료** (2026-09-28 12:46 KST, terra-api·terra-bridge 재시작). `POST /cameras/{id}/reboot` 가 인증 없이 401 을 돌려주는 것으로 확인 |
| 서버 운영 (PR #4 heartbeat 폴백 · PR #5 REST 컬럼 · ack 로그) | main 머지 완료. **운영 반영 예정** — 반영되면 이 줄 갱신. 반영 전까지 REST 의 `clip_stats` 는 `null` |
| 펌웨어 | **실기 검증 완료(9/28 13:50 KST, p4cam-0d1b47b4 1대)**: 콘솔 재부팅 → ack ok → 재부팅 → 복귀, 서버 `firmware_ver` 가 `fb2-p4 0.2.0-20260928` 로 갱신, `sys.rssi`·`reset=SW:mqtt_reboot` 수신 확인. 나머지 17대 리플래시 일정은 별도 공지 |

앱 쪽 테스트는 **p4cam-0d1b47b4** 로 바로 가능합니다(신 펌웨어, 온라인). §2.2 재부팅 판정과 §4 필드 표시를 이 카메라로 확인하고, 나머지 구 펌웨어 카메라로 "없음 처리" 를 확인해 주세요.

---

## 7. 결정 요청

1. "카메라 재시작" 버튼을 넣을지, 넣으면 위치(카메라 상세 / 설정 / 문제 해결 화면).
2. 신호 세기(rssi) 노출 여부. 넣으면 §4 의 3구간 기준으로 갈지.
3. `last_err`·`reset` 은 사용자에게 숨기고 지원용 진단 화면에만 두는 것을 제안합니다. 이견 있으면 알려주세요.

관련 정본: [API.md §4.8·§4.9](API.md) (이번 커밋에 포함), [APP_INTEGRATION.md](APP_INTEGRATION.md) §7.5 옆 링크.
