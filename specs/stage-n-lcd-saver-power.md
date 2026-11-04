# Stage N — LCD 화면보호기(컬러 사진 ↔ 온습도 교차) + LCD 전원/소등 시간

> 상태: 🚧 코드 완료·3트리 빌드 통과·실기 미검증 (2026-10-28). 설계 2026-10-26(같은 날 **컬러 직행으로 개정**). 사용자 요청 2건을 한 스테이지로 묶음.
> 1. 화면보호기 — 원하는 **컬러 사진**을 LCD 에 띄우고, 온습도 화면과 **교차로** 보여주기. 전환 시간은 사용자가 정함
> 2. LCD on/off — 수동 on/off + 새벽 등 **원하는 시간대에 꺼지도록**
>
> 개정 이유: 처음 안은 nano NVS(24KB) 한계로 1비트 흑백만 넣고 컬러를 V2 로 미뤘다. 그런데 1.8" 에 디더링 흑백 사진은
> "사진" 이라 부르기 어렵고, 1비트 전용 경로(MQTT 버퍼 상향·base64·NVS blob)를 만들었다 버리게 된다.
> 조사해 보니 nano 플래시 끝에 OTA 슬롯 밖 **128KB 빈 영역**이 있어 RGB565 40KB 를 그대로 둘 수 있고,
> 전송은 OTA 바이너리 다운로드 경로를 재사용하면 된다 → 1비트 단계를 없애고 컬러로 바로 간다.

## In / Out

**In**
- 앱/웹에서 사진 업로드 → 서버가 160×128 RGB565 로 변환해 R2 에 저장 → 기기가 HTTP 로 받아 플래시에 저장, 재부팅 후에도 유지(재다운로드 불필요)
- 화면보호기 모드: 사진 `image_sec` 초 ↔ 온습도 화면 `info_sec` 초 교대. **둘 다 사용자 지정**(5~300 / 3~300). on/off 가능
- LCD 수동 on/off (백라이트)
- 소등 시간대(예: 23:00~07:00) — 기기 로컬 시각 기준, 서버·앱이 꺼져 있어도 기기 혼자 지킴
- 현재 설정을 앱이 Realtime 으로 읽을 수 있게 `devices` 에 확정값 저장 (lcd_text 와 같은 방식)
- 앱이 "지금 올라간 사진" 을 다시 보여줄 수 있게 미리보기 PNG 도 R2 에 보관

**Out (이번 스테이지 아님)**
- 여러 장 슬라이드쇼 — 1장만. (플래시 128KB 면 3장까지 들어가므로 포맷은 `slot` 필드로 열어둠, 구현은 안 함)
- 사진 위 온습도 오버레이 — 전체 화면 교대만 (§설계 메모)
- 기기 버튼/터치로 깨우기 — 입력 장치 없음
- 밝기(디밍) 조절 — on/off 만. 필요하면 `lcd_power.brightness` 로 확장 가능하게 payload 만 열어둠

## 대상 펌웨어

| 트리 | 적용 | 비고 |
|---|---|---|
| `terra-iot-nano` | ✅ | 베타 배포 기기. 플래시 4MB, 이미지 영역 `0x3E0000` + 128KB (OTA 슬롯 뒤 빈 공간). MQTT 버퍼 변경 없음 |
| `terra-iot-supermini` | ✅ | nano 와 공통 로직 동일 유지 규칙 (핀맵만 차이). 플래시 맵 같음 — 파티션 표 대조 후 같은 주소 |
| `terra-hub-p4` | ⏸️ | 0.4.0 까지 포팅·릴리스 등록 후 **2026-11-03 허브 개발 중단**(사용자). 이후 수정은 nano 만 |
| `terra-iot-nano-relay` | ❌ | 구 보드. `unknown_action` 으로 자연 거절 |

## 조사 결과 (설계 근거)

