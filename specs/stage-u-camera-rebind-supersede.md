# Stage U — 카메라 재등록 행 재사용 + 교차 계정 superseded

> 상태: 🔧 **구현 중 (2026-11-10)**. 서버만 수정, 펌웨어·앱 변경 없음(앱은 §앱팀 통지 1건). 구현: 이관훈(Claude).
> 배경 문서: 백승수 "영상·카메라 DB 현재 문제 정리"(2026-11-09, 레포 밖) §1·§7, 회신 2026-11-10 결정.
> 활동시간 저장(12시간 칸·finalized_at)·계정 단위 영상 조회·하이라이트 계정+hw_id 예산은 **별도 Stage**(레포 3개 공동, 배치 위치 미정).

## 왜

- cameras 169행 중 활성 37행, hw_id 보고 보드는 22개. 한 행 = 등록 1회가 됐다.
- 경로 ①: 같은 계정이 해제 → 재등록. `_find_by_hw_id` 가 `unlinked_at IS NULL` 만 봐서 매번 새 행(의도된 설계였음: 사용자가 해제한 카메라를 페어링이 몰래 복구하면 안 됨). **같은 계정이 다시 등록하는 건 사용자 의도**라 되살리는 게 맞다.
- 경로 ②: 다른 계정이 같은 보드 등록. 중복 검사가 계정 단위라 이전 계정 행이 활성으로 남는다. 명령·라이브는 보드에 안 닿지만(보드는 새 camera_id 토픽만 구독), offline_monitor 가 계속 스캔하고 ACL 에 계정이 남는다.
- 경로 ③: 구 펌웨어(hw_id 없음) — 손댈 수 없음. 그대로.

## 설계 (한 장)

```
POST /cameras/pair (hw_id 있음, 같은 계정)
  ├ 활성 행 있음        → 기존 동작(토큰 재발급, reused=true)
  ├ 해제 행만 있음      → 가장 최근 해제 행 되살림: unlinked_at=NULL, 설정 초기화, reused=true, revived=true
  │                       허브면 짝 devices 행도 같이 되살림
  └ 없음                → 새 행 (기존 동작)
  ※ 다른 계정의 행은 **여기서 건드리지 않는다** (아래 보안 이유)

브리지 offline_monitor (10분마다, 활성·hw_id 있는 cameras 를 hw_id 로 묶음)
  ├ 같은 hw_id 에 소유자 2명 이상, 그중 온라인 1명(승자)
  │   → 다른 소유자 행이 24시간 이상 오프라인(또는 24시간 전 생성·미접속)이면
  │     superseded_at=now, superseded_by=승자.  목록에서만 숨김. 행·설정·영상 유지
  ├ superseded 행이 다시 온라인(is_online=true) — 보드가 옛 자격증명으로 접속
  │   → superseded_at/by=NULL (자동 복구), 승자 행에 supersede_suspect_at=now
  └ superseded 30일 경과 + mqtt_revoked_at NULL → registry.unregister_device, mqtt_revoked_at=now
```

### 왜 페어링 요청 안에서 해제하지 않나 (보안)

`POST /cameras/pair` 는 로그인만 하면 부를 수 있고 hw_id 는 본문 값이다. 페어링 응답이 camera_token 평문을 돌려주므로 남의 MAC 으로 페어링한 사람은 브로커에 붙어 텔레메트리도 위조할 수 있다 → "첫 텔레메트리" 도 소유 증명이 아니다. **위조 불가능한 신호는 "피해자 보드가 꺼졌다"** 뿐이라 24시간 오프라인 조건을 쓴다. 정전·여행으로 정상 보드가 하루 꺼질 수 있으므로 해제 대신 superseded(숨김)만 하고, 보드가 돌아오면 자동 복구한다. MQTT 해지는 30일 뒤.

### 결정 요약 (U1~U8, 2026-11-10 백승수·이관훈)

| # | 결정 | 내용 |
|---|------|------|
| U1 | 되살리기 대상 | 같은 owner·같은 hw_id 의 **가장 최근 해제 행 1개**(created_at desc). 그보다 오래된 해제 행은 그대로(영상은 계정 단위 조회 Stage 에서 해결) |
| U2 | 초기화 항목 | `name`(요청값), `enclosure_id`(요청값, 없으면 NULL), `rotate_180=false`, `unlink_request_id=NULL`, superseded 3컬럼·`mqtt_revoked_at` NULL. **유지**: camera_id, 영상, clip_stats, 라이브 상태 |
| U3 | 허브 | 되살린 cameras 행의 `device_id` 짝 devices 행도 `unlinked_at=NULL`·name·enclosure_id 초기화 |
| U4 | superseded 판정 | 새 행 온라인 **그리고** 이전 행 `last_seen_at` 24시간 초과(미접속 행은 `created_at` 24시간 초과). offline_monitor 스캔, 페어링 요청 안에서는 아무것도 안 바꿈 |
| U5 | 효과 범위 | GET /cameras 목록 제외만. GET /cameras/{id}·영상·설정은 그대로. 앱은 cameras 를 PostgREST 로 직접 읽으므로 **앱도 `superseded_at IS NULL` 필터 추가 필요**(통지) |
| U6 | 자동 복구 | superseded 행 `is_online=true` 면 해제(브리지 `_lookup_active_uuid` 는 unlinked_at 만 보므로 옛 자격증명 텔레메트리가 그대로 들어온다). 승자 행 `supersede_suspect_at` 기록 |
| U7 | MQTT 해지 | superseded 30일 뒤. `_build_acl_content` 는 `mqtt_revoked_at` 행 제외 |
| U8 | 같은 hw_id 소유자 2명이 동시 온라인 | 판정 보류 + warning 로그(물리적으로 불가 → 위조 의심) |

