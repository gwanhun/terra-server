# 앱 전달 — 카메라 재등록 행 되살리기 · 교차 계정 `superseded_at`

> **대상**: 앱 담당
> **작성**: terra-server 백엔드 담당, 2026-11-10 (같은 날 앱 코드 대조 후 수정)
> **성격**: 서버 변경 2건. 앱이 바꿔야 할 곳은 **4곳**(2번)이고, 1번은 알고만 있으면 됩니다.
> **서버 상태**: **배포 완료** — PR [#60](https://github.com/gwanhun/terra-server/pull/60) `78427c1`(2026-11-10 03:34 UTC), PR [#61](https://github.com/gwanhun/terra-server/pull/61) `83654fc`(03:46 UTC). 컬럼은 운영 DB 에 있고 브리지 스캔이 돌고 있습니다. 첫 스캔에서 10행이 숨김 대상이 됐고, 앱 필터가 들어가기 전까지는 이 행들이 "오프라인 카메라"로 계속 보입니다. **앱 변경은 지금 넣어도 됩니다.**
> **배경**: "영상·카메라 DB 현재 문제 정리"(2026-11-09) §1·§7 결정. 스펙 [specs/stage-u-camera-rebind-supersede.md](https://github.com/gwanhun/terra-server/blob/main/specs/stage-u-camera-rebind-supersede.md), API [docs/API.md](https://github.com/gwanhun/terra-server/blob/main/docs/API.md) §4.2 `hw_id`

## 1. 같은 계정 재등록 → 해제된 행을 되살립니다 (앱 변경 없음)

지금까지는 카메라를 해제(unlink)한 뒤 같은 보드를 다시 등록하면 **새 행**이 생겼습니다. 배포 뒤에는 같은 계정·같은 `hw_id` 에 활성 행이 없으면 **가장 최근 해제 행**이 되살아납니다(`unlinked_at = NULL`).

| | 값 |
|---|---|
| `POST /cameras/pair` 응답 | `reused: true`, **`revived: true`**(새 필드) |
| `cameras.id`, `camera_id` | **종전 값 그대로** — 앱이 들고 있던 UUID 가 다시 유효해집니다 |
| 과거 영상 (`motion_clips`) | 그대로 그 행에 연결돼 **다시 보입니다** |
| `name`, `enclosure_id`, `rotate_180` | **요청값으로 초기화** (이름 = 페어링 때 보낸 이름, 사육장 = 요청에 없으면 NULL, 회전 = false) |
| 반려동물 배정 (`pet_camera_assignments`) | **닫힌 이력만 보존**됩니다. 해제 때 `enclosure_id` 가 NULL 이 되면서 트리거가 열린 배정을 모두 닫았고, 되살릴 때는 재등록에서 지정한 사육장 기준으로 **새 배정이 열립니다**(사육장을 안 고르면 열리지 않음). "사육장 없음 + 열린 배정" 조합은 생기지 않습니다 |
| 허브 보드 | `cameras`·`devices` 짝 행을 둘 다 되살립니다 |

- 더 오래된 해제 행들은 그대로 남습니다. 그 영상은 "계정 단위 영상 조회" Stage 에서 따로 다룹니다.
- 앱의 `cameras-rt` Realtime 구독(`my_cage_providers.dart:163`)은 cameras 변경마다 `listAll()` 을 다시 읽으므로, 되살아난 행은 별도 처리 없이 목록에 돌아옵니다. 해제된 카메라를 따로 로컬 캐시하는 곳이 있다면 같은 UUID 가 돌아와도 깨지지 않는지만 확인해 주세요.

## 2. 다른 계정이 같은 보드를 등록하면 옛 행에 `superseded_at` 이 찍힙니다 (앱 변경 4곳)

보드를 다른 계정이 등록해도 서버는 **페어링 시점에는 이전 계정 행을 건드리지 않습니다**(`hw_id` 는 검증 없는 값이라 남의 카메라를 끊는 근거가 못 됩니다). 대신 브리지가 10분마다 "새 행 온라인 **그리고** 이전 행 24시간 이상 오프라인" 을 확인한 뒤 이전 행에 `superseded_at` 을 찍습니다. **해제가 아닙니다** — `unlinked_at` 은 NULL 그대로, 설정·영상도 그대로입니다.

- 보드가 옛 자격증명으로 다시 접속하면 서버가 `superseded_at` 을 NULL 로 되돌립니다(자동 복구).
- 숨겨진 카메라를 **원래 계정이 다시 등록**해도 `superseded_at` 이 NULL 로 돌아오고 목록에 다시 보입니다(PR #61).
- 30일 동안 재접속이 없으면 MQTT 계정만 해지합니다(`mqtt_revoked_at`). 행은 남습니다. 해지 뒤라도 원래 계정이 다시 등록하면 되돌아옵니다.

### 앱이 할 일

앱은 cameras 를 전부 받아온 뒤 Dart 에서 `unlinked_at` 을 거르고 있습니다(컬럼 미배포 시 400 을 피하려는 기존 방식). 같은 자리에 `superseded_at` 조건을 붙여 주세요. **4곳**입니다.

| 위치 (tera-ai-flutter) | 하는 일 | 바꿀 것 |
|---|---|---|
| `lib/features/my_cage/data/camera_repository.dart:30` `listAll()` | 카메라 목록 | superseded 행 제외 |
| `lib/features/my_cage/data/camera_repository.dart:38` `getById()` | 카메라 1대 조회 | superseded 면 null |
| `lib/features/my_cage/data/redesign_group_repository.dart:120` | 기기 관리 목록 | superseded 행 제외 |
| `lib/features/my_cage/data/device_add_registration_repository.dart:69` `owned()` | "이 계정 기기 행이 아직 있나" 판정 | superseded 면 null(다른 계정으로 넘어간 것으로 처리) |

```dart
// camera_repository.dart:30 — 지금
.where((r) => r['unlinked_at'] == null)
// 변경
.where((r) => r['unlinked_at'] == null && r['superseded_at'] == null)

// camera_repository.dart:38 — 지금
if (list.isEmpty || list.first['unlinked_at'] != null) return null;
// 변경
if (list.isEmpty ||
    list.first['unlinked_at'] != null ||
    list.first['superseded_at'] != null) return null;
```

나머지 두 곳도 같은 모양입니다. `owned()` 는 select 목록에 `superseded_at` 을 추가해야 합니다(지금 `'id,last_seen_at,hw_id,is_online,unlinked_at'`).

- Realtime: `cameras-rt` 가 변경마다 `listAll()` 을 다시 읽으므로, `listAll()` 만 고치면 숨김·복구가 목록에 자동 반영됩니다.
- `GET /cameras`(서버 API)는 서버가 같은 필터를 적용합니다. 단건 `GET /cameras/{id}` 와 영상 조회는 필터하지 않으니, superseded 행의 영상은 지금처럼 촬영 당시 계정 소유로 남습니다.
- 이름 중복 번호(`device_add_registration_repository.dart:35`)는 지금처럼 `unlinked_at` 만 봐도 됩니다. superseded 카메라 이름이 번호 계산에 남는 정도라 급하지 않습니다.

## 3. 새 컬럼 (`cameras`)

| 컬럼 | 뜻 |
|---|---|
| `superseded_at` | 다른 계정이 가져간 보드의 옛 행 — 목록에서 숨김. NULL = 정상 |
| `superseded_by` | 밀어낸 새 `cameras.id` |
| `supersede_suspect_at` | 이 행이 밀어낸 옛 행의 보드가 돌아와 복구된 시각 — 운영 확인용, 앱 비노출 |
| `mqtt_revoked_at` | superseded 30일 뒤 MQTT 해지 시각 — 앱 비노출 |

## 4. 확인 부탁

- [ ] 2번 표의 4곳에 `superseded_at` 조건 추가 (서버는 배포 완료, 지금 넣어도 됨)
- [ ] 해제 → 재페어링 시 같은 UUID 가 돌아오는 흐름에서 로컬 캐시가 깨지지 않는지
