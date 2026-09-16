# 백엔드 회신 — 그룹·개체·이력 SQL 초안 5개 검토 (2026-09-16)

> **회신 대상**: 앱(Flutter) `tera-ai-flutter` 브랜치 `codex/figma-redesign-20260915` 의 `supabase/drafts/20260915_*.sql` 5개 + `tools/verify_redesign_sql.py` + `test/sql/*` + `docs/handoffs/2026-09-15-group-delete-atomic-contract.md`
> **작성**: terra-server 백엔드 담당
> **성격**: 초안을 terra-server 코드·운영 스키마와 대조한 검토. **초안은 전반적으로 좋습니다.** 배포 전에 고쳐야 할 것 5개, 권장 4개, 그리고 저희 쪽 대응을 적었습니다.
> **결론 먼저**: 초안의 전제인 **"모든 관계 writer 가 헬퍼를 직접 호출"** 은 terra-server 경로에서 성립하지 않습니다(service_role 은 `auth.uid()` 가 없음). **트리거 한 장**을 번들에 추가하면 서버·웹·앱 어느 경로든 자동으로 이력이 맞습니다. 아래 §1 에 SQL 을 드립니다.

---

## 0. 요약

| 초안 | 판정 | 배포 전 수정 |
|---|---|---|
| `20260915_assignment_history.sql` | ✅ 구조 좋음 | **B2** 카메라 FK RESTRICT → hard delete 충돌 · **B3** `user_id` ON DELETE 누락 |
| `20260915_redesign_groups.sql` | ✅ 구조 좋음 | **B3** 요청·카운터 테이블 `user_id` ON DELETE 누락 · **B4** 해제 기기 필터 · N2 unlink 스텁 제거 |
| `20260915_redesign_pets.sql` | ✅ 그대로 | — |
| `20260915_redesign_delete_group.sql` | ✅ 그대로 | — |
| `20260915_clip_visibility.sql` | ✅ 그대로 (서버 무관) | — |
| `tools/verify_redesign_sql.py` + `test/sql` | ✅ 격리 실행·롤백 좋음 | **B5** 스키마 스냅샷이 9/15 이전이라 `unlinked_at` 등 없음 |
| **(신규 제안)** 관계 트리거 | — | **B1** 아래 §1 SQL 을 번들에 추가 |

잘 된 점부터: `SECURITY DEFINER` + `search_path` 고정, 앱 권한 REVOKE/GRANT 정리, 멱등 요청 테이블과 payload 충돌 거부, `expected_members` 낙관적 동시성(`40001`), 그룹 삭제 시 구성원 행 잠금 + 타인 구성원 검사, 격리 컨테이너에서 롤백하는 검증기 — 전부 그대로 가시면 됩니다.

---

## 1. B1 — 트리거 부재: terra-server 경로가 이력 갱신을 타지 않습니다

### 문제

초안 머리말이 "모든 non-app writer 는 같은 owner lock 을 잡고 `redesign_reconcile_assignments` 를 호출해야 한다" 고 전제합니다. 그런데:

- `redesign_require_owner_lock()` 은 `auth.uid()` 로 owner 를 정합니다. **terra-server 는 service_role 로 접속해 `auth.uid()` 가 NULL** 이라 이 잠금 프로토콜에 참여할 수 없습니다.
- supabase-py 는 REST 단건 호출이라 `pg_advisory_xact_lock` 을 UPDATE 와 같은 트랜잭션에 넣을 수도 없습니다.

즉 아래 경로는 전부 이력을 건너뜁니다.

| 경로 | 동작 | 이력 |
|---|---|---|
| `PATCH /devices\|cameras/{id}` `enclosure_id` | 그룹 배정·해제 | ✗ |
| `POST /devices\|cameras/{id}/unlink` (9/16 배포) | `enclosure_id = NULL` | ✗ |
| `DELETE /enclosures/{id}` (REST) | FK `ON DELETE SET NULL` | ✗ |
| 웹 콘솔 `assignEnclosure()` | 위 PATCH 호출 | ✗ |

