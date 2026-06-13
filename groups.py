from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.services.supabase_client import get_supabase
from app.services.profile_service import get_or_create_profile
from app.services.group_digest_service import generate_group_weekly_digest
from app.config import settings
import uuid
import random
import string

router = APIRouter()


class GroupCreate(BaseModel):
    name: str


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    bio: Optional[str] = None


@router.post("/")
async def create_group(body: GroupCreate, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    jwt_payload = getattr(request.state, "jwt_payload", None)
    profile = get_or_create_profile(uid, jwt_payload, sb)
    if profile.get("group_id"):
        raise HTTPException(400, "You are already in a group. Leave it first.")

    group = sb.table("groups").insert({
        "name": body.name,
        "created_by": uid,
    }).execute().data[0]

    sb.table("group_members").insert({
        "group_id": group["id"],
        "clerk_user_id": uid,
        "role": "owner",
    }).execute()

    sb.table("profiles").update({"group_id": group["id"]}).eq("clerk_user_id", uid).execute()

    return _with_invite_link(group)


@router.post("/join")
async def join_group(request: Request, invite_code: str = None, invite_token: str = None):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    if not invite_code and not invite_token:
        raise HTTPException(400, "Provide either invite_code or invite_token.")

    jwt_payload = getattr(request.state, "jwt_payload", None)
    profile = get_or_create_profile(uid, jwt_payload, sb)
    if profile.get("group_id"):
        raise HTTPException(400, "You are already in a group. Leave it first.")

    if invite_token:
        group_res = sb.table("groups").select("*").eq("invite_token", invite_token).execute()
    else:
        group_res = sb.table("groups").select("*").eq("invite_code", invite_code.upper()).execute()

    if not group_res.data:
        raise HTTPException(404, "Invalid invite code or link.")

    group = group_res.data[0]
    members = sb.table("group_members").select("id").eq("group_id", group["id"]).execute()
    if len(members.data) >= group["max_members"]:
        raise HTTPException(400, "Group is full.")

    sb.table("group_members").insert({
        "group_id": group["id"],
        "clerk_user_id": uid,
        "role": "member",
    }).execute()

    sb.table("profiles").update({"group_id": group["id"]}).eq("clerk_user_id", uid).execute()

    return _with_invite_link(group)


def _sign_group_members_avatars(members_list: list) -> list:
    """Batch generates signed URLs for all group member profile avatars."""
    if not members_list:
        return members_list

    paths = []
    members_to_sign = []
    for m in members_list:
        profile = m.get("profiles")
        if profile and profile.get("avatar_url"):
            avatar_url = profile["avatar_url"]
            if not (avatar_url.startswith("http://") or avatar_url.startswith("https://")):
                paths.append(avatar_url)
                members_to_sign.append(m)

    if not members_to_sign:
        return members_list

    sb = get_supabase()
    try:
        signed_res = sb.storage.from_("avatars").create_signed_urls(paths, 3600)
        url_map = {item["path"]: item["signedURL"] for item in signed_res if "signedURL" in item}
        for m in members_to_sign:
            profile = m["profiles"]
            avatar_url = profile["avatar_url"]
            profile["avatar_url"] = url_map.get(avatar_url) or avatar_url
    except Exception as e:
        print("Error batch signing group member avatars:", e)
        for m in members_to_sign:
            profile = m["profiles"]
            avatar_url = profile["avatar_url"]
            try:
                signed = sb.storage.from_("avatars").create_signed_url(avatar_url, 3600)
                profile["avatar_url"] = signed.get("signedURL") or avatar_url
            except Exception:
                pass
    return members_list


@router.get("/my-group")
@router.get("/me")
async def get_my_group(request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()
    jwt_payload = getattr(request.state, "jwt_payload", None)

    profile = get_or_create_profile(uid, jwt_payload, sb)
    group_id = profile.get("group_id")
    if not group_id:
        raise HTTPException(404, "You are not in a group.")

    group_res = sb.table("groups").select("*").eq("id", group_id).execute()
    if not group_res.data:
        raise HTTPException(404, "Group not found.")
    group = group_res.data[0]
    members = (
        sb.table("group_members")
        .select("*, profiles(display_name, avatar_url, username)")
        .eq("group_id", group_id)
        .execute()
    )

    member_ids = [m["clerk_user_id"] for m in members.data]
    streaks = sb.table("streaks").select("clerk_user_id, streak_count, best_streak").in_("clerk_user_id", member_ids).execute()
    streak_map = {s["clerk_user_id"]: s for s in (streaks.data or [])}

    members_with_streaks = []
    for m in members.data:
        m["streak"] = streak_map.get(m["clerk_user_id"], {"streak_count": 0, "best_streak": 0})
        members_with_streaks.append(m)

    signed_members = _sign_group_members_avatars(members_with_streaks)

    return {**_with_invite_link(group), "members": signed_members}


@router.post("/{group_id}/regenerate-invite")
async def regenerate_invite(group_id: str, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    # Verify the user is the owner/admin of the group
    member_res = sb.table("group_members").select("role").eq("group_id", group_id).eq("clerk_user_id", uid).execute()
    if not member_res.data or member_res.data[0].get("role") != "owner":
        raise HTTPException(403, "Only group owner can regenerate invite code.")

    # Generate a new 8-char uppercase invite code
    new_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    # Generate a new UUID invite token
    new_token = str(uuid.uuid4())

    updated = sb.table("groups").update({
        "invite_code": new_code,
        "invite_token": new_token,
    }).eq("id", group_id).execute()

    if not updated.data:
        raise HTTPException(404, "Group not found.")

    return _with_invite_link(updated.data[0])


@router.patch("/{group_id}")
async def update_group(group_id: str, body: GroupUpdate, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    # Verify the user is the owner/admin of the group
    member_res = sb.table("group_members").select("role").eq("group_id", group_id).eq("clerk_user_id", uid).execute()
    if not member_res.data or member_res.data[0].get("role") != "owner":
        raise HTTPException(403, "Only the group owner can update group settings.")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        # Nothing to update, return the group
        group_res = sb.table("groups").select("*").eq("id", group_id).execute()
        if not group_res.data:
            raise HTTPException(404, "Group not found.")
        return _with_invite_link(group_res.data[0])

    updated = sb.table("groups").update(updates).eq("id", group_id).execute()
    if not updated.data:
        raise HTTPException(404, "Group not found.")

    return _with_invite_link(updated.data[0])


@router.post("/{group_id}/kick/{member_clerk_id}")
async def kick_member(group_id: str, member_clerk_id: str, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    # Verify the user is the owner of the group
    member_res = sb.table("group_members").select("role").eq("group_id", group_id).eq("clerk_user_id", uid).execute()
    if not member_res.data or member_res.data[0].get("role") != "owner":
        raise HTTPException(403, "Only the group owner can kick members.")

    if member_clerk_id == uid:
        raise HTTPException(400, "You cannot kick yourself.")

    # Remove member from group_members
    sb.table("group_members").delete().eq("group_id", group_id).eq("clerk_user_id", member_clerk_id).execute()
    # Reset user's group_id in profiles
    sb.table("profiles").update({"group_id": None}).eq("clerk_user_id", member_clerk_id).execute()

    return {"kicked": member_clerk_id}



@router.delete("/me/leave")
async def leave_group(request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    jwt_payload = getattr(request.state, "jwt_payload", None)
    profile = get_or_create_profile(uid, jwt_payload, sb)
    group_id = profile.get("group_id")
    if not group_id:
        raise HTTPException(404, "You are not in a group.")

    # Verify membership and check if user is the owner
    member_res = sb.table("group_members").select("role").eq("group_id", group_id).eq("clerk_user_id", uid).execute()
    if not member_res.data:
        raise HTTPException(404, "You are not a member of this group.")

    role = member_res.data[0].get("role")
    if role == "owner":
        # Owner cannot leave if there are other members
        members = sb.table("group_members").select("clerk_user_id").eq("group_id", group_id).execute()
        if len(members.data) > 1:
            raise HTTPException(
                400,
                "As the group owner, you cannot leave the group until all other members have left or been kicked."
            )

    sb.table("group_members").delete().eq("clerk_user_id", uid).execute()
    sb.table("profiles").update({"group_id": None}).eq("clerk_user_id", uid).execute()

    remaining = sb.table("group_members").select("id").eq("group_id", group_id).execute()
    if not remaining.data:
        sb.table("groups").delete().eq("id", group_id).execute()

    return {"left": group_id}


@router.get("/me/digest/weekly")
async def get_group_weekly_digest(request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    jwt_payload = getattr(request.state, "jwt_payload", None)
    profile = get_or_create_profile(uid, jwt_payload, sb)
    group_id = profile.get("group_id")
    if not group_id:
        raise HTTPException(404, "You are not in a group.")

    result = (
        sb.table("digests")
        .select("*")
        .eq("group_id", group_id)
        .eq("period", "group_weekly")
        .eq("status", "generated")
        .order("date_start", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(404, "No group digest yet.")
    return result.data[0]


@router.post("/me/digest/weekly/trigger")
async def trigger_group_weekly_digest(request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    jwt_payload = getattr(request.state, "jwt_payload", None)
    profile = get_or_create_profile(uid, jwt_payload, sb)
    group_id = profile.get("group_id")
    if not group_id:
        raise HTTPException(404, "You are not in a group.")

    result = await generate_group_weekly_digest(group_id)
    return result


def _with_invite_link(group: dict) -> dict:
    base_url = settings.FRONTEND_URL.rstrip("/")
    group["invite_link"] = f"{base_url}/join/{group.get('invite_token', '')}"
    return group