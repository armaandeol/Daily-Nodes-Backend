from fastapi import APIRouter, Request, HTTPException, Query
from typing import Optional
from app.services.supabase_client import get_supabase
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/my")
async def get_my_digests(
    request: Request,
    period: Optional[str] = Query(None, description="Filter by period: daily or group_weekly"),
    limit: int = Query(10, ge=1, le=50)
):
    """
    Get digests for the authenticated user.

    - **period**: Filter by "daily" or "group_weekly"
    - **limit**: Number of digests to return (default 10, max 50)
    """
    uid = request.state.clerk_user_id
    sb = get_supabase()

    query = sb.table("digests").select("*").eq("clerk_user_id", uid)

    if period:
        query = query.eq("period", period)

    result = query.order("date_start", desc=True).limit(limit).execute()

    return {
        "digests": result.data,
        "count": len(result.data)
    }


@router.get("/group/{group_id}")
async def get_group_digests(
    group_id: str,
    request: Request,
    limit: int = Query(10, ge=1, le=50)
):
    """
    Get weekly digests for a specific group.
    User must be a member of the group.
    """
    uid = request.state.clerk_user_id
    sb = get_supabase()

    # Verify user is member of this group
    member = sb.table("group_members").select("*").eq("group_id", group_id).eq("clerk_user_id", uid).execute()
    if not member.data:
        raise HTTPException(403, "You must be a member of this group to view its digests")

    result = (
        sb.table("digests")
        .select("*")
        .eq("group_id", group_id)
        .eq("period", "group_weekly")
        .order("date_start", desc=True)
        .limit(limit)
        .execute()
    )

    return {
        "digests": result.data,
        "count": len(result.data),
        "group_id": group_id
    }


@router.get("/{digest_id}")
async def get_digest(digest_id: str, request: Request):
    """
    Get a specific digest by ID.
    User must have access (own digest or group member for group digests).
    """
    uid = request.state.clerk_user_id
    sb = get_supabase()

    digest = sb.table("digests").select("*").eq("id", digest_id).single().execute()
    if not digest.data:
        raise HTTPException(404, "Digest not found")

    digest_data = digest.data

    # If it's the user's own digest, allow access
    if digest_data.get("clerk_user_id") == uid:
        return digest_data

    # If it's a group digest, check membership
    if digest_data.get("group_id"):
        member = sb.table("group_members").select("*").eq("group_id", digest_data["group_id"]).eq("clerk_user_id", uid).execute()
        if member.data:
            return digest_data

    raise HTTPException(403, "Access denied")
