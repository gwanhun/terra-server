# Stage I — LCD 커스텀 텍스트 (한글, 서버 렌더 비트맵)

**목표**: 사용자가 앱에서 입력한 텍스트(한글 포함)를 디바이스 LCD 상단에 표시. 재부팅 후에도 유지.

## 확정된 결정 (사용자 합의)
- **한글 지원 필요** → 펌웨어 폰트 임베드 대신 **서버가 텍스트를 비트맵으로 렌더**해서 전송 (A안)
- **표시 위치**: LCD **상단 밴드** = 사용자 커스텀 텍스트. 미설정/clear 시 기본값 `"TERRA IOT"`(제품명)
- **지속성**: **NVS 저장** — 재부팅 후에도 유지
- 센서 표시(온습도/펌프/팬/LED)는 그대로 유지, y=34 아래로 이동

## 왜 서버 렌더 비트맵? (A안)
5×7 ASCII 폰트로는 한글 불가. 펌웨어에 한글 폰트 임베드(상용 2350자 16×16 ≈ 75KB + UTF-8 디코드 +
조합 렌더)는 무겁다. 서버(Pillow + 한글 TTF)가 1비트 비트맵으로 렌더 → 펌웨어는 blit 만.
→ 모든 유니코드/이모지, 예쁜 폰트, 펌웨어 폰트 0, 매핑 로직 0.

## 아키텍처
```
앱: "밥 6시" 입력
 → POST /devices/{id}/lcd
 → 서버: Pillow 로 128×32 1비트 비트맵 렌더 (한글 TTF) → RLE/base64 패킹
 → commands INSERT { action:"lcd_bitmap", payload:{ w,h,data } }
 → dispatcher publish → 펌웨어: base64 디코드 → 상단 밴드 blit + NVS 저장
 → 재부팅: NVS 에서 복원 재표시
```

## 화면 레이아웃 (128×160)
```
y=0    ┌──────────────┐
       │  밥 6시 🍚    │  사용자 커스텀 밴드 (~32px). 미설정 → "TERRA IOT"
y=34   ├──────────────┤
       │ 27.3°C  65%  │  센서 (기존, 아래로 이동)
       │ PUMP FAN LED │
y=140  ├──────────────┤
       │ WIFI SVR BLE │  연결 상태 행 (연결=초록, 끊김=회색)
y=160  └──────────────┘
```

## 명령 계약
```json
{ "action": "lcd_bitmap", "payload": {
    "w": 128, "h": 32,
    "enc": "rle",              // "raw" | "rle" (한글 비트맵은 sparse → RLE 권장)
    "data": "<base64>"
}}
```
- `lcd_clear` → 밴드를 기본값("TERRA IOT")으로 복귀 + NVS 클리어
- 비트맵은 밴드 크기 고정(128×32). 서버가 항상 그 크기로 렌더.

## 단계

> **진행**: 밴드 높이는 **24px**(y=0~24)로 확정 — 기존 타이틀바 자리 재사용, 센서(y=30~) 안 겹쳐 레이아웃 수정 불필요. 128×24 1비트=384B.

### I-1 — 파이프라인 + 영문 ✅ (코드 완료, 빌드 필요)
- [x] **firmware** `st7735_draw_bitmap(x,y,w,h, buf, fg,bg)` — 1비트 → 색 blit (fill_rect 청크 스트리밍 재사용)
- [x] **firmware** `command_dispatch.c` `lcd_bitmap` 핸들러 — mbedtls base64 디코드 → `lcd_band_set_bitmap` → ack
- [x] **firmware** MQTT RX 버퍼 2048 상향 (base64 ~700B 한 이벤트 수용)
- [x] **firmware** 상단 밴드 = 커스텀(미설정 시 "TERRA IOT"). 24px라 센서 offset 조정 불필요
- [x] **backend** `uv add pillow` + `POST /devices/{id}/lcd` — Pillow 렌더 → 1비트 패킹 → 큐잉
- [x] **web** 콘솔에 LCD 패널(디바이스 선택 + 텍스트 + 표시/초기화)
- [ ] E2E: 앱 텍스트 → LCD 상단 표시 (실물 빌드/플래시 후)

### I-2 — 한글 + NVS + clear ✅ (코드 완료)
- [x] **backend** 한글 TTF 탐색(`LCD_FONT_PATH` env + macOS/Ubuntu 경로), 없으면 기본폰트(영문)
- [x] **backend** 길이 상한(64자)·빈 문자열 → clear 취급
- [x] **firmware** `lcd_band` NVS 저장/복원 — 부팅 시 마지막 비트맵 재표시
- [x] **firmware** `lcd_clear` → 기본값 복귀 + NVS 클리어 (`POST /lcd/clear` 또는 빈 텍스트)
- [ ] (선택) `lcd_bitmap` 예약 연동 — 후속
- [ ] E2E: 한글 표시 + 재부팅 유지 확인 (실물)
- [ ] **배포**: 서버에 한글 TTF 설치(`apt install fonts-nanum`) + `LCD_FONT_PATH` 지정

### I-3 — 연결 상태 표시 (펌웨어 단독, 서버 무관 — 먼저 해도 됨)
> 적용 대상: `terra-iot-nano` + `terra-iot-nano-relay` 둘 다 (동일 패치)
- [x] **firmware** `wifi.h/c` 에 `wifi_is_connected(void)` getter 추가 — 이벤트 핸들러가
      `IP_EVENT_STA_GOT_IP` / `WIFI_EVENT_STA_DISCONNECTED` 에서 static bool 갱신
- [x] **firmware** `gap.h/c` 에 `gap_is_ble_connected(void)` getter 추가 — 기존 `s_ble_connected` 노출
- [x] **firmware** `main.c` 표시 루프(3초 주기)에 하단 상태 행 렌더 (y=140, size 1 = 6×8):
      - `WIFI` — `wifi_is_connected()`
      - `SVR` — `mqtt_app_is_connected()` (WiFi 는 되는데 서버가 죽었는지 구분용)
      - `BLE` — `gap_is_ble_connected()` (앱이 GATT 연결 중)
      - 연결 = GREEN, 끊김 = GRAY (PUMP/FAN/LED ON/OFF 색 규칙과 동일)
- [ ] 실기기 확인: WiFi 끊기(공유기 off) → WIFI/SVR 회색, BLE 프로비저닝 연결 시 BLE 초록

## 설계 메모
- **payload 크기**: 128×32 1비트 = 512B raw → base64 ~700자. RLE 로 한글 sparse 비트맵은 크게 줄어듦.
  MQTT/commands JSONB/dispatcher 모두 수용 가능. 펌웨어 RX 버퍼만 상향.
- **NVS**: 비트맵(최대 512B) + 유효 플래그 저장. 부팅 시 없으면 "TERRA IOT" 기본 렌더(펌웨어 ASCII 로 그려도 됨).
- **B안(폰트 임베드) 비교**: payload 는 작지만 폰트 75KB + UTF-8/조합 렌더 + 2350자 한계 + 라이선스.
  한글 품질·확장성·펌웨어 단순성에서 A안 우위라 A 채택.
- **서버 폰트 의존**: Pillow + TTF 파일 1개 추가. headless 렌더라 런타임 부담 작음.

## 참고
- 펌웨어: `terra-iot-nano/main/src/display/st7735.c` (5×7 폰트, fill_rect/draw_text), `main.c` 표시 루프
- 파이프라인: [Stage H](stage-h-timer-mist-push.md) 의 mist 와 동일 흐름 (commands 재사용)
