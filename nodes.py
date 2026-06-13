from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
import uuid, mimetypes
from app.services.supabase_client import get_supabase
from app.services.streak_service import update_streak
from app.services.profile_service import get_or_create_profile

router = APIRouter()


# ── Models ──────────────────────────────────────────────────────────────────

class NodeCreate(BaseModel):
    type: str = "custom"
    visibility: str = "solo"
    title: Optional[str] = None
    content: Optional[str] = None
    metadata: dict = {}

class NodeUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    metadata: Optional[dict] = None
    visibility: Optional[str] = None

class ReactRequest(BaseModel):
    type: Optional[str] = None
    emoji: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _assert_content(title, content, image_url):
    if not any([title, content, image_url]):
        raise HTTPException(400, "A node must have at least a title, content, or image.")

def _user_group(clerk_user_id: str) -> Optional[str]:
    sb = get_supabase()
    profile = get_or_create_profile(clerk_user_id, sb=sb)
    return profile.get("group_id")


def _sign_node_images(nodes_list: list) -> list:
    """Batch generates signed URLs for all nodes with image_url in a single storage API call."""
    if not nodes_list:
        return nodes_list
    image_nodes = [n for n in nodes_list if n.get("image_url")]
    if not image_nodes:
        return nodes_list

    paths = [n["image_url"] for n in image_nodes]
    sb = get_supabase()
    try:
        signed_res = sb.storage.from_("node-images").create_signed_urls(paths, 3600)
        url_map = {item["path"]: item["signedURL"] for item in signed_res if "signedURL" in item}
        for n in image_nodes:
            n["image_signed_url"] = url_map.get(n["image_url"])
    except Exception as e:
        print("Error batch signing node images:", e)
        for n in image_nodes:
            try:
                signed = sb.storage.from_("node-images").create_signed_url(n["image_url"], 3600)
                n["image_signed_url"] = signed.get("signedURL")
            except Exception:
                n["image_signed_url"] = None
    return nodes_list


def _sign_node_avatars(nodes_list: list) -> list:
    """Batch generates signed URLs for all node profile avatars in a single storage API call."""
    if not nodes_list:
        return nodes_list

    avatar_nodes = []
    paths = []
    for n in nodes_list:
        profile = n.get("profiles")
        if profile and profile.get("avatar_url"):
            avatar_url = profile["avatar_url"]
            if not (avatar_url.startswith("http://") or avatar_url.startswith("https://")):
                avatar_nodes.append(n)
                paths.append(avatar_url)

    if not avatar_nodes:
        return nodes_list

    sb = get_supabase()
    try:
        signed_res = sb.storage.from_("avatars").create_signed_urls(paths, 3600)
        url_map = {item["path"]: item["signedURL"] for item in signed_res if "signedURL" in item}
        for n in avatar_nodes:
            profile = n["profiles"]
            avatar_url = profile["avatar_url"]
            profile["avatar_url"] = url_map.get(avatar_url) or avatar_url
    except Exception as e:
        print("Error batch signing node avatars:", e)
        for n in avatar_nodes:
            profile = n["profiles"]
            avatar_url = profile["avatar_url"]
            try:
                signed = sb.storage.from_("avatars").create_signed_url(avatar_url, 3600)
                profile["avatar_url"] = signed.get("signedURL") or avatar_url
            except Exception:
                pass
    return nodes_list


