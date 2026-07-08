# 앱 온습도 그래프 가이드 (시계열 차트)

> **앱 개발자용.** 사육장 온도/습도 추이 그래프를 그리는 단일 진실 소스.
> 전제: 인증·RLS·Supabase 클라이언트 초기화는 [docs/APP_INTEGRATION.md](APP_INTEGRATION.md) 먼저 읽기.
> 관련: [migrations/2026-06-30_telemetry_30m_pgcron.sql](../migrations/2026-06-30_telemetry_30m_pgcron.sql) — 이 테이블을 만든 마이그레이션.

---

## 0. 핵심 — 테이블 3개, 용도가 다르다

| 테이블 | 주기 | 보관 | 쓰는 곳 |
|--------|------|------|---------|
| `telemetry` | 3초 원본 | **7일** | 실시간 현재값, 최근 몇 시간 상세 |
| `telemetry_1m` | 1분 집계 | (현재 미사용, 빈 테이블) | — |
| **`telemetry_30m`** | **30분 집계** | **영구** | **장기 추이 그래프 (이 문서)** |

> ⚠️ `telemetry` 는 7일만 보관돼. "지난 한 달 온도 추이" 같은 건 반드시 `telemetry_30m` 으로 그려야 해. 7일 넘은 원본은 DB 가 자동 삭제하거든.

---

## 1. `telemetry_30m` 컬럼

30분 구간마다 1행. 온도/습도 각각 **평균·최소·최대** 3종 + 샘플 수.

| 컬럼 | 의미 |
|------|------|
| `device_id` | 사육장(디바이스) UUID. `devices.id` |
| `bucket` | 30분 경계 시각 (예: `12:00:00`, `12:30:00`), `timestamptz` |
| `sample_count` | 이 30분에 들어온 원본 행 수. **정상 ≈ 600** (30분÷3초) |
| `t_a_avg` / `t_a_min` / `t_a_max` | DHT22-A(메인) 온도 평균/최소/최대 (°C) |
| `h_a_avg` / `h_a_min` / `h_a_max` | DHT22-A 습도 (%RH) |
| `t_b_avg` / `t_b_min` / `t_b_max` | DHT22-B(보조) 온도 |
| `h_b_avg` / `h_b_min` / `h_b_max` | DHT22-B 습도 |

> 대부분 화면은 **메인 센서(A)** 의 `t_a_avg` / `h_a_avg` 만 쓰면 돼. 보조(B)는 듀얼 센서 사육장에서만.

---

## 2. 기본 조회 — 기간별 추이

RLS 가 자동으로 본인 디바이스만 필터하니까 `owner_id` 신경 안 써도 돼. `device_id` 만 맞으면 됨.

### JS (`@supabase/supabase-js`)

```js
// 최근 7일 온습도 추이 (메인 센서)
const weekAgo = new Date(Date.now() - 7 * 864e5).toISOString();

const { data, error } = await sb
  .from('telemetry_30m')
  .select('bucket, t_a_avg, t_a_min, t_a_max, h_a_avg, sample_count')
  .eq('device_id', deviceUuid)
  .gte('bucket', weekAgo)
  .order('bucket', { ascending: true });

// data = [{ bucket, t_a_avg, t_a_min, t_a_max, h_a_avg, sample_count }, ...]
```

### Dart (Flutter)

```dart
final weekAgo = DateTime.now().toUtc().subtract(const Duration(days: 7));

final data = await sb
    .from('telemetry_30m')
    .select('bucket, t_a_avg, t_a_min, t_a_max, h_a_avg, sample_count')
    .eq('device_id', deviceUuid)
    .gte('bucket', weekAgo.toIso8601String())
    .order('bucket', ascending: true);
```

### 기간별 포인트 수 (차트 밀도 감 잡기)

| 기간 | 포인트 수 | 비고 |
|------|----------|------|
| 24시간 | 48 | 적당 |
| 7일 | 336 | 적당 |
| 30일 | 1,440 | 라인 차트 OK, 모바일은 약간 촘촘 |
| 1년 | 17,520 | **너무 많음** → §5 의 시간 추가 집계 권장 |

---

## 3. 차트 그리기 — 평균 라인 + min/max 밴드

30분 집계의 핵심 가치는 **min/max** 야. 평균 라인만 그리면 "그 30분 사이 잠깐 38도 튐" 을 놓쳐. 권장은 **평균 라인 + 최소~최대 음영 밴드**.

```js
// 차트 라이브러리에 넘길 형태로 변환 (예: Recharts / Chart.js)
const series = data.map(r => ({
  x: new Date(r.bucket),
  avg: r.t_a_avg,
  // min~max 밴드: 면적 차트의 [하한, 상한]
  band: [r.t_a_min, r.t_a_max],
  // sample_count 낮으면 점선/반투명 처리용 플래그 (§4)
  partial: r.sample_count < 300,
}));
```

UI 권장:
- **온도/습도 그래프 분리** (단위가 °C / %RH 로 달라 같이 그리면 헷갈림)
- 평균은 실선, min~max 는 반투명 밴드
- 목표 범위(`device_settings.target_temp_min/max`)를 가로 띠로 깔면 "정상 범위 벗어남" 이 한눈에 보임

---

## 4. 데이터 빠짐 처리 — `sample_count`

