import os
from types import SimpleNamespace
from typing import Any, Dict, Optional

import jwt


class SupabaseAuthAdapter:
    def __init__(self, auth: Any):
        self.auth = auth

    def get_user(self, token: str) -> Any:
        return self.auth.get_user(token)


class OIDCAuthAdapter:
    def __init__(self):
        self.issuer = os.getenv("CHIPLOOP_OIDC_ISSUER", "").rstrip("/")
        self.audience = os.getenv("CHIPLOOP_OIDC_AUDIENCE") or os.getenv("CHIPLOOP_OIDC_CLIENT_ID")
        self.jwks_url = os.getenv("CHIPLOOP_OIDC_JWKS_URL") or (f"{self.issuer}/.well-known/jwks.json" if self.issuer else "")
        if not self.jwks_url:
            raise RuntimeError("OIDC auth adapter requires CHIPLOOP_OIDC_ISSUER or CHIPLOOP_OIDC_JWKS_URL")

    def decode(self, token: str) -> Dict[str, Any]:
        signing_key = jwt.PyJWKClient(self.jwks_url).get_signing_key_from_jwt(token)
        options = {"verify_aud": bool(self.audience)}
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=self.audience,
            issuer=self.issuer or None,
            options=options,
        )

    def get_user(self, token: str) -> Any:
        claims = self.decode(token)
        user = SimpleNamespace(id=str(claims.get("sub") or ""), email=str(claims.get("email") or ""))
        return SimpleNamespace(user=user, claims=claims)