- **LCD 그리기는 센서 태스크 한 곳에서만** (`sensor_task` / `iot_lcd_update`, 3초 주기). MQTT 태스크는 플래그만 세움 — `lcd_band` 와 동일 규칙. 새 기능도 전부 이 규칙 따름
- **백라이트** `st7735_set_backlight(0~100)` (LEDC PWM, nano BLK=GPIO17 / hub Kconfig). `100` 을 쓰는 곳은 `st7735_init` 한 곳뿐 → 부팅 후엔 우리가 통제 가능. `st7735_reinit()`(60초 주기 설정 되쓰기)은 백라이트를 건드리지 않음 → off 상태가 뒤집히지 않음
- **시각**: nano 는 `esp_netif_sntp_init(DEFAULT_CONFIG)` → 1시간 주기 재동기(deinit 없음). 핫스팟+콜드부트면 미동기(`time < 1700000000`) 가능 → **시각 모르면 LCD ON 유지(fail-safe)**. TZ 는 펌웨어에 심지 않고 payload 로 `tz_offset_min` 받음 (서버 scheduling.py 가 KST 전제)
- **저장 공간 (개정)**: nano NVS 24KB 는 40KB 에 턱없이 부족. 대신 파티션 표(`partitions.csv`)를 보면 `ota_1` 이 `0x3E0000` 에서 끝나고 플래시는 `0x400000` 까지 → **128KB 가 어느 파티션에도 안 속함**. `esp_flash_write/erase_region` 은 파티션 항목 없이도 이 주소에 쓸 수 있고, `CONFIG_SPI_FLASH_DANGEROUS_WRITE_ABORTS=y` 가 막는 건 부트로더·파티션표·**실행 중인 앱 슬롯**뿐이라 이 영역은 허용. 파티션 표를 안 바꾸므로 **배포된 기기에 OTA 만으로 적용 가능**(파티션 표는 OTA 로 못 바꿈 — 그래서 "파티션 추가" 안은 기각)
- **전송 경로 (개정)**: MQTT 버퍼 2048 로 40KB 는 불가(쪼개도 base64 20여 조각). OTA 가 이미 쓰는 **HTTP 다운로드**(`ota.c download_to_slot`: esp_http_client + Bearer 기기 토큰 + sha256 검증)를 그대로 재사용. 서버도 `firmware.py download_bin` 의 토큰 검증 헬퍼 재사용
- **그리기 비용**: RGB565 40KB 를 SPI 4MHz 로 밀면 약 82ms — 지금 전체 화면 갱신과 같은 수준, 3초 주기라 무관. 플래시에서 **한 줄(320B)씩 읽어 바로 push** 하면 PSRAM/SRAM 상주 버퍼가 필요 없음
- **기존 레이아웃**(rotation=1, 160×128): y0~24 밴드, y32 온도, y54 습도, y74~ 로고, 우상단 WiFi 아이콘. `lcd_repaint_static()` 이 60초마다 정적 영역 재도색 → 이미지 모드일 땐 이걸 **막아야** 함(아니면 이미지 위에 로고가 덧그려짐)

## 아키텍처

```
앱/웹 ── POST /devices/{id}/lcd/image (PNG/JPG) ──▶ 서버 Pillow: EXIF 회전 → cover-fit 160×128 → RGB565 big-endian 40,960B
                                                 → R2  lcd/{device_id}/{sha8}.rgb565  +  lcd/{device_id}/{sha8}.png (미리보기)
                                                 → commands { action:"lcd_image", payload:{url, size, sha256, w, h, enc:"rgb565"} }
                                                 → 응답에 preview_url(presigned GET) — 앱이 "LCD 에 이렇게 보임" 미리보기
앱/웹 ── PUT  /devices/{id}/lcd/saver  {enabled,image_sec,info_sec} ──▶ commands lcd_saver
앱/웹 ── PUT  /devices/{id}/lcd/power  {on}                          ──▶ commands lcd_power
앱/웹 ── PUT  /devices/{id}/lcd/quiet  {start,end} | null            ──▶ commands lcd_quiet / lcd_quiet_clear
                      │
                      ▼ dispatcher publish (기존)
펌웨어: MQTT 태스크 ── lcd_image 만: 다운로드 태스크 기동(OTA prepare 와 같은 16KB 전용 태스크)
                        GET url (Bearer 기기 토큰) → 4KB 단위 플래시 erase+write → sha256 검증 → NVS `img` 메타 저장 → ack ok
                     ── 나머지: NVS 저장 + 플래그 ── sensor_task 가 3초 주기로 상태기계 평가 → 그리기/백라이트
                      │
                      ▼ ack result=ok
서버 handlers: devices.lcd_* 확정 (lcd_text 와 동일 — ok 아니면 안 바꿈) → 앱 Realtime
```

