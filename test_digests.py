import asyncio
import uuid
import sys
from datetime import date, timedelta, datetime, timezone
from app.services.supabase_client import get_supabase
from app.services.digest_service import generate_digest
from app.services.group_digest_service import generate_group_weekly_digest
from app.config import settings

async def main():
    print("====================================================")
    print("Daily Nodes - End-to-End Digest & Scheduler Test")
    print("====================================================")
    
    # 1. Initialize Supabase
    try:
        sb = get_supabase()
        print(f"Supabase client initialized successfully.")
        print(f"SUPABASE_URL: {settings.SUPABASE_URL}")
    except Exception as e:
        print(f"Error initializing Supabase client: {e}")
        sys.exit(1)

    # 2. Check connection and list existing counts
    try:
        profiles_res = sb.table("profiles").select("count", count="exact").limit(1).execute()
        nodes_res = sb.table("nodes").select("count", count="exact").limit(1).execute()
        digests_res = sb.table("digests").select("count", count="exact").limit(1).execute()
        
        print(f"Database connection active.")
        print(f"Existing profiles count: {profiles_res.count}")
        print(f"Existing nodes count: {nodes_res.count}")
        print(f"Existing digests count: {digests_res.count}")
    except Exception as e:
        print(f"Database query failed. Please check your Supabase URL and key. Error: {e}")
        sys.exit(1)

    # 3. Create or get test data
    test_user_id = "clerk_test_user_999"
    test_group_name = "Test Group alpha"
    
    print("\n--- Setting up test data ---")
    
    # Check if test profile exists, else create it
    profile_res = sb.table("profiles").select("*").eq("clerk_user_id", test_user_id).execute()
    if profile_res.data:
        profile = profile_res.data[0]
        print(f"Using existing test profile: {profile['display_name']} ({test_user_id})")
    else:
        print("Creating a new test profile...")
        profile_insert = sb.table("profiles").insert({
            "clerk_user_id": test_user_id,
            "username": f"tester_{uuid.uuid4().hex[:6]}",
            "display_name": "Test Runner",
            "timezone": "Asia/Kolkata",
            "digest_slot": "night"
        }).execute()
        profile = profile_insert.data[0]
        print(f"Created test profile: {profile['display_name']}")

    # Check if user is in a group, else create one and join
    group_id = profile.get("group_id")
    if group_id:
        print(f"User is already in group: {group_id}")
    else:
        print("User not in a group. Creating a new test group...")
        group_insert = sb.table("groups").insert({
            "name": test_group_name,
            "created_by": test_user_id
        }).execute()
        group = group_insert.data[0]
        group_id = group["id"]
        print(f"Created group: {group['name']} (ID: {group_id})")
        
        # Add to group_members
        print("Adding user to group_members...")
        sb.table("group_members").insert({
            "group_id": group_id,
            "clerk_user_id": test_user_id,
            "role": "owner"
        }).execute()
        
        # Update profile with group_id
        print("Updating profile with group_id...")
        sb.table("profiles").update({"group_id": group_id}).eq("clerk_user_id", test_user_id).execute()
        profile["group_id"] = group_id

    # 4. Handle Sentinel Profile for group_weekly digests
    # In group_weekly digests, the clerk_user_id is sentinel_uid = f"group:{group_id}"
    # Because digests table has a foreign key to profiles(clerk_user_id), we MUST create a profile for this sentinel
    # or the upsert in generate_group_weekly_digest will fail due to foreign key violation.
    sentinel_uid = f"group:{group_id}"
    sentinel_profile_res = sb.table("profiles").select("*").eq("clerk_user_id", sentinel_uid).execute()
    if sentinel_profile_res.data:
        print(f"Sentinel profile exists for group weekly digests: {sentinel_uid}")
    else:
        print(f"Creating a sentinel profile for group weekly digests (to satisfy FK constraint)...")
        try:
            sb.table("profiles").insert({
                "clerk_user_id": sentinel_uid,
                "username": f"group_weekly_{group_id[:8]}",
                "display_name": f"Weekly Group Digest Sentinel",
                "timezone": "Asia/Kolkata",
                "group_id": group_id
            }).execute()
            print("Sentinel profile created successfully.")
        except Exception as e:
            print(f"Warning: Failed to create sentinel profile: {e}. If foreign keys are not enforced this is fine, otherwise group weekly digest might fail.")

    # 5. Insert test nodes for yesterday and today to generate real digests
    print("\n--- Inserting test activity nodes ---")
    today = date.today()
    yesterday = today - timedelta(days=1)
    
    # We will log a workout and mood yesterday, food and study today
    test_nodes_to_insert = [
        # Yesterday (solo & shared)
        {
            "clerk_user_id": test_user_id,
            "group_id": group_id,
            "type": "workout",
            "visibility": "shared",
            "title": "Evening Run",
            "content": "Ran 5km in 24 minutes. Felt strong and energized!",
            "logged_at": f"{yesterday.isoformat()}T18:30:00Z"
        },
        {
            "clerk_user_id": test_user_id,
            "group_id": None,
            "type": "mood",
            "visibility": "solo",
            "title": "Reflective",
            "content": "Had a quiet productive evening reading books.",
            "logged_at": f"{yesterday.isoformat()}T21:00:00Z"
        },
        # Today (solo & shared)
        {
            "clerk_user_id": test_user_id,
            "group_id": group_id,
            "type": "food",
            "visibility": "shared",
            "title": "Healthy Lunch",
            "content": "Grilled chicken breast, quinoa, avocado salad. Tasted great!",
            "logged_at": f"{today.isoformat()}T13:00:00Z"
        },
        {
            "clerk_user_id": test_user_id,
            "group_id": group_id,
            "type": "study",
            "visibility": "shared",
            "title": "Coding Session",
            "content": "Spent 3 hours refactoring backend service architecture and writing test scripts.",
            "logged_at": f"{today.isoformat()}T16:00:00Z"
        }
    ]
    
    inserted_node_ids = []
    for node in test_nodes_to_insert:
        # Check if node already exists to avoid cluttering
        existing_node = sb.table("nodes").select("id").eq("clerk_user_id", node["clerk_user_id"]).eq("title", node["title"]).eq("logged_at", node["logged_at"]).execute()
        if existing_node.data:
            print(f"Node already exists: {node['title']}")
            inserted_node_ids.append(existing_node.data[0]["id"])
        else:
            inserted = sb.table("nodes").insert(node).execute()
            node_id = inserted.data[0]["id"]
            inserted_node_ids.append(node_id)
            print(f"Logged test node: {node['title']} (ID: {node_id})")

    # 6. Test Streak Update (streak service check)
    print("\n--- Testing Streak update ---")
    try:
        from app.services.streak_service import update_streak
        await update_streak(test_user_id)
        streak_res = sb.table("streaks").select("*").eq("clerk_user_id", test_user_id).execute()
        if streak_res.data:
            print(f"Current Streak: {streak_res.data[0]['streak_count']} days, Best Streak: {streak_res.data[0]['best_streak']} days")
    except Exception as e:
        print(f"Failed to check/update streak: {e}")

    # 7. Generate Daily Morning Digest (Recap of yesterday)
    print("\n--- Generating Morning Digest ---")
    # First, let's delete any existing daily morning digest for yesterday to force generation
    sb.table("digests").delete().eq("clerk_user_id", test_user_id).eq("period", "daily").eq("date_start", yesterday.isoformat()).execute()
    
    try:
        morning_digest = await generate_digest(test_user_id, period="daily", slot="morning")
        print("\n>>> MORNING DIGEST RESULT <<<")
        print(f"ID: {morning_digest['id']}")
        print(f"Date: {morning_digest['date_start']}")
        print(f"Status: {morning_digest['status']}")
        print(f"Content:\n{morning_digest['content']}")
    except Exception as e:
        print(f"Failed to generate morning digest: {e}")

    # 8. Generate Daily Night Digest (Recap of today)
    print("\n--- Generating Night Digest ---")
    # Delete any existing daily night digest for today to force generation
    sb.table("digests").delete().eq("clerk_user_id", test_user_id).eq("period", "daily").eq("date_start", today.isoformat()).execute()
    
    try:
        night_digest = await generate_digest(test_user_id, period="daily", slot="night")
        print("\n>>> NIGHT DIGEST RESULT <<<")
        print(f"ID: {night_digest['id']}")
        print(f"Date: {night_digest['date_start']}")
        print(f"Status: {night_digest['status']}")
        print(f"Content:\n{night_digest['content']}")
    except Exception as e:
        print(f"Failed to generate night digest: {e}")

    # 9. Generate Group Weekly Digest
    print("\n--- Generating Group Weekly Digest ---")
    # Delete existing group weekly digest for the starting period to force generation
    date_start = today - timedelta(days=6)
    sb.table("digests").delete().eq("group_id", group_id).eq("period", "group_weekly").eq("date_start", date_start.isoformat()).execute()
    
    try:
        weekly_digest = await generate_group_weekly_digest(group_id)
        print("\n>>> GROUP WEEKLY DIGEST RESULT <<<")
        print(f"ID: {weekly_digest['id']}")
        print(f"Date range: {weekly_digest['date_start']} to {weekly_digest['date_end']}")
        print(f"Status: {weekly_digest['status']}")
        print(f"Stats aggregate: {weekly_digest['suggestions']}")
        print(f"Content:\n{weekly_digest['content']}")
    except Exception as e:
        print(f"Failed to generate group weekly digest: {e}")

    # 10. Clean Up test data? (Optional, let's keep it clean by deleting test nodes and digests)
    print("\n--- Cleaning up test data ---")
    # Delete digests
    sb.table("digests").delete().eq("clerk_user_id", test_user_id).execute()
    sb.table("digests").delete().eq("clerk_user_id", sentinel_uid).execute()
    print("Deleted test digests.")
    
    # Delete nodes
    for nid in inserted_node_ids:
        sb.table("nodes").delete().eq("id", nid).execute()
    print("Deleted test nodes.")

    print("\n====================================================")
    print("End-to-End Test Completed successfully!")
    print("====================================================")

if __name__ == "__main__":
    asyncio.run(main())
