-- 2026-09-16: 그룹(enclosures) 이름 중복 금지 — 앱 회신 2026-09-16 §1-1 확정 규칙
--
-- 배경: 앱이 사전 중복 검사를 해도 동시 등록을 막을 수 없어 DB 제약이 필요하다
--   (2026-09-15 회신 §3.1). 앱 팀이 비교 규칙을 확정해 보내와 그대로 반영한다.
--
-- ⚠️ 비교 규칙 (앱·서버·앱팀 RPC 가 반드시 동일해야 함):
--   - 공백: **앞뒤만** 제거(btrim). 가운데 공백은 유지하고 글자수에도 포함
--            → "사육 환경 1" 은 7자
--   - 대소문자: **구분한다.** lower() 를 쓰지 않는다.
--            한쪽만 lower 면 앱 사전 검사와 서버 409 가 어긋난다 (앱 §1-1 명시)
--   - 길이 10자 제한과 허용 문자(출력가능 ASCII + 완성형 한글) 검증은 **앱과 RPC 담당**.
--            PostgreSQL char_length 는 grapheme 수가 아니라 여기서 강제하지 않는다.
--
-- 범위: 그룹 이름은 같은 계정의 enclosures 안에서만 유일.
--   (사육장·카메라 이름은 devices+cameras 두 테이블 합산이라 단일 인덱스로 불가 →
--    앱팀 RPC `redesign_rename_item_v1` 이 담당. 앱 §1-2)
--
-- 이 인덱스는 앱팀 RPC 의 검사와 **이중 방어**다 (앱 §1-2 에서 찬성).
--
-- ⚠️ 적용 전 확인: 이미 중복 이름이 있으면 인덱스 생성이 실패한다. 아래 쿼리로 먼저 점검.
--   SELECT owner_id, btrim(name), count(*)
--     FROM public.enclosures GROUP BY 1, 2 HAVING count(*) > 1;
--   중복이 있으면 사용자에게 보이는 이름을 먼저 정리한 뒤 실행할 것.
--   (기존 이름은 자동 개명하지 않는다 — 앱 §1-1)


CREATE UNIQUE INDEX IF NOT EXISTS uq_enclosures_owner_name
    ON public.enclosures (owner_id, btrim(name));

COMMENT ON INDEX public.uq_enclosures_owner_name
    IS '같은 계정 내 그룹 이름 유일. btrim 만 적용(대소문자 구분) — 앱 2026-09-16 §1-1';


-- =====================================================================
-- 적용 확인
-- =====================================================================
--   SELECT indexname FROM pg_indexes
--    WHERE tablename = 'enclosures' AND indexname = 'uq_enclosures_owner_name';
