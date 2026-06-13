from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.services.supabase_client import get_supabase
from app.services.digest_service import generate_digest
from app.services.group_digest_service import generate_group_weekly_digest
import logging
import pytz
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")


# ── Daily personal digests ────────────────────────────────────────────────────

async def run_daily_digests():
    """
    Fires every hour at :00.
    Morning slot → triggers at 8am in user's timezone (prev day's nodes)
    Night slot   → triggers at 10pm in user's timezone (current day's nodes)
    """
    slot_hours = {
        "morning": 8,
        "night": 22,
    }

    sb = get_supabase()
    now_utc = datetime.now(timezone.utc)
    profiles = sb.table("profiles").select("clerk_user_id, timezone, digest_slot").execute()

    for profile in (profiles.data or []):
        uid = profile["clerk_user_id"]
        tz_str = profile.get("timezone", "Asia/Kolkata")
        slot = profile.get("digest_slot", "night")

        try:
            tz = pytz.timezone(tz_str)
            user_now = now_utc.astimezone(tz)
            target_hour = slot_hours.get(slot, 22)

            if user_now.hour == target_hour:
                logger.info(f"Generating {slot} digest for {uid}")
                # Pass slot so digest_service knows which day's nodes to fetch
                await generate_digest(uid, period="daily", slot=slot)

        except Exception as e:
            logger.error(f"Daily digest failed for {uid}: {e}")


# ── Weekly group digests ──────────────────────────────────────────────────────

async def run_weekly_group_digests():
    """Every Sunday at 20:00 UTC."""
    sb = get_supabase()
    groups = sb.table("groups").select("id").execute()

    for group in (groups.data or []):
        try:
            logger.info(f"Generating weekly group digest for {group['id']}")
            await generate_group_weekly_digest(group["id"])
        except Exception as e:
            logger.error(f"Group digest failed for {group['id']}: {e}")


# ── Scheduler setup ───────────────────────────────────────────────────────────

def start_scheduler():
    # Check every hour on the hour for users whose slot matches
    scheduler.add_job(
        run_daily_digests,
        CronTrigger(minute=0),
        id="daily_digests",
        replace_existing=True,
    )

    # Group weekly: every Sunday at 20:00 UTC
    scheduler.add_job(
        run_weekly_group_digests,
        CronTrigger(day_of_week="sun", hour=20, minute=0),
        id="weekly_group_digests",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    scheduler.shutdown()
    logger.info("Scheduler stopped")