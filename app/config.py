from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Clerk
    CLERK_WEBHOOK_SECRET: str
    CLERK_JWKS_URL: str

    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # Groq (legacy — no longer used by digest generation, kept optional so old .env files don't break)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # Gemini
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "gemini-flash-latest"

    # Frontend
    FRONTEND_URL: str = "http://localhost:3000"

    # Cron (optional): shared secret for the external hourly pinger
    # (GitHub Actions / cron-job.org) that wakes a sleeping free-tier
    # instance and triggers a digest check. Leave empty to disable the
    # endpoint entirely — the in-process scheduler still runs on its own.
    CRON_SECRET: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