## 펌웨어 상태기계 (sensor_task, 매 사이클)

```
effective_on = lcd_power(NVS, 기본 true)
            && !(quiet 설정됨 && 시각 유효 && now_local ∈ [start, end))

if (!effective_on):
    screen = OFF    → 최초 진입 시 backlight(0) + fill_screen(BLACK). 이후 그리기 전부 스킵
                      (resync/reinit 는 계속 — 패널 설정 유지)
else if (saver.enabled && 이미지 유효(NVS 메타 있고 플래시 헤더 crc 일치)):
    mode=image → screen = IMAGE 고정 (사진만, 2026-11-03 사용자 요청)
    mode=rotate → IMAGE / INFO 를 image_sec / info_sec 로 교대 (사이클 3초 단위로 반올림)
else:
    screen = INFO

전환 시(이전 screen ≠ 현재):
    OFF→*     : backlight(100), 전체 다시 그리기 강제
    *→IMAGE   : st7735_draw_rgb565_flash(0,0,160,128, flash_addr+hdr)  — 한 줄씩 esp_flash_read → push
                WiFi 아이콘은 **끊겼을 때만** 빨간색으로 덧그림
    *→INFO    : lcd_repaint_static() + 밴드 force + 온습도
IMAGE 중      : 매 사이클 그리기 스킵, 단 reinit 가 일어난 사이클엔 이미지 재blit.
                online 상태가 바뀐 사이클엔 우상단만 갱신 — 끊김: 아이콘, 복귀: 해당 영역만 플래시에서 다시 읽어 blit
                lcd_repaint_static() 는 screen==INFO 일 때만
다운로드 중   : 화면은 그대로(INFO 또는 이전 이미지). 완료 후 다음 사이클부터 새 이미지. 실패 시 이전 이미지 유지
```

- **왜 기기가 시간대를 판정?** 서버 `schedules` 로 `lcd_off` 를 쏘는 방식(A안)은 그 순간 기기가 오프라인/재부팅이면 놓침 → 새벽에 재부팅되면 `st7735_init` 이 100% 로 켜서 아침까지 켜져 있음. 기기 NVS 에 규칙을 두면 네트워크 무관하게 지킴 (B안 채택)
- **밤새 넘는 구간**(23:00~07:00) 은 `start > end` 로 판정: `in = (m >= start || m < end)`
- **수동 off 는 끈끈함(sticky)**: `lcd_power=false` 면 quiet 와 무관하게 계속 꺼짐. 켜는 건 `lcd_power=true` 만. 두 축이 독립이라 설명이 쉬움
- 이미지 모드에선 WiFi 아이콘을 **연결 중엔 숨기고 끊겼을 때만** 표시 (사용자 요청 2026-10-26: 사진을 온전히 보고 싶음). 끊김 표시는 "화면보호기 중에 기기가 죽었는지" 를 알 유일한 단서라 유지. `lcd_saver.show_offline_icon:false` 로 완전 숨김 옵션 (기본 true)

## 이미지 플래시 영역 포맷

```
APP_LCD_IMG_FLASH_ADDR (nano/supermini 0x3E0000, hub 0x830000) , APP_LCD_IMG_FLASH_SIZE 0x20000
[0x0000] header 64B : magic "TLCD" | ver u8 | slot u8 | w u16 | h u16 | len u32 | crc32 u32 | seq u32 | sha256[32] | pad
[0x0040] data       : RGB565 big-endian, row-major, 160*128*2 = 40,960B
```
- 쓰기: `esp_flash_erase_region(addr, 0xB000)`(44KB, 4KB 정렬) → 데이터 먼저 → 헤더 마지막(중간에 전원 나가면 magic 없음 = 무효)
- 유효성: 부팅 시 헤더 magic+len+crc32 검사 1회 → 결과를 RAM 플래그로. NVS `img` 키엔 `{seq, sha8}` 만(지우기는 NVS 키 삭제, 플래시는 안 지움)
- 이 영역은 **이미지 전용으로 docs 에 못 박음**(`partitions.csv` 주석 + `docs/ota.md`). 나중에 coredump 등 다른 용도가 생기면 충돌 — 그땐 여기서 떼어 가되 주소를 Kconfig 로만 바꾼다

