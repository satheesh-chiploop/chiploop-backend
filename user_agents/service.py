from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent_capabilities import AGENT_CAPABILITIES

from .models import PrivateAgent, PrivateAgentPayload
from .repository import UserAgentRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserAgentService:
    def __init__(self, repository: UserAgentRepository):
        self.repository = repository

    def list_my_agents(self, user_id: str) -> List[Dict[str, Any]]:
        return [PrivateAgent.from_row(row).to_dict() for row in self.repository.list_by_owner(user_id)]

    def save_private_agent(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = PrivateAgentPayload.from_dict(payload)
        row = request.to_agent_row(user_id)
        saved = self.repository.insert_private(row)
        return PrivateAgent.from_row(saved).to_dict()

    def delete_my_agent(self, user_id: str, agent_id: str) -> bool:
        return self.repository.delete_owned(user_id, agent_id)

    def submit_my_agent(self, user_id: str, agent_id: str) -> Dict[str, Any]:
        agent = self.repository.get_owned(user_id, agent_id)
        if not agent:
            return {"ok": False, "reason": "not_found"}

        patch = {
            "status": "submitted",
            "visibility": "private",
            "submitted_at": _now(),
            "updated_at": _now(),
        }
        updated = self.repository.update_owned(user_id, agent_id, patch)
        updated_agent = updated or {**agent, **patch}

        submission_created = False
        submission_error = None
        try:
            self.repository.create_marketplace_submission(
                {
                    "agent_id": agent_id,
                    "submitted_by": user_id,
                    "agent_json": updated_agent,
                    "workflow_json": None,
                    "status": "pending",
                }
            )
            submission_created = True
        except Exception as exc:
            submission_error = str(exc)

        return {
            "ok": True,
            "agent": PrivateAgent.from_row(updated_agent).to_dict(),
            "marketplace_submission_created": submission_created,
            "marketplace_submission_error": submission_error,
        }

    def effective_agent_catalog(
        self,
        user_id: Optional[str],
        platform_agents: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        return build_effective_agent_catalog(self.repository, user_id, platform_agents=platform_agents)


def _platform_catalog(platform_agents: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    source = platform_agents if platform_agents is not None else AGENT_CAPABILITIES
    catalog: Dict[str, Dict[str, Any]] = {}
    for name, meta in source.items():
        catalog[str(name)] = {
            **(meta if isinstance(meta, dict) else {}),
            "agent_name": str(name),
            "visibility": "global",
            "status": "approved",
            "source": "platform",
        }
    return catalog


def _agent_row_to_catalog_item(row: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    name = str(row.get("agent_name") or row.get("name") or "")
    return {
        "agent_name": name,
        "loop_type": row.get("loop_type") or "system",
        "description": row.get("description") or "",
        "script_path": row.get("script_path"),
        "visibility": row.get("visibility") or ("marketplace" if row.get("is_marketplace") else "private"),
        "status": row.get("status") or ("approved" if row.get("is_prebuilt") else "private"),
        "owner_id": row.get("owner_id"),
        "is_custom": bool(row.get("is_custom")),
        "is_prebuilt": bool(row.get("is_prebuilt")),
        "is_marketplace": bool(row.get("is_marketplace")),
        "source": source,
        "agent_spec": row.get("agent_spec") or {},
        "skills": row.get("skills") or [],
        "tools": row.get("tools") or [],
        "hooks": row.get("hooks") or [],
    }


def build_effective_agent_catalog(
    repository: UserAgentRepository,
    user_id: Optional[str],
    *,
    platform_agents: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    catalog = _platform_catalog(platform_agents)

    for row in repository.list_global_visible():
        name = str(row.get("agent_name") or "")
        if name:
            catalog[name] = _agent_row_to_catalog_item(row, source="global")

    if user_id and user_id != "anonymous":
        for row in repository.list_by_owner(user_id):
            name = str(row.get("agent_name") or "")
            if name:
                catalog[name] = _agent_row_to_catalog_item(row, source="private")

    return catalog