사육장이 오프라인이었거나 와이파이가 끊긴 구간은 그 30분 버킷이 **아예 없거나**, 일부만 차서 `sample_count` 가 낮아.

| 상황 | 데이터 | 처리 |
|------|--------|------|
| 정상 | 행 있음, `sample_count ≈ 600` | 그대로 |
| 일부 끊김 | 행 있음, `sample_count` 낮음 (예: 80) | 그려도 되지만 **반투명/점선** 으로 "불완전" 표시 권장 |
| 완전 오프라인 | **행 자체가 없음** | 선을 잇지 말고 **끊어서** 표시 (gap) |

> 행이 없는 구간을 라이브러리가 직선으로 이어버리면 "그 동안 데이터 있었던 것처럼" 보여. 버킷 간격이 30분보다 크게 벌어지면 끊는 게 정확해.

```js
// 30분보다 더 벌어진 구간 = 데이터 공백 → null 삽입해서 선 끊기
const withGaps = [];
for (let i = 0; i < series.length; i++) {
  if (i > 0) {
    const gapMs = series[i].x - series[i - 1].x;
    if (gapMs > 30 * 60 * 1000 * 1.5) withGaps.push({ x: null });  // gap 마커
  }
  withGaps.push(series[i]);
}
```

---

## 5. 1년 이상 — 시간/일 단위로 더 줄이기

`telemetry_30m` 을 1년 통째로 받으면 1.7만 포인트라 모바일이 버거워. 화면이 보여줄 해상도에 맞게 **DB 에서 더 집계**해서 받아. 두 방법:

### (a) 클라이언트에서 솎기 (간단)
1년 뷰는 30분 단위가 무의미하니, 받아서 하루 1포인트로 평균 내거나 `n` 개마다 1개만 그려.

### (b) 서버 RPC (권장, 트래픽 절약)
일 단위 집계를 DB 에서 끝내고 365 포인트만 받기. Supabase 대시보드에 함수 한 번 만들면 돼:

```sql
create or replace function public.telemetry_daily(
  p_device_id uuid, p_from timestamptz, p_to timestamptz
)
returns table (day date, t_a_avg float, t_a_min float, t_a_max float, h_a_avg float)
language sql stable security invoker as $$
  select date_trunc('day', bucket)::date as day,
         avg(t_a_avg), min(t_a_min), max(t_a_max), avg(h_a_avg)
  from public.telemetry_30m
  where device_id = p_device_id and bucket >= p_from and bucket < p_to
  group by 1 order by 1;
$$;
```

> `security invoker` 라서 호출한 사용자의 RLS 가 그대로 적용돼 — 본인 디바이스만 집계됨. 앱에서는:
> ```js
> const { data } = await sb.rpc('telemetry_daily', {
>   p_device_id: deviceUuid, p_from: yearAgo, p_to: now,
> });
> ```
> 이 함수는 아직 **마이그레이션에 없음** — 1년 뷰가 실제로 필요해질 때 추가하면 돼 (YAGNI).

---

## 6. 실시간(현재값) + 과거(그래프) 합치기

흔한 화면: **상단에 "지금 26.4°C" (실시간) + 하단에 추이 그래프(과거)**. 데이터 출처가 둘이야.

| 부분 | 출처 | 방법 |
|------|------|------|
| 현재값 / 최근 몇 시간 | `telemetry` (3초 원본) | Realtime 구독 ([APP_INTEGRATION §3.7](APP_INTEGRATION.md)) |
| 장기 추이 | `telemetry_30m` | 이 문서의 SELECT |

```
[26.4°C  습도 55%]   ← telemetry 실시간 구독 (3초마다 갱신)
─────────────────
 온도 추이 (7일)      ← telemetry_30m SELECT (화면 진입 시 1회 로드)
   /\    /\
__/  \__/  \___
```

> ⚠️ **가장 최근 버킷은 "진행 중" 일 수 있어.** 예를 들어 지금이 12:40 이면 `12:30` 버킷은 아직 30분이 안 차서 부분 집계값(다음 cron 때 갱신됨)이야. 그래프 맨 끝점이 살짝 출렁여도 정상이고, 정확한 "지금" 값은 실시간 `telemetry` 를 써.

---

## 7. 디버그 SQL (Supabase 대시보드)

```sql
-- 특정 디바이스 최근 24시간 30분 추이
SELECT bucket, sample_count, t_a_avg, t_a_min, t_a_max, h_a_avg
FROM telemetry_30m
WHERE device_id = '<uuid>' AND bucket >= now() - interval '24 hours'
ORDER BY bucket DESC;

-- 데이터 빠진 구간 찾기 (sample_count 낮은 버킷)
SELECT device_id, bucket, sample_count
FROM telemetry_30m
WHERE sample_count < 300 ORDER BY bucket DESC LIMIT 50;

-- 집계 cron 이 잘 도는지
SELECT jobid, status, return_message, start_time
FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;
```

---

## 부록. 권한 (RLS)

`telemetry_30m` 은 SELECT 만 열려 있고, 본인 소유 디바이스 행만 보여 (`auth.uid()` 자동 검증). INSERT/UPDATE 는 DB 내부 cron 만 — 앱은 읽기 전용이야. 잘못된 `device_id` 로 조회하면 빈 배열이 와 (에러 아님).
