# 앱 적용 가이드 — LCD 화면보호기(컬러 사진) · 전원 · 소등 시간대 (2026-11-03)

> 대상: tera-ai-flutter 앱팀. 서버 Stage N([specs/stage-n-lcd-saver-power.md](../specs/stage-n-lcd-saver-power.md)) 은 **main 머지·운영 DB 마이그레이션 완료**, 펌웨어는 OTA 릴리스 등록됨(nano `terra-fw 1.2.0`, hub `terra-hub-p4 0.4.0`).
> 계약 원문은 [API.md §4.14](API.md), 명령 어휘는 [MQTT.md](MQTT.md). 이 문서는 "앱에서 어떻게 붙이나" 만 다룬다.
> 상세 회신 이력: [BACKEND_HANDOFF_REPLY_LCD_SAVER_2026-10-28.md](BACKEND_HANDOFF_REPLY_LCD_SAVER_2026-10-28.md)

## 0. 한 장 요약

| 기능 | 사용자가 보는 것 | 앱이 호출하는 것 | 기기에서 일어나는 일 |
|---|---|---|---|
| LCD 전원 | 켜기/끄기 토글 | `PUT /devices/{id}/lcd/power {on}` | 백라이트 0/100%. 끄기는 소등 시간과 무관하게 유지 |
| 소등 시간대 | "23:00~07:00 에 끄기" | `PUT /devices/{id}/lcd/quiet {start,end}` / `DELETE` | 기기가 자기 시계로 판정. 앱·서버·WiFi 없어도, 재부팅해도 지킴 |
| 화면보호기 | 모드(교대/사진만) + 초 입력 | `PUT /devices/{id}/lcd/saver {enabled, mode, image_sec, info_sec, show_offline_icon}` | `rotate`: 사진 N초 ↔ 온습도 M초, `image`: 사진만 |
| 사진 | 갤러리에서 고름 → 미리보기 → 확인 | `POST /devices/{id}/lcd/image` (multipart `file`) / `DELETE` | 서버가 160×128 컬러로 변환 → 기기가 HTTP 로 받아 플래시 저장 |
| 현재 상태 | 설정 화면 초기값 | `GET /devices/{id}/lcd` 또는 `devices` Realtime | — |

모든 쓰기 호출은 **명령을 큐잉**하고 201 `{id, action, status:"pending"}` 을 돌려준다. 실제 반영은 기기가 `result=ok` 로 ACK 한 뒤 `devices.lcd_*` 에 확정된다 — 기존 `lcd_text` 와 같은 규칙.

## 1. 지원 여부 — 먼저 이걸 본다

```
devices.capabilities.lcd_saver === true
```
- `true` 가 아니면(구 펌웨어·LCD 없는 허브 빌드) 화면보호기/전원/소등 UI 를 **숨기거나** "펌웨어 업데이트가 필요해요" 로 안내.
- 서버도 플래그 없는 기기에 **400** `unsupported_firmware: …` 를 돌려주니 UI 를 못 숨겨도 안전하다. 기존 LCD 문구(`POST /lcd`)는 게이트 없음.
- 플래그는 기기가 telemetry 로 보고해 서버가 `devices.capabilities` 에 반영한다. OTA 직후 첫 telemetry(3초) 뒤에 바뀜 → Realtime 으로 `capabilities` 변경을 받으면 UI 를 열면 된다.

## 2. 상태 읽기

