import re
from app.services.supabase_client import get_supabase

def _slugify(name: str) -> str:
    """Turn a display name into a safe username slug."""
    slug = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", ""))
    return slug or "user"

def _make_unique_username(base: str, sb) -> str:
    """Append a counter suffix until the username is unique in profiles."""
    username = base[:30]  # keep it short
    candidate = username
    i = 1
    while True:
        existing = (
            sb.table("profiles")
            .select("id")
            .eq("username", candidate)
            .execute()
        )
        if not existing.data:
            return candidate
        candidate = f"{username}{i}"
        i += 1

def get_or_create_profile(clerk_user_id: str, jwt_payload: dict = None, sb = None) -> dict:
    """
    Safely retrieves a user's profile from Supabase.
    If it does not exist, it lazily creates a default one using claims from the Clerk JWT.
    """
    if sb is None:
        sb = get_supabase()

    # Query without using .single() to avoid raising APIError on 0 rows
    result = sb.table("profiles").select("*").eq("clerk_user_id", clerk_user_id).execute()
    if result.data:
        return result.data[0]

    # Profile does not exist, initialize one
    payload = jwt_payload or {}

    first = payload.get("given_name") or payload.get("first_name") or ""
    last = payload.get("family_name") or payload.get("last_name") or ""
    display_name = f"{first} {last}".strip()
    if not display_name:
        display_name = payload.get("name") or payload.get("email") or payload.get("username") or "User"

    username_val = payload.get("username") or payload.get("email", "").split("@")[0] or f"user_{clerk_user_id.replace('user_', '')[:10]}"
    username_val = _slugify(username_val)

    username = _make_unique_username(username_val, sb)

    avatar_url = payload.get("picture") or payload.get("image_url") or payload.get("profile_image_url")

    profile_row = {
        "clerk_user_id": clerk_user_id,
        "username": username,
        "display_name": display_name,
        "avatar_url": avatar_url,
    }

    # Upsert to database (safe against concurrent creation)
    insert_res = sb.table("profiles").upsert(profile_row, on_conflict="clerk_user_id").execute()
    if insert_res.data:
        return insert_res.data[0]

    return profile_row
