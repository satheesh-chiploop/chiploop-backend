# main.py
# ChipLoop backend (FastAPI) — loop-aware workflows + per-run logging

import os
import json
import uuid
import traceback
import httpx
import re
import time;time.sleep(0.2)
import io
import zipfile
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Literal
from typing import Any, Iterable
from fastapi.responses import StreamingResponse
from fastapi import HTTPException
from typing import List

from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Query, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from agent_capabilities import AGENT_CAPABILITIES
from utils.graph_utils import build_capability_graph, serialize_graph
from fastapi.responses import JSONResponse
from utils.llm_utils import run_llm_fallback
from utils.audio_utils import transcribe_audio
from utils.notion_utils import append_to_notion, get_or_create_notion_page
from fastapi import WebSocket
import asyncio
from planner.auto_fill_missing import auto_fill_missing_fields
from agents.runtime import AgentContext, configure_runtime_logging, execute_legacy_agent, log_runtime_event
from utils.artifact_utils import save_text_artifact_and_record
from agents.digital.clock_intent import build_clock_intent
from auth_api_keys.middleware import require_sdk_api_key
from auth_api_keys.service import get_api_key_service
from billing import BillingPaymentRequired, TrialCheckoutRequired, get_billing_service
from onboarding import (
    OnboardingService,
    SupabaseOnboardingRepository,
    is_arch2rtl_guided_demo_payload,
    is_system_architecture_guided_demo_payload,
)
from studio_contract.registry import load_registry
from studio_factory.generate_agent import run_factory as run_studio_factory
from studio_factory.models import AgentFactoryRequest
from studio_planner.models import AgentPlanRequest
from studio_planner.planner import plan_agent as plan_studio_agent
from browser_routes import router as browser_router
from platform_browser_api import router as platform_browser_router
from browser_auth import BrowserUser, is_browser_admin, require_browser_user
from platform_adapters import get_platform_client


import logging
logger = logging.getLogger("chiploop")
logging.basicConfig(level=logging.INFO)
configure_runtime_logging()

# Soft limits to avoid PostgREST "payload string too long" on logs/artifacts fields.
MAX_LOG_CHARS = 850  # ~200KB
MAX_WORKFLOW_ARTIFACTS_JSON_CHARS = 850
ENABLE_LEGACY_WORKFLOW_ARTIFACTS_INDEX = False
MAX_LOG_LINE_CHARS = 400


def _upgrade_hint_for_user(user_id: Optional[str], *, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not user_id or user_id == "anonymous":
        return None
    try:
        return get_billing_service(supabase).upgrade_status(user_id, reason=reason).get("suggested_upgrade")
    except Exception:
        return None

def _truncate_tail(s: str, max_chars: int) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    # Keep the tail so the newest context stays visible.
    return "[TRUNCATED]\n" + s[-max_chars:]

# ---------------- Platform client ----------------
# Supabase remains the default provider. Private deployments may independently
# select PostgreSQL, local/S3 storage, and OIDC through CHIPLOOP_*_PROVIDER.
supabase = get_platform_client()


# ---------------- Auth helper (JWT) ----------------
# If you already have a stronger verify function, keep that and delete this one.
import jwt  # PyJWT

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

from notion_client import Client as NotionClient
import asyncio

# === Notion + Supabase setup ===
notion = NotionClient(auth=os.getenv("NOTION_API_KEY"))


def detect_domain_from_label(label: str):
    l = label.lower()
    if "digital" in l:
        return "digital"
    if "embedded" in l:
        return "embedded"
    if "analog" in l:
        return "analog"
    if "validation" in l:
        return "validation"
    return "system"


def find_missing_generic(spec, path=""):
    missing = []

    if isinstance(spec, dict):
        for key, value in spec.items():
            new_path = f"{path}.{key}" if path else key
            missing += find_missing_generic(value, new_path)

    elif isinstance(spec, list):
        for index, item in enumerate(spec):
            new_path = f"{path}[{index}]"
            missing += find_missing_generic(item, new_path)

    else:
        # ✅ Value-level missing check (dynamic, not hard-coded)
        if spec in (None, "", "unspecified"):
            missing.append({"path": path})

    return missing


def apply_spec_value(spec: dict, path: str, value: Any):
    import re

    tokens = [t for t in re.split(r"\.|\[|\]", path) if t]
    target = spec

    for tok in tokens[:-1]:
        if tok.isdigit():
            idx = int(tok)
            # Ensure target is a list
            if not isinstance(target, list):
                raise ValueError(f"Expected list in path: {path}")
            # ✅ Auto-expand list dynamically
            while len(target) <= idx:
                target.append({})
            target = target[idx]
        else:
            # Ensure dict key exists
            if tok not in target or not isinstance(target[tok], (dict, list)):
                # Detect next token - if numeric → create list, else dict
                next_tok_index = tokens.index(tok) + 1
                is_next_list = next_tok_index < len(tokens) and tokens[next_tok_index].isdigit()
                target[tok] = [] if is_next_list else {}
            target = target[tok]

    last = tokens[-1]
    if last.isdigit():
        idx = int(last)
        if not isinstance(target, list):
            raise ValueError(f"Expected list before index in path: {path}")
        while len(target) <= idx:
            target.append({})
        target[idx] = value
    else:
        target[last] = value


# --- merge helper for nested spec paths ---

def convert_numeric_types(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            obj[k] = convert_numeric_types(v)
        return obj
    elif isinstance(obj, list):
        return [convert_numeric_types(x) for x in obj]
    elif isinstance(obj, str):
        # Dynamic: convert numbers only if safe
        try:
            if '.' in obj:
                return float(obj)
            return int(obj)
        except:
            return obj
    else:
        return obj


def verify_token(request: Request) -> Dict[str, Any]:
    """Best-effort JWT decode from Authorization: Bearer <token>. Fallback to anonymous."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    if token and SUPABASE_JWT_SECRET:
        try:
            payload = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
            return payload  # should contain sub/user id
        except Exception as e:
            logger.warning(f"JWT decode failed, continuing as anonymous: {e}")
    return {"sub": "anonymous"}


def _browser_user_from_token(request: Request) -> BrowserUser:
    claims = verify_token(request)
    return BrowserUser(user_id=str(claims.get("sub") or ""), claims=claims)


def _is_admin_request(request: Request) -> bool:
    user = getattr(request.state, "browser_user", None)
    if isinstance(user, BrowserUser):
        return is_browser_admin(user)
    user = _browser_user_from_token(request)
    if user.user_id and user.user_id != "anonymous":
        request.state.browser_user = user
    return is_browser_admin(user)

# ---------------- FastAPI app ----------------
app = FastAPI(title="ChipLoop API", version="1.0")


def _allowed_cors_origins() -> List[str]:
    configured = os.getenv("CHIPLOOP_ALLOWED_ORIGINS", "")
    if configured.strip():
        return [item.strip().rstrip("/") for item in configured.split(",") if item.strip()]
    return [
        "https://www.getchiploop.com",
        "https://getchiploop.com",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


# ---------------- Validation Loop: Instruments (API schemas) ----------------

class InstrumentRegisterIn(BaseModel):
    nickname: str = Field(..., min_length=1)
    instrument_type: str = Field(..., min_length=1)     # e.g. "scope", "psu", "smu", "dmm"
    transport: str = Field(..., min_length=1)           # e.g. "pyvisa"
    interface: str = Field(..., min_length=1)           # e.g. "TCPIP", "USB", "GPIB"
    resource_string: str = Field(..., min_length=1)     # VISA resource string
    vendor: Optional[str] = None
    model: Optional[str] = None
    scpi_idn: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    make_default: Optional[bool] = False

class InstrumentSetDefaultIn(BaseModel):
    instrument_id: Optional[str] = None
    nickname: Optional[str] = None


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.supabase = supabase
app.state.api_key_service = get_api_key_service(supabase)


def _requires_trial_checkout(path: str, method: str) -> bool:
    if method.upper() != "POST":
        return False
    return (
        path == "/run_workflow"
        or (
            path.startswith("/apps/")
            and path.endswith("/run")
            and path not in {"/apps/arch2rtl/run", "/apps/system/architecture/run"}
        )
        or path == "/validation/test_plan/preview"
    )


def _onboarding_service_for_main() -> OnboardingService:
    existing = getattr(app.state, "onboarding_service", None)
    if isinstance(existing, OnboardingService):
        return existing
    service = OnboardingService(SupabaseOnboardingRepository(supabase))
    app.state.onboarding_service = service
    return service


def _checkout_started_for_user(user_id: str) -> bool:
    try:
        get_billing_service(supabase).assert_checkout_started(user_id)
        return True
    except TrialCheckoutRequired:
        return False


def _checkout_started_for_request(request: Request, user_id: str) -> bool:
    if _is_admin_request(request):
        return True
    return _checkout_started_for_user(user_id)


def _payment_required_response(exc: BillingPaymentRequired) -> HTTPException:
    return HTTPException(
        status_code=402,
        detail={
            "error": "payment_required",
            "message": "Please update your payment method to continue running workflows.",
            "billing_status": exc.billing_status,
            "grace_period_end_at": exc.grace_period_end_at,
        },
    )


def _trial_required_response(message: str) -> HTTPException:
    return HTTPException(
        status_code=402,
        detail={
            "error": "trial_checkout_required",
            "message": message,
            "requires_checkout": True,
            "checkout_plan_id": "starter",
            "checkout_url": "/pricing?trial=1",
            "checkout_label": "Start 3-day trial",
        },
    )


@app.middleware("http")
async def require_checkout_for_workflow_runs(request: Request, call_next):
    if not _requires_trial_checkout(request.url.path, request.method):
        return await call_next(request)
    try:
        user = require_browser_user(request)
        if is_browser_admin(user):
            return await call_next(request)
        get_billing_service(supabase).assert_checkout_started(user.user_id)
    except TrialCheckoutRequired:
        return JSONResponse(
            status_code=402,
            content={
                "status": "error",
                "error": "trial_checkout_required",
                "detail": "Start your 3-day trial to run workflows.",
                "requires_checkout": True,
                "checkout_plan_id": "starter",
                "checkout_url": "/pricing?trial=1",
                "checkout_label": "Start 3-day trial",
            },
        )
    except BillingPaymentRequired as exc:
        return JSONResponse(
            status_code=402,
            content={
                "status": "error",
                "error": "payment_required",
                "detail": "Please update your payment method to continue running workflows.",
                "billing_status": exc.billing_status,
                "grace_period_end_at": exc.grace_period_end_at,
            },
        )
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"status": "error", "detail": exc.detail})
    return await call_next(request)

app.include_router(browser_router)
app.include_router(platform_browser_router)



class BenchCreateIn(BaseModel):
    name: str
    location: Optional[str] = None
    org_id: Optional[str] = None
    instrument_ids: List[str] = []
    schematic: Dict[str, Any] = Field(default_factory=dict)  # connectivity intent / bench schematic
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ==========================================================
# ✅ DIGITAL AGENTS (labels must match the frontend exactly)
# ==========================================================
from agents.digital.digital_testbench_agent_uvm import run_agent as digital_testbench_agent_uvm
from agents.digital.digital_arch_doc_agent import run_agent as digital_arch_doc_agent
from agents.digital.digital_integration_doc_agent import run_agent as digital_integration_doc_agent
from agents.digital.digital_testcase_agent import run_agent as digital_testcase_agent
from agents.digital.digital_assertion_agent import run_agent as digital_assertion_agent
from agents.digital.digital_covergroup_agent import run_agent as digital_covergroup_agent
from agents.digital.digital_spec_agent import run_agent as digital_spec_agent
from agents.digital.digital_rtl_agent import run_agent as digital_rtl_agent
from agents.digital.digital_simulation_agent import run_agent as digital_simulation_agent
from agents.digital.digital_coverage_agent import run_agent as digital_coverage_agent
from agents.digital.digital_optimizer_agent import run_agent as digital_optimizer_agent
from agents.digital.digital_architecture_agent import run_agent as digital_architecture_agent
from agents.digital.digital_microarchitecture_agent import run_agent as digital_microarchitecture_agent
from agents.digital.digital_register_map_agent import run_agent as digital_register_map_agent
from agents.digital.digital_clock_reset_arch_agent import run_agent as digital_clock_reset_arch_agent
from agents.digital.digital_rtl_linting_agent import run_agent as digital_rtl_linting_agent
from agents.digital.digital_rtl_refactoring_agent import run_agent as digital_rtl_refactoring_agent
from agents.digital.digital_cdc_analysis_agent import run_agent as digital_cdc_analysis_agent
from agents.digital.digital_reset_integrity_agent import run_agent as digital_reset_integrity_agent
from agents.digital.digital_sva_assertions_agent import run_agent as digital_sva_assertions_agent
from agents.digital.digital_bug_localization_agent import run_agent as digital_bug_localization_agent
from agents.digital.digital_formal_verification_agent import run_agent as digital_formal_verification_agent
from agents.digital.digital_functional_coverage_agent import run_agent as digital_functional_coverage_agent
from agents.digital.digital_simulation_control_agent import run_agent as digital_simulation_control_agent
from agents.digital.digital_testbench_generator_agent import run_agent as digital_testbench_generator_agent
from agents.digital.digital_golden_model_comparison_agent import run_agent as digital_golden_model_comparison_agent
from agents.digital.digital_ip_packaging_handoff_agent import run_agent as digital_ip_packaging_handoff_agent
from agents.digital.digital_power_intent_upf_agent import run_agent as digital_power_intent_upf_agent
from agents.digital.digital_upf_static_check_agent import run_agent as digital_upf_static_check_agent
from agents.digital.digital_synthesis_readiness_agent import run_agent as digital_synthesis_readiness_agent
from agents.digital.digital_smoke_preflight_agent import run_agent as digital_smoke_preflight_agent
from agents.digital.digital_smoke_exec_summary_agent import run_agent as digital_smoke_exec_summary_agent
from agents.digital.digital_rtl_signature_agent import run_agent as digital_rtl_signature_agent
from agents.digital.digital_integration_intent_agent import run_agent as digital_integration_intent_agent
from agents.digital.digital_top_assembly_agent import run_agent as digital_top_assembly_agent
from agents.digital.digital_implementation_setup_agent import run_agent as digital_implementation_setup_agent
from agents.digital.digital_foundry_profile_agent import run_agent as digital_foundry_profile_agent
from agents.digital.digital_synthesis_agent import run_agent as digital_synthesis_agent
from agents.digital.digital_logic_equivalence_agent import run_agent as digital_logic_equivalence_agent
from agents.digital.digital_synthesis_closure_agent import run_agent as digital_synthesis_closure_agent
from agents.digital.digital_dft_scan_stitching_agent import run_agent as digital_dft_scan_stitching_agent
from agents.digital.digital_post_dft_lec_agent import run_agent as digital_post_dft_lec_agent
from agents.digital.digital_scan_atpg_agent import run_agent as digital_scan_atpg_agent
from agents.digital.digital_mbist_rtl_insertion_agent import run_agent as digital_mbist_rtl_insertion_agent
from agents.digital.digital_mbist_collateral_agent import run_agent as digital_mbist_collateral_agent
from agents.digital.digital_foundry_profile_agent import run_agent as digital_foundry_profile_agent
from agents.digital.digital_implementation_setup_agent import run_agent as digital_implementation_setup_agent
from agents.digital.digital_synthesis_agent import run_agent as digital_synthesis_agent
from agents.digital.digital_sta_preplace_agent import run_agent as digital_sta_preplace_agent
from agents.digital.digital_floorplan_agent import run_agent as digital_floorplan_agent
from agents.digital.digital_placement_agent import run_agent as digital_placement_agent
from agents.digital.digital_sta_postplace_agent import run_agent as digital_sta_postplace_agent
from agents.digital.digital_cts_agent import run_agent as digital_cts_agent
from agents.digital.digital_sta_postcts_agent import run_agent as digital_sta_postcts_agent
from agents.digital.digital_route_agent import run_agent as digital_route_agent
from agents.digital.digital_sta_postroute_agent import run_agent as digital_sta_postroute_agent
from agents.digital.digital_fill_agent import run_agent as digital_fill_agent
from agents.digital.digital_sta_postfill_agent import run_agent as digital_sta_postfill_agent
from agents.digital.digital_drc_agent import run_agent as digital_drc_agent
from agents.digital.digital_lvs_agent import run_agent as digital_lvs_agent
from agents.digital.digital_tapeout_agent import run_agent as digital_tapeout_agent
from agents.digital.digital_tapeout_lec_agent import run_agent as digital_tapeout_lec_agent
from agents.digital.digital_executive_summary_agent import run_agent as digital_executive_summary_agent
from agents.digital.digital_pd_signoff_closure_agent import run_agent as digital_pd_signoff_closure_agent
from agents.system.system_synthesis_closure_agent import run_agent as system_synthesis_closure_agent
from agents.system.system_pd_signoff_closure_agent import run_agent as system_pd_signoff_closure_agent
from agents.digital.digital_simulation_execution_agent import run_agent as digital_simulation_execution_agent
from agents.digital.digital_simulation_summary_coverage_agent import run_agent as digital_simulation_summary_coverage_agent
from agents.digital.digital_verification_handoff_ingest_agent import run_agent as digital_verification_handoff_ingest_agent
from agents.digital.digital_verify_closure_ingest_agent import run_agent as digital_verify_closure_ingest_agent
from agents.digital.digital_coverage_gap_analysis_agent import run_agent as digital_coverage_gap_analysis_agent
from agents.digital.digital_failure_triage_agent import run_agent as digital_failure_triage_agent
from agents.digital.digital_failure_debug_agent import run_agent as digital_failure_debug_agent
from agents.digital.digital_closure_recommendation_agent import run_agent as digital_closure_recommendation_agent
from agents.digital.digital_verification_plan_update_agent import run_agent as digital_verification_plan_update_agent
from agents.digital.digital_coverage_plan_update_agent import run_agent as digital_coverage_plan_update_agent
from agents.digital.digital_testcase_seed_update_agent import run_agent as digital_testcase_seed_update_agent
from agents.digital.digital_closure_rerun_planner_agent import run_agent as digital_closure_rerun_planner_agent
from agents.digital.digital_closure_iteration_judge_agent import run_agent as digital_closure_iteration_judge_agent
from agents.digital.digital_arch2rtl_dashboard_agent import run_agent as digital_arch2rtl_dashboard_agent
from agents.digital.digital_rtl_handoff_ingest_agent import run_agent as digital_rtl_handoff_ingest_agent
from agents.digital.digital_spec2rtl_conformance_agent import run_agent as digital_spec2rtl_conformance_agent
from agents.digital.digital_dqa_summary_agent import run_agent as digital_dqa_summary_agent

DIGITAL_AGENT_FUNCTIONS: Dict[str, Any] = {
    "Digital Spec Agent": digital_spec_agent,
    "Digital RTL Agent": digital_rtl_agent,
    "Digital Sim Agent": digital_simulation_agent,          # ← exact label used by UI
    "Digital Coverage Agent": digital_coverage_agent,
    "Digital Opitmizer Agent": digital_optimizer_agent,     # ← keep typo to match existing label
    "Digital Testbench Agent": digital_testbench_agent_uvm,
    "Digital Arch Doc Agent": digital_arch_doc_agent,
    "Digital Integration Doc Agent": digital_integration_doc_agent,
    "Digital Testcase Agent": digital_testcase_agent,
    "Digital Assertion Agent": digital_assertion_agent,
    "Digital CoverGroup Agent": digital_covergroup_agent,
    "Digital Architecture Agent": digital_architecture_agent,
    "Digital Microarchitecture Agent": digital_microarchitecture_agent,
    "Digital Register Map Agent": digital_register_map_agent,
    "Digital Clock & Reset Architecture Agent": digital_clock_reset_arch_agent,
    "Digital RTL Linting Agent": digital_rtl_linting_agent,
    "Digital CDC Analysis Agent": digital_cdc_analysis_agent,
    "Digital Reset Integrity Agent": digital_reset_integrity_agent,
    "Digital RTL Refactoring Agent": digital_rtl_refactoring_agent,
    "Digital Assertions (SVA) Agent": digital_sva_assertions_agent,
    "Digital Testbench Generator Agent": digital_testbench_generator_agent,
    "Digital Functional Coverage Agent": digital_functional_coverage_agent,
    "Digital Golden Model Comparison Agent": digital_golden_model_comparison_agent,
    "Digital Simulation Control Agent": digital_simulation_control_agent,
    "Digital Simulation Execution Agent": digital_simulation_execution_agent,
    "Digital Simulation Summary Coverage Agent": digital_simulation_summary_coverage_agent,
    "Digital Verification Handoff Ingest Agent": digital_verification_handoff_ingest_agent,
    "Digital Verify Closure Ingest Agent": digital_verify_closure_ingest_agent,
    "Digital Coverage Gap Analysis Agent": digital_coverage_gap_analysis_agent,
    "Digital Failure Triage Agent": digital_failure_triage_agent,
    "Digital Failure Debug Agent": digital_failure_debug_agent,
    "Digital Closure Recommendation Agent": digital_closure_recommendation_agent,
    "Digital Verification Plan Update Agent": digital_verification_plan_update_agent,
    "Digital Coverage Plan Update Agent": digital_coverage_plan_update_agent,
    "Digital Testcase Seed Update Agent": digital_testcase_seed_update_agent,
    "Digital Closure Rerun Planner Agent": digital_closure_rerun_planner_agent,
    "Digital Closure Iteration Judge Agent": digital_closure_iteration_judge_agent,
    "Digital Arch2RTL Dashboard Agent": digital_arch2rtl_dashboard_agent,
    "Digital RTL Handoff Ingest Agent": digital_rtl_handoff_ingest_agent,
    "Digital Spec2RTL Conformance Agent": digital_spec2rtl_conformance_agent,
    "Digital Bug Localization Agent": digital_bug_localization_agent,
    "Digital Formal Verification Agent": digital_formal_verification_agent,
    "Digital Synthesis Readiness Agent": digital_synthesis_readiness_agent,
    "Digital DQA Summary Agent": digital_dqa_summary_agent,
    "Digital Power Intent (UPF-lite) Agent": digital_power_intent_upf_agent,
    "Digital UPF Static Check Agent": digital_upf_static_check_agent,
    "Digital IP Packaging & Handoff Agent": digital_ip_packaging_handoff_agent,
    "Digital Smoke Preflight Agent": digital_smoke_preflight_agent,
    "Digital Smoke Executive Summary Agent": digital_smoke_exec_summary_agent,
    "Digital RTL Signature Agent": digital_rtl_signature_agent,
    "Digital Integration Intent Agent": digital_integration_intent_agent,
    "Digital Top Assembly Agent": digital_top_assembly_agent,
    "Digital Foundry Profile Agent": digital_foundry_profile_agent,
    "Digital Implementation Setup Agent": digital_implementation_setup_agent,
    "Digital Synthesis Agent": digital_synthesis_agent,
    "Digital Logic Equivalence Check Agent": digital_logic_equivalence_agent,
    "Digital Synthesis Closure Agent": digital_synthesis_closure_agent,
    "Digital DFT Scan Stitching Agent": digital_dft_scan_stitching_agent,
    "Digital Post-DFT Logic Equivalence Check Agent": digital_post_dft_lec_agent,
    "Digital Scan ATPG Coverage Agent": digital_scan_atpg_agent,
    "Digital MBIST RTL Insertion Agent": digital_mbist_rtl_insertion_agent,
    "Digital MBIST Collateral Agent": digital_mbist_collateral_agent,
    "Digital STA PrePlace Agent": digital_sta_preplace_agent,
    "Digital Floorplan Agent": digital_floorplan_agent,
    "Digital Placement Agent": digital_placement_agent,
    "Digital STA PostPlace Agent": digital_sta_postplace_agent,
    "Digital CTS Agent": digital_cts_agent,
    "Digital STA PostCTS Agent": digital_sta_postcts_agent,
    "Digital Route Agent": digital_route_agent,
    "Digital STA PostRoute Agent": digital_sta_postroute_agent,
    "Digital Fill Agent": digital_fill_agent,
    "Digital STA PostFill Agent": digital_sta_postfill_agent,
    "Digital DRC Agent": digital_drc_agent,
    "Digital LVS Agent": digital_lvs_agent,
    "Digital Tapeout Agent": digital_tapeout_agent,
    "Digital Tapeout Logic Equivalence Check Agent": digital_tapeout_lec_agent,
    "Digital Executive Summary Agent": digital_executive_summary_agent,
    "Digital PD Signoff Closure Agent": digital_pd_signoff_closure_agent,
    "System PD Signoff Closure Agent": system_pd_signoff_closure_agent,
}


# ==========================================================
# ✅ ANALOG AGENTS (NEW)
# ==========================================================
from agents.analog.analog_spec_builder_agent import run_agent as analog_spec_builder_agent
from agents.analog.analog_netlist_scaffold_agent import run_agent as analog_netlist_scaffold_agent
from agents.analog.analog_sim_plan_agent import run_agent as analog_sim_plan_agent
from agents.analog.analog_behavioral_model_agent import run_agent as analog_behavioral_model_agent
from agents.analog.analog_behavioral_tb_agent import run_agent as analog_behavioral_tb_agent
from agents.analog.analog_behavioral_sva_agent import run_agent as analog_behavioral_sva_agent
from agents.analog.analog_behavioral_coverage_agent import run_agent as analog_behavioral_coverage_agent
from agents.analog.analog_correlation_agent import run_agent as analog_correlation_agent
from agents.analog.analog_iteration_agent import run_agent as analog_iteration_agent
from agents.analog.analog_abstract_views_agent import run_agent as analog_abstract_views_agent
from agents.analog.analog_macro_interface_contract_agent import run_agent as analog_macro_interface_contract_agent
from agents.analog.analog_macro_interface_validation_agent import run_agent as analog_macro_interface_validation_agent
from agents.analog.analog_sky130_spice_netlist_agent import run_agent as analog_sky130_spice_netlist_agent
from agents.analog.analog_gds_generation_agent import run_agent as analog_gds_generation_agent
from agents.analog.analog_lef_extraction_agent import run_agent as analog_lef_extraction_agent
from agents.analog.analog_liberty_characterization_agent import run_agent as analog_liberty_characterization_agent
from agents.analog.analog_collateral_consistency_agent import run_agent as analog_collateral_consistency_agent
from agents.analog.analog_physical_collateral_package_agent import run_agent as analog_physical_collateral_package_agent
from agents.analog.analog_exec_summary_agent import run_agent as analog_exec_summary_agent

ANALOG_AGENT_FUNCTIONS: Dict[str, Any] = {
    "Analog Spec Builder Agent": analog_spec_builder_agent,
    "Analog Netlist Scaffold Agent": analog_netlist_scaffold_agent,
    "Analog Simulation Plan Agent": analog_sim_plan_agent,
    "Analog Behavioral Model Agent": analog_behavioral_model_agent,
    "Analog Behavioral Testbench Agent": analog_behavioral_tb_agent,
    "Analog Behavioral Assertions Agent": analog_behavioral_sva_agent,
    "Analog Behavioral Coverage Agent": analog_behavioral_coverage_agent,
    "Analog Correlation Agent": analog_correlation_agent,
    "Analog Iteration Proposal Agent": analog_iteration_agent,
    "Analog Abstract Views Agent": analog_abstract_views_agent,
    "Analog Macro Interface Contract Agent": analog_macro_interface_contract_agent,
    "Analog Macro Interface Validation Agent": analog_macro_interface_validation_agent,
    "Analog Sky130 SPICE Netlist Agent": analog_sky130_spice_netlist_agent,
    "Analog GDS Generation Agent": analog_gds_generation_agent,
    "Analog LEF Extraction Agent": analog_lef_extraction_agent,
    "Analog Liberty Characterization Agent": analog_liberty_characterization_agent,
    "Analog Collateral Consistency Agent": analog_collateral_consistency_agent,
    "Analog Physical Collateral Package Agent": analog_physical_collateral_package_agent,
    "Analog Executive Summary Agent": analog_exec_summary_agent,
}

# ==========================================================
# ✅ EMBEDDED AGENTS
# ==========================================================
from agents.embedded.embedded_spec_agent import run_agent as embedded_spec_agent
from agents.embedded.embedded_code_agent import run_agent as embedded_code_agent
from agents.embedded.embedded_sim_agent import run_agent as embedded_sim_agent
from agents.embedded.embedded_result_agent import run_agent as embedded_result_agent
from agents.embedded.embedded_firmware_register_extract_agent import run_agent as embedded_firmware_register_extract_agent_run
from agents.embedded.embedded_rust_register_layer_generator_agent import run_agent as embedded_rust_register_layer_generator_agent_run
from agents.embedded.embedded_register_validation_agent import run_agent as embedded_register_validation_agent_run
from agents.embedded.embedded_rust_driver_scaffold_agent import run_agent as embedded_rust_driver_scaffold_agent_run
from agents.embedded.embedded_interrupt_mapping_agent import run_agent as embedded_interrupt_mapping_agent_run
from agents.embedded.embedded_dma_integration_agent import run_agent as embedded_dma_integration_agent_run
from agents.embedded.embedded_boot_dependency_planner_agent import run_agent as embedded_boot_dependency_planner_agent_run
from agents.embedded.embedded_clock_and_pll_configuration_agent import run_agent as embedded_clock_and_pll_configuration_agent_run
from agents.embedded.embedded_reset_sequencing_agent import run_agent as embedded_reset_sequencing_agent_run
from agents.embedded.embedded_power_mode_configuration_agent import run_agent as embedded_power_mode_configuration_agent_run
from agents.embedded.embedded_boot_timing_validation_agent import run_agent as embedded_boot_timing_validation_agent_run
from agents.embedded.embedded_register_dump_utility_agent import run_agent as embedded_register_dump_utility_agent_run
from agents.embedded.embedded_built_in_self_test_agent import run_agent as embedded_built_in_self_test_agent_run
from agents.embedded.embedded_stress_test_generator_agent import run_agent as embedded_stress_test_generator_agent_run
from agents.embedded.embedded_firmware_integration_contract_agent import run_agent as embedded_firmware_integration_contract_agent_run
from agents.embedded.embedded_firmware_executive_summary_agent import run_agent as embedded_firmware_executive_summary_agent_run
from agents.embedded.embedded_elf_build_agent import run_agent as embedded_elf_build_agent_run
from agents.embedded.embedded_verilator_build_agent import run_agent as embedded_verilator_build_agent_run
from agents.embedded.embedded_cocotb_harness_agent import run_agent as embedded_cocotb_harness_agent_run
from agents.embedded.embedded_co_sim_runner_agent import run_agent as embedded_co_sim_runner_agent_run
from agents.embedded.embedded_coverage_collector_agent import run_agent as embedded_coverage_collector_agent_run
from agents.embedded.embedded_validation_report_agent import run_agent as embedded_validation_report_agent_run
from agents.embedded.embedded_digital_handoff_ingest_agent import run_agent as embedded_digital_handoff_ingest_agent_run


EMBEDDED_AGENT_FUNCTIONS: Dict[str, Any] = {
    "Embedded Spec Agent": embedded_spec_agent,
    "Embedded Code Agent": embedded_code_agent,
    "Embedded Sim Agent": embedded_sim_agent,
    "Embedded Result Agent": embedded_result_agent,
    "Embedded Firmware Register Extract Agent": embedded_firmware_register_extract_agent_run,
    "Embedded Rust Register Layer Generator Agent": embedded_rust_register_layer_generator_agent_run,
    "Embedded Register Validation Agent": embedded_register_validation_agent_run,
    "Embedded Rust Driver Scaffold Agent": embedded_rust_driver_scaffold_agent_run,
    "Embedded Interrupt Mapping Agent": embedded_interrupt_mapping_agent_run,
    "Embedded DMA Integration Agent": embedded_dma_integration_agent_run,
    "Embedded Boot Dependency Planner Agent": embedded_boot_dependency_planner_agent_run,
    "Embedded Clock And PLL Configuration Agent": embedded_clock_and_pll_configuration_agent_run,
    "Embedded Reset Sequencing Agent": embedded_reset_sequencing_agent_run,
    "Embedded Power Mode Configuration Agent": embedded_power_mode_configuration_agent_run,
    "Embedded Boot Timing Validation Agent": embedded_boot_timing_validation_agent_run,
    "Embedded Register Dump Utility Agent": embedded_register_dump_utility_agent_run,
    "Embedded Built In Self Test Agent": embedded_built_in_self_test_agent_run,
    "Embedded Stress Test Generator Agent": embedded_stress_test_generator_agent_run,
    "Embedded Firmware Integration Contract Agent": embedded_firmware_integration_contract_agent_run,
    "Embedded Firmware Executive Summary Agent": embedded_firmware_executive_summary_agent_run,
    "Embedded ELF Build Agent": embedded_elf_build_agent_run,
    "Embedded Verilator Build Agent": embedded_verilator_build_agent_run,
    "Embedded Cocotb Harness Agent": embedded_cocotb_harness_agent_run,
    "Embedded Co Sim Runner Agent": embedded_co_sim_runner_agent_run,
    "Embedded Coverage Collector Agent": embedded_coverage_collector_agent_run,
    "Embedded Validation Report Agent": embedded_validation_report_agent_run,
    "Embedded Digital RTL Handoff Ingest Agent": embedded_digital_handoff_ingest_agent_run,
}

from agents.validation.validation_instrument_setup_agent import run_agent as validation_instrument_setup_agent
from agents.validation.validation_test_plan_agent import run_agent as validation_test_plan_agent
from agents.validation.validation_sequence_builder_agent import run_agent as validation_sequence_builder_agent
from agents.validation.validation_execution_orchestrator_agent import run_agent as validation_execution_orchestrator_agent
from agents.validation.validation_analytics_agent import run_agent as validation_analytics_agent
from agents.validation.validation_scope_agent import run_agent as validation_scope_agent
from agents.validation.validation_connectivity_intent_agent import run_agent as validation_connectivity_intent_agent
from agents.validation.validation_wiring_instructions_agent import run_agent as validation_wiring_instructions_agent
from agents.validation.validation_preflight_agent import run_agent as validation_preflight_agent
from agents.validation.validation_bench_create_agent import run_agent as validation_bench_create_agent
from agents.validation.validation_test_plan_load_agent  import run_agent as validation_test_plan_load_agent
from agents.validation.validation_bench_schematic_agent import run_agent as validation_bench_schematic_agent
from agents.validation.validation_bench_schematic_load_mapping_agent import run_agent as validation_bench_schematic_load_mapping_agent
from agents.validation.validation_pattern_detection_agent import run_agent as validation_pattern_detection_agent
from agents.validation.validation_apply_proposal_agent import run_agent as validation_apply_proposal_agent
from agents.validation.validation_evolution_proposal_agent import run_agent as validation_evolution_proposal_agent
from agents.validation.validation_coverage_proposal_agent import run_agent as validation_coverage_proposal_agent



#  VALIDATION FUNCTIONS
# ==========================================================
VALIDATION_AGENT_FUNCTIONS: Dict[str, Any] = {
    # Day-1: keep empty or add stubs; we’ll populate with real PyVISA agents next.
    # Examples of future labels:
    "Validation Instrument Setup Agent": validation_instrument_setup_agent,
    "Validation Test Plan Agent": validation_test_plan_agent,
    "Validation Connectivity Intent Agent": validation_connectivity_intent_agent,
    "Validation Wiring Instructions Agent": validation_wiring_instructions_agent,
    "Validation Scope Agent": validation_scope_agent,
    "Validation Sequence Builder Agent": validation_sequence_builder_agent,
    "Validation Execution Orchestrator Agent": validation_execution_orchestrator_agent,
    "Validation Bench Create Agent": validation_bench_create_agent,
    "Validation Preflight Agent": validation_preflight_agent,
    "Validation Test Plan Load Agent": validation_test_plan_load_agent,
    "Validation Bench Schematic Agent": validation_bench_schematic_agent,
    "Validation Bench Schematic Load + Mapping Agent": validation_bench_schematic_load_mapping_agent,
    "Validation Pattern Detection Agent": validation_pattern_detection_agent,
    "Validation Apply Proposal Agent": validation_apply_proposal_agent,
    "Validation Evolution Proposal Agent": validation_evolution_proposal_agent,
    "Validation Coverage Proposal Agent": validation_coverage_proposal_agent,

    # "Measurement Logger Agent": validation_logger_agent,
    "Validation Analytics Agent": validation_analytics_agent,
    # "Validation Debug Agent": validation_debug_agent,
}
from agents.system.system_workflow_agent import run_agent as system_workflow_agent
from agents.system.system_cosim_integration_agent import run_agent as system_cosim_integration_agent
from agents.system.system_iss_bridge_agent import run_agent as system_iss_bridge_agent
from agents.system.system_integration_intent_agent import run_agent as system_integration_intent
from agents.system.system_top_assembly_agent import run_agent as system_top_assembly
from agents.system.system_sim_execution_agent import run_agent as system_sim_execution_agent
from agents.system.system_sim_coverage_summary_agent import run_agent as system_sim_coverage_summary_agent
from agents.system.system_firmware_cosim_execution_agent import run_agent as system_firmware_cosim_execution_agent
from agents.system.system_firmware_coverage_summary_agent import run_agent as system_firmware_coverage_summary_agent
from agents.system.system_testbench_generator_agent import run_agent as system_testbench_generator_agent
from agents.system.system_sva_assertions_agent import run_agent as system_sva_assertions_agent
from agents.system.system_formal_verification_agent import run_agent as system_formal_verification_agent
from agents.system.system_functional_coverage_agent import run_agent as system_functional_coverage_agent
from agents.system.system_golden_model_comparison_agent import run_agent as system_golden_model_comparison_agent
from agents.system.system_simulation_control_agent import run_agent as system_simulation_control_agent
from agents.system.system_implementation_setup_agent import run_agent as system_implementation_setup_agent
from agents.system.system_software_handoff_package_agent import run_agent as system_software_handoff_package_agent
from agents.system.system_software_handoff_ingest_agent import run_agent as system_software_handoff_ingest_agent
from agents.system.system_software_capability_model_agent import run_agent as system_software_capability_model_agent
from agents.system.system_software_api_contract_agent import run_agent as system_software_api_contract_agent
from agents.system.system_software_sdk_scaffold_agent import run_agent as system_software_sdk_scaffold_agent
from agents.system.system_software_hal_driver_adapter_agent import run_agent as system_software_hal_driver_adapter_agent
from agents.system.system_software_config_schema_agent import run_agent as system_software_config_schema_agent
from agents.system.system_software_service_architecture_agent import run_agent as system_software_service_architecture_agent
from agents.system.system_software_core_service_agent import run_agent as system_software_core_service_agent
from agents.system.system_software_application_scaffold_agent import run_agent as system_software_application_scaffold_agent
from agents.system.system_software_cli_tooling_agent import run_agent as system_software_cli_tooling_agent
from agents.system.system_software_build_system_agent import run_agent as system_software_build_system_agent
from agents.system.system_software_unit_test_agent import run_agent as system_software_unit_test_agent
from agents.system.system_software_mock_runtime_agent import run_agent as system_software_mock_runtime_agent
from agents.system.system_software_packaging_agent import run_agent as system_software_packaging_agent
from agents.system.system_software_executive_summary_agent import run_agent as system_software_executive_summary_agent
from agents.system.system_software_validation_ingest_agent import run_agent as system_software_validation_ingest_agent
from agents.system.system_software_build_validation_agent import run_agent as system_software_build_validation_agent
from agents.system.system_software_test_execution_agent import run_agent as system_software_test_execution_agent
from agents.system.system_software_contract_consistency_agent import run_agent as system_software_contract_consistency_agent
from agents.system.system_software_mock_runtime_validation_agent import run_agent as system_software_mock_runtime_validation_agent
from agents.system.system_software_package_audit_agent import run_agent as system_software_package_audit_agent
from agents.system.system_software_validation_summary_agent import run_agent as system_software_validation_summary_agent
from agents.system.system_rtl_handoff_package_agent import run_agent as system_rtl_handoff_package_agent
from agents.system.system_rtl_dashboard_agent import run_agent as system_rtl_dashboard_agent
from agents.system.system_cosim_ingest_agent import run_agent as system_cosim_ingest_agent
from agents.system.system_cosim_contract_agent import run_agent as system_cosim_contract_agent
from agents.system.system_cosim_scenario_generator_agent import run_agent as system_cosim_scenario_generator_agent
from agents.system.system_software_cosim_harness_agent import run_agent as system_cosim_harness_agent
from agents.system.system_software_cosim_execution_agent import run_agent as system_cosim_execution_agent
from agents.system.system_software_cosim_trace_validation_agent import run_agent as system_cosim_trace_validation_agent
from agents.system.system_software_validation_summary_l2_agent import run_agent as system_software_validation_summary_l2_agent
from agents.system.system_product_collateral_ingest_agent import run_agent as system_product_collateral_ingest_agent
from agents.system.system_product_capability_model_agent import run_agent as system_product_capability_model_agent
from agents.system.system_product_dashboard_agent import run_agent as system_product_dashboard_agent
from agents.system.system_product_package_agent import run_agent as system_product_package_agent
from agents.system.system_architecture_explorer_agents import (
    system_architecture_intent_agent,
    system_workload_characterization_agent,
    system_gem5_config_agent,
    system_design_space_exploration_agent,
    system_gem5_execution_agent,
    system_performance_metrics_agent,
    system_power_estimation_agent,
    system_area_estimation_agent,
    system_ppa_tradeoff_agent,
    system_architecture_visualization_agent,
    system_architecture_report_agent,
    system_architecture_to_rtl_intent_agent,
)

SYSTEM_AGENT_FUNCTIONS: Dict[str,Any] = {
    "Digital Spec Agent": digital_spec_agent,
    "Digital RTL Agent": digital_rtl_agent,
    "Digital Sim Agent": digital_simulation_agent,          # ← exact label used by UI
    "Digital Coverage Agent": digital_coverage_agent,
    "Digital Opitmizer Agent": digital_optimizer_agent,     # ← keep typo to match existing label
    "Digital Testbench Agent": digital_testbench_agent_uvm,
    "Digital Arch Doc Agent": digital_arch_doc_agent,
    "Digital Integration Doc Agent": digital_integration_doc_agent,
    "Digital Testcase Agent": digital_testcase_agent,
    "Digital Assertion Agent": digital_assertion_agent,
    "Digital CoverGroup Agent": digital_covergroup_agent,
    "Digital Architecture Agent": digital_architecture_agent,
    "Digital Microarchitecture Agent": digital_microarchitecture_agent,
    "Digital Register Map Agent": digital_register_map_agent,
    "Digital Clock & Reset Architecture Agent": digital_clock_reset_arch_agent,
    "Digital RTL Linting Agent": digital_rtl_linting_agent,
    "Digital CDC Analysis Agent": digital_cdc_analysis_agent,
    "Digital Reset Integrity Agent": digital_reset_integrity_agent,
    "Digital RTL Refactoring Agent": digital_rtl_refactoring_agent,
    "Digital Assertions (SVA) Agent": digital_sva_assertions_agent,
    "Digital Testbench Generator Agent": digital_testbench_generator_agent,
    "Digital Functional Coverage Agent": digital_functional_coverage_agent,
    "Digital Golden Model Comparison Agent": digital_golden_model_comparison_agent,
    "Digital Simulation Control Agent": digital_simulation_control_agent,
    "Digital Simulation Execution Agent": digital_simulation_execution_agent,
    "Digital Simulation Summary Coverage Agent": digital_simulation_summary_coverage_agent,
    "Digital Verification Handoff Ingest Agent": digital_verification_handoff_ingest_agent,
    "Digital Verify Closure Ingest Agent": digital_verify_closure_ingest_agent,
    "Digital Coverage Gap Analysis Agent": digital_coverage_gap_analysis_agent,
    "Digital Failure Triage Agent": digital_failure_triage_agent,
    "Digital Failure Debug Agent": digital_failure_debug_agent,
    "Digital Closure Recommendation Agent": digital_closure_recommendation_agent,
    "Digital Verification Plan Update Agent": digital_verification_plan_update_agent,
    "Digital Coverage Plan Update Agent": digital_coverage_plan_update_agent,
    "Digital Testcase Seed Update Agent": digital_testcase_seed_update_agent,
    "Digital Closure Rerun Planner Agent": digital_closure_rerun_planner_agent,
    "Digital Closure Iteration Judge Agent": digital_closure_iteration_judge_agent,
    "Digital Arch2RTL Dashboard Agent": digital_arch2rtl_dashboard_agent,
    "Digital RTL Handoff Ingest Agent": digital_rtl_handoff_ingest_agent,
    "Digital Spec2RTL Conformance Agent": digital_spec2rtl_conformance_agent,
    "Digital Bug Localization Agent": digital_bug_localization_agent,
    "Digital Formal Verification Agent": digital_formal_verification_agent,   
    "Digital Synthesis Readiness Agent": digital_synthesis_readiness_agent,
    "Digital DQA Summary Agent": digital_dqa_summary_agent,
    "Digital Power Intent (UPF-lite) Agent": digital_power_intent_upf_agent,
    "Digital UPF Static Check Agent": digital_upf_static_check_agent,
    "Digital IP Packaging & Handoff Agent": digital_ip_packaging_handoff_agent, 
    "Digital Smoke Preflight Agent": digital_smoke_preflight_agent,
    "Digital Smoke Executive Summary Agent": digital_smoke_exec_summary_agent,
    "Digital RTL Signature Agent": digital_rtl_signature_agent,
    "Digital Integration Intent Agent": digital_integration_intent_agent,
    "Digital Top Assembly Agent": digital_top_assembly_agent,
    "Digital Foundry Profile Agent": digital_foundry_profile_agent,
    "Digital Implementation Setup Agent": digital_implementation_setup_agent,
    "Digital Synthesis Agent": digital_synthesis_agent,
    "Digital Logic Equivalence Check Agent": digital_logic_equivalence_agent,
    "Digital Synthesis Closure Agent": digital_synthesis_closure_agent,
    "System Synthesis Closure Agent": system_synthesis_closure_agent,
    "Digital DFT Scan Stitching Agent": digital_dft_scan_stitching_agent,
    "Digital Post-DFT Logic Equivalence Check Agent": digital_post_dft_lec_agent,
    "Digital Scan ATPG Coverage Agent": digital_scan_atpg_agent,
    "Digital MBIST RTL Insertion Agent": digital_mbist_rtl_insertion_agent,
    "Digital MBIST Collateral Agent": digital_mbist_collateral_agent,
    "Digital STA PrePlace Agent": digital_sta_preplace_agent,
    "Digital Floorplan Agent": digital_floorplan_agent,
    "Digital Placement Agent": digital_placement_agent,
    "Digital STA PostPlace Agent": digital_sta_postplace_agent,
    "Digital CTS Agent": digital_cts_agent,
    "Digital STA PostCTS Agent": digital_sta_postcts_agent,
    "Digital Route Agent": digital_route_agent,
    "Digital STA PostRoute Agent": digital_sta_postroute_agent,
    "Digital Fill Agent": digital_fill_agent,
    "Digital STA PostFill Agent": digital_sta_postfill_agent,
    "Digital DRC Agent": digital_drc_agent,
    "Digital LVS Agent": digital_lvs_agent,
    "Digital Tapeout Agent": digital_tapeout_agent,
    "Digital Tapeout Logic Equivalence Check Agent": digital_tapeout_lec_agent,
    "Digital Executive Summary Agent": digital_executive_summary_agent,
    "Digital PD Signoff Closure Agent": digital_pd_signoff_closure_agent,
    "System PD Signoff Closure Agent": system_pd_signoff_closure_agent,
    "Analog Spec Builder Agent": analog_spec_builder_agent,
    "Analog Netlist Scaffold Agent": analog_netlist_scaffold_agent, 
    "Analog Simulation Plan Agent": analog_sim_plan_agent,
    "Analog Behavioral Model Agent": analog_behavioral_model_agent,
    "Analog Behavioral Testbench Agent": analog_behavioral_tb_agent,
    "Analog Behavioral Assertions Agent": analog_behavioral_sva_agent,
    "Analog Behavioral Coverage Agent": analog_behavioral_coverage_agent,
    "Analog Correlation Agent": analog_correlation_agent,
    "Analog Iteration Proposal Agent": analog_iteration_agent,
    "Analog Abstract Views Agent": analog_abstract_views_agent,
    "Analog Macro Interface Contract Agent": analog_macro_interface_contract_agent,
    "Analog Macro Interface Validation Agent": analog_macro_interface_validation_agent,
    "Analog Sky130 SPICE Netlist Agent": analog_sky130_spice_netlist_agent,
    "Analog GDS Generation Agent": analog_gds_generation_agent,
    "Analog LEF Extraction Agent": analog_lef_extraction_agent,
    "Analog Liberty Characterization Agent": analog_liberty_characterization_agent,
    "Analog Collateral Consistency Agent": analog_collateral_consistency_agent,
    "Analog Physical Collateral Package Agent": analog_physical_collateral_package_agent,
    "Analog Executive Summary Agent": analog_exec_summary_agent,
    "Embedded Spec Agent": embedded_spec_agent,
    "Embedded Code Agent": embedded_code_agent,
    "Embedded Sim Agent": embedded_sim_agent,
    "Embedded Result Agent": embedded_result_agent, 
    "Embedded Firmware Register Extract Agent": embedded_firmware_register_extract_agent_run,
    "Embedded Rust Register Layer Generator Agent": embedded_rust_register_layer_generator_agent_run,
    "Embedded Register Validation Agent": embedded_register_validation_agent_run,
    "Embedded Rust Driver Scaffold Agent": embedded_rust_driver_scaffold_agent_run,
    "Embedded Interrupt Mapping Agent": embedded_interrupt_mapping_agent_run,
    "Embedded DMA Integration Agent": embedded_dma_integration_agent_run,
    "Embedded Boot Dependency Planner Agent": embedded_boot_dependency_planner_agent_run,
    "Embedded Clock And PLL Configuration Agent": embedded_clock_and_pll_configuration_agent_run,
    "Embedded Reset Sequencing Agent": embedded_reset_sequencing_agent_run,
    "Embedded Power Mode Configuration Agent": embedded_power_mode_configuration_agent_run,
    "Embedded Boot Timing Validation Agent": embedded_boot_timing_validation_agent_run,
    "Embedded Register Dump Utility Agent": embedded_register_dump_utility_agent_run,
    "Embedded Built In Self Test Agent": embedded_built_in_self_test_agent_run,
    "Embedded Stress Test Generator Agent": embedded_stress_test_generator_agent_run,
    "Embedded Firmware Integration Contract Agent": embedded_firmware_integration_contract_agent_run,
    "Embedded Firmware Executive Summary Agent": embedded_firmware_executive_summary_agent_run,
    "Embedded ELF Build Agent": embedded_elf_build_agent_run,
    "Embedded Verilator Build Agent": embedded_verilator_build_agent_run,
    "Embedded Cocotb Harness Agent": embedded_cocotb_harness_agent_run,
    "Embedded Co Sim Runner Agent": embedded_co_sim_runner_agent_run,
    "Embedded Coverage Collector Agent": embedded_coverage_collector_agent_run,
    "Embedded Validation Report Agent": embedded_validation_report_agent_run,
    "Validation Instrument Setup Agent": validation_instrument_setup_agent,
    "Validation Test Plan Agent": validation_test_plan_agent,
    "Validation Connectivity Intent Agent": validation_connectivity_intent_agent,
    "Validation Wiring Instructions Agent": validation_wiring_instructions_agent,
    "Validation Scope Agent": validation_scope_agent,
    "Validation Sequence Builder Agent": validation_sequence_builder_agent,
    "Validation Execution Orchestrator Agent": validation_execution_orchestrator_agent,
    "Validation Preflight Agent": validation_preflight_agent,
    "Validation Analytics Agent": validation_analytics_agent,
    "Validation Bench Create Agent": validation_bench_create_agent,
    "Validation Test Plan Load Agent": validation_test_plan_load_agent,
    "Validation Bench Schematic Agent": validation_bench_schematic_agent,
    "Validation Bench Schematic Load + Mapping Agent": validation_bench_schematic_load_mapping_agent,
    "Validation Pattern Detection Agent": validation_pattern_detection_agent,
    "Validation Apply Proposal Agent": validation_apply_proposal_agent,
    "Validation Evolution Proposal Agent": validation_evolution_proposal_agent,
    "Validation Coverage Proposal Agent": validation_coverage_proposal_agent,
    "System Workflow Agent": system_workflow_agent,  
    "System CoSim Integration Agent": system_cosim_integration_agent,
    "System ISS Bridge Agent": system_iss_bridge_agent,  
    "System Integration Intent Agent": system_integration_intent,
    "System Top Assembly Agent": system_top_assembly, 
    "System Testbench Generator Agent": system_testbench_generator_agent,
    "System Implementation Setup Agent": system_implementation_setup_agent,
    "System Assertions (SVA) Agent": system_sva_assertions_agent,
    "System Formal Verification Agent": system_formal_verification_agent,
    "System Functional Coverage Agent": system_functional_coverage_agent,
    "System Golden Model Comparison Agent": system_golden_model_comparison_agent,
    "System Simulation Control Agent": system_simulation_control_agent,
    "System Simulation Execution Agent": system_sim_execution_agent,
    "System Simulation Coverage Summary Agent": system_sim_coverage_summary_agent,
    "System Sim Closure Ingest Agent": digital_verify_closure_ingest_agent,
    "System Sim Coverage Gap Analysis Agent": digital_coverage_gap_analysis_agent,
    "System Sim Failure Triage Agent": digital_failure_triage_agent,
    "System Sim Failure Debug Agent": digital_failure_debug_agent,
    "System Sim Closure Recommendation Agent": digital_closure_recommendation_agent,
    "System Sim Verification Plan Update Agent": digital_verification_plan_update_agent,
    "System Sim Coverage Plan Update Agent": digital_coverage_plan_update_agent,
    "System Sim Testcase Seed Update Agent": digital_testcase_seed_update_agent,
    "System Sim Closure Rerun Planner Agent": digital_closure_rerun_planner_agent,
    "System Sim Closure Iteration Judge Agent": digital_closure_iteration_judge_agent,
    "System Firmware CoSim Execution Agent": system_firmware_cosim_execution_agent,
    "System Firmware Coverage Summary Agent": system_firmware_coverage_summary_agent,
    "System Software Handoff Package Agent": system_software_handoff_package_agent,
    "System Software Handoff Ingest Agent": system_software_handoff_ingest_agent,
    "System Software Capability Model Agent": system_software_capability_model_agent,
    "System Software API Contract Agent": system_software_api_contract_agent,
    "System Software SDK Scaffold Agent": system_software_sdk_scaffold_agent,
    "System Software HAL/Driver Adapter Agent": system_software_hal_driver_adapter_agent,
    "System Software Config Schema Agent": system_software_config_schema_agent,
    "System Software Service Architecture Agent": system_software_service_architecture_agent,
    "System Software Core Service Agent": system_software_core_service_agent,
    "System Software Application Scaffold Agent": system_software_application_scaffold_agent,
    "System Software CLI / Tooling Agent": system_software_cli_tooling_agent,
    "System Software Build System Agent": system_software_build_system_agent,
    "System Software Unit Test Agent": system_software_unit_test_agent,
    "System Software Mock Runtime Agent": system_software_mock_runtime_agent,
    "System Software Packaging Agent": system_software_packaging_agent,
    "System Software Executive Summary Agent": system_software_executive_summary_agent,
    "System Software Validation Ingest Agent": system_software_validation_ingest_agent,
    "System Software Build Validation Agent": system_software_build_validation_agent,
    "System Software Test Execution Agent": system_software_test_execution_agent,
    "System Software Contract Consistency Agent": system_software_contract_consistency_agent,
    "System Software Mock Runtime Validation Agent": system_software_mock_runtime_validation_agent,
    "System Software Package Audit Agent": system_software_package_audit_agent,
    "System Software Validation Summary Agent": system_software_validation_summary_agent,
    "System RTL Handoff Package Agent": system_rtl_handoff_package_agent,
    "System RTL Evidence Dashboard Agent": system_rtl_dashboard_agent,
    "System CoSim Ingest Agent": system_cosim_ingest_agent,
    "System CoSim Contract Agent": system_cosim_contract_agent,
    "System CoSim Scenario Generator Agent": system_cosim_scenario_generator_agent,
    "System Software CoSim Harness Agent": system_cosim_harness_agent,
    "System Software CoSim Execution Agent": system_cosim_execution_agent,
    "System Software CoSim Trace Validation Agent": system_cosim_trace_validation_agent,
    "System Software Validation Summary (L2)": system_software_validation_summary_l2_agent,
    "System Product Collateral Ingest Agent": system_product_collateral_ingest_agent,
    "System Product Capability Model Agent": system_product_capability_model_agent,
    "System Product Dashboard Agent": system_product_dashboard_agent,
    "System Product Package Agent": system_product_package_agent,
    "System Architecture Intent Agent": system_architecture_intent_agent,
    "System Workload Characterization Agent": system_workload_characterization_agent,
    "System gem5 Config Agent": system_gem5_config_agent,
    "System Design Space Exploration Agent": system_design_space_exploration_agent,
    "System gem5 Execution Agent": system_gem5_execution_agent,
    "System Performance Metrics Agent": system_performance_metrics_agent,
    "System Power Estimation Agent": system_power_estimation_agent,
    "System Area Estimation Agent": system_area_estimation_agent,
    "System PPA Tradeoff Agent": system_ppa_tradeoff_agent,
    "System Visualization Agent": system_architecture_visualization_agent,
    "System Architecture Report Agent": system_architecture_report_agent,
    "System Architecture to RTL Intent Agent": system_architecture_to_rtl_intent_agent,
}


# ==========================================================
# 🧠 UNIFIED + CUSTOM REGISTRY
# ==========================================================

AGENT_FUNCTIONS: Dict[str, Dict[str, Any]] = {
    "digital": DIGITAL_AGENT_FUNCTIONS,
    "analog": ANALOG_AGENT_FUNCTIONS,
    "embedded": EMBEDDED_AGENT_FUNCTIONS,
    "system": SYSTEM_AGENT_FUNCTIONS,
    "validation": VALIDATION_AGENT_FUNCTIONS,
}


def _linear_workflow_definition(agent_names: List[str]) -> Dict[str, Any]:
    nodes = []
    edges = []
    for index, name in enumerate(agent_names):
        node_id = f"n{index + 1}"
        nodes.append({
            "id": node_id,
            "type": "agent",
            "position": {"x": 80 + index * 240, "y": 180},
            "data": {"uiLabel": name.replace("System ", ""), "backendLabel": name},
        })
        if index:
            edges.append({"id": f"e{index}", "source": f"n{index}", "target": node_id})
    return {"nodes": nodes, "edges": edges}


SYSTEM_ARCHITECTURE_EXPLORER_DEFINITION = _linear_workflow_definition([
        "System Architecture Intent Agent",
        "System Workload Characterization Agent",
        "System gem5 Config Agent",
        "System Design Space Exploration Agent",
        "System gem5 Execution Agent",
        "System Performance Metrics Agent",
        "System Power Estimation Agent",
        "System Area Estimation Agent",
        "System PPA Tradeoff Agent",
        "System Visualization Agent",
        "System Architecture Report Agent",
])

DIGITAL_ARCH2RTL_DEFINITION = _linear_workflow_definition([
    "Digital Spec Agent",
    "Digital Architecture Agent",
    "Digital Microarchitecture Agent",
    "Digital Register Map Agent",
    "Digital RTL Agent",
    "Digital MBIST RTL Insertion Agent",
    "Digital Power Intent (UPF-lite) Agent",
    "Digital UPF Static Check Agent",
    "Digital IP Packaging & Handoff Agent",
    "Digital Arch2RTL Dashboard Agent",
])

DIGITAL_SPEC2RTL_CHECK_DEFINITION = _linear_workflow_definition([
    "Digital RTL Handoff Ingest Agent",
    "Digital Spec2RTL Conformance Agent",
])

DIGITAL_VERIFY_DEFINITION = _linear_workflow_definition([
    "Digital Verification Handoff Ingest Agent",
    "Digital Functional Coverage Agent",
    "Digital Testbench Generator Agent",
    "Digital Assertions (SVA) Agent",
    "Digital Simulation Control Agent",
    "Digital Simulation Execution Agent",
    "Digital Simulation Summary Coverage Agent",
])

DIGITAL_VERIFY_CLOSURE_DEFINITION = _linear_workflow_definition([
    "Digital Verify Closure Ingest Agent",
    "Digital Coverage Gap Analysis Agent",
    "Digital Failure Triage Agent",
    "Digital Failure Debug Agent",
    "Digital Closure Recommendation Agent",
])

DIGITAL_VERIFY_CLOSURE_LOOP_DEFINITION = _linear_workflow_definition([
    "Digital Verify Closure Ingest Agent",
    "Digital Coverage Gap Analysis Agent",
    "Digital Failure Triage Agent",
    "Digital Failure Debug Agent",
    "Digital Closure Recommendation Agent",
    "Digital Verification Plan Update Agent",
    "Digital Coverage Plan Update Agent",
    "Digital Testcase Seed Update Agent",
    "Digital Closure Rerun Planner Agent",
    "Digital Verification Handoff Ingest Agent",
    "Digital Testbench Generator Agent",
    "Digital Assertions (SVA) Agent",
    "Digital Functional Coverage Agent",
    "Digital Simulation Control Agent",
    "Digital Simulation Execution Agent",
    "Digital Simulation Summary Coverage Agent",
    "Digital Closure Iteration Judge Agent",
])

DIGITAL_ARCH2SYNTHESIS_DEFINITION = _linear_workflow_definition([
    "Digital RTL Handoff Ingest Agent",
    "Digital Spec Agent",
    "Digital Architecture Agent",
    "Digital Microarchitecture Agent",
    "Digital Register Map Agent",
    "Digital Clock & Reset Architecture Agent",
    "Digital Power Intent (UPF-lite) Agent",
    "Digital UPF Static Check Agent",
    "Digital RTL Agent",
    "Digital IP Packaging & Handoff Agent",
    "Digital Foundry Profile Agent",
    "Digital Implementation Setup Agent",
    "Digital Synthesis Agent",
    "Digital Logic Equivalence Check Agent",
    "Digital DFT Scan Stitching Agent",
    "Digital Post-DFT Logic Equivalence Check Agent",
    "Digital Synthesis Closure Agent",
    "Digital Scan ATPG Coverage Agent",
    "Digital MBIST Collateral Agent",
])

DIGITAL_ARCH2SIM_DEFINITION = _linear_workflow_definition([
    "Digital RTL Handoff Ingest Agent",
    "Digital Spec Agent",
    "Digital Architecture Agent",
    "Digital Microarchitecture Agent",
    "Digital Register Map Agent",
    "Digital Clock & Reset Architecture Agent",
    "Digital Power Intent (UPF-lite) Agent",
    "Digital UPF Static Check Agent",
    "Digital RTL Agent",
    "Digital IP Packaging & Handoff Agent",
    "Digital Testbench Generator Agent",
    "Digital Assertions (SVA) Agent",
    "Digital Functional Coverage Agent",
    "Digital Simulation Control Agent",
    "Digital Simulation Execution Agent",
    "Digital Simulation Summary Coverage Agent",
    "Digital Executive Summary Agent",
])

DIGITAL_ARCH2TAPEOUT_DEFINITION = _linear_workflow_definition([
    "Digital RTL Handoff Ingest Agent",
    "Digital Spec Agent",
    "Digital Architecture Agent",
    "Digital Microarchitecture Agent",
    "Digital Register Map Agent",
    "Digital Clock & Reset Architecture Agent",
    "Digital Power Intent (UPF-lite) Agent",
    "Digital UPF Static Check Agent",
    "Digital RTL Agent",
    "Digital IP Packaging & Handoff Agent",
    "Digital Foundry Profile Agent",
    "Digital Implementation Setup Agent",
    "Digital Synthesis Agent",
    "Digital Logic Equivalence Check Agent",
    "Digital DFT Scan Stitching Agent",
    "Digital Post-DFT Logic Equivalence Check Agent",
    "Digital Synthesis Closure Agent",
    "Digital Scan ATPG Coverage Agent",
    "Digital MBIST Collateral Agent",
    "Digital STA PrePlace Agent",
    "Digital Floorplan Agent",
    "Digital Placement Agent",
    "Digital STA PostPlace Agent",
    "Digital CTS Agent",
    "Digital STA PostCTS Agent",
    "Digital Route Agent",
    "Digital STA PostRoute Agent",
    "Digital Fill Agent",
    "Digital STA PostFill Agent",
    "Digital DRC Agent",
    "Digital LVS Agent",
    "Digital Tapeout Agent",
    "Digital Tapeout Logic Equivalence Check Agent",
    "Digital Executive Summary Agent",
    "Digital PD Signoff Closure Agent",
])

DIGITAL_DQA_DEFINITION = _linear_workflow_definition([
    "Digital RTL Handoff Ingest Agent",
    "Digital RTL Linting Agent",
    "Digital CDC Analysis Agent",
    "Digital Reset Integrity Agent",
    "Digital Synthesis Readiness Agent",
    "Digital DQA Summary Agent",
])

DIGITAL_SMOKE_DEFINITION = _linear_workflow_definition([
    "Digital RTL Handoff Ingest Agent",
    "Digital Smoke Preflight Agent",
    "Digital RTL Agent",
    "Digital Testbench Generator Agent",
    "Digital Assertions (SVA) Agent",
    "Digital Simulation Control Agent",
    "Digital Simulation Execution Agent",
    "Digital Simulation Summary Coverage Agent",
    "Digital Bug Localization Agent",
    "Digital Smoke Executive Summary Agent",
])

DIGITAL_INTEGRATE_DEFINITION = _linear_workflow_definition([
    "Digital RTL Handoff Ingest Agent",
    "Digital RTL Signature Agent",
    "Digital Integration Intent Agent",
    "Digital Top Assembly Agent",
    "Digital IP Packaging & Handoff Agent",
])

FIRMWARE_DOWNSTREAM_DEFINITION = [
    "Embedded Digital RTL Handoff Ingest Agent",
    "Embedded Firmware Register Extract Agent",
    "Embedded Rust Register Layer Generator Agent",
    "Embedded Register Validation Agent",
    "Embedded Rust Driver Scaffold Agent",
    "Embedded Interrupt Mapping Agent",
    "Embedded Firmware Integration Contract Agent",
    "Embedded ELF Build Agent",
    "Embedded Verilator Build Agent",
    "Embedded Cocotb Harness Agent",
    "Embedded Co Sim Runner Agent",
    "System Firmware CoSim Execution Agent",
    "System Firmware Coverage Summary Agent",
    "Embedded Coverage Collector Agent",
    "Embedded Validation Report Agent",
    "Embedded Firmware Executive Summary Agent",
    "System Software Handoff Package Agent",
]

EMBEDDED_RUN_DEFINITION = _linear_workflow_definition(FIRMWARE_DOWNSTREAM_DEFINITION)

SYSTEM_SOFTWARE_DEFINITION = _linear_workflow_definition([
    "System Software Handoff Ingest Agent",
    "System Software Capability Model Agent",
    "System Software API Contract Agent",
    "System Software SDK Scaffold Agent",
    "System Software HAL/Driver Adapter Agent",
    "System Software Config Schema Agent",
    "System Software Service Architecture Agent",
    "System Software Core Service Agent",
    "System Software Application Scaffold Agent",
    "System Software Build System Agent",
    "System Software Packaging Agent",
])

SYSTEM_SOFTWARE_VALIDATION_L2_DEFINITION = _linear_workflow_definition([
    "System Software Validation Ingest Agent",
    "System CoSim Ingest Agent",
    "System CoSim Contract Agent",
    "System CoSim Scenario Generator Agent",
    "System Software CoSim Harness Agent",
    "System Software CoSim Execution Agent",
    "System Software CoSim Trace Validation Agent",
    "System Software Validation Summary (L2)",
])

SYSTEM_RTL_AGENT_SEQUENCE = [
    "Digital Spec Agent",
    "Digital Architecture Agent",
    "Digital Microarchitecture Agent",
    "Digital Register Map Agent",
    "Digital RTL Agent",
    "Digital RTL Linting Agent",
    "Digital RTL Signature Agent",
    "Analog Spec Builder Agent",
    "Analog Behavioral Model Agent",
    "Analog Abstract Views Agent",
    "System Integration Intent Agent",
    "System Top Assembly Agent",
    "Analog Macro Interface Contract Agent",
    "System Assertions (SVA) Agent",
    "System RTL Handoff Package Agent",
    "System RTL Evidence Dashboard Agent",
]

SYSTEM_RTL_DEFINITION = _linear_workflow_definition(SYSTEM_RTL_AGENT_SEQUENCE)

SYSTEM_DQA_DEFINITION = _linear_workflow_definition([
    *SYSTEM_RTL_AGENT_SEQUENCE,
    "Digital RTL Linting Agent",
    "Digital CDC Analysis Agent",
    "Digital Reset Integrity Agent",
    "Digital Synthesis Readiness Agent",
    "Digital DQA Summary Agent",
])

SYSTEM_SYNTHESIS_DEFINITION = _linear_workflow_definition([
    *SYSTEM_RTL_AGENT_SEQUENCE,
    "Digital Foundry Profile Agent",
    "Digital Implementation Setup Agent",
    "Digital Synthesis Agent",
    "Digital Logic Equivalence Check Agent",
    "Digital DFT Scan Stitching Agent",
    "Digital Post-DFT Logic Equivalence Check Agent",
    "System Synthesis Closure Agent",
    "Digital Scan ATPG Coverage Agent",
    "Digital MBIST Collateral Agent",
])

SYSTEM_PD_DEFINITION = _linear_workflow_definition([
    *SYSTEM_RTL_AGENT_SEQUENCE,
    "Analog Macro Interface Validation Agent",
    "Digital Foundry Profile Agent",
    "Digital Implementation Setup Agent",
    "Digital Synthesis Agent",
    "Digital Logic Equivalence Check Agent",
    "Digital DFT Scan Stitching Agent",
    "Digital Post-DFT Logic Equivalence Check Agent",
    "System Synthesis Closure Agent",
    "Digital Scan ATPG Coverage Agent",
    "Digital MBIST Collateral Agent",
    "Analog Sky130 SPICE Netlist Agent",
    "Analog GDS Generation Agent",
    "Analog LEF Extraction Agent",
    "Analog Liberty Characterization Agent",
    "Analog Collateral Consistency Agent",
    "Analog Physical Collateral Package Agent",
    "Digital STA PrePlace Agent",
    "Digital Floorplan Agent",
    "Digital Placement Agent",
    "Digital STA PostPlace Agent",
    "Digital CTS Agent",
    "Digital STA PostCTS Agent",
    "Digital Route Agent",
    "Digital STA PostRoute Agent",
    "Digital Fill Agent",
    "Digital STA PostFill Agent",
    "Digital DRC Agent",
    "Digital LVS Agent",
    "Digital Tapeout Agent",
    "Digital Tapeout Logic Equivalence Check Agent",
    "Digital Executive Summary Agent",
    "System PD Signoff Closure Agent",
])

SYSTEM_FIRMWARE_DEFINITION = _linear_workflow_definition([
    *SYSTEM_RTL_AGENT_SEQUENCE,
    *FIRMWARE_DOWNSTREAM_DEFINITION,
])

SYSTEM_SIM_DOWNSTREAM_DEFINITION = [
    "System CoSim Ingest Agent",
    "System Assertions (SVA) Agent",
    "System Testbench Generator Agent",
    "System Functional Coverage Agent",
    "System Simulation Control Agent",
    "System Simulation Execution Agent",
    "System Simulation Coverage Summary Agent",
]

SYSTEM_SIM_DEFINITION = _linear_workflow_definition([
    *SYSTEM_RTL_AGENT_SEQUENCE,
    *SYSTEM_SIM_DOWNSTREAM_DEFINITION,
])

SYSTEM_SIM_CLOSURE_DEFINITION = _linear_workflow_definition([
    "System Sim Closure Ingest Agent",
    "System Sim Coverage Gap Analysis Agent",
    "System Sim Failure Triage Agent",
    "System Sim Failure Debug Agent",
    "System Sim Closure Recommendation Agent",
])

SYSTEM_SIM_CLOSURE_LOOP_DEFINITION = _linear_workflow_definition([
    "System Sim Closure Ingest Agent",
    "System Sim Coverage Gap Analysis Agent",
    "System Sim Failure Triage Agent",
    "System Sim Failure Debug Agent",
    "System Sim Closure Recommendation Agent",
    "System Sim Verification Plan Update Agent",
    "System Sim Coverage Plan Update Agent",
    "System Sim Testcase Seed Update Agent",
    "System Sim Closure Rerun Planner Agent",
    "System Assertions (SVA) Agent",
    "System Testbench Generator Agent",
    "System Functional Coverage Agent",
    "System Simulation Control Agent",
    "System Simulation Execution Agent",
    "System Simulation Coverage Summary Agent",
    "System Sim Closure Iteration Judge Agent",
])

SYSTEM_PRODUCT_APP_BUILDER_DEFINITION = _linear_workflow_definition([
    "System Product Collateral Ingest Agent",
    "System Product Capability Model Agent",
    "System Product Dashboard Agent",
    "System Product Package Agent",
])

LOCAL_PREBUILT_WORKFLOW_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "Digital_Arch2RTL": DIGITAL_ARCH2RTL_DEFINITION,
    "Digital_Spec2RTL_Check": DIGITAL_SPEC2RTL_CHECK_DEFINITION,
    "Digital_Verify": DIGITAL_VERIFY_DEFINITION,
    "Digital_Verify_Closure": DIGITAL_VERIFY_CLOSURE_DEFINITION,
    "Digital_Verify_Closure_Loop": DIGITAL_VERIFY_CLOSURE_LOOP_DEFINITION,
    "Digital_Arch2Synthesis": DIGITAL_ARCH2SYNTHESIS_DEFINITION,
    "Digital_Arch2Sim": DIGITAL_ARCH2SIM_DEFINITION,
    "Digital_Arch2Tapeout": DIGITAL_ARCH2TAPEOUT_DEFINITION,
    "Digital_DQA": DIGITAL_DQA_DEFINITION,
    "Digital_Smoke": DIGITAL_SMOKE_DEFINITION,
    "Digital_Integrate": DIGITAL_INTEGRATE_DEFINITION,
    "System_Architecture_Explorer": SYSTEM_ARCHITECTURE_EXPLORER_DEFINITION,
    "System_Cache_Tuning": SYSTEM_ARCHITECTURE_EXPLORER_DEFINITION,
    "System_ISA_Compare": SYSTEM_ARCHITECTURE_EXPLORER_DEFINITION,
    "System_Memory_Bottleneck": SYSTEM_ARCHITECTURE_EXPLORER_DEFINITION,
    "System_CPU_Model": SYSTEM_ARCHITECTURE_EXPLORER_DEFINITION,
    "Embedded_Run": EMBEDDED_RUN_DEFINITION,
    "System_RTL": SYSTEM_RTL_DEFINITION,
    "System_DQA": SYSTEM_DQA_DEFINITION,
    "System_Sim": SYSTEM_SIM_DEFINITION,
    "System_Sim_Closure": SYSTEM_SIM_CLOSURE_DEFINITION,
    "System_Sim_Closure_Loop": SYSTEM_SIM_CLOSURE_LOOP_DEFINITION,
    "System_Firmware": SYSTEM_FIRMWARE_DEFINITION,
    "System_Synthesis": SYSTEM_SYNTHESIS_DEFINITION,
    "System_PD": SYSTEM_PD_DEFINITION,
    "System_Software": SYSTEM_SOFTWARE_DEFINITION,
    "System_Software_Validation_L2": SYSTEM_SOFTWARE_VALIDATION_L2_DEFINITION,
    "System_Product_App_Builder": SYSTEM_PRODUCT_APP_BUILDER_DEFINITION,
}

LOCAL_RUNTIME_WORKFLOW_OVERRIDES = {
    "Digital_Arch2RTL",
    "Digital_Spec2RTL_Check",
    "Digital_Verify",
    "Digital_Verify_Closure",
    "Digital_Verify_Closure_Loop",
    "Digital_Arch2Synthesis",
    "Digital_Arch2Sim",
    "Digital_Arch2Tapeout",
    "Digital_DQA",
    "Digital_Smoke",
    "Digital_Integrate",
    "Embedded_Run",
    "System_RTL",
    "System_DQA",
    "System_Sim",
    "System_Sim_Closure",
    "System_Sim_Closure_Loop",
    "System_Firmware",
    "System_Synthesis",
    "System_PD",
    "System_Software",
    "System_Software_Validation_L2",
    "System_Product_App_Builder",
}

# Dynamically load user-created agents as modules under `agents/` (optional)
import importlib, pkgutil, agents
AGENT_REGISTRY: Dict[str, Any] = {}  # custom/marketplace registry

def load_custom_agents():
    global AGENT_REGISTRY
    for _, name, _ in pkgutil.iter_modules(agents.__path__):
        if name not in AGENT_REGISTRY:
            try:
                module = importlib.import_module(f"agents.{name}")
                if hasattr(module, "run_agent"):
                    AGENT_REGISTRY[name] = module.run_agent
                    logger.info(f"⚡ Custom agent loaded: {name}")
            except Exception as e:
                logger.warning(f"⚠️ Could not load custom agent {name}: {e}")

load_custom_agents()

# ==========================================================
# ---------- Logging Helpers (ID-based) ----------
# ==========================================================

def append_log_workflow(
    workflow_id: str,
    line: str,
    status: Optional[str] = None,
    phase: Optional[str] = None,
    artifacts: Optional[dict] = None,
):
    """
    Append a line to workflows.logs by workflow ID, and optionally update status/phase/artifacts.

    workflows.logs is best-effort UI convenience and may be VARCHAR-limited.
    Keep it compact; never let logging kill a run.
    """
    try:
        # 1) Truncate the incoming line first (prevents single huge log entries)
        safe_line = _truncate_tail(str(line or ""), MAX_LOG_LINE_CHARS)

        # 2) Load current logs and append
        row = supabase.table("workflows").select("logs").eq("id", workflow_id).single().execute()
        current = (row.data or {}).get("logs") or ""
        new_logs = (current + ("\n" if current else "") + safe_line).strip()
        new_logs = _truncate_tail(new_logs, MAX_LOG_CHARS)

        update = {"logs": new_logs, "updated_at": datetime.utcnow().isoformat()}
        if status:
            update["status"] = status
        if phase:
            update["phase"] = phase

        # 3) Avoid pushing large artifacts JSON through this path
        if artifacts is not None:
            try:
                payload = json.dumps(artifacts, ensure_ascii=False, separators=(",", ":"))
                if len(payload) <= MAX_WORKFLOW_ARTIFACTS_JSON_CHARS:
                    update["artifacts"] = artifacts
                else:
                    logger.warning(
                        f"⚠️ append_log_workflow skipping artifacts update (too large) workflow={workflow_id}"
                    )
            except Exception:
                logger.warning(
                    f"⚠️ append_log_workflow skipping artifacts update (serialization error) workflow={workflow_id}"
                )

        # 4) Write
        supabase.table("workflows").update(update).eq("id", workflow_id).execute()

    except Exception as e:
        # Last resort: retry with ultra-small logs (never crash run due to logging)
        try:
            fallback = {"logs": _truncate_tail(str(line or ""), 200), "updated_at": datetime.utcnow().isoformat()}
            if status:
                fallback["status"] = status
            if phase:
                fallback["phase"] = phase
            supabase.table("workflows").update(fallback).eq("id", workflow_id).execute()
        except Exception:
            logger.warning(f"⚠️ append_log_workflow failed: {e}")


def append_log_run(run_id: str, line: str, status: Optional[str] = None,
                   artifacts_path: Optional[str] = None):
    """
    Append a line to runs.logs by run ID, and optionally update status and artifacts_path.

    Note: runs.logs can grow large on long runs; we truncate to avoid PostgREST payload limits.
    """
    try:
        row = supabase.table("runs").select("logs").eq("id", run_id).single().execute()
        current = (row.data or {}).get("logs") or ""
        new_logs = (current + ("\n" if current else "") + line).strip()
        new_logs = _truncate_tail(new_logs, MAX_LOG_CHARS)


        update = {"logs": new_logs}
    
        if status:
            update["status"] = status
        if artifacts_path is not None:
            update["artifacts_path"] = artifacts_path

        supabase.table("runs").update(update).eq("id", run_id).execute()
    except Exception as e:
        logger.warning(f"⚠️ append_log_run failed: {e}")

# ==========================================================
# ---------- Routes ----------
# ==========================================================

def _require_user_id(request: Request) -> str:
    # 1) Header fallback (same pattern you already use elsewhere)
    headers = request.headers
    header_user_id = (
        headers.get("x-user-id")
        or headers.get("x-supabase-user-id")
        or headers.get("x-client-user-id")
    )
    if header_user_id:
        return header_user_id

    # 2) JWT fallback
    token_data = verify_token(request)
    user_id = token_data.get("sub")
    if not user_id or user_id == "anonymous":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user_id

from typing import Tuple


def _load_workflow_def_by_name(
    name: str,
    user_id: Optional[str],
    force_platform_definition: bool = False,
) -> Dict[str, Any]:
    """
      Loads workflow template from public.workflows by exact name.
      Prefer user-owned workflow if present; fallback to global prebuilt.
      For prebuilt workflows, user_id is intentionally ignored.
    """
    select_cols = "id,name,definitions,nodes,edges,loop_type,is_prebuilt,user_id"

    def unpack(row: Dict[str, Any]) -> Dict[str, Any]:
        defn = row.get("definitions") or {}
        return {
              "nodes": (defn.get("nodes") if isinstance(defn, dict) else None) or row.get("nodes") or [],
              "edges": (defn.get("edges") if isinstance(defn, dict) else None) or row.get("edges") or [],
        }

    if force_platform_definition and name in LOCAL_RUNTIME_WORKFLOW_OVERRIDES:
        return LOCAL_PREBUILT_WORKFLOW_DEFINITIONS[name]

    # 1) User-owned custom workflow.
    if user_id:
        r = (
            supabase.table("workflows")
            .select(select_cols)
            .eq("name", name)
            .eq("user_id", user_id)
            .eq("is_prebuilt", False)
            .limit(1)
            .execute()
        )
        if r.data:
            return unpack(r.data[0])

    # Platform-owned runtime overrides keep shipped app execution in sync with
    # executable agent registrations even if an older global template remains.
    if name in LOCAL_RUNTIME_WORKFLOW_OVERRIDES:
        return LOCAL_PREBUILT_WORKFLOW_DEFINITIONS[name]

    # 2) Global prebuilt workflow. user_id is intentionally ignored.
    r2 = (
        supabase.table("workflows")
        .select(select_cols)
        .eq("name", name)
        .eq("is_prebuilt", True)
        .limit(1)
        .execute()
    )
    if r2.data:
        return unpack(r2.data[0])

    if name in LOCAL_PREBUILT_WORKFLOW_DEFINITIONS:
        return LOCAL_PREBUILT_WORKFLOW_DEFINITIONS[name]

    raise HTTPException(status_code=404, detail=f"Workflow template not found: {name}")




def _toposort_nodes(defn: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = defn.get("nodes") or []
    edges = defn.get("edges") or []
    if not nodes or not edges:
        return nodes

    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    indeg = {nid: 0 for nid in by_id.keys()}
    out = {nid: [] for nid in by_id.keys()}

    for e in edges:
        s = e.get("source")
        t = e.get("target")
        if s in out and t in indeg:
            out[s].append(t)
            indeg[t] += 1

    queue = [nid for nid, d in indeg.items() if d == 0]
    ordered = []

    while queue:
        nid = queue.pop(0)
        ordered.append(by_id[nid])
        for t in out.get(nid, []):
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)

    # cycle-safe: append leftovers
    ordered_ids = {n.get("id") for n in ordered}
    for nid, n in by_id.items():
        if nid not in ordered_ids:
            ordered.append(n)

    return ordered


def _definition_to_executor_nodes(defn: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Converts Studio workflow nodes -> executor nodes: [{"label": "<backendLabel>"}]
    """
    ordered = _toposort_nodes(defn)
    out = []
    for n in ordered:
        data = (n or {}).get("data") or {}
        label = (data.get("backendLabel") or data.get("uiLabel") or n.get("type") or "").strip()
        if label:
            out.append({"label": label})
    return out


def _insert_node_before_once(
    nodes: List[Dict[str, Any]],
    label: str,
    before_label: str,
) -> List[Dict[str, Any]]:
    if any((node or {}).get("label") == label for node in nodes):
        return nodes
    out: List[Dict[str, Any]] = []
    inserted = False
    for node in nodes:
        if not inserted and (node or {}).get("label") == before_label:
            out.append({"label": label})
            inserted = True
        out.append(node)
    if not inserted:
        out.append({"label": label})
    return out


def _execute_agent_with_runtime(
    agent_name: str,
    agent_fn: Any,
    shared_state: Dict[str, Any],
    workflow_id: str,
    run_id: Optional[str],
    loop_type: str,
) -> Dict[str, Any]:
    """
    Route legacy run_agent(state) functions through Agent Runtime Contract v1.

    The returned value remains the legacy state/result dict so existing workflow
    artifact indexing, run logging, and downstream state merging stay unchanged.
    """
    context = AgentContext(
        agent_name=agent_name,
        state=shared_state,
        workflow_id=str(workflow_id or shared_state.get("workflow_id") or "default"),
        run_id=run_id or shared_state.get("run_id"),
        loop_type=loop_type or shared_state.get("loop_type"),
        artifact_dir=shared_state.get("artifact_dir"),
        user_id=shared_state.get("user_id"),
    )
    runtime_result = execute_legacy_agent(agent_fn, context)
    return runtime_result.raw or runtime_result.to_state_update()






@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}


@app.get("/ready")
def readiness():
    from deployment_readiness import build_readiness_payload

    payload = build_readiness_payload(supabase)
    return JSONResponse(status_code=200 if payload.get("ok") else 503, content=payload)


@app.get("/runs/recent")
def recent_runs(request: Request, limit: int = Query(10, ge=1, le=50)):
    user = verify_token(request)
    res = (
        supabase.table("runs")
        .select("id, workflow_id, loop_type, status, created_at, completed_at, artifacts_path")
        .eq("user_id", user.get("sub"))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data

@app.get("/stats")
def stats():
    wf = supabase.table("workflows").select("id", count="exact").execute()
    run = supabase.table("runs").select("id", count="exact").execute()
    return {
        "workflows": wf.count if hasattr(wf, "count") else None,
        "runs": run.count if hasattr(run, "count") else None,
    }

def _run_nodes_with_shared_state(
    workflow_id: str,
    run_id: str,
    loop_type: str,
    nodes: List[Dict[str, Any]],
    shared_state: Dict[str, Any],
):
    # use the same map logic as your normal executor
    if loop_type == "system":
        loop_map = {}
        loop_map.update(DIGITAL_AGENT_FUNCTIONS)
        loop_map.update(ANALOG_AGENT_FUNCTIONS)
        loop_map.update(EMBEDDED_AGENT_FUNCTIONS)
        loop_map.update(VALIDATION_AGENT_FUNCTIONS)
        loop_map.update(SYSTEM_AGENT_FUNCTIONS)
    else:
        # IMPORTANT: use loop-specific maps (validation needs VALIDATION_AGENT_FUNCTIONS)
        loop_map = {
            "digital": DIGITAL_AGENT_FUNCTIONS,
            "analog": ANALOG_AGENT_FUNCTIONS,
            "embedded": EMBEDDED_AGENT_FUNCTIONS,
            "validation": VALIDATION_AGENT_FUNCTIONS,
            "system": SYSTEM_AGENT_FUNCTIONS,
        }.get(loop_type, DIGITAL_AGENT_FUNCTIONS)

    agent_map = dict(loop_map)
    agent_map.update(AGENT_REGISTRY)

    def _norm(s: str) -> str:
        return (s or "").strip()

    agent_map_norm = {_norm(k): v for k, v in agent_map.items()}

    def _prepare_scoped_agent_state(label: str) -> None:
        if label.startswith("Digital "):
            digital_text = shared_state.get("digital_spec_text") or shared_state.get("digital_spec") or ""
            if isinstance(digital_text, str) and digital_text.strip():
                shared_state["spec_text"] = digital_text.strip()
                shared_state["spec"] = digital_text.strip()
        elif label.startswith("Analog "):
            analog_text = (
                shared_state.get("analog_spec_text")
                or shared_state.get("analog_datasheet")
                or shared_state.get("datasheet_text")
                or ""
            )
            if isinstance(analog_text, str) and analog_text.strip():
                shared_state["datasheet_text"] = analog_text.strip()
                shared_state["analog_datasheet"] = analog_text.strip()
                shared_state["spec_text"] = analog_text.strip()
                shared_state["spec"] = analog_text.strip()
        elif label.startswith("System "):
            soc_text = (
                shared_state.get("soc_integration_spec_text")
                or shared_state.get("system_integration_description")
                or shared_state.get("soc_integration_description")
                or shared_state.get("integration_description")
                or ""
            )
            if isinstance(soc_text, str) and soc_text.strip():
                shared_state["system_integration_description"] = soc_text.strip()
                shared_state["soc_integration_description"] = soc_text.strip()
                shared_state["integration_description"] = soc_text.strip()

    for node in (nodes or []):
        label = (node or {}).get("label") or ""
        label = label.strip()
        if not label:
            continue
        if loop_type == "system":
            _prepare_scoped_agent_state(label)

        append_log_workflow(workflow_id, f"⚙️ Running {label}")
        append_log_run(run_id, f"⚙️ Running {label}")

        fn = agent_map_norm.get(label)
        if not fn:
            append_log_workflow(workflow_id, f"❌ No agent implementation found for: {label}")
            append_log_run(run_id, f"❌ No agent implementation found for: {label}")
            continue

        try:
            result = _execute_agent_with_runtime(
                label,
                fn,
                shared_state,
                workflow_id,
                run_id,
                loop_type,
            )
            if isinstance(result, dict):
                shared_state.update(result)
                result_status = str(result.get("status", ""))
                if result_status.startswith("❌"):
                    raise RuntimeError(result_status)

            append_log_workflow(workflow_id, f"✅ {label} done")
            append_log_run(run_id, f"✅ {label} done")

        except Exception as e:
            append_log_workflow(workflow_id, f"❌ {label} failed: {type(e).__name__}: {e}")
            append_log_run(run_id, f"❌ {label} failed: {type(e).__name__}: {e}")
            if shared_state.get("_fail_fast_on_agent_error"):
                raise
            # Continue for legacy workflows that preserve best-effort execution.
def _has_bench_schematic(bench_id: str) -> bool:
    try:
        rows = (
            supabase.table("validation_bench_connections")
            .select("schematic,updated_at")
            .eq("bench_id", bench_id)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return False
        sch = rows[0].get("schematic") or {}
        return isinstance(sch, dict) and sch != {}
    except Exception:
        return False


@app.post("/run_workflow")
async def run_workflow(
    request: Request,
    background_tasks: BackgroundTasks,
    workflow: str = Form(...),
    file: UploadFile = File(None),
    spec_text: str = Form(None),
    instrument_ids: Optional[str] = Form(None),
    scope_json: Optional[str] = Form(None),
    bench_id: Optional[str] = Form(None),
    bench_name: Optional[str] = Form(None),
    bench_location: Optional[str] = Form(None),
    test_plan_name: Optional[str] = Form(None),
    preview_test_plan_json: Optional[str] = Form(None),
):

    """
    Starts a workflow run:
      - Reads loop_type from payload
      - Creates rows in workflows + runs
      - Queues background execution
    """
    try:
        sdk_user_id = getattr(request.state, "user_id", None)
        user = verify_token(request) if not sdk_user_id else {"sub": sdk_user_id}
        user_id = user.get("sub") if user and user.get("sub") and user.get("sub") != "anonymous" else None
        workflow_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        data = json.loads(workflow)

        # ✅ Guard: if frontend accidentally sends "null" or non-dict, fail cleanly
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Invalid workflow payload (expected JSON object).")

        run_config: Dict[str, Any] = {}
        if run_config_json:
            try:
                parsed_run_config = json.loads(run_config_json)
                if not isinstance(parsed_run_config, dict):
                    raise ValueError("run_config_json must decode to an object")
                run_config = parsed_run_config
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid run_config_json: {exc}")
        if run_config:
            data["run_config"] = run_config
            data["workflow_config"] = run_config

        # ✅ attach instrument IDs into workflow payload so agents can read it

                # ✅ attach instrument IDs into workflow payload so agents can read it
        if instrument_ids:
            try:
                parsed = json.loads(instrument_ids)
                if isinstance(parsed, list):
                    data["instrument_ids"] = parsed
                elif isinstance(parsed, str):
                    data["instrument_ids"] = [parsed]
                else:
                    data["instrument_ids"] = []
            except Exception:
                # fallback: treat as single id
                data["instrument_ids"] = [instrument_ids]

        if bench_id:
           data["bench_id"] = bench_id

        if bench_name:
           data["bench_name"] = bench_name

        if bench_location:
           data["bench_location"] = bench_location

        if test_plan_name:
           data["test_plan_name"] = test_plan_name



        
        # payload contains nodes with exact backend "label"
        loop_type = (data.get("loop_type") or "digital").lower().strip()
        logger.info(f"[DEBUG] Client submitted loop_type={data.get('loop_type')}")
        logger.info(f"[DEBUG] Client submitted loop_type={data.get('instrument_ids')}")
        logger.info(f"[DEBUG] Normalized loop_type={loop_type}")

        if not user_id:
          user_id = request.headers.get("x-user-id")

        logger.info(f"[DEBUG] user_id ={user_id}")

        supabase.table("workflows").insert({
           "id": workflow_id,
           "user_id": user_id,
           "name": f"{loop_type.capitalize()} Loop Run",
           "status": "running",
           "phase": "queued",
           "logs": "🚀 Workflow queued.",
           "created_at": now,
           "updated_at": now,
           "artifacts": {},
           "loop_type": loop_type,
           "definitions": data
        }).execute()

        # Prepare artifact dir
        user_folder = str(user_id or "anonymous") 
        artifact_dir = os.path.join("artifacts", user_folder, workflow_id)
        os.makedirs(artifact_dir, exist_ok=True)

        # Save uploaded file (optional)
        upload_path = None
        if file is not None:
            contents = await file.read()
            upload_path = os.path.join(artifact_dir, file.filename)
            with open(upload_path, "wb") as f:
                f.write(contents)

        # Save spec text (optional)
        if spec_text:
            with open(os.path.join(artifact_dir, "spec.txt"), "w") as f:
                f.write(spec_text)

        # Insert per-run row (clean execution history)
        supabase.table("runs").insert({
            "id": run_id,
            "user_id": user_id,
            "workflow_id": workflow_id,
            "loop_type": loop_type,
            "status": "running",
            "logs": "🚀 Run started.",
            "artifacts_path": artifact_dir,
            "created_at": now
        }).execute()

        append_log_workflow(workflow_id, f"📘 Loop: {loop_type}", phase="start")
        append_log_run(run_id, f"📘 Loop: {loop_type}")
        time.sleep(0.2)

        # Queue background execution
        background_tasks.add_task(
            execute_workflow_background,
            workflow_id,
            run_id,
            user_id,
            loop_type,
            json.dumps(data),
            spec_text,
            upload_path,
            artifact_dir,
            scope_json,
            preview_test_plan_json,
        )

        return JSONResponse({
            "workflow_id": workflow_id,
            "run_id": run_id,
            "loop_type": loop_type,
            "status": "queued",
            "upgrade_hint": _upgrade_hint_for_user(user_id),
        })

    except Exception as e:
        logger.error(f"❌ run_workflow failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================================
# ---------- Background executor ----------
# ==========================================================

def _extract_node_data_fields(obj: dict) -> dict:
    out = {}
    if not isinstance(obj, dict):
        return out

    nodes = obj.get("nodes") or []
    if not nodes and isinstance(obj.get("definitions"), dict):
        nodes = obj["definitions"].get("nodes") or []

    for n in nodes:
        data_block = (n or {}).get("data") or {}
        if not isinstance(data_block, dict):
            continue

        for k, v in data_block.items():
            if v is None:
                continue
            if isinstance(v, str) and v.strip():
                out.setdefault(k, v.strip())
            elif isinstance(v, (dict, list)) and v:
                out.setdefault(k, v)

    return out

def execute_workflow_background(
    workflow_id: str,
    run_id: str,
    user_id: str,
    loop_type: str,
    workflow_json: str,
    spec_text: Optional[str],
    upload_path: Optional[str],
    artifact_dir: str,
    scope_json=None,
    preview_test_plan_json: Optional[str] = None,
):
    """
    Executes the workflow with loop-aware agent resolution and dual logging (workflows + runs).
    - Exact label resolution against DIGITAL/ANALOG/EMBEDDED maps
    - Falls back to AGENT_REGISTRY (custom agents)
    - Queues at *Sim Agent* phase for external runner (any loop)
    """
    try:
        os.makedirs(artifact_dir, exist_ok=True)
        data = json.loads(workflow_json)

        if loop_type not in AGENT_FUNCTIONS:
           append_log_workflow(workflow_id, f"⚠️ Unknown loop_type={loop_type}, defaulting to digital.")
           loop_type = "digital"

        #loop_map = AGENT_FUNCTIONS.get(loop_type, DIGITAL_AGENT_FUNCTIONS)

        # For system: merge ALL domains
        if loop_type == "system":
           loop_map = {}
           loop_map.update(DIGITAL_AGENT_FUNCTIONS)
           loop_map.update(ANALOG_AGENT_FUNCTIONS)
           loop_map.update(EMBEDDED_AGENT_FUNCTIONS)
           loop_map.update(VALIDATION_AGENT_FUNCTIONS)
           loop_map.update(SYSTEM_AGENT_FUNCTIONS)
        else:
    # Only agents from this domain
           loop_map = AGENT_FUNCTIONS.get(loop_type, DIGITAL_AGENT_FUNCTIONS)

        # if loop_type == "system":
        #   has_validation = any(
        #       n.get("label") == "System Workflow Agent"
        #        for n in (data.get("nodes") or [])
         #   )
        #   if not has_validation:
        #        logger.info("🧩 Auto-appending System Workflow Agent as final step for System Loop.")
                # Append as a node for execution
        #       data["nodes"].append({"label": "System Workflow Agent"})
        #      append_log_workflow(workflow_id, "🧩 Added System Workflow Agent as final validation step.")

        # Merge with dynamic/custom agents



        agent_map = dict(loop_map)
        agent_map.update(AGENT_REGISTRY)

        def _norm_label(s: str) -> str:
          return (s or "").strip()

        agent_map_norm = { _norm_label(k): v for k, v in agent_map.items() }

                # 🔗 Shared state across all agents in this workflow
        shared_state = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "artifact_dir": artifact_dir,
        }
        run_config = data.get("run_config") if isinstance(data.get("run_config"), dict) else {}
        workflow_config = data.get("workflow_config") if isinstance(data.get("workflow_config"), dict) else run_config
        if run_config:
            shared_state["run_config"] = run_config
            shared_state["workflow_config"] = workflow_config
            for key, value in run_config.items():
                if key not in shared_state and value is not None:
                    shared_state[key] = value
            append_log_workflow(workflow_id, f"[CONFIG] Applied run config keys: {', '.join(sorted(run_config.keys()))}")
            append_log_run(run_id, f"[CONFIG] Applied run config keys: {', '.join(sorted(run_config.keys()))}")
        # ✅ inject supabase once (so WF5 services can query history)
        shared_state["supabase_client"] = supabase
        if user_id:
            shared_state["user_id"] = user_id
        if upload_path:
            shared_state["uploaded_file"] = upload_path

        if spec_text:
            shared_state["spec_text"] = spec_text
            shared_state["spec"] = spec_text

        # NEW: also inject workflow/app payload fields into shared_state
        payload = {}
        if isinstance(data, dict):
            payload = data.get("payload") or {}
            definitions = data.get("definitions") or {}
            if not payload and isinstance(definitions, dict):
                payload = definitions.get("payload") or {}
 
        if isinstance(payload, dict):
            for k, v in payload.items():
                if v is not None:
                   shared_state[k] = v

        # NEW: also lift node.data fields from workflow definitions / Studio canvas
        node_fields = _extract_node_data_fields(data)
        for k, v in node_fields.items():
            if k not in shared_state and v is not None:
                shared_state[k] = v


                # ---------------------------------------------------------
        # Domain-specific canonical input normalization
        # Keep three separate source-of-truth channels:
        #   - digital_spec_text
        #   - analog_spec_text
        #   - system_integration_description
        # ---------------------------------------------------------
        digital_text = (
            shared_state.get("digital_spec_text")
            or (data.get("digital_spec_text") if isinstance(data, dict) else None)
            or ""
        ).strip()

        analog_text = (
            shared_state.get("analog_spec_text")
            or shared_state.get("analog_datasheet")
            or shared_state.get("datasheet_text")
            or (data.get("analog_spec_text") if isinstance(data, dict) else None)
            or ""
        ).strip()

        system_desc = (
            shared_state.get("system_integration_description")
            or shared_state.get("soc_integration_description")
            or shared_state.get("integration_description")
            or shared_state.get("soc_integration_spec_text")
            or shared_state.get("soc_integration_spec")
            or shared_state.get("soc_spec")
            or shared_state.get("system_spec")
            or (data.get("system_integration_description") if isinstance(data, dict) else None)
            or (data.get("soc_integration_description") if isinstance(data, dict) else None)
            or (data.get("integration_description") if isinstance(data, dict) else None)
            or (data.get("soc_integration_spec_text") if isinstance(data, dict) else None)
            or (data.get("soc_integration_spec") if isinstance(data, dict) else None)
            or (data.get("soc_spec") if isinstance(data, dict) else None)
            or (data.get("system_spec") if isinstance(data, dict) else None)
            or ""
        ).strip()

        if digital_text:
            shared_state["digital_spec_text"] = digital_text
            # optional digital compatibility aliases
            shared_state["digital_spec"] = digital_text

        if analog_text:
            shared_state["analog_spec_text"] = analog_text
            shared_state["datasheet_text"] = analog_text
            shared_state["analog_datasheet"] = analog_text

        if system_desc:
            shared_state["system_integration_description"] = system_desc
            shared_state["soc_integration_description"] = system_desc
            shared_state["integration_description"] = system_desc
            shared_state["soc_integration_spec_text"] = system_desc

        # IMPORTANT:
        # Do NOT globally write shared_state["spec"] or ["spec_text"] here.
        # Those generic aliases are what create cross-domain contamination.

        append_log_workflow(
            workflow_id,
            f"[DEBUG] normalized inputs: "
            f"digital={bool(digital_text)} len={len(digital_text)}, "
            f"analog={bool(analog_text)} len={len(analog_text)}, "
            f"soc={bool(system_desc)} len={len(system_desc)}"
        )

        
        if scope_json:
          try:
            shared_state["scope"] = json.loads(scope_json)
          except Exception:
            shared_state["scope"] = {}

        # ✅ ADD THIS
        instrument_ids = (data or {}).get("instrument_ids")
        if instrument_ids:
            shared_state["instrument_ids"] = instrument_ids  
        bench_id = (data or {}).get("bench_id")
        if bench_id:
            shared_state["bench_id"] = bench_id

        bench_name = (data or {}).get("bench_name")
        if bench_name:
            shared_state["bench_name"] = bench_name

        bench_location = (data or {}).get("bench_location")
        if bench_location:
            shared_state["bench_location"] = bench_location

        test_plan_name = (data or {}).get("test_plan_name")
        if test_plan_name:
            shared_state["test_plan_name"] = test_plan_name
        # Prefer form field (WF1 sends it as Form), fallback to workflow JSON
        #if test_plan_name and test_plan_name.strip():
        #   shared_state["test_plan_name"] = test_plan_name.strip()
        #else:
        #   tp = (data or {}).get("test_plan_name")
        #   if tp:
        #       shared_state["test_plan_name"] = str(tp).strip()


        # ✅ Preview plan override (WF1): if provided, treat as authoritative test_plan
        if preview_test_plan_json:
            try:
                shared_state["test_plan"] = json.loads(preview_test_plan_json)
                shared_state["test_plan_source"] = "preview_override"
            except Exception:
                # if preview JSON is invalid, ignore and fall back to generating
                pass
        # ✅ If preview override present, save it to validation_test_plans (without regenerating)
        if isinstance(shared_state.get("test_plan"), dict) and shared_state["test_plan"].get("tests"):
            try:
                from agents.validation.validation_test_plan_agent import save_plan_to_supabase
                save_plan_to_supabase(shared_state, shared_state["test_plan"])
            except Exception as e:
                append_log_run(run_id, f"⚠️ Failed to save preview test plan to table: {type(e).__name__}: {e}")
                append_log_workflow(workflow_id, f"⚠️ Failed to save preview test plan to table: {type(e).__name__}: {e}")


        append_log_workflow(workflow_id, "⚡ Executing workflow agents ...")
        append_log_run(run_id, "⚡ Executing workflow agents ...")
        time.sleep(0.2)

        nodes = data.get("nodes", []) or []
        missing = [n["label"] for n in nodes if n["label"] not in agent_map_norm]
        if missing:
          append_log_workflow(workflow_id, f"⚠️ Missing agent implementations: {', '.join(missing)}")
        for node in nodes:
            label = (node or {}).get("label", "")
            step = label or "agent"
            msg = f"⚙️ Running {step} ..."

             # ✅ WF1: if preview provided, do NOT regenerate test plan (prevents renaming mismatch)
            if step == "Validation Test Plan Agent" and isinstance(shared_state.get("test_plan"), dict) and shared_state["test_plan"].get("tests"):
                append_log_workflow(workflow_id, "⏭️ Skipped Validation Test Plan Agent (using preview_test_plan_json override).")
                append_log_run(run_id, "⏭️ Skipped Validation Test Plan Agent (using preview_test_plan_json override).")
                continue
            # ------------------------------------------------------
            # 🔍 DEBUG: PRINT LOOP TYPE + FOUND NODE
            # ------------------------------------------------------
            logger.info(f"[DEBUG] loop_type={loop_type} | step={step}")

            # ------------------------------------------------------
            # 🧠 Detect domain of this step
            # ------------------------------------------------------
            node_domain = detect_domain_from_label(step)

            # ------------------------------------------------------
            # 🚫 SKIP nodes that do NOT match the loop (unless system loop)
            # ------------------------------------------------------
            if loop_type != "system" and node_domain != loop_type:
                logger.info(
                   f"[SKIP] Skipping agent '{step}' because its domain={node_domain} "
                   f"but loop_type={loop_type}"
                )
                append_log_workflow(workflow_id,
                   f"⏭️ Skipped {step} (domain={node_domain}, loop={loop_type})"
                )
                append_log_run(run_id,
                   f"⏭️ Skipped {step} (domain={node_domain}, loop={loop_type})"
                )
                continue   
            logger.info(msg)
            append_log_workflow(workflow_id, msg)
            append_log_run(run_id, msg)
            time.sleep(0.2)

            # Queue to external runner at Simulation phase (for any loop)
            if " sim agent" in step.lower():
                log_runtime_event(
                    AgentContext(
                        agent_name=step,
                        state=shared_state,
                        workflow_id=workflow_id,
                        run_id=run_id,
                        loop_type=loop_type,
                        artifact_dir=artifact_dir,
                        user_id=user_id,
                    ),
                    "agent.queue_handoff",
                    status="queued",
                    artifacts_produced={"artifacts_path": artifact_dir},
                    phase="simulation",
                )
                # Mark workflow queued for simulation runner
                supabase.table("workflows").update({
                    "status": "queued",
                    "phase": "simulation",
                    "runner_assigned": None,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", workflow_id).execute()

                append_log_workflow(workflow_id, "🟡 Queued for ChipRunner (Simulation phase).", phase="simulation")
                append_log_run(run_id, "🟡 Queued for ChipRunner (Simulation phase).", status="queued", artifacts_path=artifact_dir)
                time.sleep(0.2)
                return  # external runner will pick up

            # Resolve function
            fn = agent_map_norm.get(step)
            if not fn:
                msg = f"❌ No agent implementation found for: {step}"
                append_log_workflow(workflow_id, msg)
                append_log_run(run_id, msg)
                time.sleep(0.2)
                continue

            try:

                # Execute agent function; start from shared_state snapshot


                result = _execute_agent_with_runtime(
                    step,
                    fn,
                    shared_state,
                    workflow_id,
                    run_id,
                    loop_type,
                )  # agents accept legacy dict state through runtime adapter

                # Save artifacts if provided
                if isinstance(result, dict):
                    # 🧠 Merge new keys (spec_json, spec_file, etc.) back into shared_state
                    shared_state.update(result)

                    # after shared_state.update(result)
                    if loop_type == "validation" and step == "Validation Analytics Agent":
                        from services.validation.validation_memory_service import compute_and_store_validation_memory
                        try:
                           append_log_run(run_id, "🧠 Validation Memory: ingesting facts + LLM interpretations ...")
                           append_log_workflow(workflow_id, "🧠 Validation Memory: ingesting facts + LLM interpretations ...")

                           import asyncio
                           asyncio.run(compute_and_store_validation_memory(shared_state, supabase))

                           append_log_run(run_id, "✅ Validation Memory updated.")
                           append_log_workflow(workflow_id, "✅ Validation Memory updated.")
                        except Exception as e:
                           append_log_run(run_id, f"⚠️ Validation Memory failed: {type(e).__name__}: {e}")
                           append_log_workflow(workflow_id, f"⚠️ Validation Memory failed: {type(e).__name__}: {e}")


                    label_safe = step.replace(" ", "_")
                    out_path = None
                    if result.get("artifact") is not None:
                        os.makedirs(artifact_dir, exist_ok=True)
                        out_path = os.path.join(artifact_dir, f"{label_safe}.txt")
                        with open(out_path, "w") as f:
                            f.write(str(result.get("artifact") or ""))

                    
                    if ENABLE_LEGACY_WORKFLOW_ARTIFACTS_INDEX:
                        # Persist artifacts metadata on workflow row
                        row = supabase.table("workflows").select("artifacts").eq("id", workflow_id).single().execute()
                        artifacts = (row.data or {}).get("artifacts") or {}

                        existing = artifacts.get(step) or {}
                        if not isinstance(existing, dict):
                            existing = {}
                        legacy = {
                            "artifact": (f"/{out_path}" if out_path else None),
                        #    "local_artifact_log": result.get("artifact_log"),
                        #    "log": result.get("log"),
                        #    "code": result.get("code"),
                        }

                        # ✅ Merge legacy fields into existing per-file artifacts (do NOT replace)
                        existing.update({k: v for k, v in legacy.items() if v is not None})
                        artifacts[step] = existing    
                        try:
                            payload = json.dumps(artifacts, ensure_ascii=False, separators=(",", ":"))
                            if len(payload) <= 7000:
                                supabase.table("workflows").update({"artifacts": artifacts}).eq("id", workflow_id).execute()
                            else:
                                logger.warning(f"⚠️ Skipping workflows.artifacts update (payload too long) workflow={workflow_id}")
                        except Exception as e:
                                logger.warning(f"⚠️ Skipping workflows.artifacts update (error) workflow={workflow_id}: {e}")
              

                msg = f"✅ {step} done"
                logger.info(msg)
                append_log_workflow(workflow_id, msg)
                append_log_run(run_id, msg)
                time.sleep(0.2)

            except Exception as agent_err:
                err = f"❌ {step} failed: {agent_err}"
                append_log_workflow(workflow_id, err)
                append_log_run(run_id, err)
                time.sleep(0.2)

        append_log_workflow(workflow_id, "🎉 Workflow complete", status="completed", phase="done")
        append_log_run(run_id, "🎉 Run complete", status="completed")
        time.sleep(0.2)

    except Exception as e:
        err = f"❌ Workflow crashed: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")
        time.sleep(0.2)



class ValidationRunAppIn(BaseModel):
    bench_id: Optional[str] = None
    create_new_bench: Optional[bool] = False
    bench_name: Optional[str] = None
    bench_location: Optional[str] = None
    instrument_ids: Optional[List[str]] = None

    # prefer test_plan_id in UI; allow name for MVP
    test_plan_id: Optional[str] = None
    test_plan_name: Optional[str] = None

    # ✅ NEW: what frontend sends when generating a new test plan
    datasheet_text: Optional[str] = None

    # keep existing field for compatibility
    spec_text: Optional[str] = None

    scope: Optional[Dict[str, Any]] = None
    toggles: Optional[Dict[str, bool]] = None  # {"apply": false, ...}




def execute_validation_run_app_background(
    workflow_id: str,
    run_id: str,
    user_id: str,
    artifact_dir: str,
    payload: Dict[str, Any],
):
    try:
        os.makedirs(artifact_dir, exist_ok=True)

        shared_state = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "artifact_dir": artifact_dir,
            "supabase_client": supabase,
            "user_id": user_id,
        }

        # Inject app payload fields into shared_state (agents expect these)
        for k, v in (payload or {}).items():
            if v is not None:
                shared_state[k] = v

        # ✅ Normalize datasheet/spec for agents
        # Many agents look for state["spec"] or state["datasheet_text"]
        ds = shared_state.get("datasheet_text") or shared_state.get("spec_text")
        if ds:
            shared_state["datasheet_text"] = ds
            shared_state["spec"] = ds

        bench_id = payload.get("bench_id")
        create_new = bool(payload.get("create_new_bench"))
        toggles = payload.get("toggles") or {}
        do_apply = bool(toggles.get("apply", False))

        skipped_create_bench = False

        # --- Fixed WF sequence using YOUR exact names ---
        steps: List[Tuple[str, str, bool]] = [
            ("Generate Lab Handoff", "Validation_Generate_Lab_Handoff", True),
            ("Create Bench", "Validation_Create_Bench", (create_new or not bench_id)),
            ("Preflight Bench", "Validation_Preflight_Bench", True),
            ("Hardware Test Run", "Validation_Hardware_Test_Run", True),
            ("Pattern Detection", "Validation_Pattern_Detection", True),
            ("Coverage Proposal", "Validation_Coverage_Proposal", True),
            ("Evolution Proposal", "Validation_Evolution_Proposal", True),
            ("Apply Proposal", "Validation_Apply_Proposal", do_apply),
        ]

        for phase_label, wf_name, should_run in steps:
            if not should_run:
                append_log_workflow(workflow_id, f"⏭️ Skipped: {phase_label}", phase=phase_label)
                append_log_run(run_id, f"⏭️ Skipped: {phase_label}")
                if wf_name == "Validation_Create_Bench":
                    skipped_create_bench = True
                continue

            # If we skipped Create Bench and user selected existing bench, ensure schematic exists BEFORE preflight
            if phase_label == "Preflight Bench" and bench_id and skipped_create_bench:
                if not _has_bench_schematic(bench_id):
                    append_log_workflow(workflow_id, "🧩 Missing schematic for selected bench → generating schematic now")
                    append_log_run(run_id, "🧩 Missing schematic for selected bench → generating schematic now")
                    # run schematic agent directly
                    shared_state["bench_id"] = bench_id
                    fn = VALIDATION_AGENT_FUNCTIONS.get("Validation Bench Schematic Agent")
                    if fn:
                        out = _execute_agent_with_runtime(
                            "Validation Bench Schematic Agent",
                            fn,
                            shared_state,
                            workflow_id,
                            run_id,
                            "validation",
                        )
                        if isinstance(out, dict):
                            shared_state.update(out)
                    else:
                        append_log_workflow(workflow_id, "⚠️ Schematic agent not found in VALIDATION_AGENT_FUNCTIONS")
                        append_log_run(run_id, "⚠️ Schematic agent not found in VALIDATION_AGENT_FUNCTIONS")

            append_log_workflow(workflow_id, f"▶️ Phase: {phase_label}", phase=phase_label)
            append_log_run(run_id, f"▶️ Phase: {phase_label}")

            defn = _load_workflow_def_by_name(wf_name, user_id=user_id)
            nodes = _definition_to_executor_nodes(defn)

            _run_nodes_with_shared_state(
                workflow_id=workflow_id,
                run_id=run_id,
                loop_type="validation",
                nodes=nodes,
                shared_state=shared_state,
            )

            # If Create Bench created a bench_id, carry it forward
            if phase_label == "Create Bench" and shared_state.get("bench_id"):
                bench_id = shared_state["bench_id"]

            append_log_workflow(workflow_id, f"✅ Phase done: {phase_label}")
            append_log_run(run_id, f"✅ Phase done: {phase_label}")

        append_log_workflow(workflow_id, "🎉 Validation Run App complete", status="completed", phase="done")
        append_log_run(run_id, "🎉 Validation Run App complete", status="completed")

    except Exception as e:
        err = f"❌ Validation Run App crashed: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")


@app.post("/apps/validation/run")
async def apps_validation_run(request: Request, background_tasks: BackgroundTasks, payload: ValidationRunAppIn):
    user_id = _require_user_id(request)

    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    # Parent workflow row (single job)
    supabase.table("workflows").insert({
        "id": workflow_id,
        "user_id": user_id,
        "name": "App: Validation Run",
        "status": "running",
        "phase": "queued",
        "logs": "🚀 App run queued.",
        "created_at": now,
        "updated_at": now,
        "artifacts": {},
        "loop_type": "validation",
        "definitions": {"app_intent": "validation_run", "payload": payload.dict()},
    }).execute()

    # Run row
    user_folder = str(user_id or "anonymous")
    artifact_dir = os.path.join("artifacts", user_folder, workflow_id, run_id)
    os.makedirs(artifact_dir, exist_ok=True)

    supabase.table("runs").insert({
        "id": run_id,
        "user_id": user_id,
        "workflow_id": workflow_id,
        "loop_type": "validation",
        "status": "running",
        "logs": "🚀 App run started.",
        "artifacts_path": artifact_dir,
        "created_at": now
    }).execute()

    append_log_workflow(workflow_id, "🚀 Starting Validation Run App", phase="start")
    append_log_run(run_id, "🚀 Starting Validation Run App")

    background_tasks.add_task(
        execute_validation_run_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


# ======================================================================
#  SYSTEM APPS ( System_End2End, System_PD, System_Firmware, System_Sim)
# ======================================================================



class SystemAppIn(BaseModel):
    project_name: Optional[str] = None
    top_module: Optional[str] = None
    digital_spec_text: str = ""
    analog_spec_text: str = ""
    soc_integration_spec_text: str = ""
    rtl_source_mode: Optional[str] = None
    repo_path: Optional[str] = None
    repo_ref: Optional[str] = None
    repo_subdir: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None
    system_rtl_workflow_id: Optional[str] = None
    from_workflow_id: Optional[str] = None
    test_intent: Optional[str] = None
    verification_plan: Optional[str] = None
    monitor_checker_plan: Optional[str] = None
    random_vs_directed: Optional[str] = None
    coverage_targets: Optional[str] = None
    coverage_plan: Optional[str] = None
    simulator_type: Optional[str] = None
    seed_count: Optional[int] = None
    toolchain: Optional[Any] = None
    run_closure_analysis: Optional[bool] = False
    enable_failure_debug: Optional[bool] = False
    failure_debug_options: Optional[Dict[str, Any]] = None
    foundry: Optional[str] = None
    pdk: Optional[str] = None
    target_frequency_mhz: Optional[float] = None
    constraints_sdc: Optional[str] = None
    clock_constraints: Optional[Any] = None
    effort: Optional[str] = "balanced"
    run_fill: Optional[bool] = True
    run_drc: Optional[bool] = True
    run_lvs: Optional[bool] = True
    run_signoff_closure_loop: Optional[bool] = False
    max_signoff_closure_iterations: Optional[int] = 1
    allow_timing_repair: Optional[bool] = True
    allow_drc_repair: Optional[bool] = True
    allow_lvs_repair: Optional[bool] = True
    allow_lec_repair: Optional[bool] = True
    run_synthesis_closure_loop: Optional[bool] = False
    max_synthesis_closure_iterations: Optional[int] = 1
    allow_synthesis_timing_repair: Optional[bool] = True
    allow_synthesis_lec_repair: Optional[bool] = True
    allow_synthesis_retiming: Optional[bool] = False
    allow_synthesis_hierarchy_flattening: Optional[bool] = False
    stop_on_synthesis_closure_failure: Optional[bool] = False
    stop_on_synthesis_lec_failure: Optional[bool] = False
    analog_physical_mode: Optional[str] = "blackbox"
    analog_pdk: Optional[str] = None
    analog_spice_text: Optional[str] = None
    analog_netlist_text: Optional[str] = None
    analog_gds_path: Optional[str] = None
    system_sim_testcases: Optional[List[str]] = None
    system_sim_seeds: Optional[List[int]] = None
    system_sim_num_iters: Optional[int] = None
    toggles: Optional[Dict[str, Any]] = None


class SystemArchitectureAppIn(BaseModel):
    project_name: Optional[str] = None
    exploration_type: Optional[str] = "cache_tuning"
    workload: Optional[str] = None
    workload_name: Optional[str] = None
    simulator: Optional[str] = "gem5"
    simulation_tool: Optional[str] = None
    isa: Optional[str] = "x86"
    isas: Optional[List[str]] = None
    cpu_model: Optional[str] = "TimingSimpleCPU"
    cpu_models: Optional[List[str]] = None
    cores: Optional[int] = 1
    clock: Optional[str] = "2GHz"
    mode: Optional[str] = "syscall_emulation"
    memory_type: Optional[str] = "DDR3_1600_8x8"
    memory_size: Optional[str] = "2GB"
    l1_assoc: Optional[int] = 2
    l2_assoc: Optional[int] = 8
    cache_line_size: Optional[int] = 64
    prefetcher: Optional[str] = "none"
    branch_predictor: Optional[str] = "default"
    maxinsts: Optional[int] = None
    workload_binary: Optional[str] = None
    goal: Optional[str] = None
    experiment_goal: Optional[str] = None
    sweep: Optional[Dict[str, Any]] = None
    memory_types: Optional[List[str]] = None
    toggles: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None


class SystemArchitectureToRTLHandoffIn(BaseModel):
    selected_run_id: Optional[str] = None
    reviewer: Optional[str] = None
    top_module: Optional[str] = None


class SystemSoftwareAppIn(BaseModel):
    project_name: Optional[str] = None

    # primary upstream handoff
    system_software_handoff_path: Optional[str] = None
    system_firmware_workflow_id: Optional[str] = None

    # optional future reuse hooks (just pass through for now)
    system_rtl_workflow_id: Optional[str] = None

    # software-side intent
    software_goal: Optional[str] = None
    app_names: Optional[List[str]] = None
    target_language: Optional[str] = None   # "c" | "rust" | "mixed"
    sdk_style: Optional[str] = None         # "c_sdk" | "rust_crate" | "mixed"
    build_system: Optional[str] = None      # "cmake" | "cargo" | "make"
    notes: Optional[str] = None

    toggles: Optional[Dict[str, Any]] = None


class SystemSoftwareValidationAppIn(BaseModel):
    system_software_workflow_id: str
    validation_mode: Literal[
        "software_package_validation",
        "full_co_simulation"
    ] = "software_package_validation"

    system_firmware_workflow_id: Optional[str] = None
    system_rtl_workflow_id: Optional[str] = None

    goal: Optional[str] = None
    notes: Optional[str] = None


class SystemProductBuilderAppIn(BaseModel):
    arch2rtl_workflow_id: Optional[str] = None
    verify_workflow_id: Optional[str] = None
    system_firmware_workflow_id: Optional[str] = None
    system_software_workflow_id: Optional[str] = None
    system_validation_workflow_id: Optional[str] = None
    product_intent: Optional[str] = None
    app_type: Optional[str] = "web_dashboard"
    target_runtime: Optional[str] = "simulated_device"
    notes: Optional[str] = None


class ProductCreateIn(BaseModel):
    name: str
    product_type: str = "digital"
    starting_point: str = "from_specs"
    description: Optional[str] = ""
    reference_slug: Optional[str] = None
    stage_config: Optional[Dict[str, Any]] = None


class ProductUpdateIn(BaseModel):
    name: Optional[str] = None
    product_type: Optional[str] = None
    starting_point: Optional[str] = None
    description: Optional[str] = None
    stage_config: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class ProductRunStartIn(BaseModel):
    start_stage: Optional[str] = None
    max_stages: Optional[int] = 8
    resume_product_run_id: Optional[str] = None


PRODUCT_REFERENCE_JOURNEYS: List[Dict[str, Any]] = [
    {
        "slug": "pwm-fan-controller",
        "name": "PWM Fan Controller",
        "product_type": "digital",
        "summary": "Full development from PWM RTL through verification, firmware, software validation, and simulator-backed product app.",
        "definition": {
            "stages": [
                {"id": "arch2rtl", "label": "RTL", "app": "Digital_Arch2RTL", "required": True},
                {"id": "dqa", "label": "DQA", "app": "Digital_DQA", "required": True},
                {"id": "verify", "label": "Verification", "app": "Digital_Verify", "required": True},
                {"id": "closure", "label": "Closure", "app": "verify_closure_loop", "recommended": True},
                {"id": "firmware", "label": "Firmware", "app": "Embedded_Run", "recommended": True},
                {"id": "software", "label": "Software", "app": "System_Software", "recommended": True},
                {"id": "validation", "label": "Validation", "app": "System_Software_Validation_L2", "recommended": True},
                {"id": "product_app", "label": "Product App", "app": "System_Product_App_Builder", "recommended": True},
            ]
        },
    },
    {
        "slug": "uart-packet-engine",
        "name": "UART Packet Engine",
        "product_type": "digital",
        "summary": "UART/FIFO/interrupt journey from Arch2RTL to verification, firmware, software validation, and product app.",
        "definition": {
            "stages": [
                {"id": "arch2rtl", "label": "RTL", "app": "Digital_Arch2RTL", "required": True},
                {"id": "dqa", "label": "DQA", "app": "Digital_DQA", "required": True},
                {"id": "verify", "label": "Verification", "app": "Digital_Verify", "required": True},
                {"id": "closure", "label": "Closure", "app": "verify_closure_loop", "recommended": True},
                {"id": "firmware", "label": "Firmware", "app": "Embedded_Run", "recommended": True},
                {"id": "software", "label": "Software", "app": "System_Software", "recommended": True},
                {"id": "product_app", "label": "Product App", "app": "System_Product_App_Builder", "recommended": True},
            ]
        },
    },
    {
        "slug": "image-dma-pipeline",
        "name": "Image DMA Pipeline",
        "product_type": "digital",
        "summary": "Large image-processing journey with DMA, line buffers, verification, firmware/software validation, and product dashboard.",
        "definition": {
            "stages": [
                {"id": "arch2rtl", "label": "RTL", "app": "Digital_Arch2RTL", "required": True},
                {"id": "dqa", "label": "DQA", "app": "Digital_DQA", "required": True},
                {"id": "verify", "label": "Verification", "app": "Digital_Verify", "required": True},
                {"id": "synthesis", "label": "Synthesis", "app": "Digital_Arch2Synthesis", "recommended": True},
                {"id": "firmware", "label": "Firmware", "app": "Embedded_Run", "recommended": True},
                {"id": "validation", "label": "Validation", "app": "System_Software_Validation_L2", "recommended": True},
                {"id": "product_app", "label": "Product App", "app": "System_Product_App_Builder", "recommended": True},
            ]
        },
    },
    {
        "slug": "soft-digital-ip-product",
        "name": "Soft Digital IP Product",
        "product_type": "digital",
        "summary": "Reusable digital IP journey from specs to RTL, DQA, verification, coverage closure, synthesis readiness, and handoff collateral.",
        "definition": {
            "stages": [
                {"id": "arch2rtl", "label": "RTL", "app": "Digital_Arch2RTL", "required": True},
                {"id": "dqa", "label": "DQA", "app": "Digital_DQA", "required": True},
                {"id": "synthesis", "label": "Synthesis", "app": "Digital_Arch2Synthesis", "recommended": True},
                {"id": "verify", "label": "Verification", "app": "Digital_Verify", "recommended": True},
                {"id": "firmware", "label": "Firmware", "app": "Embedded_Run", "optional": True},
                {"id": "software", "label": "Software", "app": "System_Software", "optional": True},
                {"id": "product_app", "label": "Product App", "app": "System_Product_App_Builder", "optional": True},
            ]
        },
    },
    {
        "slug": "temperature-monitor-soc",
        "name": "Temperature Monitor SoC",
        "product_type": "mixed_signal",
        "summary": "Mixed-signal System RTL journey with digital, analog, SoC specs, System Sim, firmware, software, validation, and product app.",
        "definition": {
            "stages": [
                {"id": "system_rtl", "label": "System RTL", "app": "System_RTL", "required": True},
                {"id": "system_dqa", "label": "System DQA", "app": "System_DQA", "required": True},
                {"id": "system_sim", "label": "System Sim", "app": "System_Sim", "required": True},
                {"id": "system_firmware", "label": "Firmware", "app": "System_Firmware", "recommended": True},
                {"id": "system_software", "label": "Software", "app": "System_Software", "recommended": True},
                {"id": "system_validation", "label": "Validation", "app": "System_Software_Validation_L2", "recommended": True},
                {"id": "system_pd", "label": "System PD", "app": "System_PD", "optional": True},
                {"id": "product_app", "label": "Product App", "app": "System_Product_App_Builder", "recommended": True},
            ]
        },
    },
]

# ==========================================================
# ✅ DIGITAL APPS (Arch2RTL / DQA / Verify) — same pattern as Validation Run App
# ==========================================================

PRODUCT_STAGE_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "Digital_Arch2RTL": {
        "note": "Spec text can be left blank only when the product description is detailed enough to use as the RTL spec.",
        "fields": [
            {"key": "project_name", "label": "Project name", "type": "text", "defaultValue": ""},
            {"key": "top_module", "label": "Top module", "type": "text", "defaultValue": ""},
            {"key": "design_language", "label": "Design language", "type": "text", "defaultValue": "systemverilog"},
            {"key": "spec_text", "label": "Spec text", "type": "text", "defaultValue": "", "helper": "Used before product description fallback."},
            {"key": "enable_regmap", "label": "Generate register map", "type": "boolean", "defaultValue": True},
            {"key": "enable_upf_lite", "label": "Generate UPF-lite", "type": "boolean", "defaultValue": False},
            {"key": "enable_packaging", "label": "Generate handoff package", "type": "boolean", "defaultValue": True},
            {"key": "enable_scan_dft", "label": "Enable scan/DFT intent", "type": "boolean", "defaultValue": False},
            {"key": "insert_mbist", "label": "Insert MBIST", "type": "boolean", "defaultValue": False},
            {"key": "mbist_algorithm", "label": "MBIST algorithm", "type": "select", "defaultValue": "march-c", "options": ["march-c", "march-raw"]},
            {"key": "run_spec2rtl_check", "label": "Run Spec2RTL compliance check", "type": "boolean", "defaultValue": False},
            {"key": "throughput_latency_targets", "label": "Throughput/latency targets", "type": "text", "defaultValue": ""},
            {"key": "power_priority", "label": "Power priority", "type": "text", "defaultValue": ""},
        ],
    },
    "Digital_DQA": {
        "note": "DQA uses the Arch2RTL handoff and does not regenerate RTL.",
        "fields": [
            {"key": "run_lint", "label": "Run lint", "type": "boolean", "defaultValue": True},
            {"key": "run_cdc", "label": "Run CDC", "type": "boolean", "defaultValue": True},
            {"key": "run_reset", "label": "Run reset integrity", "type": "boolean", "defaultValue": True},
            {"key": "run_synthesis_readiness", "label": "Run synthesis readiness", "type": "boolean", "defaultValue": True},
            {"key": "lint_profile", "label": "Lint profile", "type": "text", "defaultValue": "default"},
            {"key": "cdc_profile", "label": "CDC profile", "type": "text", "defaultValue": "default"},
            {"key": "enable_autofix", "label": "Enable autofix", "type": "boolean", "defaultValue": False},
        ],
    },
    "Digital_Verify": {
        "fields": [
            {"key": "test_intent", "label": "Test intent", "type": "text", "defaultValue": "Run smoke, reset, register access, and representative functional tests."},
            {"key": "verification_plan", "label": "Verification plan", "type": "text", "defaultValue": ""},
            {"key": "monitor_checker_plan", "label": "Monitor/checker plan", "type": "text", "defaultValue": ""},
            {"key": "random_vs_directed", "label": "Random vs directed", "type": "select", "defaultValue": "both", "options": [
                {"value": "both", "label": "Directed then random"},
                {"value": "directed", "label": "Directed only"},
                {"value": "random", "label": "Random only"},
            ]},
            {"key": "coverage_targets", "label": "Coverage target", "type": "text", "defaultValue": "90% functional, 70% line"},
            {"key": "coverage_plan", "label": "Coverage plan", "type": "text", "defaultValue": ""},
            {"key": "simulator_type", "label": "Simulator", "type": "select", "defaultValue": "verilator", "options": [
                {"value": "verilator", "label": "Verilator + Cocotb"},
                {"value": "icarus", "label": "Icarus Verilog (planned)", "disabled": True},
                {"value": "questa", "label": "Questa (planned)", "disabled": True},
                {"value": "vcs", "label": "VCS (planned)", "disabled": True},
                {"value": "xcelium", "label": "Xcelium (planned)", "disabled": True},
            ]},
            {"key": "code_coverage_tool", "label": "Code coverage tool", "type": "select", "defaultValue": "verilator_coverage", "options": [
                {"value": "verilator_coverage", "label": "verilator_coverage"},
                {"value": "none", "label": "Disabled"},
                {"value": "urg", "label": "Synopsys URG (planned)", "disabled": True},
                {"value": "imc", "label": "Cadence IMC (planned)", "disabled": True},
                {"value": "vcover", "label": "Questa vcover (planned)", "disabled": True},
            ]},
            {"key": "formal_tool", "label": "Formal tool", "type": "select", "defaultValue": "none", "options": [
                {"value": "none", "label": "Disabled"},
                {"value": "symbiyosys", "label": "SymbiYosys (sby)"},
                {"value": "jasper", "label": "JasperGold (planned)", "disabled": True},
                {"value": "vc_formal", "label": "VC Formal (planned)", "disabled": True},
            ]},
            {"key": "formal_solver", "label": "Formal solver", "type": "select", "defaultValue": "z3", "options": [
                {"value": "z3", "label": "Z3"},
                {"value": "boolector", "label": "Boolector"},
            ]},
            {"key": "golden_model_tool", "label": "Golden model tool", "type": "select", "defaultValue": "none", "options": [
                {"value": "none", "label": "Disabled"},
                {"value": "chiploop_python_scoreboard", "label": "ChipLoop Python scoreboard"},
                {"value": "custom_python", "label": "Custom Python model (planned)", "disabled": True},
                {"value": "matlab", "label": "MATLAB reference model (planned)", "disabled": True},
            ]},
            {"key": "seed_count", "label": "Seed count", "type": "number", "defaultValue": 10},
            {"key": "run_closure_analysis", "label": "Run closure analysis", "type": "boolean", "defaultValue": True},
            {"key": "enable_failure_debug", "label": "Run failure debug", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_log_only_first", "label": "Failure debug log-only first", "type": "boolean", "defaultValue": True},
            {"key": "failure_debug_generate_vcd", "label": "Generate VCD for failures", "type": "boolean", "defaultValue": True},
            {"key": "failure_debug_auto_apply_tb", "label": "Auto-apply TB fixes", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_auto_apply_rtl", "label": "Auto-apply RTL fixes", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_rerun_failing", "label": "Rerun failing tests", "type": "boolean", "defaultValue": True},
        ],
    },
    "Digital_Arch2Synthesis": {
        "note": "Synthesis uses the generated Arch2RTL handoff as RTL input and runs the synthesis stage directly.",
        "fields": [
            {"key": "foundry", "label": "Foundry", "type": "text", "defaultValue": "sky130"},
            {"key": "pdk", "label": "PDK", "type": "text", "defaultValue": "sky130A"},
            {"key": "toolchain", "label": "Toolchain", "type": "text", "defaultValue": "openlane2"},
            {"key": "target_frequency_mhz", "label": "Target frequency MHz", "type": "number", "defaultValue": 100},
            {"key": "constraints_sdc", "label": "Constraints SDC", "type": "text", "defaultValue": ""},
            {"key": "run_logic_equivalence", "label": "Run logic equivalence", "type": "boolean", "defaultValue": True},
            {"key": "run_synthesis_readiness", "label": "Run synthesis readiness", "type": "boolean", "defaultValue": True},
            {"key": "run_synthesis_closure_loop", "label": "Run synthesis closure loop", "type": "boolean", "defaultValue": False},
            {"key": "max_synthesis_closure_iterations", "label": "Max synthesis closure iterations", "type": "number", "defaultValue": 1},
            {"key": "allow_synthesis_timing_repair", "label": "Allow synthesis setup timing repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_lec_repair", "label": "Allow synthesis LEC repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_retiming", "label": "Allow synthesis retiming", "type": "boolean", "defaultValue": False},
            {"key": "allow_synthesis_hierarchy_flattening", "label": "Allow synthesis hierarchy flattening", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_closure_failure", "label": "Stop downstream on synthesis closure failure", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_lec_failure", "label": "Stop downstream on synthesis LEC failure", "type": "boolean", "defaultValue": False},
        ],
    },
    "Digital_Arch2Tapeout": {
        "note": "Tapeout uses the generated Arch2RTL handoff as RTL input and runs synthesis through physical signoff.",
        "fields": [
            {"key": "foundry", "label": "Foundry", "type": "text", "defaultValue": "sky130"},
            {"key": "pdk", "label": "PDK", "type": "text", "defaultValue": "sky130A"},
            {"key": "toolchain", "label": "Toolchain", "type": "text", "defaultValue": "openlane2"},
            {"key": "target_frequency_mhz", "label": "Target frequency MHz", "type": "number", "defaultValue": 100},
            {"key": "constraints_sdc", "label": "Constraints SDC", "type": "text", "defaultValue": ""},
            {"key": "effort", "label": "Implementation effort", "type": "select", "defaultValue": "balanced", "options": ["fast", "balanced", "signoff"]},
            {"key": "run_fill", "label": "Run fill", "type": "boolean", "defaultValue": True},
            {"key": "run_drc", "label": "Run DRC", "type": "boolean", "defaultValue": True},
            {"key": "run_lvs", "label": "Run LVS", "type": "boolean", "defaultValue": True},
            {"key": "run_logic_equivalence", "label": "Run logic equivalence", "type": "boolean", "defaultValue": True},
            {"key": "run_signoff_closure_loop", "label": "Run signoff closure loop", "type": "boolean", "defaultValue": False},
            {"key": "max_signoff_closure_iterations", "label": "Max signoff closure iterations", "type": "number", "defaultValue": 1},
            {"key": "allow_timing_repair", "label": "Allow timing repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_drc_repair", "label": "Allow DRC repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_lvs_repair", "label": "Allow LVS repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_lec_repair", "label": "Allow LEC repair", "type": "boolean", "defaultValue": True},
            {"key": "run_synthesis_closure_loop", "label": "Run synthesis closure loop", "type": "boolean", "defaultValue": False},
            {"key": "max_synthesis_closure_iterations", "label": "Max synthesis closure iterations", "type": "number", "defaultValue": 1},
            {"key": "allow_synthesis_timing_repair", "label": "Allow synthesis setup timing repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_lec_repair", "label": "Allow synthesis LEC repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_retiming", "label": "Allow synthesis retiming", "type": "boolean", "defaultValue": False},
            {"key": "allow_synthesis_hierarchy_flattening", "label": "Allow synthesis hierarchy flattening", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_closure_failure", "label": "Stop downstream on synthesis closure failure", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_lec_failure", "label": "Stop downstream on synthesis LEC failure", "type": "boolean", "defaultValue": False},
        ],
    },
    "verify_closure_loop": {
        "fields": [
            {"key": "max_iterations", "label": "Max iterations", "type": "number", "defaultValue": 1},
            {"key": "seed_count", "label": "Seed count", "type": "number", "defaultValue": 10},
            {"key": "seed_budget", "label": "Seed budget", "type": "number", "defaultValue": 10},
            {"key": "coverage_targets", "label": "Coverage target", "type": "text", "defaultValue": "90% functional, 70% line"},
            {"key": "rerun_mode", "label": "Rerun mode", "type": "select", "defaultValue": "coverage_targeted", "options": [
                {"value": "coverage_targeted", "label": "Coverage targeted"},
                {"value": "failed_only", "label": "Failed tests only"},
                {"value": "full_regression", "label": "Full regression"},
            ]},
            {"key": "random_vs_directed", "label": "Random vs directed", "type": "select", "defaultValue": "both", "options": [
                {"value": "both", "label": "Directed then random"},
                {"value": "directed", "label": "Directed only"},
                {"value": "random", "label": "Random only"},
            ]},
            {"key": "enable_failure_debug", "label": "Run failure debug", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_log_only_first", "label": "Failure debug log-only first", "type": "boolean", "defaultValue": True},
            {"key": "failure_debug_generate_vcd", "label": "Generate VCD for failures", "type": "boolean", "defaultValue": True},
            {"key": "failure_debug_auto_apply_tb", "label": "Auto-apply TB fixes", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_auto_apply_rtl", "label": "Auto-apply RTL fixes", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_rerun_failing", "label": "Rerun failing tests", "type": "boolean", "defaultValue": True},
        ],
    },
    "Embedded_Run": {
        "fields": [
            {"key": "firmware_language", "label": "Firmware language", "type": "select", "defaultValue": "rust", "options": [
                {"value": "rust", "label": "Rust"},
                {"value": "c", "label": "C"},
            ]},
            {"key": "enable_cosim", "label": "Enable firmware co-sim", "type": "boolean", "defaultValue": False},
        ],
    },
    "System_RTL": {
        "note": "System RTL starts from explicit digital, analog, and SoC specs. Downstream System apps auto-bind to this generated workflow.",
        "fields": [
            {"key": "digital_spec", "label": "Digital spec", "type": "text", "defaultValue": "", "required": True},
            {"key": "analog_spec", "label": "Analog spec", "type": "text", "defaultValue": "", "required": True},
            {"key": "soc_spec", "label": "SoC spec", "type": "text", "defaultValue": "", "required": True},
            {"key": "enable_spec2rtl", "label": "Spec2RTL check", "type": "boolean", "defaultValue": True},
        ],
    },
    "System_DQA": {
        "note": "System DQA uses the System RTL handoff and does not rerun register-map generation.",
        "fields": [
            {"key": "run_lint", "label": "Run lint", "type": "boolean", "defaultValue": True},
            {"key": "run_cdc", "label": "Run CDC", "type": "boolean", "defaultValue": True},
            {"key": "run_reset", "label": "Run reset integrity", "type": "boolean", "defaultValue": True},
            {"key": "run_synthesis_readiness", "label": "Run synthesis readiness", "type": "boolean", "defaultValue": True},
        ],
    },
    "System_Sim": {
        "fields": [
            {"key": "test_intent", "label": "Test intent", "type": "text", "defaultValue": "Run integrated system smoke and register-path scenarios."},
            {"key": "verification_plan", "label": "Verification plan", "type": "text", "defaultValue": ""},
            {"key": "monitor_checker_plan", "label": "Monitor/checker plan", "type": "text", "defaultValue": ""},
            {"key": "seed_count", "label": "Seed count", "type": "number", "defaultValue": 6},
            {"key": "system_sim_testcases", "label": "Testcases", "type": "text", "defaultValue": "system_smoke_test, integrated_input_sanity"},
            {"key": "system_sim_seeds", "label": "Seeds", "type": "text", "defaultValue": "1,2,3,4"},
            {"key": "coverage_targets", "label": "Coverage target", "type": "text", "defaultValue": "90% functional"},
            {"key": "coverage_plan", "label": "Coverage point plan", "type": "text", "defaultValue": ""},
            {"key": "system_sim_num_iters", "label": "Iteration budget", "type": "number", "defaultValue": 25},
            {"key": "simulator_type", "label": "Simulator", "type": "select", "defaultValue": "verilator", "options": [
                {"value": "verilator", "label": "verilator"},
                {"value": "icarus", "label": "icarus"},
            ]},
            {"key": "code_coverage_tool", "label": "Code coverage", "type": "select", "defaultValue": "verilator_coverage", "options": [
                {"value": "verilator_coverage", "label": "verilator_coverage"},
                {"value": "none", "label": "Disabled"},
            ]},
            {"key": "formal_tool", "label": "Formal tool", "type": "select", "defaultValue": "none", "options": [
                {"value": "none", "label": "Disabled"},
                {"value": "symbiyosys", "label": "SymbiYosys (sby)"},
            ]},
            {"key": "formal_solver", "label": "Formal solver", "type": "select", "defaultValue": "z3", "options": [
                {"value": "z3", "label": "Z3"},
                {"value": "boolector", "label": "Boolector"},
            ]},
            {"key": "golden_model_tool", "label": "Golden model comparison", "type": "select", "defaultValue": "none", "options": [
                {"value": "none", "label": "Disabled"},
                {"value": "chiploop_python_scoreboard", "label": "ChipLoop Python scoreboard"},
            ]},
            {"key": "random_vs_directed", "label": "Random vs directed", "type": "select", "defaultValue": "both", "options": [
                {"value": "both", "label": "Directed then random"},
                {"value": "directed", "label": "Directed only"},
                {"value": "random", "label": "Random only"},
            ]},
            {"key": "run_closure_analysis", "label": "Run closure analysis", "type": "boolean", "defaultValue": False},
            {"key": "enable_failure_debug", "label": "Debug failing tests", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_log_only_first", "label": "Failure debug log-only first", "type": "boolean", "defaultValue": True},
            {"key": "failure_debug_generate_vcd", "label": "Generate VCD for failures", "type": "boolean", "defaultValue": True},
            {"key": "failure_debug_auto_apply_tb", "label": "Auto-apply TB fixes", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_auto_apply_rtl", "label": "Auto-apply RTL fixes", "type": "boolean", "defaultValue": False},
            {"key": "failure_debug_rerun_failing", "label": "Rerun failing tests", "type": "boolean", "defaultValue": True},
        ],
    },
    "System_Firmware": {
        "note": "Firmware auto-binds the System RTL workflow ID, including register-map and top-level handoff artifacts.",
        "fields": [
            {"key": "firmware_language", "label": "Firmware language", "type": "select", "defaultValue": "rust", "options": [
                {"value": "rust", "label": "Rust"},
                {"value": "c", "label": "C"},
            ]},
            {"key": "validate_registers", "label": "Validate registers", "type": "boolean", "defaultValue": True},
            {"key": "enable_cosim", "label": "Enable firmware co-sim", "type": "boolean", "defaultValue": True},
        ],
    },
    "System_Synthesis": {
        "fields": [
            {"key": "foundry", "label": "Foundry", "type": "text", "defaultValue": "sky130"},
            {"key": "pdk", "label": "PDK", "type": "text", "defaultValue": "sky130A"},
            {"key": "toolchain", "label": "Toolchain", "type": "text", "defaultValue": "openlane2"},
            {"key": "target_frequency_mhz", "label": "Target frequency MHz", "type": "number", "defaultValue": 100},
            {"key": "constraints_sdc", "label": "Constraints SDC", "type": "text", "defaultValue": ""},
            {"key": "run_spec2rtl_check", "label": "Run Spec2RTL compliance check", "type": "boolean", "defaultValue": False},
            {"key": "enable_scan_dft", "label": "Enable scan/DFT intent", "type": "boolean", "defaultValue": False},
            {"key": "run_synthesis_closure_loop", "label": "Run synthesis closure loop", "type": "boolean", "defaultValue": False},
            {"key": "max_synthesis_closure_iterations", "label": "Max synthesis closure iterations", "type": "number", "defaultValue": 1},
            {"key": "allow_synthesis_timing_repair", "label": "Allow synthesis setup timing repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_lec_repair", "label": "Allow synthesis LEC repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_retiming", "label": "Allow synthesis retiming", "type": "boolean", "defaultValue": False},
            {"key": "allow_synthesis_hierarchy_flattening", "label": "Allow synthesis hierarchy flattening", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_closure_failure", "label": "Stop downstream on synthesis closure failure", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_lec_failure", "label": "Stop downstream on synthesis LEC failure", "type": "boolean", "defaultValue": False},
        ],
    },
    "System_PD": {
        "fields": [
            {"key": "foundry", "label": "Foundry", "type": "text", "defaultValue": "sky130"},
            {"key": "pdk", "label": "PDK", "type": "text", "defaultValue": "sky130A"},
            {"key": "toolchain", "label": "Toolchain", "type": "text", "defaultValue": "openlane2"},
            {"key": "target_frequency_mhz", "label": "Target frequency MHz", "type": "number", "defaultValue": 100},
            {"key": "constraints_sdc", "label": "Constraints SDC", "type": "text", "defaultValue": ""},
            {"key": "effort", "label": "Implementation effort", "type": "select", "defaultValue": "balanced", "options": ["fast", "balanced", "signoff"]},
            {"key": "run_spec2rtl_check", "label": "Run Spec2RTL compliance check", "type": "boolean", "defaultValue": False},
            {"key": "enable_scan_dft", "label": "Enable scan/DFT intent", "type": "boolean", "defaultValue": False},
            {"key": "analog_physical_mode", "label": "Analog physical mode", "type": "select", "defaultValue": "blackbox", "options": [
                {"value": "blackbox", "label": "Blackbox analog macro"},
                {"value": "generate_sky130_gds", "label": "Generate Sky130 GDS"},
                {"value": "provided_gds", "label": "Provided GDS/SPICE"},
            ]},
            {"key": "analog_pdk", "label": "Analog PDK", "type": "text", "defaultValue": "sky130A"},
            {"key": "analog_spice_text", "label": "Provided analog SPICE/netlist", "type": "text", "defaultValue": ""},
            {"key": "regenerate_lef_lib_after_gds", "label": "Regenerate LEF/LIB after GDS", "type": "boolean", "defaultValue": True},
            {"key": "run_lef_lib_consistency", "label": "Run LEF/LIB consistency", "type": "boolean", "defaultValue": True},
            {"key": "run_logic_equivalence", "label": "Run logic equivalence", "type": "boolean", "defaultValue": True},
            {"key": "run_fill", "label": "Run fill", "type": "boolean", "defaultValue": True},
            {"key": "run_drc", "label": "Run DRC", "type": "boolean", "defaultValue": True},
            {"key": "run_lvs", "label": "Run LVS", "type": "boolean", "defaultValue": True},
            {"key": "run_signoff_closure_loop", "label": "Run signoff closure loop", "type": "boolean", "defaultValue": False},
            {"key": "max_signoff_closure_iterations", "label": "Max signoff closure iterations", "type": "number", "defaultValue": 1},
            {"key": "allow_timing_repair", "label": "Allow timing repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_drc_repair", "label": "Allow DRC repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_lvs_repair", "label": "Allow LVS repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_lec_repair", "label": "Allow LEC repair", "type": "boolean", "defaultValue": True},
            {"key": "run_synthesis_closure_loop", "label": "Run synthesis closure loop", "type": "boolean", "defaultValue": False},
            {"key": "max_synthesis_closure_iterations", "label": "Max synthesis closure iterations", "type": "number", "defaultValue": 1},
            {"key": "allow_synthesis_timing_repair", "label": "Allow synthesis setup timing repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_lec_repair", "label": "Allow synthesis LEC repair", "type": "boolean", "defaultValue": True},
            {"key": "allow_synthesis_retiming", "label": "Allow synthesis retiming", "type": "boolean", "defaultValue": False},
            {"key": "allow_synthesis_hierarchy_flattening", "label": "Allow synthesis hierarchy flattening", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_closure_failure", "label": "Stop downstream on synthesis closure failure", "type": "boolean", "defaultValue": False},
            {"key": "stop_on_synthesis_lec_failure", "label": "Stop downstream on synthesis LEC failure", "type": "boolean", "defaultValue": False},
        ],
    },
    "System_Software": {
        "fields": [
            {"key": "app_names", "label": "App names", "type": "text", "defaultValue": "status_cli, product_service"},
            {"key": "target_language", "label": "Target language", "type": "select", "defaultValue": "rust", "options": [
                {"value": "rust", "label": "Rust"},
                {"value": "c", "label": "C"},
                {"value": "mixed", "label": "Mixed C/Rust"},
            ]},
            {"key": "sdk_style", "label": "SDK style", "type": "select", "defaultValue": "rust_crate", "options": [
                {"value": "rust_crate", "label": "Rust crate"},
                {"value": "c_sdk", "label": "C SDK"},
                {"value": "mixed", "label": "Mixed SDK"},
            ]},
            {"key": "build_system", "label": "Build system", "type": "select", "defaultValue": "cargo", "options": [
                {"value": "cargo", "label": "Cargo"},
                {"value": "cmake", "label": "CMake"},
                {"value": "make", "label": "Make"},
            ]},
        ],
    },
    "System_Software_Validation_L2": {
        "fields": [
            {"key": "validation_mode", "label": "Validation mode", "type": "select", "defaultValue": "full_co_simulation", "options": [
                {"value": "full_co_simulation", "label": "Full co-simulation"},
                {"value": "software_package_validation", "label": "Software package validation"},
            ]},
        ],
    },
    "System_Product_App_Builder": {
        "fields": [
            {"key": "app_type", "label": "App type", "type": "select", "defaultValue": "web_dashboard", "options": [
                {"value": "web_dashboard", "label": "Web dashboard"},
                {"value": "cli_tool", "label": "CLI tool (planned)", "disabled": True},
            ]},
            {"key": "target_runtime", "label": "Target runtime", "type": "select", "defaultValue": "simulated_device", "options": [
                {"value": "simulated_device", "label": "Simulated device"},
                {"value": "board_transport", "label": "Board transport (planned)", "disabled": True},
            ]},
        ],
    },
}


class DigitalArch2RTLAppIn(BaseModel):
    project_name: Optional[str] = None
    top_module: Optional[str] = None
    design_language: Optional[str] = None  # "SV/Verilog" etc

    spec_text: Optional[str] = None
    interfaces: Optional[Any] = None  # keep flexible for now

    throughput_latency_targets: Optional[str] = None
    clocks: Optional[Any] = None
    resets: Optional[Any] = None
    power_priority: Optional[str] = None

    toggles: Optional[Dict[str, Any]] = None  # Allows booleans plus string options such as mbist_algorithm.


class DigitalRTLSourceIn(BaseModel):
    """
    Used by apps that can START from existing RTL (skip Arch2RTL).
    """
    rtl_source_mode: Optional[str] = None  # "from_arch2rtl" | "paste" | "repo_path"
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    repo_ref: Optional[str] = None
    repo_subdir: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None  # [{path, content}]
    source_arch2rtl_workflow_id: Optional[str] = None
    parent_workflow_id: Optional[str] = None
    upstream_workflows: Optional[Dict[str, Any]] = None


class DigitalArch2SynthesisAppIn(DigitalArch2RTLAppIn, DigitalRTLSourceIn):
    """
    Arch2Synthesis = Arch2RTL + optional RTL source + synthesis knobs + stage control
    """
    # synthesis knobs
    foundry: Optional[str] = None          # e.g., "sky130"
    pdk: Optional[str] = None              # e.g., "sky130A"
    toolchain: Optional[str] = None        # e.g., "openlane2"
    target_frequency_mhz: Optional[float] = None
    constraints_sdc: Optional[str] = None
    clock_constraints: Optional[Any] = None
    run_synthesis_closure_loop: Optional[bool] = False
    max_synthesis_closure_iterations: Optional[int] = 1
    allow_synthesis_timing_repair: Optional[bool] = True
    allow_synthesis_lec_repair: Optional[bool] = True
    allow_synthesis_retiming: Optional[bool] = False
    allow_synthesis_hierarchy_flattening: Optional[bool] = False
    stop_on_synthesis_closure_failure: Optional[bool] = False
    stop_on_synthesis_lec_failure: Optional[bool] = False

    # stage control
    start_stage: Optional[str] = "arch2rtl"   # "arch2rtl" | "synth"
    stop_stage: Optional[str] = "synth"


class DigitalArch2TapeoutAppIn(DigitalArch2RTLAppIn, DigitalRTLSourceIn):
    """
    Arch2Tapeout = Arch2RTL + optional RTL source + synthesis + impl knobs + stage control
    Supports:
      - arch2rtl -> tapeout
      - synth -> tapeout (RTL provided)
      - floorplan -> tapeout (future: if you later add netlist/source hooks)
    """
    # foundry/tool knobs
    foundry: Optional[str] = None
    pdk: Optional[str] = None
    toolchain: Optional[str] = None
    target_frequency_mhz: Optional[float] = None
    constraints_sdc: Optional[str] = None
    clock_constraints: Optional[Any] = None

    # implementation knobs
    effort: Optional[str] = "balanced"     # "fast" | "balanced" | "signoff"
    run_fill: Optional[bool] = True
    run_drc: Optional[bool] = True
    run_lvs: Optional[bool] = True
    run_signoff_closure_loop: Optional[bool] = False
    max_signoff_closure_iterations: Optional[int] = 1
    allow_timing_repair: Optional[bool] = True
    allow_drc_repair: Optional[bool] = True
    allow_lvs_repair: Optional[bool] = True
    allow_lec_repair: Optional[bool] = True
    run_synthesis_closure_loop: Optional[bool] = False
    max_synthesis_closure_iterations: Optional[int] = 1
    allow_synthesis_timing_repair: Optional[bool] = True
    allow_synthesis_lec_repair: Optional[bool] = True
    allow_synthesis_retiming: Optional[bool] = False
    allow_synthesis_hierarchy_flattening: Optional[bool] = False
    stop_on_synthesis_closure_failure: Optional[bool] = False
    stop_on_synthesis_lec_failure: Optional[bool] = False

    # stage control (lets users do synth->gds or floorplan->gds later)
    start_stage: Optional[str] = "arch2rtl"   # "arch2rtl" | "synth" | "floorplan"
    stop_stage: Optional[str] = "tapeout"


class DigitalSpec2RTLCheckAppIn(DigitalArch2RTLAppIn, DigitalRTLSourceIn):
    """
    Standalone Spec-to-RTL conformance check for generated or third-party RTL.
    """
    check_depth: Optional[str] = "standard"


class DigitalDQAAppIn(BaseModel):
    # RTL source options
    rtl_source_mode: Optional[str] = None  # "from_arch2rtl" | "paste" | "repo_path"
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    repo_ref: Optional[str] = None
    repo_subdir: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None  # [{path, content}]
    source_arch2rtl_workflow_id: Optional[str] = None
    parent_workflow_id: Optional[str] = None
    upstream_workflows: Optional[Dict[str, Any]] = None

    clocks: Optional[Any] = None
    resets: Optional[Any] = None
    clock_constraints: Optional[Any] = None

    lint_profile: Optional[str] = "medium"
    cdc_profile: Optional[str] = "default"
    target: Optional[str] = "asic"
    toggles: Optional[Dict[str, bool]] = None  # {"enable_autofix":false}


class DigitalVerifyAppIn(BaseModel):
    rtl_source_mode: Optional[str] = None
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    repo_ref: Optional[str] = None
    repo_subdir: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None
    source_arch2rtl_workflow_id: Optional[str] = None
    parent_workflow_id: Optional[str] = None
    upstream_workflows: Optional[Dict[str, Any]] = None

    test_intent: Optional[str] = None
    verification_plan: Optional[str] = None
    monitor_checker_plan: Optional[str] = None
    interfaces: Optional[Any] = None
    random_vs_directed: Optional[str] = None
    coverage_targets: Optional[str] = None
    coverage_plan: Optional[str] = None
    simulator_type: Optional[str] = None
    seed_count: Optional[int] = None

    toolchain: Optional[Dict[str, str]] = None
    run_closure_analysis: Optional[bool] = False
    enable_failure_debug: Optional[bool] = False
    failure_debug_options: Optional[Dict[str, Any]] = None
    toggles: Optional[Dict[str, bool]] = None  # {"enable_formal":false, "enable_golden_model":false}
    clock_constraints: Optional[Any] = None


class DigitalVerifyClosureAppIn(BaseModel):
    source_verify_workflow_id: str
    coverage_targets: Optional[str] = None
    seed_count: Optional[int] = None
    seed_budget: Optional[int] = None
    max_iterations: Optional[int] = 1
    rerun_mode: Optional[str] = "coverage_targeted"
    random_vs_directed: Optional[str] = None
    enable_failure_debug: Optional[bool] = False
    failure_debug_options: Optional[Dict[str, Any]] = None
    approval_mode: Optional[str] = "automatic"
    toolchain: Optional[Dict[str, str]] = None


class DigitalSmokeAppIn(BaseModel):
    # RTL source (same spirit as DQA/Verify)
    rtl_source_mode: Optional[str] = None  # "from_arch2rtl" | "paste" | "repo_path"
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    repo_ref: Optional[str] = None
    repo_subdir: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None  # [{path, content}]
    source_arch2rtl_workflow_id: Optional[str] = None
    parent_workflow_id: Optional[str] = None
    upstream_workflows: Optional[Dict[str, Any]] = None

    # Smoke knobs (small)
    simulator_type: Optional[str] = None     # e.g. "verilator" / "iverilog" / "vcs"
    time_budget: Optional[str] = "fast"      # "fast" | "medium"
    seed_count: Optional[int] = None         # optional override
    enable_waveform: Optional[bool] = False
    clock_constraints: Optional[Any] = None

    # optional freeform notes
    notes: Optional[str] = None


class DigitalIntegrateAppIn(BaseModel):
    # RTL source (same spirit as DQA/Verify/Smoke)
    rtl_source_mode: Optional[str] = None  # "from_arch2rtl" | "paste" | "repo_path"
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    repo_ref: Optional[str] = None
    repo_subdir: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None  # [{path, content}]
    source_arch2rtl_workflow_id: Optional[str] = None
    parent_workflow_id: Optional[str] = None
    upstream_workflows: Optional[Dict[str, Any]] = None

    # Integrate knobs (Model 2: text -> intent -> top.sv)
    top_name: str
    integration_description: str

    # Optional extras
    enable_packaging: Optional[bool] = True
    notes: Optional[str] = None
    clock_constraints: Optional[Any] = None

class ValidationPlanCoverageAppIn(BaseModel):
    datasheet_text: str
    goal: Optional[str] = None
    scope_filters: Optional[Dict[str, Any]] = None
    enable_scope: Optional[bool] = False
    enable_coverage: Optional[bool] = True

class BenchSetupAppIn(BaseModel):
    bench_name: str
    instruments: List[Dict[str, Any]] = Field(default_factory=list)  # UI sends richer objects
    instrument_ids: Optional[List[str]] = None  # allow direct selection too
    enable_schematic: Optional[bool] = True
    enable_preflight: Optional[bool] = False
    bench_location: Optional[str] = None

class PreflightAppIn(BaseModel):
    bench_ref: str
    quick_mode: Optional[bool] = True

class ValidationInsightsAppIn(BaseModel):
    lookback_runs: int = 10
    tags: Optional[List[str]] = None
    enable_evolution: Optional[bool] = True
    enable_coverage: Optional[bool] = True

def execute_validation_app_background(
    workflow_id: str,
    run_id: str,
    user_id: str,
    artifact_dir: str,
    app_name: str,                 # "validation-plan" | "bench-setup" | "preflight" | "validation-insights"
    studio_template: str,          # "Validation_PlanCoverage" | ...
    payload: Dict[str, Any],
):
    try:
        os.makedirs(artifact_dir, exist_ok=True)

        shared_state = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "artifact_dir": artifact_dir,
            "supabase_client": supabase,
            "user_id": user_id,
        }

        # Inject payload -> shared_state
        for k, v in (payload or {}).items():
            if v is not None:
                shared_state[k] = v

        # Normalize datasheet/spec text for plan apps
        ds = shared_state.get("datasheet_text") or shared_state.get("spec_text")
        if ds:
            shared_state["datasheet_text"] = ds
            shared_state["spec"] = ds

        append_log_workflow(workflow_id, f"🚀 Starting Validation App: {app_name}", phase="start")
        append_log_run(run_id, f"🚀 Starting Validation App: {app_name}")

        # Load Studio template, execute nodes
        defn = _load_workflow_def_by_name(studio_template, user_id=user_id)
        nodes = _definition_to_executor_nodes(defn)

        append_log_workflow(workflow_id, f"▶️ Template: {studio_template}", phase="running")
        append_log_run(run_id, f"▶️ Template: {studio_template}")

        _run_nodes_with_shared_state(
            workflow_id=workflow_id,
            run_id=run_id,
            loop_type="validation",
            nodes=nodes,
            shared_state=shared_state,
        )

        append_log_workflow(workflow_id, f"🎉 Validation App complete: {app_name}", status="completed", phase="done")
        append_log_run(run_id, f"🎉 Validation App complete: {app_name}", status="completed")

    except Exception as e:
        err = f"❌ Validation App crashed ({app_name}): {type(e).__name__}: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")

def execute_digital_app_background(
    workflow_id: str,
    run_id: str,
    user_id: str,
    artifact_dir: str,
    app_name: str,
    template_workflow_name: str,
    payload: Dict[str, Any],
):
    try:
        os.makedirs(artifact_dir, exist_ok=True)

        shared_state = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "artifact_dir": artifact_dir,
            "supabase_client": supabase,
            "user_id": user_id,
            # IMPORTANT: give agents a canonical storage prefix for robust ZIP (?full=1)
            "artifact_prefix": f"backend/workflows/{workflow_id}/digital/{app_name}/{run_id}/",
            "app_name": app_name,
            "loop_type": "digital",
            "template_workflow": template_workflow_name,
            "template_workflow_name": template_workflow_name,
            "workflow_name": template_workflow_name,
        }

        # Inject payload fields into shared_state
        for k, v in (payload or {}).items():
            if v is not None:
                shared_state[k] = v
        if shared_state.get("system_rtl_workflow_id") and not shared_state.get("from_workflow_id"):
            shared_state["from_workflow_id"] = shared_state.get("system_rtl_workflow_id")
            shared_state["source_arch2rtl_workflow_id"] = shared_state.get("system_rtl_workflow_id")

        upstream = shared_state.get("upstream_workflows") if isinstance(shared_state.get("upstream_workflows"), dict) else {}
        source_arch2rtl_workflow_id = (
            shared_state.get("source_arch2rtl_workflow_id")
            or shared_state.get("from_workflow_id")
            or upstream.get("arch2rtl")
        )
        if shared_state.get("rtl_source_mode") == "from_arch2rtl" and source_arch2rtl_workflow_id and not shared_state.get("from_workflow_id"):
            shared_state["from_workflow_id"] = source_arch2rtl_workflow_id
        workflow_chain = {
            "source_arch2rtl_workflow_id": source_arch2rtl_workflow_id,
            "parent_workflow_id": shared_state.get("parent_workflow_id"),
            "current_workflow_id": workflow_id,
            "current_run_id": run_id,
            "current_app": app_name,
            "upstream_workflows": {
                **upstream,
                **({"arch2rtl": source_arch2rtl_workflow_id} if source_arch2rtl_workflow_id else {}),
                app_name: workflow_id,
            },
        }
        shared_state["workflow_chain"] = workflow_chain
        save_text_artifact_and_record(
            workflow_id,
            "Digital Workflow Chain",
            f"digital/{app_name}",
            "workflow_chain.json",
            json.dumps(workflow_chain, indent=2),
        )

        # Normalize spec fields (Arch2RTL agents often expect state["spec"])
        if shared_state.get("spec_text"):
            shared_state["spec"] = shared_state["spec_text"]
        if app_name == "verify":
            shared_state["_fail_fast_on_agent_error"] = True
        if app_name == "arch2rtl":
            toggles_for_fail_fast = shared_state.get("toggles") if isinstance(shared_state.get("toggles"), dict) else {}
            if toggles_for_fail_fast.get("insert_mbist") or toggles_for_fail_fast.get("enable_mbist_rtl_insertion"):
                shared_state["_fail_fast_on_agent_error"] = True

        append_log_workflow(workflow_id, f"🚀 Starting Digital App: {app_name}", phase="start")
        append_log_run(run_id, f"🚀 Starting Digital App: {app_name}")

        append_log_workflow(workflow_id, f"▶️ Loading Studio workflow: {template_workflow_name}", phase="load")
        append_log_run(run_id, f"▶️ Loading Studio workflow: {template_workflow_name}")

        # Load Studio prebuilt workflow and convert to executor nodes
        defn = _load_workflow_def_by_name(
            template_workflow_name,
            user_id=user_id,
            force_platform_definition=True,
        )
        nodes = _definition_to_executor_nodes(defn)
        toggles = shared_state.get("toggles") if isinstance(shared_state.get("toggles"), dict) else {}
        rtl_source_mode = str(shared_state.get("rtl_source_mode") or "").strip().lower()
        using_existing_system_rtl = bool(shared_state.get("system_rtl_workflow_id")) or rtl_source_mode in {"paste", "repo_path"}
        if template_workflow_name in {"System_DQA", "System_Sim", "System_Firmware", "System_Synthesis", "System_PD"} and using_existing_system_rtl:
            skip_labels = {
                "Digital Spec Agent",
                "Digital Architecture Agent",
                "Digital Microarchitecture Agent",
                "Digital Register Map Agent",
                "Digital Clock & Reset Architecture Agent",
                "Digital RTL Agent",
                "Digital RTL Signature Agent",
                "Digital RTL Refactoring Agent",
                "Digital Power Intent (UPF-lite) Agent",
                "Digital UPF Static Check Agent",
                "Digital IP Packaging & Handoff Agent",
                "Analog Spec Builder Agent",
                "Analog Behavioral Model Agent",
                "Analog Abstract Views Agent",
                "System Integration Intent Agent",
                "System Top Assembly Agent",
                "Analog Macro Interface Contract Agent",
                "System Assertions (SVA) Agent",
                "System RTL Handoff Package Agent",
                "System RTL Evidence Dashboard Agent",
                "Digital Spec2RTL Conformance Agent",
            }
            original_count = len(nodes)
            nodes = [
                node for node in nodes
                if str((node.get("data") or {}).get("backendLabel") or node.get("label") or node.get("name") or "") not in skip_labels
            ]
            append_log_workflow(
                workflow_id,
                f"{template_workflow_name}: using source System RTL workflow {shared_state.get('system_rtl_workflow_id')}; skipped {original_count - len(nodes)} System RTL generation nodes",
                phase="system_rtl_source",
            )
            append_log_run(
                run_id,
                f"{template_workflow_name}: using source System RTL workflow {shared_state.get('system_rtl_workflow_id')}; skipped {original_count - len(nodes)} System RTL generation nodes",
            )
        if toggles.get("gen_upf_lite") is False:
            upf_agents = {
                "Digital Power Intent (UPF-lite) Agent",
                "Digital UPF Static Check Agent",
            }
            nodes = [
                node for node in nodes
                if ((node.get("data") or {}).get("backendLabel") or node.get("label")) not in upf_agents
            ]
        if not (toggles.get("insert_mbist") or toggles.get("enable_mbist_rtl_insertion")):
            nodes = [
                node for node in nodes
                if ((node.get("data") or {}).get("backendLabel") or node.get("label")) != "Digital MBIST RTL Insertion Agent"
            ]
        if toggles.get("run_spec2rtl_check") and app_name in {"arch2rtl", "arch2synthesis", "arch2sim", "arch2tapeout"}:
            before_by_app = {
                "arch2rtl": "Digital Arch2RTL Dashboard Agent",
                "arch2synthesis": "Digital Foundry Profile Agent",
                "arch2sim": "Digital Testbench Generator Agent",
                "arch2tapeout": "Digital Foundry Profile Agent",
            }
            nodes = _insert_node_before_once(
                nodes,
                "Digital Spec2RTL Conformance Agent",
                before_by_app.get(app_name, "Digital Executive Summary Agent"),
            )
        if shared_state.get("rtl_source_mode") and app_name == "arch2synthesis":
            keep = {
                "Digital RTL Handoff Ingest Agent",
                "Digital IP Packaging & Handoff Agent",
                "Digital Spec2RTL Conformance Agent",
                "Digital Foundry Profile Agent",
                "Digital Implementation Setup Agent",
                "Digital Synthesis Agent",
                "Digital Logic Equivalence Check Agent",
                "Digital Synthesis Closure Agent",
                "Digital UPF Static Check Agent",
                "Digital DFT Scan Stitching Agent",
                "Digital Post-DFT Logic Equivalence Check Agent",
                "Digital Scan ATPG Coverage Agent",
                "Digital MBIST Collateral Agent",
            }
            nodes = [node for node in nodes if ((node.get("data") or {}).get("backendLabel") or node.get("label")) in keep]
        if shared_state.get("rtl_source_mode") and app_name == "smoke":
            skip = {"Digital RTL Agent"}
            nodes = [node for node in nodes if ((node.get("data") or {}).get("backendLabel") or node.get("label")) not in skip]
        if shared_state.get("rtl_source_mode") and app_name == "arch2tapeout":
            keep = {
                "Digital RTL Handoff Ingest Agent",
                "Digital IP Packaging & Handoff Agent",
                "Digital Spec2RTL Conformance Agent",
                "Digital Foundry Profile Agent",
                "Digital Implementation Setup Agent",
                "Digital Synthesis Agent",
                "Digital Logic Equivalence Check Agent",
                "Digital Synthesis Closure Agent",
                "Digital UPF Static Check Agent",
                "Digital DFT Scan Stitching Agent",
                "Digital Post-DFT Logic Equivalence Check Agent",
                "Digital Scan ATPG Coverage Agent",
                "Digital MBIST Collateral Agent",
                "Digital STA PrePlace Agent",
                "Digital Floorplan Agent",
                "Digital Placement Agent",
                "Digital STA PostPlace Agent",
                "Digital CTS Agent",
                "Digital STA PostCTS Agent",
                "Digital Route Agent",
                "Digital STA PostRoute Agent",
                "Digital Fill Agent",
                "Digital STA PostFill Agent",
                "Digital DRC Agent",
                "Digital LVS Agent",
                "Digital Tapeout Agent",
                "Digital Tapeout Logic Equivalence Check Agent",
                "Digital Executive Summary Agent",
                "Digital PD Signoff Closure Agent",
            }
            nodes = [node for node in nodes if ((node.get("data") or {}).get("backendLabel") or node.get("label")) in keep]
        if app_name == "verify":
            optional_before = "Digital Simulation Control Agent"
            if toggles.get("enable_golden_model"):
                nodes = _insert_node_before_once(
                    nodes,
                    "Digital Golden Model Comparison Agent",
                    optional_before,
                )
            if toggles.get("enable_formal"):
                nodes = _insert_node_before_once(
                    nodes,
                    "Digital Formal Verification Agent",
                    optional_before,
                )

        # Run nodes (loop_type="digital" so it uses DIGITAL_AGENT_FUNCTIONS)
        if app_name == "verify_closure_loop":
            max_iterations = max(1, min(int(shared_state.get("max_iterations") or 1), 10))

            def _node_label(node: Dict[str, Any]) -> str:
                return ((node.get("data") or {}).get("backendLabel") or node.get("label") or "").strip()

            ingest_nodes = [node for node in nodes if _node_label(node) == "Digital Verify Closure Ingest Agent"]
            body_nodes = [node for node in nodes if _node_label(node) != "Digital Verify Closure Ingest Agent"]
            if ingest_nodes:
                append_log_workflow(workflow_id, "Closure loop ingest: loading baseline Verify artifacts", phase="closure_loop_ingest")
                append_log_run(run_id, "Closure loop ingest: loading baseline Verify artifacts")
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="digital",
                    nodes=ingest_nodes,
                    shared_state=shared_state,
                )
            for iteration in range(1, max_iterations + 1):
                shared_state["closure_iteration_index"] = iteration
                append_log_workflow(workflow_id, f"Closure loop iteration {iteration}/{max_iterations} started", phase=f"closure_loop_iteration_{iteration}")
                append_log_run(run_id, f"Closure loop iteration {iteration}/{max_iterations} started")
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="digital",
                    nodes=body_nodes,
                    shared_state=shared_state,
                )
                judgement = shared_state.get("closure_iteration_judgement") if isinstance(shared_state.get("closure_iteration_judgement"), dict) else {}
                stop_reason = judgement.get("stop_reason") or "not_reported"
                append_log_workflow(workflow_id, f"Closure loop iteration {iteration}/{max_iterations} completed: {stop_reason}", phase=f"closure_loop_iteration_{iteration}_done")
                append_log_run(run_id, f"Closure loop iteration {iteration}/{max_iterations} completed: {stop_reason}")
                if stop_reason in {"closure_achieved", "no_measurable_improvement"}:
                    append_log_workflow(workflow_id, f"Closure loop stopped early after iteration {iteration}: {stop_reason}", phase="closure_loop_stop")
                    append_log_run(run_id, f"Closure loop stopped early after iteration {iteration}: {stop_reason}")
                    break
        elif app_name == "arch2tapeout" and bool(shared_state.get("run_signoff_closure_loop") or (isinstance(shared_state.get("toggles"), dict) and shared_state["toggles"].get("run_signoff_closure_loop"))):
            max_iterations = max(1, min(int(shared_state.get("max_signoff_closure_iterations") or shared_state.get("max_signoff_iterations") or 1), 2))

            def _node_label(node: Dict[str, Any]) -> str:
                return ((node.get("data") or {}).get("backendLabel") or node.get("label") or "").strip()

            labels = [_node_label(node) for node in nodes]
            closure_label = "Digital PD Signoff Closure Agent"
            if closure_label not in labels:
                append_log_workflow(workflow_id, "Signoff closure loop requested but closure agent is not in this workflow.", phase="signoff_closure_missing")
                append_log_run(run_id, "Signoff closure loop requested but closure agent is not in this workflow.")
                _run_nodes_with_shared_state(workflow_id=workflow_id, run_id=run_id, loop_type="digital", nodes=nodes, shared_state=shared_state)
            else:
                closure_idx = labels.index(closure_label)
                prefix_nodes = nodes[:closure_idx + 1]
                suffix_nodes = nodes[closure_idx + 1:]
                if bool(shared_state.get("run_synthesis_closure_loop") or (isinstance(shared_state.get("toggles"), dict) and shared_state["toggles"].get("run_synthesis_closure_loop"))):
                    synth_label = "Digital Synthesis Closure Agent"
                    if synth_label in labels and labels.index(synth_label) < closure_idx:
                        synth_idx = labels.index(synth_label)
                        try:
                            synth_start_idx = labels.index("Digital Synthesis Agent")
                        except ValueError:
                            synth_start_idx = synth_idx
                        synth_max_iterations = max(1, min(int(shared_state.get("max_synthesis_closure_iterations") or 1), 2))
                        append_log_workflow(workflow_id, "Synthesis closure loop baseline started before signoff closure", phase="synthesis_closure_baseline")
                        append_log_run(run_id, "Synthesis closure loop baseline started before signoff closure")
                        shared_state["synthesis_closure_iteration_index"] = 0
                        _run_nodes_with_shared_state(
                            workflow_id=workflow_id,
                            run_id=run_id,
                            loop_type="digital",
                            nodes=nodes[:synth_idx + 1],
                            shared_state=shared_state,
                        )
                        plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                        append_log_workflow(workflow_id, f"Synthesis closure baseline completed: {plan.get('status') or 'not_reported'}", phase="synthesis_closure_baseline_done")
                        append_log_run(run_id, f"Synthesis closure baseline completed: {plan.get('status') or 'not_reported'}")
                        for synth_iteration in range(1, synth_max_iterations + 1):
                            plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                            if plan.get("closure_complete") is True or plan.get("status") == "clean":
                                append_log_workflow(workflow_id, f"Synthesis closure stopped before iteration {synth_iteration}: closure achieved", phase="synthesis_closure_stop")
                                append_log_run(run_id, f"Synthesis closure stopped before iteration {synth_iteration}: closure achieved")
                                break
                            shared_state["synthesis_closure_iteration_index"] = synth_iteration
                            append_log_workflow(workflow_id, f"Synthesis closure iteration {synth_iteration}/{synth_max_iterations} started", phase=f"synthesis_closure_iteration_{synth_iteration}")
                            append_log_run(run_id, f"Synthesis closure iteration {synth_iteration}/{synth_max_iterations} started")
                            _run_nodes_with_shared_state(
                                workflow_id=workflow_id,
                                run_id=run_id,
                                loop_type="digital",
                                nodes=nodes[synth_start_idx:synth_idx + 1],
                                shared_state=shared_state,
                            )
                            plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                            append_log_workflow(workflow_id, f"Synthesis closure iteration {synth_iteration}/{synth_max_iterations} completed: {plan.get('status') or 'not_reported'}", phase=f"synthesis_closure_iteration_{synth_iteration}_done")
                            append_log_run(run_id, f"Synthesis closure iteration {synth_iteration}/{synth_max_iterations} completed: {plan.get('status') or 'not_reported'}")
                            if plan.get("closure_complete") is True or plan.get("status") == "clean":
                                append_log_workflow(workflow_id, f"Synthesis closure stopped after iteration {synth_iteration}: closure achieved", phase="synthesis_closure_stop")
                                append_log_run(run_id, f"Synthesis closure stopped after iteration {synth_iteration}: closure achieved")
                                break
                        prefix_nodes = nodes[synth_idx + 1:closure_idx + 1]

                append_log_workflow(workflow_id, "Signoff closure loop baseline started", phase="signoff_closure_baseline")
                append_log_run(run_id, "Signoff closure loop baseline started")
                shared_state["signoff_closure_iteration_index"] = 0
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="digital",
                    nodes=prefix_nodes,
                    shared_state=shared_state,
                )
                plan = (((shared_state.get("digital") or {}).get("signoff_closure") or {}).get("plan") or {})
                append_log_workflow(
                    workflow_id,
                    f"Signoff closure baseline completed: {plan.get('status') or 'not_reported'}",
                    phase="signoff_closure_baseline_done",
                )
                append_log_run(run_id, f"Signoff closure baseline completed: {plan.get('status') or 'not_reported'}")

                for iteration in range(1, max_iterations + 1):
                    plan = (((shared_state.get("digital") or {}).get("signoff_closure") or {}).get("plan") or {})
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"Signoff closure stopped before iteration {iteration}: closure achieved", phase="signoff_closure_stop")
                        append_log_run(run_id, f"Signoff closure stopped before iteration {iteration}: closure achieved")
                        break
                    restart_stage = str(plan.get("selected_restart_stage") or "").strip()
                    if not restart_stage or restart_stage not in labels:
                        append_log_workflow(workflow_id, f"Signoff closure stopped before iteration {iteration}: no runnable restart stage", phase="signoff_closure_stop")
                        append_log_run(run_id, f"Signoff closure stopped before iteration {iteration}: no runnable restart stage")
                        break
                    start_idx = labels.index(restart_stage)
                    rerun_nodes = nodes[start_idx:closure_idx + 1]
                    shared_state["signoff_closure_iteration_index"] = iteration
                    append_log_workflow(workflow_id, f"Signoff closure iteration {iteration}/{max_iterations} started from {restart_stage}", phase=f"signoff_closure_iteration_{iteration}")
                    append_log_run(run_id, f"Signoff closure iteration {iteration}/{max_iterations} started from {restart_stage}")
                    _run_nodes_with_shared_state(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        loop_type="digital",
                        nodes=rerun_nodes,
                        shared_state=shared_state,
                    )
                    plan = (((shared_state.get("digital") or {}).get("signoff_closure") or {}).get("plan") or {})
                    append_log_workflow(
                        workflow_id,
                        f"Signoff closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}",
                        phase=f"signoff_closure_iteration_{iteration}_done",
                    )
                    append_log_run(run_id, f"Signoff closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}")
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"Signoff closure stopped after iteration {iteration}: closure achieved", phase="signoff_closure_stop")
                        append_log_run(run_id, f"Signoff closure stopped after iteration {iteration}: closure achieved")
                        break

                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="digital",
                    nodes=suffix_nodes,
                    shared_state=shared_state,
                )
        elif app_name in {"arch2synthesis", "arch2tapeout"} and bool(shared_state.get("run_synthesis_closure_loop") or (isinstance(shared_state.get("toggles"), dict) and shared_state["toggles"].get("run_synthesis_closure_loop"))):
            max_iterations = max(1, min(int(shared_state.get("max_synthesis_closure_iterations") or 1), 2))

            def _node_label(node: Dict[str, Any]) -> str:
                return ((node.get("data") or {}).get("backendLabel") or node.get("label") or "").strip()

            labels = [_node_label(node) for node in nodes]
            closure_label = "Digital Synthesis Closure Agent"
            if closure_label not in labels:
                append_log_workflow(workflow_id, "Synthesis closure loop requested but closure agent is not in this workflow.", phase="synthesis_closure_missing")
                append_log_run(run_id, "Synthesis closure loop requested but closure agent is not in this workflow.")
                _run_nodes_with_shared_state(workflow_id=workflow_id, run_id=run_id, loop_type="digital", nodes=nodes, shared_state=shared_state)
            else:
                closure_idx = labels.index(closure_label)
                first_rerun_label = "Digital Synthesis Agent"
                try:
                    rerun_start_idx = labels.index(first_rerun_label)
                except ValueError:
                    rerun_start_idx = closure_idx
                prefix_nodes = nodes[:closure_idx + 1]
                rerun_nodes = nodes[rerun_start_idx:closure_idx + 1]
                suffix_nodes = nodes[closure_idx + 1:]

                append_log_workflow(workflow_id, "Synthesis closure loop baseline started", phase="synthesis_closure_baseline")
                append_log_run(run_id, "Synthesis closure loop baseline started")
                shared_state["synthesis_closure_iteration_index"] = 0
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="digital",
                    nodes=prefix_nodes,
                    shared_state=shared_state,
                )
                closure_state = ((shared_state.get("digital") or {}).get("synthesis_closure") or {})
                plan = closure_state.get("plan") if isinstance(closure_state, dict) else {}
                append_log_workflow(
                    workflow_id,
                    f"Synthesis closure baseline completed: {plan.get('status') or 'not_reported'}",
                    phase="synthesis_closure_baseline_done",
                )
                append_log_run(run_id, f"Synthesis closure baseline completed: {plan.get('status') or 'not_reported'}")

                for iteration in range(1, max_iterations + 1):
                    plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"Synthesis closure stopped before iteration {iteration}: closure achieved", phase="synthesis_closure_stop")
                        append_log_run(run_id, f"Synthesis closure stopped before iteration {iteration}: closure achieved")
                        break
                    shared_state["synthesis_closure_iteration_index"] = iteration
                    append_log_workflow(workflow_id, f"Synthesis closure iteration {iteration}/{max_iterations} started", phase=f"synthesis_closure_iteration_{iteration}")
                    append_log_run(run_id, f"Synthesis closure iteration {iteration}/{max_iterations} started")
                    _run_nodes_with_shared_state(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        loop_type="digital",
                        nodes=rerun_nodes,
                        shared_state=shared_state,
                    )
                    plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                    append_log_workflow(
                        workflow_id,
                        f"Synthesis closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}",
                        phase=f"synthesis_closure_iteration_{iteration}_done",
                    )
                    append_log_run(run_id, f"Synthesis closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}")
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"Synthesis closure stopped after iteration {iteration}: closure achieved", phase="synthesis_closure_stop")
                        append_log_run(run_id, f"Synthesis closure stopped after iteration {iteration}: closure achieved")
                        break

                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="digital",
                    nodes=suffix_nodes,
                    shared_state=shared_state,
                )
        else:
            _run_nodes_with_shared_state(
                workflow_id=workflow_id,
                run_id=run_id,
                loop_type="digital",
                nodes=nodes,
                shared_state=shared_state,
            )

        append_log_workflow(workflow_id, f"🎉 Digital App complete: {app_name}", status="completed", phase="done")
        append_log_run(run_id, f"🎉 Digital App complete: {app_name}", status="completed")

    except Exception as e:
        err = f"❌ Digital App crashed ({app_name}): {type(e).__name__}: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")


def _create_app_workflow_and_run(user_id: str, app_title: str, loop_type: str):
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    supabase.table("workflows").insert({
        "id": workflow_id,
        "user_id": user_id,
        "name": app_title,
        "status": "running",
        "phase": "queued",
        "logs": "🚀 App run queued.",
        "created_at": now,
        "updated_at": now,
        "artifacts": {},
        "loop_type": loop_type,
        "definitions": {"app_intent": app_title},
    }).execute()

    user_folder = str(user_id or "anonymous")
    artifact_dir = os.path.join("artifacts", user_folder, workflow_id, run_id, "digital")
    os.makedirs(artifact_dir, exist_ok=True)

    supabase.table("runs").insert({
        "id": run_id,
        "user_id": user_id,
        "workflow_id": workflow_id,
        "loop_type": loop_type,
        "status": "running",
        "logs": "🚀 App run started.",
        "artifacts_path": artifact_dir,
        "created_at": now
    }).execute()

    return workflow_id, run_id, artifact_dir


@app.post("/apps/arch2rtl/run")
async def apps_arch2rtl_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalArch2RTLAppIn):
    user_id = _require_user_id(request)
    payload_dict = payload.dict()
    demo_run = False
    try:
        checkout_started = _checkout_started_for_request(request, user_id)
    except BillingPaymentRequired as exc:
        raise _payment_required_response(exc)

    if not checkout_started:
        onboarding_service = _onboarding_service_for_main()
        if not is_arch2rtl_guided_demo_payload(payload_dict):
            raise _trial_required_response(
                "Start your 3-day trial to run custom Arch2RTL workflows."
            )
        if not onboarding_service.can_run_arch2rtl_demo(user_id):
            raise _trial_required_response(
                "You have completed your free Arch2RTL demo runs. Start your 3-day trial to keep using ChipLoop."
            )
        demo_run = True

    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Arch2RTL", "digital")
    artifact_dir = os.path.join(base_dir, "arch2rtl")
    os.makedirs(artifact_dir, exist_ok=True)
    if demo_run:
        _onboarding_service_for_main().record_arch2rtl_demo_run(user_id, workflow_id=workflow_id)

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "arch2rtl",
        "Digital_Arch2RTL",
        payload_dict,
    )

    response = {"ok": True, "workflow_id": workflow_id, "run_id": run_id}
    if demo_run:
        response["demo"] = _onboarding_service_for_main().arch2rtl_demo_usage(user_id)
        response["trial_cta"] = {
            "show": True,
            "message": "Start your 3-day trial to run your own specs. No charge today.",
            "checkout_plan_id": "starter",
            "checkout_url": "/pricing?trial=1",
            "checkout_label": "Start 3-day trial",
        }
    return response

@app.post("/apps/arch2synthesis/run")
async def apps_arch2synthesis_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalArch2SynthesisAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Arch2Synthesis", "digital")
    artifact_dir = os.path.join(base_dir, "arch2synthesis")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "arch2synthesis",
        "Digital_Arch2Synthesis",
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/arch2tapeout/run")
async def apps_arch2tapeout_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalArch2TapeoutAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Arch2Tapeout", "digital")
    artifact_dir = os.path.join(base_dir, "arch2tapeout")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "arch2tapeout",
        "Digital_Arch2Tapeout",
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/spec2rtl/run")
async def apps_spec2rtl_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalSpec2RTLCheckAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Spec2RTL Check", "digital")
    artifact_dir = os.path.join(base_dir, "spec2rtl")
    os.makedirs(artifact_dir, exist_ok=True)

    payload_dict = payload.dict()
    toggles = payload_dict.get("toggles") if isinstance(payload_dict.get("toggles"), dict) else {}
    toggles["run_spec2rtl_check"] = True
    payload_dict["toggles"] = toggles

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "spec2rtl",
        "Digital_Spec2RTL_Check",
        payload_dict,
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


def _artifact_storage_paths_for_main(value: Any) -> List[str]:
    paths: List[str] = []
    if isinstance(value, dict):
        for child in value.values():
            paths.extend(_artifact_storage_paths_for_main(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_artifact_storage_paths_for_main(child))
    elif isinstance(value, str):
        paths.append(value.replace("\\", "/"))
    return paths


def _list_storage_tree_for_main(folder: str, depth: int = 0, max_depth: int = 5) -> List[str]:
    if depth > max_depth:
        return []
    try:
        entries = supabase.storage.from_(ARTIFACT_BUCKET).list(folder) or []
    except Exception:
        return []
    paths: List[str] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            continue
        path = f"{folder.rstrip('/')}/{name}"
        paths.append(path)
        paths.extend(_list_storage_tree_for_main(path, depth + 1, max_depth))
    return paths


def _download_text_for_main(path: str, max_bytes: int = 2_000_000) -> str:
    try:
        raw = supabase.storage.from_(ARTIFACT_BUCKET).download(path)
    except Exception:
        return ""
    if not raw:
        return ""
    if len(raw) > max_bytes:
        return ""
    return raw.decode("utf-8", errors="replace")


@app.get("/apps/digital/clock-candidates/{source_workflow_id}")
async def apps_digital_clock_candidates(request: Request, source_workflow_id: str):
    _require_user_id(request)
    row = (
        supabase.table("workflows")
        .select("id,artifacts")
        .eq("id", source_workflow_id)
        .single()
        .execute()
    ).data or {}
    if not row:
        raise HTTPException(status_code=404, detail="source workflow not found")

    prefix = f"backend/workflows/{source_workflow_id}"
    paths = list(dict.fromkeys(
        _artifact_storage_paths_for_main(row.get("artifacts") or {})
        + _list_storage_tree_for_main(prefix)
    ))
    rtl_texts = [
        _download_text_for_main(path)
        for path in paths
        if path.lower().endswith((".sv", ".v")) and "/rtl/" in path.lower()
    ][:16]
    spec_text = ""
    for path in paths:
        lowered = path.lower()
        if lowered.endswith(".json") and ("spec" in os.path.basename(lowered) or "/spec/" in lowered):
            spec_text = _download_text_for_main(path)
            if spec_text:
                break
    sdc_text = ""
    for path in paths:
        if path.lower().endswith(".sdc"):
            sdc_text = _download_text_for_main(path)
            if sdc_text:
                break

    temp_dir = os.path.join("artifacts", "_clock_probe", source_workflow_id)
    os.makedirs(temp_dir, exist_ok=True)
    rtl_files: List[str] = []
    for idx, text in enumerate(rtl_texts):
        if not text:
            continue
        local = os.path.join(temp_dir, f"source_{idx}.sv")
        with open(local, "w", encoding="utf-8") as f:
            f.write(text)
        rtl_files.append(local)
    spec_obj: Any = {}
    if spec_text:
        try:
            spec_obj = json.loads(spec_text)
        except Exception:
            spec_obj = {}
    intent = build_clock_intent(spec=spec_obj, rtl_files=rtl_files, sdc_text=sdc_text)
    return {"ok": True, "source_workflow_id": source_workflow_id, "clock_intent": intent}


@app.post("/apps/dqa/run")
async def apps_dqa_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalDQAAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: DQA", "digital")
    artifact_dir = os.path.join(base_dir, "dqa")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "dqa",
        "Digital_DQA",
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/verify/run")
async def apps_verify_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalVerifyAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Verify", "digital")
    artifact_dir = os.path.join(base_dir, "verify")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "verify",
        "Digital_Verify",
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}

@app.post("/apps/verify/closure/run")
async def apps_verify_closure_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalVerifyClosureAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Verify Closure Analysis", "digital")
    artifact_dir = os.path.join(base_dir, "verify_closure")
    os.makedirs(artifact_dir, exist_ok=True)

    payload_dict = payload.dict()
    payload_dict["parent_workflow_id"] = payload.source_verify_workflow_id

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "verify_closure",
        "Digital_Verify_Closure",
        payload_dict,
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}

@app.post("/apps/verify/closure-loop/run")
async def apps_verify_closure_loop_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalVerifyClosureAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Verify Closure Loop", "digital")
    artifact_dir = os.path.join(base_dir, "verify_closure_loop")
    os.makedirs(artifact_dir, exist_ok=True)

    payload_dict = payload.dict()
    payload_dict["parent_workflow_id"] = payload.source_verify_workflow_id
    payload_dict["closure_iteration_index"] = 1
    payload_dict["max_iterations"] = max(int(payload.max_iterations or 1), 1)
    if not payload_dict.get("seed_budget"):
        payload_dict["seed_budget"] = payload.seed_count or 5

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "verify_closure_loop",
        "Digital_Verify_Closure_Loop",
        payload_dict,
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}

@app.post("/apps/smoke/run")
async def apps_smoke_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalSmokeAppIn):
    user_id = _require_user_id(request)

    # Reuse same helper used by other apps (Arch2RTL/DQA/Verify)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Smoke", "digital")

    artifact_dir = os.path.join(base_dir, "smoke")
    os.makedirs(artifact_dir, exist_ok=True)

    # Auto defaults (keep Smoke fast)
    data = payload.dict() if payload else {}
    if not data.get("seed_count"):
        tb = (data.get("time_budget") or "fast").lower()
        data["seed_count"] = 5 if tb == "fast" else 20

    # Normalize simulator key for downstream agents (some use sim_type/simulator)
    if data.get("simulator_type") and not data.get("simulator"):
        data["simulator"] = data["simulator_type"]
    if data.get("simulator_type") and not data.get("sim_type"):
        data["sim_type"] = data["simulator_type"]

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "smoke",             # app_name
        "Digital_Smoke",      # Studio workflow template name
        data,
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/integrate/run")
async def apps_integrate_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalIntegrateAppIn):
    user_id = _require_user_id(request)

    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Integrate", "digital")

    artifact_dir = os.path.join(base_dir, "integrate")
    os.makedirs(artifact_dir, exist_ok=True)

    data = payload.dict() if payload else {}

    # Normalize keys expected by Integrate agents / workflow
    # Model 2 uses integration_description (free text) and top_module
    data["top_module"] = data.get("top_name")

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "integrate",          # app_name
        "Digital_Integrate",  # Studio workflow template name
        data,
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}

@app.post("/apps/validation-plan/run")
async def apps_validation_plan_run(request: Request, background_tasks: BackgroundTasks, payload: ValidationPlanCoverageAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Validation Plan & Coverage", "validation")
    artifact_dir = os.path.join(base_dir, "validation-plan")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_validation_app_background,
        workflow_id, run_id, user_id, artifact_dir,
        "validation-plan",
        "Validation_PlanCoverage",
        payload.dict(),
    )
    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/bench-setup/run")
async def apps_bench_setup_run(request: Request, background_tasks: BackgroundTasks, payload: BenchSetupAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Bench Setup", "validation")
    artifact_dir = os.path.join(base_dir, "bench-setup")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_validation_app_background,
        workflow_id, run_id, user_id, artifact_dir,
        "bench-setup",
        "Validation_BenchSetup",
        payload.dict(),
    )
    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/preflight/run")
async def apps_preflight_run(request: Request, background_tasks: BackgroundTasks, payload: PreflightAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Preflight", "validation")
    artifact_dir = os.path.join(base_dir, "preflight")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_validation_app_background,
        workflow_id, run_id, user_id, artifact_dir,
        "preflight",
        "Validation_Preflight",
        payload.dict(),
    )
    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/validation-insights/run")
async def apps_validation_insights_run(request: Request, background_tasks: BackgroundTasks, payload: ValidationInsightsAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Validation Insights", "validation")
    artifact_dir = os.path.join(base_dir, "validation-insights")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_validation_app_background,
        workflow_id, run_id, user_id, artifact_dir,
        "validation-insights",
        "Validation_Insights",
        payload.dict(),
    )
    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}



from fastapi import Body
from fastapi.responses import FileResponse
from pathlib import Path

# ---------- Helpers ----------
def _artifacts_dir_for_workflow(workflow_id: str) -> Path:
    # Prefer the latest run row (artifacts_path), fallback to canonical path using workflows.user_id
    try:
        r = supabase.table("runs").select("artifacts_path").eq("workflow_id", workflow_id).order("created_at", desc=True).limit(1).execute()
        path = (r.data or [{}])[0].get("artifacts_path")
        if path and Path(path).exists():
            return Path(path)
    except Exception:
        pass

    wf = supabase.table("workflows").select("user_id").eq("id", workflow_id).single().execute()
    user_id = (wf.data or {}).get("user_id") or "anonymous"
    p = Path("artifacts") / user_id / workflow_id
    return p

def _unsupported_legacy_external_execution(payload: dict = Body(...)):
    """
    Body: { workflow_id, status, logs, artifacts?, runner_name }
    - Appends logs
    - Updates workflow status/phase
    - Marks run completed
    """
    raise HTTPException(status_code=410, detail="unsupported_legacy_endpoint")
    workflow_id = payload.get("workflow_id")
    status = (payload.get("status") or "completed").lower()
    logs = payload.get("logs") or ""
    artifacts = payload.get("artifacts") or {}
    runner_name = payload.get("runner_name")

    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id required")

    # Append logs and finalize workflow
    append_log_workflow(workflow_id, logs, status=status, phase="done")

    # Clear runner assignment and mark status
    supabase.table("workflows").update({
        "status": status,
        "phase": "done",
        "runner_assigned": None,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", workflow_id).execute()

    # Mark latest run completed
    try:
        r = supabase.table("runs").select("id").eq("workflow_id", workflow_id).order("created_at", desc=True).limit(1).execute()
        if (r.data or []):
            run_id = r.data[0]["id"]
            append_log_run(run_id, f"📦 Runner '{runner_name}' uploaded results.", status=status)
            supabase.table("runs").update({
                "status": status,
                "completed_at": datetime.utcnow().isoformat()
            }).eq("id", run_id).execute()
    except Exception as e:
        logger.warning(f"runs finalize failed: {e}")

    # Optionally merge artifacts metadata into workflow row
    try:
        row = supabase.table("workflows").select("artifacts").eq("id", workflow_id).single().execute()
        cur = (row.data or {}).get("artifacts") or {}
        cur["runner_upload"] = artifacts
        supabase.table("workflows").update({"artifacts": cur}).eq("id", workflow_id).execute()
    except Exception as e:
        logger.warning(f"artifacts merge failed: {e}")

    if runner_name:
        supabase.table("runners").upsert({
            "runner_name": runner_name,
            "status": "idle",
            "current_job": None,
            "last_seen": datetime.utcnow().isoformat()
        }, on_conflict="runner_name").execute()

    return {"ok": True}

# ---------- 4) List artifacts for a workflow ----------
@app.get("/list_artifacts/{workflow_id}")
def list_artifacts(workflow_id: str):
    base = _artifacts_dir_for_workflow(workflow_id)
    files = []
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file():
                files.append(str(p.relative_to(base)))
    return {"files": files, "base": str(base)}

# ---------- 5) Download a specific artifact file ----------
@app.get("/download_artifacts/{workflow_id}/{rel_path:path}")
def download_artifact(workflow_id: str, rel_path: str):
    base = _artifacts_dir_for_workflow(workflow_id)
    # prevent path traversal
    requested = (base / rel_path).resolve()
    if not str(requested).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not requested.exists() or not requested.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(requested))

@app.get("/apps/dashboard/artifact/{workflow_id}")
def dashboard_json_artifact(workflow_id: str, filename: str = Query(..., min_length=1, max_length=160)):
    if not re.fullmatch(r"[A-Za-z0-9_./-]+\.json", filename) or ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="invalid artifact filename")

    local_base = _artifacts_dir_for_workflow(workflow_id)
    if local_base.exists():
        exact = (local_base / filename).resolve()
        if str(exact).startswith(str(local_base.resolve())) and exact.is_file():
            try:
                return JSONResponse(json.loads(exact.read_text(encoding="utf-8")))
            except Exception:
                pass
        if "/" not in filename:
            for path in local_base.rglob(filename):
                if path.is_file():
                    try:
                        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
                    except Exception:
                        continue

    prefix = f"backend/workflows/{workflow_id}/"
    exact_storage_path = f"{prefix}{filename}"
    if "/" in filename:
        try:
            data = supabase.storage.from_(ARTIFACT_BUCKET).download(exact_storage_path)
            text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
            return JSONResponse(json.loads(text))
        except Exception:
            pass

    def find_storage_file(path_prefix: str) -> Optional[str]:
        entries = supabase.storage.from_(ARTIFACT_BUCKET).list(path_prefix) or []
        basename = filename.rsplit("/", 1)[-1]
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            full_path = f"{path_prefix}{name}"
            if name == basename:
                return full_path
            metadata = entry.get("metadata") or {}
            if not metadata.get("mimetype"):
                found = find_storage_file(full_path + "/")
                if found:
                    return found
        return None

    storage_path = find_storage_file(prefix)
    if not storage_path:
        raise HTTPException(status_code=404, detail=f"artifact not found: {filename}")
    try:
        raw = supabase.storage.from_(ARTIFACT_BUCKET).download(storage_path)
        return JSONResponse(json.loads(raw.decode("utf-8")))
    except Exception as exc:
        logger.warning("Dashboard artifact load failed workflow=%s filename=%s path=%s error=%s", workflow_id, filename, storage_path, exc)
        raise HTTPException(status_code=404, detail=f"artifact not found: {filename}") from exc

@app.get("/list_agents")
async def list_agents():
    G = build_capability_graph(AGENT_CAPABILITIES)
    return {
        "status": "ok",
        "agents": AGENT_CAPABILITIES,
        "graph": serialize_graph(G)
    }


@app.get("/sdk/agents", dependencies=[Depends(require_sdk_api_key("sdk_agents_list"))])
def sdk_list_agents(request: Request):
    registry = load_registry("registry")
    return {
        "status": "ok",
        "agents": [agent.__dict__ for agent in registry.agents.values()],
        "count": len(registry.agents),
        "upgrade_hint": getattr(request.state, "upgrade_hint", None),
    }


@app.get("/sdk/v1/agents", dependencies=[Depends(require_sdk_api_key("sdk_agents_list"))])
def sdk_v1_list_agents(request: Request):
    return sdk_list_agents(request)


@app.get("/sdk/workflows", dependencies=[Depends(require_sdk_api_key("sdk_workflows_list"))])
def sdk_list_workflows(request: Request):
    registry = load_registry("registry")
    return {
        "status": "ok",
        "workflows": [workflow.__dict__ for workflow in registry.workflows.values()],
        "count": len(registry.workflows),
        "upgrade_hint": getattr(request.state, "upgrade_hint", None),
    }


@app.get("/sdk/v1/workflows", dependencies=[Depends(require_sdk_api_key("sdk_workflows_list"))])
def sdk_v1_list_workflows(request: Request):
    return sdk_list_workflows(request)


@app.post("/sdk/v1/workflows/run", dependencies=[Depends(require_sdk_api_key("sdk_workflow_run"))])
async def sdk_v1_run_workflow(
    request: Request,
    background_tasks: BackgroundTasks,
    workflow: str = Form(...),
    file: UploadFile = File(None),
    spec_text: str = Form(None),
    run_config_json: Optional[str] = Form(None),
    instrument_ids: Optional[str] = Form(None),
    scope_json: Optional[str] = Form(None),
    bench_id: Optional[str] = Form(None),
    bench_name: Optional[str] = Form(None),
    bench_location: Optional[str] = Form(None),
    test_plan_name: Optional[str] = Form(None),
    preview_test_plan_json: Optional[str] = Form(None),
):
    return await run_workflow(
        request=request,
        background_tasks=background_tasks,
        workflow=workflow,
        file=file,
        spec_text=spec_text,
        run_config_json=run_config_json,
        instrument_ids=instrument_ids,
        scope_json=scope_json,
        bench_id=bench_id,
        bench_name=bench_name,
        bench_location=bench_location,
        test_plan_name=test_plan_name,
        preview_test_plan_json=preview_test_plan_json,
    )


@app.get("/sdk/workflows/{workflow_id}/status", dependencies=[Depends(require_sdk_api_key("sdk_workflow_status"))])
def sdk_workflow_status(workflow_id: str, request: Request):
    row = (
        supabase.table("workflows")
        .select("id,name,status,phase,loop_type,logs,artifacts,created_at,updated_at")
        .eq("id", workflow_id)
        .single()
        .execute()
    )
    if not row.data:
        raise HTTPException(status_code=404, detail="workflow not found")
    data = dict(row.data)
    data["workflow_id"] = data.get("id")
    data["upgrade_hint"] = getattr(request.state, "upgrade_hint", None)
    return data


@app.get("/sdk/v1/workflows/{workflow_id}/status", dependencies=[Depends(require_sdk_api_key("sdk_workflow_status"))])
def sdk_v1_workflow_status(workflow_id: str, request: Request):
    return sdk_workflow_status(workflow_id, request)


@app.get("/sdk/v1/workflows/{workflow_id}/artifacts", dependencies=[Depends(require_sdk_api_key("sdk_artifacts_list"))])
def sdk_v1_list_artifacts(workflow_id: str, request: Request):
    payload = list_artifacts(workflow_id)
    payload["upgrade_hint"] = getattr(request.state, "upgrade_hint", None)
    return payload


@app.get(
    "/sdk/v1/workflows/{workflow_id}/artifacts/{rel_path:path}",
    dependencies=[Depends(require_sdk_api_key("sdk_artifact_download"))],
)
def sdk_v1_download_artifact(workflow_id: str, rel_path: str):
    return download_artifact(workflow_id, rel_path)


@app.get("/sdk/v1/usage", dependencies=[Depends(require_sdk_api_key("sdk_usage"))])
def sdk_v1_usage(request: Request):
    service = get_api_key_service(getattr(request.app.state, "supabase", None))
    user_id = getattr(request.state, "user_id", None)
    return {
        "status": "ok",
        "usage": service.usage_summary(user_id),
        "upgrade_hint": getattr(request.state, "upgrade_hint", None),
    }


@app.get("/sdk/v1/plan", dependencies=[Depends(require_sdk_api_key("sdk_plan"))])
def sdk_v1_plan(request: Request):
    billing = getattr(request.app.state, "billing_service", None) or get_billing_service(
        getattr(request.app.state, "supabase", None)
    )
    user_id = getattr(request.state, "user_id", None)
    return {
        "status": "ok",
        "plan": billing.plan_summary(user_id),
        "upgrade_hint": getattr(request.state, "upgrade_hint", None),
    }


@app.post("/sdk/studio/plan-agent", dependencies=[Depends(require_sdk_api_key("sdk_studio_plan_agent"))])
async def sdk_studio_plan_agent(request: Request):
    data = await request.json()
    result = plan_studio_agent(AgentPlanRequest.from_dict(data))
    return {**result.to_dict(), "upgrade_hint": getattr(request.state, "upgrade_hint", None)}


@app.post("/sdk/v1/studio/plan-agent", dependencies=[Depends(require_sdk_api_key("sdk_studio_plan_agent"))])
async def sdk_v1_studio_plan_agent(request: Request):
    return await sdk_studio_plan_agent(request)


@app.post("/sdk/studio/generate-agent", dependencies=[Depends(require_sdk_api_key("sdk_studio_generate_agent"))])
async def sdk_studio_generate_agent(request: Request):
    data = await request.json()
    request_data = data.get("request") if isinstance(data.get("request"), dict) else data
    dry_run = bool(data.get("dry_run", True))
    entitlement = getattr(request.state, "entitlement", None)
    if not dry_run and not getattr(entitlement, "agent_factory_write_enabled", False):
        raise HTTPException(status_code=403, detail="agent_factory_write_not_enabled")
    result = run_studio_factory(AgentFactoryRequest.from_dict(request_data), dry_run=dry_run)
    return {**result.to_dict(), "upgrade_hint": getattr(request.state, "upgrade_hint", None)}


@app.post("/sdk/v1/studio/generate-agent", dependencies=[Depends(require_sdk_api_key("sdk_studio_generate_agent"))])
async def sdk_v1_studio_generate_agent(request: Request):
    return await sdk_studio_generate_agent(request)

from planner.ai_work_planner import plan_workflow


@app.post("/plan_workflow")
async def plan_workflow_api(request: Request):
    data = await request.json()
    user_prompt = data.get("prompt", "")
    workflow_id = data.get("workflow_id", "manual_plan")
    structured_spec_final = data.get("structured_spec_final")
    user_id = data.get("user_id") or data.get("uid") or "anonymous"
    output_mode = data.get("workflow_mode") or data.get("output_mode") or "serial"

    plan = await plan_workflow(
       user_prompt,
       structured_spec_final=structured_spec_final,
       user_id=user_id,
       output_mode=output_mode,
    )
    return {"status": "ok", "plan": plan}


# ==========================================================
#  🔥 Memory-Aware Planner + Spec Analyzer Integration
# ==========================================================

from utils.spec_analyzer import analyze_spec_text
from utils.domain_classifier import detect_domain
from analyze.digital.analyze_spec_digital import analyze_spec_digital, finalize_spec_digital


# backend/main.py  (inside /analyze_spec)
from utils.domain_classifier import detect_domain
from utils.spec_analyzer import analyze_spec_text  # your existing analyzer
from analyze.digital.analyze_spec_digital import analyze_spec_digital, finalize_spec_digital

@app.post("/analyze_spec")
async def analyze_spec(request: Request):
    data = await request.json()
    goal = data.get("goal", "")
    voice_summary = data.get("voice_summary", "")
    user_id = data.get("user_id", "anonymous")

    combined = (voice_summary or "") + "\n" + (goal or "")
    domain_info = await detect_domain(combined)
    domain = (domain_info.get("domain") or "mixed").lower()
    conf = domain_info.get("confidence") or {}
    max_conf = max(conf.values()) if conf else 0.0

    logger = logging.getLogger(__name__)

    # after domain_info = await detect_domain(...)

    logger.info("AnalyzeSpec: domain=%s conf=%s reason=%s",
            domain_info.get("domain"),
            domain_info.get("confidence"),
            domain_info.get("reason"))


    if domain == "digital" and max_conf >= 0.70:
        result = await analyze_spec_digital(goal, voice_summary, user_id)
    else:
        # keep current behavior for analog/embedded/mixed for now
        result = await analyze_spec_text(goal, voice_summary, user_id)

    return {
        "domain_info": domain_info,
        "result": result
    }


# ---------- /plan_workflow_memory ----------
@app.post("/plan_workflow_memory")
async def plan_workflow_memory(request: Request):
    """
    Plans workflow using memory from user_memory and agent_memory.
    """
    data = await request.json()
    goal = data.get("goal", "")
    user_id = data.get("user_id", "anonymous")

    # Retrieve memory context
    user_mem = supabase.table("user_memory").select("*").eq("user_id", user_id).execute()
    user_data = user_mem.data[0] if user_mem.data else {}
    agent_mem = supabase.table("agent_memory").select("*").execute()
    agent_data = agent_mem.data or []

    memory_context = f"""
    Recent goals: {user_data.get('recent_goals', [])}
    Frequent agents: {user_data.get('frequent_agents', [])}
    Known agent types: {[a['agent_name'] for a in agent_data[:8]]}
    """

    planner_prompt = f"""
    You are ChipLoop's Memory-Aware Planner.
    Use the memory below to propose a workflow for goal: "{goal}"

    Memory Context:
    {memory_context}

    Return only JSON:
    {{
      "loop_type": "<digital|analog|embedded|system|validation>",
      "agents": ["Agent A", "Agent B", ...]
    }}
    """

    try:
        response = await run_llm_fallback(planner_prompt)
        plan = json.loads(response)

        # Update user memory (append goal + agents)
        supabase.table("user_memory").upsert({
            "user_id": user_id,
            "recent_goals": (user_data.get("recent_goals", []) + [goal])[-5:],
            "frequent_agents": list(set(user_data.get("frequent_agents", []) + plan.get("agents", []))),
            "updated_at": datetime.utcnow().isoformat(),
        }).execute()

        return {"status": "ok", "plan": plan}

    except Exception as e:
        logger.error(f"❌ Memory planner failed: {e}")
        return {"status": "error", "message": str(e)}


# ---------- Memory Update Helper for Auto-Compose ----------
def update_agent_memory(agent_name: str, workflow_name: str):
    """Update agent usage and last_used_in list."""
    try:
        record = {
            "agent_name": agent_name,
            "last_used_in": [workflow_name],
            "updated_at": datetime.utcnow().isoformat()
        }
        supabase.table("agent_memory").upsert(record).execute()
    except Exception as e:
        logger.warning(f"⚠️ Failed to update agent memory for {agent_name}: {e}")

def fetch_memory_context(user_id: str):
    try:
        user_mem = supabase.table("user_memory").select("*").eq("user_id", user_id).execute()
        user_data = user_mem.data[0] if user_mem.data else {}
        agent_mem = supabase.table("agent_memory").select("agent_name, capability_tags").execute()
        agent_data = agent_mem.data or []

        return {
            "recent_goals": user_data.get("recent_goals", []),
            "frequent_agents": user_data.get("frequent_agents", []),
            "known_agents": [a["agent_name"] for a in agent_data],
        }
    except Exception as e:
        logger.warning(f"⚠️ fetch_memory_context failed: {e}")
        return {"recent_goals": [], "frequent_agents": [], "known_agents": []}


from planner.ai_agent_planner import plan_agent_with_memory

@app.post("/plan_agent")
async def plan_agent(request: Request):
    """
    Agentic Agent Planner:
    - Analyzes spec
    - Fetches user + agent memory
    - Returns proposed agent JSON
    """
    data = await request.json()
    goal = data.get("goal", "")
    user_id = data.get("user_id", "anonymous")

    from planner.ai_agent_planner import plan_agent_with_memory
    result = await plan_agent_with_memory(goal, user_id)
    return {"status": "ok", "agent_plan": result}

@app.post("/save_custom_agent")
async def save_custom_agent(request: Request):
    """
    Save AI-generated agent from planner into Supabase `agents` table.
    """
    try:
        data = await request.json()
        agent = data.get("agent", {})
        body_user_id = data.get("user_id") or agent.get("owner_id") or agent.get("user_id")
        header_user_id = (
            request.headers.get("x-user-id")
            or request.headers.get("x-supabase-user-id")
            or request.headers.get("x-client-user-id")
        )
        jwt_user_id = None
        try:
            token_data = verify_token(request)
            jwt_user_id = token_data.get("sub")
        except Exception:
            pass
        owner_id = header_user_id or body_user_id or jwt_user_id
        if owner_id == "anonymous":
            owner_id = None
        if not agent.get("agent_name"):
            return {"status": "error", "message": "agent_name missing"}

        record = {
            "agent_name": agent.get("agent_name"),
            "loop_type": agent.get("domain", "system"),
            "tool": "AI Agent Planner",
            "script_path": None,
            "description": agent.get("description", ""),
            "is_custom": True,
            "is_prebuilt": False,
            "is_marketplace": False,
            "owner_id": owner_id,
        }

        res = supabase.table("agents").insert(record).execute()
        if hasattr(res, "data") and res.data:
            logger.info(f"💾 Saved new custom agent: {agent.get('agent_name')}")
            return {"status": "ok", "data": res.data}
        else:
            logger.warning(f"⚠️ Supabase returned empty response for {agent.get('agent_name')}")
            return {"status": "warning", "message": "Insert may not have completed"}

    except Exception as e:
        logger.error(f"❌ Failed to save custom agent: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/save_custom_workflow")
async def save_custom_workflow(request: Request):
    """
    Save a workflow (manual or AI-created) into Supabase.
    Automatically adds 'System Workflow Agent' if workflow spans multiple domains.
    Stores nodes, edges, goal, and summary for Canvas restore and dashboard preview.
    """
    try:
        data = await request.json()
        print("\n\n========== DEBUG SAVE_CUSTOM_WORKFLOW ==========")
        print("RAW data keys:", list(data.keys()))
        print("RAW data:", data)
        print("RAW workflow type:", type(data.get("workflow")))
        print("RAW workflow keys:", list(data.get("workflow", {}).keys()) if isinstance(data.get("workflow"), dict) else data.get("workflow"))
        print("===============================================\n\n")

    

        # Support both the nested Studio Planner payload and the older flat
        # canvas-save payload so saved workflows keep their nodes/edges.
        wf = data.get("workflow")
        if not isinstance(wf, dict):
            definitions = data.get("definitions") if isinstance(data.get("definitions"), dict) else {}
            wf = {
                "workflow_name": data.get("workflow_name") or data.get("name"),
                "name": data.get("name"),
                "goal": data.get("goal", ""),
                "summary": data.get("summary", ""),
                "loop_type": data.get("loop_type", "system"),
                "nodes": data.get("nodes") or definitions.get("nodes") or [],
                "edges": data.get("edges") or definitions.get("edges") or [],
                "user_id": data.get("user_id"),
            }
        body_user_id = data.get("user_id") or wf.get("user_id")

        # ✅ Step 2: extract from headers if body didn’t include
        headers = request.headers
        header_user_id = (
          headers.get("x-user-id")
          or headers.get("x-supabase-user-id")
          or headers.get("x-client-user-id")
        )
        # ✅ Step 3: Fallback to JWT (for logged-in Supabase users)
        jwt_user_id = None
        try:
          token_data = verify_token(request)
          jwt_user_id = token_data.get("sub")
        except Exception:
           pass

    # ✅ Step 4: Final priority (header > body > JWT > anonymous)
        auth_user_id = header_user_id or body_user_id or jwt_user_id or "anonymous"

        # ✅ Apply consistently to both data & workflow objects
        data["user_id"] = auth_user_id
        wf["user_id"] = auth_user_id
        user_id = auth_user_id
        logger.info(f"💾 Final resolved user_id={user_id}")
        
        name = (
          wf.get("workflow_name")
          or wf.get("name")
          or "Untitled Workflow"
        )

        name= str(name).strip()
        logger.info(f"💾 Normalized workflow name: '{name}' (type={type(name)})")
        
        goal = wf.get("goal", "")
        summary = wf.get("summary") or wf.get("data", {}).get("summary", "")
        loop_type = wf.get("loop_type", "system")
        nodes = wf.get("nodes") or wf.get("data", {}).get("nodes", [])
        edges = wf.get("edges") or wf.get("data", {}).get("edges", [])
        workflow_config_schema = wf.get("workflow_config_schema") if isinstance(wf.get("workflow_config_schema"), dict) else {}
        default_run_config = wf.get("default_run_config") if isinstance(wf.get("default_run_config"), dict) else {}

        logger.info(f"📦 Saving workflow: name={name}, loop_type={loop_type}, nodes={len(nodes)}, edges={len(edges)}, user={user_id}")

        for node in nodes:
            if "data" not in node and "type" in node:
                node["data"] = {"backendLabel": node["type"]}
            elif "data" in node and "backendLabel" not in node["data"]:
                node["data"]["backendLabel"] = node.get("type", "Unknown")

        # --- Domain detection for auto system agent ---
        domains = set()
        for node in nodes:
            label = node.get("data", {}).get("backendLabel", "")
            if "digital" in label.lower():
                domains.add("digital")
            elif "analog" in label.lower():
                domains.add("analog")
            elif "embedded" in label.lower():
                domains.add("embedded")
            elif "system" in label.lower():
                domains.add("system")

        # ✅ Auto-append System Workflow Agent if multiple domains exist
        #if len(domains) > 1 and not any("System Workflow Agent" in n.get("data", {}).get("backendLabel", "") for n in nodes):
        #   system_agent = {
        #       "id": f"system_validation_{len(nodes) + 1}",
        #       "type": "default",
        #       "data": {
        #           "uiLabel": "System Workflow Agent",
        #            "backendLabel": "System Workflow Agent",
        #           "description": "Validates cross-domain integration.",
        #       },
        #         "position": {"x": 400, "y": 400},
        #   }
        #   nodes.append(system_agent)
        #     logger.info(f"🧩 Auto-added System Workflow Agent to {name}")

        # --- Prepare payload for Supabase ---
        payload = {
            "user_id": user_id if user_id not in ("anonymous", None) and len(user_id) == 36 else None,
            "name": name,
            "goal": goal,
            "summary": summary or f"Workflow for {goal[:80]}",
            "loop_type": loop_type,
            "definitions": {
              "nodes": nodes,
              "edges": edges,
              "workflow_config_schema": workflow_config_schema,
              "default_run_config": default_run_config,
            },
            "status": "saved",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        # --- Insert into Supabase ---
        result = supabase.table("workflows").insert(payload).execute()
        logger.info(f"💾 Workflow save payload user_id={user_id} (len={len(user_id) if user_id else 0})")

        logger.info(f"💾 Workflow saved: {name} | domains={list(domains)} | user={user_id or 'anonymous'}")
        return {"status": "ok", "data": result.data}

    except Exception as e:
        logger.error(f"❌ Failed to save workflow: {e}")
        return {"status": "error", "message": str(e)}


def _request_user_id_optional(request: Request) -> Optional[str]:
    header_user_id = (
        request.headers.get("x-user-id")
        or request.headers.get("x-supabase-user-id")
        or request.headers.get("x-client-user-id")
    )
    if header_user_id and len(str(header_user_id)) == 36:
        return str(header_user_id)
    try:
        token_data = verify_token(request)
        sub = token_data.get("sub")
        if sub and len(str(sub)) == 36:
            return str(sub)
    except Exception:
        return None
    return None


def _normalize_reference_journey(row: Dict[str, Any]) -> Dict[str, Any]:
    definition = row.get("definition") if isinstance(row.get("definition"), dict) else {}
    return {
        "id": row.get("id"),
        "slug": row.get("slug"),
        "name": row.get("name"),
        "product_type": row.get("product_type") or "digital",
        "summary": row.get("summary") or "",
        "definition": definition,
        "stage_count": len(definition.get("stages") or []),
    }


def _reference_journey_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    slug = str(slug or "").strip()
    if not slug:
        return None
    try:
        rows = (
            supabase.table("product_reference_journeys")
            .select("id,slug,name,product_type,summary,definition,is_active")
            .eq("slug", slug)
            .eq("is_active", True)
            .limit(1)
            .execute()
        ).data or []
        if rows:
            return _normalize_reference_journey(rows[0])
    except Exception:
        pass
    for item in PRODUCT_REFERENCE_JOURNEYS:
        if item.get("slug") == slug:
            return _normalize_reference_journey(item)
    return None


@app.get("/products/reference-journeys")
async def list_product_reference_journeys():
    try:
        rows = (
            supabase.table("product_reference_journeys")
            .select("id,slug,name,product_type,summary,definition,is_active")
            .eq("is_active", True)
            .order("name")
            .execute()
        ).data or []
        if rows:
            return {"status": "ok", "reference_journeys": [_normalize_reference_journey(row) for row in rows]}
    except Exception as exc:
        logger.warning("Reference journey table unavailable, using built-in defaults: %s", exc)
    return {"status": "ok", "reference_journeys": [_normalize_reference_journey(row) for row in PRODUCT_REFERENCE_JOURNEYS]}


@app.get("/products/stage-schemas")
async def list_product_stage_schemas():
    try:
        rows = (
            supabase.table("product_stage_schemas")
            .select("app,schema,is_active")
            .eq("is_active", True)
            .execute()
        ).data or []
        if rows:
            schemas = {
                str(row.get("app")): row.get("schema")
                for row in rows
                if row.get("app") and isinstance(row.get("schema"), dict)
            }
            if schemas:
                return {"status": "ok", "stage_schemas": schemas}
    except Exception as exc:
        logger.warning("Product stage schema table unavailable, using built-in defaults: %s", exc)
    return {"status": "ok", "stage_schemas": PRODUCT_STAGE_SCHEMAS}


@app.get("/products")
async def list_products(request: Request):
    user_id = _request_user_id_optional(request)
    if not user_id:
        return {"status": "ok", "products": []}
    try:
        rows = (
            supabase.table("products")
            .select("id,name,product_type,starting_point,description,stage_config,stage_results,status,created_at,updated_at")
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(100)
            .execute()
        ).data or []
        return {"status": "ok", "products": rows}
    except Exception as exc:
        logger.error("Failed to list products: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to list products: {exc}")


@app.get("/products/{product_id}")
async def get_product(product_id: str, request: Request):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to view a product")
    try:
        rows = (
            supabase.table("products")
            .select("id,name,product_type,starting_point,description,stage_config,stage_results,status,created_at,updated_at")
            .eq("id", product_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        ).data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Product not found")
        return {"status": "ok", "product": rows[0]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get product: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to get product: {exc}")


@app.post("/products")
async def create_product(payload: ProductCreateIn, request: Request):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to create a product")
    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Product name is required")

    reference = _reference_journey_by_slug(payload.reference_slug or "") if payload.reference_slug else None
    stage_config = payload.stage_config if isinstance(payload.stage_config, dict) else {}
    if reference and not stage_config:
        stage_config = {
            "source": "reference_journey",
            "reference_slug": reference.get("slug"),
            "stages": reference.get("definition", {}).get("stages") or [],
        }

    record = {
        "user_id": user_id,
        "name": name,
        "product_type": payload.product_type or (reference or {}).get("product_type") or "digital",
        "starting_point": payload.starting_point or "from_specs",
        "description": payload.description or (reference or {}).get("summary") or "",
        "stage_config": stage_config,
        "stage_results": {},
        "status": "draft",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    try:
        rows = supabase.table("products").insert(record).execute().data or []
        product = rows[0] if rows else record
        return {"status": "ok", "product": product}
    except Exception as exc:
        logger.error("Failed to create product: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to create product: {exc}")


@app.patch("/products/{product_id}")
async def update_product(product_id: str, payload: ProductUpdateIn, request: Request):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to update a product")
    allowed_status = {"draft", "ready", "running", "completed", "failed", "archived"}
    update: Dict[str, Any] = {"updated_at": datetime.utcnow().isoformat()}
    for key in ("name", "product_type", "starting_point", "description", "stage_config"):
        value = getattr(payload, key)
        if value is not None:
            update[key] = value
    if payload.status is not None:
        if payload.status not in allowed_status:
            raise HTTPException(status_code=400, detail="Invalid product status")
        update["status"] = payload.status
    try:
        rows = (
            supabase.table("products")
            .update(update)
            .eq("id", product_id)
            .eq("user_id", user_id)
            .execute()
        ).data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Product not found")
        return {"status": "ok", "product": rows[0]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update product: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to update product: {exc}")


@app.delete("/products/{product_id}")
async def delete_product(product_id: str, request: Request):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to delete a product")
    try:
        rows = (
            supabase.table("products")
            .delete()
            .eq("id", product_id)
            .eq("user_id", user_id)
            .execute()
        ).data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Product not found")
        return {"status": "ok", "deleted_product_id": product_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to delete product: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to delete product: {exc}")


def _product_stage_enabled(stage: Dict[str, Any]) -> bool:
    if stage.get("required"):
        return True
    if stage.get("optional") and "enabled" not in stage:
        return False
    return stage.get("enabled") is not False


def _product_run_row(product_run_id: str, user_id: str) -> Dict[str, Any]:
    row = (
        supabase.table("product_runs")
        .select("id,product_id,user_id,status,current_stage,stage_results,logs,error,created_at,updated_at,completed_at")
        .eq("id", product_run_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data or []
    return row[0] if row else {}


def _product_stage_rows(product_run_id: str, user_id: str) -> List[Dict[str, Any]]:
    rows = (
        supabase.table("product_stage_runs")
        .select("id,product_run_id,product_id,stage_id,stage_label,app,status,workflow_id,run_id,inputs,outputs,error,started_at,completed_at,created_at,updated_at")
        .eq("product_run_id", product_run_id)
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    ).data or []
    return _enrich_product_stage_rows(rows if isinstance(rows, list) else [])


def _count_workflow_agents_from_logs(logs: Optional[str]) -> Optional[int]:
    if not logs:
        return None
    agents: set[str] = set()
    for raw_line in str(logs).splitlines():
        line = raw_line.strip()
        running = re.search(r"Running\s+(.+?\sAgent)\b", line, flags=re.IGNORECASE)
        finished = re.search(r"^(.+?\sAgent)\s+(?:done|failed)\b", line, flags=re.IGNORECASE)
        name = (running.group(1) if running else None) or (finished.group(1) if finished else None)
        if name:
            agents.add(name.strip())
    return len(agents) if agents else None


def _enrich_product_stage_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    workflow_ids = sorted({
        str(row.get("workflow_id"))
        for row in rows
        if row.get("workflow_id")
    })
    if not workflow_ids:
        return rows
    logs_by_workflow: Dict[str, str] = {}
    try:
        workflow_rows = (
            supabase.table("workflows")
            .select("id,logs")
            .in_("id", workflow_ids)
            .execute()
        ).data or []
        logs_by_workflow = {
            str(row.get("id")): str(row.get("logs") or "")
            for row in workflow_rows
            if row.get("id")
        }
    except Exception as exc:
        logger.warning("Failed to enrich product stage agent counts: %s", exc)
        return rows
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        next_row = dict(row)
        outputs = next_row.get("outputs") if isinstance(next_row.get("outputs"), dict) else {}
        if "agent_count" not in outputs:
            agent_count = _count_workflow_agents_from_logs(logs_by_workflow.get(str(next_row.get("workflow_id"))))
            if agent_count is not None:
                outputs = {**outputs, "agent_count": agent_count}
        next_row["outputs"] = outputs
        enriched.append(next_row)
    return enriched


def _update_product_run(product_run_id: str, updates: Dict[str, Any]) -> None:
    updates = {**updates, "updated_at": datetime.utcnow().isoformat()}
    try:
        supabase.table("product_runs").update(updates).eq("id", product_run_id).execute()
    except Exception as exc:
        logger.warning("Failed to update product_run %s: %s", product_run_id, exc)


def _append_product_run_log(product_run_id: str, line: str) -> None:
    safe_line = str(line or "").strip()
    if not safe_line:
        return
    try:
        rows = supabase.table("product_runs").select("logs").eq("id", product_run_id).limit(1).execute().data or []
        current = str((rows[0] if rows else {}).get("logs") or "")
        timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        next_logs = (current + ("\n" if current else "") + f"[{timestamp}] {safe_line}").strip()
        next_logs = _truncate_tail(next_logs, MAX_LOG_CHARS)
        supabase.table("product_runs").update({
            "logs": next_logs,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", product_run_id).execute()
    except Exception as exc:
        logger.warning("Failed to append product_run log %s: %s", product_run_id, exc)


def _update_product_stage_run(stage_run_id: str, updates: Dict[str, Any]) -> None:
    updates = {**updates, "updated_at": datetime.utcnow().isoformat()}
    try:
        supabase.table("product_stage_runs").update(updates).eq("id", stage_run_id).execute()
    except Exception as exc:
        logger.warning("Failed to update product_stage_run %s: %s", stage_run_id, exc)


def _update_product_status(product_id: str, user_id: str, status: str, stage_results: Optional[Dict[str, Any]] = None) -> None:
    updates: Dict[str, Any] = {
        "status": status,
        "updated_at": datetime.utcnow().isoformat(),
    }
    if stage_results is not None:
        updates["stage_results"] = stage_results
    try:
        supabase.table("products").update(updates).eq("id", product_id).eq("user_id", user_id).execute()
    except Exception as exc:
        logger.warning("Failed to update product %s status: %s", product_id, exc)


def _product_run_cancelled(product_run_id: str, user_id: str) -> bool:
    try:
        rows = (
            supabase.table("product_runs")
            .select("status")
            .eq("id", product_run_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        ).data or []
        return str((rows[0] if rows else {}).get("status") or "") == "cancelled"
    except Exception as exc:
        logger.warning("Failed to check product_run cancellation %s: %s", product_run_id, exc)
        return False


def _workflow_status(workflow_id: str) -> str:
    try:
        rows = supabase.table("workflows").select("status").eq("id", workflow_id).limit(1).execute().data or []
        return str((rows[0] if rows else {}).get("status") or "")
    except Exception:
        return ""


def _stage_setting(stage: Dict[str, Any], key: str, default: Any = None) -> Any:
    settings = stage.get("settings") if isinstance(stage.get("settings"), dict) else {}
    value = settings.get(key, default)
    return default if value is None else value


def _product_upstream_key_for_app(app_name: str) -> Optional[str]:
    return {
        "Digital_Arch2RTL": "arch2rtl",
        "Digital_DQA": "dqa",
        "Digital_Verify": "verify",
        "Digital_Arch2Synthesis": "arch2synthesis",
        "Digital_Arch2Tapeout": "tapeout",
        "verify_closure_loop": "closure",
        "Embedded_Run": "firmware",
        "System_Software": "software",
        "System_Software_Validation_L2": "validation",
        "System_Product_App_Builder": "product_app",
        "System_RTL": "system_rtl",
        "System_DQA": "system_dqa",
        "System_Sim": "system_sim",
        "System_Synthesis": "system_synthesis",
        "System_Firmware": "system_firmware",
        "System_PD": "system_pd",
    }.get(app_name)


def _seed_product_upstream_from_prior_run(
    resume_product_run_id: Optional[str],
    product_id: str,
    user_id: str,
    start_stage: Optional[str],
) -> Dict[str, str]:
    if not resume_product_run_id or not start_stage:
        return {}
    try:
        prior = _product_run_row(str(resume_product_run_id), user_id)
        if not prior or str(prior.get("product_id")) != str(product_id):
            return {}
        upstream: Dict[str, str] = {}
        normalized_start = str(start_stage).strip()
        for row in _product_stage_rows(str(resume_product_run_id), user_id):
            if str(row.get("stage_id") or "") == normalized_start or str(row.get("app") or "") == normalized_start:
                break
            if str(row.get("status") or "") != "completed" or not row.get("workflow_id"):
                continue
            key = _product_upstream_key_for_app(str(row.get("app") or ""))
            if key:
                upstream[key] = str(row.get("workflow_id"))
        return upstream
    except Exception as exc:
        logger.warning("Failed to seed product upstream from prior run %s: %s", resume_product_run_id, exc)
        return {}


def _product_stage_payload(product: Dict[str, Any], stage: Dict[str, Any], upstream: Dict[str, str]) -> Dict[str, Any]:
    app_name = str(stage.get("app") or "")
    product_name = str(product.get("name") or "product")
    description = str(product.get("description") or "")
    if app_name == "Digital_Arch2RTL":
        top_module = str(_stage_setting(stage, "top_module", "") or "").strip()
        if not top_module:
            top_module = re.sub(r"[^a-zA-Z0-9_]+", "_", product_name.strip().lower()).strip("_") or "product_top"
        spec_text = str(_stage_setting(stage, "spec_text", "") or "").strip() or description
        if not spec_text.strip():
            raise RuntimeError("Arch2RTL requires a spec_text setting or product description before running.")
        return {
            "project_name": str(_stage_setting(stage, "project_name", "") or "").strip() or product_name,
            "top_module": top_module,
            "design_language": str(_stage_setting(stage, "design_language", "systemverilog") or "systemverilog"),
            "spec_text": spec_text,
            "throughput_latency_targets": str(_stage_setting(stage, "throughput_latency_targets", "") or ""),
            "power_priority": str(_stage_setting(stage, "power_priority", "") or ""),
            "toggles": {
                "gen_regmap": bool(_stage_setting(stage, "enable_regmap", True)),
                "gen_upf_lite": bool(_stage_setting(stage, "enable_upf_lite", False)),
                "gen_packaging": bool(_stage_setting(stage, "enable_packaging", True)),
                "enable_scan_dft": bool(_stage_setting(stage, "enable_scan_dft", False)),
                "insert_mbist": bool(_stage_setting(stage, "insert_mbist", False)),
                "mbist_algorithm": str(_stage_setting(stage, "mbist_algorithm", "march-c") or "march-c"),
                "run_spec2rtl_check": bool(_stage_setting(stage, "run_spec2rtl_check", False)),
            },
        }
    if app_name == "Digital_DQA":
        arch2rtl_id = upstream.get("arch2rtl")
        if not arch2rtl_id:
            raise RuntimeError("DQA requires Arch2RTL workflow output.")
        return {
            "rtl_source_mode": "from_arch2rtl",
            "from_workflow_id": arch2rtl_id,
            "source_arch2rtl_workflow_id": arch2rtl_id,
            "upstream_workflows": {"arch2rtl": arch2rtl_id},
            "target": str(_stage_setting(stage, "target", "pre_verify_quality_gate")),
            "lint_profile": str(_stage_setting(stage, "lint_profile", "default")),
            "cdc_profile": str(_stage_setting(stage, "cdc_profile", "default")),
            "toggles": {
                "enable_autofix": bool(_stage_setting(stage, "enable_autofix", False)),
                "run_lint": bool(_stage_setting(stage, "run_lint", True)),
                "run_cdc": bool(_stage_setting(stage, "run_cdc", True)),
                "run_reset": bool(_stage_setting(stage, "run_reset", True)),
                "run_synthesis_readiness": bool(_stage_setting(stage, "run_synthesis_readiness", True)),
            },
        }
    if app_name == "Digital_Verify":
        arch2rtl_id = upstream.get("arch2rtl")
        if not arch2rtl_id:
            raise RuntimeError("Verify requires Arch2RTL workflow output.")
        formal_tool = str(_stage_setting(stage, "formal_tool", "none") or "none")
        golden_model_tool = str(_stage_setting(stage, "golden_model_tool", "none") or "none")
        failure_debug_enabled = bool(_stage_setting(stage, "enable_failure_debug", False))
        return {
            "rtl_source_mode": "from_arch2rtl",
            "from_workflow_id": arch2rtl_id,
            "source_arch2rtl_workflow_id": arch2rtl_id,
            "parent_workflow_id": upstream.get("dqa"),
            "upstream_workflows": {key: value for key, value in upstream.items() if value},
            "test_intent": str(_stage_setting(stage, "test_intent", "Run smoke, reset, register access, and representative functional tests.")),
            "verification_plan": str(_stage_setting(stage, "verification_plan", "") or ""),
            "monitor_checker_plan": str(_stage_setting(stage, "monitor_checker_plan", "") or ""),
            "coverage_targets": str(_stage_setting(stage, "coverage_targets", "90% functional, 70% line")),
            "coverage_plan": str(_stage_setting(stage, "coverage_plan", "") or ""),
            "seed_count": int(_stage_setting(stage, "seed_count", 4) or 4),
            "random_vs_directed": str(_stage_setting(stage, "random_vs_directed", "both")),
            "simulator_type": str(_stage_setting(stage, "simulator_type", "verilator")),
            "toolchain": {
                "simulator": str(_stage_setting(stage, "simulator_type", "verilator")),
                "code_coverage": str(_stage_setting(stage, "code_coverage_tool", "verilator_coverage")),
                "formal": formal_tool,
                "formal_solver": str(_stage_setting(stage, "formal_solver", "z3")),
                "golden_model": golden_model_tool,
            },
            "run_closure_analysis": bool(_stage_setting(stage, "run_closure_analysis", True)) or failure_debug_enabled,
            "enable_failure_debug": failure_debug_enabled,
            "failure_debug_options": {
                "enabled": failure_debug_enabled,
                "log_only_first": bool(_stage_setting(stage, "failure_debug_log_only_first", True)),
                "generate_vcd": bool(_stage_setting(stage, "failure_debug_generate_vcd", True)),
                "auto_apply_testbench_fixes": bool(_stage_setting(stage, "failure_debug_auto_apply_tb", False)),
                "auto_apply_rtl_fixes": bool(_stage_setting(stage, "failure_debug_auto_apply_rtl", False)),
                "rerun_failing_tests": bool(_stage_setting(stage, "failure_debug_rerun_failing", True)),
            },
            "toggles": {
                "enable_formal": formal_tool != "none",
                "enable_golden_model": golden_model_tool != "none",
            },
        }
    if app_name == "Digital_Arch2Synthesis":
        arch2rtl_id = upstream.get("arch2rtl")
        if not arch2rtl_id:
            raise RuntimeError("Arch2Synthesis requires Arch2RTL workflow output.")
        return {
            "rtl_source_mode": "from_arch2rtl",
            "from_workflow_id": arch2rtl_id,
            "source_arch2rtl_workflow_id": arch2rtl_id,
            "parent_workflow_id": upstream.get("verify") or upstream.get("dqa"),
            "upstream_workflows": {key: value for key, value in upstream.items() if value},
            "project_name": product_name,
            "top_module": str(_stage_setting(stage, "top_module", "") or "").strip() or None,
            "foundry": str(_stage_setting(stage, "foundry", "sky130")),
            "pdk": str(_stage_setting(stage, "pdk", "sky130A")),
            "toolchain": str(_stage_setting(stage, "toolchain", "openlane2")),
            "target_frequency_mhz": float(_stage_setting(stage, "target_frequency_mhz", 100) or 100),
            "constraints_sdc": str(_stage_setting(stage, "constraints_sdc", "") or ""),
            "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
            "max_synthesis_closure_iterations": int(_stage_setting(stage, "max_synthesis_closure_iterations", 1) or 1),
            "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
            "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
            "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
            "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
            "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
            "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            "toggles": {
                "run_logic_equivalence": bool(_stage_setting(stage, "run_logic_equivalence", True)),
                "run_synthesis_readiness": bool(_stage_setting(stage, "run_synthesis_readiness", True)),
                "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
                "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
                "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
                "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
                "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
                "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
                "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            },
            "start_stage": "synth",
            "stop_stage": "synth",
        }
    if app_name == "Digital_Arch2Tapeout":
        arch2rtl_id = upstream.get("arch2rtl")
        if not arch2rtl_id:
            raise RuntimeError("Arch2Tapeout requires Arch2RTL workflow output.")
        return {
            "rtl_source_mode": "from_arch2rtl",
            "from_workflow_id": arch2rtl_id,
            "source_arch2rtl_workflow_id": arch2rtl_id,
            "parent_workflow_id": upstream.get("arch2synthesis") or upstream.get("verify") or upstream.get("dqa"),
            "upstream_workflows": {key: value for key, value in upstream.items() if value},
            "project_name": product_name,
            "top_module": str(_stage_setting(stage, "top_module", "") or "").strip() or None,
            "foundry": str(_stage_setting(stage, "foundry", "sky130")),
            "pdk": str(_stage_setting(stage, "pdk", "sky130A")),
            "toolchain": str(_stage_setting(stage, "toolchain", "openlane2")),
            "target_frequency_mhz": float(_stage_setting(stage, "target_frequency_mhz", 100) or 100),
            "constraints_sdc": str(_stage_setting(stage, "constraints_sdc", "") or ""),
            "effort": str(_stage_setting(stage, "effort", "balanced")),
            "run_fill": bool(_stage_setting(stage, "run_fill", True)),
            "run_drc": bool(_stage_setting(stage, "run_drc", True)),
            "run_lvs": bool(_stage_setting(stage, "run_lvs", True)),
            "run_signoff_closure_loop": bool(_stage_setting(stage, "run_signoff_closure_loop", False)),
            "max_signoff_closure_iterations": int(_stage_setting(stage, "max_signoff_closure_iterations", 1) or 1),
            "allow_timing_repair": bool(_stage_setting(stage, "allow_timing_repair", True)),
            "allow_drc_repair": bool(_stage_setting(stage, "allow_drc_repair", True)),
            "allow_lvs_repair": bool(_stage_setting(stage, "allow_lvs_repair", True)),
            "allow_lec_repair": bool(_stage_setting(stage, "allow_lec_repair", True)),
            "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
            "max_synthesis_closure_iterations": int(_stage_setting(stage, "max_synthesis_closure_iterations", 1) or 1),
            "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
            "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
            "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
            "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
            "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
            "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            "toggles": {
                "run_drc": bool(_stage_setting(stage, "run_drc", True)),
                "run_lvs": bool(_stage_setting(stage, "run_lvs", True)),
                "run_fill": bool(_stage_setting(stage, "run_fill", True)),
                "run_logic_equivalence": bool(_stage_setting(stage, "run_logic_equivalence", True)),
                "run_signoff_closure_loop": bool(_stage_setting(stage, "run_signoff_closure_loop", False)),
                "allow_timing_repair": bool(_stage_setting(stage, "allow_timing_repair", True)),
                "allow_drc_repair": bool(_stage_setting(stage, "allow_drc_repair", True)),
                "allow_lvs_repair": bool(_stage_setting(stage, "allow_lvs_repair", True)),
                "allow_lec_repair": bool(_stage_setting(stage, "allow_lec_repair", True)),
                "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
                "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
                "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
                "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
                "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
                "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
                "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            },
            "start_stage": "synth",
            "stop_stage": "tapeout",
        }
    if app_name == "verify_closure_loop":
        verify_id = upstream.get("verify")
        if not verify_id:
            raise RuntimeError("Closure loop requires Verify workflow output.")
        return {
            "source_verify_workflow_id": verify_id,
            "coverage_targets": str(_stage_setting(stage, "coverage_targets", "90% functional, 70% line")),
            "seed_count": int(_stage_setting(stage, "seed_count", 5) or 5),
            "seed_budget": int(_stage_setting(stage, "seed_budget", _stage_setting(stage, "seed_count", 5)) or 5),
            "max_iterations": int(_stage_setting(stage, "max_iterations", 1) or 1),
            "rerun_mode": str(_stage_setting(stage, "rerun_mode", "coverage_targeted")),
            "random_vs_directed": str(_stage_setting(stage, "random_vs_directed", "both")),
            "enable_failure_debug": bool(_stage_setting(stage, "enable_failure_debug", False)),
            "failure_debug_options": {
                "enabled": bool(_stage_setting(stage, "enable_failure_debug", False)),
                "log_only_first": bool(_stage_setting(stage, "failure_debug_log_only_first", True)),
                "generate_vcd": bool(_stage_setting(stage, "failure_debug_generate_vcd", True)),
                "auto_apply_testbench_fixes": bool(_stage_setting(stage, "failure_debug_auto_apply_tb", False)),
                "auto_apply_rtl_fixes": bool(_stage_setting(stage, "failure_debug_auto_apply_rtl", False)),
                "rerun_failing_tests": bool(_stage_setting(stage, "failure_debug_rerun_failing", True)),
            },
            "toolchain": {
                "simulator": str(_stage_setting(stage, "simulator_type", "verilator")),
                "code_coverage": str(_stage_setting(stage, "code_coverage_tool", "verilator_coverage")),
                "formal": str(_stage_setting(stage, "formal_tool", "none") or "none"),
                "formal_solver": str(_stage_setting(stage, "formal_solver", "z3")),
                "golden_model": str(_stage_setting(stage, "golden_model_tool", "none") or "none"),
            },
        }
    if app_name == "Embedded_Run":
        arch2rtl_id = upstream.get("arch2rtl")
        verify_id = upstream.get("verify")
        if not arch2rtl_id:
            raise RuntimeError("Embedded firmware requires Arch2RTL workflow output.")
        return {
            "spec_text": str(_stage_setting(stage, "spec_text", "") or product.get("description") or "Generate firmware from the RTL register map and verified hardware handoff."),
            "goal": str(_stage_setting(stage, "goal", "Generate firmware HAL, register layer, driver scaffold, and validation collateral.")),
            "toolchain": {"language": str(_stage_setting(stage, "firmware_language", "rust"))},
            "toggles": {"enable_cosim": bool(_stage_setting(stage, "enable_cosim", False))},
            "from_workflow_id": arch2rtl_id,
            "source_verification_workflow_id": verify_id,
        }
    if app_name == "System_Software":
        firmware_id = upstream.get("firmware") or upstream.get("system_firmware")
        arch2rtl_id = upstream.get("arch2rtl") or upstream.get("system_rtl")
        if not firmware_id:
            raise RuntimeError("System Software requires Firmware workflow output.")
        return {
            "project_name": f"{product_name.lower().replace(' ', '_')}_software",
            "system_firmware_workflow_id": firmware_id,
            "system_rtl_workflow_id": arch2rtl_id,
            "software_goal": str(_stage_setting(stage, "software_goal", f"Build software APIs and services for {product_name}.")),
            "app_names": [name.strip() for name in str(_stage_setting(stage, "app_names", "status_cli, product_service")).split(",") if name.strip()],
            "target_language": str(_stage_setting(stage, "target_language", "rust")),
            "sdk_style": str(_stage_setting(stage, "sdk_style", "rust_crate")),
            "build_system": str(_stage_setting(stage, "build_system", "cargo")),
            "notes": "Product run handoff from generated RTL/firmware stages.",
        }
    if app_name == "System_Software_Validation_L2":
        software_id = upstream.get("software")
        firmware_id = upstream.get("firmware") or upstream.get("system_firmware")
        arch2rtl_id = upstream.get("arch2rtl") or upstream.get("system_rtl")
        if not software_id:
            raise RuntimeError("Validation requires System Software workflow output.")
        return {
            "system_software_workflow_id": software_id,
            "validation_mode": str(_stage_setting(stage, "validation_mode", "full_co_simulation")),
            "system_firmware_workflow_id": firmware_id,
            "system_rtl_workflow_id": arch2rtl_id,
            "goal": str(_stage_setting(stage, "goal", f"Validate {product_name} full stack from software controls through generated RTL behavior.")),
            "notes": "Product run validation stage.",
        }
    if app_name == "System_Product_App_Builder":
        software_id = upstream.get("software")
        validation_id = upstream.get("validation")
        if not software_id or not validation_id:
            raise RuntimeError("Product App requires Software and Validation workflow outputs.")
        return {
            "arch2rtl_workflow_id": upstream.get("arch2rtl") or upstream.get("system_rtl"),
            "verify_workflow_id": upstream.get("verify"),
            "system_firmware_workflow_id": upstream.get("firmware") or upstream.get("system_firmware"),
            "system_software_workflow_id": software_id,
            "system_validation_workflow_id": validation_id,
            "product_intent": str(_stage_setting(stage, "product_intent", product.get("description") or f"Build a simulator-backed product dashboard for {product_name}.")),
            "app_type": str(_stage_setting(stage, "app_type", "web_dashboard")),
            "target_runtime": str(_stage_setting(stage, "target_runtime", "simulated_device")),
        }
    if app_name == "System_RTL":
        digital_spec = str(_stage_setting(stage, "digital_spec", "") or "").strip()
        analog_spec = str(_stage_setting(stage, "analog_spec", "") or "").strip()
        soc_spec = str(_stage_setting(stage, "soc_spec", "") or "").strip()
        missing = [name for name, value in (("digital_spec", digital_spec), ("analog_spec", analog_spec), ("soc_spec", soc_spec)) if not value]
        if missing:
            raise RuntimeError(f"System RTL requires configured settings before run: {', '.join(missing)}")
        return {
            "project_name": product_name,
            "top_module": str(_stage_setting(stage, "top_module", "") or "").strip() or None,
            "digital_spec_text": digital_spec,
            "analog_spec_text": analog_spec,
            "soc_integration_spec_text": soc_spec,
            "toggles": {
                "run_spec2rtl_check": bool(_stage_setting(stage, "enable_spec2rtl", True)),
            },
        }
    if app_name == "System_DQA":
        system_rtl_id = upstream.get("system_rtl")
        if not system_rtl_id:
            raise RuntimeError("System DQA requires System RTL workflow output.")
        return {
            "rtl_source_mode": "from_system_rtl",
            "system_rtl_workflow_id": system_rtl_id,
            "from_workflow_id": system_rtl_id,
            "source_system_rtl_workflow_id": system_rtl_id,
            "toggles": {
                "run_lint": bool(_stage_setting(stage, "run_lint", True)),
                "run_cdc": bool(_stage_setting(stage, "run_cdc", True)),
                "run_reset": bool(_stage_setting(stage, "run_reset", True)),
                "run_synthesis_readiness": bool(_stage_setting(stage, "run_synthesis_readiness", True)),
            },
        }
    if app_name == "System_Sim":
        system_rtl_id = upstream.get("system_rtl")
        if not system_rtl_id:
            raise RuntimeError("System Sim requires System RTL workflow output.")
        seeds_text = str(_stage_setting(stage, "system_sim_seeds", "1,2,3,4") or "1,2,3,4")
        seeds = [int(part.strip()) for part in seeds_text.split(",") if part.strip().isdigit()]
        testcases_text = str(_stage_setting(stage, "system_sim_testcases", "system_smoke_test, integrated_input_sanity") or "")
        formal_default = "symbiyosys" if bool(_stage_setting(stage, "enable_formal", False)) else "none"
        golden_default = "chiploop_python_scoreboard" if bool(_stage_setting(stage, "enable_golden_model", False)) else "none"
        formal_tool = str(_stage_setting(stage, "formal_tool", formal_default) or formal_default)
        golden_model_tool = str(_stage_setting(stage, "golden_model_tool", golden_default) or golden_default)
        return {
            "rtl_source_mode": "from_system_rtl",
            "system_rtl_workflow_id": system_rtl_id,
            "from_workflow_id": system_rtl_id,
            "test_intent": str(_stage_setting(stage, "test_intent", "Run integrated system smoke and register-path scenarios.")),
            "coverage_targets": str(_stage_setting(stage, "coverage_targets", "90% functional")),
            "verification_plan": str(_stage_setting(stage, "verification_plan", "") or ""),
            "monitor_checker_plan": str(_stage_setting(stage, "monitor_checker_plan", "") or ""),
            "coverage_plan": str(_stage_setting(stage, "coverage_plan", "") or ""),
            "random_vs_directed": str(_stage_setting(stage, "random_vs_directed", "both")),
            "simulator_type": str(_stage_setting(stage, "simulator_type", "verilator")),
            "seed_count": len(seeds) or int(_stage_setting(stage, "seed_count", 4) or 4),
            "system_sim_testcases": [item.strip() for item in testcases_text.split(",") if item.strip()],
            "system_sim_seeds": seeds or [1, 2, 3, 4],
            "system_sim_num_iters": int(_stage_setting(stage, "system_sim_num_iters", 25) or 25),
            "toolchain": {
                "simulator": str(_stage_setting(stage, "simulator_type", "verilator")),
                "code_coverage": str(_stage_setting(stage, "code_coverage_tool", "verilator_coverage")),
                "formal": formal_tool,
                "formal_solver": str(_stage_setting(stage, "formal_solver", "z3")),
                "golden_model": golden_model_tool,
            },
            "toggles": {
                "enable_formal": formal_tool != "none",
                "enable_golden_model": golden_model_tool != "none",
            },
            "run_closure_analysis": bool(_stage_setting(stage, "run_closure_analysis", False)) or bool(_stage_setting(stage, "enable_failure_debug", False)),
            "enable_failure_debug": bool(_stage_setting(stage, "enable_failure_debug", False)),
            "failure_debug_options": {
                "enabled": bool(_stage_setting(stage, "enable_failure_debug", False)),
                "log_only_first": bool(_stage_setting(stage, "failure_debug_log_only_first", True)),
                "generate_vcd": bool(_stage_setting(stage, "failure_debug_generate_vcd", True)),
                "generate_vcd_if_inconclusive": bool(_stage_setting(stage, "failure_debug_generate_vcd", True)),
                "auto_apply_testbench_fixes": bool(_stage_setting(stage, "failure_debug_auto_apply_tb", False)),
                "auto_apply_rtl_fixes": bool(_stage_setting(stage, "failure_debug_auto_apply_rtl", False)),
                "rerun_failing_tests": bool(_stage_setting(stage, "failure_debug_rerun_failing", True)),
            },
        }
    if app_name == "System_Firmware":
        system_rtl_id = upstream.get("system_rtl")
        if not system_rtl_id:
            raise RuntimeError("System Firmware requires System RTL workflow output.")
        return {
            "rtl_source_mode": "from_system_rtl",
            "system_rtl_workflow_id": system_rtl_id,
            "from_workflow_id": system_rtl_id,
            "toolchain": {"language": str(_stage_setting(stage, "firmware_language", "rust"))},
            "toggles": {
                "enable_cosim": bool(_stage_setting(stage, "enable_cosim", True)),
                "validate_registers": bool(_stage_setting(stage, "validate_registers", True)),
            },
        }
    if app_name == "System_Synthesis":
        system_rtl_id = upstream.get("system_rtl")
        if not system_rtl_id:
            raise RuntimeError("System Synthesis requires System RTL workflow output.")
        return {
            "rtl_source_mode": "from_system_rtl",
            "system_rtl_workflow_id": system_rtl_id,
            "from_workflow_id": system_rtl_id,
            "foundry": str(_stage_setting(stage, "foundry", "sky130")),
            "pdk": str(_stage_setting(stage, "pdk", "sky130A")),
            "toolchain": str(_stage_setting(stage, "toolchain", "openlane2")),
            "target_frequency_mhz": float(_stage_setting(stage, "target_frequency_mhz", 100) or 100),
            "constraints_sdc": str(_stage_setting(stage, "constraints_sdc", "") or ""),
            "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
            "max_synthesis_closure_iterations": int(_stage_setting(stage, "max_synthesis_closure_iterations", 1) or 1),
            "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
            "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
            "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
            "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
            "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
            "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            "synthesis_closure": {
                "enabled": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
                "max_iterations": int(_stage_setting(stage, "max_synthesis_closure_iterations", 1) or 1),
                "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
                "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
                "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
                "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
                "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
                "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            },
            "toggles": {
                "run_spec2rtl_check": bool(_stage_setting(stage, "run_spec2rtl_check", False)),
                "enable_scan_dft": bool(_stage_setting(stage, "enable_scan_dft", False)),
                "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
                "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
                "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
                "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
                "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
                "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
                "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            },
        }
    if app_name == "System_PD":
        system_rtl_id = upstream.get("system_rtl")
        if not system_rtl_id:
            raise RuntimeError("System PD requires System RTL workflow output.")
        analog_physical_mode = str(_stage_setting(stage, "analog_physical_mode", "blackbox"))
        return {
            "rtl_source_mode": "from_system_rtl",
            "system_rtl_workflow_id": system_rtl_id,
            "from_workflow_id": system_rtl_id,
            "foundry": str(_stage_setting(stage, "foundry", "sky130")),
            "pdk": str(_stage_setting(stage, "pdk", "sky130A")),
            "toolchain": str(_stage_setting(stage, "toolchain", "openlane2")),
            "target_frequency_mhz": float(_stage_setting(stage, "target_frequency_mhz", 100) or 100),
            "constraints_sdc": str(_stage_setting(stage, "constraints_sdc", "") or ""),
            "effort": str(_stage_setting(stage, "effort", "balanced")),
            "analog_physical_mode": analog_physical_mode,
            "analog_pdk": str(_stage_setting(stage, "analog_pdk", _stage_setting(stage, "pdk", "sky130A")) or "sky130A"),
            "analog_spice_text": str(_stage_setting(stage, "analog_spice_text", "") or "") or None,
            "run_fill": bool(_stage_setting(stage, "run_fill", True)),
            "run_drc": bool(_stage_setting(stage, "run_drc", True)),
            "run_lvs": bool(_stage_setting(stage, "run_lvs", True)),
            "run_signoff_closure_loop": bool(_stage_setting(stage, "run_signoff_closure_loop", False)),
            "max_signoff_closure_iterations": int(_stage_setting(stage, "max_signoff_closure_iterations", 1) or 1),
            "allow_timing_repair": bool(_stage_setting(stage, "allow_timing_repair", True)),
            "allow_drc_repair": bool(_stage_setting(stage, "allow_drc_repair", True)),
            "allow_lvs_repair": bool(_stage_setting(stage, "allow_lvs_repair", True)),
            "allow_lec_repair": bool(_stage_setting(stage, "allow_lec_repair", True)),
            "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
            "max_synthesis_closure_iterations": int(_stage_setting(stage, "max_synthesis_closure_iterations", 1) or 1),
            "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
            "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
            "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
            "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
            "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
            "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            "signoff_closure": {
                "enabled": bool(_stage_setting(stage, "run_signoff_closure_loop", False)),
                "max_iterations": int(_stage_setting(stage, "max_signoff_closure_iterations", 1) or 1),
                "allow_timing_repair": bool(_stage_setting(stage, "allow_timing_repair", True)),
                "allow_drc_repair": bool(_stage_setting(stage, "allow_drc_repair", True)),
                "allow_lvs_repair": bool(_stage_setting(stage, "allow_lvs_repair", True)),
                "allow_lec_repair": bool(_stage_setting(stage, "allow_lec_repair", True)),
            },
            "synthesis_closure": {
                "enabled": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
                "max_iterations": int(_stage_setting(stage, "max_synthesis_closure_iterations", 1) or 1),
                "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
                "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
                "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
                "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
                "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
                "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
            },
            "toggles": {
                "run_spec2rtl_check": bool(_stage_setting(stage, "run_spec2rtl_check", False)),
                "enable_scan_dft": bool(_stage_setting(stage, "enable_scan_dft", False)),
                "run_drc": bool(_stage_setting(stage, "run_drc", True)),
                "run_lvs": bool(_stage_setting(stage, "run_lvs", True)),
                "run_fill": bool(_stage_setting(stage, "run_fill", True)),
                "run_signoff_closure_loop": bool(_stage_setting(stage, "run_signoff_closure_loop", False)),
                "allow_timing_repair": bool(_stage_setting(stage, "allow_timing_repair", True)),
                "allow_drc_repair": bool(_stage_setting(stage, "allow_drc_repair", True)),
                "allow_lvs_repair": bool(_stage_setting(stage, "allow_lvs_repair", True)),
                "allow_lec_repair": bool(_stage_setting(stage, "allow_lec_repair", True)),
                "run_synthesis_closure_loop": bool(_stage_setting(stage, "run_synthesis_closure_loop", False)),
                "allow_synthesis_timing_repair": bool(_stage_setting(stage, "allow_synthesis_timing_repair", True)),
                "allow_synthesis_lec_repair": bool(_stage_setting(stage, "allow_synthesis_lec_repair", True)),
                "allow_synthesis_retiming": bool(_stage_setting(stage, "allow_synthesis_retiming", False)),
                "allow_synthesis_hierarchy_flattening": bool(_stage_setting(stage, "allow_synthesis_hierarchy_flattening", False)),
                "stop_on_synthesis_closure_failure": bool(_stage_setting(stage, "stop_on_synthesis_closure_failure", False)),
                "stop_on_synthesis_lec_failure": bool(_stage_setting(stage, "stop_on_synthesis_lec_failure", False)),
                "generate_analog_gds": bool(_stage_setting(stage, "generate_analog_gds", False)) or analog_physical_mode == "generate_sky130_gds",
                "regenerate_lef_lib_after_gds": bool(_stage_setting(stage, "regenerate_lef_lib_after_gds", True)),
                "run_lef_lib_consistency": bool(_stage_setting(stage, "run_lef_lib_consistency", True)),
                "run_logic_equivalence": bool(_stage_setting(stage, "run_logic_equivalence", True)),
            },
        }
    raise RuntimeError(f"Product run does not support stage app '{app_name}' yet.")


def _run_product_stage(product: Dict[str, Any], product_run_id: str, stage_run: Dict[str, Any], upstream: Dict[str, str]) -> str:
    stage = {
        "id": stage_run["stage_id"],
        "label": stage_run.get("stage_label"),
        "app": stage_run.get("app"),
        **(stage_run.get("inputs") if isinstance(stage_run.get("inputs"), dict) else {}),
    }
    app_name = str(stage.get("app") or "")
    _append_product_run_log(product_run_id, f"Running {stage.get('label') or app_name} ({app_name})")
    payload = _product_stage_payload(product, stage, upstream)
    workflow_title = f"Product: {product.get('name')} / {stage.get('label') or app_name}"
    digital_slug = {
        "Digital_Arch2RTL": "arch2rtl",
        "Digital_DQA": "dqa",
        "Digital_Verify": "verify",
        "Digital_Arch2Synthesis": "arch2synthesis",
        "Digital_Arch2Tapeout": "tapeout",
        "verify_closure_loop": "verify_closure_loop",
    }.get(app_name)
    digital_template = {
        "Digital_Arch2RTL": "Digital_Arch2RTL",
        "Digital_DQA": "Digital_DQA",
        "Digital_Verify": "Digital_Verify",
        "Digital_Arch2Synthesis": "Digital_Arch2Synthesis",
        "Digital_Arch2Tapeout": "Digital_Arch2Tapeout",
        "verify_closure_loop": "Digital_Verify_Closure_Loop",
    }.get(app_name)
    embedded_template = {"Embedded_Run": "Embedded_Run"}.get(app_name)
    system_template = {
        "System_RTL": "System_RTL",
        "System_DQA": "System_DQA",
        "System_Sim": "System_Sim",
        "System_Synthesis": "System_Synthesis",
        "System_Firmware": "System_Firmware",
        "System_PD": "System_PD",
        "System_Software": "System_Software",
        "System_Software_Validation_L2": "System_Software_Validation_L2",
        "System_Product_App_Builder": "System_Product_App_Builder",
    }.get(app_name)
    loop_type = "digital" if digital_slug else "embedded" if embedded_template else "system"
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(str(product.get("user_id") or ""), workflow_title, loop_type)
    artifact_dir = os.path.join(base_dir, str(stage.get("id") or app_name).replace("_", "-"))
    _update_product_stage_run(stage_run["id"], {
        "status": "running",
        "workflow_id": workflow_id,
        "run_id": run_id,
        "inputs": payload,
        "started_at": datetime.utcnow().isoformat(),
    })
    user_id = str(product.get("user_id") or "")
    if digital_slug and digital_template:
        if app_name == "verify_closure_loop":
            payload["parent_workflow_id"] = payload.get("source_verify_workflow_id")
            payload["closure_iteration_index"] = 1
            payload["max_iterations"] = max(int(payload.get("max_iterations") or 1), 1)
            if not payload.get("seed_budget"):
                payload["seed_budget"] = payload.get("seed_count") or 5
        execute_digital_app_background(
            workflow_id,
            run_id,
            user_id,
            artifact_dir,
            digital_slug,
            digital_template,
            payload,
        )
    elif embedded_template:
        execute_embedded_app_background(
            workflow_id,
            run_id,
            user_id,
            artifact_dir,
            embedded_template,
            payload,
        )
    elif system_template:
        execute_system_app_background(
            workflow_id,
            run_id,
            user_id,
            artifact_dir,
            system_template,
            payload,
        )
    else:
        raise RuntimeError(f"Product run does not support stage app '{app_name}' yet.")
    status = _workflow_status(workflow_id)
    if status != "completed":
        raise RuntimeError(f"{stage.get('label') or app_name} workflow ended with status '{status or 'unknown'}'")
    outputs = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "app": app_name,
        "dashboard_url": f"/apps/dashboard/artifact/{workflow_id}",
        "download_url": f"/workflow/{workflow_id}/download_zip",
    }
    _update_product_stage_run(stage_run["id"], {
        "status": "completed",
        "outputs": outputs,
        "completed_at": datetime.utcnow().isoformat(),
    })
    _append_product_run_log(product_run_id, f"Completed {stage.get('label') or app_name}: workflow_id={workflow_id}")
    return workflow_id


def execute_product_run_background(
    product_id: str,
    product_run_id: str,
    user_id: str,
    max_stages: int = 8,
    start_stage: Optional[str] = None,
    resume_product_run_id: Optional[str] = None,
) -> None:
    upstream: Dict[str, str] = _seed_product_upstream_from_prior_run(
        resume_product_run_id,
        product_id,
        user_id,
        start_stage,
    )
    stage_results: Dict[str, Any] = {}
    try:
        product_rows = (
            supabase.table("products")
            .select("id,user_id,name,product_type,starting_point,description,stage_config,status")
            .eq("id", product_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        ).data or []
        if not product_rows:
            raise RuntimeError("Product not found")
        product = product_rows[0]
        stages = ((product.get("stage_config") or {}).get("stages") or []) if isinstance(product.get("stage_config"), dict) else []
        enabled = [stage for stage in stages if isinstance(stage, dict) and _product_stage_enabled(stage)]
        supported_apps = {
            "Digital_Arch2RTL",
            "Digital_DQA",
            "Digital_Verify",
            "Digital_Arch2Synthesis",
            "Digital_Arch2Tapeout",
            "verify_closure_loop",
            "Embedded_Run",
            "System_Software",
            "System_Software_Validation_L2",
            "System_Product_App_Builder",
            "System_RTL",
            "System_DQA",
            "System_Sim",
            "System_Synthesis",
            "System_Firmware",
            "System_PD",
        }
        supported = [stage for stage in enabled if stage.get("app") in supported_apps]
        if not supported:
            raise RuntimeError("No supported runnable stages found.")
        if start_stage:
            normalized_start_stage = str(start_stage).strip()
            start_index = next(
                (
                    index
                    for index, stage in enumerate(supported)
                    if str(stage.get("id") or "") == normalized_start_stage
                    or str(stage.get("app") or "") == normalized_start_stage
                ),
                -1,
            )
            if start_index < 0:
                raise RuntimeError(f"Start stage '{normalized_start_stage}' was not found in enabled runnable stages.")
            supported = supported[start_index:]
        supported = supported[: max(1, min(int(max_stages or 8), 8))]
        _update_product_run(product_run_id, {"status": "running", "current_stage": supported[0].get("id")})
        _update_product_status(product_id, user_id, "running")
        _append_product_run_log(product_run_id, f"Product run started with {len(supported)} enabled stage(s).")

        for stage in supported:
            if _product_run_cancelled(product_run_id, user_id):
                _update_product_run(product_run_id, {
                    "status": "cancelled",
                    "current_stage": None,
                    "stage_results": stage_results,
                    "error": "Cancelled by user",
                    "completed_at": datetime.utcnow().isoformat(),
                })
                _update_product_status(product_id, user_id, "cancelled", stage_results)
                _append_product_run_log(product_run_id, "Product run cancelled before next stage.")
                return
            _append_product_run_log(product_run_id, f"Queued {stage.get('label') or stage.get('id')} ({stage.get('app')})")
            stage_record = {
                "product_run_id": product_run_id,
                "product_id": product_id,
                "user_id": user_id,
                "stage_id": stage.get("id"),
                "stage_label": stage.get("label") or stage.get("id") or "",
                "app": stage.get("app") or "",
                "status": "queued",
                "inputs": stage,
                "outputs": {},
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
            rows = supabase.table("product_stage_runs").insert(stage_record).execute().data or []
            stage_run = rows[0] if rows else stage_record
            _update_product_run(product_run_id, {"current_stage": stage.get("id")})
            try:
                workflow_id = _run_product_stage(product, product_run_id, stage_run, upstream)
                upstream_key = _product_upstream_key_for_app(str(stage.get("app") or ""))
                if upstream_key:
                    upstream[upstream_key] = workflow_id
                stage_results[str(stage.get("id"))] = {
                    "status": "completed",
                    "workflow_id": workflow_id,
                    "app": stage.get("app") or "",
                    "label": stage.get("label") or stage.get("id") or "",
                    "dashboard_url": f"/apps/dashboard/artifact/{workflow_id}",
                    "download_url": f"/workflow/{workflow_id}/download_zip",
                }
                _update_product_run(product_run_id, {"stage_results": stage_results})
                _update_product_status(product_id, user_id, "running", stage_results)
            except Exception as exc:
                message = str(exc)
                _append_product_run_log(product_run_id, f"Failed {stage.get('label') or stage.get('id')}: {message}")
                _update_product_stage_run(stage_run["id"], {
                    "status": "failed",
                    "error": message,
                    "completed_at": datetime.utcnow().isoformat(),
                })
                stage_results[str(stage.get("id"))] = {
                    "status": "failed",
                    "error": message,
                    "app": stage.get("app") or "",
                    "label": stage.get("label") or stage.get("id") or "",
                }
                _update_product_run(product_run_id, {
                    "status": "failed",
                    "stage_results": stage_results,
                    "error": message,
                    "completed_at": datetime.utcnow().isoformat(),
                })
                _update_product_status(product_id, user_id, "failed", stage_results)
                return
        _update_product_run(product_run_id, {
            "status": "completed",
            "current_stage": None,
            "stage_results": stage_results,
            "completed_at": datetime.utcnow().isoformat(),
        })
        _update_product_status(product_id, user_id, "completed", stage_results)
        _append_product_run_log(product_run_id, "Product run completed.")
    except Exception as exc:
        _append_product_run_log(product_run_id, f"Product run failed: {exc}")
        _update_product_run(product_run_id, {
            "status": "failed",
            "error": str(exc),
            "stage_results": stage_results,
            "completed_at": datetime.utcnow().isoformat(),
        })
        _update_product_status(product_id, user_id, "failed", stage_results)


@app.post("/products/{product_id}/run")
async def start_product_run(product_id: str, payload: ProductRunStartIn, request: Request, background_tasks: BackgroundTasks):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to run a product")
    product = await get_product(product_id, request)
    if not product.get("product"):
        raise HTTPException(status_code=404, detail="Product not found")
    record = {
        "product_id": product_id,
        "user_id": user_id,
        "status": "queued",
        "stage_results": {},
        "logs": "Product run queued.",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    try:
        rows = supabase.table("product_runs").insert(record).execute().data or []
        product_run = rows[0] if rows else {**record, "id": str(uuid.uuid4())}
        _update_product_status(product_id, user_id, "queued", {})
    except Exception as exc:
        logger.error("Failed to create product run: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to create product run: {exc}")
    background_tasks.add_task(
        execute_product_run_background,
        product_id,
        product_run["id"],
        user_id,
        payload.max_stages or 8,
        payload.start_stage,
        payload.resume_product_run_id,
    )
    return {"status": "ok", "product_run": product_run}


@app.post("/products/{product_id}/runs/{product_run_id}/cancel")
async def cancel_product_run(product_id: str, product_run_id: str, request: Request):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to cancel a product run")
    run = _product_run_row(product_run_id, user_id)
    if not run or str(run.get("product_id")) != str(product_id):
        raise HTTPException(status_code=404, detail="Product run not found")
    status = str(run.get("status") or "")
    if status not in {"queued", "running"}:
        return {"status": "ok", "product_run": run}
    _update_product_run(product_run_id, {
        "status": "cancelled",
        "current_stage": None,
        "error": "Cancelled by user",
        "completed_at": datetime.utcnow().isoformat(),
    })
    _update_product_status(product_id, user_id, "cancelled", run.get("stage_results") if isinstance(run.get("stage_results"), dict) else None)
    _append_product_run_log(product_run_id, "Cancellation requested by user.")
    return {"status": "ok", "product_run": _product_run_row(product_run_id, user_id)}


@app.get("/products/{product_id}/runs")
async def list_product_runs(product_id: str, request: Request, limit: int = Query(10, ge=1, le=50)):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to view product runs")
    try:
        rows = (
            supabase.table("product_runs")
            .select("id,product_id,user_id,status,current_stage,stage_results,logs,error,created_at,updated_at,completed_at")
            .eq("product_id", product_id)
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        ).data or []
        stage_rows: List[Dict[str, Any]] = []
        run_ids = [str(row.get("id")) for row in rows if row.get("id")]
        if run_ids:
            stage_rows = (
                supabase.table("product_stage_runs")
                .select("id,product_run_id,product_id,stage_id,stage_label,app,status,workflow_id,run_id,inputs,outputs,error,started_at,completed_at,created_at,updated_at")
                .in_("product_run_id", run_ids)
                .eq("user_id", user_id)
                .order("created_at")
                .execute()
            ).data or []
            stage_rows = _enrich_product_stage_rows(stage_rows if isinstance(stage_rows, list) else [])
        stages_by_run: Dict[str, List[Dict[str, Any]]] = {}
        for stage in stage_rows if isinstance(stage_rows, list) else []:
            stages_by_run.setdefault(str(stage.get("product_run_id")), []).append(stage)
        return {
            "status": "ok",
            "product_runs": [
                {**row, "stage_runs": stages_by_run.get(str(row.get("id")), [])}
                for row in rows
            ],
        }
    except Exception as exc:
        logger.error("Failed to list product runs: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to list product runs: {exc}")


@app.get("/products/{product_id}/runs/{product_run_id}")
async def get_product_run(product_id: str, product_run_id: str, request: Request):
    user_id = _request_user_id_optional(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required to view product runs")
    run = _product_run_row(product_run_id, user_id)
    if not run or str(run.get("product_id")) != str(product_id):
        raise HTTPException(status_code=404, detail="Product run not found")
    stages = _product_stage_rows(product_run_id, user_id)
    return {"status": "ok", "product_run": run, "stage_runs": stages}


@app.post("/auto_compose_workflow")
async def auto_compose_workflow(request: Request):
    """
    Auto-compose a workflow graph from a user goal.
    - Uses LLM fallback chain (Ollama → Portkey → OpenAI)
    - Calls Agent Planner for missing agents
    - Returns nodes + edges for rendering
    """
    try:
        data = await request.json()
        goal = data.get("goal", "")
        preplan = data.get("preplan", None)
        logger.info(f"🧠 Auto-composing workflow for goal: {goal}")
        logger.info(f"📥 Received preplan: {type(preplan)} | Keys: {list(preplan.keys()) if isinstance(preplan, dict) else 'None'}")
        from planner.ai_work_planner import auto_compose_workflow_graph
        structured_spec_final = data.get("structured_spec_final")
        graph = await auto_compose_workflow_graph(goal,structured_spec_final=structured_spec_final,preplan=preplan)

        return {"status": "ok", **graph}
    except Exception as e:
        logger.error(f"❌ Auto-compose failed: {e}")
        return {"status": "error", "message": str(e)}

from utils.audio_utils import transcribe_audio
from utils.notion_utils import append_to_notion, get_or_create_notion_page
from utils.llm_utils import run_llm_fallback
from fastapi import File, UploadFile, Form

@app.post("/voice_stream")
async def voice_stream(file: UploadFile = File(...), user_id: str = Form("anonymous")):
    """Takes audio input, transcribes it, and appends to Notion."""
    audio_bytes = await file.read()
    transcript = transcribe_audio(audio_bytes)
    page_id = get_or_create_notion_page(user_id)
    append_to_notion(page_id, transcript)
    return {"status": "ok", "transcript": transcript}

@app.post("/summarize_notion")
async def summarize_notion(user_id: str = Form("anonymous")):
    """Fetch recent Notion text → summarize → send to analyzer."""
    from utils.notion_utils import notion, get_or_create_notion_page
    page_id = get_or_create_notion_page(user_id)
    blocks = notion.blocks.children.list(page_id).get("results", [])
    text = " ".join(b["paragraph"]["text"][0]["text"]["content"] for b in blocks if b["type"] == "paragraph")

    summary = run_llm_fallback(f"Summarize this chip design spec in JSON (intent, inputs, outputs, constraints, verification): {text}")
    return {"status": "ok", "summary": summary}

# ==============================
# 🔄 WebSocket for Live Spec Feedback
# ==============================


@app.websocket("/spec_live_feedback")
async def spec_live_feedback(websocket: WebSocket):
    await websocket.accept()
    print("✅ WebSocket connection accepted")

    try:
        while True:
            try:
                # === 1. Fetch latest entry from Notion database ===
                notion_data = notion.databases.query(
                    **{
                        "database_id": os.getenv("NOTION_DATABASE_ID"),
                        "sorts": [{"property": "Last edited time", "direction": "descending"}],
                        "page_size": 1,
                    }
                )
                latest_page = notion_data["results"][0]
                notion_summary = latest_page["properties"].get("Name", {}).get("title", [{}])[0].get("plain_text", "")
                last_edit_time = latest_page["last_edited_time"]

                # === 2. Get latest coverage from Supabase ===
                coverage_row = (
                    supabase.table("spec_coverage")
                    .select("intent_score, io_score, constraint_score, verification_score, total_score")
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                coverage = coverage_row.data[0] if coverage_row.data else None

                # === 3. Combine & send ===
                message = {
                    "summary": {
                        "Notion_Summary": notion_summary or "Waiting for new Notion content...",
                        "Last_Updated": last_edit_time,
                    },
                    "coverage": coverage.get("total_score", 0) if coverage else 0,
                }
                await websocket.send_json(message)

            except Exception as inner_e:
                print(f"⚠️ Inner loop error in WebSocket: {inner_e}")
                await websocket.send_json({"error": str(inner_e)})

            await asyncio.sleep(5)

    except Exception as e:
        print(f"⚠️ WebSocket disconnected: {e}")

@app.post("/voice_to_spec")
async def voice_to_spec(file: UploadFile = File(...)):
    """Legacy voice-to-spec endpoint. Prefer /studio/voice/* for new UI."""
    try:
        text = transcribe_audio(await file.read())
        summary_prompt = f"Summarize and structure this design spec:\n{text}"
        summary = await run_llm_fallback(summary_prompt)

        return {"summary": summary, "transcript": text, "coverage": 65, "mode": "voice", "legacy": True}

    except Exception as e:
        return {"error": str(e)}

# ---------- DELETE a saved custom workflow ----------
@app.delete("/delete_custom_workflow")
def delete_custom_workflow(request: Request, name: str = Query(...)):
    """
    Deletes a workflow by name for the current user.
    Removes associated runs too.
    """
    user_id = request.query_params.get("user_id")

    # Find workflow by name and user
    q = supabase.table("workflows").select("id").eq("name", name)
    q = q.eq("user_id", user_id) if user_id else q.is_("user_id", None)
    res = q.limit(1).execute()
    if not res.data:
        return {"status": "ok", "deleted": 0, "message": "No such workflow"}

    wf_id = res.data[0]["id"]

    # Delete dependent runs
    try:
        supabase.table("runs").delete().eq("workflow_id", wf_id).execute()
    except Exception:
        pass

    # Delete workflow
    supabase.table("workflows").delete().eq("id", wf_id).execute()
    return {"status": "ok", "deleted": 1, "workflow_id": wf_id}


# ---------- RENAME a saved custom workflow ----------
@app.post("/rename_custom_workflow")
async def rename_custom_workflow(request: Request):
    """
    Renames a saved workflow for the current user.
    Body: { "old_name": "Foo", "new_name": "Bar" }
    """
    try:
        data = await request.json()
        old_name = data.get("old_name")
        new_name = data.get("new_name")

        if not old_name or not new_name:
            raise HTTPException(status_code=400, detail="Both old_name and new_name required")

        user_id = (await request.json()).get("user_id") or None

        q = supabase.table("workflows").select("id").eq("name", old_name)
        q = q.eq("user_id", user_id) if user_id else q.is_("user_id", None)
        res = q.limit(1).execute()
        if not res.data:
            return {"status": "error", "message": "Workflow not found"}

        wf_id = res.data[0]["id"]
        supabase.table("workflows").update({
            "name": new_name,
            "updated_at": datetime.utcnow().isoformat()
        }).eq("id", wf_id).execute()

        return {"status": "ok", "workflow_id": wf_id, "new_name": new_name}

    except Exception as e:
        logger.error(f"❌ Rename failed: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/publish_custom_workflow")
async def publish_custom_workflow(request: Request):
    """
    Submit a custom workflow to marketplace_submissions as a pending entry.
    """
    try:
        data = await request.json()
        workflow_name = data.get("workflow_name")
        user_id = data.get("user_id")

        if not workflow_name or not user_id:
            return {"status": "error", "message": "workflow_name and user_id required"}

        # Find workflow row for this user
        q = supabase.table("workflows").select("*").eq("name", workflow_name)
        q = q.eq("user_id", user_id) if user_id else q.is_("user_id", None)
        res = q.limit(1).execute()

        if not res.data:
            return {"status": "error", "message": "Workflow not found"}

        wf_row = res.data[0]

        # For workflow-only submissions, agent_json must still be non-null → use empty object
        submission = {
            "agent_id": None,
            "submitted_by": user_id,
            "agent_json": {},            # required NOT NULL
            "workflow_json": wf_row,
            "status": "pending",
        }

        supabase.table("marketplace_submissions").insert(submission).execute()
        logger.info(f"🧩 Marketplace submission created for workflow '{workflow_name}' by {user_id}")

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ publish_custom_workflow failed: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/agents/get_code")
async def get_agent_code(agent: str):
    from agent_capabilities import AGENT_CAPABILITIES
    import pathlib

    possible_paths = [
        f"agents/custom/{agent.lower()}.py",
        f"agents/{agent.lower()}.py"
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return open(path).read()

    return ""

@app.post("/agents/save_code")
async def save_agent_code(data: dict):
    agent = data["agent"]
    code = data["code"]

    file_path = f"agents/custom/{agent.lower()}.py"
    with open(file_path, "w") as f:
        f.write(code)

    # Reload custom agents
    from agents.loader import load_custom_agents
    load_custom_agents()

    return {"status": "ok"}





@app.post("/finalize_spec_natural_sentences")
async def finalize_spec_natural_sentences(data: dict):
    """
    Convert edited missing values into natural language sentences and append to original prompt.
    Then recompute structured spec + real coverage and return all in one shot.
    Optional: if structured_spec_draft is provided, we finalize from the draft for higher accuracy.


    """

    

    original_raw = (data.get("original_text") or "").strip()
    improved_raw = (data.get("improved_text") or "").strip()

    # ✅ Prefer improved (LLM-enhanced) text if available
    base_text = improved_raw if (improved_raw and len(improved_raw) > len(original_raw)) else original_raw
    # --- REMOVE incomplete / missing details block ---
    base_text = re.sub(
        r"Detected missing or incomplete details:\s*\n(?:- .*\n)*",
        "",
        base_text,
        flags=re.MULTILINE
    ).strip()

    # Remove autofill hint lines like: field_name: [value]
    base_text = re.sub(
        r"^[A-Za-z0-9_.\[\]-]+\s*:\s*\[.*?\]\s*$",
        "",
        base_text,
        flags=re.MULTILINE
    ).strip()

    # Remove any pre-existing "Additional Inferred Design Details" block if present
    base_text = re.sub(
        r"Additional Inferred Design Details:.*$",
        "",
        base_text,
        flags=re.DOTALL
    ).strip()



    missing = data.get("missing", [])
    edited_values = data.get("edited_values", {})
    def normalize_value(path, value):
        mf = next((m for m in missing if m.get("path") == path), None)
        if mf and mf.get("type") == "number":
            try:
                return int(value)
            except:
                try:
                   return float(value)
                except:
                   return value
        return value

    edited_values = {path: normalize_value(path, val) for path, val in edited_values.items()}
    structured_spec_draft = data.get("structured_spec_draft")  # optional
    user_id = data.get("user_id",None)

    print("Structured_spec_draft just before merge",structured_spec_draft)
    print("Edited_values just before merge",edited_values)
    print("missing_values just before merge",missing)

    additions = []

    additions = [] 

    for item in missing:
        path = item.get("path", "")
        ask = item.get("ask", "") or path.replace("_", " ").replace(".", " → ")

    # ✅ FIXED: safe numeric + string handling
        raw_val = edited_values.get(path, "")
        value = str(raw_val).strip()

        if not value:
           continue

        sentence_prompt = f"""
        Write one clear natural language design clarification sentence.
        Clarification: "{ask}"
        Value: "{value}"
        Style example: "The clock frequency is 200 MHz."
        Do NOT repeat or rewrite the original spec.
        Keep concise, factual, and correct.
        """.strip()

        sentence = (await run_llm_fallback(sentence_prompt)).strip()
        additions.append(f"- {sentence}")

    additions_text = "\n".join(additions)
    final_text = f"""{base_text}

    Additional Inferred Design Details:
    {additions_text}
    """.strip()

    

    # --- Recompute structured spec + coverage ---
    try:
        # Prefer finalizing from a known draft if provided
        from analyze.digital.analyze_spec_digital import analyze_spec_digital, finalize_spec_digital

        if structured_spec_draft:
            # Merge edited values into draft (path strings like "clock.freq")

            # Merge edited values into draft (path strings like "clock_domains[0].frequency_mhz")
            print("\n---- FINALIZE START ----")
            print("edited_values:", edited_values)
            print("missing:", missing)
            print("structured_spec_draft inside if :", structured_spec_draft)
            
            for path, value in edited_values.items():
                try:
                    apply_spec_value(structured_spec_draft, path, value)
                except Exception as e:
                    print(f"❌ ERROR applying {path} = {value} → {e}")

            # ✅ Mark edited fields as user-confirmed so coverage increases
            for item in missing:
                path = item.get("path")
                if path in edited_values:
        # Mark explicit confirmation in the structured spec
                    try:
                        tokens = [t for t in re.split(r"\.|\[|\]", path) if t]
                        target = structured_spec_draft
                        for t in tokens[:-1]:
                           target = target[int(t)] if t.isdigit() else target.get(t, target)
                           last = tokens[-1]

                        # Add a confidence marker without overwriting actual field values
                        if isinstance(target, dict):
                           meta = target.setdefault("_confirmed", {})
                           meta[last] = True

                    except Exception as e:
                        print(f"⚠️ Confirmation tag failed for {path}: {e}")



    
            if additions_text and additions_text.strip():
               structured_spec_draft["natural_language_notes"] = additions_text.strip()

            
            print("structured_spec_draft AFTER merge:", structured_spec_draft)

            try:
                structured_spec_draft = convert_numeric_types(structured_spec_draft)
                print("structured_spec_draft after type normalization:", structured_spec_draft)
               # ✅ Recompute missing from updated spec
                from analyze.digital.missing_slot_detector import detect_missing_slots
                detected_missing = await detect_missing_slots(structured_spec_draft)
               # Treat client-provided missing as the *original frozen catalog*
                initial_missing_catalog = {m.get("path") for m in (missing or []) if m and m.get("path")}

               # Filter: only keep the ones that were originally missing
                remaining_missing = [
                    m for m in detected_missing
                    if m.get("path") in initial_missing_catalog
                ]
            except Exception as e:
               print("🔥 ERROR in convert_numeric_types:", e)
               print("Offending structured_spec_draft:", structured_spec_draft)

            final = await finalize_spec_digital(structured_spec_draft,edited_values,user_id)

            structured_final = final.get("structured_spec_final", structured_spec_draft)
      

            
               
            print("final result raw:", final)
            print("structured_final", structured_final)
            print("Remaining missing",remaining_missing)

            total_slots = max(1, len(initial_missing_catalog))
            covered_slots = total_slots - len(remaining_missing)
            coverage_final = round((covered_slots / total_slots) * 100)

            print("coverage_final computed:", coverage_final)
            print("---- FINALIZE END ----\n")

           
        else:
            # Fallback: analyze the natural-language final text to produce structure + coverage
            analyzed = await analyze_spec_digital(final_text, voice_summary="", user_id="anonymous")
            # support either shape: {structured_spec_final, coverage} OR nested result
            structured_final = analyzed.get("structured_spec_final") or analyzed.get("structured_spec_draft") or analyzed
            coverage_final = (
                analyzed.get("coverage")
                or (analyzed.get("coverage", {}) if isinstance(analyzed.get("coverage"), dict) else {})
                or analyzed.get("coverage_score")
                or 0
            )
            # Handle nested object coverage.total_score
            if isinstance(coverage_final, dict) and "total_score" in coverage_final:
                coverage_final = coverage_final["total_score"]

    except Exception as e:
        # Never fail the finalize call—return at least the final_text
        structured_final, coverage_final = {}, 0

    for item in missing:
        path = item.get("path", "")
        if path in edited_values and item.get("type") == "enum":
            val = str(edited_values[path]).lower().replace(" ", "").replace("-", "")
            options = [o.lower().replace(" ", "").replace("-", "") for o in item.get("options", [])]

        # If direct match → keep it
            if val not in options:
            # Best-effort normalization mappings (still dynamic)
                if val in ("none", "no"):
                   val = "not_required"
                elif val in ("yes", "true"):
                   val = "required"
                else:
                   val = "unspecified"

            edited_values[path] = val

# --- Remove already-resolved missing fields (dynamic) ---
    remaining_missing = [
        m for m in (final.get("remaining_missing", []) if "final" in locals() else [])
        if m.get("path") not in edited_values
    ]

    return {
        "status": "ok",
        "final_text": final_text,
        "structured_spec_final": structured_final,
        "coverage": int(coverage_final) if isinstance(coverage_final, (int, float)) else 0,
        "coverage_final": int(coverage_final) if isinstance(coverage_final, (int, float)) else 0,
        "additions": additions,
        "remaining_missing": remaining_missing,
    }



@app.post("/auto_fill_missing_fields")
async def auto_fill_missing_fields_endpoint(payload: dict):
    original_text = payload.get("user_prompt") or payload.get("original_text")
    structured_spec_draft = payload.get("structured_spec_draft")
    missing_fields = payload.get("missing") or payload.get("remaining_missing_fields") or []

    improved_text, structured_spec_enhanced, remaining_missing_fields, auto_filled_values = \
        await auto_fill_missing_fields(original_text, structured_spec_draft, missing_fields)

    return {
        "improved_text": improved_text,
        "structured_spec_enhanced": structured_spec_enhanced,
        "remaining_missing_fields": remaining_missing_fields,
        "auto_filled_values": auto_filled_values
    }

from planner.ai_agent_planner import generate_missing_agents_batch

@app.post("/generate_missing_agents_batch")
async def api_generate_missing_agents_batch(request: Request):
    # Legacy System Planner path. Keep for compatibility, but new browser flows should
    # prefer Studio Agent Planner + Agent Factory dry-run + private user-agent save.
    payload = await request.json()
    result = await generate_missing_agents_batch(payload)
    created_names = result.get("created_agents", [])
    return {
        "status": "ok",
        "created_agents": created_names,
        "loop_type": result.get("loop_type")
    }
@app.post("/build_workflow")
async def build_workflow(request: Request):
    """
    Build workflow graph directly from selected agents.
    No re-planning, no missing-agent generation.
    Pure graph assembly for System Planner Step 3.
    """
    try:
        data = await request.json()
        user_id = data.get("user_id", "anonymous")
        final_agents = data.get("final_agents") or data.get("agents") or []


        if not final_agents or not isinstance(final_agents, list):
            raise HTTPException(status_code=400, detail="final_agents[] list required")

        # Import local workflow builder
        from planner.ai_work_planner import auto_compose_workflow_graph

        # Convert selected agents → preplan format expected by auto_compose_workflow_graph
        preplan = {
            "loop_type": "system",   # default safe
            "agents": final_agents,
            "missing_agents": []     # we do NOT want auto-generation here
        }

        # We intentionally pass structured_spec_final=None → no re-analysis
        graph = await auto_compose_workflow_graph(
            goal="User selected workflow",
            structured_spec_final=None,
            preplan=preplan,
            final_agents=final_agents
        )

        return {"status": "ok", "workflow": graph}

    except Exception as e:
        logger.error(f"❌ build_workflow failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
 
# ---------- DELETE a saved custom agent ----------
@app.delete("/delete_custom_agent")
def delete_custom_agent(request: Request, name: str = Query(...)):
    """
    Delete a custom agent by name (scoped per user; only is_custom=True).
    """
    user_id = request.query_params.get("user_id")

    q = supabase.table("agents") \
        .select("id") \
        .eq("agent_name", name) \
        .eq("is_custom", True)

    # ✅ match what frontend loads: owner_id, NOT user_id
    q = q.eq("owner_id", user_id) if user_id else q.is_("owner_id", None)
   
    res = q.limit(1).execute()

    if not res.data:
        return {"status": "ok", "deleted": 0, "message": "No such custom agent"}

    agent_id = res.data[0]["id"]
    supabase.table("agents") \
      .delete() \
      .eq("agent_name", name) \
      .eq("is_custom", True) \
      .eq("owner_id", user_id) \
      .execute()
    
    return {"status": "ok", "deleted": 1, "agent_id": agent_id}


# ---------- RENAME a saved custom agent ----------
@app.post("/rename_custom_agent")
async def rename_custom_agent(request: Request):
    """
    Rename a saved custom agent (scoped per user; only is_custom=True).
    Body: { "old_name": "X", "new_name": "Y" }
    """
    data = await request.json()
    old_name = data.get("old_name")
    new_name = data.get("new_name")

    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="old_name and new_name required")


    user_id = data.get("user_id") or None

    q = supabase.table("agents") \
        .select("id") \
        .eq("agent_name", old_name) \
        .eq("is_custom", True)
    q = q.eq("owner_id", user_id) if user_id else q.is_("owner_id", None)
    res = q.limit(1).execute()

    if not res.data:
        return {"status": "error", "message": "Agent not found"}

    agent_id = res.data[0]["id"]
    supabase.table("agents").update({
        "agent_name": new_name,
        "updated_at": datetime.utcnow().isoformat()
    }).eq("id", agent_id).execute()

    return {"status": "ok", "agent_id": agent_id, "new_name": new_name}


@app.post("/publish_custom_agent")
async def publish_custom_agent(request: Request):
    """
    Submit a custom agent to marketplace_submissions as a pending entry.
    """
    try:
        data = await request.json()
        agent_name = data.get("agent_name")
        user_id = data.get("user_id")

        if not agent_name or not user_id:
            return {"status": "error", "message": "agent_name and user_id required"}

        # Find this user's custom agent by name
        q = (
            supabase.table("agents")
            .select("*")
            .eq("agent_name", agent_name)
            .eq("is_custom", True)
        )
        # Match how delete_custom_agent scopes by owner_id
        q = q.eq("owner_id", user_id) if user_id else q.is_("owner_id", None)
        res = q.limit(1).execute()

        if not res.data:
            return {"status": "error", "message": "Custom agent not found"}

        agent_row = res.data[0]
        agent_id = agent_row.get("id")

        # Insert submission into marketplace_submissions
        submission = {
            "agent_id": agent_id,
            "submitted_by": user_id,
            "agent_json": agent_row,
            "workflow_json": None,
            "status": "pending",
        }

        supabase.table("marketplace_submissions").insert(submission).execute()
        logger.info(f"🧩 Marketplace submission created for agent '{agent_name}' by {user_id}")

        return {"status": "ok", "submission_id": submission.get("submission_id")}
    except Exception as e:
        logger.error(f"❌ publish_custom_agent failed: {e}")
        return {"status": "error", "message": str(e)}


# ==========================================================
# 🧭 DESIGN INTENT PLANNER (ClarifyLoop)
# ==========================================================

@app.post("/clarify_intent_round")
async def clarify_intent_round(request: Request):
    """
    Handles one LLM clarification round for Design Intent Planner.
    Generates questions, refined prompt, and loop-wise interpretation.
    """
    try:
        body = await request.json()
        user_id = body.get("user_id", "anonymous")
        round_num = int(body.get("round", 1))
        prompt = body.get("prompt", "").strip()
        previous_interpretation = body.get("previous_loop_interpretation", {})

        # ✅ Question limits per round
        question_limits = {1: 5, 2: 5, 3: 5}
        max_questions = question_limits.get(round_num, 4)

        # -----------------------------
        # Build LLM prompt dynamically
        # -----------------------------

        system_prompt = """
You are the Design Intent Clarify Agent for chip/system design.
Your job is to ask EXACTLY 5 clarifying questions per round.

STRICT RULES:

1. ALWAYS generate exactly 5 clarifying questions per round.

2. Only generate questions from RELEVANT domains:
   - DIGITAL (logic, datapath, clocking, interfaces, FSMs)
   - EMBEDDED (firmware flow, interrupts, sleep modes, memory)
   - ANALOG (sensor interfaces, ADC, DAC, amplifiers, power rails)
   - SYSTEM (integration, block interactions, power modes, timing)

3. DO NOT ask about a domain that is NOT implied by the user's design intent.
   Examples:
   - If user mentions only digital → ask 5 DIGITAL questions.
   - If user mentions only firmware → embed+system only.
   - If user mentions analog sensors → include analog.
   - If user mentions all → mix all 4 domains.

4. OUTPUT MUST ALWAYS BE STRICT JSON:
{
  "questions": ["Q1", "Q2", "Q3", "Q4", "Q5"], 
  "suggested_answers": {
      "Q1": "answer1",
      "Q2": "answer2",
      "Q3": "answer3",
      "Q4": "answer4",
      "Q5": "answer5"
  },
  "loop_interpretation": {
      "digital": "...",
      "embedded": "...",
      "analog": "...",
      "validation": "...",
      "system": "..."
  },
  "refined_prompt": "Professionally rewritten updated design intent.",
  "structured_intent": {...}
}

5. Suggested answers must be practical, realistic, and helpful for the domain.
   The user will edit these answers directly.

6. loop_interpretation should reflect the user's intent + the answers.
   Keep it short but meaningful.

7. refined_prompt must rewrite the user design intent clearly and cleanly.

8. Each question MUST have a practical suggested answer.
        """


        # 🧠 Build input for model
        llm_input = f"""
### USER DESIGN INTENT
{prompt}

### PREVIOUS LOOP INTERPRETATION
{json.dumps(previous_interpretation, indent=2)}
        """

        # -----------------------------
        # Run LLM (via Portkey/OpenAI)
        # -----------------------------
        from utils.llm_utils import run_llm_fallback
        llm_raw = await run_llm_fallback(system_prompt + "\n" + llm_input)

        print("LLM RAW RESULT:", llm_raw)

        try:
            data = json.loads(llm_raw)
        except Exception:
            # fallback if the LLM returned text-like JSON
            data = json.loads(re.search(r"\{.*\}", llm_raw, re.S).group(0))

        # Ensure structure
        questions = data.get("questions", [])
        question_keys = []
        question_keys = [f"Q{i+1}" for i in range(len(questions))]
        raw_answers = data.get("suggested_answers", {})

        # Convert dict → ordered list aligned with questions

        #✅ Convert dict → ordered list aligned with Q1..Qn
        suggested_answers = []
        for idx, q in enumerate(questions):
            key = f"Q{idx + 1}"
            suggested_answers.append(raw_answers.get(key, ""))

        refined_prompt = data.get("refined_prompt", prompt)
        loop_interpretation = data.get("loop_interpretation", previous_interpretation)

        # 🔍 Debug logging
        print(f"🔍 clarify_intent_round parsed questions: {questions}")
        print(f"🔍 clarify_intent_round raw_answers: {raw_answers}")
        print(f"🔍 clarify_intent_round suggested_answers list: {suggested_answers}")
        print(f"🔍 clarify_intent_round question_keys list: {question_keys}")



        return JSONResponse({
            "status": "ok",
            "round": round_num,
            "max_questions": max_questions,
            "questions": questions,
            "question_keys":question_keys,
            "suggested_answers": suggested_answers,
            "refined_prompt": refined_prompt,
            "loop_interpretation": loop_interpretation,
        })

    except Exception as e:
        import traceback
        logger.error(f"❌ clarify_intent_round failed: {e}\n{traceback.format_exc()}")
        return JSONResponse({"status": "error", "message": str(e)})

@app.post("/save_design_intent_draft")
async def save_design_intent_draft(request: Request):
    try:
        data = await request.json()
        user_id = data.get("user_id", "anonymous")
        title = data.get("title", "Untitled Design Intent")
        refined_prompt = data.get("refined_prompt", "")
        implementation_strategy = data.get("implementation_strategy", "")
        structured_intent = data.get("structured_intent", {})
        qa_pairs = data.get("qa_pairs", {})        # ← ADD THIS
        full_intent = data.get("full_intent", {})  # ← ADD THIS
        version = int(data.get("version", 1))

        payload = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "title": title,
            "refined_prompt": refined_prompt,
            "implementation_strategy": implementation_strategy,
            "structured_intent": structured_intent,
            "qa_pairs": qa_pairs,                
            "full_intent": full_intent,         
            "version": version,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        supabase.table("design_intent_drafts").insert(payload).execute()
        logger.info(f"💾 Design Intent Draft saved for user {user_id}: {title}")

        return JSONResponse({"status": "ok", "data": payload})

    except Exception as e:
        logger.error(f"❌ save_design_intent_draft failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)})

ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")


def _iter_leaf_strings(obj: Any) -> Iterable[str]:
    """
    Walk any nested dict/list and yield leaf string values.
    We only want strings that look like storage object paths.
    """
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_leaf_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_leaf_strings(v)
    elif isinstance(obj, str):
        s = obj.strip()
        if s:
            yield s


def _normalize_storage_path(p: str) -> str:
    """
    Your artifacts JSON sometimes stores:
      - "/artifacts/anonymous/<id>/X.txt"  (web route)
      - "backend/workflows/<id>/spec/file.v" (storage object path)
    For storage download we need the *object path* inside the bucket.

    Rules:
    - If it starts with "/artifacts/anonymous/", strip leading "/artifacts/anonymous/".
    - If it starts with "/artifacts/", strip leading "/artifacts/".
    - Else use as-is.
    """
    p = p.strip()
    if p.startswith("/artifacts/anonymous/"):
        return p[len("/artifacts/anonymous/") :]
    if p.startswith("/artifacts/"):
        return p[len("/artifacts/") :]
    if p.startswith("/"):
        return p[1:]
    return p


#@app.get("/workflow/{workflow_id}/download_zip")
#def download_workflow_zip(workflow_id: str):
#    # 1) Load artifacts JSON
#   row = (
#       supabase.table("workflows")
#        .select("artifacts")
#        .eq("id", workflow_id)
#        .single()
#       .execute()
#    )

#    artifacts = (row.data or {}).get("artifacts") or {}
#    if not artifacts:
#        raise HTTPException(status_code=404, detail="No artifacts found for this workflow.")

#   # 2) Collect storage paths
#    raw_paths = list(_iter_leaf_strings(artifacts))
#    storage_paths = []
#    seen = set()
#   for p in raw_paths:
#        sp = _normalize_storage_path(p)
#        # ignore obvious non-storage values
#        if not sp or sp.endswith(".txt") is False and "backend/workflows/" not in sp and "/" not in sp:
#           # (keep this loose; you can tighten later)
#            pass
#       if sp not in seen:
#            storage_paths.append(sp)
#            seen.add(sp)

#    if not storage_paths:
#        raise HTTPException(status_code=404, detail="No downloadable storage paths found in artifacts JSON.")

#    # 3) Build ZIP in-memory
#    buf = io.BytesIO()
#    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
#        for sp in storage_paths:
#            try:
#                res = supabase.storage.from_(ARTIFACT_BUCKET).download(sp)
#                # supabase-py returns bytes
#                if not res:
#                    continue

#                # Put in zip with a friendly name
#                # Example: backend/workflows/<id>/spec/top_module.v -> spec/top_module.v
#                arcname = sp
#                needle = f"backend/workflows/{workflow_id}/"
#                if needle in sp:
#                    arcname = sp.split(needle, 1)[1]
#                zf.writestr(arcname, res)
#            except Exception as e:
#                # Don’t fail whole zip; include an error file instead
#                zf.writestr(f"errors/{sp.replace('/', '_')}.error.txt", f"Failed to download {sp}\n{e}\n")

#    buf.seek(0)

#    filename = f"workflow_{workflow_id}_artifacts.zip"
#    return StreamingResponse(
#        buf,
#        media_type="application/zip",
#        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
#    )

@app.get("/workflow/{workflow_id}/download_zip")
def download_workflow_zip(workflow_id: str, full: bool = False):
    """
    Default (full=False): existing Studio behavior (zip from workflows.artifacts JSON)
    If full=True: zip by listing ALL objects in Storage under backend/workflows/<workflow_id>/
                  (works even if workflows.artifacts is incomplete due to payload limits)
    """

    prefix = f"backend/workflows/{workflow_id}/"

    def _list_all_files_recursive(path_prefix: str):
        """
        Recursively list all files under a storage prefix.
        supabase.storage.list(path) returns entries; folders typically have no mimetype.
        """
        out = []
        entries = supabase.storage.from_(ARTIFACT_BUCKET).list(path_prefix) or []
        for e in entries:
            name = e.get("name")
            if not name:
                continue

            full_path = f"{path_prefix}{name}"

            meta = (e.get("metadata") or {})
            mimetype = meta.get("mimetype")

            if mimetype:
                # It's a file
                out.append(full_path)
            else:
                # It's a folder
                out.extend(_list_all_files_recursive(full_path + "/"))
        return out

    # --------------------------
    # 1) Decide storage paths
    # --------------------------
    if full:
        storage_paths = _list_all_files_recursive(prefix)
        if not storage_paths:
            raise HTTPException(status_code=404, detail="No artifacts found in storage for this workflow.")
    else:
        # Existing behavior: use artifacts JSON
        row = (
            supabase.table("workflows")
            .select("artifacts")
            .eq("id", workflow_id)
            .single()
            .execute()
        )

        artifacts = (row.data or {}).get("artifacts") or {}
        if not artifacts:
            raise HTTPException(status_code=404, detail="No artifacts found for this workflow.")

        raw_paths = list(_iter_leaf_strings(artifacts))
        storage_paths = []
        seen = set()
        for p in raw_paths:
            sp = _normalize_storage_path(p)
            if not sp:
                continue
            if sp not in seen:
                storage_paths.append(sp)
                seen.add(sp)

        if not storage_paths:
            raise HTTPException(status_code=404, detail="No downloadable storage paths found in artifacts JSON.")

    # --------------------------
    # 2) Build ZIP in-memory
    # --------------------------
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for sp in storage_paths:
            try:
                res = supabase.storage.from_(ARTIFACT_BUCKET).download(sp)
                if not res:
                    continue

                # Put in zip with a friendly name relative to workflow folder if possible
                arcname = sp
                if sp.startswith(prefix):
                    arcname = sp[len(prefix):]

                zf.writestr(arcname, res)
            except Exception as e:
                zf.writestr(
                    f"errors/{sp.replace('/', '_')}.error.txt",
                    f"Failed to download {sp}\n{e}\n"
                )

    buf.seek(0)

    filename = f"workflow_{workflow_id}_artifacts.zip" if not full else f"workflow_{workflow_id}_artifacts_full.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/validation/instruments/register")
async def validation_register_instrument(payload: InstrumentRegisterIn, request: Request):
    user_id = _require_user_id(request)

    row = {
        "user_id": user_id,
        "nickname": payload.nickname.strip(),
        "vendor": payload.vendor,
        "model": payload.model,
        "instrument_type": payload.instrument_type.strip(),
        "transport": payload.transport.strip(),
        "interface": payload.interface.strip(),
        "resource_string": payload.resource_string.strip(),
        "scpi_idn": payload.scpi_idn,
        "capabilities": payload.capabilities or {},
        "metadata": payload.metadata or {},
    }

    # If this is the first instrument, or user asked, make it default
    # (unique partial index ensures only one default per user)
    existing = supabase.table("validation_instruments") \
        .select("id,is_default") \
        .eq("user_id", user_id) \
        .limit(1) \
        .execute()

    should_make_default = bool(payload.make_default) or (not existing.data)
    if should_make_default:
        # clear any existing default
        supabase.table("validation_instruments") \
            .update({"is_default": False}) \
            .eq("user_id", user_id) \
            .eq("is_default", True) \
            .execute()
        row["is_default"] = True

    # Upsert by (user_id, nickname) unique index
    res = supabase.table("validation_instruments") \
        .upsert(row, on_conflict="user_id,nickname") \
        .execute()

    return {"ok": True, "instrument": (res.data[0] if res.data else None)}

@app.get("/validation/instruments")
async def validation_list_instruments(request: Request, user_id: Optional[str] = None):
    """
    Backward compatible:
      - Apps call: /validation/instruments  (user_id from auth)
      - Studio can call: /validation/instruments?user_id=...
    Also supports legacy ownership stored in metadata.
    """
    effective_user_id = user_id or _require_user_id(request)

    try:
        # Primary: user_id column
        res = (
            supabase.table("validation_instruments")
            .select("*")
            .eq("user_id", effective_user_id)
            .order("created_at", desc=True)
            .execute()
        )
        instruments = res.data or []

        # Fallback: legacy rows where user_id is NULL but metadata stores ownership
        if not instruments:
            res2 = (
                supabase.table("validation_instruments")
                .select("*")
                .order("created_at", desc=True)
                .execute()
            )
            instruments = []
            for r in (res2.data or []):
                md = r.get("metadata") or {}
                owner = md.get("owner_user_id") or md.get("created_by")
                if owner == effective_user_id:
                    instruments.append(r)

        return {"ok": True, "instruments": instruments}

    except Exception as e:
        logger.exception("validation_list_instruments failed")
        return JSONResponse(status_code=500, content={"ok": False, "message": f"{type(e).__name__}: {e}"})


@app.get("/validation/instruments/default")
async def validation_get_default_instrument(request: Request):
    user_id = _require_user_id(request)

    try:
        # Primary: correct column
        res = (
            supabase.table("validation_instruments")
            .select("*")
            .eq("user_id", user_id)
            .eq("is_default", True)
            .limit(1)
            .execute()
        )

        if res.data:
            return {"ok": True, "instrument": res.data[0]}

        # Fallback: legacy metadata-based ownership
        res2 = (
            supabase.table("validation_instruments")
            .select("*")
            .eq("is_default", True)
            .execute()
        )

        for r in (res2.data or []):
            md = r.get("metadata") or {}
            owner = md.get("owner_user_id") or md.get("created_by")
            if owner == user_id:
                return {"ok": True, "instrument": r}

        return {"ok": True, "instrument": None}

    except Exception as e:
        logger.exception("validation_get_default_instrument failed")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": f"{type(e).__name__}: {e}"}
        )


@app.post("/validation/instruments/default")
async def validation_set_default_instrument(payload: InstrumentSetDefaultIn, request: Request):
    user_id = _require_user_id(request)

    if not payload.instrument_id and not payload.nickname:
        raise HTTPException(status_code=400, detail="Provide instrument_id or nickname")

    # resolve target instrument row
    q = supabase.table("validation_instruments").select("id").eq("user_id", user_id)
    if payload.instrument_id:
        q = q.eq("id", payload.instrument_id)
    else:
        q = q.eq("nickname", payload.nickname)

    found = q.limit(1).execute()
    if not found.data:
        raise HTTPException(status_code=404, detail="Instrument not found")

    target_id = found.data[0]["id"]

    # clear existing default, then set new default
    supabase.table("validation_instruments") \
        .update({"is_default": False}) \
        .eq("user_id", user_id) \
        .eq("is_default", True) \
        .execute()

    supabase.table("validation_instruments") \
        .update({"is_default": True}) \
        .eq("user_id", user_id) \
        .eq("id", target_id) \
        .execute()

    return {"ok": True, "default_instrument_id": target_id}

def _guess_capabilities(instrument_type: str) -> dict:
    t = (instrument_type or "").lower()
    if "scope" in t or "oscillo" in t:
        return {
            "category": "oscilloscope",
            "scpi": {"idn": "*IDN?", "meas_vpp": "MEAS:VPP?", "meas_freq": "MEAS:FREQ?"},
        }
    if "power" in t and ("supply" in t or "psu" in t):
        return {
            "category": "power_supply",
            "scpi": {"idn": "*IDN?", "set_v": "VOLT {ch},{v}", "set_i": "CURR {ch},{i}", "out": "OUTP {ch},{state}"},
        }
    if "dmm" in t or "multimeter" in t:
        return {
            "category": "dmm",
            "scpi": {"idn": "*IDN?", "meas_vdc": "MEAS:VOLT:DC?", "meas_idc": "MEAS:CURR:DC?"},
        }
    if "load" in t:
        return {
            "category": "electronic_load",
            "scpi": {"idn": "*IDN?", "mode_cc": "FUNC CURR", "set_i": "CURR {i}", "input": "INP {state}"},
        }
    return {"category": "generic", "scpi": {"idn": "*IDN?"}}


@app.post("/validation/instruments/{instrument_id}/probe")
async def validation_probe_instrument(instrument_id: str, request: Request):
    """
    Test connection to the instrument using its resource_string.
    MVP behavior:
      - If PyVISA is available AND VALIDATION_PROBE_MODE != "stub", try real *IDN?
      - Otherwise, return a mocked IDN so UI can proceed.
    Also stores scpi_idn + a capabilities_guess into validation_instruments.
    """
    user_id = _require_user_id(request)

    # 1) Load instrument row and verify ownership
    res = supabase.table("validation_instruments") \
        .select("*") \
        .eq("user_id", user_id) \
        .eq("id", instrument_id) \
        .limit(1) \
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Instrument not found")

    inst = res.data[0]
    resource = inst.get("resource_string")
    instrument_type = inst.get("instrument_type") or "generic"

    scpi_idn = None
    ok = False
    probe_mode = (os.environ.get("VALIDATION_PROBE_MODE") or "stub").lower()

    # 2) Try real probe (optional)
    if probe_mode != "stub":
        try:
            import pyvisa  # pip install pyvisa
            rm = pyvisa.ResourceManager()
            h = rm.open_resource(resource)
            # keep conservative defaults
            try:
                h.timeout = 3000  # ms
            except Exception:
                pass
            scpi_idn = str(h.query("*IDN?")).strip()
            try:
                h.close()
            except Exception:
                pass
            ok = True
        except Exception as e:
            # fall back to stub (don’t block the demo)
            ok = False
            scpi_idn = f"PROBE_FAILED({type(e).__name__})"

    # 3) Stub probe (default)
    if not ok and probe_mode == "stub":
        scpi_idn = inst.get("scpi_idn") or "MOCK,INSTRUMENT,0,1"
        ok = True

    capabilities_guess = _guess_capabilities(instrument_type)

    # 4) Persist probe results for “feels real” UX
    supabase.table("validation_instruments") \
        .update({
            "scpi_idn": scpi_idn,
            "capabilities": capabilities_guess,
            "updated_at": datetime.utcnow().isoformat(),
        }) \
        .eq("user_id", user_id) \
        .eq("id", instrument_id) \
        .execute()

    return {"ok": ok, "scpi_idn": scpi_idn, "capabilities_guess": capabilities_guess}

@app.post("/validation/test_plan/preview")
async def validation_test_plan_preview(request: Request):
    """
    Generates validation test plan JSON from datasheet text (preview step for Scope modal).
    Reuses Validation Test Plan Agent.
    """
    data = await request.json()
    workflow_id = data.get("workflow_id")
    datasheet_text = (data.get("datasheet_text") or "").strip()
    goal = data.get("goal") or "Create a validation test plan"

    if not workflow_id or not datasheet_text:
        return {"status": "error", "message": "workflow_id and datasheet_text are required"}

    # reuse existing agent
    from agents.validation.validation_test_plan_agent import run_agent as validation_test_plan_agent

    state = {
        "workflow_id": workflow_id,
        "datasheet_text": datasheet_text,
        "goal": goal,
        "preview_only": True,
        "loop_type": "validation",
    }
    out_state = _execute_agent_with_runtime(
        "Validation Test Plan Agent",
        validation_test_plan_agent,
        state,
        workflow_id,
        state.get("run_id"),
        "validation",
    )

    plan = out_state.get("test_plan") or {}
    return {"status": "ok", "test_plan": plan}

@app.post("/validation/benches")
def create_validation_bench(request: Request, payload: BenchCreateIn):
    user_id = _require_user_id(request)  # your helper enforces auth :contentReference[oaicite:1]{index=1}

    if not payload.instrument_ids:
        raise HTTPException(status_code=400, detail="instrument_ids is required")

    # 1) Create bench
    bench_row = {
        "org_id": payload.org_id,
        "name": payload.name,
        "location": payload.location,
        "status": "offline",
        "metadata": {
            **(payload.metadata or {}),
            "owner_user_id": user_id,  # MVP ownership (since instruments are user_id-based)
        },
    }
    bench_res = supabase.table("validation_benches").insert(bench_row).execute()
    bench = (bench_res.data or [None])[0]
    if not bench:
        raise HTTPException(status_code=500, detail="Failed to create bench")

    bench_id = bench["id"]

    # 2) Save bench connections (schematic/connectivity intent)
    supabase.table("validation_bench_connections").insert({
        "bench_id": bench_id,
        "schematic": payload.schematic or {},
    }).execute()

    # 3) Fetch instruments (must belong to this user)
    inst_res = (
        supabase.table("validation_instruments")
        .select("*")
        .in_("id", payload.instrument_ids)
        .eq("user_id", user_id)
        .execute()
    )
    instruments = inst_res.data or []
    if len(instruments) != len(payload.instrument_ids):
        raise HTTPException(status_code=403, detail="One or more instruments not owned by user")

    # 4) Create bench ↔ instrument mappings
    # Optional: store role as instrument_type ("psu" / "dmm" / "scope")
    map_rows = []
    for inst in instruments:
        map_rows.append({
            "bench_id": bench_id,
            "instrument_id": inst["id"],
            "role": inst.get("instrument_type"),
        })
    supabase.table("validation_bench_instruments").insert(map_rows).execute()

    # 5) Derive bench capabilities (simple MVP: one row per instrument_type)
    caps_by_type: Dict[str, Any] = {}
    for inst in instruments:
        t = inst.get("instrument_type") or "unknown"
        caps_by_type.setdefault(t, {"count": 0, "instruments": []})
        caps_by_type[t]["count"] += 1
        caps_by_type[t]["instruments"].append({
            "id": inst["id"],
            "nickname": inst.get("nickname"),
            "vendor": inst.get("vendor"),
            "model": inst.get("model"),
            "resource_string": inst.get("resource_string"),
            "capabilities": inst.get("capabilities") or {},
        })

    cap_rows = [{"bench_id": bench_id, "capability": k, "details": v} for k, v in caps_by_type.items()]
    if cap_rows:
        supabase.table("validation_bench_capabilities").insert(cap_rows).execute()

    return {"ok": True, "bench_id": bench_id}


from typing import Optional
from fastapi import Request

from typing import Optional
from fastapi import Request

@app.get("/validation/test_plans")
async def list_validation_test_plans(request: Request, user_id: Optional[str] = None):
    """
    Backward compatible:
      - Studio can call: /validation/test_plans?user_id=...
      - Apps call:       /validation/test_plans  (no user_id; derived from auth)
    """
    if not supabase:
        return JSONResponse(status_code=500, content={"status": "error", "message": "Supabase not configured"})

    try:
        effective_user_id = user_id or _require_user_id(request)

        resp = (
            supabase.table("validation_test_plans")
            # include is_active so UI can filter later
            .select("id,name,description,is_active,created_at")
            .eq("user_id", effective_user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"status": "ok", "plans": resp.data or []}

    except Exception as e:
        logger.exception("list_validation_test_plans failed")
        return JSONResponse(status_code=500, content={"status": "error", "message": f"{type(e).__name__}: {e}"})


@app.get("/validation/benches")
async def list_validation_benches(request: Request):
    """
    Return benches owned by the current user.
    For MVP, ownership is stored inside metadata.
    We support BOTH keys:
      - metadata.owner_user_id  (older expectation)
      - metadata.created_by     (what create agent currently writes)
    """
    user_id = _require_user_id(request)

    try:
        res = (
            supabase.table("validation_benches")
            # ✅ match your real table columns (no updated_at)
            .select("id,org_id,name,location,status,metadata,created_at")
            .order("created_at", desc=True)
            .execute()
        )

        benches = []
        for b in (res.data or []):
            md = b.get("metadata") or {}
            owner = md.get("owner_user_id") or md.get("created_by")
            if owner == user_id:
                benches.append(b)

        return {"ok": True, "benches": benches}

    except Exception as e:
        logger.exception("list_validation_benches failed")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": f"{type(e).__name__}: {e}"},
        )
# ==========================================================
# ✅ ANALOG APPS — same pattern as Validation Run App
# ==========================================================

class AnalogAppIn(BaseModel):
    datasheet_text: str
    goal: Optional[str] = None
    scope: Optional[Dict[str, Any]] = None
    toggles: Optional[Dict[str, bool]] = None
    model_style: Optional[str] = None  # "sv_rnm" | "verilog_a"
    from_workflow_id: Optional[str] = None

def execute_analog_app_background(
    workflow_id: str,
    run_id: str,
    user_id: str,
    artifact_dir: str,
    template_workflow_name: str,
    payload: Dict[str, Any],
):
    try:
        os.makedirs(artifact_dir, exist_ok=True)

        shared_state = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "artifact_dir": artifact_dir,
            "supabase_client": supabase,
            "user_id": user_id,
        }
        for k, v in (payload or {}).items():
            if v is not None:
                shared_state[k] = v

        # Normalize: allow either datasheet_text or spec/spec_text
        ds = (shared_state.get("datasheet_text") or shared_state.get("spec_text") or shared_state.get("spec") or "").strip()
        if ds:
            shared_state["datasheet_text"] = ds
            shared_state["spec"] = ds

        append_log_workflow(workflow_id, f"▶️ Phase: {template_workflow_name}", phase="start")
        append_log_run(run_id, f"▶️ Phase: {template_workflow_name}")

        defn = _load_workflow_def_by_name(
            template_workflow_name,
            user_id=user_id,
            force_platform_definition=True,
        )
        nodes = _definition_to_executor_nodes(defn)

        _run_nodes_with_shared_state(
            workflow_id=workflow_id,
            run_id=run_id,
            loop_type="analog",
            nodes=nodes,
            shared_state=shared_state,
        )

        append_log_workflow(workflow_id, "🎉 Analog App complete", status="completed", phase="done")
        append_log_run(run_id, "🎉 Analog App complete", status="completed")

    except Exception as e:
        err = f"❌ Analog App crashed: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")

def _start_analog_app(background_tasks: BackgroundTasks, request: Request, payload: AnalogAppIn, app_name: str, template_workflow_name: str):
    user_id = _require_user_id(request)

    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    supabase.table("workflows").insert({
        "id": workflow_id,
        "user_id": user_id,
        "name": app_name,
        "status": "running",
        "phase": "queued",
        "logs": "🚀 App run queued.",
        "created_at": now,
        "updated_at": now,
        "artifacts": {},
        "loop_type": "analog",
        "definitions": {"app_intent": template_workflow_name, "payload": payload.dict()},
    }).execute()

    user_folder = str(user_id or "anonymous")
    artifact_dir = os.path.join("artifacts", user_folder, workflow_id, run_id)
    os.makedirs(artifact_dir, exist_ok=True)

    supabase.table("runs").insert({
        "id": run_id,
        "user_id": user_id,
        "workflow_id": workflow_id,
        "loop_type": "analog",
        "status": "running",
        "logs": "🚀 App run started.",
        "artifacts_path": artifact_dir,
        "created_at": now
    }).execute()

    append_log_workflow(workflow_id, f"🚀 Starting {app_name}", phase="start")
    append_log_run(run_id, f"🚀 Starting {app_name}")

    background_tasks.add_task(
        execute_analog_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        template_workflow_name,
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}

@app.post("/apps/analog/spec/run")
async def apps_analog_spec(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Spec", "Analog_SpecBuilder")

@app.post("/apps/analog/netlist/run")
async def apps_analog_netlist(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Netlist", "Analog_NetlistScaffold")

@app.post("/apps/analog/model/run")
async def apps_analog_model(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Behavioral Model", "Analog_BehavioralModel")

@app.post("/apps/analog/validate-model/run")
async def apps_analog_validate_model(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Model Validation", "Analog_ModelValidation")

@app.post("/apps/analog/correlate/run")
async def apps_analog_correlate(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Correlate", "Analog_Correlation")

@app.post("/apps/analog/iterate/run")
async def apps_analog_iterate(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Iterate", "Analog_Iterate")

@app.post("/apps/analog/abstracts/run")
async def apps_analog_abstracts(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Abstract Views", "Analog_Abstracts")

@app.post("/apps/analog/run")
async def apps_analog_run(request: Request, background_tasks: BackgroundTasks, payload: AnalogAppIn):
    return _start_analog_app(background_tasks, request, payload, "App: Analog Run", "Analog_Run")


# ==========================================================
# ✅ EMBEDDED APPS — same pattern as Analog Apps
# ==========================================================

class EmbeddedAppIn(BaseModel):
    spec_text: str
    goal: Optional[str] = None
    toolchain: Dict[str, str] = Field(default_factory=dict)
    toggles: Optional[Dict[str, bool]] = None
    from_workflow_id: Optional[str] = None
    from_run_id: Optional[str] = None
    source_verification_workflow_id: Optional[str] = None
    source_verification_run_id: Optional[str] = None
    top_module: Optional[str] = None


def execute_embedded_app_background(
    workflow_id: str,
    run_id: str,
    user_id: str,
    artifact_dir: str,
    template_workflow_name: str,
    payload: Dict[str, Any],
):
    try:
        os.makedirs(artifact_dir, exist_ok=True)

        shared_state = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "artifact_dir": artifact_dir,
            "supabase_client": supabase,
            "user_id": user_id,
        }

        # Inject payload -> shared_state
        for k, v in (payload or {}).items():
            if v is not None:
                shared_state[k] = v

        # Normalize: allow spec_text/spec/datasheet_text to converge
        spec = (shared_state.get("spec_text") or shared_state.get("spec") or shared_state.get("datasheet_text") or "").strip()
        if spec:
            shared_state["spec_text"] = spec
            shared_state["spec"] = spec

        # Ensure toolchain dict exists even if UI sends nothing
        if not isinstance(shared_state.get("toolchain"), dict):
            shared_state["toolchain"] = {}
        toggles = shared_state.get("toggles") or {}
        if template_workflow_name == "Embedded_Run" and bool(toggles.get("enable_cosim")):
            shared_state["execute_cosim"] = True

        append_log_workflow(workflow_id, f"▶️ Phase: {template_workflow_name}", phase="start")
        append_log_run(run_id, f"▶️ Phase: {template_workflow_name}")

        defn = _load_workflow_def_by_name(
            template_workflow_name,
            user_id=user_id,
            force_platform_definition=True,
        )
        nodes = _definition_to_executor_nodes(defn)

        _run_nodes_with_shared_state(
            workflow_id=workflow_id,
            run_id=run_id,
            loop_type="system" if template_workflow_name == "Embedded_Run" else "embedded",
            nodes=nodes,
            shared_state=shared_state,
        )

        append_log_workflow(workflow_id, "🎉 Embedded App complete", status="completed", phase="done")
        append_log_run(run_id, "🎉 Embedded App complete", status="completed")

    except Exception as e:
        err = f"❌ Embedded App crashed: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")


def _start_embedded_app(
    background_tasks: BackgroundTasks,
    request: Request,
    payload: EmbeddedAppIn,
    app_name: str,
    template_workflow_name: str,
):
    user_id = _require_user_id(request)

    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    # Parent workflow row (single job)
    supabase.table("workflows").insert({
        "id": workflow_id,
        "user_id": user_id,
        "name": app_name,
        "status": "running",
        "phase": "queued",
        "logs": "🚀 App run queued.",
        "created_at": now,
        "updated_at": now,
        "artifacts": {},
        "loop_type": "embedded",
        "definitions": {"app_intent": template_workflow_name, "payload": payload.dict()},
    }).execute()

    # Run row
    user_folder = str(user_id or "anonymous")
    artifact_dir = os.path.join("artifacts", user_folder, workflow_id, run_id)
    os.makedirs(artifact_dir, exist_ok=True)

    supabase.table("runs").insert({
        "id": run_id,
        "user_id": user_id,
        "workflow_id": workflow_id,
        "loop_type": "embedded",
        "status": "running",
        "logs": "🚀 App run started.",
        "artifacts_path": artifact_dir,
        "created_at": now
    }).execute()

    append_log_workflow(workflow_id, f"🚀 Starting {app_name}", phase="start")
    append_log_run(run_id, f"🚀 Starting {app_name}")

    background_tasks.add_task(
        execute_embedded_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        template_workflow_name,
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


def _start_system_app(background_tasks, request, payload, app_name, template_workflow_name):
    user_id = _require_user_id(request)

    workflow_id, run_id, base_dir = _create_app_workflow_and_run(
        user_id,
        app_name,
        "system"
    )

    artifact_dir = os.path.join(base_dir, "system")
    os.makedirs(artifact_dir, exist_ok=True)

    append_log_workflow(workflow_id, f"🚀 Starting {app_name}", phase="start")
    append_log_run(run_id, f"🚀 Starting {app_name}")

    background_tasks.add_task(
        execute_system_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        template_workflow_name,
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


def _start_system_software_validation_app(
    background_tasks: BackgroundTasks,
    request: Request,
    payload: SystemSoftwareValidationAppIn,
):
    user_id = _require_user_id(request)

    mode = (payload.validation_mode or "software_package_validation").strip()

    if mode not in {"software_package_validation", "full_co_simulation"}:
        raise HTTPException(
            status_code=400,
            detail="validation_mode must be 'software_package_validation' or 'full_co_simulation'"
        )

    if not payload.system_software_workflow_id:
        raise HTTPException(status_code=400, detail="system_software_workflow_id is required")

    if mode == "full_co_simulation":
        if not payload.system_firmware_workflow_id:
            raise HTTPException(
                status_code=400,
                detail="system_firmware_workflow_id is required for full co-simulation"
            )
        if not payload.system_rtl_workflow_id:
            raise HTTPException(
                status_code=400,
                detail="system_rtl_workflow_id is required for full co-simulation"
            )

    app_title = (
        "App: System Software Validation - Full Co-Simulation"
        if mode == "full_co_simulation"
        else "App: System Software Validation - Software Package Validation"
    )

    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, app_title, "system")

    artifact_dir = os.path.join(base_dir, "system-software-validation")
    os.makedirs(artifact_dir, exist_ok=True)

    template_workflow_name = (
        "System_Software_Validation_L2"
        if mode == "full_co_simulation"
        else "System_Software_Validation_L1"
    )

    payload_dict = payload.dict()
    payload_dict["app_name"] = "system_software_validation"
    payload_dict["validation_scope"] = (
        "full_stack" if mode == "full_co_simulation" else "software_only"
    )
    payload_dict["co_sim_enabled"] = (mode == "full_co_simulation")

    payload_dict["source_software_workflow_id"] = payload.system_software_workflow_id
    payload_dict["system_software_workflow_id"] = payload.system_software_workflow_id

    if payload.system_firmware_workflow_id:
        payload_dict["source_firmware_workflow_id"] = payload.system_firmware_workflow_id
        payload_dict["system_firmware_workflow_id"] = payload.system_firmware_workflow_id

    if payload.system_rtl_workflow_id:
        payload_dict["source_rtl_workflow_id"] = payload.system_rtl_workflow_id
        payload_dict["system_rtl_workflow_id"] = payload.system_rtl_workflow_id

    background_tasks.add_task(
        execute_system_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        template_workflow_name,
        payload_dict,
        "system-software-validation",
    )

    return {
        "ok": True,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "validation_mode": mode,
        "template_workflow": template_workflow_name,
    }


def execute_system_app_background(
    workflow_id,
    run_id,
    user_id,
    artifact_dir,
    template_workflow_name,
    payload,
    app_name=None,
):
    try:
        os.makedirs(artifact_dir, exist_ok=True)

        shared_state = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "artifact_dir": artifact_dir,
            "workflow_dir": os.path.dirname(artifact_dir),
            "supabase_client": supabase,
            "user_id": user_id,
            "app_name": app_name,
            "loop_type": "system",
            "template_workflow": template_workflow_name,
            "template_workflow_name": template_workflow_name,
            "workflow_name": template_workflow_name,
            "_fail_fast_on_agent_error": template_workflow_name in {
                "System_Architecture_Explorer",
                "System_Cache_Tuning",
                "System_ISA_Compare",
                "System_Memory_Bottleneck",
                "System_CPU_Model",
                "System_Architecture_to_RTL_Delivery",
                "System_RTL",
                "System_Synthesis",
                "System_PD",
            },
        }

        # ---------------------------------------------------------
        # 1) Raw payload passthrough
        # ---------------------------------------------------------
        for k, v in (payload or {}).items():
            if v is not None:
                shared_state[k] = v

        rtl_source_mode = str(shared_state.get("rtl_source_mode") or "").strip().lower()
        if rtl_source_mode in {"from_system_rtl", "from_workflow", "workflow"}:
            source_id = str(shared_state.get("system_rtl_workflow_id") or shared_state.get("from_workflow_id") or "").strip()
            if source_id:
                shared_state["system_rtl_workflow_id"] = source_id
        elif shared_state.get("system_rtl_workflow_id") and not rtl_source_mode:
            rtl_source_mode = "from_system_rtl"
        if rtl_source_mode:
            shared_state["rtl_source_mode"] = rtl_source_mode

        requested_system_top = str(
            shared_state.get("top_module")
            or shared_state.get("system_top_module")
            or shared_state.get("soc_top_name")
            or ""
        ).strip()
        if requested_system_top:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", requested_system_top):
                raise ValueError(f"Requested System RTL top_module '{requested_system_top}' is not a valid Verilog identifier.")
            shared_state["system_requested_top_module"] = requested_system_top
            shared_state["system_top_module"] = requested_system_top
            shared_state["soc_top_name"] = requested_system_top
            # In System RTL this field means SoC wrapper top, not the digital IP top.
            # Avoid forcing Digital Spec Agent to rename the digital block.
            shared_state.pop("top_module", None)

        def _find_first_existing_file(candidates: List[str]) -> str:
            for candidate in candidates:
                if candidate and os.path.exists(candidate):
                    return candidate
            return ""

        def _resolve_filelist_lines(filelist_path: str, roots: List[str]) -> List[str]:
            lines: List[str] = []
            try:
                with open(filelist_path, "r", encoding="utf-8", errors="ignore") as fh:
                    raw_lines = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]
            except Exception:
                raw_lines = []
            search_roots = list(dict.fromkeys([os.path.dirname(filelist_path), *[r for r in roots if r]]))
            for raw in raw_lines:
                expanded = os.path.expandvars(raw)
                if os.path.isabs(expanded):
                    lines.append(os.path.abspath(expanded))
                    continue
                resolved = ""
                for root in search_roots:
                    candidate = os.path.abspath(os.path.join(root, expanded))
                    if os.path.exists(candidate):
                        resolved = candidate
                        break
                lines.append(resolved or os.path.abspath(os.path.join(os.path.dirname(filelist_path), expanded)))
            return list(dict.fromkeys(lines))

        def _module_names_from_rtl(path: str) -> List[str]:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except Exception:
                return []
            return re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)

        def _rtl_file_preference(path: str) -> Tuple[int, str]:
            stem = os.path.splitext(os.path.basename(path))[0].lower()
            is_intermediate = bool(re.search(r"(?:^|_)pass\d+$", stem) or re.search(r"_pass\d+(?:_|$)", stem))
            return (1 if is_intermediate else 0, os.path.basename(path).lower())

        def _dedupe_system_rtl_files(filelist_lines: List[str]) -> List[str]:
            existing = [
                os.path.abspath(p)
                for p in dict.fromkeys(str(p) for p in filelist_lines if isinstance(p, str) and str(p).strip())
                if os.path.exists(p)
            ]
            if not existing:
                return []
            modules_by_path = {path: _module_names_from_rtl(path) for path in existing}
            owner_by_module: Dict[str, str] = {}
            for path, modules in modules_by_path.items():
                for module in modules:
                    prior = owner_by_module.get(module)
                    if not prior or _rtl_file_preference(path) < _rtl_file_preference(prior):
                        owner_by_module[module] = path
            cleaned: List[str] = []
            for path in existing:
                modules = modules_by_path.get(path) or []
                if modules and not any(owner_by_module.get(module) == path for module in modules):
                    continue
                cleaned.append(path)
            return list(dict.fromkeys(cleaned))

        def _materialize_system_rtl_import(
            filelist_lines: List[str],
            source_soc_top: str = "",
            source_phys_top: str = "",
            source_intent: str = "",
        ) -> None:
            integration_dir = os.path.join(shared_state["workflow_dir"], "system", "integration")
            os.makedirs(integration_dir, exist_ok=True)
            normalized = _dedupe_system_rtl_files(filelist_lines)
            local_filelist = os.path.join(integration_dir, "system_rtl_filelist_sim.txt")
            with open(local_filelist, "w", encoding="utf-8") as fh:
                fh.write("\n".join(normalized) + ("\n" if normalized else ""))
            local_phys_filelist = os.path.join(integration_dir, "system_rtl_filelist_phys.txt")
            with open(local_phys_filelist, "w", encoding="utf-8") as fh:
                fh.write("\n".join(normalized) + ("\n" if normalized else ""))
            shared_state["system_rtl_filelist_sim"] = local_filelist
            shared_state["system_rtl_filelist_phys"] = local_phys_filelist
            shared_state["system_rtl_files"] = normalized
            shared_state["rtl_inputs"] = normalized
            shared_state["rtl_files"] = normalized
            shared_state["source_rtl_files"] = normalized
            digital_state = shared_state.setdefault("digital", {})
            if isinstance(digital_state, dict):
                digital_state["rtl_files"] = normalized
            if source_soc_top and os.path.exists(source_soc_top):
                shared_state["soc_top_sim_path"] = os.path.abspath(source_soc_top)
                shared_state["system_top_sim_path"] = os.path.abspath(source_soc_top)
            if source_phys_top and os.path.exists(source_phys_top):
                shared_state["soc_top_phys_path"] = os.path.abspath(source_phys_top)
                shared_state["system_top_phys_path"] = os.path.abspath(source_phys_top)
            if source_intent and os.path.exists(source_intent):
                shared_state["system_integration_intent_json"] = os.path.abspath(source_intent)

        def _materialized_system_rtl_files() -> List[str]:
            candidates: List[str] = []
            value = shared_state.get("system_rtl_files")
            if isinstance(value, list):
                candidates.extend(str(p) for p in value if str(p).strip())
            filelist_value = shared_state.get("system_rtl_filelist_sim")
            if isinstance(filelist_value, list):
                candidates.extend(str(p) for p in filelist_value if str(p).strip())
            elif isinstance(filelist_value, str) and os.path.exists(filelist_value):
                candidates.extend(_resolve_filelist_lines(filelist_value, [shared_state["workflow_dir"]]))
            return _dedupe_system_rtl_files(candidates)

        def _storage_path_candidates_from_source_path(source_path: str, indexed_paths: List[str], source_workflow_id: str) -> List[str]:
            raw = str(source_path or "").strip().replace("\\", "/")
            candidates: List[str] = []
            if not raw:
                return candidates
            if raw.startswith("backend/workflows/") or raw.startswith("artifacts/"):
                candidates.append(raw)
            marker = "/artifacts/"
            if marker in raw:
                candidates.append("artifacts/" + raw.split(marker, 1)[1].lstrip("/"))
            basename = os.path.basename(raw)
            suffixes = []
            for key in ("/digital/", "/system/", "/analog/", "/rtl/", "/vv/"):
                if key in raw:
                    suffixes.append(raw.split(key, 1)[1].lstrip("/"))
                    suffixes.append(key.strip("/") + "/" + raw.split(key, 1)[1].lstrip("/"))
            for path in indexed_paths:
                norm = str(path or "").replace("\\", "/")
                if not norm:
                    continue
                if basename and norm.endswith("/" + basename):
                    candidates.append(norm)
                if any(norm.endswith("/" + suffix) for suffix in suffixes if suffix):
                    candidates.append(norm)
                if source_workflow_id and source_workflow_id in norm and basename and norm.endswith(basename):
                    candidates.append(norm)
            return list(dict.fromkeys([c for c in candidates if c]))

        def _materialize_system_rtl_import_from_supabase(source_workflow_id: str) -> bool:
            try:
                wf = (
                    supabase.table("workflows")
                    .select("id,user_id,artifacts")
                    .eq("id", source_workflow_id)
                    .single()
                    .execute()
                )
                wf_row = wf.data or {}
            except Exception:
                wf_row = {}

            indexed_paths: List[str] = []

            def _collect_artifact_paths(obj: Any) -> None:
                if isinstance(obj, dict):
                    for value in obj.values():
                        _collect_artifact_paths(value)
                elif isinstance(obj, list):
                    for value in obj:
                        _collect_artifact_paths(value)
                elif isinstance(obj, str):
                    value = obj.strip().replace("\\", "/")
                    if value:
                        indexed_paths.append(value)

            _collect_artifact_paths((wf_row or {}).get("artifacts") or {})
            indexed_paths = list(dict.fromkeys(indexed_paths))
            user_id_for_source = str((wf_row or {}).get("user_id") or user_id or "").strip()
            prefixes = [
                f"backend/workflows/{source_workflow_id}/",
            ]
            if user_id_for_source:
                prefixes.extend([
                    f"artifacts/{user_id_for_source}/{source_workflow_id}/",
                    f"{user_id_for_source}/{source_workflow_id}/",
                ])

            allow_local_handoff_cache = str(os.getenv("CHIPLOOP_ALLOW_LOCAL_HANDOFF_CACHE", "")).strip().lower() in {"1", "true", "yes"}

            def _source_local_roots() -> List[str]:
                if not allow_local_handoff_cache:
                    return []
                roots = [os.path.abspath(os.path.join("backend", "workflows", source_workflow_id))]
                try:
                    response = (
                        supabase.table("runs")
                        .select("artifacts_path")
                        .eq("workflow_id", source_workflow_id)
                        .order("created_at", desc=True)
                        .limit(5)
                        .execute()
                    )
                    rows = response.data or []
                    if isinstance(rows, dict):
                        rows = [rows]
                    for row in rows:
                        if not isinstance(row, dict) or not row.get("artifacts_path"):
                            continue
                        root = os.path.abspath(str(row["artifacts_path"]))
                        roots.extend([
                            root,
                            os.path.dirname(root),
                            os.path.join(root, "system"),
                            os.path.join(root, "digital"),
                            os.path.join(os.path.dirname(root), "system"),
                            os.path.join(os.path.dirname(root), "digital"),
                        ])
                except Exception:
                    pass
                return list(dict.fromkeys([r for r in roots if r]))

            local_roots = _source_local_roots()

            def _first_local_text(rel_paths: List[str]) -> Tuple[str, str]:
                if not allow_local_handoff_cache:
                    return "", ""
                for rel in rel_paths:
                    rel_norm = str(rel or "").strip().replace("\\", "/").lstrip("/")
                    if not rel_norm:
                        continue
                    for root in local_roots:
                        candidate = os.path.abspath(os.path.join(root, rel_norm))
                        if os.path.isfile(candidate):
                            try:
                                with open(candidate, "r", encoding="utf-8", errors="ignore") as fh:
                                    return fh.read(), candidate
                            except Exception:
                                continue
                return "", ""

            def _local_rtl_files() -> List[str]:
                if not allow_local_handoff_cache:
                    return []
                found: List[str] = []
                for root in local_roots:
                    if not os.path.isdir(root):
                        continue
                    for dirpath, _, filenames in os.walk(root):
                        norm_dir = dirpath.replace("\\", "/").lower()
                        if any(skip in norm_dir for skip in ("/vv/", "/tb/", "/testbench/", "/node_modules/")):
                            continue
                        for filename in filenames:
                            if filename.lower().endswith((".sv", ".v", ".svh", ".vh")):
                                found.append(os.path.abspath(os.path.join(dirpath, filename)))
                return list(dict.fromkeys(found))

            def _download_text_storage(path: str) -> str:
                try:
                    raw = supabase.storage.from_(ARTIFACT_BUCKET).download(path)
                    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                except Exception:
                    return ""

            def _download_bytes_storage(path: str) -> bytes:
                try:
                    raw = supabase.storage.from_(ARTIFACT_BUCKET).download(path)
                    return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
                except Exception:
                    return b""

            def _list_storage_folder(folder: str) -> List[str]:
                try:
                    entries = supabase.storage.from_(ARTIFACT_BUCKET).list(folder) or []
                except Exception:
                    return []
                paths: List[str] = []
                for entry in entries:
                    name = entry.get("name") if isinstance(entry, dict) else None
                    if name:
                        paths.append(f"{folder.rstrip('/')}/{name}")
                return paths

            def _list_storage_tree(folder: str, depth: int = 0, max_depth: int = 7) -> List[str]:
                if depth > max_depth:
                    return []
                paths: List[str] = []
                for path in _list_storage_folder(folder):
                    paths.append(path)
                    paths.extend(_list_storage_tree(path, depth + 1, max_depth))
                return paths

            for prefix in prefixes:
                indexed_paths.extend(_list_storage_tree(prefix.rstrip("/")))
            indexed_paths = list(dict.fromkeys(indexed_paths))

            def _download_first(rel_paths: List[str]) -> Tuple[str, str]:
                candidate_paths: List[str] = []
                for rel in rel_paths:
                    rel = str(rel or "").strip().replace("\\", "/")
                    if not rel:
                        continue
                    if rel.startswith("backend/workflows/") or rel.startswith("artifacts/"):
                        candidate_paths.append(rel)
                    else:
                        candidate_paths.extend([f"{p.rstrip('/')}/{rel}" for p in prefixes])
                        candidate_paths.append(rel)
                        candidate_paths.extend([
                            path for path in indexed_paths
                            if path.endswith("/" + rel) or path.endswith(rel)
                        ])
                for candidate in list(dict.fromkeys(candidate_paths)):
                    text = _download_text_storage(candidate)
                    if text:
                        return text, candidate
                return "", ""

            package_text, package_path = _download_first([
                "system/package/system_rtl_package.json",
                "digital/system/package/system_rtl_package.json",
            ])
            if not package_text:
                package_text, package_path = _first_local_text([
                    "system/package/system_rtl_package.json",
                    "digital/system/package/system_rtl_package.json",
                    "package/system_rtl_package.json",
                    "system_rtl_package.json",
                ])
            package_obj: Dict[str, Any] = {}
            if package_text:
                try:
                    loaded = json.loads(package_text)
                    if isinstance(loaded, dict):
                        package_obj = loaded
                except Exception:
                    package_obj = {}

            def _materialize_macro_collateral(package: Dict[str, Any]) -> None:
                storage_obj_local = package.get("storage") if isinstance(package.get("storage"), dict) else {}
                macros = package.get("analog_macros") if isinstance(package.get("analog_macros"), dict) else {}

                def _as_list(value: Any) -> List[str]:
                    return [str(x) for x in value if x is not None and str(x).strip()] if isinstance(value, list) else []

                rel_groups = {
                    "macro_lefs": _as_list(storage_obj_local.get("macro_lefs")) + _as_list(macros.get("lef_files")),
                    "macro_libs": _as_list(storage_obj_local.get("macro_libs")) + _as_list(macros.get("lib_files")),
                    "macro_gds": _as_list(storage_obj_local.get("macro_gds")) + _as_list(macros.get("gds_files")),
                }
                contract_paths = _as_list([storage_obj_local.get("analog_macro_interface_contract")])
                if not any(rel_groups.values()):
                    rel_groups_empty = True
                else:
                    rel_groups_empty = False

                macro_dir = os.path.join(shared_state["workflow_dir"], "system", "imported_macros")
                os.makedirs(macro_dir, exist_ok=True)
                materialized_groups: Dict[str, List[str]] = {"macro_lefs": [], "macro_libs": [], "macro_gds": []}

                for key, paths in rel_groups.items():
                    for idx, source_path in enumerate(list(dict.fromkeys(paths))):
                        raw_source = str(source_path or "").strip().replace("\\", "/")
                        if not raw_source:
                            continue
                        content = b""
                        if os.path.isfile(raw_source):
                            try:
                                with open(raw_source, "rb") as fh:
                                    content = fh.read()
                            except Exception:
                                content = b""
                        else:
                            candidates = _storage_path_candidates_from_source_path(raw_source, indexed_paths, source_workflow_id)
                            for candidate in candidates or [raw_source]:
                                content = _download_bytes_storage(candidate)
                                if content:
                                    break
                        if not content:
                            continue
                        basename = os.path.basename(raw_source) or f"{key}_{idx}"
                        local_path = os.path.abspath(os.path.join(macro_dir, basename))
                        with open(local_path, "wb") as fh:
                            fh.write(content)
                        materialized_groups[key].append(local_path)

                for idx, source_path in enumerate(list(dict.fromkeys(contract_paths))):
                    raw_source = str(source_path or "").strip().replace("\\", "/")
                    if not raw_source:
                        continue
                    content = b""
                    candidates = _storage_path_candidates_from_source_path(raw_source, indexed_paths, source_workflow_id)
                    for candidate in candidates or [raw_source]:
                        content = _download_bytes_storage(candidate)
                        if content:
                            break
                    if not content:
                        continue
                    local_path = os.path.abspath(os.path.join(macro_dir, "analog_macro_interface_contract.json"))
                    with open(local_path, "wb") as fh:
                        fh.write(content)
                    try:
                        loaded_contract = json.loads(content.decode("utf-8"))
                        if isinstance(loaded_contract, dict):
                            shared_state["analog_macro_interface_contract"] = loaded_contract
                            shared_state["analog_macro_interface_contract_path"] = local_path
                    except Exception:
                        shared_state["analog_macro_interface_contract_path"] = local_path

                if rel_groups_empty and not contract_paths:
                    return

                digital_block = shared_state.setdefault("digital", {})
                if isinstance(digital_block, dict):
                    for key, paths in materialized_groups.items():
                        if paths:
                            digital_block[key] = list(dict.fromkeys((digital_block.get(key) or []) + paths))
                    if (digital_block.get("macro_lefs") or digital_block.get("macro_libs")) and not digital_block.get("macro_gds"):
                        digital_block["macro_blackbox_mode"] = "lef_lib_no_gds"
                        digital_block["macro_blackboxes"] = [
                            os.path.splitext(os.path.basename(p))[0]
                            for p in (digital_block.get("macro_lefs") or [])
                        ]
                        shared_state["physical_blackbox_macros"] = digital_block["macro_blackboxes"]

            filelist_paths = []
            filelists = package_obj.get("filelists") if isinstance(package_obj.get("filelists"), dict) else {}
            storage_obj = package_obj.get("storage") if isinstance(package_obj.get("storage"), dict) else {}
            if package_obj:
                _materialize_macro_collateral(package_obj)
            storage_rtl_from_package = storage_obj.get("rtl_files") if isinstance(storage_obj, dict) else []
            if isinstance(storage_rtl_from_package, list):
                filelist_paths.extend([str(p) for p in storage_rtl_from_package if str(p).strip()])
            sim_from_package = filelists.get("sim") if isinstance(filelists, dict) else []
            if isinstance(sim_from_package, list):
                filelist_paths.extend([str(p) for p in sim_from_package if str(p).strip()])

            if not filelist_paths:
                filelist_text, _ = _download_first([
                    "system/integration/system_rtl_filelist_sim.txt",
                    "digital/system/integration/system_rtl_filelist_sim.txt",
                ])
                if not filelist_text:
                    filelist_text, _ = _first_local_text([
                        "system/integration/system_rtl_filelist_sim.txt",
                        "digital/system/integration/system_rtl_filelist_sim.txt",
                        "integration/system_rtl_filelist_sim.txt",
                        "system_rtl_filelist_sim.txt",
                    ])
                filelist_paths.extend([ln.strip() for ln in filelist_text.splitlines() if ln.strip()])

            if not filelist_paths:
                rtl_exts = (".sv", ".v", ".svh", ".vh")
                filelist_paths.extend([
                    path for path in indexed_paths
                    if path.lower().endswith(rtl_exts)
                    and not any(skip in path.lower() for skip in ("/vv/", "/tb/", "/testbench/"))
                ])
                filelist_paths.extend(_local_rtl_files())

            if not filelist_paths:
                return False

            import_dir = os.path.join(shared_state["workflow_dir"], "system", "imported_rtl")
            os.makedirs(import_dir, exist_ok=True)
            materialized: List[str] = []
            for idx, source_path in enumerate(filelist_paths):
                storage_candidates = _storage_path_candidates_from_source_path(source_path, indexed_paths, source_workflow_id)
                content = ""
                resolved_storage = ""
                for candidate in storage_candidates:
                    content = _download_text_storage(candidate)
                    if content:
                        resolved_storage = candidate
                        break
                if not content:
                    local_candidates = []
                    raw_source = str(source_path or "").strip()
                    basename = os.path.basename(raw_source.replace("\\", "/"))
                    if allow_local_handoff_cache:
                        if raw_source:
                            local_candidates.append(raw_source)
                        for root in local_roots:
                            if raw_source:
                                local_candidates.append(os.path.join(root, raw_source))
                            if basename:
                                local_candidates.append(os.path.join(root, basename))
                    for candidate in list(dict.fromkeys(local_candidates)):
                        candidate_abs = os.path.abspath(os.path.expandvars(candidate))
                        if not os.path.isfile(candidate_abs):
                            continue
                        try:
                            with open(candidate_abs, "r", encoding="utf-8", errors="ignore") as fh:
                                content = fh.read()
                            resolved_storage = candidate_abs
                            break
                        except Exception:
                            continue
                if not content:
                    continue
                basename = os.path.basename(str(source_path).replace("\\", "/")) or f"system_rtl_{idx}.sv"
                if not basename.lower().endswith((".v", ".sv", ".vh", ".svh")):
                    basename = f"{basename}.sv"
                local_path = os.path.abspath(os.path.join(import_dir, basename))
                with open(local_path, "w", encoding="utf-8") as fh:
                    fh.write(content.rstrip() + "\n")
                materialized.append(local_path)

            if not materialized:
                return False
            materialized = _dedupe_system_rtl_files(materialized)

            def _module_names_from_file(path: str) -> List[str]:
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                except Exception:
                    return []
                return re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)

            def _contains_module(path: str, module_name: str) -> bool:
                return module_name in _module_names_from_file(path)

            top = package_obj.get("top") if isinstance(package_obj.get("top"), dict) else {}
            top_sim = str((top or {}).get("sim") or "").strip()
            top_phys = str((top or {}).get("phys") or "").strip()
            source_soc_top = next((p for p in materialized if top_sim and _contains_module(p, top_sim)), "")
            source_phys_top = next((p for p in materialized if top_phys and _contains_module(p, top_phys)), "")
            if not source_soc_top:
                source_soc_top = next(
                    (
                        p
                        for p in materialized
                        if "soc" in os.path.basename(p).lower() and "sim" in os.path.basename(p).lower()
                    ),
                    "",
                )
            if source_soc_top:
                modules = _module_names_from_file(source_soc_top)
                if modules:
                    top_sim = modules[0]
            if not source_phys_top:
                source_phys_top = next(
                    (
                        p
                        for p in materialized
                        if "soc" in os.path.basename(p).lower() and "phys" in os.path.basename(p).lower()
                    ),
                    "",
                )
            if source_phys_top:
                modules = _module_names_from_file(source_phys_top)
                if modules:
                    top_phys = modules[0]
            top_phys = top_phys or top_sim
            if top_sim:
                shared_state["soc_top_sim_module"] = top_sim
            if top_phys:
                shared_state["soc_top_phys_module"] = top_phys
            regmap_text, regmap_storage_path = _download_first([
                (storage_obj.get("digital_regmap") if isinstance(storage_obj, dict) else ""),
                "digital/digital_regmap.json",
                "digital_regmap.json",
                "handoff/docs/regmap/digital_regmap.json",
                "handoff/digital_subsystem_ip_package/docs/regmap/digital_regmap.json",
            ])
            if regmap_text:
                try:
                    regmap_obj = json.loads(regmap_text)
                    if isinstance(regmap_obj, dict):
                        regmap_local = os.path.join(shared_state["workflow_dir"], "digital", "digital_regmap.json")
                        os.makedirs(os.path.dirname(regmap_local), exist_ok=True)
                        with open(regmap_local, "w", encoding="utf-8") as fh:
                            fh.write(json.dumps(regmap_obj, indent=2))
                        shared_state["digital_regmap"] = regmap_obj
                        shared_state["digital_regmap_path"] = "digital/digital_regmap.json"
                        shared_state["digital_register_map_path"] = "digital/digital_regmap.json"
                        shared_state["digital_regmap_json"] = regmap_local
                        digital_block = shared_state.setdefault("digital", {})
                        if isinstance(digital_block, dict):
                            digital_block["digital_regmap"] = regmap_obj
                            digital_block["regmap"] = regmap_obj
                            digital_block["digital_regmap_path"] = "digital/digital_regmap.json"
                            digital_block["register_map_path"] = "digital/digital_regmap.json"
                        if package_obj:
                            package_obj.setdefault("storage", {})["digital_regmap"] = regmap_storage_path
                except Exception:
                    pass
            intent_text, _ = _download_first([
                "system/integration/system_integration_intent.json",
                "digital/system/integration/system_integration_intent.json",
            ])
            intent_local = ""
            if intent_text:
                intent_local = os.path.join(shared_state["workflow_dir"], "system", "integration", "system_integration_intent.json")
                os.makedirs(os.path.dirname(intent_local), exist_ok=True)
                with open(intent_local, "w", encoding="utf-8") as fh:
                    fh.write(intent_text)
            _materialize_system_rtl_import(
                materialized,
                source_soc_top=source_soc_top,
                source_phys_top=source_phys_top,
                source_intent=intent_local,
            )
            if package_obj:
                package_top = package_obj.setdefault("top", {})
                package_top["sim"] = package_top.get("sim") or top_sim
                package_top["phys"] = package_top.get("phys") or top_phys
            shared_state["system_rtl_package"] = package_obj or {
                "package_type": "system_rtl",
                "source_workflow_id": source_workflow_id,
                "filelists": {"sim": materialized, "phys": materialized, "libs": []},
                "top": {
                    "sim": top_sim or os.path.splitext(os.path.basename(source_soc_top or materialized[-1]))[0],
                    "phys": top_phys or os.path.splitext(os.path.basename(source_phys_top or source_soc_top or materialized[-1]))[0],
                },
                "compile": {"sim": "imported_from_arch2rtl"},
                "ready_for_cosim": True,
            }
            package_top = shared_state["system_rtl_package"].get("top") if isinstance(shared_state["system_rtl_package"].get("top"), dict) else {}
            if package_top.get("phys"):
                shared_state["soc_top_phys_module"] = package_top["phys"]
            if package_top.get("sim"):
                shared_state["soc_top_sim_module"] = package_top["sim"]
            active_top_kind = "phys" if template_workflow_name in {"System_Synthesis", "System_PD"} else "sim"
            active_top = str(package_top.get(active_top_kind) or package_top.get("sim") or package_top.get("phys") or "").strip()
            if active_top:
                shared_state["top_module"] = active_top
                shared_state["system_top_module"] = active_top
            shared_state["system_rtl_package"]["filelists"] = {
                **(shared_state["system_rtl_package"].get("filelists") or {}),
                "sim": materialized,
                "phys": materialized,
            }
            append_log_workflow(
                workflow_id,
                f"System RTL source: materialized {len(materialized)} RTL files from Supabase workflow {source_workflow_id}",
                phase="system_rtl_source",
            )
            append_log_run(
                run_id,
                f"System RTL source: materialized {len(materialized)} RTL files from Supabase workflow {source_workflow_id}",
            )
            return True

        def _materialize_pasted_system_rtl() -> None:
            pasted = shared_state.get("pasted_rtl_files")
            if not isinstance(pasted, list) or not pasted:
                return
            import_dir = os.path.join(shared_state["workflow_dir"], "system", "imported_rtl")
            os.makedirs(import_dir, exist_ok=True)
            written: List[str] = []
            for idx, item in enumerate(pasted):
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "")
                if not content.strip():
                    continue
                raw_path = str(item.get("path") or f"rtl/system_pasted_{idx}.sv").replace("\\", "/")
                safe_name = os.path.basename(raw_path) or f"system_pasted_{idx}.sv"
                if not safe_name.lower().endswith((".v", ".sv", ".vh", ".svh")):
                    safe_name = f"{safe_name}.sv"
                path = os.path.join(import_dir, safe_name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                written.append(os.path.abspath(path))
            if written:
                soc_top = next((p for p in written if "soc" in os.path.basename(p).lower() or "top" in os.path.basename(p).lower()), written[-1])
                _materialize_system_rtl_import(written, source_soc_top=soc_top)
                append_log_workflow(workflow_id, f"System RTL source: imported {len(written)} pasted RTL files", phase="system_rtl_source")
                append_log_run(run_id, f"System RTL source: imported {len(written)} pasted RTL files")

        def _materialize_repo_system_rtl() -> None:
            repo_path = str(shared_state.get("repo_path") or "").strip()
            if not repo_path:
                return
            repo_root = os.path.abspath(os.path.expanduser(repo_path))
            if not os.path.isdir(repo_root):
                return
            rtl_files: List[str] = []
            for root, _, files in os.walk(repo_root):
                for fn in files:
                    if fn.lower().endswith((".v", ".sv", ".vh", ".svh")):
                        rtl_files.append(os.path.abspath(os.path.join(root, fn)))
            rtl_files = sorted(set(rtl_files))
            if rtl_files:
                soc_top = next((p for p in rtl_files if "soc" in os.path.basename(p).lower() or "top" in os.path.basename(p).lower()), rtl_files[-1])
                _materialize_system_rtl_import(rtl_files, source_soc_top=soc_top)
                append_log_workflow(workflow_id, f"System RTL source: imported {len(rtl_files)} RTL files from repo/path", phase="system_rtl_source")
                append_log_run(run_id, f"System RTL source: imported {len(rtl_files)} RTL files from repo/path")

        if shared_state.get("system_rtl_workflow_id") and not shared_state.get("from_workflow_id"):
            shared_state["from_workflow_id"] = shared_state.get("system_rtl_workflow_id")
        if shared_state.get("system_rtl_workflow_id"):
            shared_state.setdefault("source_rtl_workflow_id", shared_state.get("system_rtl_workflow_id"))
            shared_state.setdefault("source_system_rtl_workflow_id", shared_state.get("system_rtl_workflow_id"))
            try:
                source_run = (
                    supabase.table("runs")
                    .select("artifacts_path,created_at")
                    .eq("workflow_id", shared_state.get("system_rtl_workflow_id"))
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                rows = source_run.data or []
                source_workflow_dir = str((rows[0] or {}).get("artifacts_path") or "").strip() if rows else ""
                if not source_workflow_dir:
                    source_workflow_dir = os.path.join("backend", "workflows", str(shared_state.get("system_rtl_workflow_id")))
                if source_workflow_dir:
                    shared_state["source_system_rtl_workflow_dir"] = source_workflow_dir
                    base = os.path.abspath(source_workflow_dir)
                    roots = list(dict.fromkeys([
                        base,
                        os.path.dirname(base),
                        os.path.join(base, "system"),
                        os.path.join(base, "digital"),
                        os.path.join(os.path.dirname(base), "system"),
                        os.path.join(os.path.dirname(base), "digital"),
                    ]))
                    source_filelist = _find_first_existing_file([
                        os.path.join(root, rel)
                        for root in roots
                        for rel in (
                            "system/integration/system_rtl_filelist_sim.txt",
                            "integration/system_rtl_filelist_sim.txt",
                            "digital/system/integration/system_rtl_filelist_sim.txt",
                            "system_rtl_filelist_sim.txt",
                        )
                    ])
                    source_soc_top = _find_first_existing_file([
                        os.path.join(root, rel)
                        for root in roots
                        for rel in (
                            "system/integration/soc_top_sim.sv",
                            "integration/soc_top_sim.sv",
                            "digital/system/integration/soc_top_sim.sv",
                            "soc_top_sim.sv",
                        )
                    ])
                    source_intent = _find_first_existing_file([
                        os.path.join(root, rel)
                        for root in roots
                        for rel in (
                            "system/integration/system_integration_intent.json",
                            "integration/system_integration_intent.json",
                            "digital/system/integration/system_integration_intent.json",
                            "system_integration_intent.json",
                        )
                    ])
                    source_regmap = _find_first_existing_file([
                        os.path.join(root, rel)
                        for root in roots
                        for rel in (
                            "digital/digital_regmap.json",
                            "digital/regmap.json",
                            "digital/register_map.json",
                            "handoff/docs/regmap/digital_regmap.json",
                            "handoff/digital_subsystem_ip_package/docs/regmap/digital_regmap.json",
                            "digital_regmap.json",
                            "regmap.json",
                        )
                    ])
                    if source_regmap:
                        try:
                            with open(source_regmap, "r", encoding="utf-8") as fh:
                                regmap_obj = json.load(fh)
                            if isinstance(regmap_obj, dict):
                                regmap_local = os.path.join(shared_state["workflow_dir"], "digital", "digital_regmap.json")
                                os.makedirs(os.path.dirname(regmap_local), exist_ok=True)
                                with open(regmap_local, "w", encoding="utf-8") as fh:
                                    fh.write(json.dumps(regmap_obj, indent=2))
                                shared_state["digital_regmap"] = regmap_obj
                                shared_state["digital_regmap_path"] = "digital/digital_regmap.json"
                                shared_state["digital_register_map_path"] = "digital/digital_regmap.json"
                                shared_state["digital_regmap_json"] = regmap_local
                                digital_block = shared_state.setdefault("digital", {})
                                if isinstance(digital_block, dict):
                                    digital_block["digital_regmap"] = regmap_obj
                                    digital_block["regmap"] = regmap_obj
                                    digital_block["digital_regmap_path"] = "digital/digital_regmap.json"
                                    digital_block["register_map_path"] = "digital/digital_regmap.json"
                        except Exception as regmap_exc:
                            append_log_workflow(
                                workflow_id,
                                f"System RTL source regmap hydration warning: {regmap_exc}",
                                phase="system_rtl_source",
                            )
                    if source_filelist:
                        resolved_source_files = _resolve_filelist_lines(source_filelist, roots)
                        _materialize_system_rtl_import(
                            resolved_source_files,
                            source_soc_top=source_soc_top,
                            source_intent=source_intent,
                        )
                        append_log_workflow(
                            workflow_id,
                            f"System RTL source: hydrated filelist from workflow {shared_state.get('system_rtl_workflow_id')}",
                            phase="system_rtl_source",
                        )
                        append_log_run(
                            run_id,
                            f"System RTL source: hydrated filelist from workflow {shared_state.get('system_rtl_workflow_id')}",
                        )
                        if not any(os.path.exists(p) for p in resolved_source_files):
                            _materialize_system_rtl_import_from_supabase(str(shared_state.get("system_rtl_workflow_id")))
                    elif shared_state.get("system_rtl_workflow_id"):
                        _materialize_system_rtl_import_from_supabase(str(shared_state.get("system_rtl_workflow_id")))
            except Exception as source_exc:
                append_log_workflow(
                    workflow_id,
                    f"System RTL source lookup warning: {source_exc}",
                    phase="system_rtl_source",
                )
        elif rtl_source_mode == "paste":
            _materialize_pasted_system_rtl()
        elif rtl_source_mode == "repo_path":
            _materialize_repo_system_rtl()

        if template_workflow_name in {"System_DQA", "System_Sim", "System_Firmware", "System_Synthesis", "System_PD"} and (
            shared_state.get("system_rtl_workflow_id") or rtl_source_mode in {"paste", "repo_path"}
        ):
            materialized_rtl = _materialized_system_rtl_files()
            if materialized_rtl:
                shared_state["system_rtl_files"] = materialized_rtl
                shared_state["rtl_inputs"] = materialized_rtl
                pkg = shared_state.get("system_rtl_package") if isinstance(shared_state.get("system_rtl_package"), dict) else {}
                pkg_top = pkg.get("top") if isinstance(pkg.get("top"), dict) else {}
                top_sim = str(pkg_top.get("sim") or shared_state.get("soc_top_sim_module") or "").strip()
                top_phys = str(pkg_top.get("phys") or shared_state.get("soc_top_phys_module") or "").strip()
                if top_sim:
                    shared_state["soc_top_sim_module"] = top_sim
                if top_phys:
                    shared_state["soc_top_phys_module"] = top_phys
                active_top = top_phys if template_workflow_name in {"System_Synthesis", "System_PD"} else top_sim
                active_top = active_top or top_sim or top_phys
                if active_top:
                    shared_state["top_module"] = active_top
                    shared_state["system_top_module"] = active_top
            elif template_workflow_name in {"System_Sim", "System_Firmware"}:
                source_hint = (
                    shared_state.get("system_rtl_workflow_id")
                    or shared_state.get("source_rtl_workflow_id")
                    or shared_state.get("source_system_rtl_workflow_id")
                    or rtl_source_mode
                    or "unknown"
                )
                raise FileNotFoundError(
                    f"{template_workflow_name} source RTL hydration failed for source '{source_hint}'. "
                    "No existing RTL files were materialized into system/integration/system_rtl_filelist_sim.txt."
                )

        # ---------------------------------------------------------
        # 2) Canonical domain-specific normalization
        #    Keep these three as the source of truth.
        # ---------------------------------------------------------
        digital_text = (
            shared_state.get("digital_spec_text")
            or shared_state.get("digital_spec")
            or ""
        ).strip()

        analog_text = (
            shared_state.get("analog_spec_text")
            or shared_state.get("analog_spec")
            or shared_state.get("analog_datasheet")
            or ""
        ).strip()

        soc_text = (
            shared_state.get("soc_integration_spec_text")
            or shared_state.get("system_integration_description")
            or shared_state.get("soc_integration_description")
            or shared_state.get("integration_description")
            or shared_state.get("soc_integration_spec")
            or shared_state.get("soc_spec")
            or shared_state.get("system_spec")
            or ""
        ).strip()

        # Canonical fields
        if digital_text:
            shared_state["digital_spec_text"] = digital_text

        if analog_text:
            shared_state["analog_spec_text"] = analog_text

        if soc_text:
            shared_state["system_integration_description"] = soc_text
            shared_state["soc_integration_description"] = soc_text
            shared_state["integration_description"] = soc_text
            shared_state["soc_integration_spec_text"] = soc_text

        # ---------------------------------------------------------
        # 3) Compatibility aliases for existing agents
        #    IMPORTANT: only derive aliases from the correct domain.
        # ---------------------------------------------------------
        if digital_text:
            shared_state["digital_spec"] = digital_text

        if analog_text:
            shared_state["datasheet_text"] = analog_text
            shared_state["analog_datasheet"] = analog_text


            # ---------------------------------------------------------
        # Demo defaults for System_SIM execution / coverage agents
        # ---------------------------------------------------------
        shared_state.setdefault("system_sim_testcases", ["smoke_test", "constrained_random_sanity"])
        shared_state.setdefault("system_sim_seeds", [1, 2])
        shared_state.setdefault("system_sim_num_iters", 25)

        # Do NOT set shared_state["spec"] / ["spec_text"] globally here,
        # because that causes cross-domain contamination.

        append_log_workflow(
            workflow_id,
            f"[DEBUG] SystemApp normalized inputs: "
            f"digital={bool(digital_text)} len={len(digital_text)}, "
            f"analog={bool(analog_text)} len={len(analog_text)}, "
            f"soc={bool(soc_text)} len={len(soc_text)}"
        )
        append_log_run(
            run_id,
            f"[DEBUG] SystemApp normalized inputs: "
            f"digital={bool(digital_text)} len={len(digital_text)}, "
            f"analog={bool(analog_text)} len={len(analog_text)}, "
            f"soc={bool(soc_text)} len={len(soc_text)}"
        )

        append_log_workflow(workflow_id, f"🚀 Starting System App: {template_workflow_name}", phase="start")
        append_log_run(run_id, f"🚀 Starting System App: {template_workflow_name}")

        append_log_workflow(workflow_id, f"▶️ Loading Studio workflow: {template_workflow_name}", phase="load")
        append_log_run(run_id, f"▶️ Loading Studio workflow: {template_workflow_name}")

        # Load Studio workflow and resolve nodes exactly like digital/embedded/validation style
        defn = _load_workflow_def_by_name(
            template_workflow_name,
            user_id=user_id,
            force_platform_definition=True,
        )
        nodes = _definition_to_executor_nodes(defn)
        toggles = shared_state.get("toggles") if isinstance(shared_state.get("toggles"), dict) else {}
        using_existing_system_rtl = bool(shared_state.get("system_rtl_workflow_id")) or rtl_source_mode in {"paste", "repo_path"}
        canonical_system_downstream_nodes = {
            "System_DQA": [
                "Digital RTL Linting Agent",
                "Digital CDC Analysis Agent",
                "Digital Reset Integrity Agent",
                "Digital Synthesis Readiness Agent",
                "Digital DQA Summary Agent",
            ],
            "System_Sim": [
                "System CoSim Ingest Agent",
                "System Assertions (SVA) Agent",
                "System Testbench Generator Agent",
                "System Functional Coverage Agent",
                "System Simulation Control Agent",
                "System Simulation Execution Agent",
                "System Simulation Coverage Summary Agent",
            ],
            "System_Firmware": [
                "Embedded Digital RTL Handoff Ingest Agent",
                "Embedded Firmware Register Extract Agent",
                "Embedded Rust Register Layer Generator Agent",
                "Embedded Register Validation Agent",
                "Embedded Rust Driver Scaffold Agent",
                "Embedded Interrupt Mapping Agent",
                "Embedded Firmware Integration Contract Agent",
                "Embedded ELF Build Agent",
                "Embedded Verilator Build Agent",
                "Embedded Cocotb Harness Agent",
                "Embedded Co Sim Runner Agent",
                "System Firmware CoSim Execution Agent",
                "System Firmware Coverage Summary Agent",
                "Embedded Coverage Collector Agent",
                "Embedded Validation Report Agent",
                "Embedded Firmware Executive Summary Agent",
                "System Software Handoff Package Agent",
            ],
        }
        if template_workflow_name in canonical_system_downstream_nodes and using_existing_system_rtl:
            existing_labels = {
                str((node.get("data") or {}).get("backendLabel") or node.get("label") or node.get("name") or "")
                for node in nodes
            }
            canonical_labels = canonical_system_downstream_nodes[template_workflow_name]
            if template_workflow_name == "System_Sim" and toggles.get("enable_formal"):
                canonical_labels = [
                    *canonical_labels[:-1],
                    "System Formal Verification Agent",
                    canonical_labels[-1],
                ]
            if template_workflow_name == "System_Sim" and toggles.get("enable_golden_model"):
                insert_before = "System Simulation Control Agent"
                if "System Golden Model Comparison Agent" not in canonical_labels:
                    insert_at = canonical_labels.index(insert_before) if insert_before in canonical_labels else len(canonical_labels)
                    canonical_labels = [
                        *canonical_labels[:insert_at],
                        "System Golden Model Comparison Agent",
                        *canonical_labels[insert_at:],
                    ]
            original_count = len(nodes)
            nodes = [
                node
                for label in canonical_labels
                for node in [{"label": label}]
                if label in SYSTEM_AGENT_FUNCTIONS or label in EMBEDDED_AGENT_FUNCTIONS or label in DIGITAL_AGENT_FUNCTIONS
            ]
            append_log_workflow(
                workflow_id,
                f"{template_workflow_name}: using canonical source-RTL downstream graph; ignored {original_count - len(existing_labels.intersection(canonical_labels))} stale template nodes",
                phase="system_rtl_source",
            )
            append_log_run(
                run_id,
                f"{template_workflow_name}: using canonical source-RTL downstream graph; ignored stale template nodes",
            )
        if template_workflow_name in {"System_DQA", "System_Sim", "System_Firmware", "System_Synthesis", "System_PD"} and using_existing_system_rtl:
            skip_labels = {
                "Digital Spec Agent",
                "Digital Architecture Agent",
                "Digital Microarchitecture Agent",
                "Digital Register Map Agent",
                "Digital Clock & Reset Architecture Agent",
                "Digital RTL Agent",
                "Digital RTL Signature Agent",
                "Digital RTL Refactoring Agent",
                "Digital Power Intent (UPF-lite) Agent",
                "Digital UPF Static Check Agent",
                "Digital IP Packaging & Handoff Agent",
                "Analog Spec Builder Agent",
                "Analog Behavioral Model Agent",
                "Analog Abstract Views Agent",
                "System Integration Intent Agent",
                "System Top Assembly Agent",
                "Analog Macro Interface Contract Agent",
                "System Assertions (SVA) Agent",
                "System RTL Handoff Package Agent",
                "System RTL Evidence Dashboard Agent",
                "Digital Spec2RTL Conformance Agent",
            }
            original_count = len(nodes)
            nodes = [
                node for node in nodes
                if str((node.get("data") or {}).get("backendLabel") or node.get("label") or node.get("name") or "") not in skip_labels
            ]
            append_log_workflow(
                workflow_id,
                f"{template_workflow_name}: using existing System RTL source ({rtl_source_mode or 'from_system_rtl'}); skipped {original_count - len(nodes)} System RTL generation/dashboard nodes",
                phase="system_rtl_source",
            )
            append_log_run(
                run_id,
                f"{template_workflow_name}: using existing System RTL source ({rtl_source_mode or 'from_system_rtl'}); skipped {original_count - len(nodes)} System RTL generation/dashboard nodes",
            )
        if template_workflow_name == "System_DQA":
            dqa_toggle_agents = {
                "run_lint": "Digital RTL Linting Agent",
                "run_cdc": "Digital CDC Analysis Agent",
                "run_reset": "Digital Reset Integrity Agent",
                "run_synthesis_readiness": "Digital Synthesis Readiness Agent",
            }
            disabled_agents = {
                label
                for key, label in dqa_toggle_agents.items()
                if toggles.get(key) is False
            }
            if disabled_agents:
                original_count = len(nodes)
                nodes = [
                    node for node in nodes
                    if str((node.get("data") or {}).get("backendLabel") or node.get("label") or node.get("name") or "") not in disabled_agents
                ]
                skipped = original_count - len(nodes)
                append_log_workflow(
                    workflow_id,
                    f"System_DQA: skipped {skipped} disabled quality check agent(s): {', '.join(sorted(disabled_agents))}",
                    phase="system_dqa_tools",
                )
                append_log_run(
                    run_id,
                    f"System_DQA: skipped {skipped} disabled quality check agent(s)",
                )
        if template_workflow_name in {"System_RTL", "System_Synthesis", "System_PD"}:
            labels = [str(node.get("label") or node.get("name") or "") for node in nodes]
            if "System RTL Evidence Dashboard Agent" not in labels:
                insert_after = "System RTL Handoff Package Agent" if "System RTL Handoff Package Agent" in labels else "System Top Assembly Agent"
                insert_at = next(
                    (idx + 1 for idx, label in enumerate(labels) if label == insert_after),
                    len(nodes),
                )
                nodes.insert(insert_at, {"label": "System RTL Evidence Dashboard Agent"})
                append_log_workflow(workflow_id, f"{template_workflow_name}: added System RTL evidence dashboard", phase="system_rtl_dashboard")
                append_log_run(run_id, f"{template_workflow_name}: added System RTL evidence dashboard")
        if template_workflow_name in {"System_RTL", "System_Synthesis", "System_PD"} and toggles.get("run_spec2rtl_check"):
            labels = [str(node.get("label") or node.get("name") or "") for node in nodes]
            if "Digital Spec2RTL Conformance Agent" not in labels:
                insert_after = "System RTL Handoff Package Agent" if "System RTL Handoff Package Agent" in labels else "System Top Assembly Agent"
                insert_at = next(
                    (idx + 1 for idx, label in enumerate(labels) if label == insert_after),
                    len(nodes),
                )
                nodes.insert(insert_at, {"label": "Digital Spec2RTL Conformance Agent"})
                append_log_workflow(workflow_id, f"{template_workflow_name}: added Spec2RTL conformance check", phase="system_spec2rtl")
                append_log_run(run_id, f"{template_workflow_name}: added Spec2RTL conformance check")

        if template_workflow_name == "System_Sim":
            labels = [str(node.get("label") or node.get("name") or "") for node in nodes]
            if toggles.get("enable_formal") and "System Formal Verification Agent" not in labels:
                insert_before = "System Simulation Coverage Summary Agent"
                insert_at = labels.index(insert_before) if insert_before in labels else len(nodes)
                nodes.insert(insert_at, {"label": "System Formal Verification Agent"})
                labels.insert(insert_at, "System Formal Verification Agent")
                append_log_workflow(workflow_id, "System_Sim: added formal verification", phase="system_formal")
                append_log_run(run_id, "System_Sim: added formal verification")
            if toggles.get("enable_golden_model") and "System Golden Model Comparison Agent" not in labels:
                insert_before = "System Simulation Control Agent"
                insert_at = labels.index(insert_before) if insert_before in labels else len(nodes)
                nodes.insert(insert_at, {"label": "System Golden Model Comparison Agent"})
                append_log_workflow(workflow_id, "System_Sim: added golden model comparison", phase="system_golden_model")
                append_log_run(run_id, "System_Sim: added golden model comparison")

        # Execute using system loop map
        if template_workflow_name == "System_Sim_Closure_Loop":
            max_iterations = max(1, min(int(shared_state.get("max_iterations") or 1), 10))

            def _node_label(node: Dict[str, Any]) -> str:
                return ((node.get("data") or {}).get("backendLabel") or node.get("label") or "").strip()

            ingest_nodes = [node for node in nodes if _node_label(node) == "System Sim Closure Ingest Agent"]
            body_nodes = [node for node in nodes if _node_label(node) != "System Sim Closure Ingest Agent"]
            body_labels = [_node_label(node) for node in body_nodes]
            toolchain = shared_state.get("toolchain") if isinstance(shared_state.get("toolchain"), dict) else {}
            if str(toolchain.get("formal") or "").lower() not in {"", "none", "disabled"} and "System Formal Verification Agent" not in body_labels:
                insert_before = "System Simulation Coverage Summary Agent"
                insert_at = body_labels.index(insert_before) if insert_before in body_labels else len(body_nodes)
                body_nodes.insert(insert_at, {"label": "System Formal Verification Agent"})
                body_labels.insert(insert_at, "System Formal Verification Agent")
            if str(toolchain.get("golden_model") or "").lower() not in {"", "none", "disabled"} and "System Golden Model Comparison Agent" not in body_labels:
                insert_before = "System Simulation Control Agent"
                insert_at = body_labels.index(insert_before) if insert_before in body_labels else len(body_nodes)
                body_nodes.insert(insert_at, {"label": "System Golden Model Comparison Agent"})
                body_labels.insert(insert_at, "System Golden Model Comparison Agent")
            if ingest_nodes:
                append_log_workflow(workflow_id, "System Sim closure loop ingest: loading baseline System Sim artifacts", phase="closure_loop_ingest")
                append_log_run(run_id, "System Sim closure loop ingest: loading baseline System Sim artifacts")
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="system",
                    nodes=ingest_nodes,
                    shared_state=shared_state,
                )
            for iteration in range(1, max_iterations + 1):
                shared_state["closure_iteration_index"] = iteration
                append_log_workflow(workflow_id, f"System Sim closure loop iteration {iteration}/{max_iterations} started", phase=f"closure_loop_iteration_{iteration}")
                append_log_run(run_id, f"System Sim closure loop iteration {iteration}/{max_iterations} started")
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="system",
                    nodes=body_nodes,
                    shared_state=shared_state,
                )
                judgement = shared_state.get("closure_iteration_judgement") if isinstance(shared_state.get("closure_iteration_judgement"), dict) else {}
                stop_reason = judgement.get("stop_reason") or "not_reported"
                append_log_workflow(workflow_id, f"System Sim closure loop iteration {iteration}/{max_iterations} completed: {stop_reason}", phase=f"closure_loop_iteration_{iteration}_done")
                append_log_run(run_id, f"System Sim closure loop iteration {iteration}/{max_iterations} completed: {stop_reason}")
                if stop_reason in {"closure_achieved", "no_measurable_improvement"}:
                    append_log_workflow(workflow_id, f"System Sim closure loop stopped early after iteration {iteration}: {stop_reason}", phase="closure_loop_stop")
                    append_log_run(run_id, f"System Sim closure loop stopped early after iteration {iteration}: {stop_reason}")
                    break
        elif template_workflow_name == "System_PD" and bool(shared_state.get("run_signoff_closure_loop") or (isinstance(shared_state.get("toggles"), dict) and shared_state["toggles"].get("run_signoff_closure_loop"))):
            max_iterations = max(1, min(int(shared_state.get("max_signoff_closure_iterations") or shared_state.get("max_signoff_iterations") or 1), 2))

            def _node_label(node: Dict[str, Any]) -> str:
                return ((node.get("data") or {}).get("backendLabel") or node.get("label") or node.get("name") or "").strip()

            labels = [_node_label(node) for node in nodes]
            closure_label = "System PD Signoff Closure Agent"
            if closure_label not in labels:
                append_log_workflow(workflow_id, "System signoff closure loop requested but closure agent is not in this workflow.", phase="signoff_closure_missing")
                append_log_run(run_id, "System signoff closure loop requested but closure agent is not in this workflow.")
                _run_nodes_with_shared_state(workflow_id=workflow_id, run_id=run_id, loop_type="system", nodes=nodes, shared_state=shared_state)
            else:
                closure_idx = labels.index(closure_label)
                prefix_nodes = nodes[:closure_idx + 1]
                suffix_nodes = nodes[closure_idx + 1:]
                if bool(shared_state.get("run_synthesis_closure_loop") or (isinstance(shared_state.get("toggles"), dict) and shared_state["toggles"].get("run_synthesis_closure_loop"))):
                    synth_label = "System Synthesis Closure Agent"
                    if synth_label in labels and labels.index(synth_label) < closure_idx:
                        synth_idx = labels.index(synth_label)
                        try:
                            synth_start_idx = labels.index("Digital Synthesis Agent")
                        except ValueError:
                            synth_start_idx = synth_idx
                        synth_max_iterations = max(1, min(int(shared_state.get("max_synthesis_closure_iterations") or 1), 2))
                        append_log_workflow(workflow_id, "System synthesis closure loop baseline started before signoff closure", phase="synthesis_closure_baseline")
                        append_log_run(run_id, "System synthesis closure loop baseline started before signoff closure")
                        shared_state["synthesis_closure_iteration_index"] = 0
                        _run_nodes_with_shared_state(
                            workflow_id=workflow_id,
                            run_id=run_id,
                            loop_type="system",
                            nodes=nodes[:synth_idx + 1],
                            shared_state=shared_state,
                        )
                        plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                        append_log_workflow(workflow_id, f"System synthesis closure baseline completed: {plan.get('status') or 'not_reported'}", phase="synthesis_closure_baseline_done")
                        append_log_run(run_id, f"System synthesis closure baseline completed: {plan.get('status') or 'not_reported'}")
                        for synth_iteration in range(1, synth_max_iterations + 1):
                            plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                            if plan.get("closure_complete") is True or plan.get("status") == "clean":
                                append_log_workflow(workflow_id, f"System synthesis closure stopped before iteration {synth_iteration}: closure achieved", phase="synthesis_closure_stop")
                                append_log_run(run_id, f"System synthesis closure stopped before iteration {synth_iteration}: closure achieved")
                                break
                            shared_state["synthesis_closure_iteration_index"] = synth_iteration
                            append_log_workflow(workflow_id, f"System synthesis closure iteration {synth_iteration}/{synth_max_iterations} started", phase=f"synthesis_closure_iteration_{synth_iteration}")
                            append_log_run(run_id, f"System synthesis closure iteration {synth_iteration}/{synth_max_iterations} started")
                            _run_nodes_with_shared_state(
                                workflow_id=workflow_id,
                                run_id=run_id,
                                loop_type="system",
                                nodes=nodes[synth_start_idx:synth_idx + 1],
                                shared_state=shared_state,
                            )
                            plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                            append_log_workflow(workflow_id, f"System synthesis closure iteration {synth_iteration}/{synth_max_iterations} completed: {plan.get('status') or 'not_reported'}", phase=f"synthesis_closure_iteration_{synth_iteration}_done")
                            append_log_run(run_id, f"System synthesis closure iteration {synth_iteration}/{synth_max_iterations} completed: {plan.get('status') or 'not_reported'}")
                            if plan.get("closure_complete") is True or plan.get("status") == "clean":
                                append_log_workflow(workflow_id, f"System synthesis closure stopped after iteration {synth_iteration}: closure achieved", phase="synthesis_closure_stop")
                                append_log_run(run_id, f"System synthesis closure stopped after iteration {synth_iteration}: closure achieved")
                                break
                        prefix_nodes = nodes[synth_idx + 1:closure_idx + 1]

                append_log_workflow(workflow_id, "System signoff closure loop baseline started", phase="signoff_closure_baseline")
                append_log_run(run_id, "System signoff closure loop baseline started")
                shared_state["signoff_closure_iteration_index"] = 0
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="system",
                    nodes=prefix_nodes,
                    shared_state=shared_state,
                )
                plan = (((shared_state.get("digital") or {}).get("signoff_closure") or {}).get("plan") or {})
                append_log_workflow(
                    workflow_id,
                    f"System signoff closure baseline completed: {plan.get('status') or 'not_reported'}",
                    phase="signoff_closure_baseline_done",
                )
                append_log_run(run_id, f"System signoff closure baseline completed: {plan.get('status') or 'not_reported'}")

                for iteration in range(1, max_iterations + 1):
                    plan = (((shared_state.get("digital") or {}).get("signoff_closure") or {}).get("plan") or {})
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"System signoff closure stopped before iteration {iteration}: closure achieved", phase="signoff_closure_stop")
                        append_log_run(run_id, f"System signoff closure stopped before iteration {iteration}: closure achieved")
                        break
                    restart_stage = str(plan.get("selected_restart_stage") or "").strip()
                    if not restart_stage or restart_stage not in labels:
                        append_log_workflow(workflow_id, f"System signoff closure stopped before iteration {iteration}: no runnable restart stage", phase="signoff_closure_stop")
                        append_log_run(run_id, f"System signoff closure stopped before iteration {iteration}: no runnable restart stage")
                        break
                    start_idx = labels.index(restart_stage)
                    rerun_nodes = nodes[start_idx:closure_idx + 1]
                    shared_state["signoff_closure_iteration_index"] = iteration
                    append_log_workflow(workflow_id, f"System signoff closure iteration {iteration}/{max_iterations} started from {restart_stage}", phase=f"signoff_closure_iteration_{iteration}")
                    append_log_run(run_id, f"System signoff closure iteration {iteration}/{max_iterations} started from {restart_stage}")
                    _run_nodes_with_shared_state(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        loop_type="system",
                        nodes=rerun_nodes,
                        shared_state=shared_state,
                    )
                    plan = (((shared_state.get("digital") or {}).get("signoff_closure") or {}).get("plan") or {})
                    append_log_workflow(
                        workflow_id,
                        f"System signoff closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}",
                        phase=f"signoff_closure_iteration_{iteration}_done",
                    )
                    append_log_run(run_id, f"System signoff closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}")
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"System signoff closure stopped after iteration {iteration}: closure achieved", phase="signoff_closure_stop")
                        append_log_run(run_id, f"System signoff closure stopped after iteration {iteration}: closure achieved")
                        break

                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="system",
                    nodes=suffix_nodes,
                    shared_state=shared_state,
                )
        elif template_workflow_name in {"System_Synthesis", "System_PD"} and bool(shared_state.get("run_synthesis_closure_loop") or (isinstance(shared_state.get("toggles"), dict) and shared_state["toggles"].get("run_synthesis_closure_loop"))):
            max_iterations = max(1, min(int(shared_state.get("max_synthesis_closure_iterations") or 1), 2))

            def _node_label(node: Dict[str, Any]) -> str:
                return ((node.get("data") or {}).get("backendLabel") or node.get("label") or node.get("name") or "").strip()

            labels = [_node_label(node) for node in nodes]
            closure_label = "System Synthesis Closure Agent"
            if closure_label not in labels:
                append_log_workflow(workflow_id, "System synthesis closure loop requested but closure agent is not in this workflow.", phase="synthesis_closure_missing")
                append_log_run(run_id, "System synthesis closure loop requested but closure agent is not in this workflow.")
                _run_nodes_with_shared_state(workflow_id=workflow_id, run_id=run_id, loop_type="system", nodes=nodes, shared_state=shared_state)
            else:
                closure_idx = labels.index(closure_label)
                first_rerun_label = "Digital Synthesis Agent"
                try:
                    rerun_start_idx = labels.index(first_rerun_label)
                except ValueError:
                    rerun_start_idx = closure_idx
                prefix_nodes = nodes[:closure_idx + 1]
                rerun_nodes = nodes[rerun_start_idx:closure_idx + 1]
                suffix_nodes = nodes[closure_idx + 1:]

                append_log_workflow(workflow_id, "System synthesis closure loop baseline started", phase="synthesis_closure_baseline")
                append_log_run(run_id, "System synthesis closure loop baseline started")
                shared_state["synthesis_closure_iteration_index"] = 0
                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="system",
                    nodes=prefix_nodes,
                    shared_state=shared_state,
                )
                closure_state = ((shared_state.get("digital") or {}).get("synthesis_closure") or {})
                plan = closure_state.get("plan") if isinstance(closure_state, dict) else {}
                append_log_workflow(
                    workflow_id,
                    f"System synthesis closure baseline completed: {plan.get('status') or 'not_reported'}",
                    phase="synthesis_closure_baseline_done",
                )
                append_log_run(run_id, f"System synthesis closure baseline completed: {plan.get('status') or 'not_reported'}")

                for iteration in range(1, max_iterations + 1):
                    plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"System synthesis closure stopped before iteration {iteration}: closure achieved", phase="synthesis_closure_stop")
                        append_log_run(run_id, f"System synthesis closure stopped before iteration {iteration}: closure achieved")
                        break
                    shared_state["synthesis_closure_iteration_index"] = iteration
                    append_log_workflow(workflow_id, f"System synthesis closure iteration {iteration}/{max_iterations} started", phase=f"synthesis_closure_iteration_{iteration}")
                    append_log_run(run_id, f"System synthesis closure iteration {iteration}/{max_iterations} started")
                    _run_nodes_with_shared_state(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        loop_type="system",
                        nodes=rerun_nodes,
                        shared_state=shared_state,
                    )
                    plan = (((shared_state.get("digital") or {}).get("synthesis_closure") or {}).get("plan") or {})
                    append_log_workflow(
                        workflow_id,
                        f"System synthesis closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}",
                        phase=f"synthesis_closure_iteration_{iteration}_done",
                    )
                    append_log_run(run_id, f"System synthesis closure iteration {iteration}/{max_iterations} completed: {plan.get('status') or 'not_reported'}")
                    if plan.get("closure_complete") is True or plan.get("status") == "clean":
                        append_log_workflow(workflow_id, f"System synthesis closure stopped after iteration {iteration}: closure achieved", phase="synthesis_closure_stop")
                        append_log_run(run_id, f"System synthesis closure stopped after iteration {iteration}: closure achieved")
                        break

                _run_nodes_with_shared_state(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    loop_type="system",
                    nodes=suffix_nodes,
                    shared_state=shared_state,
                )
        else:
            _run_nodes_with_shared_state(
                workflow_id=workflow_id,
                run_id=run_id,
                loop_type="system",
                nodes=nodes,
                shared_state=shared_state,
            )

        append_log_workflow(workflow_id, f"🎉 System App complete: {template_workflow_name}", status="completed", phase="done")
        append_log_run(run_id, f"🎉 System App complete: {template_workflow_name}", status="completed")

    except Exception as e:
        err = f"❌ System App crashed ({template_workflow_name}): {type(e).__name__}: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")


# ---------------- Embedded app endpoints ----------------

@app.post("/apps/embedded/hal/run")
async def apps_embedded_hal(request: Request, background_tasks: BackgroundTasks, payload: EmbeddedAppIn):
    return _start_embedded_app(background_tasks, request, payload, "App: Embedded HAL", "Embedded_HAL")

@app.post("/apps/embedded/driver/run")
async def apps_embedded_driver(request: Request, background_tasks: BackgroundTasks, payload: EmbeddedAppIn):
    return _start_embedded_app(background_tasks, request, payload, "App: Embedded Driver", "Embedded_Driver")

@app.post("/apps/embedded/boot/run")
async def apps_embedded_boot(request: Request, background_tasks: BackgroundTasks, payload: EmbeddedAppIn):
    return _start_embedded_app(background_tasks, request, payload, "App: Embedded Boot", "Embedded_Boot")

@app.post("/apps/embedded/diagnostics/run")
async def apps_embedded_diagnostics(request: Request, background_tasks: BackgroundTasks, payload: EmbeddedAppIn):
    return _start_embedded_app(background_tasks, request, payload, "App: Embedded Diagnostics", "Embedded_Diagnostics")

@app.post("/apps/embedded/log-analyzer/run")
async def apps_embedded_log_analyzer(request: Request, background_tasks: BackgroundTasks, payload: EmbeddedAppIn):
    return _start_embedded_app(background_tasks, request, payload, "App: Embedded Log Analyzer", "Embedded_LogAnalyzer")

@app.post("/apps/embedded/validate/run")
async def apps_embedded_validate(request: Request, background_tasks: BackgroundTasks, payload: EmbeddedAppIn):
    return _start_embedded_app(background_tasks, request, payload, "App: Embedded Validate", "Embedded_Validate")

@app.post("/apps/embedded/run")
async def apps_embedded_run(request: Request, background_tasks: BackgroundTasks, payload: EmbeddedAppIn):
    return _start_embedded_app(background_tasks, request, payload, "App: Embedded Run", "Embedded_Run")


# ---------------- System App endpoints ----------------

@app.post("/apps/system/end2end/run")
async def apps_system_end2end(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System End2End",
        "System_End2End"
    )


@app.post("/apps/system/sim/run")
async def apps_system_sim(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System Simulation",
        "System_Sim"
    )

@app.post("/apps/system/sim/closure/run")
async def apps_system_sim_closure_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalVerifyClosureAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: System Sim Closure Analysis", "system")
    artifact_dir = os.path.join(base_dir, "system-sim-closure")
    os.makedirs(artifact_dir, exist_ok=True)

    payload_dict = payload.dict()
    payload_dict["parent_workflow_id"] = payload.source_verify_workflow_id
    payload_dict["source_system_sim_workflow_id"] = payload.source_verify_workflow_id

    background_tasks.add_task(
        execute_system_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "System_Sim_Closure",
        payload_dict,
        "system_sim_closure",
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/system/sim/closure-loop/run")
async def apps_system_sim_closure_loop_run(request: Request, background_tasks: BackgroundTasks, payload: DigitalVerifyClosureAppIn):
    user_id = _require_user_id(request)
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: System Sim Closure Loop", "system")
    artifact_dir = os.path.join(base_dir, "system-sim-closure-loop")
    os.makedirs(artifact_dir, exist_ok=True)

    payload_dict = payload.dict()
    payload_dict["parent_workflow_id"] = payload.source_verify_workflow_id
    payload_dict["source_system_sim_workflow_id"] = payload.source_verify_workflow_id
    payload_dict["closure_iteration_index"] = 1
    payload_dict["max_iterations"] = max(int(payload.max_iterations or 1), 1)
    if not payload_dict.get("seed_budget"):
        payload_dict["seed_budget"] = payload.seed_count or 5

    background_tasks.add_task(
        execute_system_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "System_Sim_Closure_Loop",
        payload_dict,
        "system_sim_closure_loop",
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


@app.post("/apps/system/architecture/run")
async def apps_system_architecture(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemArchitectureAppIn
):
    user_id = _require_user_id(request)
    payload_dict = payload.dict()
    demo_run = False
    try:
        checkout_started = _checkout_started_for_request(request, user_id)
    except BillingPaymentRequired as exc:
        raise _payment_required_response(exc)

    if not checkout_started:
        onboarding_service = _onboarding_service_for_main()
        if not is_system_architecture_guided_demo_payload(payload_dict):
            raise _trial_required_response(
                "Start your 3-day trial to run custom System Architecture Explorer workflows."
            )
        if not onboarding_service.can_run_system_architecture_demo(user_id):
            raise _trial_required_response(
                "You have completed your free System Architecture Explorer demo runs. Start your 3-day trial to keep exploring custom architectures."
            )
        demo_run = True

    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: System Architecture Explorer", "system")
    artifact_dir = os.path.join(base_dir, "system-architecture")
    os.makedirs(artifact_dir, exist_ok=True)
    if demo_run:
        _onboarding_service_for_main().record_system_architecture_demo_run(user_id, workflow_id=workflow_id)

    background_tasks.add_task(
        execute_system_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "System_Architecture_Explorer",
        payload_dict,
    )

    response = {"ok": True, "workflow_id": workflow_id, "run_id": run_id}
    if demo_run:
        response["demo"] = _onboarding_service_for_main().system_architecture_demo_usage(user_id)
        response["trial_cta"] = {
            "show": True,
            "message": "Start your 3-day trial to run your own workloads, binaries, and architecture sweeps.",
            "checkout_plan_id": "starter",
            "checkout_url": "/pricing?trial=1",
            "checkout_label": "Start 3-day trial",
        }
    return response


@app.get("/apps/system/architecture/results/{workflow_id}")
def apps_system_architecture_results(workflow_id: str):
    base = _artifacts_dir_for_workflow(workflow_id)
    candidates = [
        base / "system-architecture" / "gem5_run_results.json",
        base / "gem5_run_results.json",
        base / "system" / "architecture" / "gem5_run_results.json",
        base / "system-architecture" / "system" / "architecture" / "gem5_run_results.json",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            try:
                return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to parse gem5 results artifact: {exc}")
    for path in base.rglob("gem5_run_results.json") if base.exists() else []:
        if path.is_file():
            try:
                return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to parse gem5 results artifact: {exc}")
    raise HTTPException(
        status_code=404,
        detail="gem5 results are not available yet. Wait for the System Architecture workflow to complete successfully.",
    )


@app.post("/apps/system/architecture/rtl-handoff/{workflow_id}")
def apps_system_architecture_rtl_handoff(
    workflow_id: str,
    request: Request,
    payload: SystemArchitectureToRTLHandoffIn,
):
    user_id = _require_user_id(request)
    base = _artifacts_dir_for_workflow(workflow_id)
    source_dir = base / "system-architecture"
    if not source_dir.exists():
        source_dir = base
    state = {
        "workflow_id": workflow_id,
        "artifact_dir": str(source_dir),
        "user_id": user_id,
        "reviewer": payload.reviewer or user_id,
        "selected_run_id": payload.selected_run_id,
        "top_module": payload.top_module,
    }
    try:
        result = system_architecture_to_rtl_intent_agent(state)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "handoff": result.get("system_architecture_to_rtl_handoff"),
    }


@app.post("/apps/system/rtl/run")
async def apps_system_rtl(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System RTL",
        "System_RTL"
    )


@app.post("/apps/system/synthesis/run")
async def apps_system_synthesis(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System Synthesis",
        "System_Synthesis"
    )


@app.post("/apps/system/dqa/run")
async def apps_system_dqa(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System DQA",
        "System_DQA"
    )


@app.post("/apps/system/pd/run")
async def apps_system_pd(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System PD",
        "System_PD"
    )


@app.post("/apps/system/firmware/run")
async def apps_system_firmware(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System Firmware",
        "System_Firmware"
    )

@app.post("/apps/system/software/run")
async def apps_system_software(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemSoftwareAppIn
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System Software",
        "System_Software"
    )


@app.post("/apps/system/software-validation/run")
async def apps_system_software_validation(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemSoftwareValidationAppIn
):
    return _start_system_software_validation_app(
        background_tasks=background_tasks,
        request=request,
        payload=payload,
    )


@app.post("/apps/system/product-builder/run")
async def apps_system_product_builder(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: SystemProductBuilderAppIn,
):
    return _start_system_app(
        background_tasks,
        request,
        payload,
        "App: System Product Builder",
        "System_Product_App_Builder",
    )

