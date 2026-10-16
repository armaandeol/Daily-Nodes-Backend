"""
getSections() — turns a raw `digests` row into the flat, structured shape
the API and frontend consume.

Rows generated after the `sections` jsonb migration already carry
structured data; this module's job there is just to re-resolve the photo
descriptors into live URLs (signed URLs for real log photos expire after
an hour, so they're never stored — only the node_id is — and are
re-signed on every read).

Rows generated before the migration have no `sections` at all — only the
old plain-text `content` column, with headers that differ by digest slot
(morning vs night). For those we tolerantly regex-split `content` back
into sections, best-effort, and simply omit anything that column never
had (headline, pull_quote, photo, stats).
"""

import logging
import re
from typing import Optional

from app.services.digest_service import STOCK_PHOTO_BUCKET

logger = logging.getLogger(__name__)

# Every header string either prompt has ever written to `content`,
# mapped to the canonical section name.
_HEADER_MAP = {
    "yesterday's roz": "tapestry",
    "today's tapestry": "tapestry",
    "the circle": "circle",
    "echoes from the circle": "circle",
    "the pivot": "pivot",
    "for tomorrow": "pivot",
}

_HEADER_PATTERN = re.compile(
    r"^\s*[#*_\s]*(" + "|".join(re.escape(h) for h in _HEADER_MAP) + r")[#*_\s]*\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_legacy_content(content: str) -> dict:
    result = {"tapestry": None, "circle": None, "pivot": None}
    if not content:
        return result

    matches = list(_HEADER_PATTERN.finditer(content))
    if not matches:
        # No recognizable headers — better to surface the raw text as the
        # lead story than to silently drop a whole digest.
        result["tapestry"] = content.strip()
        return result

    for i, m in enumerate(matches):
        section_name = _HEADER_MAP.get(m.group(1).strip().lower())
        if not section_name:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = re.sub(r"^\*+|\*+$", "", content[start:end].strip()).strip()
        if body:
            result[section_name] = body

    return result


def _resolve_stored_photo(photo: Optional[dict], sb) -> Optional[dict]:
    if not photo:
        return None

    if photo.get("type") == "log":
        node_id = photo.get("node_id")
        node_res = sb.table("nodes").select("image_url").eq("id", node_id).maybe_single().execute()
        image_path = (node_res.data or {}).get("image_url") if node_res else None
        if not image_path:
            # Node or its image may have been deleted since generation.
            return None
        try:
            signed = sb.storage.from_("node-images").create_signed_url(image_path, 3600)
            url = signed.get("signedURL")
        except Exception as e:
            logger.warning(f"Failed to sign digest photo for node {node_id}: {e}")
            url = None
        if not url:
            return None
        return {"url": url, "caption": photo.get("caption"), "source": "log"}

    if photo.get("type") == "stock":
        theme = photo.get("theme")
        index = photo.get("index")
        if not theme or not index:
            return None
        url = sb.storage.from_(STOCK_PHOTO_BUCKET).get_public_url(f"{theme}/{index}.jpg")
        return {"url": url, "caption": photo.get("caption"), "source": "stock"}

    return None


def get_sections(digest: dict, sb) -> dict:
    stored = digest.get("sections")

    if stored:
        resolved = dict(stored)
        resolved["photo"] = _resolve_stored_photo(stored.get("photo"), sb)
        resolved["circle_photo"] = _resolve_stored_photo(stored.get("circle_photo"), sb)
        return resolved

    parsed = _parse_legacy_content(digest.get("content") or "")
    return {
        "headline": None,
        "tapestry": parsed["tapestry"],
        "circle_headline": None,
        "circle": parsed["circle"],
        "pivot": parsed["pivot"],
        "pull_quote": None,
        "has_circle": bool(parsed["circle"]),
        "stats": None,
        "slot": digest.get("slot"),
        "photo": None,
        "circle_photo": None,
    }
