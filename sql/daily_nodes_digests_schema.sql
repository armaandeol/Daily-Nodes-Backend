-- ============================================================
-- Daily Nodes — Initial Schema
-- Run this in Supabase SQL Editor
-- Auth is handled by Clerk. Supabase only stores app data.
-- The clerk_user_id (text) is the foreign key throughout.
-- ============================================================

-- Extensions
create extension if not exists "uuid-ossp";
create extension if not exists "pg_cron";        -- for digest scheduling
create extension if not exists "moddatetime";    -- auto-update updated_at

-- ============================================================
-- PROFILES
-- Created via Clerk webhook on user.created event
-- ============================================================
create table public.profiles (
  id              uuid primary key default uuid_generate_v4(),
  clerk_user_id   text unique not null,           -- Clerk's user ID, the join key
  username        text unique not null,
  display_name    text not null,
  avatar_url      text,                           -- points to storage bucket or external URL
  bio             text,
  timezone        text not null default 'Asia/Kolkata',
  digest_time     time not null default '22:00',  -- user-chosen daily digest time
  group_id        uuid,                           -- FK added after groups table exists
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- ============================================================
-- GROUPS
-- A circle of 2–5 users sharing activity
-- ============================================================
create table public.groups (
  id              uuid primary key default uuid_generate_v4(),
  name            text not null,
  bio             text,
  invite_code     text unique not null default upper(substr(md5(random()::text), 1, 8)),
  max_members     int not null default 5,
  created_by      text not null references public.profiles(clerk_user_id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- Back-fill FK on profiles now that groups exists
alter table public.profiles
  add constraint fk_profiles_group
  foreign key (group_id) references public.groups(id) on delete set null;

-- ============================================================
-- GROUP MEMBERS (join table)
-- Enforces one group per user at the API layer too
-- ============================================================
create table public.group_members (
  id              uuid primary key default uuid_generate_v4(),
  group_id        uuid not null references public.groups(id) on delete cascade,
  clerk_user_id   text not null references public.profiles(clerk_user_id) on delete cascade,
  role            text not null default 'member' check (role in ('owner', 'member')),
  joined_at       timestamptz not null default now(),
  unique (clerk_user_id)                          -- one group per user, enforced at DB level
);

-- ============================================================
-- NODES
-- Core activity log. visibility = solo | shared
-- image_url is optional, points to storage bucket
-- title and content are both optional (image-only nodes allowed)
-- ============================================================
create type node_visibility as enum ('solo', 'shared');
create type node_type as enum ('workout', 'food', 'mood', 'study', 'social', 'custom');

create table public.nodes (
  id              uuid primary key default uuid_generate_v4(),
  clerk_user_id   text not null references public.profiles(clerk_user_id) on delete cascade,
  group_id        uuid references public.groups(id) on delete set null,
  type            node_type not null default 'custom',
  visibility      node_visibility not null default 'solo',
  title           text,                           -- optional
  content         text,                           -- optional
  image_url       text,                           -- optional, storage path
  metadata        jsonb default '{}',             -- type-specific extras (reps, calories, mood score…)
  logged_at       timestamptz not null default now(),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  -- at least one of title, content, or image_url must be present
  constraint node_has_content check (
    title is not null or content is not null or image_url is not null
  ),
  -- shared nodes must belong to a group
  constraint shared_needs_group check (
    visibility = 'solo' or (visibility = 'shared' and group_id is not null)
  )
);

create index idx_nodes_user_logged    on public.nodes(clerk_user_id, logged_at desc);
create index idx_nodes_group_shared   on public.nodes(group_id, visibility, logged_at desc);
create index idx_nodes_logged_at      on public.nodes(logged_at desc);

-- ============================================================
-- NODE REACTIONS
-- Lightweight emoji reactions on shared nodes
-- ============================================================
create table public.node_reactions (
  id              uuid primary key default uuid_generate_v4(),
  node_id         uuid not null references public.nodes(id) on delete cascade,
  clerk_user_id   text not null references public.profiles(clerk_user_id) on delete cascade,
  emoji           text not null,
  created_at      timestamptz not null default now(),
  unique (node_id, clerk_user_id, emoji)          -- one of each emoji per user per node
);

-- ============================================================
-- DIGESTS
-- AI-generated daily and weekly recaps, stored per user
-- status: pending → generated | failed
-- ============================================================
create type digest_period as enum ('daily', 'weekly');
create type digest_status as enum ('pending', 'generated', 'failed');

create table public.digests (
  id              uuid primary key default uuid_generate_v4(),
  clerk_user_id   text not null references public.profiles(clerk_user_id) on delete cascade,
  period          digest_period not null,
  date_start      date not null,
  date_end        date not null,
  content         text,                           -- full generated digest text
  suggestions     jsonb default '[]',             -- array of suggestion objects
  status          digest_status not null default 'pending',
  generated_at    timestamptz,
  created_at      timestamptz not null default now(),

  unique (clerk_user_id, period, date_start)      -- one digest per user per period per day
);

create index idx_digests_user_date on public.digests(clerk_user_id, date_start desc);

-- ============================================================
-- AUTO-UPDATE updated_at via moddatetime trigger
-- ============================================================
create trigger handle_profiles_updated_at
  before update on public.profiles
  for each row execute procedure moddatetime(updated_at);

create trigger handle_groups_updated_at
  before update on public.groups
  for each row execute procedure moddatetime(updated_at);

create trigger handle_nodes_updated_at
  before update on public.nodes
  for each row execute procedure moddatetime(updated_at);

-- ============================================================
-- ROW LEVEL SECURITY
-- FastAPI uses the service role key, so RLS is a safety net.
-- Direct DB access (Supabase dashboard, psql) is still locked.
-- ============================================================
alter table public.profiles      enable row level security;
alter table public.groups        enable row level security;
alter table public.group_members enable row level security;
alter table public.nodes         enable row level security;
alter table public.node_reactions enable row level security;
alter table public.digests       enable row level security;

-- Service role bypasses RLS automatically (Supabase default).
-- These policies apply to any anon/authenticated direct access.

-- profiles: only visible to the owner
create policy "profiles: owner only"
  on public.profiles for all
  using (clerk_user_id = current_setting('app.clerk_user_id', true));

-- groups: visible to members only
create policy "groups: members only"
  on public.groups for select
  using (
    id in (
      select group_id from public.group_members
      where clerk_user_id = current_setting('app.clerk_user_id', true)
    )
  );

-- group_members: readable by group members
create policy "group_members: members only"
  on public.group_members for select
  using (
    group_id in (
      select group_id from public.group_members
      where clerk_user_id = current_setting('app.clerk_user_id', true)
    )
  );

-- nodes: solo nodes only to owner; shared nodes to group members
create policy "nodes: solo owner only"
  on public.nodes for select
  using (
    (visibility = 'solo' and clerk_user_id = current_setting('app.clerk_user_id', true))
    or
    (visibility = 'shared' and group_id in (
      select group_id from public.group_members
      where clerk_user_id = current_setting('app.clerk_user_id', true)
    ))
  );

create policy "nodes: owner insert/update/delete"
  on public.nodes for all
  using (clerk_user_id = current_setting('app.clerk_user_id', true));

-- reactions: readable by group members, writable by owner
create policy "reactions: group members read"
  on public.node_reactions for select
  using (
    node_id in (
      select id from public.nodes where group_id in (
        select group_id from public.group_members
        where clerk_user_id = current_setting('app.clerk_user_id', true)
      )
    )
  );

create policy "reactions: owner write"
  on public.node_reactions for all
  using (clerk_user_id = current_setting('app.clerk_user_id', true));

-- digests: owner only
create policy "digests: owner only"
  on public.digests for all
  using (clerk_user_id = current_setting('app.clerk_user_id', true));