### 2-1. REST (설정 화면 진입 시)
`GET /devices/{id}/lcd` → 200
```json
{
  "device_id": "…",
  "lcd_text": "밥 6시", "lcd_text_updated_at": "…",
  "lcd_power": true,
  "lcd_quiet_start": "23:00:00", "lcd_quiet_end": "07:00:00",
  "lcd_saver": {"enabled": true, "mode": "rotate", "image_sec": 20, "info_sec": 10, "show_offline_icon": true},
  "lcd_image_key": "lcd/<uuid>/3f9a1c2e",
  "lcd_image_set_at": "2026-11-03T02:10:00+00:00",
  "lcd_updated_at": "2026-11-03T02:10:00+00:00",
  "preview_url": "https://…r2…/lcd/<uuid>/3f9a1c2e.png?X-Amz-…"
}
```
- `lcd_quiet_start/end` 는 Postgres TIME 문자열(`HH:MM:SS`, KST 벽시계). NULL 둘 다 = 소등 없음.
- `lcd_saver` NULL = 아직 한 번도 설정 안 함 → 기기 기본은 **꺼짐**. UI 기본값은 `{enabled:false, mode:"rotate", image_sec:20, info_sec:10, show_offline_icon:true}` 로 그리면 된다.
- `lcd_image_key` NULL = 사진 없음. 있으면 `preview_url`(presigned, **1시간** 유효) 로 "지금 올라간 사진" 을 보여준다. 만료되면 GET 을 다시 부른다. 캐시하지 말 것.

### 2-2. Realtime (설정 화면 열어 둔 동안)
`devices` 행 구독에 이미 `lcd_text` 를 쓰고 있으면 같은 채널에서 `lcd_power`, `lcd_quiet_start`, `lcd_quiet_end`, `lcd_saver`, `lcd_image_key`, `lcd_image_set_at`, `lcd_updated_at` 이 함께 온다. `preview_url` 은 Realtime 에 없다 — `lcd_image_key` 가 바뀌면 `GET /lcd` 한 번.

## 3. 쓰기 — 호출별 주의점

### 3-1. 전원 `PUT /devices/{id}/lcd/power`
```json
{"on": false}
```
- `false` 는 **끈끈하다(sticky)**: 소등 시간이 끝나도 안 켜진다. `true` 로만 풀림. UI 문구: "끄기 — 다시 켤 때까지 꺼져 있어요".
- ack `state`: `LCD_ON` / `LCD_OFF`.

### 3-2. 소등 시간대 `PUT /devices/{id}/lcd/quiet` · `DELETE`
```json
{"start": "23:00", "end": "07:00"}
```
- `HH:MM` KST. `start > end` 면 자정 넘는 구간(위 예: 밤 11시 → 아침 7시). `start == end` 는 400.
- 서버가 분 단위 + `tz_offset_min: 540` 으로 바꿔 기기에 보낸다. 앱은 KST 만 신경 쓰면 된다.
- 해제는 `DELETE …/lcd/quiet` (body 없음) → 201 `{action:"lcd_quiet_clear"}`.
- 기기 시계가 아직 안 맞으면(핫스팟 콜드부트 등) **켜 둔다**(안전 쪽). 보통 부팅 10초 내 SNTP 로 맞는다.

### 3-3. 화면보호기 `PUT /devices/{id}/lcd/saver`
```json
{"enabled": true, "mode": "rotate", "image_sec": 20, "info_sec": 10, "show_offline_icon": true}
```
| 필드 | 범위/기본 | 뜻 |
|---|---|---|
| `enabled` | 필수 | 켜기/끄기 |
| `mode` | `"rotate"`(기본) / `"image"` | `rotate` = 사진 ↔ 온습도 교대, `image` = 사진만(온습도 화면 안 나옴) |
| `image_sec` | 5~300, 기본 20 | 사진 유지 초 (`rotate` 만 의미) |
| `info_sec` | 3~300, 기본 10 | 온습도 유지 초 (`rotate` 만 의미) |
| `show_offline_icon` | 기본 true | 사진 중 서버 끊김이면 우상단 빨간 아이콘 |

