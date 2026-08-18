-- 2026-08-18: schedules.pair_id (구간 예약 묶음) — 앱 핸드오프 §3 (B안)
--
-- 배경: 구간(시작~종료) 예약을 앱이 *_on / *_off 2행으로 만드는데 서버엔 연결 정보가
-- 없어, 한쪽만 지우면 고아 예약(켜지고 안 꺼짐 / 껐다 안 켜짐)이 남는다.
-- 같은 구간의 두 행에 동일 pair_id(앱 생성 UUID)를 부여 → 한쪽 삭제 시 서버가 짝도 삭제.
-- 러너는 무변경(각 행이 독립 발행). 앱은 pair_id 로 목록을 구간 한 줄로 묶어 표시.
-- 적용 후 MIGRATIONS_APPLIED.md 등에 기록.

ALTER TABLE public.schedules
    ADD COLUMN IF NOT EXISTS pair_id UUID;

CREATE INDEX IF NOT EXISTS idx_schedules_pair
    ON public.schedules(pair_id) WHERE pair_id IS NOT NULL;
