# 교차 검수 결과 (Codex + Claude) — specs/stage-m-ops-camera-reboot.md

## 요약
- Codex: 15건, Claude: 22건 / 공통 지적: 3건
- 최대 수정: 발행 → 기록 순서를 **기록 먼저(fail-closed)** 로 뒤집음

## 🔴 2모델 공통 — 수정
1. **발행 후 INSERT 실패 시 200 반환 = 쿨다운 무력화(fail-open).** 마이그레이션 전 배포나 Supabase 순단이면 감시 스크립트가 매시간 재부팅한다. 브리지가 브로커와 같은 호스트라, INSERT보다 ack가 먼저 와서 ack 기록도 유실될 수 있다(Claude).
   → **기록 먼저**: `cmd` 생성 → INSERT(`published=NULL`) → 발행 → `published` UPDATE. INSERT 실패면 발행하지 않고 503. 쿨다운은 `published IS DISTINCT FROM false`(진행 중 포함).
2. **`msg_id` UUID 컬럼인데 테스트는 `"m-1"`.** 실 DB라면 22P02로 실패한다.
   → 테스트는 UUID 문자열로 쓴다. `_record_ops_reboot_ack`는 `parse_uuid(msg_id)`가 실패하면 건너뛴다.
3. **기록 실패 시 복구 경로 없음**(Codex #8, Claude 렌즈2) → 1번 수정으로 해소된다.

## 🟡 단독 지적 — 채택/기각
| 출처 | 지적 | 판단 |
|------|------|------|
| Claude | FakeSB insert는 `requested_at` 기본값을 안 채운다 → 쿨다운 테스트 깨짐. `gte`에 None 가드 없음 | **채택.** 라우터가 `requested_at=now`를 명시하고, `gte`는 `lt`와 같은 None 가드 |
| Claude | Task 3 테스트 파일은 MagicMock 기반 → FakeSB 주입 방법 명시. 기존 ack 테스트 2건 회귀 확인 | **채택** |
| Claude | OTA 진행 ack마다 UPDATE 낭비 | **채택.** `ota` 블록이 있거나 `result`가 없으면 건너뜀 |
| Claude | OTA 작업 중 재부팅하면 작업이 깨짐 → `ota_active` 건너뜀 | **채택(계약 추가).** `ota_service.active_job_for` 재사용, 409 `ota_active`. 회신서에 추가 사유로 기록 |
| Claude | 브리지(terra-bridge)도 재시작해야 ack 기록이 동작 | **채택.** 배포 순서에 두 유닛 재시작 |
| Claude | MIGRATIONS_APPLIED 기록, `.env.example` 필수 수정 | **채택** |
| Claude | 시크릿 미설정 경고가 요청마다 쌓임 | **채택.** 경고는 시작 시 1회 |
| Claude | `labeling_group`은 "필터일 뿐 권한 아님"이고 감시 스크립트가 직접 설정 → 스크립트 버그에는 무방비 | **채택(문서화).** 실패 시나리오·회신서에 한계를 적고, 배포 전 beta 카메라 수 확인 |
| Codex | `ON DELETE CASCADE`가 리포트 기록을 지움 | **채택.** FK 없음(`ota_jobs`와 같은 이력 보존 관례) |
| Claude | 트랙 명시, "문서 4종" 표현(실제 5개) | **채택** |
| Codex | `live_session_id`가 남아 있으면 영구 차단 | **기각.** `LiveSessionReaper`가 15초마다 만료 세션을 지운다(`live_session.py:165-190`) |
| Codex | `publish()` False 반환 구분 누락 | **기각.** 반환은 None이고, 실패하면 예외를 던진다(`webrtc_signaling.py:158-182`) |
| Codex | 프로세스 내 락은 다중 워커를 못 막음 | **기각.** uvicorn 단일 프로세스(`docs/DEPLOYMENT.md:146`). 계획에 근거 명시 |
| Codex | 앱 확인 전 `last_ops_reboot_at` 미생성 선결정 | **기각.** 앱 코드로 확인했다(Claude 독립 재확인 일치). 회신서로 앱 담당 최종 확인만 요청 |
| Codex | `result` 분리는 소비자 계약 변경 | **기각(유지).** 소비자가 아직 없고, 발행과 ack는 채워지는 시점이 다르다. 회신서에 명시 |
| Codex | 건너뜀 미기록, 쿨다운 기준 | **사용자 결정 항목으로 유지** |
| Codex | 실 ack·앱 화면은 자동 검증 불가 | **인정.** 요청서 4절 수동 검증으로 둔다 |
| Codex | PR까지 스코프 초과 | **기각.** 사용자가 개발 착수를 요청했고 PR은 레포 관례. 머지·배포는 관훈님이 한다 |
| Codex | 요청서 frontmatter plan_path | **채택(경미).** 회신서에 계획서 경로·커밋을 적는다 |
| Claude | Task 3를 YAGNI로 빼기 | **기각.** 기록 먼저로 경합이 사라져 비용이 작다. 요청서 2-2가 원하는 것 |

## 💡 직관 질문
- **삭제 가능한 것:** 없음. Task 3(ack 기록)이 가장 약하지만, 기록 먼저 구조에서는 몇 줄이다.
- **최대 리스크:** 기록 실패 시에도 재부팅이 나가는 fail-open 구조였다 → 수정했다. 남은 리스크는 시크릿 유출(베타 카메라만, 2시간 1회로 제한).
- **우선 프로토타입:** 운영 저널에서 기존 재부팅의 msg_id와 ack 시각을 비교해 보기(Claude 제안). 기록 먼저 구조와 UUID 가드로 결과에 의존하지 않게 돼서 생략한다.
