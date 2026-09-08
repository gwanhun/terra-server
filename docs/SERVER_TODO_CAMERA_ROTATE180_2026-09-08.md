# 서버 작업 명세 — 카메라 180° 회전 설정 (2026-09-08)

> **연관**: 앱 `handoff-camera-rotate180-2026-09-08.md` / 펌웨어 회신 `firebeetle2-p4-yr030/docs/HANDOFF_REPLY_CAMERA_ROTATE180_2026-09-08.md`
> **성격**: terra-server 내부 작업 명세(앱에 공개하는 계약은 회신 문서 §4~§6 이 정본). 이 문서는 구현자용.
> **범위**: 마이그레이션 1건, `cameras` 라우터, MQTT 텔레메트리 핸들러, MQTT.md 갱신, 테스트.

---

## 0. 설계 요약

- **선언적 설정**: 진실은 `cameras.rotate_180`. 앱은 PATCH 만 하고, 카메라는 서버 값에 수렴한다.
- **두 갈래 전달**: ① PATCH 직후 즉시 MQTT 명령 발행(온라인 카메라 즉시 반영) ② 카메라 텔레메트리에 실려 오는 현재값과 DB 가 다르면 브리지가 명령 재발행(오프라인/유실/재부팅 보정).
- **capabilities**: 카메라가 텔레메트리로 보고 → `cameras.capabilities` 에 저장. 앱은 이 값으로 토글 노출 여부 결정.
- 카메라 대상 `commands` 테이블은 만들지 않는다(WebRTC 시그널링과 동일 원칙 — `handlers.py` 주석 참고). ack 는 last_seen 갱신만.

## 1. 마이그레이션

`migrations/2026-09-08_cameras_rotate_capabilities.sql`

```sql
-- 2026-09-08: 카메라 180° 회전 설정 + 카메라 capabilities (앱 핸드오프 rotate180 R4/R5)
-- rotate_180  : 선언적 설정. 앱 PATCH 로 변경, 펌웨어가 이 값에 수렴.
-- capabilities: 펌웨어가 MQTT 텔레메트리로 보고. NULL = 구 펌웨어(아직 보고 안 함).
--               예) {"rotate_180": true}
ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS rotate_180 BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE public.cameras
    ADD COLUMN IF NOT EXISTS capabilities JSONB;

COMMENT ON COLUMN public.cameras.rotate_180 IS '영상 180° 회전(설치 방향 보정). 앱 설정값, 펌웨어가 동기화';
COMMENT ON COLUMN public.cameras.capabilities IS '펌웨어 보고 능력 플래그 JSONB. 예 {"rotate_180":true}. NULL=미보고(구 펌웨어)';
```

- 백필 없음. 구 펌웨어는 `capabilities` NULL 로 두어 앱이 토글을 숨기게 한다(devices 때처럼 relay 기본으로 채우면 안 됨).
- 적용 후 `MIGRATIONS_APPLIED.md` 에 행 추가, `docs/DATABASE.md` cameras 절에 두 컬럼 추가.

## 2. `backend/routers/cameras.py`

### 2-1. 모델

```python
class CameraUpdate(BaseModel):
    ...
    rotate_180: bool | None = Field(None, description="영상 180° 회전(설치 방향 보정)")

class CameraOut(BaseModel):
    ...
    rotate_180: bool = False
    capabilities: dict[str, Any] | None = Field(
        None, description='펌웨어 보고 능력 플래그. 예 {"rotate_180": true}. null=구 펌웨어')
```

- `CameraPairRequest` 는 건드리지 않는다. capabilities 는 페어링이 아니라 텔레메트리로 들어온다(기존 카메라도 펌웨어 업데이트만으로 갱신되게).

### 2-2. `update_camera` — PATCH 시 명령 발행

```python
res = sb.table("cameras").update(updates).eq("id", camera_uuid).eq("owner_id", user_id).execute()
if not res.data:
    raise HTTPException(404, ...)
cam = res.data[0]

if "rotate_180" in updates:
    _publish_rotation(cam["camera_id"], bool(updates["rotate_180"]))   # best-effort

return CameraOut.model_validate(cam)
```

