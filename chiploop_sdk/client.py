import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from .errors import ChipLoopAPIError, ChipLoopConfigError
from .models import WorkflowRun, WorkflowStatus


class ChipLoopClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url or os.getenv("CHIPLOOP_BASE_URL") or "").rstrip("/")
        if not self.base_url:
            raise ChipLoopConfigError("ChipLoop base_url is required. Set CHIPLOOP_BASE_URL or pass base_url.")
        self.api_key = api_key if api_key is not None else os.getenv("CHIPLOOP_API_KEY")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _headers(self) -> Dict[str, str]:
        headers = {"User-Agent": "chiploop-python-sdk/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._headers())
        response = self.session.request(method, self._url(path), timeout=self.timeout, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise ChipLoopAPIError(
                f"ChipLoop API error {response.status_code}: {response.text}",
                status_code=response.status_code,
                response_text=response.text,
            )
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        try:
            return response.json()
        except Exception:
            return response.content

    def run_workflow(
        self,
        workflow: str | Dict[str, Any],
        *,
        spec_path: Optional[str] = None,
        spec_text: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
    ) -> WorkflowRun:
        if spec_path and spec_text:
            raise ChipLoopConfigError("Use only one of spec_path or spec_text.")

        payload = dict(workflow) if isinstance(workflow, dict) else {"name": workflow, "loop_type": workflow}
        if inputs:
            payload.update(inputs)

        data = {"workflow": json.dumps(payload)}
        if spec_text:
            data["spec_text"] = spec_text

        files = None
        handle = None
        try:
            if spec_path:
                path = Path(spec_path)
                handle = path.open("rb")
                files = {"file": (path.name, handle)}
            result = self._request("POST", "/run_workflow", data=data, files=files)
            return WorkflowRun.from_dict(result)
        finally:
            if handle:
                handle.close()

    def get_workflow_status(self, workflow_id: str) -> WorkflowStatus:
        return WorkflowStatus.from_dict(self._request("GET", f"/sdk/workflows/{workflow_id}/status"))

    def list_artifacts(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/list_artifacts/{workflow_id}")

    def download_artifact(self, workflow_id: str, artifact_name: str, output_dir: str) -> Path:
        target = _safe_artifact_target(output_dir, artifact_name)
        encoded = quote(artifact_name.replace("\\", "/"), safe="/")
        content = self._request("GET", f"/download_artifacts/{workflow_id}/{encoded}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            payload = content
        elif isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            payload = bytes(content)
        target.write_bytes(payload)
        return target

    def list_agents(self) -> Dict[str, Any]:
        return self._request("GET", "/sdk/agents")

    def list_workflows(self) -> Dict[str, Any]:
        return self._request("GET", "/sdk/workflows")

    def plan_agent(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/sdk/studio/plan-agent", json=request)

    def generate_agent(self, request: Dict[str, Any], *, dry_run: bool = True) -> Dict[str, Any]:
        return self._request("POST", "/sdk/studio/generate-agent", json={"request": request, "dry_run": dry_run})


def _safe_artifact_target(output_dir: str, artifact_name: str) -> Path:
    if not artifact_name or Path(artifact_name).is_absolute():
        raise ChipLoopConfigError("artifact_name must be a relative path")
    root = Path(output_dir).resolve()
    target = (root / artifact_name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ChipLoopConfigError("artifact_name cannot escape output_dir") from exc
    return target
