-- 2026-09-29: commands.source 에 'reconcile' 추가 — 예약 상태 재조정 교정 명령
--
-- 배경(specs/camera-reliability-2026-09.md P2-(b)): 예약 명령이 유실되면 다음 이벤트까지 액추에이터가
-- 어긋난 채였다(09-29 냉각팬이 예약 OFF 뒤 1시간 넘게 가동). 브리지가 telemetry 실제 상태와 예약상
-- "지금 있어야 할 상태"를 5분마다 비교해 어긋나면 교정 명령 1회를 큐잉한다(backend/actuator_reconcile.py).
-- 예약 푸시(source='schedule' 만)가 나가지 않게 별도 source 값.
--
-- 적용 후 MIGRATIONS_APPLIED.md 에 기록. **코드 배포 전에 적용할 것** — 없으면 교정 INSERT 가
-- CHECK 위반(23514)으로 실패한다(로그만 남고 온라인 표시·다른 명령에는 영향 없음).
-- ⚠️ petcam-lab 과 같은 Supabase 프로젝트 — commands 는 terra 전용 테이블이라 petcam 영향 없음.

ALTER TABLE public.commands DROP CONSTRAINT IF EXISTS commands_source_check;
ALTER TABLE public.commands
    ADD CONSTRAINT commands_source_check
    CHECK (source IN ('manual', 'schedule', 'timer', 'guard', 'restore', 'reconcile'));
