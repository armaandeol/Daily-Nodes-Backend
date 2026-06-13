CREATE INDEX IF NOT EXISTS idx_nodes_user_logged_at 
ON nodes (clerk_user_id, logged_at DESC);