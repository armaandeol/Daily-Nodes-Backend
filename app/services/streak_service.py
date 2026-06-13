from datetime import date, timedelta
from app.services.supabase_client import get_supabase


async def update_streak(clerk_user_id: str, logged_date: date = None) -> dict:
    """
    Call this every time a node is created.
    Logic:
    - Same day as last_logged_date → no change (already counted today)
    - Previous day → increment streak
    - Any earlier → reset to 1
    - Never logged before → set to 1
    """
    sb = get_supabase()
    today = logged_date or date.today()

    result = sb.table("streaks").select("*").eq("clerk_user_id", clerk_user_id).execute()

    # Should always exist due to trigger, but guard anyway
    if not result.data:
        sb.table("streaks").insert({"clerk_user_id": clerk_user_id}).execute()
        result = sb.table("streaks").select("*").eq("clerk_user_id", clerk_user_id).execute()

    streak = result.data[0]
    last_date = date.fromisoformat(streak["last_logged_date"]) if streak["last_logged_date"] else None
    current = streak["streak_count"]
    best = streak["best_streak"]

    if last_date == today:
        # Already logged today, no update needed
        return streak

    if last_date == today - timedelta(days=1):
        # Consecutive day — increment
        current += 1
    else:
        # Gap — reset
        current = 1

    best = max(best, current)

    updated = sb.table("streaks").update({
        "streak_count": current,
        "best_streak": best,
        "last_logged_date": today.isoformat(),
    }).eq("clerk_user_id", clerk_user_id).execute()

    return updated.data[0]


async def get_streak(clerk_user_id: str) -> dict:
    sb = get_supabase()
    result = sb.table("streaks").select("*").eq("clerk_user_id", clerk_user_id).execute()
    if not result.data:
        return {"streak_count": 0, "best_streak": 0, "last_logged_date": None}

    # Auto-reset if user hasn't logged yesterday or today
    streak = result.data[0]
    if streak["last_logged_date"]:
        last_date = date.fromisoformat(streak["last_logged_date"])
        today = date.today()
        if last_date < today - timedelta(days=1):
            # Streak is broken — reset in DB
            sb.table("streaks").update({
                "streak_count": 0,
            }).eq("clerk_user_id", clerk_user_id).execute()
            streak["streak_count"] = 0

    return streak
