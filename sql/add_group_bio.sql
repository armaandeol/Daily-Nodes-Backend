-- Add bio column to groups table
ALTER TABLE public.groups ADD COLUMN IF NOT EXISTS bio text;
