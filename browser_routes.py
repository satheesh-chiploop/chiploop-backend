import logging
import os
import json
import asyncio
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from auth_api_keys.service import get_api_key_service
from artifact_policy import artifact_policy_summary
from billing import BillingPaymentRequired, CreditLimitExceeded, EntitlementDenied, TrialCheckoutRequired, get_billing_service
from browser_auth import BrowserUser, browser_user_email, is_browser_admin, require_browser_user
from deployment_modes import deployment_summary
from github_integration import GitHubIntegrationService, GitHubNotConfiguredError, GitHubRequestError, SupabaseGitHubInstallationRepository
from help_center import answer_help_question
from license_policy import license_summary
from marketplace import MarketplaceService, SupabaseMarketplaceRepository
from model_gateway import complete_text, model_profile_summary
from onboarding import OnboardingService, SupabaseOnboardingRepository
from platform_services import platform_services_summary
from studio_contract.registry import load_registry
from studio_factory.generate_agent import run_factory as run_studio_factory
from studio_factory.models import AgentFactoryRequest
from studio_planner.models import AgentPlanRequest
from studio_planner.planner import plan_agent as plan_studio_agent
from stripe_billing import StripeBillingConfigError, StripeBillingService
from user_agents.repository import SupabaseUserAgentRepository
from user_agents.service import UserAgentService
from webinar import SupabaseWebinarRegistrationRepository, WebinarRegistrationError, WebinarRegistrationService
from workshop import SupabaseWorkshopRegistrationRepository, WorkshopRegistrationError, WorkshopService
from voice_design import VoiceDesignConfigError, build_spec_draft, transcribe_audio_bytes
from workflow_dag.models import WorkflowDAG
from workflow_dag.planner import dag_from_agents, dag_from_studio_graph, dry_run_plan
from workflow_dag.validator import validate_dag
from tooling.adapters import list_adapters
from tooling.profiles import profile_diagnostics
from tooling.runner import run_tool_diagnostics


router = APIRouter()
logger = logging.getLogger("chiploop.billing")
ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")
ASK_THIS_RUN_MAX_LOG_CHARS = int(os.getenv("ASK_THIS_RUN_MAX_LOG_CHARS", "8000"))
ASK_THIS_RUN_MAX_ARTIFACT_CHARS = int(os.getenv("ASK_THIS_RUN_MAX_ARTIFACT_CHARS", "6000"))
ASK_THIS_RUN_MAX_CONTEXT_CHARS = int(os.getenv("ASK_THIS_RUN_MAX_CONTEXT_CHARS", "24000"))


def _iter_leaf_strings(obj: Any):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_leaf_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_leaf_strings(value)
    elif isinstance(obj, str):
        value = obj.strip()
        if value:
            yield value


def _normalize_storage_path(path: str) -> str:
    path = path.strip()
    if path.startswith("/artifacts/anonymous/"):
        return path[len("/artifacts/anonymous/") :]
    if path.startswith("/artifacts/"):
        return path[len("/artifacts/") :]
    if path.startswith("/"):
        return path[1:]
    return path