- 범위 밖·모르는 `mode` → **422**(Pydantic). 기기 쪽 검증도 있어 `bad_request` ack 가 올 수 있지만 서버에서 먼저 걸린다.
- **사진이 없으면** `enabled:true` 여도 온습도만 나온다. UI 에서 "사진을 먼저 올려 주세요" 안내 권장.
- 소등 시간·수동 끄기가 **항상 우선**. 사진만 모드여도 새벽엔 꺼진다.
- `image_sec`/`info_sec` 를 안 보내면 기본값이 들어가 **기존 값을 덮는다**(부분 수정 아님). 항상 5개를 다 보낼 것.

### 3-4. 사진 `POST /devices/{id}/lcd/image` (multipart) · `DELETE`
```
POST /devices/{id}/lcd/image
Content-Type: multipart/form-data; boundary=…
--…
Content-Disposition: form-data; name="file"; filename="photo.jpg"
Content-Type: image/jpeg
<bytes>
```
- 원본 ≤ **5MB**(PNG/JPG/HEIC 는 Pillow 가 읽는 포맷만 — HEIC 는 앱에서 JPG 로 바꿔 보내는 게 안전). EXIF 회전 반영.
- 변환: 비율 유지 **cover-fit 160×128**(가운데 크롭) → RGB565. **세로 사진은 가운데 띠만 남는다** — 업로드 전에 앱에서 5:4 로 크롭 UI 를 주면 결과가 훨씬 낫다.
- 응답 201:
  ```json
  {"id":"<command id>","action":"lcd_image","status":"pending",
   "sha256":"3f9a1c2e…","preview_url":"https://…/3f9a1c2e.png?…"}
  ```
  `preview_url` 이 **기기 화면과 같은 색·크롭**이다. 바로 보여주고 "이렇게 표시돼요" 확인.
- 이 명령은 **ack 가 늦다**: 기기가 서버에서 40KB 를 받아 플래시에 쓰고 검증한 뒤 ack → 보통 1~3초, 느린 WiFi 10초, 서버 `no_ack` 판정은 30초. 그동안 "기기로 보내는 중…" 스피너.
- 같은 사진을 다시 올리면 기기가 다운로드 없이 바로 ok(플래시에 같은 sha 가 있을 때).
- 연속으로 두 장 올리면 뒤 것이 이긴다. 앞 명령이 아직 pending 이면 그 사진도 서버에 보존되니 404 는 안 난다.
- 원본은 서버에 **저장하지 않는다**. 서버엔 변환본(.rgb565)과 미리보기(.png)만.
- 삭제 `DELETE …/lcd/image` → `lcd_image_clear`. 기기는 메타만 지우고 화면보호기는 온습도로 떨어진다.
- R2 미설정 환경(로컬 dev) 은 503.

## 4. 명령 결과 받기 — `commands` Realtime

기존 명령 처리와 동일하게 `commands` 행의 `status`/`result`/`state` 를 본다.

| action | ok 일 때 `state` | 실패 `result` (사용자 문구 예) |
|---|---|---|
| `lcd_power` | `LCD_ON` / `LCD_OFF` | `bad_request` |
| `lcd_quiet` / `lcd_quiet_clear` | `LCD_QUIET` / `LCD_QUIET_CLR` | `bad_request` |
| `lcd_saver` | `LCD_SAVER` | `bad_request` |
| `lcd_image` | `LCD_IMG` | `dl_connect`("기기가 서버에 연결하지 못했어요"), `dl_read`("다운로드가 끊겼어요"), `http_401/404`("사진을 찾지 못했어요, 다시 올려 주세요"), `sha_mismatch`("전송 오류, 다시 시도"), `flash_write`("기기 저장 실패"), `busy`("다른 사진을 받는 중이에요, 잠시 후"), `no_token`(기기 재페어링 필요) |
| `lcd_image_clear` | `LCD_IMG_CLR` | — |
| 공통 | — | `unknown_action`(구 펌웨어 — §1 게이트가 막아야 정상), `expired`(기기가 30초 넘게 오프라인), `no_ack`(서버가 30초 내 응답 못 받음), `device_offline` |

