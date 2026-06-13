-- Drop all the current_setting policies and replace with this:
-- Service role bypasses RLS automatically.
-- These just block any direct anon/client access.

create policy "block anon: profiles"
  on public.profiles for all
  using (false);

create policy "block anon: groups"
  on public.groups for all
  using (false);

create policy "block anon: group_members"
  on public.group_members for all
  using (false);

create policy "block anon: nodes"
  on public.nodes for all
  using (false);

create policy "block anon: node_reactions"
  on public.node_reactions for all
  using (false);

create policy "block anon: digests"
  on public.digests for all
  using (false);