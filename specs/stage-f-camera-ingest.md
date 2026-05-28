# Stage F — ESP32-P4 카메라 워커 + 모션 영상 인제스트 (⏸️ 보류)

## 개요

ESP32-P4 + MIPI-CSI 카메라가 자체 모션 감지 → 10초 H.264 mp4 캡처 (내장 HW 인코더) → terra-api 로부터 R2 presigned URL 받아 PUT 업로드 → 메타를 Supabase `motion_clips` 에 등록.

terra-server 는 영상 분석을 **하지 않음**. 메타 + presigned URL 발급만.

> 대안 워커(Raspberry Pi)도 cloud_integration.md 에 명시. 인터페이스 동일하므로 사육장별 선택 가능. 본 스펙은 메인 구현 대상인 **ESP32-P4** 기준으로 작성.

## 하드웨어 사양

| 항목 | 사양 |
|------|------|
| 보드 | **DFRobot FireBeetle 2 ESP32-P4** 또는 Waveshare ESP32-P4-Nano |
| Wi-Fi | ESP32-C6 코프로세서 (WiFi 6 ax, ESP-Hosted 통신) |
| BLE | BLE 5.0 (ESP32-C6 또는 ESP32-P4 자체) |
| 카메라 | MIPI-CSI 2-lane 모듈 (SC2336 권장, Pi Camera 호환 OK) |
| 야간 촬영 | NoIR 모듈 + 940nm IR LED (필요 시) |
| 저장 | 내장 Flash 16MB + microSD 슬롯 (옵션, 폴백용) |
| PSRAM | 32MB (영상 10초 임시 보관 가능) |
| H.264 인코더 | ESP32-P4 내장 HW (`esp_h264` 컴포넌트) |
| 영상 스펙 | HD 720p @ 24fps, 10초 → ~500KB~1.5MB / 클립 |

## 페어링 방식

**BLE 5.0 + JWT** (ESP32-S3 펌웨어와 동일 패턴, NimBLE 재사용)

```
ESP32-P4 → BLE 광고 (이름: "Terra-Cam-XXXX")
앱 → BLE write: { ssid, password, jwt, enclosure_id?, name, model: "esp32-p4" }
ESP32-P4 → WiFi 연결 → HTTPS POST /cameras/pair (jwt 헤더)
terra-api → JWT 검증 → DB INSERT → 응답 { camera_id, camera_token }
ESP32-P4 → NVS 저장 → MQTT connect + 워커 시작
```

→ RPi 의 QR 페어링과 달리 BLE 단일 흐름 (디바이스/카메라 일관성).

## In (선행 조건)

- Stage A, B 완료 (MQTT 브리지 + ESP32-S3 페어링)
- ESP32-P4 보드 + MIPI 카메라 확보
- Cloudflare R2 계정 + 버킷 + API 토큰
- `migrations/2026-05-26_camera_schema.sql` Supabase 에 적용
- terra-server `.env` 에 R2 변수 입력
- ESP-IDF v5.3+ 환경

## Out

- 영상 분석 / 행동 분류 — 본 프로젝트 범위 외
- 라이브 스트리밍 — **Stage G** 에서 별도 진행
- 음성 녹음 — 단순화 위해 제외
- 다중 카메라 한 보드 — 1 보드 = 1 카메라

## 완료 조건

### F1 — 백엔드 (terra-server)

- [ ] `backend/r2_client.py` 신규
  - [ ] boto3 R2 클라이언트 (S3 호환)
  - [ ] `generate_presigned_put_url(key, expires_in=300)`
  - [ ] `generate_presigned_get_url(key, expires_in=3600)`
- [ ] `backend/routers/enclosures.py` 신규 (CRUD)
- [ ] `backend/routers/cameras.py` 신규
  - [ ] `POST /cameras/pair` (JWT) → camera_id + camera_token 발급
  - [ ] CRUD (JWT)
