from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
import httpx
import jwt
from jwt import PyJWKClient
from app.config import settings

# Cache the JWKS client — it caches keys internally
_jwks_client = PyJWKClient(settings.CLERK_JWKS_URL)

EXCLUDED_PATHS = {"/health", "/webhooks/clerk", "/docs", "/openapi.json"}


class ClerkAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse({"detail": "Missing or invalid Authorization header"}, status_code=401)

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

        # Attach the verified clerk_user_id to request state
        # All routers read from here — never from request body or query params
        request.state.clerk_user_id = payload.get("sub")

        return await call_next(request)