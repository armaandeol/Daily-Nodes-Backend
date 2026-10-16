-- ============================================================
-- Fix: groups.created_by had `not null` + `on delete set null`,
-- a self-contradictory pair — deleting a group's creator's
-- profile violated the not-null constraint and aborted the
-- whole delete (this is what broke manual test-profile cleanup).
--
-- Fix: deleting the creator's profile now cascades to delete the
-- group itself. group_members/nodes/digests tied to that group
-- already cascade/set-null from profiles or groups, so a single
-- `delete from profiles where clerk_user_id = ...` now cleans up
-- everything for a solo test profile with no extra steps.
--
-- Non-owner members are unaffected: group_members already cascades
-- on their profile delete, so they're just removed from the group
-- while it keeps existing for the rest.
-- ============================================================

alter table public.groups
  drop constraint if exists groups_created_by_fkey;

alter table public.groups
  add constraint groups_created_by_fkey
  foreign key (created_by) references public.profiles(clerk_user_id)
  on delete cascade;