## In / Out

### In
- `migrations/2026-11-10_cameras_supersede.sql`: 4컬럼 + 부분 인덱스
- `backend/routers/cameras.py`: `_find_by_hw_id` → 활성/해제 모두 조회해 (행, revived) 반환. pair 되살리기 패치(U2), `CameraPairResponse.revived`, `_ensure_hub_device` 되살리기(U3), `list_cameras` superseded 필터(U5)
- `backend/offline_monitor.py`: `scan_supersede()` — 판정(U4)·복구(U6)·해지(U7). 10분 간격
- `backend/mqtt/registry.py`: ACL 에서 `mqtt_revoked_at` 행 제외(U7)
- 테스트: `tests/test_cameras_api.py`(되살리기·목록 필터), `tests/test_hub.py`(허브 되살리기), `tests/test_offline_supersede.py`(FakeSB)
- 문서: `docs/DATABASE.md`(cameras 컬럼, 영상 보관 정책 107·109줄 현실 반영), `docs/API.md`(pair 응답 `revived`), `MIGRATIONS_APPLIED.md`

### Out (안 함)
- 활동시간 저장·finalized_at, 계정 단위 영상 조회, 하이라이트 예산 → 별도 Stage
- devices(센서 기기) 교차 계정 superseded — 같은 구조지만 이번 결정 범위 밖. 허브 짝 행은 cameras 쪽 판정에 따라감(계정이 하나)
- 구 펌웨어(hw_id NULL) 행 정리
- 라벨링 웹(petcam-lab) superseded 기준 통일 — 컬럼이 생겼으니 그쪽에서 읽으면 됨(통지)
- DELETE /clips FK 순서 문제 — 별도

## 함정 (구현·배포 때 반드시)

1. **마이그레이션 먼저.** `list_cameras` 가 `superseded_at` 필터를 쓰므로 컬럼 없으면 PostgREST 400 → 앱 카메라 목록(콘솔 경로) 실패. 브리지 스캔은 컬럼 없으면 예외 → 로그만(다른 스캔 영향 없음).
2. **되살리기와 (owner_id, hw_id) 부분 UNIQUE**: 활성 행이 있으면 활성 행을 쓰므로 충돌 없음. 해제 행 되살리기는 활성 행이 없을 때만.
3. **MOSQUITTO_REGISTRY_ENABLED 는 브리지도 읽는다**(같은 .env). 해지(U7)는 브리지 프로세스에서 `sudo` 헬퍼를 부른다 — terra-bridge 도 ubuntu 로 돌고 sudoers 는 사용자 기준이라 동작해야 하지만, **첫 해지 로그를 확인**할 것. 비활성이면 True 반환·로그만.
4. **테스트 mock 체인**: pair 의 hw_id 조회 체인이 `select→eq→eq→is_→limit` 에서 `select→eq→eq→order→execute` 로 바뀜. `_hw_lookup`(test_cameras_api)·`_hub_pair_tables`(test_hub) 갱신.
5. **자동 복구 신호는 is_online** — offline_monitor 가 superseded 행도 `is_online=false` 로 뒤집으므로(활성 행 스캔 그대로), 복구 판정은 "직전 스캔 뒤 다시 true 로 올라온 행" 이다. 10분 스캔 사이에 15초 heartbeat 가 오면 true 로 잡힌다.

## 배포 순서

1. Supabase SQL Editor 에 마이그레이션 적용 → `MIGRATIONS_APPLIED.md` 기록
2. PR 머지 → terra-api·terra-bridge 재시작
3. 확인: (a) 실기 해제 → 재페어링 → `revived: true`, 카메라 목록에 같은 행, 이름 초기화 (b) 브리지 로그 `supersede scan` 1회 이상 (c) 운영 DB `superseded_at IS NOT NULL` 행 수 — 기대값은 "활성 행이 보드 수보다 많은 13행 중 24시간 이상 오프라인인 것"
4. 앱팀 통지: cameras 조회에 `superseded_at IS NULL` 추가, pair 응답 `revived` 필드

## 완료 조건

- [ ] 마이그레이션 적용·기록
- [ ] 같은 계정 재페어링이 해제 행을 되살린다(테스트 + 실기)
- [ ] 허브 재페어링이 devices 짝 행도 되살린다(테스트)
- [ ] offline_monitor 가 교차 계정 superseded 를 찍고(테스트), 보드 재접속 시 풀고(테스트), 30일 뒤 해지(테스트)
- [ ] GET /cameras 가 superseded 행을 빼고 돌려준다(테스트)
- [ ] DATABASE.md 영상 보관 정책 현실 반영
- [ ] 앱팀 통지 발송

## 학습 노트

- "같은 보드 재등록 → 새 행" 은 버그가 아니라 2026-09-21 설계였다. 바뀐 건 요구사항(해제 행이 쌓이는 비용 > 몰래 복구 위험, 같은 계정이면 사용자 의도).
- 서버가 검증할 수 없는 클라이언트 값(hw_id)으로 **다른 사용자**의 상태를 바꾸면 안 된다. 바꿔야 한다면 그 사용자 쪽에서만 나올 수 있는 신호(보드 오프라인)를 기다린다.
