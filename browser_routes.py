import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from auth_api_keys.service import get_api_key_service
from billing import BillingPaymentRequired, CreditLimitExceeded, EntitlementDenied, TrialCheckoutRequired, get_billing_service
from browser_auth import BrowserUser, require_browser_user
from marketplace import MarketplaceService, SupabaseMarketplaceRepository
from onboarding import OnboardingService, SupabaseOnboardingRepository
from studio_contract.registry import load_registry
from studio_factory.generate_agent import run_factory as run_studio_factory
from studio_factory.models import AgentFactoryRequest
from studio_planner.models import AgentPlanRequest
from studio_planner.planner import plan_agent as plan_studio_agent
from stripe_billing import StripeBillingConfigError, StripeBillingService
from user_agents.repository import SupabaseUserAgentRepository
from user_agents.service import UserAgentService
from webinar import SupabaseWebinarRegistrationRepository, WebinarRegistrationError, WebinarRegistrationService
from voice_design import VoiceDesignConfigError, build_spec_draft, transcribe_audio_bytes
from workflow_dag.models import WorkflowDAG
from workflow_dag.planner import dag_from_agents, dag_from_studio_graph, dry_run_plan
from workflow_dag.validator import validate_dag


router = APIRouter()
logger = logging.getLogger("chiploop.billing")


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


def _enforce_feature(request: Request, user_id: str, feature: str):
    service = _billing_service(request)
    try:
        service.assert_checkout_started(user_id)
        return service.assert_entitlement(user_id, feature)
    except TrialCheckoutRequired:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "trial_checkout_required",
                "message": "Start your 7-day trial with a credit card to use this Studio feature.",
                "requires_checkout": True,
                "checkout_plan_id": "starter",
            },
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
    return _billing_service(request).upgrade_status(user_id, reason=reason).get("suggested_upgrade")


def _with_upgrade(payload: Dict[str, Any], request: Request, user_id: str, *, reason: str | None = None) -> Dict[str, Any]:
    upgrade = _upgrade_hint(request, user_id, reason=reason)
    return {**payload, "upgrade_hint": upgrade, "upgrade": upgrade}


def _require_checkout(request: Request, user_id: str):
    try:
        _billing_service(request).assert_checkout_started(user_id)
    except TrialCheckoutRequired:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "trial_checkout_required",
                "message": "Start your 7-day trial with a credit card to use voice design sessions.",
                "requires_checkout": True,
                "checkout_plan_id": "starter",
            },
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


def _require_admin(user: BrowserUser) -> None:
    admin_ids = {
        item.strip()
        for item in os.environ.get("CHIPLOOP_ADMIN_USER_IDS", "").split(",")
        if item.strip()
    }
    role = str(user.claims.get("role") or user.claims.get("app_role") or "")
    if role in {"admin", "platform_admin", "marketplace_admin"}:
        return
    if admin_ids and user.user_id in admin_ids:
        return
    raise HTTPException(status_code=403, detail="admin_required")


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
            detail={
                "error": "trial_checkout_required",
                "message": "Start your 7-day trial before creating API keys.",
                "requires_checkout": True,
                "checkout_plan_id": "starter",
            },
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


@router.get("/settings/plan")
def settings_plan(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
    return {"status": "ok", "plan": _billing_service(request).plan_summary(user.user_id)}


@router.get("/settings/upgrade-status")
def settings_upgrade_status(
    request: Request,
    user: BrowserUser = Depends(require_browser_user),
):
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
