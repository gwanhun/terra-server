# 백엔드 회신 — APP_CAMERA_REBOOT_HEALTH 검토(9/28) 답신: PR 머지·ack 로그·문서 정정·펌웨어 확인

> **회신 대상**: petcam 검토 회신 2026-09-28 13:15 (Slack)
> **작성**: terra-server 백엔드 담당
> **성격**: 요청 9건 중 8건 처리, 1건(운영 smoke)은 콘솔 버튼을 붙여 두었으니 바로 실행 가능. 펌웨어 질문 4개는 코드로 확인해 답합니다. 검토 중 드러난 **image_state 마이그레이션 누락(9/17~28)** 이 펌웨어 멈춤 분석의 전제를 흔들어서 §5 에 따로 적었습니다.

---

## 0. 항목 → 처리 결과

| # | 항목 | 결과 |
|:---:|---|---|
| 1~3 | 앱 결정(버튼 · rssi · last_err 미사용) | ✅ 확인. rssi 경계 **−75 이하 = 약함** 으로 문서 통일 |
| 4·5 | PR #4 · #5 | ✅ **main 머지 완료** (`dcff402`), 320 passed. 순서는 §1 참고 |
| 6 | 카메라 ack 로그 | ✅ 구현 — `camera ack camera=… msg_id=… result=… action=…` info 1줄 (§2) |
| 7 | 운영 인증 smoke | ✅ **완료** — 신 펌웨어 1대(p4cam-0d1b47b4)로 E2E: 발행 → 카메라 ack ok → 재부팅 → 12초 후 MQTT 복귀 → `firmware_ver`/`rssi`/`reset` 서버 반영 (§3.1) |
| 8 | 문서 정정 4건 | ✅ 반영 (§4). API.md §4.8·4.9 이번 커밋에 포함 |
| 9 | 펌웨어 확인 질문 4개 | ✅ 전부 "예" — 근거 §6 |

---

## 1. PR #4 · #5 머지

- 둘 다 main 에 머지했습니다. 로컬에 같은 이름의 미커밋 마이그레이션 파일이 있어 #4 가 한 번 튕기는 바람에 **#5 → #4 순서**로 들어갔습니다. 요청하신 순서와 반대지만, 머지 직전 운영 DB 에 `cameras.image_state` 컬럼이 이미 있는 것을 select 로 확인했으므로(#4 의 적용 기록과 일치) 배포 순서 위험은 없습니다.
- #4 리뷰: 컬럼 누락 시 그 컬럼만 빼고 재시도 → 그래도 거부면 liveness 만 → 루프는 매 회 필드가 줄어 종료 보장. `firmware_ver` 캐시를 `written` 기준으로 옮긴 것 동의합니다(TTL 30초 동안 재시작 버튼이 숨는 문제 지적 정확).
- #5 리뷰: `_CAMERA_OUT_COLUMNS` 와 CameraOut 필드 일치 테스트 좋습니다. `rotate_180` 이 이전부터 항상 false 였다는 것도 맞습니다.
- 운영 반영은 이번 커밋(ack 로그·콘솔 버튼)과 함께 한 번에 합니다. 반영 후 APP 문서 §6 표를 갱신하고 알려드리겠습니다.

## 2. 카메라 ack 로그 (3-1)

`handle_ack` 카메라 분기에 info 로그 1줄. webrtc_answer/ice ack 는 payload 가 커서 식별 필드만 남깁니다.

```
reboot 발행 camera=p4cam-xxxx msg_id=8f1c…                              ← terra-api
camera ack camera=p4cam-xxxx msg_id=8f1c… result=rejected_unknown_action action=None   ← terra-bridge (구 펌웨어)
camera ack camera=p4cam-xxxx msg_id=8f1c… result=ok action=None                        ← 신 펌웨어
```

발행은 terra-api, ack 는 terra-bridge 프로세스라 **저널이 둘로 나뉩니다**: `journalctl -u terra-api -u terra-bridge --since "10 min ago" | grep <msg_id>`.

## 3. 운영 smoke (3-2)

인증은 Supabase JWKS 비대칭 서명이라 백엔드에서 토큰을 만들 수 없어, 웹 콘솔(`api.terra-server.uk/`) 카메라 행에 **재부팅** 버튼을 넣었습니다(앱과 같은 API 호출). 배포 후 절차:

1. 콘솔 로그인 → 본인 계정의 온라인·구 펌웨어 카메라 행에서 **재부팅** → 확인.
2. 토스트에 `reboot 발행 · msg_id …` (= `published: true`).
3. 서버에서 `journalctl -u terra-api -u terra-bridge --since "5 min ago" | grep <msg_id>` → 발행 1줄 + `result=rejected_unknown_action` ack 1줄.

### 3.1 결과 (9/28 13:50 KST)

구 펌웨어가 아니라 **오늘 빌드를 올린 카메라 1대(p4cam-0d1b47b4)** 로 했습니다. 시리얼·서버 양쪽 확인:

```
카메라: command: action=reboot msg_id=e9773269-…  →  ack: … result=ok  →  1.5초 후 재부팅: mqtt_reboot
        재부팅 → got ip(+7.0s) → MQTT connected(+10.5s)            ≈ 12초
서버:   cameras.firmware_ver = "fb2-p4 0.2.0-20260928"  (heartbeat fw 로 갱신됨)
        clip_stats.sys = {reset: "SW:mqtt_reboot", uptime_s: 52, rssi: -36, heap: …}
```

