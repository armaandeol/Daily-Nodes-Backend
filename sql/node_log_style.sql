-- ============================================================
-- Daily Nodes — Migration 004
-- Adds: nodes.style (the paper/visual style picked in the composer)
-- Run after 003_user_streaks_group_weekly_digests_invite_tokens.sql
-- ============================================================

alter table public.nodes
  add column style text not null default 'postit';

alter table public.nodes
  add constraint nodes_style_valid check (
    style in ('postit', 'lined', 'index', 'scrap')
  );