def _map_node_reactions(nodes_list: list) -> list:
    for node in nodes_list:
        reactions_data = node.pop("node_reactions", []) or []
        reactions = []
        for r in reactions_data:
            profile = r.get("profiles") or {}
            display_name = profile.get("display_name") or "Unknown"
            reactions.append({
                "type": r.get("emoji"),
                "user_id": r.get("clerk_user_id"),
                "display_name": display_name
            })
        node["reactions"] = reactions
    return nodes_list


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/")
async def list_nodes(request: Request, limit: int = 20, offset: int = 0, date: Optional[str] = None):
    """Returns the user's own nodes (both solo and shared) and their group's shared nodes."""
    uid = request.state.clerk_user_id
    sb = get_supabase()
    
    group_id = _user_group(uid)
    if group_id:
        query = sb.table("nodes").select("*, profiles(display_name, avatar_url), node_reactions(emoji, clerk_user_id, profiles(display_name))").or_(
            f"clerk_user_id.eq.{uid},and(group_id.eq.{group_id},visibility.eq.shared)"
        )
    else:
        query = sb.table("nodes").select("*, profiles(display_name, avatar_url), node_reactions(emoji, clerk_user_id, profiles(display_name))").eq("clerk_user_id", uid)
    
    if date:
        start_of_day = f"{date}T00:00:00"
        end_of_day = f"{date}T23:59:59.999999"
        query = query.gte("logged_at", start_of_day).lte("logged_at", end_of_day)
        
    result = (
        query
        .order("logged_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return _map_node_reactions(_sign_node_avatars(_sign_node_images(result.data)))


@router.get("/feed")
async def group_feed(request: Request, limit: int = 20, offset: int = 0):
    """Returns shared nodes from the user's group (excluding their own)."""
    uid = request.state.clerk_user_id
    group_id = _user_group(uid)
    if not group_id:
        raise HTTPException(404, "You are not in a group yet.")

    sb = get_supabase()
    result = (
        sb.table("nodes")
        .select("*, profiles(display_name, avatar_url), node_reactions(emoji, clerk_user_id, profiles(display_name))")
        .eq("group_id", group_id)
        .eq("visibility", "shared")
        .neq("clerk_user_id", uid)
        .order("logged_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return _map_node_reactions(_sign_node_avatars(_sign_node_images(result.data)))


@router.post("/")
async def create_node(
    request: Request,
    type: str = Form("custom"),
    visibility: str = Form("solo"),
    title: Optional[str] = Form(None),
    content: Optional[str] = Form(None),
    metadata: Optional[str] = Form("{}"),
    image: Optional[UploadFile] = File(None),
):
    """
    Creates a node. Accepts multipart/form-data so an image can be
    included in the same request as the node data.
    """
    uid = request.state.clerk_user_id
    sb = get_supabase()
    image_url = None

    # Upload image if provided
    if image:
        ext = mimetypes.guess_extension(image.content_type) or ".jpg"
        storage_path = f"{uid}/{uuid.uuid4()}{ext}"
        data = await image.read()
        sb.storage.from_("node-images").upload(
            storage_path, data, {"content-type": image.content_type}
        )
        image_url = storage_path  # store path; signed URL generated on read

    _assert_content(title, content, image_url)

    import json
    meta = json.loads(metadata) if metadata else {}

    group_id = None
    if visibility == "shared":
        group_id = _user_group(uid)
        if not group_id:
            raise HTTPException(400, "You must be in a group to post shared nodes.")

    row = {
        "clerk_user_id": uid,
        "group_id": group_id,
        "type": type,
        "visibility": visibility,
        "title": title,
        "content": content,
        "image_url": image_url,
        "metadata": meta,
    }
    result = sb.table("nodes").insert(row).execute()
    node = result.data[0]

    # Update streak
    try:
        await update_streak(uid)
    except Exception as e:
        print("Error updating user streak:", e)

    if node.get("image_url"):
        try:
            signed = sb.storage.from_("node-images").create_signed_url(node["image_url"], 3600)
            node["image_signed_url"] = signed.get("signedURL")
        except Exception:
            node["image_signed_url"] = None
    return node


@router.get("/{node_id}")
async def get_node(node_id: str, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()
    result = sb.table("nodes").select("*, profiles(display_name, avatar_url), node_reactions(emoji, clerk_user_id, profiles(display_name))").eq("id", node_id).single().execute()
    node = result.data
    if not node:
        raise HTTPException(404, "Node not found.")

    # Security: only owner can see solo nodes; shared nodes need group membership
    if node["visibility"] == "solo" and node["clerk_user_id"] != uid:
        raise HTTPException(403, "Access denied.")
    if node["visibility"] == "shared":
        group_id = _user_group(uid)
        if node["group_id"] != group_id:
            raise HTTPException(403, "Access denied.")

    # Generate signed URL for image if present
    if node.get("image_url"):
        signed = sb.storage.from_("node-images").create_signed_url(node["image_url"], 3600)
        node["image_signed_url"] = signed.get("signedURL")

    node = _map_node_reactions([node])[0]

    return node


@router.patch("/{node_id}")
async def update_node(node_id: str, body: NodeUpdate, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    # Verify ownership
    existing = sb.table("nodes").select("clerk_user_id").eq("id", node_id).execute()
    if not existing.data:
        raise HTTPException(404, "Node not found.")
    if existing.data[0]["clerk_user_id"] != uid:
        raise HTTPException(403, "Access denied.")

    updates = body.model_dump(exclude_none=True)
    result = sb.table("nodes").update(updates).eq("id", node_id).execute()
    return result.data[0]


@router.delete("/{node_id}")
async def delete_node(node_id: str, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    existing = sb.table("nodes").select("clerk_user_id, image_url").eq("id", node_id).execute()
    if not existing.data:
        raise HTTPException(404, "Node not found.")
    if existing.data[0]["clerk_user_id"] != uid:
        raise HTTPException(403, "Access denied.")

    # Delete image from storage if exists
    if existing.data.get("image_url"):
        sb.storage.from_("node-images").remove([existing.data["image_url"]])

    sb.table("nodes").delete().eq("id", node_id).execute()
    return {"deleted": node_id}


@router.post("/{node_id}/react")
async def react_to_node(node_id: str, body: ReactRequest, request: Request):
    uid = request.state.clerk_user_id
    sb = get_supabase()

    emoji = body.type or body.emoji
    if not emoji:
        raise HTTPException(400, "Reaction type or emoji is required.")

    # Fetch node and verify it's shared
    node_res = sb.table("nodes").select("visibility, group_id").eq("id", node_id).execute()
    if not node_res.data:
        raise HTTPException(404, "Node not found.")
    node = node_res.data[0]
    if node["visibility"] != "shared":
        raise HTTPException(400, "You can only react to shared nodes.")

    # Verify user is in the same group
    group_id = _user_group(uid)
    if node["group_id"] != group_id:
        raise HTTPException(403, "Access denied.")

    # Find any existing reaction by this user on this node
    existing = sb.table("node_reactions")\
        .select("id, emoji")\
        .eq("node_id", node_id)\
        .eq("clerk_user_id", uid)\
        .execute()

    if existing.data:
        existing_reaction = existing.data[0]
        if existing_reaction["emoji"] == emoji:
            # Same emoji: toggle off (delete)
            sb.table("node_reactions").delete().eq("id", existing_reaction["id"]).execute()
            return {"action": "removed", "emoji": emoji}
        else:
            # Different emoji: update to new emoji
            sb.table("node_reactions").update({"emoji": emoji}).eq("id", existing_reaction["id"]).execute()
            return {"action": "updated", "emoji": emoji}
    else:
        # No reaction: insert new
        sb.table("node_reactions").insert({
            "node_id": node_id,
            "clerk_user_id": uid,
            "emoji": emoji,
        }).execute()
        return {"action": "added", "emoji": emoji}