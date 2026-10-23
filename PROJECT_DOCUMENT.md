# Roz — Project Document

This is the single, system-wide explanation of how Roz works end to end: both
repos, the data model, the AI pipeline, and the reasoning behind the
non-obvious decisions in each. The two READMEs ([backend](./README.md),
[frontend](../daily-nodes-web/README.md)) are the "what's in this repo" docs;
this is the "why does the whole thing look like this" doc.

**Roz** — is a life-logging app: you log short moments
throughout the day ("nodes"), optionally share some with a small group ("Circle"),
and get them read back to you as an AI-written daily digest styled like a
miniature newspaper, instead of a plain list.

---

## Contents

1. [System overview](#1-system-overview)
2. [Why these particular tools](#2-why-these-particular-tools)
3. [The data model, and how it got there](#3-the-data-model-and-how-it-got-there)
4. [Request lifecycle: how a log gets from tap to database](#4-request-lifecycle-how-a-log-gets-from-tap-to-database)
5. [The digest pipeline, in full](#5-the-digest-pipeline-in-full)
6. [Scheduling without paying for an always-on server](#6-scheduling-without-paying-for-an-always-on-server)
7. [Concurrency and idempotency](#7-concurrency-and-idempotency)
8. [Security model](#8-security-model)
9. [Frontend architecture and its specific tricks](#9-frontend-architecture-and-its-specific-tricks)
10. [Performance and data-handling optimizations](#10-performance-and-data-handling-optimizations)
11. [Known inconsistencies and cleanup candidates](#11-known-inconsistencies-and-cleanup-candidates)
12. [Glossary](#12-glossary)

---

## 1. System overview

```
                         ┌─────────────────────────┐
                         │          Clerk           │  hosted auth
                         │  (sign-in/up, sessions)  │
                         └─────────┬────────────────┘
                                   │ user.created webhook (Svix-signed)
                                   ▼
┌──────────────────┐   Bearer JWT  ┌───────────────────────┐   service role key   ┌──────────────┐
│   Next.js web     │──────────────▶│   FastAPI backend      │──────────────────────▶│   Supabase    │
│ (daily-nodes-web) │◀──────────────│ (daily-nodes-backend) │◀──────────────────────│  (Postgres +  │
└──────────────────┘    JSON        └───────────┬───────────┘       rows/files      │   Storage)   │
                                                 │                                   └──────────────┘
                                                 │ prompt (structured output)
                                                 ▼
                                         ┌───────────────┐
                                         │  Groq (LLM)    │  openai/gpt-oss-120b
                                         └───────────────┘
```

Two repositories, one product:

- **`daily-nodes-backend`** — FastAPI. Owns the schema, all business rules, the
  digest-generation pipeline, and the only code that ever holds the Supabase
  service-role key.
- **`daily-nodes-web`** — Next.js (App Router). Renders everything, and is the
  only thing end users ever talk to directly. Never touches Supabase or Groq
  directly — every read and write goes through the backend, authenticated with a
  Clerk-issued JWT the frontend forwards as a Bearer token.

Clerk is deliberately the *only* identity system. Supabase is used purely as a
managed Postgres + object-storage host — its own auth system is unused; the
backend talks to it exclusively with the service-role key and enforces all
access rules itself.

## 2. Why these particular tools

| Decision | Reasoning |
|---|---|
| **Clerk over rolling our own auth / Supabase Auth** | Hosted session management, JWKS-based JWT verification, and a webhook to sync user lifecycle events — all of this would otherwise be a meaningful chunk of the backend. |
| **Supabase over a bare Postgres host** | Postgres + file storage + a Python client, in one box, with signed URLs built in for private buckets. The project does not use Supabase Auth, Realtime, or Edge Functions — just Postgres and Storage. |
| **FastAPI over Django/Flask** | Async-native (matters for a backend making LLM API calls), typed request/response models via Pydantic, and free interactive docs (`/docs`) with no extra setup. |
| **Groq over the original Gemini** | Gemini was the original digest provider. Its prepaid credits ran out, and Groq was chosen as the replacement — notably for its OpenAI-compatible **strict structured-output mode**, which this project leans on heavily (see §5.4). `openai/gpt-oss-120b` is an open-weight model served by Groq's fast inference. |
| **Next.js App Router over the project's original TanStack Start scaffold** | The project was bootstrapped from a Lovable TanStack Start template; it was migrated to Next.js App Router for Server Components + Server Actions as a data layer. The old scaffold files were never deleted — see [§11](#11-known-inconsistencies-and-cleanup-candidates). |
| **APScheduler (in-process) + external HTTP pinger, instead of a real worker/cron service** | No paid infrastructure for background jobs. See [§6](#6-scheduling-without-paying-for-an-always-on-server) — this is one of the more deliberate tradeoffs in the whole system. |
| **Postgres unique constraints as a distributed lock, instead of Redis/a queue** | At this scale, Postgres's own constraint-violation-on-conflict behavior *is* a correct distributed lock, and it's one the project already pays for. See [§7](#7-concurrency-and-idempotency). |

## 3. The data model, and how it got there

The schema was built incrementally; the `sql/` directory is effectively a
migration history, each file named for what it adds rather than numbered
sequentially (Supabase SQL Editor, run by hand — there's no migration runner).
Reading them in order tells the story of the product's growth:

1. **`daily_nodes_digests_schema.sql`** — the original shape: `profiles`,
   `groups`, `group_members`, `nodes`, `node_reactions`, `digests`. RLS enabled
   with `current_setting('app.clerk_user_id')`-based policies (later replaced,
   see below).
2. **`storage_buckets_rls_policies.sql`** — `avatars` and `node-images` private
   buckets, with storage-path-based RLS so a direct bucket request can't read
   another user's folder.
3. **`user_streaks_group_weekly_digests_invite_tokens.sql`** — three unrelated
   features landed in one migration: a `streaks` table (with an auto-insert
   trigger on profile creation), `group_weekly` added to the `digest_period`
   enum plus a partial unique index so group digests can share the `digests`
   table with personal ones, and a UUID `invite_token` for shareable links
   alongside the original short `invite_code`.
4. **`node_log_style.sql`** — the four visual "paper styles" a log can be
   created with (`postit`/`lined`/`index`/`scrap`), enforced with a `check`
   constraint rather than trusted to the API layer alone.
5. **`fix_groups_created_by_cascade.sql`** — a genuine bug fix, documented in
   place: `groups.created_by` was `not null` *and* `on delete set null`, a
   self-contradictory pair that aborted deleting a group creator's profile.
   Switched to `on delete cascade`.
6. **`add_group_bio.sql`** — trivial column addition.
7. **`digests_coverage_and_locking.sql`** — `covered_until` and `claimed_at`
   columns on `digests`, turning the table into a rolling-window + distributed-lock
   mechanism (see §5.3, §7). Also documents that `digest_slot`/`digest_enabled`
   on `profiles` were added ad hoc via the Supabase dashboard before ever being
   captured in a tracked migration — the comment exists so the migration history
   matches what's actually in production.
8. **`digest_sections_and_slot.sql`** — adds the `sections` jsonb column (the
   structured-output payload) and `slot`, moving the digest format from
   plain-text `content` to structured data without breaking old rows (see §5.4).
9. **`block_anonymous_access_via_rls_policies.sql`** — replaces the original
   `current_setting()`-based RLS policies with blanket `using (false)` policies.
   **Why:** the backend never sets `app.clerk_user_id` as a Postgres session
   variable — it authenticates in Python and queries with the service-role key,
   which bypasses RLS entirely regardless of policy content. The original
   policies were therefore dead code that looked like real protection but did
   nothing. The replacement is honest about what RLS's job actually is here:
   block any *direct* anon/authenticated access to Supabase (dashboard, a leaked
   anon key, psql), not gate the API.

### Schema at a glance

```
profiles ──1:1── streaks
   │  \
   │   \── group_id (nullable FK)
   │
   ▼
groups ──1:N── group_members ──N:1── profiles
   │
   ▼
nodes ──N:1── profiles, groups (nullable)
   │
   ▼
node_reactions ──N:1── nodes, profiles

digests ──N:1── profiles (nullable) and/or groups (nullable)
   (period: 'daily' | 'group_weekly'; personal and group rows share this table)
```

Constraints worth knowing about because they're enforced **in the schema**, not
just the API:

- `nodes.node_has_content` — a node must have a title, content, or image. Even a
  bug in the API layer can't insert a content-less node.
- `nodes.shared_needs_group` — a `shared` node must have a `group_id`.
- `group_members` has `unique(clerk_user_id)` — one group per user, enforced at
  the database level, independent of whatever the API checks.
- `digests` has `unique(clerk_user_id, period, date_start)` for personal digests,
  and a **partial** unique index `(group_id, period, date_start) where period =
  'group_weekly'` for group ones — letting both digest kinds share one table
  without the unique constraints colliding (a personal digest's `clerk_user_id`
  is a real user; a group digest's `clerk_user_id` is a `group:{id}` sentinel —
  see §5.2 — so they never collide on that column either).

## 4. Request lifecycle: how a log gets from tap to database

1. User taps the add-log button on `/home` → `NodeComposer` opens.
2. User fills in the form, optionally attaches + crops a photo (crop happens
   entirely client-side via canvas — only the cropped pixels are ever encoded).
3. On submit, the Next.js **Server Action** `createNode` (`app/home/actions.ts`)
   runs: it calls `auth().getToken()` to get a fresh Clerk JWT, and `POST`s a
   `multipart/form-data` request straight to `{API_URL}/nodes/`.
4. The backend's `ClerkAuthMiddleware` intercepts every request before it reaches
   a route: verifies the JWT against Clerk's JWKS (RS256, 60s leeway for clock
   skew), and sets `request.state.clerk_user_id` from the verified `sub` claim.
   This is the **only** place a user's identity is ever established — no route
   trusts an ID from the request body or query string.
5. `nodes.py::create_node` validates (image nodes can't have a title; text-only
   nodes must have one), uploads the image to the `node-images` bucket under
   `{clerk_user_id}/{uuid}.{ext}` if present, inserts the row, and fires
   `update_streak()`.
6. The response includes a freshly-signed URL for the image (1-hour expiry) —
   never the raw storage path, and the path itself is never a public URL.
7. Back on the frontend, the feed's `refresh()` re-fetches (preserving however
   many pages were already scroll-loaded) and the new node renders as a polaroid
   or sticky note depending on whether it has a photo.

## 5. The digest pipeline, in full

This is the most engineering-dense part of the system, so it's worth walking
through completely rather than just listing tricks.

### 5.1 What a digest actually is

Twice a day (configurable per user: `morning` or `night`), and once a week per
group, the backend asks an LLM to read a user's (or group's) recent activity and
write a short, structured "edition": a headline, a 3–5 sentence narrative
("tapestry"), an optional Circle section, one sharp "pivot" nudge for tomorrow, a
standalone pull quote, and a hero photo it selects itself from that day's logged
photos (or a themed stock photo if none qualify).

The prompts (`MORNING_PROMPT` / `NIGHT_PROMPT` / `GROUP_WEEKLY_PROMPT` in
`app/services/*.py`) are deliberately opinionated about tone: "synthesize, don't
summarize", "no grocery lists", "be challenging, not nice — but every challenge
must be anchored to a specific fact in the data." The goal is a digest that reads
like a perceptive friend wrote it, not a bulleted activity log.

### 5.2 Claiming a slot: one table, two kinds of digest

Personal (`daily`) and group (`group_weekly`) digests share the `digests` table.
A group digest has no natural `clerk_user_id`, so `group_digest_service.py` uses a
sentinel value — `f"group:{group_id}"` — purely so the *same* claim/lock function
(`_claim_digest_slot`, see §7) works unmodified for both. This is a deliberate
reuse of one mechanism over writing a parallel one for groups.

### 5.3 Rolling coverage windows

A digest's node query doesn't use a fixed calendar-day boundary. Each digest row
stores `covered_until` — the exact timestamp its query ran up to — and the next
digest for that user/period resumes exactly there. Why this matters concretely:
if a night digest runs at 10pm and the user logs one more thing at 11:30pm, a
fixed-boundary design would either double-count that log in tomorrow's "yesterday"
window or drop it outright depending on which boundary was chosen. The rolling
window means it's picked up by whichever digest runs next, exactly once, with no
special-casing.

On generation **failure**, `covered_until` is deliberately left unset — so a
retry recomputes the same window (plus anything new) rather than losing
coverage of whatever that failed attempt should have included.

### 5.4 Structured output over text parsing

Groq is called with `response_format: {"type": "json_schema", "strict": true}`
against a Pydantic schema (`DigestSections`). A few consequences of this choice:

- Every field — including the nullable ones, like `circle_headline` for a
  solo user — must be a `required` key in the schema with an explicit `null`
  as its valid empty value, because strict mode forbids omitting keys. The
  Pydantic model models this as `Optional[x]` *without* a default.
- `has_circle` is decided **in Python**, from whether the user actually has a
  `group_id` — never trusted from the LLM, even if it hallucinates circle
  content for a solo user. The backend drops it regardless of what the model
  wrote.
- Photo selection asks the model to choose a 1-based index from a **numbered
  list of captions**, never the images themselves (`photo_choice: 0` means
  "nothing earns the front page"). This keeps the whole pipeline text-only —
  cheaper, faster, and means user photos are never sent to a third-party model.
- **Backward compatibility without a backfill.** Digests generated before the
  `sections` column existed only have the old plain-text `content` column, with
  headers that differ by slot (`"Yesterday's Roz"` vs `"Today's Tapestry"`, etc).
  `digest_sections.py::get_sections()` checks for a `sections` payload first and,
  if absent, regex-splits `content` by every header either prompt has ever
  written, best-effort. Old and new digests render through one identical API
  shape with zero migration needed.

### 5.5 Photo resolution happens at read time, not write time

A digest never stores a signed URL for its photo — those expire in an hour, and a
digest should still show its photo weeks later. Instead it stores a **descriptor**:
either `{"type": "log", "node_id": ..., "caption": ...}` or `{"type": "stock",
"theme": ..., "index": ..., "caption": ...}`. `get_sections()` resolves this into
a live URL fresh on every read — a new signed URL for a log photo, or a public
Storage URL for a themed stock photo. If the underlying node (or its image) was
deleted since the digest was generated, the photo descriptor silently resolves to
`None` rather than erroring the whole digest.

### 5.6 Server-computed stats

Stats shown on a digest page — logs today, current streak, most active hour, top
log category — are computed directly from node rows in Python (`_compute_stats`),
never asked of the LLM. This keeps arithmetic the LLM has no business doing out of
its hands entirely; its only job is narrative judgment.

### 5.7 Prompt-injection boundary

Every piece of user-controlled text that reaches a prompt — bio, log titles/content,
group name — is wrapped in `<user_content>` tags with an explicit instruction that
anything inside is data to analyze, never an instruction to follow. This is the
project's one deliberate defense against a user writing something like "ignore
previous instructions" inside a log entry and having it actually change the
digest's behavior.

## 6. Scheduling without paying for an always-on server

Render's free web-service tier sleeps an instance after ~15 minutes without
inbound HTTP traffic. An in-process scheduler (`APScheduler`) stops firing the
moment the instance sleeps — there's nothing to wake it back up on its own.

The project's answer is to treat the scheduler as "best-effort while awake" and
back it with an **external HTTP pinger** that does the actual waking:

- `app/services/scheduler.py` runs an hourly check (`CronTrigger(minute=0)`) plus
  a one-off "startup catch-up" job that fires once immediately on boot, so a
  deploy landing mid-window doesn't wait a full hour for its first check.
- `.github/workflows/digest-cron.yml` hits `POST /internal/cron/daily-digests`
  every 30 minutes (and `/internal/cron/weekly-group-digests` once a week), with
  a 90-second `curl --max-time` to outlast Render's free-tier cold start (which
  the comments note can take 50–90s) rather than report a false failure. A
  `cron-job.org` ping is layered on as a second, independent trigger for extra
  redundancy against GitHub Actions scheduling drift.
- The cron endpoints are gated by a shared secret (`X-Cron-Secret` header,
  compared with `hmac.compare_digest` to avoid timing attacks) and are **disabled
  outright** (503) rather than left open if `CRON_SECRET` isn't configured —
  the safer failure mode for an endpoint that triggers real work.

Each digest slot also has a **generous eligibility window**, not a tight
"exactly 8:00am" check: a morning digest is eligible any time from 8am through
end-of-day in the user's own timezone, and a night digest is eligible from 10pm
through to 8am the next day. Combined with `generate_digest()`'s idempotency
(§7), this means a missed exact-hour trigger — a slow deploy, a crashed worker,
a GitHub Actions delay — just gets caught on the next hourly tick instead of
silently skipping that user's digest for the day.

## 7. Concurrency and idempotency

Two overlapping triggers (the in-process scheduler and the external pinger) can
and will fire the same check at close to the same time, sometimes from different
app instances. `_claim_digest_slot()` in `digest_service.py` is what makes this
safe, using the `digests` table's unique constraint as a lock rather than
standing up Redis or a job queue:

1. **Fast path:** attempt to `INSERT` a `pending` row keyed on the unique
   constraint. If it succeeds, this caller owns the slot.
2. **Conflict path:** the insert fails (another worker already has or had this
   slot). Re-select the row and branch on its state:
   - `generated` → already done, return it as a no-op.
   - `pending`, claimed recently (`claimed_at` within 15 minutes) → someone else
     is actively working on it right now, skip.
   - `pending`, claimed **more** than 15 minutes ago → that worker almost
     certainly crashed mid-generation (a slow Groq call finishing is the
     intended ceiling this threshold is set against); reclaim it with a
     conditional `UPDATE ... WHERE status = 'pending'`, so a second reclaimer
     racing the same stale row can't also win.
   - `failed` or `skipped` → both are explicitly retry-safe outcomes (not a
     permanent verdict — "skipped" just means too few logs *as of that check*),
     reclaim immediately.

No distributed lock service, no message queue — the database's own uniqueness
guarantee does the job, and it's infrastructure the project already has to run.

## 8. Security model

- **Identity is Clerk-only.** The backend never issues or stores a password; it
  only ever verifies a Clerk-issued JWT against Clerk's public JWKS.
- **The service-role Supabase key never leaves the backend.** It's used because
  all authorization logic (ownership checks, group-membership checks) is written
  explicitly in each FastAPI route — RLS is a backstop against someone bypassing
  the API entirely (a leaked key, direct dashboard access), not the primary gate.
  See §3's note on `block_anonymous_access_via_rls_policies.sql`.
- **Signed URLs, never public paths, for private media.** Avatars and node
  photos live in private Storage buckets; every read generates a time-boxed
  (1 hour) signed URL, batched into a single Storage API call per list response
  rather than one round-trip per image (see §10).
- **Webhook signature verification.** `/webhooks/clerk` verifies the Svix
  signature before trusting any payload — an unsigned or mis-signed request
  can't create or delete a profile.
- **Cron endpoints are shared-secret gated and fail closed.** See §6.
- **Prompt-injection boundary around all user text sent to the LLM.** See §5.7.

## 9. Frontend architecture and its specific tricks

(Full detail in the [frontend README](../daily-nodes-web/README.md); summarized
here for the system-wide picture.)

The frontend has **no general API client** — each route's `actions.ts` is a set
of plain Server Actions, each independently re-deriving a Clerk token via
`auth().getToken()` and calling the FastAPI backend directly. This was chosen
over a shared fetch wrapper so every data dependency a page has is visible by
reading that one file, at the cost of some repetition across files.

The single most distinctive frontend trick is the **client-side
stale-while-revalidate cache** (`src/lib/client-cache.ts`) — a Zustand store kept
intentionally outside the React tree. It exists because `template.tsx` wraps
every page in a drop-in page-transition animation, which — by how Next.js
`template.tsx` remounting works — unmounts and remounts a page's client
components on *every* navigation, including navigating back to a page you were
just on. Without this cache, every back-navigation to Home or Profile would
force a fresh fetch and show a loading skeleton, even for data fetched seconds
ago. `peekCache`/`writeCache`/`useCachedFetch` let a component paint already-known
data on its very first render after a remount, then quietly revalidate in the
background.

Everything else notable — infinite scroll with ref-based stale-closure handling,
mandatory client-side photo cropping, timezone-safe local-date parsing, and the
hand-drawn "paper" design system — is detailed in the frontend README's
[Notable engineering decisions](../daily-nodes-web/README.md#notable-engineering-decisions)
section.

## 10. Performance and data-handling optimizations

A consolidated list — some already called out above, repeated here because this
was explicitly what was asked about:

| Optimization | Where | What it avoids |
|---|---|---|
| Composite indexes matching actual query shapes (`clerk_user_id, logged_at desc`; `group_id, visibility, logged_at desc`) | `sql/daily_nodes_digests_schema.sql`, refined in `sql/query_optimization_for_retiving_nodes_on_a_specific_date.sql` | sequential scans on the feed and per-day lookup queries, which run on every Home page load |
| Batch-signing of image URLs (`create_signed_urls`, plural) with a per-item fallback | `nodes.py` (`_sign_node_images`, `_sign_node_avatars`), `groups.py`, `profiles.py` | N storage API round-trips per list response turning into 1 |
| One random photo per day, signed in a single batch call, for the activity calendar | `nodes.py::node_activity` | fetching every node for every day in a month just to pick a thumbnail |
| Rolling coverage windows instead of re-querying full calendar days every digest | `digest_service.py` | re-processing logs the previous digest already covered, and (more importantly) silently dropping logs at a window boundary |
| Server-computed stats instead of asking the LLM to count things | `digest_service.py::_compute_stats` | token spend and arithmetic errors on data the backend already has in hand |
| Postgres unique constraint as the digest lock | `digest_service.py::_claim_digest_slot` | paying for Redis/a queue just to prevent duplicate digest generation |
| Minimum log threshold (3) before generating a digest | `digest_service.py` | burning an LLM call on a digest with nothing real to say |
| Frontend: client cache surviving forced remounts | `src/lib/client-cache.ts` | refetching on every back-navigation caused by the page-transition animation |
| Frontend: paginated infinite scroll (12/page) instead of fetching a whole day at once | `NodeFeed.tsx` | the backend's own 20-item unpaginated cap silently truncating any day with more logs |
| Frontend: route-level code-splitting (`next/dynamic`) for the composer and Circle modals | `NodeFeed.tsx`, `GroupManager.tsx` | shipping `react-easy-crop` and modal code in Home's initial JS bundle when most visits never open either |

## 11. Known inconsistencies and cleanup candidates

Documented rather than silently fixed, so context isn't lost:

- **Backend: `auth.py` at the repo root is dead code.** `main.py` uses
  `app/middleware/auth.py`'s `ClerkAuthMiddleware`; nothing imports the root file.
- **Backend: `.env.example` has the LLM provider story backwards** — it labels
  Gemini as active and Groq as legacy, which is the reverse of `app/config.py`.
- **Backend: `requirements.txt` still pins `google-genai`** though no live code
  path calls Gemini anymore.
- **Frontend: a dead TanStack Start scaffold sits alongside the real app** —
  `src/router.tsx`, `src/routes/`, `src/routeTree.gen.ts`, `src/server.ts`,
  `src/start.ts`, `vite.config.ts`. Leftover from the project's original Lovable
  scaffold; the live app is 100% Next.js App Router. Not wired into any build
  script, but still fully present and documented (it even has its own
  `src/routes/README.md` explaining TanStack file-based routing conventions,
  which no longer applies to anything).
- **Frontend: stray `GROQ_API_KEY`/`GROQ_MODEL` in `.env`** with no referencing
  code — looks like an accidental copy from the backend's env file.
- **Frontend: `NEXT_PUBLIC_API_URL` is not set in `.env`**, so every environment
  that doesn't explicitly set it silently falls back to `http://localhost:8000` —
  fine in dev, a real risk if forgotten in a production deploy config.

## 12. Glossary

| Term | Meaning |
|---|---|
| **Node** | A single logged moment — title/content/photo, a category, and a visibility (solo or shared). |
| **Circle** | The product's name (frontend-facing) for what the schema calls a `group` — 2–5 people sharing select nodes. |
| **Digest** | An AI-generated recap: `daily` (per user, morning or night) or `group_weekly` (per Circle). |
| **Slot** | Which of the two daily digest times a user receives: `morning` (processes yesterday) or `night` (processes today). |
| **Edition** | The frontend's name for a single rendered digest on the Explore page. |
| **Coverage window** | The time range of nodes a given digest actually queried, tracked via `covered_until` so the next digest resumes exactly where the last one stopped. |
| **Claim** | The act of a worker taking ownership of a pending digest row via the unique-constraint-as-lock mechanism (§7). |
