# 사육장 ↔ 카메라/디바이스 매칭 API (프론트 전달용)

> 목적: 계정에 사육장·카메라·디바이스가 여러 개일 때 **"어느 사육장에 무엇이 설치됐는지"** 를
> 화면에 보여주고, 배정/해제할 수 있게 한다.

공통:
- 인증: 모든 엔드포인트 `Authorization: Bearer <JWT>` 필요. 누락/실패 → `401`.
- 소유권: 본인 리소스만 접근. 남의 리소스는 `404` (존재 여부 노출 안 함).
- 시각 필드는 전부 ISO8601 문자열.

---

## 1. 화면: 사육장 목록

`GET /enclosures`

각 사육장에 **설치된 카메라/디바이스 대수**가 함께 온다. 목록 카드에 "카메라 2 · 기기 1" 뱃지로 쓰면 됨.

```jsonc
[
  {
    "id": "enc-uuid",
    "name": "거실 사육장",
    "species": "bearded_dragon",   // nullable
    "note": null,                   // nullable
    "camera_count": 2,              // ★ 신규
    "device_count": 1,              // ★ 신규
    "created_at": "2026-05-27T00:00:00Z",
    "updated_at": "2026-05-27T00:00:00Z"
  }
]
```

정렬: 생성 시각 내림차순. 페이지네이션 없음.

---

## 2. 화면: 사육장 상세 (무엇이 매칭됐는지)

`GET /enclosures/{id}`

목록 필드 + 소속 **카메라/디바이스 배열**이 nested 로 온다. 상세 화면에서 그대로 리스트 렌더.

```jsonc
{
  "id": "enc-uuid",
  "name": "거실 사육장",
  "species": "bearded_dragon",
  "note": null,
  "camera_count": 1,
  "device_count": 1,
  "created_at": "2026-05-27T00:00:00Z",
  "updated_at": "2026-05-27T00:00:00Z",

  "cameras": [                       // ★ 신규
    {
      "id": "cam-uuid",              // PATCH/삭제/스트리밍 호출 시 쓰는 UUID
      "camera_id": "p4cam-a1b2c3d4", // MQTT client_id (표시/디버깅용)
      "name": "거실 캠",
      "model": "esp32-p4",           // nullable
      "is_online": true,
      "stream_mode": null,           // null | "snapshot" | "webrtc"
      "last_seen_at": "2026-05-27T00:00:00Z"  // nullable
    }
  ],
  "devices": [                       // ★ 신규
    {
      "id": "dev-uuid",
      "device_id": "terra-a1b2c3d4",
      "name": "거실 컨트롤러",
      "species": "bearded_dragon",   // nullable
      "is_online": false,
      "last_seen_at": null           // nullable
    }
  ]
}
```

> `cameras`/`devices` 안의 필드는 요약본이다. 카메라 상세 설정(fps, resolution, clip_sec 등)이
> 필요하면 `GET /cameras/{id}` 를 따로 호출.

---

## 3. 매칭(배정/해제) 방법

배정은 "카메라/디바이스 쪽에 사육장 id 를 심는" 방식. 사육장 API 로 하는 게 아니다.

### 카메라를 사육장에 배정/해제
`PATCH /cameras/{camera_uuid}`

```jsonc
// 배정
{ "enclosure_id": "enc-uuid" }
// 해제(단독 카메라로)
{ "enclosure_id": null }
```

### 디바이스를 사육장에 배정/해제  ★ 신규
`PATCH /devices/{device_uuid}`

```jsonc
{ "enclosure_id": "enc-uuid" }   // 배정
{ "enclosure_id": null }         // 해제
```

- `camera_uuid`/`device_uuid` 는 위 배열의 `id`(UUID) 를 쓴다 (`camera_id`/`device_id` 아님).
- 응답: 수정된 카메라/디바이스 객체 전체 (`enclosure_id` 포함).

### 에러
| 상황 | 코드 |
|---|---|
| 남의(또는 없는) 사육장 id 로 배정 시도 | `400` `enclosure_id 가 본인 사육장이 아님.` |
| 본인 카메라/디바이스가 아님 | `404` |
| 변경 필드 없음 (빈 body) | `400` |

---

## 4. 페어링 시 바로 배정 (선택)

새 기기를 등록하면서 처음부터 사육장에 넣고 싶으면 pair 요청에 `enclosure_id` 를 넣는다.

- `POST /cameras/pair` — 기존부터 지원
- `POST /devices/pair` — ★ 신규 지원

```jsonc
{ "name": "거실 캠", "enclosure_id": "enc-uuid" }   // enclosure_id 는 옵션, 생략 시 단독
```

남의 사육장 id 면 `400` (등록 자체가 안 됨).

---

## 5. 프론트 구현 순서 제안

1. 사육장 목록 화면 → `GET /enclosures` 로 카드 + `camera_count`/`device_count` 뱃지.
2. 사육장 상세 → `GET /enclosures/{id}` 로 `cameras`/`devices` 리스트.
3. "카메라 옮기기/연결" UI → 사육장 선택 후 `PATCH /cameras/{id}` (또는 `/devices/{id}`) 에
   `enclosure_id` 전송, 성공 시 상세 재조회.
4. `is_online`, `last_seen_at` 으로 온라인 뱃지 표시.

## 참고
- 서버 스키마 변경 없음 (`enclosure_id` 컬럼은 원래 존재). 마이그레이션 불필요.
- 삭제 정책: 사육장 삭제 시 소속 카메라/디바이스는 지워지지 않고 `enclosure_id` 만 `NULL`(단독).
