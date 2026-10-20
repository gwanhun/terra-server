# 운영 자동 재부팅 API — 회신 (2026-10-20)

> 요청: petcam-lab `docs/requests/2026-10-20-terra-server-ops-camera-reboot.md` (요청 6)
> 계획서: [specs/stage-m-ops-camera-reboot.md](../specs/stage-m-ops-camera-reboot.md) (교차 검수 반영, owner 2026-10-20 승인)
> PR: https://github.com/gwanhun/terra-server/pull/22
> 요청서 frontmatter 의 `commit_sha`·`implementation_host` 는 owner 가 채운다.

## 결론

요청 2-1·2-2·2-3을 모두 받습니다. **경로·헤더는 요청서 그대로**(`POST /ops/cameras/{camera_uuid}/reboot`, `X-Ops-Secret`)입니다.
요청서와 다른 점은 세 가지입니다 — ① 응답 2개 추가(409 `ota_active`, 503 `ops log unavailable`), ② 기록의 `result` 를 `published`·`ack_result`·`acked_at` 으로 나눔, ③ 2-3은 **푸시 미발행만으로 끝**(`last_ops_reboot_at` 컬럼 안 만듦).

상태: **배포 완료 (2026-10-20, main `d289194`).** 마이그레이션 적용 → `OPS_REBOOT_SECRET` 설정 → API·브리지 재시작 → 시크릿 없는 호출 401 확인까지 마침. 실기 검증(요청서 4절)은 petcam 담당.

배포 시 beta 카메라 수가 **13** 으로 나옴 — 현재 베타 10계정 소유 9대(전부 온라인) + 옛 테스트 4대(`p4cam-ac5cd849` 01036554738, `p4cam-f1c767e0` 01097361840, `p4cam-7f1fac88` beard, `p4cam-07383099` plantimal — 전부 오프라인, 9/23~10/09 이후 미접속). 4대는 오프라인이라 `409 offline` 으로 걸러지지만, 다시 켜지면 재부팅 대상이 되므로 **petcam 쪽에서 `labeling_group` 을 빼야 함**(terra-server 가 관리하는 컬럼이 아니라 건드리지 않음).

---

## 1. 엔드포인트 (요청서 2-1)

```
POST /ops/cameras/{camera_uuid}/reboot
X-Ops-Secret: <서버 .env OPS_REBOOT_SECRET>
{ "reason": "night_rec_stall", "requested_by": "beta-camera-watch" }     (각 1~64자)
```

| 응답 | 본문 | 언제 |
|------|------|------|
| 200 | `{"published": true, "msg_id": "<uuid>"}` | `reboot` 발행됨 |
| 200 | `{"published": false, "msg_id": null}` | 브로커 발행 실패 — 다음 실행에서 재시도 (쿨다운 안 걸림) |
| 401 | `{"detail": "invalid ops secret"}` | 헤더 없음/불일치. 서버에 시크릿이 없을 때도 401 |
| 404 | `{"detail": "camera not found"}` | 카메라 없음·해제됨·uuid 형식 아님 |
| 409 | `{"detail": "skipped", "reason": "not_beta"}` | `labeling_group <> 'beta'` |
| 409 | `{"detail": "skipped", "reason": "offline"}` | `is_online = false` |
| 409 | `{"detail": "skipped", "reason": "live_active"}` | `live_session_id` NOT NULL 또는 `stream_until > now()` |
| 409 | `{"detail": "skipped", "reason": "ota_active"}` | **추가.** 진행 중 OTA 작업이 있음 — 재부팅하면 OTA 가 깨져 작업이 타임아웃까지 묶인다 |
| 429 | `{"detail": "cooldown", "retry_after": <초>}` + `Retry-After` 헤더 | 같은 카메라 운영 재부팅 후 2시간 이내 |
| 503 | `{"detail": "ops log unavailable"}` | **추가.** 기록 INSERT 실패 — **재부팅하지 않음**. 기록 없이 발행하면 쿨다운이 뚫리므로 막는다 |
| 422 | FastAPI 기본 | 바디 필드 누락·길이 위반 |

검사 순서: 401 → 404 → `not_beta` → `offline` → `live_active` → `ota_active` → 쿨다운 429 → 기록 → 발행.
petcam 스크립트는 409 `ota_active` 를 `live_active` 와 같이 "건너뜀"으로, 503 은 "실패(다음 실행에서 재시도)"로 다루면 됩니다.

- 인증은 상수 시간 비교, 시크릿·헤더 값은 로그에 남기지 않습니다. service_role 키는 쓰지 않습니다.
- 발행은 기존 `POST /cameras/{id}/reboot` 와 같은 `reboot_command()` + `MqttWebRTCSignaling().publish()` 입니다. 펌웨어 변경 없음.

## 2. 재부팅 기록 (요청서 2-2) — `camera_ops_reboots`

`result` 한 칸 대신 **`published`·`ack_result`·`acked_at`** 으로 나눴습니다. 발행 결과와 카메라 ack 는 채워지는 시점이 달라서입니다.

