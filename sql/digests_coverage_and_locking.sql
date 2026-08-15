-- ============================================================
-- Daily Nodes — Migration 004
-- Digest scheduler hardening: rolling coverage window + a
-- DB-backed distributed lock so multiple app instances never
-- generate the same digest twice.
-- ============================================================

-- covered_until: the exact timestamp this digest's node query ran up to.
-- The next digest for the same user/period resumes from here instead of
-- a fixed calendar-day boundary, so nodes logged after the digest was
-- generated (e.g. after 10pm for a night digest) are never skipped.
alter table public.digests
  add column if not exists covered_until timestamptz;

-- claimed_at: set when a worker claims this row for processing.
-- Used to detect and reclaim rows stuck in 'pending' because the
-- worker that claimed them crashed or was killed mid-generation.
alter table public.digests
  add column if not exists claimed_at timestamptz;

create index if not exists idx_digests_covered_until
  on public.digests (clerk_user_id, period, covered_until desc);

-- ------------------------------------------------------------
-- Note on digest_slot / digest_enabled:
-- These columns already exist on public.profiles in production
-- (added ad hoc via the Supabase dashboard) but were never
-- captured in a tracked migration. Documenting them here so the
-- schema history matches reality; safe to run — no-ops if present.
-- ------------------------------------------------------------
alter table public.profiles
  add column if not exists digest_slot text not null default 'night'
    check (digest_slot in ('morning', 'night'));

alter table public.profiles
  add column if not exists digest_enabled boolean not null default true;
