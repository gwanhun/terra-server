-- 2026-09-28: commands.source 에 'restore' 추가 — 재부팅 후 예약 상태 복원 명령 (앱 회신 §2)
--
-- 배경: 기기가 재부팅(원격 reboot · 브라운아웃 · 크래시)하면 액추에이터가 전부 OFF 로 켜지는데
-- schedule_runner 는 next_run_at 시각에만 발행하므로 "지금 켜져 있어야 할" 조명·팬이 다음 예약까지
-- 꺼진 채였다. 브리지가 telemetry.uptime_sec 감소로 재부팅을 감지해 ON 명령을 1회 큐잉한다
-- (backend/schedule_restore.py). 예약 푸시(source='schedule' 만)가 나가지 않게 별도 source 값.
--
-- 적용 후 MIGRATIONS_APPLIED.md 에 기록. 코드(schedule_restore) 배포 전에 적용할 것 — 없으면
-- 복원 INSERT 가 CHECK 위반으로 실패한다(온라인 표시·다른 명령에는 영향 없음).

ALTER TABLE public.commands DROP CONSTRAINT IF EXISTS commands_source_check;
ALTER TABLE public.commands
    ADD CONSTRAINT commands_source_check
    CHECK (source IN ('manual', 'schedule', 'timer', 'guard', 'restore'));
