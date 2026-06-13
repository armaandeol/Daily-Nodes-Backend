from fastapi import APIRouter, Request, HTTPException
from svix.webhooks import Webhook, WebhookVerificationError
from app.config import settings
from app.services.supabase_client import get_supabase
import re

router = APIRouter()


def _slugify(name: str) -> str:
    """Turn a display name into a safe username slug."""
    slug = re.sub(r"[^a-z0-9]", "", name.lower().replace(" ", ""))
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


@router.post("/webhooks/clerk")
async def clerk_webhook(request: Request):
    """
    Receives Clerk webhook events and syncs user data to Supabase.

    Events handled:
      user.created  → insert a row into profiles
      user.deleted  → cascade-delete handled by DB; we just clean up here
    """
    payload = await request.body()
    headers = dict(request.headers)

    # ── Verify the Svix signature ─────────────────────────────────────────
    wh = Webhook(settings.CLERK_WEBHOOK_SECRET)
    try:
        event = wh.verify(payload, headers)
    except WebhookVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type: str = event.get("type", "")
    data: dict = event.get("data", {})

    sb = get_supabase()

    # ── user.created ──────────────────────────────────────────────────────
    if event_type == "user.created":
        clerk_user_id: str = data["id"]

        # ── Idempotency guard: bail early if profile already exists ────────
        existing = (
            sb.table("profiles")
            .select("id")
            .eq("clerk_user_id", clerk_user_id)
            .execute()
        )
        if existing.data:
            return {"status": "already_exists", "clerk_user_id": clerk_user_id}

        # Build display_name from first/last name, fall back to email local part
        first = (data.get("first_name") or "").strip()
        last = (data.get("last_name") or "").strip()
        display_name = f"{first} {last}".strip()

        if not display_name:
            email_addresses = data.get("email_addresses", [])
            if email_addresses:
                display_name = email_addresses[0]["email_address"].split("@")[0]
            else:
                display_name = "Anonymous"

        # Derive a username slug from the display name
        base_slug = _slugify(display_name) or "user"
        username = _make_unique_username(base_slug, sb)

        # Avatar: prefer Clerk's image_url
        avatar_url = data.get("image_url") or data.get("profile_image_url")

        profile_row = {
            "clerk_user_id": clerk_user_id,
            "username": username,
            "display_name": display_name,
            "avatar_url": avatar_url,
        }

        # Upsert on clerk_user_id — safe against duplicate webhook deliveries.
        # Requires a UNIQUE constraint on profiles.clerk_user_id in Supabase.
        result = (
            sb.table("profiles")
            .upsert(profile_row, on_conflict="clerk_user_id")
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create profile")

        return {"status": "created", "clerk_user_id": clerk_user_id}

    # ── user.deleted ──────────────────────────────────────────────────────
    elif event_type == "user.deleted":
        clerk_user_id: str = data.get("id", "")
        if clerk_user_id:
            # DB cascade handles child rows (nodes, digests, etc.)
            sb.table("profiles").delete().eq("clerk_user_id", clerk_user_id).execute()
        return {"status": "deleted", "clerk_user_id": clerk_user_id}

    # ── unknown event — acknowledge without error ─────────────────────────
    return {"status": "ignored", "type": event_type}