```python
def _publish_rotation(camera_id_text: str, rotate: bool) -> None:
    """best-effort. 실패해도 DB 는 이미 갱신됐고 텔레메트리 동기화(§3)가 수렴시키므로 502 안 던짐."""
    payload = {
        "msg_id": str(uuid4()),
        "issued_at": int(time.time()),
        "ttl_sec": 60,
        "action": "set_rotation",
        "rotate_180": rotate,
    }
    try:
        MqttWebRTCSignaling().publish(camera_id_text, payload)
    except Exception:
        logger.warning("set_rotation publish 실패 camera=%s (텔레메트리 동기화로 수렴)", camera_id_text, exc_info=True)
```

- 발행 경로는 `webrtc.py` 가 쓰는 `MqttWebRTCSignaling.publish()` 재사용(API 프로세스는 브리지의 long-lived 클라이언트를 갖고 있지 않음). 이름이 webrtc 전용처럼 보이지만 실체는 "카메라 command 토픽 one-shot publish"다. 리네임은 이번 범위 밖.
- `MqttWebRTCSignaling.__init__` 이 env 누락 시 예외를 던지므로 반드시 try 로 감싼다(테스트 환경에 MQTT 자격증명 없음).
- `ttl_sec` 60: 펌웨어가 SNTP 동기 시 `now - issued_at > ttl` 이면 거부. 즉시성 명령이라 짧게.

### 2-3. 명령 페이로드 계약 (펌웨어 회신 문서와 일치해야 함)

```jsonc
// esp32/{camera_id}/command  QoS1 retain=false
{ "msg_id": "uuid", "issued_at": 1757300000, "ttl_sec": 60,
  "action": "set_rotation", "rotate_180": true }

// esp32/{camera_id}/ack
{ "msg_id": "uuid", "status": "ok" }                 // 적용 + NVS 저장 완료
{ "msg_id": "uuid", "status": "rejected_unknown_action" }  // 구 펌웨어
```

## 3. `backend/mqtt/handlers.py` — 텔레메트리 동기화

펌웨어는 텔레메트리(`esp32/{camera_id}/telemetry`, 15초 주기 + 연결 직후 1회)에 아래 필드를 추가한다:

```jsonc
{ "ts": 1757300000, "uptime_sec": 123, "free_heap": 456789,
  "rotate_180": false,                     // 현재 NVS 값 (신규)
  "capabilities": { "rotate_180": true } } // 신규. 연결 직후 1회만 실릴 수도 있음 → 없으면 건너뜀
```

`handle_telemetry` 의 camera 분기(현재 last_seen/is_online 만 갱신)를 확장:

```python
if entity_type == "camera":
    update = {"last_seen_at": _now_iso(), "is_online": True}
    caps = payload.get("capabilities")
    if isinstance(caps, dict):
        update["capabilities"] = caps
    # rotate_180 동기화: DB 값이 진실. 카메라 보고값과 다르면 명령 재발행.
    reported = payload.get("rotate_180")
    if isinstance(reported, bool):
        desired = _cached_camera_rotate(entity_uuid)   # 아래 캐시
        if desired is not None and desired != reported:
            bridge.publish_command(device_id_text, _rotation_command(desired))
    sb.table("cameras").update(update).eq("id", entity_uuid).execute()
```

- `desired` 조회는 15초마다 SELECT 하지 않도록 **짧은 TTL 캐시**(예 30초)로 둔다. `_cached_camera_uuid` 와 같은 `functools.lru_cache`+dict 패턴이면 되지만, PATCH 로 값이 바뀌면 API 프로세스와 브리지 프로세스가 달라 캐시 무효화가 안 된다 → TTL 캐시로 간다. 최악의 경우 PATCH 직후 명령이 유실됐을 때 최대 TTL 만큼 늦게 수렴.
- `capabilities` 는 매번 UPDATE 해도 15초에 한 번이라 부담 없음. 값이 같으면 스킵하는 최적화는 선택.
- 재발행 무한루프 방지: 카메라가 구 펌웨어라 `rotate_180` 키를 아예 안 보내면 `reported` 가 None → 발행 안 함. 신 펌웨어인데 적용 실패로 계속 false 를 보고하면 15초마다 재발행되는데, 이건 의도된 수렴 동작이고 QoS1 한 건이라 허용. 로그는 `warning` 으로 남겨 육안 확인.
- `_rotation_command()` 는 §2-2 `_publish_rotation` 페이로드 생성부와 **공유 헬퍼로 뽑는다**(`backend/mqtt/camera_commands.py` 신설 또는 `handlers.py` 에 두고 라우터가 import). action 이름·필드가 두 군데서 어긋나지 않게.
- `handle_ack` camera 분기는 변경 없음(last_seen 갱신만). `rejected_unknown_action` 이 오면 구 펌웨어라는 뜻이며 `capabilities` 가 NULL 일 것이므로 별도 처리 불필요.

