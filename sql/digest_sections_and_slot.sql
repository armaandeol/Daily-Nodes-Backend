-- ============================================================
-- Daily Nodes — Migration 005
-- Structured digest sections for the newspaper-style front page,
-- plus persisting which slot (morning/night) a digest belongs to
-- so readers (API, frontend) don't have to re-derive it.
-- ============================================================

-- sections: structured JSON produced by generate_digest() — headline,
-- tapestry, circle_headline, circle, pivot, pull_quote, has_circle,
-- photo, circle_photo, stats. Nullable so legacy rows (generated before
-- this migration) fall back to parsing `content` at read time via
-- app.services.digest_sections.get_sections().
alter table public.digests
  add column if not exists sections jsonb;

-- slot: 'morning' or 'night'. Was previously only a transient argument
-- to generate_digest(), never persisted — the frontend needs it to pick
-- the right section-title wording for a given digest.
alter table public.digests
  add column if not exists slot text;