- 앱 문서 §2.2 의 "20~40초 추정" 을 **실측 12초(MQTT), 첫 하트비트까지 ≤27초** 로 정정했습니다.
- 앱 테스트는 이 카메라로 바로 가능합니다(온라인, 신 펌웨어). 구 펌웨어 `rejected_unknown_action` 경로는 다른 카메라로 눌러보면 됩니다.

## 4. 문서 정정 (3-3)

| 요청 | 반영 |
|---|---|
| §4 "REST 에도 포함" | "Realtime 은 항상, REST 는 **PR #5 배포부터**" 로 수정. §6 반영 표에 줄 추가 |
| 정본 링크 | `API.md` §4.8(reboot)·§4.9(clip_stats/firmware_ver) 를 이번 커밋에 포함. `APP_INTEGRATION.md` §7.4-b 에 링크 |
| 상대 시간 | `last_rec_s`/`age_s`/`uptime_s` 는 `clip_stats_at` 기준, `N분 전 = last_rec_s + (now − clip_stats_at)`, 오프라인이면 마지막 값 유지 — §4 에 명시 |
| rssi 경계 | **−75 이하 = 약함** 으로 통일(콘솔 `<= -75` 빨강과 동일). 표에서 "보통" 구간을 −61~−74 로 정정 |

## 5. 검토 중 드러난 것 — image_state 미적용(9/17~28)이 멈춤 분석에 미치는 영향

PR #4 의 기록대로 `cameras.image_state` 컬럼이 9/17~9/28 운영 DB 에 없었습니다. 9/17 이후 펌웨어는 heartbeat 에 `img` 를 실어 보내므로 그 카메라들은 **heartbeat UPDATE 자체가 실패**해 `last_seen_at`·`clip_stats_at` 이 멈추고 서버에선 오프라인으로 보였습니다.

- 펌웨어 쪽 핸드오프에서 "`clip_stats_at=NULL` → 9/16 이전 구 빌드" 로 추정했던 부분은 **무효**입니다. 오프라인으로 보이는 카메라 중 일부는 9/17 이후 빌드로 리플래시된 뒤 `last_seen_at` 이 플래시 시점에 얼어 있었을 수 있습니다.
- **9/17~9/28 사이 "카메라 오프라인" 사용자 보고는 펌웨어가 아니라 이 서버 사고일 수 있습니다.** 멈춤 사례를 분석하실 때 시각 대조를 부탁드립니다. 다만 "온라인인데 영상이 안 옴"(업로드 정체)은 이 사고와 무관합니다.
- 앞으로는 heartbeat `fw` 로 카메라별 빌드가 서버에 남고, PR #4 로 선택 필드 하나가 온라인 표시를 막지 못하게 됐습니다.

## 6. 펌웨어 확인 질문 답변 (코드 기준)

| # | 질문 | 답 | 근거 |
|:---:|---|:---:|---|
| 1 | `last_err` 가 `clips` 객체 **안**에 오는가 | **예** | `app_mqtt.c` telemetry: `clips{…,"up_busy_s":N,"last_err":{…}}` 로 clips 문자열 안에 이어 붙임. 실패 이력 없으면(`last_err_age_s<0`) 키 자체 생략 |
| 2 | heartbeat `fw` 가 페어링 `firmware_ver` 와 같은 형식인가 | **예** | 둘 다 같은 매크로 `APP_FIRMWARE_VER`("fb2-p4 0.2.0-20260928") 하나에서 나옴. 형식 `fb2-p4 <major.minor.patch>[-<build>]` 확정, 빌드마다 올림 |
| 3 | ack → 1.5초 뒤 재부팅, TTL 초과 폐기 | **예** | `reboot` 액션은 `publish_ack(msg_id,"ok")` 후 별도 태스크가 1.5초 대기 → `app_sys_restart("mqtt_reboot")`. TTL 은 공통 전처리에서 `now − issued_at > ttl_sec` 이면 `rejected_ttl_expired` ack 후 폐기 — 단 **SNTP 동기화 전(부팅 직후 수 초)엔 TTL 검사를 건너뜀**. msg_id 중복은 최근 8개 링버퍼로 거부 |
| 4 | `SW:mqtt_reboot` 등 문자열 일치 | **예** | `app_sys.c` 가 `"SW:%s"` + 재부팅 사유로 만들고, 사유 문자열은 `"mqtt_reboot"`, `"boot_net_wd"`, `"upload_stuck"` 리터럴. 서버는 48자로 자르는데 전부 그 안 |

## 7. 이번엔 안 한 것 (원문 §5 동의)

- `.single()` 0행 → 500: 맞습니다(`GET /cameras/{id}` 등 기존 패턴 공유). 앱이 5xx 를 일반 에러로 처리하면 되고, 정리는 카메라 라우터 전체를 한 번에 하겠습니다(별도 커밋).
- 서버측 재부팅 쿨다운: 보류 동의. 본인 카메라만 대상이고 명령은 QoS1·retain 없음이라 연타해도 카메라는 1.5초 안에 한 번만 내려갑니다(재부팅 뒤 남은 명령은 clean session 이라 안 옴).

## 8. 변경 파일 (이번 커밋)

- `backend/mqtt/handlers.py`(ack 로그), `tests/test_mqtt_handlers.py`(+1), `web/index.html`(재부팅 버튼), `docs/API.md`(§4.8·4.9), `docs/APP_INTEGRATION.md`(§7.4-b 링크), `docs/APP_CAMERA_REBOOT_HEALTH_2026-09-28.md`(정정), 이 문서.
- 머지: PR #5 `80ebf6d`, PR #4 `434052f` → `dcff402`.