`reconcile` 이 owner 전체를 재계산하는 구조라 **다음 앱 RPC 때 자가 치유는 되지만**, 그 사이 `end_at` 이 실제보다 늦게 찍힙니다. 해제한 카메라의 개체 연결이 며칠 열려 있을 수 있습니다.

### 해법 — 트리거에서 헬퍼 호출 (9/15 회신 §4.4, 9/16 답신 §2-3-2 합의안)

`reconcile` 은 **상태 기반·멱등**이라 트리거에서 불러도 안전합니다. 아래를 `supabase/drafts/20260916_relationship_triggers.sql` 로 번들에 넣어 주세요. `redesign_reconcile_assignments` 가 앱 팀 소유라 함께 배포되는 게 맞습니다.

```sql
-- 관계가 바뀌면 경로와 무관하게 개체↔카메라 이력을 맞춘다.
-- 앱 RPC 는 이미 명시 호출하므로 같은 트랜잭션에서 두 번 돌지만 멱등이라 무해.
CREATE OR REPLACE FUNCTION public.redesign_touch_assignments()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE owner uuid;
BEGIN
  -- NEW.user_id / NEW.owner_id 를 한 식에 같이 쓰면 안 된다 — PL/pgSQL 은 식을 실행하기 전에
  -- 참조된 레코드 필드를 전부 파라미터로 바인딩하므로, cameras 행(user_id 없음)에서는
  -- CASE 분기와 무관하게 `record "new" has no field "user_id"` 로 죽는다 (앱 팀 격리 검증에서 발견).
  owner := (to_jsonb(NEW) ->> CASE TG_TABLE_NAME WHEN 'pets' THEN 'user_id' ELSE 'owner_id' END)::uuid;
  IF owner IS NOT NULL THEN
    PERFORM public.redesign_reconcile_assignments(owner, clock_timestamp());
  END IF;
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.redesign_touch_assignments() FROM PUBLIC, anon, authenticated;

-- 카메라: 그룹 이동·해제(unlink 는 enclosure_id=NULL 로 잡힘)
DROP TRIGGER IF EXISTS trg_cameras_touch_assignments ON public.cameras;
CREATE TRIGGER trg_cameras_touch_assignments
  AFTER UPDATE OF enclosure_id ON public.cameras
  FOR EACH ROW WHEN (OLD.enclosure_id IS DISTINCT FROM NEW.enclosure_id)
  EXECUTE FUNCTION public.redesign_touch_assignments();

-- 개체: 그룹 이동·툼스톤
DROP TRIGGER IF EXISTS trg_pets_touch_assignments ON public.pets;
CREATE TRIGGER trg_pets_touch_assignments
  AFTER UPDATE OF enclosure_id, deleted_at ON public.pets
  FOR EACH ROW WHEN (OLD.enclosure_id IS DISTINCT FROM NEW.enclosure_id
                  OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
  EXECUTE FUNCTION public.redesign_touch_assignments();
```

- `devices` 는 개체↔카메라 이력과 무관하므로 트리거 불필요합니다 (reconcile 이 pets↔cameras 만 봄).
- **`ON DELETE SET NULL` 도 행 UPDATE 트리거를 발화**시키므로 REST `DELETE /enclosures` 까지 자동 커버됩니다.
- 다중 행 UPDATE 면 reconcile 이 행 수만큼 돌지만 owner 규모상 무시할 수준입니다. 필요해지면 statement 트리거로 바꾸면 됩니다.

### ⚠️ 트리거에서 advisory lock 을 잡지 마세요

RPC 는 **advisory lock → 행 잠금(`FOR UPDATE`)** 순서입니다. 트리거는 이미 **행 잠금을 쥔 상태**에서 실행되므로 거기서 advisory lock 을 잡으면 **잠금 순서 역전 → 데드락**입니다. 트리거는 잠금 없이 reconcile 만 부르고, 직렬화는 행 잠금에 맡기면 됩니다 — RPC 의 `redesign_owned_member_group` 이 같은 행을 `FOR UPDATE` 하므로 terra-server 의 UPDATE 커밋을 기다립니다. `pet_camera_assignment_open` 부분 UNIQUE 가 최후 방어선입니다.

---

## 2. 배포 전 수정 (B)

### B2 — `pet_camera_assignments.camera_id … ON DELETE RESTRICT` 가 hard delete 를 막습니다