## 명령 계약 (MQTT payload)

| action | payload | ack state | NVS 키 (`lcd/*`) |
|---|---|---|---|
| `lcd_image` | `{url, size:40960, sha256, w:160, h:128, enc:"rgb565", slot:0}` | `LCD_IMG` | `img`(`{seq,sha8}`) — 플래시 저장 후 |
| `lcd_image_clear` | — | `LCD_IMG_CLR` | 삭제. saver 는 켜져 있어도 이미지 없으니 INFO 고정 |
| `lcd_saver` | `{enabled:bool, mode:"rotate"\|"image"(기본 rotate), image_sec:5~300, info_sec:3~300, show_offline_icon:bool(기본 true)}` | `LCD_SAVER` | `saver`(blob struct) |
| `lcd_power` | `{on:bool}` | `LCD_ON` / `LCD_OFF` | `pwr`(u8) |
| `lcd_quiet` | `{start_min:0~1439, end_min:0~1439, tz_offset_min:540}` | `LCD_QUIET` | `quiet`(blob struct) |
| `lcd_quiet_clear` | — | `LCD_QUIET_CLR` | 삭제 |

- `lcd_image` 는 **다운로드·검증·플래시 기록이 끝난 뒤** ack. 40KB 는 WiFi 에서 1~3초, dispatcher 의 30초 no_ack 안에 충분. 실패 result: `dl_connect` / `dl_read` / `sha_mismatch` / `flash_write` / `bad_request`(크기·enc 불일치) — OTA 와 같은 어휘
- 같은 `sha256` 이 이미 플래시에 있으면 다운로드 생략하고 바로 ok (앱이 같은 사진 재전송해도 마모 없음)
- `enc:"rgb565"` = 16비트 big-endian(ST7735 가 SPI 로 받는 바이트 순서 그대로 → 펌웨어 변환 없음)
- 구 펌웨어 → `unknown_action` (dispatcher 가 이미 처리)
- **capabilities 에 `lcd_saver: true` 추가** (telemetry·페어링 body) → 서버가 미지원 기기에 400 으로 선거절, 앱은 UI 숨김

## 서버

### DB (`migrations/2026-10-XX_devices_lcd_saver.sql`)
`devices` 에 확정값 컬럼 — lcd_text 관례(ACK ok 시 확정, 앱은 devices Realtime 으로 읽음):

| 컬럼 | 타입 | 뜻 |
|---|---|---|
| `lcd_power` | bool NOT NULL DEFAULT true | 수동 전원 |
| `lcd_quiet_start` / `lcd_quiet_end` | time (KST 벽시계) nullable | 둘 다 NULL = 미설정 |
| `lcd_saver` | jsonb nullable | `{enabled, image_sec, info_sec, show_offline_icon}` |
| `lcd_image_key` | text nullable | R2 키 접두 `lcd/{device_id}/{sha8}` — 있으면 이미지 있음. 앱은 `GET /lcd` 로 presigned 미리보기 URL 받음 |
| `lcd_image_set_at` | timestamptz nullable | 확정 시각 |
| `lcd_updated_at` | timestamptz | 위 값 중 마지막 확정 |

- R2 에는 `.rgb565`(기기용 40KB) + `.png`(앱 미리보기) 두 객체. 이전 사진 객체는 **다음 업로드 때** `_cleanup_orphans` 가 삭제 — ACK 경로(MQTT 스레드)에서는 R2 를 부르지 않는다(막히면 전 기기 telemetry 가 멈춤, 2026-11-03 리뷰). 정리 시 확정 키 + 아직 pending/sent 인 `lcd_image` 명령의 키는 남긴다(연속 업로드 404 방지)
- 적용 후 `MIGRATIONS_APPLIED.md` 기록. **코드보다 먼저** (ACK 커밋 UPDATE 가 400 나면 조용히 실패)

