from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.middleware.auth import ClerkAuthMiddleware
from app.routers import webhooks
from nodes import router as nodes_router
from groups import router as groups_router
from profiles import router as profiles_router
from contextlib import asynccontextmanager
from app.services.scheduler import start_scheduler, stop_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()

app = FastAPI(title="Daily Nodes API", version="0.1.0", lifespan=lifespan)

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Next.js dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth middleware (skips webhook + health routes) ───────────────────────
app.add_middleware(ClerkAuthMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────
app.include_router(webhooks.router, tags=["webhooks"])
app.include_router(nodes_router, prefix="/nodes", tags=["nodes"])
app.include_router(groups_router, prefix="/api/v1/groups", tags=["groups"])
app.include_router(profiles_router, prefix="/api/v1/profiles", tags=["profiles"])



@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}


