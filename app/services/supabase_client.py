from functools import lru_cache
from supabase import create_client, Client
from app.config import settings


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Return a singleton Supabase client using the service role key.
    The service role key bypasses RLS — all auth is enforced at the API layer."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
