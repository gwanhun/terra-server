# LCD 화면보호기(컬러 사진) · 전원 · 소등 시간대 — 앱 핸드오프 (2026-10-28)

> 서버·펌웨어 Stage N ([specs/stage-n-lcd-saver-power.md](../specs/stage-n-lcd-saver-power.md)). 상세 계약은 [API.md §4.14](API.md).

## 기능 요약

1. **LCD 전원** — 백라이트 켜기/끄기. 끄기는 소등 시간과 무관하게 유지, 켜기로만 풀림.
2. **소등 시간대** — 예: 23:00~07:00 (KST). 기기가 자기 시계로 판정해서 **앱·서버가 꺼져 있어도, 재부팅해도** 지킴.
3. **화면보호기** — 사용자가 올린 **컬러 사진**(160×128)과 온습도 화면을 N초/M초로 교대(`rotate`), 또는 **사진만**(`image`). 사진은 기기 플래시에 저장돼 재부팅·WiFi 없이도 유지.

## 지원 여부 판단

`devices.capabilities.lcd_saver === true` 인 기기만. 아니면 UI 를 숨기거나 "펌웨어 업데이트 필요" 안내.
서버도 플래그 없는 기기에 400 `unsupported_firmware` 를 돌려준다.

## 엔드포인트

| 할 일 | 호출 |
|---|---|
| 현재 설정 읽기 | `GET /devices/{id}/lcd` → `lcd_power`, `lcd_quiet_start/end`, `lcd_saver`, `lcd_image_key`, `lcd_image_set_at`, `lcd_updated_at`, **`preview_url`**(확정된 사진 PNG, presigned 1시간). `devices` Realtime 으로 같은 컬럼을 구독해도 됨(`preview_url` 은 REST 만) |
| 전원 | `PUT /devices/{id}/lcd/power` `{on: true|false}` |
| 소등 시간 설정/해제 | `PUT /devices/{id}/lcd/quiet` `{start:"23:00", end:"07:00"}` / `DELETE …/lcd/quiet` |
| 화면보호기 | `PUT /devices/{id}/lcd/saver` `{enabled, mode:"rotate"|"image"(기본 rotate), image_sec(5~300, 기본 20), info_sec(3~300, 기본 10), show_offline_icon(기본 true)}` — `image` 는 사진만(온습도 화면 안 나옴) |
| 사진 올리기/지우기 | `POST /devices/{id}/lcd/image` **multipart `file`** (PNG/JPG ≤5MB) / `DELETE …/lcd/image` |

모두 201 `{id, action, status:"pending"}` (사진은 `sha256`, `preview_url` 추가). 명령 결과는 기존처럼 `commands` Realtime 으로.

## 앱 UX 에서 꼭 지켜줄 것

- **미리보기.** 업로드 응답의 `preview_url` 이 기기 화면과 같은 색(RGB565)·크롭이다. 세로 사진은 가운데를 가로로 잘라내니
  사용자에게 보여주고 확인받기. 나중에 "지금 올라간 사진" 은 `GET /lcd` 의 `preview_url` 로 다시 받을 수 있다.
- **사진 명령은 ack 가 늦다.** 기기가 서버에서 40KB 를 받아 플래시에 쓰고 검증한 뒤 ack 하므로 보통 1~3초, 느린 WiFi 면 10초까지.
  그동안 "기기로 전송 중…" 표시. 실패 result: `dl_connect` / `dl_read` / `http_NNN` / `sha_mismatch` / `flash_write` / `busy`(이미 다운로드 중, 잠시 뒤 재시도) / `no_token`(기기 재페어링 필요).
- 같은 사진을 다시 보내면 기기가 다운로드 없이 바로 ok 한다(플래시에 같은 sha 가 있을 때).
- 소등 시간 입력은 KST `HH:MM`. 시작>끝이면 자정 넘는 구간(23:00~07:00). 시작==끝은 400.
- 확정 시점은 **기기 ACK ok** — 201 직후엔 옛 값, 이후 Realtime 으로 새 값. 기기 오프라인이면 안 바뀐다(기존 lcd_text 와 같은 규칙).
- 화면보호기 사진 중 서버 연결이 끊기면 우상단에 빨간 아이콘이 뜬다(`show_offline_icon:false` 로 끌 수 있음). 연결 중엔 사진만.

## 기기 동작 (참고)

```
켜짐 = 전원 ON && !(소등 창 설정됨 && 시각 동기됨 && 지금 ∈ 창)
```
- 시각 미동기(핫스팟 콜드부트 등)면 소등 창을 몰라 켜둔다(안전 쪽).
- 화면보호기는 사진이 있을 때만 교대. 사진 없이 `enabled:true` 면 온습도만.
- 사진 다운로드 중엔 이전 사진이 지워지고 온습도 화면(단일 저장 영역). 실패해도 온습도 화면으로 남는다.
- 규칙은 기기 NVS, 사진은 기기 플래시에 저장 → `erase_flash` 하면 사라진다(서버 값과 어긋날 수 있음).

## 배포 상태

- [x] 운영 DB 마이그레이션 `migrations/2026-10-28_devices_lcd_saver.sql` (2026-11-03)
- [x] 서버 코드 main 머지 (PR #32, 2026-11-03) — 재배포는 `uv sync` 포함(`python-multipart`)
- [x] 펌웨어 OTA 릴리스 등록 (2026-11-03): nano `terra-fw 1.2.0-20261103`(release b7d2e2dc), hub `terra-hub-p4 0.4.0-20261103`(release b7c49bc4)
- ⚠️ **허브(hub_p4) 개발은 2026-11-03 부로 중단** — 이 기능의 실기 검증·후속 수정은 **nano 만**. 허브 0.4.0 은 등록만 된 상태(미검증)
- [ ] 실기 검증 — 1대 선검증 후 전개

앱 적용 상세는 [APP_GUIDE_LCD_SAVER_2026-11-03.md](APP_GUIDE_LCD_SAVER_2026-11-03.md).