### 엔드포인트 (`backend/routers/lcd.py` 확장)

| 메서드 | 경로 | body | 비고 |
|---|---|---|---|
| POST | `/devices/{id}/lcd/image` | multipart `file` (PNG/JPG ≤ 5MB) | 응답 `{command, preview_url}`. EXIF 회전, cover-fit 크롭, RGB565 변환, R2 put 2개, 명령 발행 |
| GET | `/devices/{id}/lcd/image/bin` | — | **펌웨어 전용**, `Authorization: Bearer <기기 토큰>` (`firmware.download_bin` 의 검증 헬퍼 재사용). R2 에서 스트리밍. 명령 payload 의 `url` 이 이것 |
| DELETE | `/devices/{id}/lcd/image` | — | `lcd_image_clear` |
| PUT | `/devices/{id}/lcd/saver` | `{enabled, image_sec, info_sec, show_offline_icon?}` | 범위 검증 |
| PUT | `/devices/{id}/lcd/power` | `{on}` | |
| PUT | `/devices/{id}/lcd/quiet` | `{start:"23:00", end:"07:00"}` 또는 `null` | `parse_time_of_day` 재사용, 분 단위로 변환, `tz_offset_min=540` 고정(KST) |
| GET | `/devices/{id}/lcd` | — | 확정값 + `preview_url`(presigned, 있을 때) |

- 펌웨어용 bin 외엔 모두 JWT + `require_active_device`. TTL 은 `LCD_TTL_SEC=30` 재사용
- 미지원 기기(`capabilities.lcd_saver` 없음) → 400 `unsupported_firmware`
- `lcd_render.py` 에 `render_image_rgb565(bytes, w=160, h=128) -> (rgb565_bytes, preview_png)` 추가. 기존 `build_lcd_payload` 와 나란히
- 다운로드 URL 은 서버 경유(presigned R2 직접 X) — 기기 토큰으로 "내 기기의 이미지만" 검증하고, R2 anycast 블랙홀 이슈(2026-09-29)를 기기가 안 만나게

### ACK 확정 (`handlers._commit_lcd_text` 옆에 `_commit_lcd_state`)
- `lcd_power` ok → `lcd_power`
- `lcd_quiet`/`_clear` ok → `lcd_quiet_start/end`
- `lcd_saver` ok → `lcd_saver`
- `lcd_image` ok → `lcd_image_key`(payload 의 sha 로 역산)·`lcd_image_set_at=now`, `_clear` → NULL. R2 삭제는 안 함(위 참조)
- `lcd_image` 실패 중 erase 뒤 실패(`busy`/`no_token`/`bad_request`/`unknown_action` 외 전부) → 기기 사진 없음 → `lcd_image_key`·`set_at` NULL (2026-11-03 앱팀 요청, 펌웨어 `lcd_img_task` 순서: no_token → write_begin(erase) → download → finish, 실패 시 abort)
- 공통 `lcd_updated_at`. payload 에 큰 원문이 없으므로(url·sha 만) 서버 전용 키 불필요

### 웹 콘솔 (`web/index.html` LCD 패널 확장)
- 사진 파일 선택 → 업로드 → preview_url 표시 / 지우기
- 화면보호기 토글 + 초 입력 2개(사진 유지 / 온습도 유지)
- LCD 전원 토글 + 소등 시간 start/end + 해제

## 단계

### N-1 — LCD 전원 + 소등 시간 (펌웨어 먼저, 이미지 없이 완결됨)
- [x] **firmware** `lcd_power.c/h`(nano·supermini) / `iot_lcd_power.c`(hub): NVS `pwr`·`quiet` 로드/저장, `lcd_power_tick(now)` 판정
- [x] **firmware** `command_dispatch.c` `lcd_power` / `lcd_quiet` / `lcd_quiet_clear` 핸들러 (플래그+NVS 만)
- [x] **firmware** sensor_task: OFF 전환 시 `backlight(0)+fill BLACK`, 그리기 스킵, 복귀 시 전체 재묘사 **후** `backlight(100)`(검은 화면 번쩍임 방지). 시각 미동기 → ON. `lcd_power_init` 이 부팅 직후 NVS 읽어 창 안이면 즉시 0%
- [x] **firmware** capabilities `lcd_saver: true` (telemetry + 페어링 body)
- [x] **backend** 마이그레이션 + `PUT /lcd/power`, `PUT/DELETE /lcd/quiet`, `GET /lcd` + ACK 확정 (`_commit_lcd_state`)
- [x] **web** 전원 토글 + 소등 시간
- [ ] E2E: 23:00~07:00 설정 → 기기 시각 강제(또는 짧은 창 2분 뒤)로 꺼짐·켜짐 확인, 재부팅 후 창 안이면 부팅 직후부터 꺼짐, 핫스팟 콜드부트(시각 없음) → ON 유지