def _is_text_artifact(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith((
        ".txt",
        ".md",
        ".json",
        ".log",
        ".csv",
        ".yaml",
        ".yml",
        ".sv",
        ".v",
        ".py",
        ".sdc",
        ".upf",
        ".rpt",
    ))


def _safe_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[TRUNCATED]"


def _download_text_artifact(supabase: Any, path: str) -> str:
    data = supabase.storage.from_(ARTIFACT_BUCKET).download(path)
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    if isinstance(data, bytearray):
        return bytes(data).decode("utf-8", errors="replace")
    return str(data or "")


def _inspection_probe_paths(workflow: Dict[str, Any]) -> list[str]:
    workflow_id = str(workflow.get("id") or "").strip()
    if not workflow_id:
        return []
    prefix = f"backend/workflows/{workflow_id}"
    return [
        f"{prefix}/digital/upf/upf_static_check.json",
        f"{prefix}/digital/upf/upf_static_check.md",
        f"{prefix}/digital/upf/upf_unsupported_commands.txt",
        f"{prefix}/digital/upf/logs/openroad_read_upf.log",
        f"{prefix}/digital/upf/logs/private_upf_static_check.log",
        f"{prefix}/digital/synth/synth_summary.json",
        f"{prefix}/digital/synth/synth_summary.md",
        f"{prefix}/digital/lec/lec_summary.json",
        f"{prefix}/digital/dft/scan_summary.json",
        f"{prefix}/digital/atpg/atpg_summary.json",
    ]


def _collect_run_inspection_context(supabase: Any, workflow: Dict[str, Any]) -> tuple[str, list[Dict[str, str]]]:
    sources: list[Dict[str, str]] = []
    sections: list[str] = [
        "RUN METADATA",
        _safe_text(
            {
                "id": workflow.get("id"),
                "name": workflow.get("name"),
                "status": workflow.get("status"),
                "loop_type": workflow.get("loop_type"),
                "created_at": workflow.get("created_at"),
            },
            2000,
        ),
    ]

    logs = _safe_text(workflow.get("logs") or "", ASK_THIS_RUN_MAX_LOG_CHARS)
    if logs:
        sections.extend(["WORKFLOW LOGS", logs])
        sources.append({"type": "logs", "path": "workflows.logs"})

    artifacts = workflow.get("artifacts") or {}
    artifact_index = _safe_text(artifacts, 5000)
    if artifact_index:
        sections.extend(["ARTIFACT INDEX", artifact_index])
        sources.append({"type": "artifact_index", "path": "workflows.artifacts"})

    seen: set[str] = set()
    for raw_path in list(_iter_leaf_strings(artifacts)) + _inspection_probe_paths(workflow):
        path = _normalize_storage_path(raw_path)
        if not path or path in seen or not _is_text_artifact(path):
            continue
        seen.add(path)
        try:
            content = _safe_text(_download_text_artifact(supabase, path), ASK_THIS_RUN_MAX_ARTIFACT_CHARS)
        except Exception as exc:
            logger.info("ask_this_run: skipped artifact %s: %s", path, exc)
            continue
        if not content:
            continue
        sections.extend([f"ARTIFACT: {path}", content])
        sources.append({"type": "artifact", "path": path})
        if len("\n\n".join(sections)) >= ASK_THIS_RUN_MAX_CONTEXT_CHARS:
            break

    context = "\n\n".join(sections)
    if len(context) > ASK_THIS_RUN_MAX_CONTEXT_CHARS:
        context = context[:ASK_THIS_RUN_MAX_CONTEXT_CHARS] + "\n[CONTEXT TRUNCATED]"
    return context, sources


async def _run_inspection_llm(prompt: str) -> str:
    from model_gateway import complete_text

    try:
        return await asyncio.to_thread(
            complete_text,
            prompt,
            capability="inspection",
            agent_name="Ask This Run Inspector",
        )
    except Exception as exc:
        logger.warning("ask_this_run: inspection LLM unavailable: %s", exc)
        return (
            "I could not reach the inspection model for this request. "
            "The run context was collected successfully, so use Download ZIP or the listed run artifacts/logs "
            "to inspect the generated outputs while the model service is retried."
        )


def _api_key_service(request: Request):
    return getattr(request.app.state, "api_key_service", None) or get_api_key_service(
        getattr(request.app.state, "supabase", None)
    )


def _billing_service(request: Request):
    return getattr(request.app.state, "billing_service", None) or get_billing_service(
        getattr(request.app.state, "supabase", None)
    )


def _stripe_billing_service(request: Request) -> StripeBillingService:
    existing = getattr(request.app.state, "stripe_billing_service", None)
    if existing is not None:
        return existing
    return StripeBillingService(_billing_service(request).repository)


def _stripe_error_response(exc: Exception) -> Dict[str, Any]:
    user_message = getattr(exc, "user_message", None)
    message = user_message or str(exc) or type(exc).__name__
    response: Dict[str, Any] = {
        "error": "stripe_checkout_failed",
        "type": type(exc).__name__,
        "message": message,
    }
    code = getattr(exc, "code", None)
    param = getattr(exc, "param", None)
    request_id = getattr(exc, "request_id", None)
    if code:
        response["code"] = code
    if param:
        response["param"] = param
    if request_id:
        response["request_id"] = request_id
    return response


def _checkout_trial_requested(data: Dict[str, Any]) -> bool:
    return data.get("trial") is True or data.get("checkout_kind") == "trial"


@router.post("/help/ask")
async def ask_chiploop_help(request: Request, _: BrowserUser = Depends(require_browser_user)):
    data = await request.json()
    question = str(data.get("question") or "").strip()
    try:
        return answer_help_question(question)
    except ValueError:
        raise HTTPException(status_code=400, detail="question_too_short")


@router.get("/help/catalog")
def help_catalog(_: BrowserUser = Depends(require_browser_user)):
    registry = load_registry()
    agents = [
        {
            "type": "agent",
            "name": agent.name,
            "loop_type": agent.loop_type,
            "domain": agent.domain,
            "description": agent.description,
            "steps": None,
        }
        for agent in registry.agents.values()
    ]
    workflows = [
        {
            "type": "workflow",
            "name": workflow.name,
            "loop_type": workflow.loop_type,
            "domain": "workflow",
            "description": workflow.description,
            "steps": len(workflow.agents),
        }
        for workflow in registry.workflows.values()
    ]
    agents.sort(key=lambda row: (row["loop_type"], row["name"]))
    workflows.sort(key=lambda row: (row["loop_type"], row["name"]))
    agent_loop_counts = Counter(row["loop_type"] or "unknown" for row in agents)
    workflow_loop_counts = Counter(row["loop_type"] or "unknown" for row in workflows)
    return {
        "status": "ok",
        "counts": {
            "agents": len(agents),
            "workflows": len(workflows),
            "agents_by_loop": dict(sorted(agent_loop_counts.items())),
            "workflows_by_loop": dict(sorted(workflow_loop_counts.items())),
        },
        "rows": [*agents, *workflows],
    }


def _trial_checkout_detail(message: str) -> Dict[str, Any]:
    return {
        "error": "trial_checkout_required",
        "message": message,
        "requires_checkout": True,
        "checkout_plan_id": "starter",
        "checkout_url": "/pricing?trial=1",
        "checkout_label": "Start 3-day trial",
    }


def _enforce_feature(request: Request, user_id: str, feature: str):
    user = getattr(request.state, "browser_user", None)
    if user and is_browser_admin(user):
        return None
    service = _billing_service(request)
    try:
        service.assert_checkout_started(user_id)
        return service.assert_entitlement(user_id, feature)
    except TrialCheckoutRequired:
        raise HTTPException(
            status_code=402,
            detail=_trial_checkout_detail("Start your 3-day trial to use this Studio feature."),
        )
    except BillingPaymentRequired as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_required",
                "billing_status": exc.billing_status,
                "grace_period_end_at": exc.grace_period_end_at,
            },
        )
    except EntitlementDenied:
        raise HTTPException(status_code=403, detail=f"{feature}_not_enabled")


