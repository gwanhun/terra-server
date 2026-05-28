# Stage E — 시계열 다운샘플 (telemetry → telemetry_1m) (⏸️ 보류)

## 개요

`telemetry` 원본 (3초 주기) 을 1분 단위로 집계 → `telemetry_1m` 에 저장. 원본은 7일 후 자동 삭제. 장기 트렌드 / 그래프 / 통계는 `telemetry_1m` 사용.

## In

- Stage A 완료 (`telemetry` 에 데이터 누적 중)
- Supabase 에서 `pg_cron` 확장 활성화

## Out

- 1시간 / 1일 단위 추가 다운샘플 (필요 시 차후)
- InfluxDB 마이그레이션 (디바이스 100+ 도달 시)
- 다운샘플 데이터 시각화 (앱 화면)

## 완료 조건

- [ ] `migrations/YYYY-MM-DD_pgcron_downsample.sql` 작성
  - [ ] `pg_cron` 확장 활성화
  - [ ] 매 분: `telemetry` 직전 1분 → AVG/MIN/MAX → `telemetry_1m` UPSERT
  - [ ] 매 시간: `telemetry` 7일 이전 데이터 DELETE
- [ ] 데이터 검증 쿼리 (예: 1분 데이터 빠짐 없는지)
- [ ] Supabase 대시보드 cron job 상태 모니터링

## 설계 메모

### 왜 pg_cron?
- DB 안에서 완결 → 외부 스케줄러 불필요
- 트랜잭션 보장
- Supabase 가 공식 지원

대안:
- 브리지 안에 asyncio 스케줄러 → DB 와 분리되어 복원력 약함
- Supabase Edge Function + cron → 외부 호출 비용

### UPSERT 사용 이유
같은 분 bucket 에 두 번 INSERT 시도 시 중복. `ON CONFLICT (device_id, bucket) DO UPDATE` 로 재실행 안전.

### 7일 보관 근거
- 디바이스 1대 = 3초 × 60 × 60 × 24 × 7 = 약 20만 row, ~30MB
- 디바이스 10대 = ~300MB → Supabase Free tier 500MB 안
- 90일 보관 시 약 4GB → Pro tier 필요

### 집계 컬럼
- 온도/습도: AVG / MIN / MAX (트렌드 + 극값 모두 보존)
- 액추에이터 상태 (relay/fan/heater): 집계 의미 없음 → 1m 테이블에서 제외
  - 변경 이력은 `commands` + `ack` 에서 추출 가능

## 학습 노트

### pg_cron 작성 패턴

```sql
SELECT cron.schedule(
  'downsample-telemetry-1m',
  '* * * * *',  -- 매 분
  $$
  INSERT INTO telemetry_1m (device_id, bucket, t_a_avg, t_a_min, t_a_max, ...)
  SELECT
    device_id,
    date_trunc('minute', ts) AS bucket,
    AVG(t_a), MIN(t_a), MAX(t_a),
    ...
  FROM telemetry
  WHERE ts >= NOW() - INTERVAL '2 minutes'
    AND ts <  NOW() - INTERVAL '1 minute'
  GROUP BY device_id, date_trunc('minute', ts)
  ON CONFLICT (device_id, bucket) DO UPDATE SET
    t_a_avg = EXCLUDED.t_a_avg,
    ...;
  $$
);
```

## 참고

- Supabase Docs > Extensions > pg_cron
- [docs/DATABASE.md](../docs/DATABASE.md) — 시계열 관리 정책
