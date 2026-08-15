from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from app.services.supabase_client import get_supabase
from app.services.digest_service import generate_digest
from app.services.group_digest_service import generate_group_weekly_digest
import logging
import pytz
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")


# ── Daily personal digests ────────────────────────────────────────────────────

async def run_daily_digests():
    """
    Fires every hour at :00.
    Morning slot → triggers at 8am in user's timezone (prev day's nodes)
    Night slot   → triggers at 10pm in user's timezone (current day's nodes)

    Backfill: rather than a tight "== target_hour and minute < 5" window,
    each slot gets a multi-hour catch-up eligibility window. If the app was
    down or slow at the exact trigger minute (deploy, restart, crash), the
    next hourly tick still catches it — generate_digest() is idempotent
    (via the digests table's unique constraint + distributed lock), so a
    user who's already been generated for today is a fast no-op here, not
    a duplicate.
    """
    sb = get_supabase()
    now_utc = datetime.now(timezone.utc)
    logger.info(f"Running daily digests check at {now_utc.isoformat()}")

    profiles = sb.table("profiles").select("clerk_user_id, timezone, digest_slot, digest_enabled").execute()
    logger.info(f"Found {len(profiles.data or [])} profiles to check")

    checked_count = 0
    triggered_count = 0

    for profile in (profiles.data or []):
        uid = profile["clerk_user_id"]
        tz_str = profile.get("timezone", "Asia/Kolkata")
        slot = profile.get("digest_slot", "night")
        digest_enabled = profile.get("digest_enabled", True)

        if not digest_enabled:
            logger.debug(f"Skipping {uid} - digests disabled")
            continue

        try:
            tz = pytz.timezone(tz_str)
            user_now = now_utc.astimezone(tz)
            hour = user_now.hour

            # Eligibility window per slot — generous enough to backfill a
            # missed exact-hour trigger without bleeding into the next slot.
            # reference_date is the user's *local* "today" that digest_service
            # anchors the slot's target date on — computed here, in the
            # user's timezone, not the server's.
            if slot == "morning":
                # 8am through end of day: yesterday's nodes, all day to catch up.
                eligible = hour >= 8
                reference_date = user_now.date()
            elif hour >= 22:
                # Tonight's digest, right on schedule (or a same-day catch-up).
                eligible = True
                reference_date = user_now.date()
            elif hour < 8:
                # Past midnight — still catching up on *last* night's digest.
                eligible = True
                reference_date = user_now.date() - timedelta(days=1)
            else:
                eligible = False
                reference_date = None

            checked_count += 1
            logger.debug(f"User {uid}: timezone={tz_str}, slot={slot}, eligible={eligible}, current_hour={hour}")

            if eligible:
                logger.info(f"🎯 Checking {slot} digest for {uid} (user local time: {user_now.strftime('%Y-%m-%d %H:%M %Z')})")
                # Pass slot so digest_service knows which day's nodes to fetch.
                # generate_digest() itself no-ops if already generated for
                # this slot/date, so it's safe to call every eligible hour.
                result = await generate_digest(uid, period="daily", slot=slot, reference_date=reference_date)
                if result.get("status") == "generated" or "content" in result:
                    triggered_count += 1
                    logger.info(f"✅ Digest generated for {uid}")

        except Exception as e:
            logger.error(f"❌ Daily digest failed for {uid}: {e}", exc_info=True)

    logger.info(f"Digest check complete: checked={checked_count}, triggered={triggered_count}")


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

    # Startup catch-up: run both checks once, almost immediately, so a
    # deploy/restart that lands inside someone's window (or spans a missed
    # Sunday run) doesn't wait for the next hourly/weekly tick. Both jobs
    # are idempotent (DB-backed distributed lock in _claim_digest_slot),
    # so this is a safe no-op for anyone already covered.
    scheduler.add_job(
        run_daily_digests,
        DateTrigger(),
        id="daily_digests_startup_catchup",
        replace_existing=True,
    )
    scheduler.add_job(
        run_weekly_group_digests,
        DateTrigger(),
        id="weekly_group_digests_startup_catchup",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("✨ Scheduler started successfully")
    logger.info("📅 Daily digests: every hour at :00 (+ startup catch-up)")
    logger.info("📊 Weekly group digests: Sundays at 20:00 UTC (+ startup catch-up)")


def stop_scheduler():
    scheduler.shutdown()
    logger.info("Scheduler stopped")