- **확정값 갱신 시점** = `result == "ok"`. 그때 `devices.lcd_*` 가 바뀌고 Realtime 으로 온다. ok 가 아니면 서버 값은 그대로(화면도 안 바뀌었으니).
- `lcd_image` ok 뒤 `lcd_image_key` 가 새 sha 로 바뀐다. 이전 사진의 미리보기 URL 은 다음 업로드 때 정리되므로 **키가 바뀌면 `GET /lcd` 로 `preview_url` 을 새로 받는다**.

## 5. 권장 UI 흐름

1. 기기 상세 → "LCD" 섹션. `capabilities.lcd_saver` 없으면 섹션 자체를 "펌웨어 업데이트 필요" 카드로.
2. 진입 시 `GET /lcd` 한 번 → 토글·시간·모드·초·미리보기 채움.
3. 전원/소등/보호기는 변경 즉시 PUT → 버튼에 pending 표시 → `commands` ok 오면 해제, 실패면 토스트 + 이전 값 복원(Realtime `devices` 값이 진실).
4. 사진: 고르기 → (가능하면 5:4 크롭) → POST → 응답 `preview_url` 즉시 표시(기기 반영 전이라 "전송 중" 뱃지) → `lcd_image` ok 오면 뱃지 제거. 실패면 §4 문구.
5. 화면보호기 켤 때 사진이 없으면 사진 올리기로 유도.

## 6. 기기 동작 — 사용자에게 설명할 때

```
화면 켜짐 = 전원 ON  AND  NOT (소등 시간 설정됨 AND 기기 시계 OK AND 지금이 소등 시간)
```
- 소등 중 재부팅해도 켜지지 않는다(기기 NVS 에 규칙 저장).
- 화면보호기 사진 중 서버 연결이 끊기면 우상단에 빨간 아이콘(끌 수 있음). 연결 중엔 사진만 꽉 차게.
- 사진 다운로드 중엔 잠깐 온습도 화면(단일 저장 영역이라 이전 사진을 먼저 지움). 실패해도 온습도 화면으로 남고 크래시 없음.
- 기기를 `erase_flash` 하면 사진·규칙이 사라진다. 그때 서버 값과 어긋나므로 앱은 Realtime 값을 "서버가 마지막으로 확인한 값" 으로만 표시.

## 7. 테스트 체크리스트 (앱 QA)

- [ ] 구 펌웨어 기기: 섹션 숨김 또는 업데이트 안내, 400 `unsupported_firmware` 가 토스트로 새지 않음
- [ ] 전원 끄기 → 소등 시간 지나도 안 켜짐 → 켜기 → 켜짐
- [ ] 소등 23:00~07:00 설정 → `lcd_quiet_start/end` Realtime 반영 → 해제 → NULL
- [ ] 보호기 `rotate` 20/10 → 기기 교대 확인 / `image` → 사진 고정 / `enabled:false` → 온습도만
- [ ] 세로 사진 업로드 → 미리보기 크롭 확인 → ack ok → `preview_url` 재조회 시 같은 사진
- [ ] 같은 사진 재업로드 → ok 가 즉시(1초 내) 옴
- [ ] 기기 전원 끈 상태에서 사진 업로드 → `expired`/`no_ack` 문구 → 켜도 자동 재전송 없음(사용자 재시도)
- [ ] 5MB 초과·비이미지 파일 → 400 문구

## 8. 배포 상태

- [x] 서버 코드 main 머지 (PR #32, 2026-11-03)
- [x] 운영 DB 마이그레이션 `2026-10-28_devices_lcd_saver.sql`
- [ ] 서버 재배포 (`uv sync` 포함 — `python-multipart` 신규 의존성)
- [x] 펌웨어 OTA 릴리스 등록: nano `terra-fw 1.2.0-20261103`, hub `terra-hub-p4 0.4.0-20261103`
- [ ] 실기 검증(1대 선검증 후 전개) — 완료되면 이 문서에 체크해서 알림
