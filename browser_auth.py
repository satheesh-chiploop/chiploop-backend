import os
from dataclasses import dataclass
from typing import Any, Dict

import jwt
from fastapi import HTTPException, Request


SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")


@dataclass
class BrowserUser:
    user_id: str
    claims: Dict[str, Any]


ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get("CHIPLOOP_ADMIN_EMAILS", "chiploop.agx@gmail.com").split(",")
    if email.strip()
}


def browser_user_email(user: BrowserUser) -> str:
    email = user.claims.get("email")
    if not email and isinstance(user.claims.get("user_metadata"), dict):
        email = user.claims["user_metadata"].get("email")
    return str(email or "").strip().lower()


def is_browser_admin(user: BrowserUser) -> bool:
    role = str(user.claims.get("role") or user.claims.get("app_role") or "")
    if role in {"admin", "platform_admin", "marketplace_admin"}:
        return True
    return browser_user_email(user) in ADMIN_EMAILS


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return ""
    return header.split(" ", 1)[1].strip()


def _claims_from_supabase_client(request: Request, token: str) -> Dict[str, Any]:
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None or not hasattr(supabase, "auth"):
        return {}
    result = supabase.auth.get_user(token)
    user = getattr(result, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return {}
    email = str(getattr(user, "email", "") or "")
    return {"sub": user_id, "email": email}


def require_browser_user(request: Request) -> BrowserUser:
    token = _bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing_session_token")

    if SUPABASE_JWT_SECRET:
        try:
            claims = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
        except Exception:
            raise HTTPException(status_code=401, detail="invalid_session_token")
        user_id = str(claims.get("sub") or "")
        if not user_id:
            raise HTTPException(status_code=401, detail="invalid_session_token")
        user = BrowserUser(user_id=user_id, claims=claims)
        request.state.browser_user = user
        return user

    try:
        claims = _claims_from_supabase_client(request, token)
    except Exception:
        claims = {}
    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_session_token")
    user = BrowserUser(user_id=user_id, claims=claims)
    request.state.browser_user = user
    return user
