# LCD 문구 서버 저장 — 회신 (2026-10-07)

> 요청: tera-ai-flutter `docs/handoffs/2026-10-06-lcd-text-server-request.md`

## 결론

`devices` 에 컬럼 2개를 추가했어요. 앱은 기존 `devices` SELECT·Realtime 구독으로 그대로 읽으면 됩니다.

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `lcd_text` | text, nullable | 지금 기기 LCD 상단에 떠 있는 커스텀 문구(요청 원문 그대로) |
| `lcd_text_updated_at` | timestamptz, nullable | 위 값을 확정한 시각(ACK 수신 시각) |

## 확정 시점 — **기기 ACK 기준** (요청 1-3)

- `POST /devices/{id}/lcd` `{text}` → 기기가 `result=ok` 로 ACK 하면 `lcd_text = text`.
- `POST /devices/{id}/lcd/clear` 또는 빈 텍스트 → ok ACK 시 `lcd_text = null`.
- 기기 오프라인 · TTL(30초) 만료 · `no_ack` · ok 아닌 결과 → **값을 바꾸지 않음**(화면도 안 바뀌었으므로).
- 따라서 API 201 응답 직후엔 아직 옛 값이고, 보통 1~2초 뒤 Realtime 으로 새 값이 옵니다.

## `null` 의 뜻

- 기본값("TERRA IOT") 표시 중, **또는** 이 기능 배포 전에 설정해서 서버가 모르는 경우.
- 둘을 구분하지 않습니다. 앱이 말한 순서(서버 값 → 휴대폰 저장값 → 기기 ID)가 맞아요.
  다만 `lcd_text_updated_at` 이 있고 `lcd_text` 가 null 이면 "서버가 확인한 기본값" 이라
  휴대폰 저장값보다 이 쪽을 믿어도 됩니다.

## 그 외

- 명령 이력: `commands.payload.lcd_text` 에도 원문이 남습니다(기기로는 발행하지 않음).
- 입력 상한은 그대로 서버 64자.

## 배포 상태

- [x] 운영 DB 마이그레이션 `migrations/2026-10-07_devices_lcd_text.sql` 적용
- [ ] 서버 배포
- 둘 다 끝나면 이 문서에 체크해서 알려 드릴게요.