- [ ] `backend/routers/clips.py` 신규
  - [ ] `POST /cameras/{id}/clips/upload-url` (Bearer camera_token) → R2 presigned PUT
  - [ ] `POST /cameras/{id}/clips` (Bearer camera_token) → motion_clips INSERT
  - [ ] `GET /clips/{clip_id}/url` (JWT) → presigned GET URL
  - [ ] `GET /enclosures/{id}/clips` (JWT) → 클립 목록 (cursor pagination)
- [ ] 카메라 토큰 인증 미들웨어 (`backend/auth_camera.py`)
- [ ] `backend/mqtt/bridge.py` motion_event 핸들러 추가
- [ ] tests/test_cameras_api.py, test_clips_api.py, test_r2_client.py

### F2 — 영상 수명 관리

- [ ] Cloudflare R2 lifecycle rule 추가 (`clips/` prefix, 30일 후 delete)
- [ ] `scripts/cleanup_orphan_clips.py` 작성 (cron 매일 1회)

### F3 — ESP32-P4 카메라 워커 펌웨어 (별도 레포)

- [ ] 신규 레포 부트스트랩: `~/project/esp32/terra-cam-p4/`
  - [ ] ESP-IDF v5.3+ 프로젝트 구조
  - [ ] CMakeLists.txt + sdkconfig.defaults
  - [ ] components: esp_video, esp_h264, esp_mp4, esp_http_client, esp_tls, nvs_flash, nimble
- [ ] `main/main.c` — 진입점 + 태스크 생성
- [ ] `main/camera.c/h` — esp_video MIPI-CSI 초기화 + 프레임 수신
- [ ] `main/motion.c/h` — 프레임 차분 모션 감지 (ESP32-S3 NimBLE_Connection 의 motion 알고리즘 차용)
- [ ] `main/encoder.c/h` — esp_h264 + esp_mp4 컨테이너
- [ ] `main/uploader.c/h` — esp_http_client → terra-api + R2 PUT
- [ ] `main/mqtt_client.c/h` — esp-mqtt + motion_event publish
- [ ] `main/pairing.c/h` — NimBLE 광고 + JWT 수신 (ESP32-S3 코드 재사용)
- [ ] `main/cloud_client.c/h` — terra-api Bearer 토큰 인증
- [ ] systemd 같은 부팅 자동 가동은 펌웨어 자체 (FreeRTOS)

## 설계 메모

### 왜 ESP32-P4?

| 비교 | RPi Zero 2 W | **ESP32-P4** |
|------|-------------|-------------|
| OS | Linux | **FreeRTOS** (본 펌웨어와 동일) |
| 전력 | 2.5W | **0.5~1W** (1/3~1/5) |
| 부팅 시간 | 30초 | **<1초** |
| 가격 (보드+카메라) | ~8~10만원 | **~6~8만원** |
| 펌웨어 자산 재사용 | 0% | **~50%** (NimBLE/esp-mqtt/esp_http_client 등) |
| 양산 적합성 | 낮음 | **높음** |

단점 (감수):
- ESP-IDF + FreeRTOS 학습 부담 (이미 ESP32-S3 작성 경험 있으니 절감)
- `esp_h264`, `esp_video` 컴포넌트 신생 (2024년 출시, 자료 점차 늘어남)
- WiFi 가 ESP32-C6 코프로세서 통한 ESP-Hosted (설정 약간 복잡)

### 모션 감지 알고리즘

ESP32-S3 NimBLE_Connection 의 motion 패턴 그대로 C 포팅 (이미 있다면 재사용):

```c
// motion_detect_task
camera_fb_t *fb = esp_camera_fb_get();  // grayscale 320x240
// 이전 프레임과 픽셀 차분
int diff_count = 0;
for (int i = 0; i < W*H; i++) {
    if (abs(fb->buf[i] - prev_buf[i]) > PIXEL_THRESHOLD) diff_count++;
}
if (diff_count > W*H * MOTION_RATIO) {
    // motion!
}
```

PSRAM 32MB 활용으로 이전 프레임 + 현재 프레임 동시 보유 여유.

### 영상 캡처 + 인코딩

