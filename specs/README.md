# Specs (스테이지별 스펙)

> 각 스펙은 `In / Out / 완료 조건 / 설계 메모 / 학습 노트` 구조.
> 상태: 🚧 진행 중 / ✅ 완료 / ⏸️ 보류 / 🗑️ 폐기

## 스테이지 목록

### IoT (ESP32-S3 센서/제어)

| 상태 | 스펙 | 한 줄 |
|------|-----|-------|
| 🚧 | [stage-a-mqtt-bridge.md](stage-a-mqtt-bridge.md) | MQTT 브리지 telemetry → Supabase 저장 |
| ⏸️ | [stage-b-device-pairing.md](stage-b-device-pairing.md) | BLE + JWT 페어링 + MQTT 토큰 발급 |
| ⏸️ | [stage-c-command-dispatch.md](stage-c-command-dispatch.md) | Supabase Realtime → MQTT publish |
| ⏸️ | [stage-d-alerts.md](stage-d-alerts.md) | alerts INSERT + FCM 푸시 |
| ⏸️ | [stage-e-timeseries-downsample.md](stage-e-timeseries-downsample.md) | telemetry → telemetry_1m, pg_cron |

### 영상 (ESP32-P4 카메라)

| 상태 | 스펙 | 한 줄 |
|------|-----|-------|
| ⏸️ | [stage-f-camera-ingest.md](stage-f-camera-ingest.md) | ESP32-P4 카메라 워커 → H.264 mp4 → R2 + motion_clips 메타 |
| ⏸️ | [stage-g-live-streaming.md](stage-g-live-streaming.md) | 라이브 스트리밍 (G1 JPEG → G2 WebRTC 단계적) |

## 다음 세션 시작점

[next-session.md](next-session.md) — 새 Claude 가 첫 읽을 문서. 매 세션 끝에 갱신.
