from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from jwt import PyJWKClient
import jwt
from app.config import settings

# Cache the JWKS client — it caches signing keys internally
_jwks_client = PyJWKClient(settings.CLERK_JWKS_URL)

EXCLUDED_PATHS = {"/health", "/webhooks/clerk", "/docs", "/openapi.json"}
# /internal/cron/* authenticates itself via X-Cron-Secret (see app/routers/cron.py)
EXCLUDED_PREFIXES = ("/internal/cron/",)


class ClerkAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXCLUDED_PATHS or request.url.path.startswith(EXCLUDED_PREFIXES):
            return await call_next(request)

        # Regular Clerk authentication
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"detail": "Missing or invalid Authorization header"},
                status_code=401,
            )

        token = auth_header.removeprefix("Bearer ").strip()

        try:
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={"verify_aud": False},  # Clerk doesn't set aud by default
                leeway=60,
            )
        except jwt.ExpiredSignatureError:
            return JSONResponse({"detail": "Token expired"}, status_code=401)
        except jwt.InvalidTokenError as e:
            return JSONResponse({"detail": f"Invalid token: {str(e)}"}, status_code=401)

        # Attach verified clerk_user_id and jwt_payload to request state
        request.state.clerk_user_id = payload.get("sub")
        request.state.jwt_payload = payload

        return await call_next(request)
