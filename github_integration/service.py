import base64
import os
from typing import Any, Dict, List, Optional

import httpx


class GitHubNotConfiguredError(Exception):
    pass


class GitHubRequestError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class GitHubIntegrationService:
    def __init__(self, token: Optional[str] = None, api_base: str = "https://api.github.com"):
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_APP_INSTALLATION_TOKEN")
        self.app_slug = os.getenv("GITHUB_APP_SLUG", "")
        self.api_base = api_base.rstrip("/")

    def status(self) -> Dict[str, Any]:
        configured = bool(self.token)
        return {
            "configured": configured,
            "auth_mode": "token" if configured else None,
            "connect_url": f"https://github.com/apps/{self.app_slug}/installations/new" if self.app_slug else None,
            "message": None if configured else "GitHub import is not configured. Set GITHUB_TOKEN or GitHub App installation credentials.",
        }

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            raise GitHubNotConfiguredError("github_not_configured")
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any] | List[Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{self.api_base}{path}", headers=self._headers(), params=params or {})
        if response.status_code >= 400:
            try:
                body = response.json()
                message = str(body.get("message") or response.text)
            except Exception:
                message = response.text
            raise GitHubRequestError(response.status_code, message)
        return response.json()

    async def list_repositories(self, limit: int = 50) -> List[Dict[str, Any]]:
        data = await self._get("/user/repos", {"per_page": min(max(limit, 1), 100), "sort": "updated"})
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

    async def list_tree(self, owner: str, repo: str, ref: str = "main", path: str = "") -> List[Dict[str, Any]]:
        query_path = f"/repos/{owner}/{repo}/contents/{path.strip('/')}" if path else f"/repos/{owner}/{repo}/contents"
        data = await self._get(query_path, {"ref": ref})
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

    async def read_file(self, owner: str, repo: str, path: str, ref: str = "main") -> Dict[str, Any]:
        data = await self._get(f"/repos/{owner}/{repo}/contents/{path.strip('/')}", {"ref": ref})
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

    async def read_files(self, owner: str, repo: str, paths: List[str], ref: str = "main") -> List[Dict[str, Any]]:
        if not paths:
            raise GitHubRequestError(400, "at least one file path is required")
        if len(paths) > 20:
            raise GitHubRequestError(400, "import up to 20 files at a time")
        files = []
        for path in paths:
            files.append(await self.read_file(owner, repo, path, ref=ref))
        return files
