-- 푸시 스테이징 샘플 — device.action.failed (no_ack) 한 건을 push_outbox 에 직접 적재.
--
-- 언제 쓰나: failed 는 기기가 응답을 안 해야 생기는데, 온라인 기기로는 만들기 어렵다.
--   실제 ACK 경로는 started/ended/skipped 로 검증하고, failed 는 이걸로 전송·수신만 검증한다.
--   워커가 5초 안에 집어가 Edge Function 으로 POST 한다. 결과는 아래 확인 쿼리로.
--
-- 사용: <...> 두 곳을 채워서 Supabase SQL Editor 에서 실행.
--   테스트 계정 uuid       : 앱 팀이 알려줌 (auth.users.id)
--   기기 uuid / device_id  : SELECT id, device_id, name FROM devices WHERE name LIKE '%03%';
--
-- 재실행하려면 event_id 끝 숫자를 바꿔야 한다 (UNIQUE).

INSERT INTO public.push_outbox (event_id, event_type, body)
SELECT
  'command:00000000-0000-4000-8000-0000000f0001:failed',
  'device.action.failed',
  jsonb_build_object(
    'schema_version', 1,
    'event_id',       'command:00000000-0000-4000-8000-0000000f0001:failed',
    'type',           'device.action.failed',
    'occurred_at',    now(),
    'user_id',        '<테스트 계정 uuid>',
    'payload', jsonb_build_object(
      'command_id',       '00000000-0000-4000-8000-0000000f0001',
      'device_id',        d.id,
      'device_key',       d.device_id,
      'enclosure_id',     d.enclosure_id,
      'schedule_id',      NULL,
      'execution_source', 'schedule',
      'execution_phase',  'failed',
      'action',           'fan_on',
      'outcome',          'failed',
      'result',           'no_ack',
      'device_name',      d.name
    )
  )
FROM public.devices d
WHERE d.device_id = '<기기 device_id 예: terra-a1b2c3d4>';

-- 확인 (5~10초 뒤): status 가 sent 면 앱이 202 로 받은 것. failed 면 last_error 에 응답이 남는다.
-- SELECT event_id, status, attempts, last_error, sent_at
--   FROM public.push_outbox ORDER BY created_at DESC LIMIT 5;
