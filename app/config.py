from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Clerk
    CLERK_WEBHOOK_SECRET: str
    CLERK_JWKS_URL: str

    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # Groq
    GROQ_API_KEY: str
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # Frontend
    FRONTEND_URL: str = "http://localhost:3000"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
