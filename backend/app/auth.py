"""Verify Supabase access tokens and expose their authenticated user id."""
import logging
from functools import lru_cache

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client, create_client

from . import config

_bearer = HTTPBearer(auto_error=False)
log = logging.getLogger("notesgraph.auth")


@lru_cache(maxsize=1)
def _auth_client() -> Client:
    if not config.SUPABASE_URL or not config.SUPABASE_PUBLISHABLE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be configured.")
    return create_client(config.SUPABASE_URL, config.SUPABASE_PUBLISHABLE_KEY)


def current_user(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="A valid Supabase access token is required.")
    try:
        user = _auth_client().auth.get_user(credentials.credentials).user
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        if status in (400, 401, 403):
            raise HTTPException(
                status_code=401, detail="Supabase access token is invalid or expired."
            ) from exc
        log.exception("Supabase Auth token verification failed")
        raise HTTPException(
            status_code=503, detail="Unable to verify the Supabase access token."
        ) from exc
    if user is None:
        raise HTTPException(status_code=401, detail="Supabase access token is invalid or expired.")
    return str(user.id)
