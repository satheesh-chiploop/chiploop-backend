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


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return ""
    return header.split(" ", 1)[1].strip()


def _user_id_from_supabase_client(request: Request, token: str) -> str:
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None or not hasattr(supabase, "auth"):
        return ""
    result = supabase.auth.get_user(token)
    user = getattr(result, "user", None)
    return str(getattr(user, "id", "") or "")


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
        return BrowserUser(user_id=user_id, claims=claims)

    try:
        user_id = _user_id_from_supabase_client(request, token)
    except Exception:
        user_id = ""
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_session_token")
    return BrowserUser(user_id=user_id, claims={"sub": user_id})