```c
// capture_task (10초)
esp_h264_enc_handle_t encoder = esp_h264_enc_open(...);
esp_mp4_writer_t mp4 = esp_mp4_writer_create(buffer, max_size);

for (int frame = 0; frame < 24 * 10; frame++) {
    camera_fb_t *fb = esp_camera_fb_get();  // I420 720p
    esp_h264_enc_frame_t encoded = esp_h264_enc_process(encoder, fb);
    esp_mp4_writer_add_frame(mp4, encoded);
}
esp_mp4_writer_finalize(mp4);  // PSRAM 버퍼에 완성된 mp4
```

### R2 업로드

```c
// upload_task
// 1) terra-api 에 presigned URL 요청
esp_http_client_handle_t client = esp_http_client_init(&config);
esp_http_client_set_url(client, "https://api.example.com/cameras/.../clips/upload-url");
esp_http_client_set_header(client, "Authorization", "Bearer <camera_token>");
esp_http_client_perform(client);
// 응답 JSON 파싱 → presigned_url 추출

// 2) R2 에 PUT (chunked transfer)
esp_http_client_set_url(client, presigned_url);
esp_http_client_set_method(client, HTTP_METHOD_PUT);
esp_http_client_open(client, mp4_size);
esp_http_client_write(client, mp4_buffer, mp4_size);  // 또는 chunked
esp_http_client_fetch_headers(client);

// 3) terra-api 에 메타 등록
// POST /cameras/.../clips
```

### R2 객체 키 규칙

```
clips/{YYYY}/{MM}/{DD}/{camera_id}/{clip_id}.mp4
clips/{YYYY}/{MM}/{DD}/{camera_id}/{clip_id}_thumb.jpg
```

### 메모리 관리 (32MB PSRAM)

- 720p H.264 10초 ≈ 1MB → PSRAM 충분
- I420 raw frame (720p) = ~1.4MB → 모션 감지용 prev+current 2장 = ~3MB
- esp_h264 encoder context: ~수백KB
- 합계 ~5~10MB 사용, 여유 충분

### 야간 촬영

cloud_integration.md 0.6 절 참조. ESP32-P4 + Pi NoIR Camera (IMX219) + 940nm IR LED 권장.

```c
// motion 감지 시
gpio_set_level(IR_LED_GPIO, 1);  // IR LED ON
vTaskDelay(pdMS_TO_TICKS(100));  // 노출 안정
// 캡처 시작
...
gpio_set_level(IR_LED_GPIO, 0);  // IR LED OFF
```

## 학습 노트

### ESP32-P4 + ESP32-C6 (ESP-Hosted)
ESP32-P4 자체에 WiFi 가 없어 ESP32-C6 모듈을 SDIO/SPI 로 연결, ESP-Hosted 펌웨어로 통신.
DFRobot FireBeetle 2 / Waveshare Nano 보드는 이미 통합되어 있어 사용자가 직접 연결할 필요 없음.

### esp_h264 출력 형식
NAL unit 시퀀스. esp_mp4 또는 자체 컨테이너 작성 라이브러리로 mp4 frag 만들기.
또는 ffmpeg 컴파일된 esp32 빌드 사용 (raw stream → mp4 컨테이너).

### Cloudflare R2 비용 (디바이스 100대 + 매일 1000 클립)
- 1000 클립 × 1MB = 1GB/day, 30일 보관 = 30GB → 무료 10GB 초과분 $0.30/월
- Class A (PUT): 30K/월 → 무료 1M 이내
- Class B (GET): 150K/월 → 무료 10M 이내
- **Egress: 무제한 무료**

## 참고

- petcam-lab `backend/r2_uploader.py` (백엔드 R2 boto3 패턴)
- petcam-lab `backend/routers/clips.py` (clips 라우터 패턴)
- petcam-lab `backend/motion.py` (모션 감지 알고리즘 — C 포팅)
- ESP-IDF examples: `examples/peripherals/camera/`
- esp-video docs: https://github.com/espressif/esp-video-components
- esp-h264 docs: https://github.com/espressif/esp-adf-libs (포함)
- [docs/MQTT.md](../docs/MQTT.md) — motion_event 페이로드
- [stage-g-live-streaming.md](stage-g-live-streaming.md) — 라이브 스트리밍 (별도 스테이지)
