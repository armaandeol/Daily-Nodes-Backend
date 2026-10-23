# Roz — Backend

The FastAPI service behind **Roz**  — a life-logging app where
you log short moments ("nodes") throughout the day, optionally share them with a
small group ("Circle"), and get an AI-written daily digest that reads your day back
to you instead of just listing it.

This repo is the API + background job layer. The web client lives in
[`daily-nodes-web`](../daily-nodes-web) (Next.js) — see its README for the frontend.

> 📸 *Screenshot: architecture diagram or Swagger UI (`/docs`) goes here.*

---

## Table of contents

- [What this service does](#what-this-service-does)
- [Tech stack](#tech-stack)
- [Architecture at a glance](#architecture-at-a-glance)
- [Data model](#data-model)
- [The digest pipeline](#the-digest-pipeline)
- [Notable engineering decisions](#notable-engineering-decisions)
- [API surface](#api-surface)
- [Running locally](#running-locally)
- [Environment variables](#environment-variables)
- [Deployment](#deployment)
- [Known rough edges](#known-rough-edges)

---

## What this service does

- **Auth sync** — Clerk is the identity provider; a webhook mirrors each new user
  into a `profiles` row in Supabase so the rest of the schema has something to
  foreign-key against.
- **Nodes** — the core unit of the app. A node is a short log entry: a title, some
  text, an optional photo, a category (`workout` / `food` / `mood` / `study` /
  `social` / `custom`), and a visibility (`solo` or `shared` with your Circle).
- **Circles (groups)** — 2–5 people who share select nodes, react to each other's
  logs with emoji, and get a weekly group digest.
- **Streaks** — a running count of consecutive days with at least one log.
- **Digests** — the centerpiece. An LLM (Groq, running an open-weight GPT-OSS
  model) reads a user's recent logs and their Circle's shared activity, and writes
  a short magazine-style page: a headline, a narrative paragraph, a pull quote, one
  sharp "pivot" nudge, and a hero photo it picks itself. Runs automatically twice a
  day per user (morning/night, user's choice) and once a week per group.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| API framework | **FastAPI** | async-first, typed, auto docs at `/docs` |
| Database | **Supabase (Postgres)** | managed Postgres + storage buckets in one place; accessed here purely as a Postgres host — RLS is a defense-in-depth layer, not the primary access control (see below) |
| Auth | **Clerk** | hosted auth; this service only ever verifies Clerk-issued JWTs, never handles passwords |
| LLM | **Groq** (`openai/gpt-oss-120b`), **legacy: Gemini** | Groq chosen for speed + an OpenAI-compatible structured-output mode; Gemini was the original provider until its prepaid credits ran out |
| Scheduling | **APScheduler** (in-process) + an external HTTP pinger | see [Notable engineering decisions](#notable-engineering-decisions) |
| File storage | **Supabase Storage** (private buckets, signed URLs) | avatars, node photos, digest stock photos |

## Architecture at a glance

```
Clerk (auth) ──webhook──► /webhooks/clerk ──► profiles table
     │
     │ JWT (RS256, JWKS-verified)
     ▼
Next.js frontend ──Bearer token──► FastAPI ──service-role──► Supabase (Postgres + Storage)
                                      │
                                      ├─ nodes.py      (log CRUD, reactions, activity calendar)
                                      ├─ groups.py     (Circles, invites, membership)
                                      ├─ profiles.py   (profile, avatar, digest preferences)
                                      ├─ digests.py    (read-only digest API)
                                      └─ app/services/
                                           ├─ digest_service.py        (daily digest generation)
                                           ├─ group_digest_service.py  (weekly group digest)
                                           ├─ digest_sections.py       (raw row → frontend shape)
                                           ├─ streak_service.py
                                           ├─ profile_service.py       (lazy profile creation)
                                           └─ scheduler.py             (APScheduler jobs)
                                      │
                                      └─ app/routers/cron.py  ◄── external pinger (GitHub Actions)
                                      └─ Groq API  (digest text generation)
```

Every router reads the authenticated user **only** from `request.state.clerk_user_id`,
set once by `ClerkAuthMiddleware` after verifying the JWT against Clerk's JWKS. No
route ever trusts a user ID from the request body or query string — this is a hard
rule throughout the codebase.

The Supabase client is created once with the **service role key** (bypasses RLS).
All real access control — "can this user see this node?", "is this user a member
of this group?" — is enforced by hand in Python, in each route. Postgres RLS
policies exist too (`sql/block_anonymous_access_via_rls_policies.sql`), but they're
a backstop against someone hitting Supabase directly, not the primary gate.

## Data model

Defined across `sql/*.sql`, applied as sequential migrations against Supabase.

| Table | Purpose |
|---|---|
| `profiles` | One row per Clerk user. Also carries app prefs: `timezone`, `digest_slot` (morning/night), `digest_enabled`. |
| `groups` | A Circle. Has both a short `invite_code` (typed) and a `invite_token` (UUID, for link-sharing). |
| `group_members` | Join table. `unique(clerk_user_id)` enforces **one group per user** at the DB level, not just in application code. |
| `nodes` | The log entries. `check` constraints enforce "must have title, content, or image" and "shared nodes must have a group" **in the schema**, so a bug in the API can't silently create an invalid row. |
| `node_reactions` | Emoji reactions, `unique(node_id, user, emoji)` — one of each emoji per user per node. |
| `streaks` | One row per user, auto-created by a trigger on `profiles` insert so the app never has to upsert-from-scratch. |
| `digests` | Generated digest rows — personal daily and group weekly share this table (see below). |

**Why personal and group digests share one table:** a `group_id` column (nullable)
plus a partial unique index (`where period = 'group_weekly'`) let group digests
reuse the same unique-constraint-as-lock trick personal digests use, instead of
duplicating the whole claim/generate/store pipeline for a second table.

### Indexing

```sql
create index idx_nodes_user_logged  on nodes(clerk_user_id, logged_at desc);
create index idx_nodes_group_shared on nodes(group_id, visibility, logged_at desc);
create index idx_nodes_logged_at    on nodes(logged_at desc);
```

These line up with the actual query shapes in `nodes.py` — "this user's nodes,
newest first" and "this group's shared nodes, newest first" — rather than being
generic single-column indexes. `sql/query_optimization_for_retiving_nodes_on_a_specific_date.sql`
is a second pass at the same index after noticing per-day lookups (the `date=`
query param, the activity calendar) leaned on it too.

## The digest pipeline

This is the most involved part of the backend, so it gets its own section.

### 1. Scheduling without a worker process

Render's free tier doesn't run background workers, and a free-tier *web* service
goes to sleep after ~15 minutes without inbound HTTP traffic — which also kills an
in-process scheduler. Rather than pay for an always-on instance, the backend uses
**both**:

- `app/services/scheduler.py` — an `APScheduler` job that fires every hour on the
  hour, checking every profile against its chosen digest slot in its *own*
  timezone.
- An external pinger (`.github/workflows/digest-cron.yml`, running every 30 min,
  plus an optional `cron-job.org` ping as a second, redundant trigger) that hits
  `POST /internal/cron/daily-digests`. The HTTP request itself is what wakes a
  sleeping instance — the handler then runs the exact same check the in-process
  scheduler runs on its own.

Both are safe to fire concurrently or redundantly because of the locking scheme
below — whichever request reaches Postgres first wins the row, the other is a
no-op.

### 2. Idempotency and locking via a unique constraint

`digests` has `unique(clerk_user_id, period, date_start)`. `_claim_digest_slot()`
in `digest_service.py` turns that constraint into a distributed lock:

1. Try to `INSERT` a `pending` row for this user/period/date. If it succeeds, this
   worker owns the slot — proceed to call Groq.
2. If the insert fails (unique violation — someone else already claimed it),
   re-select the row and look at its status:
   - `generated` → done already, skip.
   - `pending` and claimed recently → another worker is actively on it, skip.
   - `pending` but `claimed_at` is >15 minutes old → that worker likely crashed
     mid-generation; reclaim it with a conditional `UPDATE ... WHERE status =
     'pending'` (so two reclaimers can't both win).
   - `failed` / `skipped` → reclaimable immediately, since both are "retry-safe"
     outcomes, not permanent ones.

No separate lock table, no Redis — Postgres's own uniqueness guarantee *is* the
lock, which is enough at this scale and one less moving part to operate.

### 3. Rolling coverage windows, not calendar days

A digest doesn't just query "today's" or "yesterday's" nodes by calendar date. It
resumes from `covered_until` — the exact timestamp the *previous* digest's query
ran up to. This means a node logged at 11pm, after the night digest already ran at
10pm, isn't silently dropped from every future digest — it shows up the next time
a digest runs, because the window starts exactly where the last one stopped,
not at the next midnight.

### 4. Structured output instead of parsing free text

Groq is called with `response_format: json_schema` (strict mode) against a
Pydantic model (`DigestSections`). The LLM fills in exact fields — `headline`,
`tapestry`, `pivot`, `pull_quote`, `photo_choice`, etc. — rather than returning
markdown that the backend then has to regex apart. Two consequences worth calling
out:

- **Legacy data still reads cleanly.** Digests generated before this migration
  only have the old plain-text `content` column. `digest_sections.py` tolerantly
  regex-splits that column by the headers the old prompts used to write
  (`"Yesterday's Roz"`, `"The Circle"`, …) so old and new digests render through
  the same API shape without a backfill migration.
- **The LLM never sees images, only captions.** Photo selection works by listing a
  user's photo nodes as a numbered list of *captions only* and asking Groq to pick
  the most caption-worthy one (or 0, for none). This keeps the whole pipeline
  text-only — cheaper, faster, and avoids ever sending user photos to a third-party
  model.

### 5. Server-computed stats, LLM-written prose

Things like "logs today", "most active hour", and "top log type" are computed
directly from the node rows in Python (`_compute_stats`), never asked of the LLM.
The model's job is narrative judgment, not arithmetic it could get wrong.

### 6. Prompt-injection boundary

Every user-controlled string (bio, log content, group name) is wrapped in
`<user_content>` tags with an explicit instruction: anything inside is data to
analyze, never an instruction to follow. This is the one line of defense against a
user writing "ignore previous instructions" inside a log entry.

## Notable engineering decisions

- **Batch-signing storage URLs.** Node images and avatars are stored as private
  paths and only ever exposed as time-limited signed URLs, generated at read time
  (never stored — they'd expire). Every list endpoint (`list_nodes`, `group_feed`,
  `get_my_group`) signs all its image URLs in **one** Supabase Storage call
  (`create_signed_urls`, plural) instead of one HTTP round-trip per image, with a
  per-item fallback if the batch call errors.
- **Timezone-correct "today".** A node's `logged_at` is UTC, but "today" has to
  mean the *user's* today. `_local_day_bounds_utc()` converts a `YYYY-MM-DD` into
  UTC instants using the user's own `timezone` profile field before querying —
  fixing an earlier bug where a late-night log in, say, `Asia/Kolkata` would show
  up under the wrong UTC calendar day.
- **Lazy profile creation.** `get_or_create_profile()` means no route has to assume
  a profile already exists — if the Clerk webhook hasn't landed yet (or was
  missed), the first authenticated request creates one from JWT claims instead of
  404ing.
- **Self-contradictory FK fixed via a dedicated migration.** `groups.created_by`
  was originally `not null` *and* `on delete set null` — deleting a group creator's
  profile violated the not-null constraint and aborted the whole delete.
  `sql/fix_groups_created_by_cascade.sql` documents the bug and switches it to
  `on delete cascade`, so deleting a profile now cleanly tears down everything that
  belongs to it.

## API surface

All routes except `/health`, `/webhooks/clerk`, `/docs`, and `/openapi.json`
require `Authorization: Bearer <Clerk JWT>`.

| Prefix | Router | Covers |
|---|---|---|
| `/nodes` | `nodes.py` | create/list/update/delete nodes, reactions, the activity calendar feed, group feed |
| `/api/v1/groups` | `groups.py` | create/join/leave a Circle, invite codes & links, member management, group digest trigger |
| `/api/v1/profiles` | `profiles.py` | profile read/update, avatar upload, username availability |
| `/api/v1/digests` | `digests.py` | read-only access to generated digests (own + group) |
| `/internal/cron` | `app/routers/cron.py` | external-pinger-only trigger endpoints, gated by `X-Cron-Secret` |
| `/webhooks/clerk` | `app/routers/webhooks.py` | Clerk → Supabase profile sync (Svix-signature verified) |

Full interactive schema is at `/docs` (Swagger) once the server is running.

> 📸 *Screenshot: `/docs` Swagger UI goes here.*

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate        # or `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
cp .env.example .env          # fill in the real values — see below
uvicorn main:app --reload
```

The API comes up on `http://localhost:8000`. The frontend (`daily-nodes-web`)
expects it there by default in dev.

## Environment variables

See `.env.example` for the full list. Summary:

| Variable | Required | Notes |
|---|---|---|
| `CLERK_WEBHOOK_SECRET` | yes | verifies the Svix signature on `/webhooks/clerk` |
| `CLERK_JWKS_URL` | yes | used to verify every incoming request's JWT |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | yes | service-role key — bypasses RLS, so this key never goes near the frontend |
| `GROQ_API_KEY` / `GROQ_MODEL` | yes | active digest LLM provider |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | no | legacy — kept optional so older `.env` files don't break; unused since the Groq switch |
| `FRONTEND_URL` | no | used to build invite links (`{FRONTEND_URL}/join/{token}`) |
| `CRON_SECRET` | no | leave blank to disable `/internal/cron/*` entirely rather than leave it open |

> **Note:** `.env.example` currently documents Gemini as the active provider and
> Groq as legacy — that's backwards from the current code (`app/config.py`) and
> worth fixing next time this file is touched.

## Deployment

Deployed to **Render** (`render.yaml`) on the free web-service tier — see
[The digest pipeline → Scheduling](#the-digest-pipeline) for why that shapes the
cron design. `healthCheckPath: /health` is what Render polls to consider the
instance alive.

## Known rough edges

Documented here rather than silently fixed, so nothing is lost if you pick this up
later:

- **`auth.py` at the repo root is dead code.** `main.py` wires up
  `app/middleware/auth.py`'s `ClerkAuthMiddleware` instead; the root-level file
  isn't imported anywhere. Safe to delete, left as-is for now.
- **`.env.example` is stale** — see the note above.
- **`requirements.txt` still lists `google-genai`** even though Gemini is no
  longer called from any live code path.

---

*For the system-wide picture — how this talks to the frontend, the full schema
rationale, and the "why" behind every trick above — see
[`PROJECT_DOCUMENT.md`](./PROJECT_DOCUMENT.md).*