def _deduct(request: Request, user_id: str, action_type: str, *, reference_id: str | None = None):
    user = getattr(request.state, "browser_user", None)
    if user and is_browser_admin(user):
        return {"monthly_credits": None, "credits_used": 0, "credits_remaining": None, "deducted": 0}
    service = _billing_service(request)
    try:
        return service.deduct_credits(user_id, action_type, reference_id=reference_id)
    except BillingPaymentRequired as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_required",
                "billing_status": exc.billing_status,
                "grace_period_end_at": exc.grace_period_end_at,
            },
        )
    except CreditLimitExceeded as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_credits",
                "required": exc.required,
                "remaining": exc.remaining,
            },
        )


def _upgrade_hint(request: Request, user_id: str, *, reason: str | None = None):
    user = getattr(request.state, "browser_user", None)
    if user and is_browser_admin(user):
        return None
    return _billing_service(request).upgrade_status(user_id, reason=reason).get("suggested_upgrade")


def _with_upgrade(payload: Dict[str, Any], request: Request, user_id: str, *, reason: str | None = None) -> Dict[str, Any]:
    upgrade = _upgrade_hint(request, user_id, reason=reason)
    return {**payload, "upgrade_hint": upgrade, "upgrade": upgrade}


def _require_checkout(request: Request, user_id: str):
    user = getattr(request.state, "browser_user", None)
    if user and is_browser_admin(user):
        return
    try:
        _billing_service(request).assert_checkout_started(user_id)
    except TrialCheckoutRequired:
        raise HTTPException(
            status_code=402,
            detail=_trial_checkout_detail("Start your 3-day trial to use voice design sessions."),
        )
    except BillingPaymentRequired as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_required",
                "billing_status": exc.billing_status,
                "grace_period_end_at": exc.grace_period_end_at,
            },
        )


def _user_agent_service(request: Request) -> UserAgentService:
    existing = getattr(request.app.state, "user_agent_service", None)
    if existing is not None:
        return existing
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="user_agent_store_unavailable")
    return UserAgentService(SupabaseUserAgentRepository(supabase))


def _marketplace_service(request: Request) -> MarketplaceService:
    existing = getattr(request.app.state, "marketplace_service", None)
    if existing is not None:
        return existing
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="marketplace_store_unavailable")
    return MarketplaceService(SupabaseMarketplaceRepository(supabase), _user_agent_service(request))


USER_APP_SELECT_COLUMNS = (
    "id,owner_id,workflow_id,workflow_name,name,slug,description,category,loop_type,"
    "input_schema,output_schema,default_config,app_config,visibility,status,"
    "marketplace_status,price_usd,submitted_at,reviewed_at,reviewed_by,review_notes,"
    "created_at,updated_at"
)


def _user_app_store(request: Request):
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="user_app_store_unavailable")
    return supabase


def _app_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "studio-app"


def _owned_workflow_snapshot(supabase: Any, user_id: str, workflow_id: str) -> Dict[str, Any]:
    try:
        res = (
            supabase.table("workflows")
            .select("id,name,user_id,loop_type,definitions")
            .eq("id", workflow_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="workflow_not_found")
    row = (getattr(res, "data", None) or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="workflow_not_found")
    owner_id = str(row.get("user_id") or "")
    if owner_id and owner_id != user_id:
        raise HTTPException(status_code=403, detail="workflow_access_denied")
    return row


def _github_service(request: Request) -> GitHubIntegrationService:
    existing = getattr(request.app.state, "github_service", None)
    if existing is not None:
        return existing
    supabase = getattr(request.app.state, "supabase", None)
    repository = SupabaseGitHubInstallationRepository(supabase) if supabase is not None else None
    return GitHubIntegrationService(repository=repository)


def _github_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GitHubNotConfiguredError):
        return HTTPException(status_code=503, detail="github_not_configured")
    if isinstance(exc, GitHubRequestError):
        status = 400 if exc.status_code < 500 else 502
        return HTTPException(status_code=status, detail=str(exc))
    return HTTPException(status_code=502, detail=f"github_request_failed: {type(exc).__name__}")


def _require_admin(user: BrowserUser) -> None:
    admin_ids = {
        item.strip()
        for item in os.environ.get("CHIPLOOP_ADMIN_USER_IDS", "").split(",")
        if item.strip()
    }
    if is_browser_admin(user):
        return
    if admin_ids and user.user_id in admin_ids:
        return
    raise HTTPException(status_code=403, detail="admin_required")


def _admin_plan_summary(user: BrowserUser) -> Dict[str, Any]:
    entitlements = {
        "plan_id": "admin",
        "plan_name": "Administrator",
        "monthly_credits": None,
        "max_api_keys": 999,
        "max_private_agents": 999,
        "sdk_cli_enabled": True,
        "agent_planner_enabled": True,
        "agent_factory_dry_run_enabled": True,
        "private_agent_save_enabled": True,
        "dag_optimization_enabled": True,
        "marketplace_submit_enabled": True,
        "agent_factory_write_enabled": True,
        "higher_workflow_limits": True,
        "custom_limits": True,
    }
    return {
        "current_plan": {"id": "admin", "name": "Administrator", "display_name": "Administrator"},
        "plan_name": "Administrator",
        "base_price": "admin",
        "discounted_price": None,
        "price_monthly": None,
        "price": "admin",
        "credits": None,
        "monthly_credits": None,
        "credits_used": 0,
        "credits_remaining": None,
        "trial_days_remaining": None,
        "discount_months_remaining": 0,
        "entitlements": entitlements,
        "billing_status": "admin",
        "billing_blocked": False,
        "grace_period_end_at": None,
        "requires_checkout": False,
        "can_run_workflows": True,
        "trial": {"status": None, "days_remaining": None, "auto_renew": False, "converts_to": None},
        "suggested_upgrade": None,
        "upgrade_hint": None,
        "is_admin": True,
        "admin_email": browser_user_email(user),
    }


