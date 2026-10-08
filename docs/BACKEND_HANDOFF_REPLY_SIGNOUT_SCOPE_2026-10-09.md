# 스크립트·웹 도구 로그아웃 범위 — 회신 (2026-10-09)

> 요청: tera-ai-flutter `docs/handoffs/2026-10-09-global-signout-request.md`

## 결론

같은 Supabase 프로젝트에서 global 로그아웃을 하던 곳은 두 군데예요. terra-server 쪽은 이 PR 로 고쳤고, petcam-lab 쪽은 별도로 처리합니다.

| 도구 | 로그아웃 호출 | 상태 |
|---|---|---|
| terra-server 웹 콘솔 (`web/index.html`) | 상단 [로그아웃] 버튼 + 베타 등록 패널 8곳 | 이 PR 로 `scope: 'local'` |
| terra-server `scripts/seed_test_pets.py` | 테스터 계정 일괄 로그인 후 `sign_out()` | 이 PR 로 `scope: 'local'` |
| petcam-lab 라벨링 웹 (`web/src/app/labeling/layout.tsx`) | [로그아웃] 버튼 + 401 시 자동 로그아웃 | **아직 global** — petcam-lab 에서 별도 수정 |

| 사고 | 추정 원인 | 확실한 정도 |
|---|---|---|
| 10/8 13:26 테스터 계정 약 25개 9초 로그인→로그아웃 | 콘솔 베타 등록 패널 계정 목록 자동 조회(`checkAllAccounts`, 10/8 추가). 계정마다 `signInWithPassword` → API 조회 → `signOut()` | 높음 — 같은 날 추가된 기능이고 패턴(계정 다수·초 단위 연속)이 일치 |
| 10/6 16:21 leegawnhun@ 로그아웃 | 콘솔 [로그아웃] 버튼 **또는** petcam-lab 라벨링 웹 [로그아웃] | **미확인** — IP(211.234.226.193)만으로는 어느 도구인지 구분 못 함. 둘 다 고치면 원인과 관계없이 재발은 막힘 |

모두 supabase 기본값(global)이라 그 계정의 앱 세션까지 지워졌어요.

## 바꾼 곳

- `web/index.html` — `signOut()` 9곳 전부 `signOut({ scope: 'local' })` (콘솔 로그아웃 1 + 베타 패널 조회·OTA·재부팅·해제·일괄 명령 8)
- `scripts/seed_test_pets.py` — `sign_out()` → `sign_out({"scope": "local"})` (테스터 계정 일괄 로그인 스크립트)

`local` 은 그 브라우저/스크립트가 만든 세션 하나만 끊어서, 같은 계정의 폰 앱 세션은 그대로 남습니다.

## 요청 3 (service role / admin API)

이번엔 하지 않았어요. 베타 패널은 "그 계정 권한으로 백엔드 API 를 호출" 하는 게 목적이라(소유자 검증 경로 그대로 확인) 사용자 JWT 가 필요합니다.
대신 계정별 access_token 을 만료 1분 전까지 재사용하고 있어 로그인 횟수 자체는 적어요. 로그아웃이 local 이 된 지금은 로그인 횟수가 앱 세션에 영향을 주지 않습니다.

## 배포 상태

- [ ] terra-server PR 머지 + 서버 반영
- [ ] 반영 후 **이미 열려 있던 콘솔 탭은 새로고침** — 열린 탭은 옛 JS(global 로그아웃)로 계속 돈다
- [ ] petcam-lab 라벨링 웹 수정·배포
- 셋 다 끝나기 전까지는 콘솔 베타 패널·로그아웃 버튼, 라벨링 웹 로그아웃을 테스터 계정에 쓰지 말아 주세요.

## 재발 방지

`tests/test_signout_scope.py` — `web/index.html`·`scripts/*.py` 에 scope local 없는 `signOut(`/`sign_out(` 이 생기면 pytest 실패.
