# 스크립트·웹 도구 로그아웃 범위 — 회신 (2026-10-09)

> 요청: tera-ai-flutter `docs/handoffs/2026-10-09-global-signout-request.md`

## 결론

원인 도구는 이 레포의 **웹 콘솔(`web/index.html`)** 이었어요. 로그아웃을 전부 `scope: 'local'` 로 바꿨습니다.

| 사고 | 원인 코드 |
|---|---|
| 10/8 13:26 테스터 계정 약 25개 9초 로그인→로그아웃 | 베타 등록 패널 계정 목록 자동 조회(`checkAllAccounts`, 10/8 추가). 계정마다 `signInWithPassword` → API 조회 → `signOut()` |
| 10/6 16:21 leegawnhun@ 로그아웃 | 콘솔 상단 [로그아웃] 버튼 `sb.auth.signOut()` |

둘 다 supabase-js 기본값(global)이라 그 계정의 앱 세션까지 지워졌어요.

## 바꾼 곳

- `web/index.html` — `signOut()` 9곳 전부 `signOut({ scope: 'local' })` (콘솔 로그아웃 1 + 베타 패널 조회·OTA·재부팅·해제·일괄 명령 8)
- `scripts/seed_test_pets.py` — `sign_out()` → `sign_out({"scope": "local"})` (테스터 계정 일괄 로그인 스크립트)

`local` 은 그 브라우저/스크립트가 만든 세션 하나만 끊어서, 같은 계정의 폰 앱 세션은 그대로 남습니다.

## 요청 3 (service role / admin API)

이번엔 하지 않았어요. 베타 패널은 "그 계정 권한으로 백엔드 API 를 호출" 하는 게 목적이라(소유자 검증 경로 그대로 확인) 사용자 JWT 가 필요합니다.
대신 계정별 access_token 을 만료 1분 전까지 재사용하고 있어 로그인 횟수 자체는 적어요. 로그아웃이 local 이 된 지금은 로그인 횟수가 앱 세션에 영향을 주지 않습니다.

## 배포 상태

- [ ] PR 머지 + 서버 반영 (`web/` 는 정적 서빙이라 반영 즉시 적용)
- 반영 전까지는 콘솔 베타 패널·로그아웃 버튼을 테스터 계정에 쓰지 말아 주세요.