def _onboarding_service(request: Request) -> OnboardingService:
    existing = getattr(request.app.state, "onboarding_service", None)
    if existing is not None:
        return existing
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="onboarding_store_unavailable")
    return OnboardingService(SupabaseOnboardingRepository(supabase))


def _webinar_service(request: Request) -> WebinarRegistrationService:
    existing = getattr(request.app.state, "webinar_service", None)
    if existing is not None:
        return existing
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="webinar_registration_store_unavailable")
    return WebinarRegistrationService(SupabaseWebinarRegistrationRepository(supabase))


def _workshop_service(request: Request) -> WorkshopService:
    existing = getattr(request.app.state, "workshop_service", None)
    if existing is not None:
        return existing
    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="workshop_registration_store_unavailable")
    return WorkshopService(SupabaseWorkshopRegistrationRepository(supabase))


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


@router.post("/webinar/register")
async def webinar_register(request: Request):
    data = await request.json()
    try:
        registration = _webinar_service(request).register(data)
    except WebinarRegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "ok",
        "registration": {
            "id": registration.id,
            "preferred_session": registration.preferred_session,
            "created_at": registration.created_at,
        },
    }


@router.get("/webinar/sessions")
def webinar_sessions(request: Request):
    return {"status": "ok", "sessions": _webinar_service(request).sessions()}


@router.get("/workshop/batches")
def workshop_batches(request: Request):
    return {"status": "ok", "batches": _workshop_service(request).batches()}


@router.post("/workshop/checkout")
async def workshop_checkout(request: Request):
    data = await request.json()
    try:
        result = _workshop_service(request).create_checkout(data)
    except WorkshopRegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StripeBillingConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        service = _workshop_service(request)
        stripe_error_base = getattr(getattr(service.stripe, "error", None), "StripeError", None)
        if stripe_error_base and isinstance(exc, stripe_error_base):
            detail = _stripe_error_response(exc)
            raise HTTPException(status_code=502, detail=detail)
        raise HTTPException(status_code=502, detail=f"workshop_checkout_failed: {type(exc).__name__}")
    return {"status": "ok", **result}


@router.get("/workshop/registrations/{registration_id}")
def workshop_registration(registration_id: str, request: Request):
    registration = _workshop_service(request).get_registration(registration_id)
    if not registration:
        raise HTTPException(status_code=404, detail="workshop_registration_not_found")
    return {
        "status": "ok",
        "registration": {
            "id": registration.id,
            "email": registration.email,
            "batch_id": registration.batch_id,
            "payment_status": registration.status,
            "paid": registration.status == "paid",
        },
    }


@router.post("/workflow/{workflow_id}/ask")
async def ask_this_run(workflow_id: str, request: Request, user: BrowserUser = Depends(require_browser_user)):
    data = await request.json()
    question = str(data.get("question") or "").strip()
    if len(question) < 3:
        raise HTTPException(status_code=400, detail="question_required")
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="question_too_long")

    supabase = getattr(request.app.state, "supabase", None)
    if supabase is None:
        raise HTTPException(status_code=500, detail="workflow_store_unavailable")

    try:
        row = (
            supabase.table("workflows")
            .select("id,user_id,name,status,loop_type,created_at,logs,artifacts")
            .eq("id", workflow_id)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="workflow_not_found")

    workflow = getattr(row, "data", None) or {}
    if not workflow:
        raise HTTPException(status_code=404, detail="workflow_not_found")

    owner_id = str(workflow.get("user_id") or "")
    if owner_id and owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="workflow_access_denied")

    context, sources = _collect_run_inspection_context(supabase, workflow)
    if not context.strip():
        raise HTTPException(status_code=400, detail="run_context_empty")

    prompt = f"""
You are ChipLoop's Ask This Run inspector.

Answer the user's question using only the run context below. Be concise, technical, and explicit.
If the context does not contain enough evidence, say what is missing instead of guessing.
Reference source paths from the context when useful.

User question:
{question}

Run context:
{context}
""".strip()

    answer = (await _run_inspection_llm(prompt)).strip()
    if not answer:
        answer = (
            "I could not generate an inspection summary from the model for this request. "
            "The run context was collected, so download the ZIP or review the listed logs/artifacts "
            "to inspect generated outputs while the model service is retried."
        )

    return {
        "workflow_id": workflow_id,
        "question": question,
        "answer": answer,
        "sources": sources[:20],
        "source_count": len(sources),
    }


@router.get("/studio/registry/summary")
def studio_registry_summary(_: BrowserUser = Depends(require_browser_user)):
    return {"status": "ok", **_registry_counts()}