terra-server 의 `DELETE /cameras/{id}` (운영·탈퇴용 hard delete)가 이력 행이 하나라도 있으면 **FK 위반으로 500** 이 됩니다. 앱은 안 부르지만 운영 경로가 깨집니다.

권장: **`ON DELETE CASCADE`**. hard delete 는 "기록까지 삭제" 가 의미이므로 이력도 함께 지우는 게 일관됩니다. RESTRICT 를 유지하시려면 저희가 DELETE 전에 이력을 먼저 지우도록 바꾸겠습니다 — 어느 쪽인지 알려주세요.

### B3 — 새 테이블 3개의 `user_id REFERENCES auth.users(id)` 에 `ON DELETE` 가 없습니다

`pet_camera_assignments` · `redesign_group_requests` · `redesign_group_counters`. 이 상태면 **회원 탈퇴(`auth.users` 삭제)가 FK 위반으로 실패**합니다. 기존 `devices`/`cameras`/`enclosures`/`motion_clips` 는 전부 `ON DELETE CASCADE` 입니다.

권장: 셋 다 `ON DELETE CASCADE`. 참고로 운영 스냅샷 기준 **기존 `pets.user_id` 도 `ON DELETE` 가 없습니다** — 같은 문제가 이미 있으니 이번에 같이 고치시길 권합니다 (앱 팀 소유 테이블이라 판단은 맡깁니다).

### B4 — 해제된 기기가 그룹에 들어갈 수 있습니다

`redesign_owned_member_group` 의 device/camera 분기가 `unlinked_at` 을 안 봅니다. 해제된 카메라 id 로 `save_group` 을 부르면 그룹에 붙습니다.

```sql
-- device / camera 분기에 추가
... WHERE id=p_id AND owner_id=p_owner AND unlinked_at IS NULL FOR UPDATE;
```

`redesign_rename_item_v1` 의 중복 검사도 브랜치 버전에는 `unlinked_at IS NULL` 필터가 없습니다. 9/16 답신에서 "갱신했다" 고 하셨으니 **브랜치에 최신본이 안 올라간 것** 같습니다. 밀어주시면 다시 보겠습니다.

### B5 — 검증기 스키마 스냅샷이 9/15 이전입니다

`test/sql/redesign_base_schema.sql` 에 다음이 없습니다: `devices/cameras.unlinked_at`, `unlink_request_id`, `cameras.clip_stats`, `enclosures.group_number`(초안이 추가하는 컬럼이라 이건 괜찮음). 그래서 **검증기가 해제 상호작용을 전혀 못 봅니다.**

권장: 스냅샷 갱신 + assertion 추가 — "해제된 카메라는 `save_group` 에서 `42501`", "카메라 `enclosure_id=NULL` UPDATE 후 열린 이력이 닫힘(트리거 검증)".

---

## 3. 권장 (N)

- **N1 빈 그룹 자동 정리**: REST `PATCH … {enclosure_id: null}` 과 웹 콘솔은 `redesign_cleanup_empty_groups` 를 안 탑니다. 앱 흐름은 전부 RPC 라 무관하고 웹 콘솔은 운영용이라 그대로 두겠습니다. 트리거에 cleanup 을 얹는 건 부수효과가 커서 권하지 않습니다.
- **N2 `redesign_unlink_device_v1` 스텁 제거**: REST `POST …/unlink` 가 배포됐으니 `0A000` 스텁은 혼선만 줍니다. 제거를 권합니다.
- **N3 이름 저장 정합**: RPC 는 trim 후 저장, REST `POST /enclosures` 는 원본 저장이었습니다. **저희가 REST 도 trim 하도록 맞췄습니다** (이번 배포분). UNIQUE 인덱스가 `btrim(name)` 이라 어느 쪽이든 중복은 잡힙니다.
- **N4 `btrim` 공백 집합 차이**: `redesign_validate_name` 은 유니코드 공백까지 trim, 인덱스의 `btrim(name)` 은 ASCII 공백만. RPC 경유 이름은 이미 trim 돼 저장되므로 실무 차이 없습니다. NBSP 가 섞인 레거시 이름만 예외인데, 개명 안 하기로 했으니 그대로 둡니다.
- **N5 성능**: `reconcile` 이 owner 전체를 매번 재계산합니다. 현재 규모(기기 수 대)에선 무시할 수준이고, 계정당 카메라가 수십 대를 넘으면 그때 봅니다.

