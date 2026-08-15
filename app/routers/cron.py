"""
External-trigger endpoint for the digest checks.

Free-tier hosts (Render's free plan, etc.) sleep the app after ~15 minutes
of no HTTP traffic, which kills an in-process scheduler between requests.
Rather than pay for an always-on instance, an external pinger (GitHub
Actions cron, cron-job.org, ...) hits this endpoint hourly — the HTTP
request itself wakes a sleeping instance, and the handler runs the same
checks the in-process scheduler runs on its own hourly tick.

Both triggers are safe to run concurrently or redundantly: every digest
write goes through _claim_digest_slot()'s DB-backed distributed lock, so
whichever fires "first" wins the row and the other is a no-op.
"""

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException

from app.config import settings
from app.services.scheduler import run_daily_digests, run_weekly_group_digests

router = APIRouter()
logger = logging.getLogger(__name__)


def _check_secret(x_cron_secret: str | None) -> None:
    if not settings.CRON_SECRET:
        # Not configured — keep the endpoint disabled rather than open.
        raise HTTPException(503, "Cron endpoint not configured.")
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret, settings.CRON_SECRET):
        raise HTTPException(401, "Invalid or missing cron secret.")


@router.post("/daily-digests")
async def trigger_daily_digests(x_cron_secret: str | None = Header(None)):
    _check_secret(x_cron_secret)
    logger.info("⏰ External cron ping: running daily digest check")
    await run_daily_digests()
    return {"status": "ok"}


@router.post("/weekly-group-digests")
async def trigger_weekly_group_digests(x_cron_secret: str | None = Header(None)):
    _check_secret(x_cron_secret)
    logger.info("⏰ External cron ping: running weekly group digest check")
    await run_weekly_group_digests()
    return {"status": "ok"}
