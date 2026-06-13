-- ============================================================
-- Daily Nodes — Migration 003
-- Adds: streaks, group_weekly digest support, invite link
-- Run after 001_initial_schema.sql and 002_storage_buckets.sql
-- ============================================================


-- ============================================================
-- 1. STREAKS
-- One row per user. Updated by the backend every time a node
-- is logged. streak_count = current consecutive days with
-- at least one node (solo or shared). best_streak = all-time max.
-- last_logged_date = the most recent date a node was logged,
-- used by the backend to determine if streak should increment,
-- stay the same, or reset.
-- ============================================================
create table public.streaks (
  id                uuid primary key default uuid_generate_v4(),
  clerk_user_id     text unique not null references public.profiles(clerk_user_id) on delete cascade,
  streak_count      int not null default 0,
  best_streak       int not null default 0,
  last_logged_date  date,                         -- null = never logged
  updated_at        timestamptz not null default now()
);

create index idx_streaks_user on public.streaks(clerk_user_id);

-- Auto-insert a streak row when a profile is created
-- so the backend never has to upsert from scratch
create or replace function public.create_streak_for_profile()
returns trigger language plpgsql as $$
begin
  insert into public.streaks (clerk_user_id)
  values (new.clerk_user_id)
  on conflict (clerk_user_id) do nothing;
  return new;
end;
$$;

create trigger trg_create_streak
  after insert on public.profiles
  for each row execute procedure public.create_streak_for_profile();

-- RLS: owner only
alter table public.streaks enable row level security;

create policy "streaks: owner only"
  on public.streaks for all
  using (clerk_user_id = current_setting('app.clerk_user_id', true));

-- moddatetime trigger
create trigger handle_streaks_updated_at
  before update on public.streaks
  for each row execute procedure moddatetime(updated_at);


-- ============================================================
-- 2. GROUP WEEKLY DIGEST SUPPORT
-- Reuses the existing digests table. Adds 'group_weekly' to
-- the digest_period enum and a group_id column so we can store
-- one group summary per group per week, separate from personal
-- digests.
-- group_id is nullable — personal digests leave it null.
-- ============================================================
alter type digest_period add value if not exists 'group_weekly';

alter table public.digests
  add column if not exists group_id uuid references public.groups(id) on delete cascade;

-- A group_weekly digest is unique per group per week, not per user.
-- Personal digests keep their existing unique constraint.
-- Add a partial unique index for group digests.
create unique index if not exists idx_digests_group_weekly
  on public.digests (group_id, period, date_start)
  where period = 'group_weekly';

-- Group weekly digests also store aggregate stats in the
-- existing suggestions jsonb column. Structure will be:
-- {
--   "total_nodes": 47,
--   "most_active_day": "Tuesday",
--   "most_active_member": "Armaan",
--   "member_streaks": [{"name": "Jamie", "streak": 14}, ...],
--   "node_type_breakdown": {"workout": 12, "food": 8, ...}
-- }
-- No schema change needed — jsonb handles this already.

-- Update RLS: group members can read their group's weekly digest
create policy "digests: group members read group_weekly"
  on public.digests for select
  using (
    period = 'group_weekly'
    and group_id in (
      select group_id from public.group_members
      where clerk_user_id = current_setting('app.clerk_user_id', true)
    )
  );


-- ============================================================
-- 3. INVITE LINK TOKEN
-- The existing invite_code is 8 chars — good for typing.
-- For mobile sharing we add a full invite_token (UUID-based)
-- that maps to a shareable URL:
-- https://yourdomain.com/join/{invite_token}
-- Both codes resolve to the same group.
-- ============================================================
alter table public.groups
  add column if not exists invite_token text unique default gen_random_uuid()::text;

create index if not exists idx_groups_invite_token on public.groups(invite_token);

-- ============================================================
-- 4. BACKFILL invite_token for any existing groups
-- (safe to run even if table is empty)
-- ============================================================
update public.groups
  set invite_token = gen_random_uuid()::text
  where invite_token is null;