### 확인된 것 (변경 불필요)

- `group_number`, `redesign_group_requests`, `unlinked_at` 등 새 컬럼·테이블: terra-server 응답 모델이 `extra="ignore"` 라 영향 없음 (확인)
- `redesign_save_group_v1` 의 `INSERT INTO enclosures(owner_id,name,group_number)`: `species`/`note` nullable, `created_at`/`updated_at` 기본값·트리거 있음 → OK
- `clip_visibility`: `user_hidden_clips.clip_id … ON DELETE CASCADE` 라 `DELETE /clips` 시 자동 정리. 서버 무관
- `redesign_pets` restrictive SELECT policy: service_role 은 RLS 우회하고 서버는 `pets` 를 안 읽음 → 무관
- `redesign_delete_group_v1` 의 `motion_clips.enclosure_id` SET NULL: 클립 소유자는 촬영 시점 값이라 조회 영향 없음 (9/15 회신 §2.2)

---

## 4. terra-server 쪽 대응

| 항목 | 상태 |
|---|---|
| REST `POST/PATCH /enclosures` 이름 trim | ✅ 이번 배포분 |
| `unlink` 가 `enclosure_id=NULL` 로 트리거를 탐 | ✅ 트리거 배포 시 자동 |
| `DELETE /cameras/{id}` 와 B2 | ⏳ 답 주시면 맞춤 (CASCADE 면 변경 없음) |
| 트리거 SQL | ✅ §1 에 제공. 번들 포함·운영 적용은 아래 순서 |

---

## 5. 적용 순서 제안

운영 적용은 **한 사람이 순서대로** 해야 합니다. `devices`/`cameras` 트리거는 저희 테이블이지만 함수는 앱 팀 소유라, **앱 팀이 번들을 확정하면 저희가 Supabase SQL Editor 에서 순서대로 적용하고 `MIGRATIONS_APPLIED.md` 에 기록**하는 방식을 제안합니다.

1. `20260915_assignment_history.sql` (+ B2 결정 반영, + B3 CASCADE)
2. `20260915_redesign_groups.sql` (+ B4, N2)
3. **`20260916_relationship_triggers.sql`** (§1)
4. `20260915_redesign_pets.sql`
5. `20260915_redesign_delete_group.sql`
6. `20260915_clip_visibility.sql`
7. 검증기 스냅샷 갱신(B5) 후 `verify_redesign_sql.py` 재실행 결과 공유 → 운영 적용

각 파일의 `BEGIN … ROLLBACK` 은 검증기용이므로 운영 적용 시 `COMMIT` 으로 바꾸거나 검증기처럼 벗겨서 실행합니다.

---

## 6. 답을 부탁드리는 것

1. **B2**: `camera_id` FK 를 `ON DELETE CASCADE` 로 바꾸실지, RESTRICT 유지(저희가 선삭제)인지
2. **B3**: 새 테이블 3개 + 기존 `pets.user_id` 에 `ON DELETE CASCADE` 적용 여부
3. **B4**: `unlinked_at` 필터 반영본 브랜치 push
4. **§1 트리거** 를 번들에 포함하는 데 이견 없으신지
5. 운영 적용 주체·순서(§5) 동의 여부


---

## 추가 (2026-09-16 저녁) — 앱 회신 `reply-3-sql-drafts` 반영

### 트리거 버그 — 확인, 수정안 채택

지적하신 대로입니다. 제가 드린 한 줄이 틀렸습니다.

```
ERROR:  record "new" has no field "user_id"
```

PL/pgSQL 은 식(expression)을 SQL 로 넘기기 전에 **식에 등장하는 레코드 필드를 전부 파라미터로 먼저 평가**합니다. 그래서 `CASE … THEN NEW.user_id ELSE NEW.owner_id END` 는 어느 분기를 타든 `NEW.user_id` 와 `NEW.owner_id` 를 둘 다 읽고, `cameras` 행에서는 `user_id` 가 없어 실패합니다. 보내주신 수정안이 맞습니다.

