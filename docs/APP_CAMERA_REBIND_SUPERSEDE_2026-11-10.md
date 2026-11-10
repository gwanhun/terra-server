# 앱 전달 — 카메라 재등록 행 되살리기 · 교차 계정 `superseded_at`

> **대상**: 앱 담당
> **작성**: terra-server 백엔드 담당, 2026-11-10
> **성격**: 서버 변경 2건. 앱이 바꿔야 할 것은 **카메라 목록 필터 1줄**(2번)이고, 1번은 알고만 있으면 됩니다.
> **서버 상태**: **배포 완료 2026-11-10 03:34 UTC** — main [`78427c1`](https://github.com/gwanhun/terra-server/commit/78427c1) (PR #60). 컬럼은 운영 DB 에 있고 브리지 스캔이 돌고 있습니다. 2번 필터는 지금 넣어도 됩니다.
> **배경**: "영상·카메라 DB 현재 문제 정리"(2026-11-09) §1·§7 결정. 스펙 [specs/stage-u-camera-rebind-supersede.md](../specs/stage-u-camera-rebind-supersede.md), API [API.md](API.md) §4.2 `hw_id`

## 1. 같은 계정 재등록 → 해제된 행을 되살립니다 (앱 변경 없음)

지금까지는 카메라를 해제(unlink)한 뒤 같은 보드를 다시 등록하면 **새 행**이 생겼습니다. 배포 뒤에는 같은 계정·같은 `hw_id` 의 **가장 최근 해제 행**이 되살아납니다(`unlinked_at = NULL`).

| | 값 |
|---|---|
| `POST /cameras/pair` 응답 | `reused: true`, **`revived: true`**(새 필드) |
| `cameras.id`, `camera_id` | **종전 값 그대로** — 앱이 들고 있던 UUID 가 다시 유효해집니다 |
| 과거 영상 (`motion_clips`) | 그대로 그 행에 연결돼 **다시 보입니다** |
| `name`, `enclosure_id`, `rotate_180` | **요청값으로 초기화** (이름 = 페어링 때 보낸 이름, 사육장 = 요청에 없으면 NULL, 회전 = false) |

앱 쪽 영향: Realtime 으로 `cameras` UPDATE(`unlinked_at` → NULL)가 옵니다. 해제된 카메라를 로컬에 캐시하고 있다면 이 이벤트로 목록에 복귀시켜 주세요. 되살리기는 활성 행이 없을 때만 일어나고, 더 오래된 해제 행들은 그대로 남습니다(그 영상은 "계정 단위 영상 조회" Stage 에서 따로 다룹니다).

## 2. 다른 계정이 같은 보드를 등록하면 옛 행에 `superseded_at` 이 찍힙니다 (앱 변경 1줄)

보드를 다른 계정이 등록해도 서버는 **페어링 시점에는 이전 계정 행을 건드리지 않습니다**(`hw_id` 는 검증 없는 값이라 남의 카메라를 끊는 근거가 못 됩니다). 대신 브리지가 "새 행 온라인 **그리고** 이전 행 24시간 이상 오프라인" 을 확인한 뒤 이전 행에 `superseded_at` 을 찍습니다. **해제가 아닙니다** — `unlinked_at` 은 NULL 그대로, 설정·영상도 그대로입니다.

- 보드가 옛 자격증명으로 다시 접속하면 서버가 `superseded_at` 을 NULL 로 되돌립니다(자동 복구).
- 30일 동안 재접속이 없으면 MQTT 계정만 해지합니다(`mqtt_revoked_at`). 행은 남습니다.

**앱이 할 일**: 카메라 목록 조회에 필터 추가.

```dart
// camera_repository.dart — 지금
.filter('unlinked_at', 'is', null)
// 배포 뒤
.filter('unlinked_at', 'is', null)
.filter('superseded_at', 'is', null)
```

`GET /cameras`(서버 API 경로)는 서버가 같은 필터를 적용합니다. 단건 `GET /cameras/{id}` 와 영상 조회는 필터하지 않으니, superseded 행의 영상은 지금처럼 계정 소유로 남아 있습니다. Realtime 으로 `superseded_at` UPDATE 가 오면 목록에서 빼 주세요(복구 시 NULL 로 다시 옵니다).

## 3. 새 컬럼 (`cameras`)

| 컬럼 | 뜻 |
|---|---|
| `superseded_at` | 다른 계정이 가져간 보드의 옛 행 — 목록에서 숨김. NULL = 정상 |
| `superseded_by` | 밀어낸 새 `cameras.id` |
| `supersede_suspect_at` | 이 행이 밀어낸 옛 행의 보드가 돌아와 복구된 시각 — 운영 확인용, 앱 비노출 |
| `mqtt_revoked_at` | superseded 30일 뒤 MQTT 해지 시각 — 앱 비노출 |

## 4. 확인 부탁

- [ ] 카메라 목록 쿼리에 `superseded_at IS NULL` 추가 (서버 배포 뒤 앱 배포, 순서 무관 — 컬럼은 서버 배포 전에 생깁니다)
- [ ] 해제 → 재페어링 시 같은 UUID 가 돌아오는 흐름에서 로컬 캐시가 깨지지 않는지