### N-2 — 컬러 사진 + 화면보호기
- [x] **firmware** `lcd_image_store.c/h`: Kconfig `APP_LCD_IMG_FLASH_ADDR/SIZE`(hub `APP_IOT_LCD_IMG_*`), 헤더 검증·erase·write·row read. 부팅 시 유효성 1회. 정적 assert 대신 **런타임 검사**(init 이 파티션 표 전체와 겹침·플래시 크기 확인, 실패 시 저장소 비활성 + 로그) — 파티션 표는 CSV 라 컴파일 타임에 못 본다
- [x] **firmware** `st7735_draw_rgb565_rows(x,y,w,h, read_cb, ctx)` — 한 줄씩 콜백으로 받아 push (3트리 동일 시그니처, nano 는 acquire-bus 스트림/supermini·hub 는 polling)
- [x] **firmware** `lcd_image` 핸들러: 16KB 전용 태스크 + `http_download()`(새 공용 파일) → 플래시 → 완료 후 지연 ack(`result="deferred"` 관례). 같은 sha 면 `adopt` 로 스킵. **`ota.c download_to_slot` 은 건드리지 않음** — 실기 검증된 OTA 경로를 리팩터링하면 재검증이 필요해서, 세 번째 사용처가 생길 때 합치기로
- [x] **firmware** `lcd_image_clear` / `lcd_saver` 핸들러 + NVS
- [x] **firmware** sensor_task 상태기계(IMAGE↔INFO 교대, reinit 사이클 재blit, repaint_static 은 INFO 만). 다운로드 중엔 단일 영역이라 **이전 이미지는 지워지고 INFO**(erase 가 먼저) — 실패해도 INFO 유지, 크래시 없음. 교대 기본 이미지 20초 / 온습도 10초
- [x] **backend** `render_image_rgb565` + `POST/DELETE /lcd/image`(multipart, `python-multipart` 추가) + `GET /lcd/image/bin?sha=`(기기 토큰) + `PUT /lcd/saver` + ACK 확정(R2 호출 없음) + 업로드 시 고아 정리(확정·진행 중 키 보존). 테스트 790 통과
- [x] **web** 업로드/미리보기/보호기 설정 (`api()` 가 FormData 면 Content-Type 생략)
- [x] E2E **실기 통과(2026-11-04, nano 1대, 서버 #40 배포본)**: 사진 업로드 → LCD 표시 확인, 소등 시간대 설정 동작, 화면보호기(대기모드) 교대 동작
- [ ] E2E 잔여: 재부팅 후 WiFi 없이도 사진 유지 → 같은 사진 재업로드 시 다운로드 스킵 로그 → 다운로드 중 전원 차단 후 재부팅 시 이전 이미지 또는 INFO(크래시 없음)
- [x] **docs** MQTT.md action 목록·DATABASE.md devices 컬럼·API.md §4.14 갱신, nano/hub `partitions.csv` 주석(supermini 는 내장 표라 Kconfig help 에), 앱 핸드오프 `docs/BACKEND_HANDOFF_REPLY_LCD_SAVER_2026-10-28.md`
- [x] **마이그레이션 적용** (2026-11-03) · **main 머지** PR #32
- [x] **OTA 릴리스 등록** (2026-11-03): nano `terra-fw 1.2.0-20261103`, hub `terra-hub-p4 0.4.0-20261103` — 둘 다 bin+elf R2. supermini 는 OTA 대상 아님(USB)
- [x] `lcd_saver.mode` `rotate`/`image` (사진만 모드, 2026-11-03 요청)
- [ ] **서버 재배포**(`uv sync` — python-multipart) → `.env API_PUBLIC_BASE_URL` 확인 → 기기 1대 OTA 선검증 → 전개. 앱 가이드 `docs/APP_GUIDE_LCD_SAVER_2026-11-03.md`

### N-3 (선택) — 슬라이드쇼
- 128KB = 3장. `slot` 0~2 로 저장, `lcd_saver.slots:[0,1,2]` 순환. 사용자 요구가 실제로 나오면

### N-4 — 소등 중 수동 조작 override + 실제 화면 상태 보고 (앱팀 요청서 2026-11-03)

요청: tera-ai-flutter `docs/handoffs/2026-11-03-lcd-quiet-manual-override-request.md`. 디자인은 "예약 중에도 켤 수 있고,
21시에 켰다가 다시 껐다면 8시에 다시 켜짐". 합의 규칙: **소등 시간대가 설정돼 있으면 사용자의 켜기/끄기는 다음 예약 경계
(start/end 중 먼저 오는 것)까지만 우선, 경계를 지나면 예약 규칙. 예약이 없으면 sticky(현행)**. 재부팅해도 유지.
"낮(예약 밖)에 끈 것을 아침 경계에서 켤지"는 디자이너 확인 중 — 기본은 표대로(20:00 경계에서 예약 규칙 → 08:00 켜짐).
허브는 2026-11-03 개발 중단 → **nano 만**.

펌웨어(nano, `lcd_power.c`):
- `effective_on`:
  ```
  in_quiet   = quiet 설정 && 시각 유효 && now ∈ [start, end)
  override   = NVS override_until 이 있고 now < override_until  (경계 지나면 NVS 에서 지움)
  effective  = override ? override_on : (quiet 설정 ? !in_quiet : lcd_power)
  ```
  `lcd_power` 수신 시: quiet 설정돼 있고 시각 유효 → `override_on = on`, `override_until = 다음 경계` 를 NVS `ovr` 에 저장.
  `pwr` 도 **항상** 같이 저장(소등 창을 지우면 마지막 수동 의도로 돌아가게). quiet 없음 또는 시각 미동기 → 현행(sticky).
  `lcd_quiet`/`lcd_quiet_clear` 는 override 를 지운다(새 예약 = 이전 수동 조작 무효). 경계를 지나면 tick 이 지운다(NVS 삭제 1회).
  시각 미동기면 override 판정도 보류 → fail-safe ON. (구현 2026-11-04, terra-iot-nano `feat/lcd-override`)
- telemetry 에 `lcd: {on: effective, override_until: ISO|null, image: sha8|null}` 매 건(3초). `image` 는 `lcd_image_store` 의
  present && NVS 메타 sha8, 아니면 null(다운로드 중 잠깐 null — 서버가 진행 중 명령을 보고 거른다).
- `capabilities.lcd_override: true` 추가. ack 는 변화 없음(`LCD_ON`/`LCD_OFF`).

서버(2026-11-04 선행, PR feat/lcd-screen-state):
- `migrations/2026-11-04_devices_lcd_screen_state.sql`: `lcd_screen_on BOOLEAN`, `lcd_override_until TIMESTAMPTZ` (NULL = 구 펌웨어).
- `handlers._sync_device_lcd_state`: telemetry `lcd` 블록 → 바뀔 때만 UPDATE(capabilities 와 같은 프로세스 캐시). `image: null` 이고
  서버 `lcd_image_key` 가 있고 pending/sent `lcd_image` 가 없으면 key·set_at NULL. 컬럼 누락은 `_missing_columns` 로 10분 보류.
- `GET /lcd` 응답에 두 필드. `PUT /lcd/power` 형식 불변.

- [x] **backend** 컬럼·동기화·응답·문서 (2026-11-04)
- [x] **마이그레이션 적용** `2026-11-04_devices_lcd_screen_state.sql` (2026-11-04, 코드 배포 전)
- [ ] **firmware(nano)** override 규칙·NVS·telemetry `lcd` 블록·`capabilities.lcd_override` — 코드 작성 완료(`feat/lcd-override`), 빌드·플래시·실기 전
- [ ] 디자이너 결론("낮에 끈 경우") 반영
- [ ] E2E: 21시 켬→08시 켜져 있음 / 21시 켬→22시 끔→08시 켜짐 / 23시 끔→변화 없음→08시 켜짐 / 재부팅 후 override 유지 / `lcd_screen_on` Realtime 반영 / erase_flash 뒤 `lcd_image_key` 비워짐

## 설계 메모

- **왜 교대(rotate)지 오버레이가 아닌가**: 사진 위에 온습도 글자를 겹치면 둘 다 망가짐. 전체 화면 교대가 단순·명확. 나중에 "사진 위 작은 온도" 가 필요하면 IMAGE 화면에 하단 띠를 옵션으로 추가
- **왜 1비트를 아예 뺐나**: 사진이 목적이면 흑백 디더는 결과가 실망스럽고, 그 경로(MQTT 버퍼 4096·base64·NVS blob 2.5KB)는 컬러에 하나도 재사용되지 않는다. 로고 같은 단색 용도는 컬러 포맷으로도 그냥 표현됨
- **왜 플래시 raw 영역이지 PSRAM 상주가 아닌가**: PSRAM 은 부팅 때마다 재다운로드 → WiFi 없으면 사진 없음, 서버·R2 트래픽도 매 부팅. 플래시면 한 번 받고 끝. 쓰기 마모는 사진 교체가 하루 몇 번 수준이라 무시(10만 회 보증)
- **왜 서버 경유 다운로드인가**: 기기 토큰으로 소유 검증 + R2 anycast 블랙홀(2026-09-29, 펌웨어 DNS 폴백 없음)을 기기가 직접 안 만남. 서버 메모리 부담은 40KB 스트리밍이라 무시
- **OFF 중에도 resync/reinit 유지**: 패널 설정 깨짐 복구(2026-10-18 이슈)는 계속 돌아야 켤 때 멀쩡함. GRAM 은 검정이라 백라이트만 꺼도 되지만 fill BLACK 한 번 해서 PWM 누설광에 잔상 안 보이게
- **전환 번쩍임**: 소등 창 안에서 재부팅하면 `st7735_init` 이 100% 로 켜고 첫 사이클에 0% → 2~3초 켜짐. `lcd_bring_up` 직후 NVS 읽어 즉시 0 — N-1 에서 같이 처리
- **서버 KST 고정**: scheduling.py 와 동일 전제. 해외 사용자 생기면 `devices.tz` 컬럼 추가하고 `tz_offset_min` 만 바꿔 보내면 펌웨어 수정 없음
- **A안(서버 schedules 로 lcd_on/off 발행) 기각 이유**: 오프라인·재부팅 시 유실, 부팅 기본값이 ON 이라 "새벽에 켜짐" 재현. 즉시 명령 `lcd_power` 는 수동 토글용으로 유지

## 학습 노트 (이 스테이지에서 새로 쓰는 개념)

- **파티션 밖 raw 플래시**: ESP-IDF 는 파티션 표에 없는 주소도 `esp_flash_erase_region/write/read` 로 다룰 수 있다. 보호(`DANGEROUS_WRITE_ABORTS`)는 부트로더·파티션표·실행 중 앱만. 대신 "그 주소에 뭐가 있는지" 를 아는 건 우리뿐이라 헤더 magic+crc 로 자기검증하고 docs 에 못 박는다
- **RGB565 바이트 순서**: ST7735 는 픽셀당 2바이트를 MSB 먼저 받는다. 서버가 처음부터 big-endian 으로 써 주면 펌웨어는 플래시 → SPI 로 바이트를 그대로 흘려보내면 된다. 서버 쪽: `((r&0xF8)<<8)|((g&0xFC)<<3)|(b>>3)` → `struct.pack(">H")`
- **cover-fit**: 원본 비율을 지키며 160×128 을 꽉 채우고 넘치는 쪽을 잘라냄(letterbox 반대). Pillow `ImageOps.fit`
- **벽시계 분(minute-of-day) 비교**: 자정 넘는 구간은 `start > end` 일 때 `m >= start || m < end`. 서버 `compute_next_run` 도 같은 함정을 KST 환산으로 피함