```sql
owner := (to_jsonb(NEW) ->> CASE TG_TABLE_NAME WHEN 'pets' THEN 'user_id' ELSE 'owner_id' END)::uuid;
```

같은 효과를 내는 다른 형태로 `IF TG_TABLE_NAME = 'pets' THEN owner := NEW.user_id; ELSE owner := NEW.owner_id; END IF;` 도 있습니다(문장이 분리돼 실행되는 분기의 필드만 평가). 어느 쪽이든 동작은 같으니 **이미 검증 통과한 `to_jsonb` 판 그대로** 가시죠. 위 §1 스니펫도 그렇게 고쳤습니다.

### 답 5건 — 전부 확인

| # | 항목 | 반영 |
|:---:|---|---|
| 1 | B2 `camera_id` FK `ON DELETE CASCADE` | ✅ 서버 변경 없음. hard delete 가 이력까지 지우는 게 의미상 맞습니다 |
| 2 | B3 새 테이블 3개 + 기존 `pets.user_id` CASCADE | ✅ 탈퇴 경로 막힘 해소 |
| 3 | B4 `unlinked_at` 필터 (`owned_member_group` · `rename_item`) | ✅ |
| 4 | §1 트리거 번들 포함 (`20260916_relationship_triggers.sql`) | ✅ + 버그 수정 |
| 5 | 적용 주체·순서 | ✅ 앱 팀 번들 확정 → 저희가 SQL Editor 순서 적용 → `MIGRATIONS_APPLIED.md` 기록 |

N1·N3·N4·N5 동의, N2 스텁 제거 — 확인했습니다. B5 스냅샷 갱신과 신규 assertion(해제 카메라 `42501`, RPC 없이 UPDATE 만으로 이력 열림/닫힘, 해제·툼스톤으로 종료)도 정확히 원하던 검증입니다.

### 다음

**반영본 번들(7개 파일 + assertion 3개)이 브랜치에 올라오면** 그 커밋 기준으로 다시 읽고, `BEGIN … ROLLBACK` 을 벗겨 §5 순서대로 운영에 적용한 뒤, 함수명·오류코드가 초안과 같은지 한 줄로 회신하겠습니다. 브랜치 push 알려주세요.


---

## 운영 적용 완료 (2026-09-16)

`tera-ai-flutter@bb430fa` 번들 6개를 §5 순서대로 Supabase 운영에 적용했습니다. 적용본은 terra-server `migrations/2026-09-16_app_redesign_0[1-6]_*.sql` (원본 그대로, `BEGIN…COMMIT` 래핑), 기록은 `MIGRATIONS_APPLIED.md`.

### 적용 후 확인 결과 (`migrations/2026-09-16_app_redesign_VERIFY.sql`)

| 항목 | 결과 | 판정 |
|---|---|---|
| 함수 | 13개, 시그니처 초안과 동일. `redesign_unlink_device_v1` 없음 | ✅ |
| 트리거 | `trg_cameras_touch_assignments ON cameras` · `trg_pets_touch_assignments ON pets` | ✅ |
| `pets.user_id` FK | `pets_user_id_fkey … ON DELETE CASCADE` **단일** (옛 제약 잔존 없음) | ✅ |
| 새 테이블 | `pet_camera_assignments` · `redesign_group_requests` · `redesign_group_counters` · `user_hidden_clips` | ✅ |
| 새 컬럼 | `pets.deleted_at` · `enclosures.group_number` | ✅ |
| `authenticated` EXECUTE | RPC 6개 전부 `true` | ✅ |

**함수명·시그니처·오류 코드는 초안과 동일합니다.** SQL 을 원본 그대로 적용했으므로 오류 코드(`0A000`/`23505`/`40001`/`42501`/`22023`)도 초안 본문 그대로입니다. 앱은 변경 없이 붙이면 됩니다.

terra-server 쪽 후속 없음 — 응답 모델이 새 컬럼을 무시하고(`extra="ignore"`), `unlink`·`PATCH enclosure_id`·`DELETE /enclosures` 는 트리거로 이력이 자동 갱신됩니다.
