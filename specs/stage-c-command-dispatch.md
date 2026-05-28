# Stage C — 명령 디스패치 (Supabase Realtime → MQTT publish) (⏸️ 보류)

## 개요

앱이 Supabase `commands` 테이블에 `status='pending'` INSERT → 브리지가 Realtime subscribe 로 감지 → MQTT publish (`retain=false`, QoS 1) → 디바이스가 실행 후 ack → 브리지가 `commands` UPDATE.

## In

- Stage A, B 완료
- 펌웨어 측 command 핸들러 + ack publish 구현

## Out

- 명령 큐잉 / 우선순위
- 다중 디바이스 동시 명령 (각각 독립)
- 명령 스케줄러 (cron-style 정기 명령)

## 완료 조건

- [ ] `backend/mqtt/command_dispatcher.py` 신규
  - [ ] Supabase Realtime client (supabase-py 2.x async)
  - [ ] `commands` INSERT 이벤트 → bridge.publish_command()
  - [ ] publish 성공 시 status='sent', issued_at + TTL 페이로드 구성
- [ ] `backend/mqtt/bridge.py` `_handle_ack` 실제 구현
  - [ ] msg_id 로 commands 찾기 → status='acked', result, acked_at UPDATE
- [ ] msg_id 중복 / TTL 만료 정책 펌웨어 측 검증
- [ ] 통합 테스트: 앱 INSERT → MQTT publish → ack → status='acked'

## 설계 메모

### Realtime 연결 vs polling
Supabase Realtime 은 WebSocket 기반. 끊김 자동 재연결. polling (1초) 대비:
- 지연: ms vs 평균 500ms
- 부하: 0 (push) vs 매초 SELECT
- 신뢰성: 끊김 감지 + 재연결 vs polling 은 무조건 동작

→ Realtime 채택. 폴백으로 30초 폴링 추가 (Realtime 끊김 시).

### retain=false 강제
페어링 후 ESP32 첫 MQTT 연결 시 retain 메시지가 자동 전달되면 위험 (예: 과거 heater_toggle). 코드 레벨 + Mosquitto config 양쪽 강제.

### TTL 정책
- 일반 명령: 30초
- 안전 액추에이터 (heater): 10초
- token_rotate: 60초 (네트워크 지연 허용)

펌웨어가 TTL 검증 → `rejected_ttl_expired` ack. 브리지는 commands.status='expired' 처리.

## 학습 노트

### supabase-py Realtime
v2.x 부터 async 지원. `client.realtime.channel("commands").on(...)` 패턴.
sync 클라이언트는 Realtime 없음 → 별도 asyncio 태스크.

## 참고

- petcam-lab `backend/vlm/worker.py` (폴링 + INSERT 패턴)
- [docs/MQTT.md](../docs/MQTT.md) — command/ack 페이로드