| 컬럼 | 뜻 |
|------|----|
| `id`, `camera_id`(cameras.id), `msg_id`(UNIQUE), `reason`, `requested_by`, `requested_at` | 요청서 그대로 |
| `published` | NULL=발행 중, true=브로커 발행 성공, false=실패 |
| `ack_result` | 카메라 ack `result` (`ok` / `rejected_*`). terra-bridge 가 같은 `msg_id` 로 채움 |
| `acked_at` | ack 받은 시각 |

- **발행 전에 먼저 INSERT**(`published=NULL`)하고 결과로 UPDATE 합니다. INSERT 실패면 503, 재부팅 없음.
- **건너뜀(409)·401·404 는 기록하지 않습니다.** 발행을 시도한 것만 남습니다. 건너뜀 이력은 petcam 슬랙에 남습니다.
- RLS 켜고 정책 없음 + anon·authenticated REVOKE = service_role 전용. 앱은 못 봅니다.
- FK 없음 — 카메라 행이 지워져도 리포트용 기록은 남습니다(`ota_jobs` 관례).
- 베타 리포트 집계 예: `select camera_id, count(*) from camera_ops_reboots where published group by 1;`

## 3. 앱에 알리지 않기 (요청서 2-3) — 결론: 푸시 미발행으로 끝

- 이 경로는 푸시·알림 outbox 를 부르지 않습니다(카메라용 앱 푸시는 원래 없음).
- **"확인 필요(앱 담당)" 항목**: 앱 코드를 확인한 결과, 앱은 **사용자가 누른 재시작에만 반응**합니다.
  - `tera-ai-flutter/lib/features/my_cage/presentation/sys_health_controllers.dart` — `RebootController._hold()` 가 `request()`(사용자가 재시작 버튼을 누름) 이후에만 heartbeat 를 구독해 재시작 완료를 판정합니다.
  - 다른 `sysHealthProvider` 사용처는 `isOnline` 만 읽습니다(`domain/sys_health.dart`).
  - 누르지 않은 재부팅(`reset == "SW:mqtt_reboot"`, `uptime_s` 감소)을 보고 토스트·화면을 띄우는 곳은 없습니다.
- 그래서 **`cameras.last_ops_reboot_at` 컬럼은 만들지 않았습니다.** 앱 담당께 위 판단의 최종 확인만 부탁드립니다. 다르면 알려 주세요 — 그때 컬럼을 추가합니다.
- 재부팅은 12~27초라 오프라인 판정(180초)에 안 걸리고, 라이브 중에는 재부팅하지 않습니다(2-1).

## 4. 쿨다운

- 서버 강제 2시간(`OPS_REBOOT_COOLDOWN_SEC = 7200`, owner 2026-10-20 승인).
- **발행 실패(`published=false`)는 세지 않습니다** — 요청서대로 다음 실행에서 바로 재시도할 수 있습니다. 발행 중(NULL)인 행은 셉니다(동시 호출 방지).
- `retry_after` 는 남은 초(올림), 같은 값을 `Retry-After` 헤더에도 넣습니다.
- "한 밤 최대 2회"는 petcam 스크립트 몫입니다. 서버는 강제하지 않습니다.

## 5. 안전장치의 한계 — `labeling_group`

`labeling_group = 'beta'` 가드는 **잘못된 UUID 로 내부·고객 카메라를 재부팅하는 것**은 막지만, `labeling_group` 은 petcam 감시 스크립트가 직접 쓰는 값(“필터일 뿐 권한 아님”)이라 **스크립트가 엉뚱한 카메라를 beta 로 찍으면 막지 못합니다.** 그 경우 상한은 "카메라당 2시간 1회"입니다. 배포 전 beta 카메라 수를 확인합니다(아래 5단계).

## 6. 배포 순서 (terra-server 담당)

1. Supabase 에 `migrations/2026-10-20_camera_ops_reboots.sql` 적용 — **코드 배포 전에.** 없으면 API 가 503(재부팅 안 함).
2. `MIGRATIONS_APPLIED.md` 에 기록.
3. 서버 `.env` 에 `OPS_REBOOT_SECRET` 추가 (생성: `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
4. 코드 배포 후 **API 와 브리지 둘 다 재시작**: `sudo systemctl restart terra-api terra-bridge` (ack 기록은 브리지 쪽 코드).
5. beta 카메라 수 확인: `select count(*) from cameras where labeling_group = 'beta' and unlinked_at is null;` → **9** 여야 함.
6. 요청서 4절 검증 1~6 실행. 4번의 "앱에 푸시·화면 표시 없음"은 앱 담당 확인.

## 7. 시크릿 전달

`OPS_REBOOT_SECRET` 값은 **owner 가 안전한 채널로 petcam-lab 에 전달**합니다. 커밋·문서·채팅·슬랙 금지.
로테이션은 서버 `.env` 교체 + `terra-api` 재시작 후 petcam 쪽 값 교체입니다.

## 8. 참고

- API 명세: [API.md §4.12](API.md)
- 테이블: [DATABASE.md](DATABASE.md) `camera_ops_reboots`
- 환경변수: [ENV.md](ENV.md) "운영 카메라 재부팅"
- 계획서: [specs/stage-m-ops-camera-reboot.md](../specs/stage-m-ops-camera-reboot.md)
- PR: https://github.com/gwanhun/terra-server/pull/22
