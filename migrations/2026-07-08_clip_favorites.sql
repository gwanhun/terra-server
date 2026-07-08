-- =====================================================================
-- terra-server — 클립 즐겨찾기 (clip_favorites) (2026-07-08)
-- 적용 방법: Supabase 대시보드 > SQL Editor 에서 통째로 실행
--
-- 배경: 앱의 "영상 즐겨찾기"가 지금은 로컬(Hive) 전용 → 재설치/기기변경 시 소실.
--       계정 단위로 클라우드 동기화하기 위한 테이블.
--
-- 설계 메모:
--   - CRUD 는 앱이 Supabase 직결(RLS)로 처리 → 별도 terra-api 엔드포인트 불필요.
--   - clip_id 는 느슨한 참조(FK 안 검). 이유: 클립 실체가 motion_clips / camera_clips
--     두 계열로 나뉘어 있어(2026-07 기준) 한쪽에 FK 를 걸면 다른 쪽 즐겨찾기가 막힘.
--     클립이 R2 lifecycle 로 삭제돼도 즐겨찾기 행은 남아 "복원 대상" 표식이 됨.
--   - R2 보존정책(즐겨찾기 클립 만료삭제 제외)은 별도 작업 — 이 마이그레이션 범위 밖.
--     현재 보존은 R2 대시보드 lifecycle rule(prefix 단위)이라 개별 제외 불가 → 재설계 필요.
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.clip_favorites (
    owner_id    UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    clip_id     UUID        NOT NULL,          -- motion_clips.id 또는 camera_clips.id (느슨한 참조)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, clip_id)
);

-- 본인 즐겨찾기 목록 최신순 조회용
CREATE INDEX IF NOT EXISTS idx_clip_favorites_owner
    ON public.clip_favorites(owner_id, created_at DESC);


-- =====================================================================
-- RLS — 본인 즐겨찾기만 보고/추가/삭제 (UPDATE 는 의미 없어 미개방)
-- =====================================================================
ALTER TABLE public.clip_favorites ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own favorites select" ON public.clip_favorites
    FOR SELECT USING (owner_id = auth.uid());

CREATE POLICY "own favorites insert" ON public.clip_favorites
    FOR INSERT WITH CHECK (owner_id = auth.uid());

CREATE POLICY "own favorites delete" ON public.clip_favorites
    FOR DELETE USING (owner_id = auth.uid());
