from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from pydantic import BaseModel, field_validator
from typing import Optional
import uuid
import mimetypes
from app.services.supabase_client import get_supabase
from app.services.profile_service import get_or_create_profile

router = APIRouter()


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    username: Optional[str] = None
    bio: Optional[str] = None
    digest_slot: Optional[str] = None
    digest_enabled: Optional[bool] = None  # opt out of the daily digest entirely

    @field_validator("digest_slot")
    @classmethod
    def validate_digest_slot(cls, v: Optional[str]):
        if v is not None and v not in ("morning", "night"):
            raise ValueError("digest_slot must be either 'morning' or 'night'")
        return v


def _sign_avatar_url(profile: dict) -> dict:
    if not profile or not profile.get("avatar_url"):
        return profile

    avatar_url = profile["avatar_url"]
    # If it is a full URL, it is external (e.g. Clerk). Keep it as-is.
    if avatar_url.startswith("http://") or avatar_url.startswith("https://"):
        profile["avatar_signed_url"] = avatar_url
        return profile

    # Otherwise, it's a storage path inside our 'avatars' bucket.
    sb = get_supabase()
    try:
        signed = sb.storage.from_("avatars").create_signed_url(avatar_url, 3600)
        profile["avatar_signed_url"] = signed.get("signedURL")
    except Exception as e:
        print(f"Error signing avatar url {avatar_url}: {e}")
        profile["avatar_signed_url"] = None
    return profile


@router.get("/me")
async def get_profile(request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()
    jwt_payload = getattr(request.state, "jwt_payload", None)

    profile = get_or_create_profile(uid, jwt_payload, sb)
    return _sign_avatar_url(profile)


@router.get("/check-username")
async def check_username(username: str, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    cleaned_username = username.strip().lower()
    if not cleaned_username:
        return {"available": False, "reason": "Username cannot be empty"}

    result = (
        sb.table("profiles")
        .select("clerk_user_id")
        .eq("username", cleaned_username)
        .execute()
    )

    if not result.data:
        return {"available": True}

    if result.data[0]["clerk_user_id"] == uid:
        return {"available": True}

    return {"available": False}


@router.patch("/me")
async def update_profile(body: ProfileUpdate, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()
    jwt_payload = getattr(request.state, "jwt_payload", None)

    # Ensure profile exists
    get_or_create_profile(uid, jwt_payload, sb)

    updates = body.model_dump(exclude_none=True)
    if not updates:
        profile = get_or_create_profile(uid, jwt_payload, sb)
        return _sign_avatar_url(profile)

    if "username" in updates:
        username_val = updates["username"].strip().lower()
        if not username_val:
            raise HTTPException(status_code=400, detail="Username cannot be empty")

        existing = (
            sb.table("profiles")
            .select("clerk_user_id")
            .eq("username", username_val)
            .execute()
        )
        if existing.data and existing.data[0]["clerk_user_id"] != uid:
            raise HTTPException(status_code=400, detail="Username is already taken")
        updates["username"] = username_val

    result = sb.table("profiles").update(updates).eq("clerk_user_id", uid).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Profile not found.")

    return _sign_avatar_url(result.data[0])


@router.post("/me/avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    uid = request.state.clerk_user_id
    sb = get_supabase()
    jwt_payload = getattr(request.state, "jwt_payload", None)

    # Ensure profile exists
    get_or_create_profile(uid, jwt_payload, sb)

    ext = mimetypes.guess_extension(file.content_type) or ".jpg"
    storage_path = f"{uid}/avatar{ext}"

    try:
        data = await file.read()
        sb.storage.from_("avatars").upload(
            path=storage_path,
            file=data,
            file_options={"content-type": file.content_type, "x-upsert": "true"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload image to storage: {str(e)}")

    result = sb.table("profiles").update({"avatar_url": storage_path}).eq("clerk_user_id", uid).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Profile not found.")

    return _sign_avatar_url(result.data[0])