@router.post("/studio/agent-planner/plan")
async def studio_agent_planner_plan(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _enforce_feature(request, user.user_id, "agent_planner_enabled")
    _deduct(request, user.user_id, "studio_agent_planner")
    data = await request.json()
    result = plan_studio_agent(AgentPlanRequest.from_dict(data))
    return _with_upgrade({"status": "ok", "result": result.to_dict()}, request, user.user_id)


@router.post("/studio/agent-factory/dry-run")
async def studio_agent_factory_dry_run(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _enforce_feature(request, user.user_id, "agent_factory_dry_run_enabled")
    _deduct(request, user.user_id, "studio_agent_factory_dry_run")
    data = await request.json()
    request_data = data.get("request") if isinstance(data.get("request"), dict) else data
    result = run_studio_factory(AgentFactoryRequest.from_dict(request_data), dry_run=True)
    response = result.to_dict()
    response["dry_run"] = True
    response["written_files"] = []
    return _with_upgrade({"status": "ok", "result": response}, request, user.user_id)


@router.post("/studio/dag/preview")
async def studio_dag_preview(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _enforce_feature(request, user.user_id, "dag_optimization_enabled")
    _deduct(request, user.user_id, "dag_parallelism_analyze")
    data = await request.json()
    dag = _dag_from_payload(data)
    ok, errors = validate_dag(dag)
    preview = dry_run_plan(dag)
    return _with_upgrade({
        "status": "ok",
        "valid": ok,
        "warnings": [],
        "errors": errors,
        "dag": dag.to_dict(),
        "preview": preview,
    }, request, user.user_id)


@router.post("/studio/dag/validate")
async def studio_dag_validate(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _enforce_feature(request, user.user_id, "dag_optimization_enabled")
    _deduct(request, user.user_id, "dag_parallelism_analyze")
    data = await request.json()
    dag = _dag_from_payload(data)
    ok, errors = validate_dag(dag)
    return _with_upgrade({"status": "ok", "valid": ok, "errors": errors, "warnings": []}, request, user.user_id)


@router.post("/studio/voice/transcribe")
async def studio_voice_transcribe(
    request: Request,
    file: UploadFile = File(...),
    user: BrowserUser = Depends(require_browser_user),
):
    _require_checkout(request, user.user_id)
    try:
        transcript = transcribe_audio_bytes(await file.read(), filename=file.filename or "voice.webm")
    except VoiceDesignConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _with_upgrade({"status": "ok", "transcript": transcript}, request, user.user_id)


@router.post("/studio/voice/spec-draft")
async def studio_voice_spec_draft(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_checkout(request, user.user_id)
    data = await request.json()
    transcripts = data.get("transcripts") or []
    if not isinstance(transcripts, list):
        raise HTTPException(status_code=400, detail="transcripts_must_be_list")
    try:
        draft = await build_spec_draft(
            [str(item) for item in transcripts],
            loop_type=str(data.get("loop_type") or "digital"),
            target=str(data.get("target") or "Arch2RTL"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _deduct(request, user.user_id, "studio_agent_planner")
    return _with_upgrade({"status": "ok", "spec_draft": draft}, request, user.user_id)


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
    billing = _billing_service(request)
    service = _user_agent_service(request)
    if not is_browser_admin(user):
        try:
            entitlements = billing.assert_entitlement(user.user_id, "private_agent_save_enabled")
            billing.assert_private_agent_limit(user.user_id)
            if len(service.list_my_agents(user.user_id)) >= entitlements.max_private_agents:
                raise EntitlementDenied("max_private_agents")
            billing.deduct_credits(user.user_id, "private_agent_save")
        except EntitlementDenied as exc:
            raise HTTPException(status_code=403, detail=f"{exc.feature}_not_enabled")
        except CreditLimitExceeded as exc:
            raise HTTPException(
                status_code=402,
                detail={"error": "insufficient_credits", "required": exc.required, "remaining": exc.remaining},
            )
    data = await request.json()
    payload = data.get("agent") if isinstance(data.get("agent"), dict) else data
    try:
        agent = service.save_private_agent(user.user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _with_upgrade({"status": "ok", "agent": agent}, request, user.user_id)


@router.patch("/studio/user-agents/{agent_id}")
async def studio_update_user_agent(
    agent_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    service = _user_agent_service(request)
    data = await request.json()
    payload = data.get("agent") if isinstance(data.get("agent"), dict) else data
    try:
        agent = service.update_private_agent(user.user_id, agent_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not agent:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return _with_upgrade({"status": "ok", "agent": agent}, request, user.user_id)


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
    _enforce_feature(request, user.user_id, "marketplace_submit_enabled")
    _deduct(request, user.user_id, "marketplace_submit", reference_id=agent_id)
    service = _user_agent_service(request)
    result = service.submit_my_agent(user.user_id, agent_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="agent_not_found")
    return _with_upgrade({"status": "ok", **result}, request, user.user_id)


@router.get("/studio/user-apps")
def studio_list_user_apps(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    supabase = _user_app_store(request)
    res = (
        supabase.table("user_apps")
        .select(USER_APP_SELECT_COLUMNS)
        .eq("owner_id", user.user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"status": "ok", "apps": getattr(res, "data", None) or []}


@router.post("/studio/user-apps")
async def studio_create_user_app(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_checkout(request, user.user_id)
    data = await request.json()
    payload = data.get("app") if isinstance(data.get("app"), dict) else data
    workflow_id = str(payload.get("workflow_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id_required")
    if not name:
        raise HTTPException(status_code=400, detail="app_name_required")

    supabase = _user_app_store(request)
    workflow = _owned_workflow_snapshot(supabase, user.user_id, workflow_id)
    workflow_name = str(payload.get("workflow_name") or workflow.get("name") or "").strip()
    loop_type = str(payload.get("loop_type") or workflow.get("loop_type") or "system").strip().lower()
    price_usd = payload.get("price_usd")
    if price_usd in ("", None):
        price_usd = None

    app_config = payload.get("app_config") if isinstance(payload.get("app_config"), dict) else {}
    if "workflow_snapshot" not in app_config:
        app_config = {**app_config, "workflow_snapshot": workflow}

    row = {
        "owner_id": user.user_id,
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "name": name,
        "slug": _app_slug(name),
        "description": str(payload.get("description") or "").strip(),
        "category": str(payload.get("category") or loop_type or "system").strip().lower(),
        "loop_type": loop_type,
        "input_schema": payload.get("input_schema") if isinstance(payload.get("input_schema"), dict) else {},
        "output_schema": payload.get("output_schema") if isinstance(payload.get("output_schema"), dict) else {},
        "default_config": payload.get("default_config") if isinstance(payload.get("default_config"), dict) else {},
        "app_config": app_config,
        "visibility": "private",
        "status": "private",
        "marketplace_status": "draft",
        "price_usd": price_usd,
    }
    try:
        res = supabase.table("user_apps").insert(row).execute()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"create_app_failed: {exc}")
    app = (getattr(res, "data", None) or [row])[0]
    return _with_upgrade({"status": "ok", "app": app}, request, user.user_id)


@router.patch("/studio/user-apps/{app_id}")
async def studio_update_user_app(
    app_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    supabase = _user_app_store(request)
    data = await request.json()
    payload = data.get("app") if isinstance(data.get("app"), dict) else data
    name = str(payload.get("name") or "").strip() if "name" in payload else None
    patch: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if name is not None:
        if not name:
            raise HTTPException(status_code=400, detail="app_name_required")
        patch["name"] = name
        patch["slug"] = _app_slug(name)
    for key in ("description", "category", "loop_type", "input_schema", "output_schema", "default_config", "app_config", "price_usd"):
        if key in payload:
            value = payload.get(key)
            if key in {"input_schema", "output_schema", "default_config", "app_config"} and not isinstance(value, dict):
                value = {}
            if key in {"category", "loop_type"} and value is not None:
                value = str(value).strip().lower()
            patch[key] = value
    try:
        res = (
            supabase.table("user_apps")
            .update(patch)
            .eq("id", app_id)
            .eq("owner_id", user.user_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"update_app_failed: {exc}")
    app = (getattr(res, "data", None) or [None])[0]
    if not app:
        raise HTTPException(status_code=404, detail="app_not_found")
    return _with_upgrade({"status": "ok", "app": app}, request, user.user_id)


@router.delete("/studio/user-apps/{app_id}")
def studio_delete_user_app(
    app_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    supabase = _user_app_store(request)
    try:
        existing = (
            supabase.table("user_apps")
            .select("id")
            .eq("id", app_id)
            .eq("owner_id", user.user_id)
            .limit(1)
            .execute()
        )
        if not (getattr(existing, "data", None) or []):
            raise HTTPException(status_code=404, detail="app_not_found")
        supabase.table("user_apps").delete().eq("id", app_id).eq("owner_id", user.user_id).execute()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"delete_app_failed: {exc}")
    return {"status": "ok", "deleted": 1, "app_id": app_id}


@router.post("/studio/user-apps/{app_id}/submit")
def studio_submit_user_app(
    app_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _enforce_feature(request, user.user_id, "marketplace_submit_enabled")
    _deduct(request, user.user_id, "marketplace_submit", reference_id=app_id)
    supabase = _user_app_store(request)
    existing_res = (
        supabase.table("user_apps")
        .select(USER_APP_SELECT_COLUMNS)
        .eq("id", app_id)
        .eq("owner_id", user.user_id)
        .limit(1)
        .execute()
    )
    app = (getattr(existing_res, "data", None) or [None])[0]
    if not app:
        raise HTTPException(status_code=404, detail="app_not_found")
    patch = {
        "status": "submitted",
        "visibility": "private",
        "marketplace_status": "pending",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    updated_res = (
        supabase.table("user_apps")
        .update(patch)
        .eq("id", app_id)
        .eq("owner_id", user.user_id)
        .execute()
    )
    updated_app = (getattr(updated_res, "data", None) or [{**app, **patch}])[0]
    submission_created = False
    submission_error = None
    try:
        supabase.table("marketplace_submissions").insert(
            {
                "agent_id": None,
                "submitted_by": user.user_id,
                "agent_json": None,
                "workflow_json": updated_app,
                "status": "pending",
            }
        ).execute()
        submission_created = True
    except Exception as exc:
        submission_error = str(exc)
    return _with_upgrade(
        {
            "status": "ok",
            "ok": True,
            "app": updated_app,
            "marketplace_submission_created": submission_created,
            "marketplace_submission_error": submission_error,
        },
        request,
        user.user_id,
    )


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
    billing = _billing_service(request)
    key_service = _api_key_service(request)
    if not is_browser_admin(user):
        try:
            entitlements = billing.assert_api_key_limit(user.user_id)
            active_keys = [
                key for key in key_service.list_key_metadata(user.user_id) if not key.get("revoked_at")
            ]
            if len(active_keys) >= entitlements.max_api_keys:
                raise EntitlementDenied("max_api_keys")
        except TrialCheckoutRequired:
            raise HTTPException(
                status_code=402,
                detail=_trial_checkout_detail("Start your 3-day trial before creating API keys."),
            )
        except BillingPaymentRequired as exc:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "payment_required",
                    "billing_status": exc.billing_status,
                    "grace_period_end_at": exc.grace_period_end_at,
                },
            )
        except EntitlementDenied as exc:
            raise HTTPException(status_code=403, detail=f"{exc.feature}_limit_reached")
    data = await request.json()
    name = str(data.get("name") or "Browser key").strip() or "Browser key"
    environment = str(data.get("environment") or data.get("mode") or "test").lower()
    test = environment != "live"
    raw_key, record = key_service.create_key(user.user_id, name, test=test)
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


@router.get("/settings/deployment")
def settings_deployment(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    return {
        "status": "ok",
        "deployment": deployment_summary(),
        "model_profile": model_profile_summary(),
        "tool_profile": profile_diagnostics(),
        "tool_adapters": list_adapters(),
        "artifact_policy": artifact_policy_summary(),
        "platform_services": platform_services_summary(),
        "license": license_summary(),
        "editable": is_browser_admin(user),
    }


@router.post("/settings/deployment/tool-diagnostics")
async def settings_deployment_tool_diagnostics(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    requested = data.get("tools") if isinstance(data.get("tools"), list) else None
    tools = [str(item) for item in requested] if requested else None
    return {
        "status": "ok",
        "diagnostics": run_tool_diagnostics(tools=tools),
        "requested_by": user.user_id,
    }


@router.post("/settings/deployment/model-diagnostics")
def settings_deployment_model_diagnostics(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_admin(user)
    text = complete_text(
        "Return exactly: CHIPLOOP_MODEL_OK",
        capability="inspection",
        agent_name="Deployment Model Diagnostics",
    )
    return {
        "status": "ok",
        "model_profile": model_profile_summary(),
        "response": text[:200],
    }


@router.get("/settings/deployment/support-bundle")
def settings_deployment_support_bundle(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_admin(user)
    return {
        "status": "ok",
        "support_bundle": {
            "deployment": deployment_summary(),
            "artifact_policy": artifact_policy_summary(),
            "platform_services": platform_services_summary(),
            "license": license_summary(),
            "model_profile": model_profile_summary(),
            "tool_diagnostics": run_tool_diagnostics(),
        },
    }


@router.get("/settings/github/status")
def settings_github_status(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    return {"status": "ok", "github": _github_service(request).status(user.user_id)}


@router.post("/settings/github/callback")
async def settings_github_callback(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    installation_id = str(data.get("installation_id") or "").strip()
    if not installation_id:
        raise HTTPException(status_code=400, detail="installation_id_required")
    try:
        installation = await _github_service(request).register_installation(user.user_id, installation_id)
    except Exception as exc:
        raise _github_error(exc)
    return {"status": "ok", "installation": installation, "github": _github_service(request).status(user.user_id)}


@router.post("/settings/github/disconnect")
async def settings_github_disconnect(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    installation_id = str(data.get("installation_id") or "").strip()
    if not installation_id:
        current = _github_service(request).status(user.user_id).get("installation") or {}
        installation_id = str(current.get("github_installation_id") or "")
    if not installation_id:
        raise HTTPException(status_code=400, detail="installation_id_required")
    disconnected = _github_service(request).disconnect(user.user_id, installation_id)
    return {"status": "ok", "disconnected": disconnected}


@router.get("/github/repos")
async def github_repos(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    try:
        repos = await _github_service(request).list_repositories(user_id=user.user_id)
    except Exception as exc:
        raise _github_error(exc)
    return {"status": "ok", "repos": repos}


@router.get("/github/tree")
async def github_tree(
    owner: str,
    repo: str,
    request: Request,
    ref: str = "main",
    path: str = "",
    user: BrowserUser = Depends(require_browser_user),
):
    try:
        tree = await _github_service(request).list_tree(owner, repo, ref=ref, path=path, user_id=user.user_id)
    except Exception as exc:
        raise _github_error(exc)
    return {"status": "ok", "tree": tree}


@router.post("/github/import")
async def github_import(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    owner = str(data.get("owner") or "").strip()
    repo = str(data.get("repo") or "").strip()
    ref = str(data.get("ref") or "main").strip() or "main"
    paths = [str(item).strip() for item in (data.get("paths") or []) if str(item).strip()]
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="owner_and_repo_required")
    try:
        files = await _github_service(request).read_files(owner, repo, paths, ref=ref, user_id=user.user_id)
    except Exception as exc:
        raise _github_error(exc)
    return {
        "status": "ok",
        "source": {"provider": "github", "owner": owner, "repo": repo, "ref": ref},
        "files": files,
        "combined_text": "\n\n".join(f"// {item['path']}\n{item['content']}" for item in files),
    }


@router.post("/github/export-plan")
async def github_export_plan(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    workflow_id = str(data.get("workflow_id") or "").strip()
    repo = str(data.get("repo") or "").strip()
    owner = str(data.get("owner") or "").strip()
    branch = str(data.get("branch") or f"chiploop/{workflow_id[:8] or 'export'}").strip()
    if not workflow_id or not owner or not repo:
        raise HTTPException(status_code=400, detail="workflow_id_owner_repo_required")
    return {
        "status": "ok",
        "export_plan": {
            "provider": "github",
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "workflow_id": workflow_id,
            "created_by": user.user_id,
            "next_steps": [
                "Create or update the export branch.",
                "Write selected ChipLoop artifacts to the branch.",
                "Open a pull request with the run summary.",
            ],
        },
    }


@router.get("/settings/plan")
def settings_plan(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    if is_browser_admin(user):
        return {"status": "ok", "plan": _admin_plan_summary(user)}
    return {"status": "ok", "plan": _billing_service(request).plan_summary(user.user_id)}


@router.get("/settings/upgrade-status")
def settings_upgrade_status(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    if is_browser_admin(user):
        return {"status": "ok", "upgrade_status": {**_admin_plan_summary(user), "suggested_upgrade": None}}
    return {"status": "ok", "upgrade_status": _billing_service(request).upgrade_status(user.user_id)}


@router.post("/settings/billing/checkout")
async def settings_billing_checkout(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    plan_id = str(data.get("plan_id") or "starter").lower()
    trial = _checkout_trial_requested(data)
    if trial:
        plan_id = "starter"
    service = _stripe_billing_service(request)
    try:
        result = service.create_checkout_session(
            user_id=user.user_id,
            user_email=str(user.claims.get("email") or "") or None,
            plan_id=plan_id,
            trial=trial,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StripeBillingConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        stripe_error_base = getattr(getattr(service.stripe, "error", None), "StripeError", None)
        if stripe_error_base and isinstance(exc, stripe_error_base):
            detail = _stripe_error_response(exc)
            logger.warning(
                "stripe_checkout_failed",
                extra={
                    "stripe_error_type": detail.get("type"),
                    "stripe_error_code": detail.get("code"),
                    "stripe_error_param": detail.get("param"),
                    "stripe_request_id": detail.get("request_id"),
                    "plan_id": plan_id,
                },
            )
            raise HTTPException(status_code=502, detail=detail)
        raise HTTPException(status_code=502, detail=f"stripe_checkout_failed: {type(exc).__name__}")
    return {"status": "ok", **result}


@router.post("/settings/billing/portal")
def settings_billing_portal(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    try:
        result = _stripe_billing_service(request).create_portal_session(user_id=user.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StripeBillingConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "ok", **result}


@router.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = _stripe_billing_service(request).construct_event(payload, signature)
        obj = (event.get("data") or {}).get("object") or {}
        metadata = obj.get("metadata") or {}
        if event.get("type") == "checkout.session.completed" and metadata.get("checkout_kind") == "workshop":
            result = _workshop_service(request).complete_checkout(obj)
        else:
            result = _stripe_billing_service(request).handle_event(event)
    except StripeBillingConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"stripe_webhook_invalid: {type(exc).__name__}")
    return {"status": "ok", **result}


@router.get("/settings/onboarding")
def settings_onboarding(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    state = _onboarding_service(request).get_state(user.user_id)
    return {"status": "ok", "onboarding": state.to_dict()}


@router.post("/settings/onboarding")
async def settings_update_onboarding(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    state = _onboarding_service(request).update_state(user.user_id, data)
    return {"status": "ok", "onboarding": state.to_dict()}


@router.get("/marketplace/agents")
def marketplace_agents(
    request: Request,
    q: str = "",
    loop_type: str = "",
    domain: str = "",
    _: BrowserUser = Depends(require_browser_user),
):
    agents = _marketplace_service(request).list_agents(query=q, loop_type=loop_type, domain=domain)
    return {"status": "ok", "agents": agents}


@router.get("/marketplace/apps")
def marketplace_apps(
    request: Request,
    q: str = "",
    loop_type: str = "",
    category: str = "",
    _: BrowserUser = Depends(require_browser_user),
):
    apps = _marketplace_service(request).list_apps(query=q, loop_type=loop_type, category=category)
    return {"status": "ok", "apps": apps}


@router.get("/marketplace/agents/{listing_id_or_slug}")
def marketplace_agent_detail(
    listing_id_or_slug: str,
    request: Request,
    _: BrowserUser = Depends(require_browser_user),
):
    agent = _marketplace_service(request).get_agent(listing_id_or_slug)
    if not agent:
        raise HTTPException(status_code=404, detail="marketplace_agent_not_found")
    return {"status": "ok", "agent": agent}


@router.get("/marketplace/apps/{listing_id_or_slug}")
def marketplace_app_detail(
    listing_id_or_slug: str,
    request: Request,
    _: BrowserUser = Depends(require_browser_user),
):
    app = _marketplace_service(request).get_app(listing_id_or_slug)
    if not app:
        raise HTTPException(status_code=404, detail="marketplace_app_not_found")
    return {"status": "ok", "app": app}


@router.post("/marketplace/agents/{listing_id_or_slug}/install")
def marketplace_install_agent(
    listing_id_or_slug: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    result = _marketplace_service(request).install_agent(user.user_id, listing_id_or_slug)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "install_failed")
    return {"status": "ok", **result}


@router.post("/marketplace/apps/{listing_id_or_slug}/install")
def marketplace_install_app(
    listing_id_or_slug: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    result = _marketplace_service(request).install_app(user.user_id, listing_id_or_slug)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "install_app_failed")
    return {"status": "ok", **result}


@router.get("/marketplace/agents/{listing_id_or_slug}/reviews")
def marketplace_reviews(
    listing_id_or_slug: str,
    request: Request,
    _: BrowserUser = Depends(require_browser_user),
):
    return {"status": "ok", "reviews": _marketplace_service(request).list_reviews(listing_id_or_slug)}


@router.post("/marketplace/agents/{listing_id_or_slug}/reviews")
async def marketplace_create_review(
    listing_id_or_slug: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    data = await request.json()
    result = _marketplace_service(request).review_agent(
        user.user_id,
        listing_id_or_slug,
        int(data.get("rating") or 5),
        str(data.get("review_text") or data.get("text") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "review_failed")
    return {"status": "ok", **result}


@router.get("/admin/marketplace/submissions")
def admin_marketplace_submissions(
    request: Request,
    status: str = "",
    user: BrowserUser = Depends(require_browser_user),
):
    _require_admin(user)
    return {"status": "ok", "submissions": _marketplace_service(request).list_submissions(status=status)}


@router.get("/admin/marketplace/submissions/{submission_id}")
def admin_marketplace_submission_detail(
    submission_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_admin(user)
    submission = _marketplace_service(request).get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="submission_not_found")
    return {"status": "ok", "submission": submission}


@router.post("/admin/marketplace/submissions/{submission_id}/approve")
async def admin_marketplace_approve(
    submission_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_admin(user)
    data = await request.json()
    result = _marketplace_service(request).approve_submission(submission_id, user.user_id, str(data.get("notes") or ""))
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "approval_failed")
    return {"status": "ok", **result}


@router.post("/admin/marketplace/submissions/{submission_id}/reject")
async def admin_marketplace_reject(
    submission_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_admin(user)
    data = await request.json()
    result = _marketplace_service(request).reject_submission(submission_id, user.user_id, str(data.get("notes") or ""))
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "reject_failed")
    return {"status": "ok", **result}


@router.post("/admin/marketplace/submissions/{submission_id}/request-changes")
async def admin_marketplace_request_changes(
    submission_id: str,
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    _require_admin(user)
    data = await request.json()
    result = _marketplace_service(request).reject_submission(
        submission_id,
        user.user_id,
        str(data.get("notes") or ""),
        changes_requested=True,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "request_changes_failed")
    return {"status": "ok", **result}
