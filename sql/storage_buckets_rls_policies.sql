-- ============================================================
-- Daily Nodes — Storage Buckets
-- Run this after 001_initial_schema.sql
-- ============================================================

-- Avatars bucket (user profile photos)
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'avatars',
  'avatars',
  false,                          -- private; served via signed URLs from FastAPI
  2097152,                        -- 2 MB limit
  array['image/jpeg', 'image/png', 'image/webp']
);

-- Node images bucket (images attached to node posts)
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'node-images',
  'node-images',
  false,                          -- private; signed URLs only
  10485760,                       -- 10 MB limit
  array['image/jpeg', 'image/png', 'image/webp', 'image/gif']
);

-- ============================================================
-- Storage RLS Policies
-- FastAPI uses service role so bypasses these.
-- Policies below protect against any direct bucket access.
-- ============================================================

-- AVATARS: user can only read/write their own folder
-- Path convention: avatars/{clerk_user_id}/avatar.{ext}

create policy "avatars: owner read"
  on storage.objects for select
  using (
    bucket_id = 'avatars'
    and (storage.foldername(name))[1] = current_setting('app.clerk_user_id', true)
  );

create policy "avatars: owner write"
  on storage.objects for insert
  with check (
    bucket_id = 'avatars'
    and (storage.foldername(name))[1] = current_setting('app.clerk_user_id', true)
  );

create policy "avatars: owner delete"
  on storage.objects for delete
  using (
    bucket_id = 'avatars'
    and (storage.foldername(name))[1] = current_setting('app.clerk_user_id', true)
  );

-- NODE IMAGES: owner can write; group members can read shared node images
-- Path convention: node-images/{clerk_user_id}/{node_id}.{ext}

create policy "node-images: owner write"
  on storage.objects for insert
  with check (
    bucket_id = 'node-images'
    and (storage.foldername(name))[1] = current_setting('app.clerk_user_id', true)
  );

create policy "node-images: owner delete"
  on storage.objects for delete
  using (
    bucket_id = 'node-images'
    and (storage.foldername(name))[1] = current_setting('app.clerk_user_id', true)
  );

-- Node image reads are handled by FastAPI generating signed URLs.
-- No direct public read policy here — keeps images private by default.