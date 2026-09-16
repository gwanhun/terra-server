-- 푸시 스테이징 샘플 4종 — push_outbox 에 직접 적재해 워커가 Edge Function 으로 보내게 한다.
--
-- 언제: 테스트 계정에 실제 예약이 없어 ACK 경로로는 이벤트가 안 생길 때 (앱 요청 2026-09-16).
-- 무엇: started(fan_on) / ended(fan_off, 구간 예약) / failed(no_ack) / skipped(guard 포함).
--       본문 형식은 backend/push_events.py 의 build_command_event / build_skipped_event 와 동일.
-- 주의: 실제 명령 행(commands)은 만들지 않는다. 기기도 안 돈다. 전송·수신·문구만 검증한다.
--
-- 순서:
--   [1] 아래 INSERT 실행 → 5~10초 뒤 [확인] → 4건 sent
--   [2] 중복 테스트: [재전송] 실행 → 같은 event_id 로 다시 POST → 앱이 202 duplicate 처리하는지
--   [3] 끝나면 [정리] 로 샘플 행 삭제 (선택)
--
-- <...> 두 곳을 채운다: 테스트 계정 user_id(auth.users.id), 기기 uuid(devices.id).
-- device_key/enclosure_id/device_name 은 devices 행에서 가져온다.

-- ===== [1] 적재 =====
WITH p AS (
  SELECT '<테스트 계정 user_id>'::text AS user_id,
         d.id::text AS device_id, d.device_id AS device_key, d.enclosure_id, d.name AS device_name
    FROM public.devices d
   WHERE d.id = '<기기 uuid>'
), ev AS (
  SELECT * FROM (VALUES
    ('00000000-0000-4000-8000-0000000f0001', 'started', 'device.action.started', 'fan_on',  'succeeded', 'ok',            NULL::jsonb),
    ('00000000-0000-4000-8000-0000000f0002', 'ended',   'device.action.ended',   'fan_off', 'succeeded', 'ok',            NULL),
    ('00000000-0000-4000-8000-0000000f0003', 'failed',  'device.action.failed',  'fan_on',  'failed',    'no_ack',        NULL),
    ('00000000-0000-4000-8000-0000000f0004', 'skipped', 'device.action.skipped', 'fan_on',  'skipped',   'guard_skipped',
       '{"kind":"skip_when_temp_above","metric":"temperature","threshold":30,"value":33.5}'::jsonb)
  ) v(cmd, phase, type, action, outcome, result, guard)
)
INSERT INTO public.push_outbox (event_id, event_type, body)
SELECT
  'command:' || ev.cmd || ':' || ev.phase,
  ev.type,
  jsonb_build_object(
    'schema_version', 1,
    'event_id',       'command:' || ev.cmd || ':' || ev.phase,
    'type',           ev.type,
    'occurred_at',    now(),
    'user_id',        p.user_id,
    'payload',
      jsonb_build_object(
        'command_id',       ev.cmd,
        'device_id',        p.device_id,
        'device_key',       p.device_key,
        'enclosure_id',     p.enclosure_id,
        'schedule_id',      '00000000-0000-4000-8000-0000000f00aa',
        'execution_source', 'schedule',
        'execution_phase',  ev.phase,
        'action',           ev.action,
        'outcome',          ev.outcome,
        'result',           ev.result,
        'device_name',      p.device_name
      )
      || CASE WHEN ev.guard IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('guard', ev.guard) END
  )
FROM ev CROSS JOIN p;

-- ===== [확인] =====
-- SELECT event_id, status, attempts, last_error, sent_at
--   FROM public.push_outbox
--  WHERE event_id LIKE 'command:00000000-0000-4000-8000-0000000f000%' ORDER BY event_id;

-- ===== [재전송] 같은 event_id 로 한 번 더 (중복 처리 검증) =====
-- UPDATE public.push_outbox
--    SET status = 'pending', attempts = 0, next_retry_at = now(), last_error = NULL
--  WHERE event_id LIKE 'command:00000000-0000-4000-8000-0000000f000%';

-- ===== [정리] =====
-- DELETE FROM public.push_outbox WHERE event_id LIKE 'command:00000000-0000-4000-8000-0000000f000%';
