import base64
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
import jwt

from .repository import GitHubInstallationRepository


class GitHubNotConfiguredError(Exception):
    pass


class GitHubRequestError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class GitHubIntegrationService:
    def __init__(
        self,
        token: Optional[str] = None,
        api_base: str = "https://api.github.com",
        repository: Optional[GitHubInstallationRepository] = None,
    ):
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_APP_INSTALLATION_TOKEN")
        self.app_id = os.getenv("GITHUB_APP_ID", "")
        self.app_slug = os.getenv("GITHUB_APP_SLUG", "")
        self.app_private_key = self._load_private_key()
        self.frontend_base_url = os.getenv("FRONTEND_BASE_URL", "https://www.getchiploops.com").rstrip("/")
        self.api_base = api_base.rstrip("/")
        self.repository = repository

    def _load_private_key(self) -> str:
        raw = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
        b64 = os.getenv("GITHUB_APP_PRIVATE_KEY_BASE64", "")
        if raw:
            return raw.replace("\\n", "\n")
        if b64:
            return base64.b64decode(b64).decode("utf-8")
        return ""

    def app_configured(self) -> bool:
        return bool(self.app_id and self.app_slug and self.app_private_key)

    def connect_url(self, user_id: Optional[str] = None) -> Optional[str]:
        if not self.app_slug:
            return None
        params = {}
        if user_id:
            params["state"] = user_id
        query = f"?{urlencode(params)}" if params else ""
        return f"https://github.com/apps/{self.app_slug}/installations/new{query}"

    def status(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        installation = self.repository.get_active_for_user(user_id) if user_id and self.repository else None
        app_ready = self.app_configured()
        configured = bool(installation and app_ready) or bool(self.token)
        return {
            "configured": configured,
            "connected": bool(installation),
            "auth_mode": "github_app" if installation and app_ready else ("token" if self.token else None),
            "connect_url": self.connect_url(user_id),
            "installation": installation,
            "app_configured": app_ready,
            "message": None if configured else "Connect GitHub to import files from your repositories.",
        }

    def _app_jwt(self) -> str:
        if not self.app_configured():
            raise GitHubNotConfiguredError("github_app_not_configured")
        now = datetime.now(timezone.utc)
        payload = {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.app_private_key, algorithm="RS256")

    async def _installation_token(self, installation_id: str) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.api_base}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code >= 400:
            raise GitHubRequestError(response.status_code, response.text)
        return str(response.json().get("token") or "")

    async def _token_for_user(self, user_id: Optional[str]) -> str:
        if user_id and self.repository and self.app_configured():
            installation = self.repository.get_active_for_user(user_id)
            if installation:
                return await self._installation_token(str(installation["github_installation_id"]))
        if self.token:
            return self.token
        raise GitHubNotConfiguredError("github_not_configured")

    async def _headers(self, user_id: Optional[str] = None) -> Dict[str, str]:
        token = await self._token_for_user(user_id)
        if not token:
            raise GitHubNotConfiguredError("github_not_configured")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None, user_id: Optional[str] = None) -> Dict[str, Any] | List[Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{self.api_base}{path}", headers=await self._headers(user_id), params=params or {})
        if response.status_code >= 400:
            try:
                body = response.json()
                message = str(body.get("message") or response.text)
            except Exception:
                message = response.text
            raise GitHubRequestError(response.status_code, message)
        return response.json()

    async def _get_app(self, path: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.api_base}{path}",
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code >= 400:
            raise GitHubRequestError(response.status_code, response.text)
        return response.json()

    async def register_installation(self, user_id: str, installation_id: str) -> Dict[str, Any]:
        if not self.repository:
            raise GitHubNotConfiguredError("github_installation_store_unavailable")
        info = await self._get_app(f"/app/installations/{installation_id}")
        account = info.get("account") if isinstance(info, dict) else {}
        row = {
            "user_id": user_id,
            "github_installation_id": str(installation_id),
            "github_account_login": account.get("login") if isinstance(account, dict) else None,
            "github_account_type": account.get("type") if isinstance(account, dict) else None,
            "permissions": info.get("permissions") if isinstance(info, dict) else {},
            "repository_selection": info.get("repository_selection") if isinstance(info, dict) else None,
            "status": "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return self.repository.upsert_installation(row)

    def disconnect(self, user_id: str, installation_id: str) -> bool:
        if not self.repository:
            return False
        return self.repository.disconnect(user_id, installation_id)

    async def list_repositories(self, limit: int = 50, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        installation = self.repository.get_active_for_user(user_id) if user_id and self.repository and self.app_configured() else None
        if installation:
            data = await self._get("/installation/repositories", {"per_page": min(max(limit, 1), 100)}, user_id=user_id)
            repos = data.get("repositories", []) if isinstance(data, dict) else []
        else:
            data = await self._get("/user/repos", {"per_page": min(max(limit, 1), 100), "sort": "updated"}, user_id=user_id)
            repos = data if isinstance(data, list) else []
        return [
            {
                "id": repo.get("id"),
                "full_name": repo.get("full_name"),
                "private": bool(repo.get("private")),
                "default_branch": repo.get("default_branch") or "main",
                "updated_at": repo.get("updated_at"),
            }
            for repo in repos
        ]

    async def list_tree(self, owner: str, repo: str, ref: str = "main", path: str = "", user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query_path = f"/repos/{owner}/{repo}/contents/{path.strip('/')}" if path else f"/repos/{owner}/{repo}/contents"
        data = await self._get(query_path, {"ref": ref}, user_id=user_id)
        items = data if isinstance(data, list) else [data]
        return [
            {
                "name": item.get("name"),
                "path": item.get("path"),
                "type": item.get("type"),
                "size": item.get("size"),
                "sha": item.get("sha"),
            }
            for item in items
        ]

    async def read_file(self, owner: str, repo: str, path: str, ref: str = "main", user_id: Optional[str] = None) -> Dict[str, Any]:
        data = await self._get(f"/repos/{owner}/{repo}/contents/{path.strip('/')}", {"ref": ref}, user_id=user_id)
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubRequestError(400, f"GitHub path is not a file: {path}")
        encoding = data.get("encoding")
        content = data.get("content") or ""
        if encoding == "base64":
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        else:
            text = str(content)
        return {
            "path": data.get("path") or path,
            "name": data.get("name") or path.rsplit("/", 1)[-1],
            "sha": data.get("sha"),
            "size": data.get("size"),
            "content": text,
        }

    async def read_files(self, owner: str, repo: str, paths: List[str], ref: str = "main", user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not paths:
            raise GitHubRequestError(400, "at least one file path is required")
        if len(paths) > 20:
            raise GitHubRequestError(400, "import up to 20 files at a time")
        files = []
        for path in paths:
            files.append(await self.read_file(owner, repo, path, ref=ref, user_id=user_id))
        return files