## 4. 문서 갱신

- `docs/MQTT.md`
  - 카메라 `action` 목록에 `set_rotation` (추가 필드 `rotate_180: bool`) 추가.
  - 카메라 telemetry 페이로드 절에 `rotate_180`, `capabilities` 추가. 변경 이력 표에 행 추가.
- `docs/API.md` / `docs/DATABASE.md`: `cameras` 두 컬럼, `PATCH /cameras/{id}` body `rotate_180`, `CameraOut` 필드.
- `docs/FIRMWARE_INTEGRATION.md`: 텔레메트리 필드 + `set_rotation` 처리 규칙 한 절 추가.
- 앱 공개 계약은 펌웨어 회신 문서 §4~§6 이 정본. 서버 배포 후 그 문서 §8 에 배포 완료 표기.

## 5. 테스트

`tests/test_cameras_api.py`
- PATCH `{rotate_180: true}` → 200, 응답 `rotate_180 == true`, `MqttWebRTCSignaling.publish` 가 `action=set_rotation, rotate_180=true` 로 1회 호출됨(monkeypatch).
- PATCH 에 `rotate_180` 없으면 publish 호출 안 됨.
- publish 가 예외를 던져도 200 (best-effort).
- GET 응답에 `capabilities` null / dict 양쪽 직렬화.

`tests/test_mqtt_handlers.py`
- camera telemetry에 `capabilities` 있으면 `cameras.capabilities` UPDATE 포함.
- `rotate_180` 보고값 ≠ DB → `publish_command` 1회, 같으면 0회, 키 없으면 0회.
- 구 페이로드(`ts/uptime_sec/free_heap` 만) 회귀 없음.

## 6. 배포 순서 / 호환성

1. 마이그레이션 적용(컬럼 추가만이라 무중단).
2. terra-api + mqtt bridge 배포. 구 펌웨어는 `set_rotation` 을 `rejected_unknown_action` 으로 ack 하고, 텔레메트리에 신규 키가 없어 동기화 로직이 비활성 → 무해.
3. 펌웨어 배포 → 재연결 시 `capabilities` 보고 → 앱 토글 노출.
4. 앱 배포.

서버는 펌웨어보다 먼저 나가도 되고, 펌웨어보다 나중에 나가면 그동안 카메라 텔레메트리의 미지 키는 무시되므로 순서 제약 없음.

## 7. 체크리스트 (2026-09-08 구현 완료, 배포 대기)

- [x] 마이그레이션 작성 (`migrations/2026-09-08_cameras_rotate_capabilities.sql`)
- [ ] **마이그레이션 Supabase 적용** → `MIGRATIONS_APPLIED.md` ⬜→✅
- [x] `CameraUpdate.rotate_180` / `CameraOut.rotate_180`, `capabilities`
- [x] `update_camera` → `_publish_rotation` (best-effort, `MqttWebRTCSignaling` 재사용)
- [x] 명령 페이로드 공유 헬퍼 `backend/mqtt/camera_commands.py`
- [x] `handle_telemetry` camera 분기: capabilities 저장(같으면 생략) + rotate_180 불일치 재발행(카메라당 60초 최소 간격) + 30초 TTL 캐시
- [x] 브리지가 `handlers.set_command_publisher(self.publish_command)` 등록
- [x] MQTT.md / API.md / DATABASE.md (FIRMWARE_INTEGRATION.md 는 디바이스 전용이라 MQTT.md 에만 기록)
- [x] 테스트 10종 (`test_cameras_api.py` 4, `test_mqtt_handlers.py` 6) — 전체 175 passed
- [ ] terra-api + mqtt bridge 배포 (`docs/DEPLOYMENT.md`)
- [ ] 배포 후 앱 팀 통보 + 펌웨어 회신 문서 §8/§10 갱신

**구현 중 결정한 것**
- capabilities 는 값이 같으면 UPDATE 에서 제외 — 앱이 cameras Realtime 을 구독하므로 15초마다 UPDATE 이벤트가 나가는 걸 막기 위해 필수로 격상.
- 재발행 최소 간격 60초 — 카메라가 적용 실패로 옛 값을 계속 보고해도 15초마다 명령이 나가지 않게.
- `MqttWebRTCSignaling` 리네임은 안 함(범위 밖). 라우터 주석에 실체("카메라 command 토픽 one-shot publish") 명시.
