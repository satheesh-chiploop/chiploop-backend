from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from auth_api_keys.service import get_api_key_service
from browser_auth import BrowserUser, require_browser_user
from studio_contract.registry import load_registry
from studio_factory.generate_agent import run_factory as run_studio_factory
from studio_factory.models import AgentFactoryRequest
from studio_planner.models import AgentPlanRequest
from studio_planner.planner import plan_agent as plan_studio_agent
from user_agents.repository import SupabaseUserAgentRepository
from user_agents.service import UserAgentService
from workflow_dag.models import WorkflowDAG
from workflow_dag.planner import dag_from_agents, dag_from_studio_graph, dry_run_plan
from workflow_dag.validator import validate_dag


router = APIRouter()


def _api_key_service(request: Request):
    return getattr(request.app.state, "api_key_service", None) or get_api_key_service(
        getattr(request.app.state, "supabase", None)
    )


def _user_agent_service(request: Request) -> UserAgentService:
    existing = getattr(request.app.state, "user_agent_service", None)
    if existing is not None:
        return existing
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="user_agent_store_unavailable")
    return UserAgentService(SupabaseUserAgentRepository(supabase))


def _registry_counts(registry_dir: str = "registry") -> Dict[str, int]:
    registry = load_registry(registry_dir)
    return {
        "agent_count": len(registry.agents),
        "workflow_count": len(registry.workflows),
        "skill_count": len(registry.skills),
        "tool_count": len(registry.tools),
        "hook_count": len(registry.hooks),
        "command_count": len(registry.commands),
    }


def _dag_from_payload(data: Dict[str, Any]) -> WorkflowDAG:
    if isinstance(data.get("dag"), dict):
        return WorkflowDAG.from_dict(data["dag"])
    if isinstance(data.get("graph"), dict):
        return dag_from_studio_graph(data["graph"], loop_type=str(data.get("loop_type") or "digital"))
    if data.get("agents"):
        return dag_from_agents(
            [str(agent) for agent in data.get("agents") or []],
            loop_type=str(data.get("loop_type") or "digital"),
            metadata=dict(data.get("metadata") or {}),
            infer_parallel=bool(data.get("infer_parallel", True)),
        )
    return WorkflowDAG.from_dict(data)


@router.get("/studio/registry/summary")
def studio_registry_summary(_: BrowserUser = Depends(require_browser_user)):
    return {"status": "ok", **_registry_counts()}


@router.post("/studio/agent-planner/plan")
async def studio_agent_planner_plan(
    request: Request,
    _: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    result = plan_studio_agent(AgentPlanRequest.from_dict(data))
    return {"status": "ok", "result": result.to_dict()}


@router.post("/studio/agent-factory/dry-run")
async def studio_agent_factory_dry_run(
    request: Request,
    _: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    request_data = data.get("request") if isinstance(data.get("request"), dict) else data
    result = run_studio_factory(AgentFactoryRequest.from_dict(request_data), dry_run=True)
    response = result.to_dict()
    response["dry_run"] = True
    response["written_files"] = []
    return {"status": "ok", "result": response}


@router.post("/studio/dag/preview")
async def studio_dag_preview(
    request: Request,
    _: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    dag = _dag_from_payload(data)
    ok, errors = validate_dag(dag)
    preview = dry_run_plan(dag)
    return {
        "status": "ok",
        "valid": ok,
        "warnings": [],
        "errors": errors,
        "dag": dag.to_dict(),
        "preview": preview,
    }


@router.post("/studio/dag/validate")
async def studio_dag_validate(
    request: Request,
    _: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    dag = _dag_from_payload(data)
    ok, errors = validate_dag(dag)
    return {"status": "ok", "valid": ok, "errors": errors, "warnings": []}


@router.get("/studio/user-agents")
def studio_list_user_agents(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    service = _user_agent_service(request)
    return {"status": "ok", "agents": service.list_my_agents(user.user_id)}


@router.post("/studio/user-agents")
async def studio_save_user_agent(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    payload = data.get("agent") if isinstance(data.get("agent"), dict) else data
    service = _user_agent_service(request)
    try:
        agent = service.save_private_agent(user.user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "agent": agent}


@router.delete("/studio/user-agents/{agent_id}")
def studio_delete_user_agent(
    agent_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    service = _user_agent_service(request)
    if not service.delete_my_agent(user.user_id, agent_id):
        raise HTTPException(status_code=404, detail="agent_not_found")
    return {"status": "ok", "deleted": 1, "agent_id": agent_id}


@router.post("/studio/user-agents/{agent_id}/submit")
def studio_submit_user_agent(
    agent_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    service = _user_agent_service(request)
    result = service.submit_my_agent(user.user_id, agent_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="agent_not_found")
    return {"status": "ok", **result}


@router.get("/settings/api-keys")
def settings_list_api_keys(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    service = _api_key_service(request)
    return {"status": "ok", "api_keys": service.list_key_metadata(user.user_id)}


@router.post("/settings/api-keys")
async def settings_create_api_key(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    name = str(data.get("name") or "Browser key").strip() or "Browser key"
    environment = str(data.get("environment") or data.get("mode") or "test").lower()
    test = environment != "live"
    service = _api_key_service(request)
    raw_key, record = service.create_key(user.user_id, name, test=test)
    return {
        "status": "ok",
        "api_key": raw_key,
        "api_key_metadata": {
            "id": record.id,
            "key_prefix": record.key_prefix,
            "name": record.name,
            "created_at": record.created_at,
            "last_used_at": record.last_used_at,
            "revoked_at": record.revoked_at,
        },
    }


@router.post("/settings/api-keys/{key_id}/revoke")
def settings_revoke_api_key(
    key_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    service = _api_key_service(request)
    if not service.revoke_key(key_id, user.user_id):
        raise HTTPException(status_code=404, detail="api_key_not_found")
    return {"status": "ok", "revoked": True, "id": key_id}


@router.get("/settings/usage")
def settings_usage(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    service = _api_key_service(request)
    return {"status": "ok", "usage": service.usage_summary(user.user_id)}
