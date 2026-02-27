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
from utils.artifact_utils import save_text_artifact_and_record


import logging
logger = logging.getLogger("chiploop")
logging.basicConfig(level=logging.INFO)

# Soft limits to avoid PostgREST "payload string too long" on logs/artifacts fields.
MAX_LOG_CHARS = 850  # ~200KB
MAX_WORKFLOW_ARTIFACTS_JSON_CHARS = 5000
ENABLE_LEGACY_WORKFLOW_ARTIFACTS_INDEX = False
MAX_LOG_LINE_CHARS = 400

def _truncate_tail(s: str, max_chars: int) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    # Keep the tail so the newest context stays visible.
    return "[TRUNCATED]\n" + s[-max_chars:]

# ---------------- Supabase client ----------------
try:
    from supabase import create_client, Client  # supabase-py v2
except ImportError:
    raise RuntimeError("Please install supabase-py v2: pip install supabase")

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # <<< REMOVE fallback

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


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
            payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"])
            return payload  # should contain sub/user id
        except Exception as e:
            logger.warning(f"JWT decode failed, continuing as anonymous: {e}")
    return {"sub": "anonymous"}

# ---------------- FastAPI app ----------------
app = FastAPI(title="ChipLoop API", version="1.0")


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
    allow_origins=["*"],  # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



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
from agents.digital.digital_synthesis_readiness_agent import run_agent as digital_synthesis_readiness_agent
from agents.digital.digital_smoke_preflight_agent import run_agent as digital_smoke_preflight_agent
from agents.digital.digital_smoke_exec_summary_agent import run_agent as digital_smoke_exec_summary_agent
from agents.digital.digital_rtl_signature_agent import run_agent as digital_rtl_signature_agent
from agents.digital.digital_integration_intent_agent import run_agent as digital_integration_intent_agent
from agents.digital.digital_top_assembly_agent import run_agent as digital_top_assembly_agent



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
    "Digital Bug Localization Agent": digital_bug_localization_agent,
    "Digital Formal Verification Agent": digital_formal_verification_agent,
    "Digital Synthesis Readiness Agent": digital_synthesis_readiness_agent,
    "Digital Power Intent (UPF-lite) Agent": digital_power_intent_upf_agent,
    "Digital IP Packaging & Handoff Agent": digital_ip_packaging_handoff_agent,
    "Digital Smoke Preflight Agent": digital_smoke_preflight_agent,
    "Digital Smoke Executive Summary Agent": digital_smoke_exec_summary_agent,
    "Digital RTL Signature Agent": digital_rtl_signature_agent,
    "Digital Integration Intent Agent": digital_integration_intent_agent,
    "Digital Top Assembly Agent": digital_top_assembly_agent,
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
    "Digital Bug Localization Agent": digital_bug_localization_agent,
    "Digital Formal Verification Agent": digital_formal_verification_agent,   
    "Digital Synthesis Readiness Agent": digital_synthesis_readiness_agent,
    "Digital Power Intent (UPF-lite) Agent": digital_power_intent_upf_agent,
    "Digital IP Packaging & Handoff Agent": digital_ip_packaging_handoff_agent, 
    "Digital Smoke Preflight Agent": digital_smoke_preflight_agent,
    "Digital Smoke Executive Summary Agent": digital_smoke_exec_summary_agent,
    "Digital RTL Signature Agent": digital_rtl_signature_agent,
    "Digital Integration Intent Agent": digital_integration_intent_agent,
    "Digital Top Assembly Agent": digital_top_assembly_agent,
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

def _load_workflow_def_by_name(name: str, user_id: Optional[str]) -> Dict[str, Any]:
    """
    Loads workflow template from public.workflows by exact name.
    Prefer user-owned workflow if present; fallback to prebuilt.
    Returns dict: {"nodes": [...], "edges": [...]}
    """
    q = supabase.table("workflows").select(
        "id,name,definitions,nodes,edges,loop_type,is_prebuilt,user_id"
    ).eq("name", name)

    # Prefer user-owned
    if user_id:
        r = q.eq("user_id", user_id).limit(1).execute()
        if r.data:
            row = r.data[0]
            defn = row.get("definitions") or {}
            return {
                "nodes": (defn.get("nodes") if isinstance(defn, dict) else None) or row.get("nodes") or [],
                "edges": (defn.get("edges") if isinstance(defn, dict) else None) or row.get("edges") or [],
            }

    # Fallback to prebuilt
    r2 = q.eq("is_prebuilt", True).limit(1).execute()
    if r2.data:
        row = r2.data[0]
        defn = row.get("definitions") or {}
        return {
            "nodes": (defn.get("nodes") if isinstance(defn, dict) else None) or row.get("nodes") or [],
            "edges": (defn.get("edges") if isinstance(defn, dict) else None) or row.get("edges") or [],
        }

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






@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

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

    for node in (nodes or []):
        label = (node or {}).get("label") or ""
        label = label.strip()
        if not label:
            continue

        append_log_workflow(workflow_id, f"⚙️ Running {label}")
        append_log_run(run_id, f"⚙️ Running {label}")

        fn = agent_map_norm.get(label)
        if not fn:
            append_log_workflow(workflow_id, f"❌ No agent implementation found for: {label}")
            append_log_run(run_id, f"❌ No agent implementation found for: {label}")
            continue

        try:
            result = fn(shared_state)
            if isinstance(result, dict):
                shared_state.update(result)

            append_log_workflow(workflow_id, f"✅ {label} done")
            append_log_run(run_id, f"✅ {label} done")

        except Exception as e:
            append_log_workflow(workflow_id, f"❌ {label} failed: {type(e).__name__}: {e}")
            append_log_run(run_id, f"❌ {label} failed: {type(e).__name__}: {e}")
            # continue (do not crash whole app run)
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
        user = verify_token(request)
        user_id = user.get("sub") if user and user.get("sub") and user.get("sub") != "anonymous" else None
        workflow_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        data = json.loads(workflow)

        # ✅ Guard: if frontend accidentally sends "null" or non-dict, fail cleanly
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Invalid workflow payload (expected JSON object).")

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

        return JSONResponse({"workflow_id": workflow_id, "run_id": run_id, "loop_type": loop_type, "status": "queued"})

    except Exception as e:
        logger.error(f"❌ run_workflow failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================================
# ---------- Background executor ----------
# ==========================================================

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
        # ✅ inject supabase once (so WF5 services can query history)
        shared_state["supabase_client"] = supabase
        if user_id:
            shared_state["user_id"] = user_id
        if upload_path:
            shared_state["uploaded_file"] = upload_path
        if spec_text:
            shared_state["spec"] = spec_text



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
                state = dict(shared_state)  # shallow copy

                result = fn(state)  # your agents accept a dict 'state'

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
                        out = fn(shared_state)
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



# ==========================================================
# ✅ DIGITAL APPS (Arch2RTL / DQA / Verify) — same pattern as Validation Run App
# ==========================================================

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

    toggles: Optional[Dict[str, bool]] = None  # {"gen_regmap":true, "gen_upf_lite":false, "gen_packaging":true}


class DigitalDQAAppIn(BaseModel):
    # RTL source options
    rtl_source_mode: Optional[str] = None  # "from_arch2rtl" | "paste" | "repo_path"
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None  # [{path, content}]

    clocks: Optional[Any] = None
    resets: Optional[Any] = None

    lint_profile: Optional[str] = "medium"
    cdc_profile: Optional[str] = "default"
    target: Optional[str] = "asic"
    toggles: Optional[Dict[str, bool]] = None  # {"enable_autofix":false}


class DigitalVerifyAppIn(BaseModel):
    rtl_source_mode: Optional[str] = None
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None

    test_intent: Optional[str] = None
    interfaces: Optional[Any] = None
    random_vs_directed: Optional[str] = None
    coverage_targets: Optional[str] = None
    simulator_type: Optional[str] = None
    seed_count: Optional[int] = None

    toggles: Optional[Dict[str, bool]] = None  # {"enable_formal":false, "enable_golden_model":false}



class DigitalSmokeAppIn(BaseModel):
    # RTL source (same spirit as DQA/Verify)
    rtl_source_mode: Optional[str] = None  # "from_arch2rtl" | "paste" | "repo_path"
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None  # [{path, content}]

    # Smoke knobs (small)
    simulator_type: Optional[str] = None     # e.g. "verilator" / "iverilog" / "vcs"
    time_budget: Optional[str] = "fast"      # "fast" | "medium"
    seed_count: Optional[int] = None         # optional override
    enable_waveform: Optional[bool] = False

    # optional freeform notes
    notes: Optional[str] = None


class DigitalIntegrateAppIn(BaseModel):
    # RTL source (same spirit as DQA/Verify/Smoke)
    rtl_source_mode: Optional[str] = None  # "from_arch2rtl" | "paste" | "repo_path"
    from_workflow_id: Optional[str] = None
    repo_path: Optional[str] = None
    pasted_rtl_files: Optional[Any] = None  # [{path, content}]

    # Integrate knobs (Model 2: text -> intent -> top.sv)
    top_name: str
    integration_description: str

    # Optional extras
    enable_packaging: Optional[bool] = True
    notes: Optional[str] = None

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
        }

        # Inject payload fields into shared_state
        for k, v in (payload or {}).items():
            if v is not None:
                shared_state[k] = v

        # Normalize spec fields (Arch2RTL agents often expect state["spec"])
        if shared_state.get("spec_text"):
            shared_state["spec"] = shared_state["spec_text"]

        append_log_workflow(workflow_id, f"🚀 Starting Digital App: {app_name}", phase="start")
        append_log_run(run_id, f"🚀 Starting Digital App: {app_name}")

        append_log_workflow(workflow_id, f"▶️ Loading Studio workflow: {template_workflow_name}", phase="load")
        append_log_run(run_id, f"▶️ Loading Studio workflow: {template_workflow_name}")

        # Load Studio prebuilt workflow and convert to executor nodes
        defn = _load_workflow_def_by_name(template_workflow_name, user_id=user_id)
        nodes = _definition_to_executor_nodes(defn)

        # Run nodes (loop_type="digital" so it uses DIGITAL_AGENT_FUNCTIONS)
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
    workflow_id, run_id, base_dir = _create_app_workflow_and_run(user_id, "App: Arch2RTL", "digital")
    artifact_dir = os.path.join(base_dir, "arch2rtl")
    os.makedirs(artifact_dir, exist_ok=True)

    background_tasks.add_task(
        execute_digital_app_background,
        workflow_id,
        run_id,
        user_id,
        artifact_dir,
        "arch2rtl",
        "Digital_Arch2RTL",
        payload.dict(),
    )

    return {"ok": True, "workflow_id": workflow_id, "run_id": run_id}


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

def _touch_runner_seen(runner_name: str):
    try:
        supabase.table("runners").update({"last_seen": datetime.utcnow().isoformat()}).eq("runner_name", runner_name).execute()
    except Exception:
        pass

# ---------- 1) Register runner ----------
@app.post("/register_runner")
def register_runner(payload: dict = Body(...)):
    """
    Body: { runner_name, email, token }
    Upserts runner and marks online.
    """
    runner = {
        "runner_name": payload.get("runner_name"),
        "email": payload.get("email"),
        "token": payload.get("token"),
        "status": "idle",
        "last_seen": datetime.utcnow().isoformat(),
    }
    if not runner["runner_name"]:
        raise HTTPException(status_code=400, detail="runner_name required")
    # upsert by runner_name
    supabase.table("runners").upsert(runner, on_conflict="runner_name").execute()
    return {"ok": True, "runner": runner}

# ---------- 2) Get a queued job (simulation) ----------
@app.get("/get_job")
def get_job(runner: str):
    """
    Query: ?runner=<runner_name>
    Finds a workflow queued for 'simulation' phase and assigns it.
    Returns: { job: { workflow_id, loop_type, top_module?, ... } } or { job: null }
    """
    if not runner:
        raise HTTPException(status_code=400, detail="runner required")

    _touch_runner_seen(runner)

    # Find first queued workflow for simulation
    q = (
        supabase.table("workflows")
        .select("id, user_id, loop_type, params")
        .eq("status", "queued")
        .eq("phase", "simulation")
        .is_("runner_assigned", None)
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    rows = q.data or []
    if not rows:
        return {"job": None}

    wf = rows[0]
    workflow_id = wf["id"]
    loop_type = (wf.get("loop_type") or "digital")
    params = wf.get("params") or ""
    # Allow a 'top_module=<name>' hint in workflows.params
    top_module = None
    if isinstance(params, str) and "top_module" in params:
        # naive parse "top_module=<name>" from params text
        for token in params.replace(",", " ").split():
            if token.startswith("top_module="):
                top_module = token.split("=", 1)[1].strip()

    # Assign runner
    supabase.table("workflows").update({
        "runner_assigned": runner,
        "status": "running",
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", workflow_id).execute()

    supabase.table("runners").upsert({
        "runner_name": runner,
        "status": "busy",
        "current_job": workflow_id,
        "last_seen": datetime.utcnow().isoformat()
    }, on_conflict="runner_name").execute()

    job = {
        "workflow_id": workflow_id,
        "loop_type": loop_type,
        "top_module": top_module or "tb_counter_4b",
    }
    return {"job": job}

# ---------- 3) Upload results from runner ----------
@app.post("/upload_results")
def upload_results(payload: dict = Body(...)):
    """
    Body: { workflow_id, status, logs, artifacts?, runner_name }
    - Appends logs
    - Updates workflow status/phase
    - Marks run completed
    """
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

@app.get("/list_agents")
async def list_agents():
    G = build_capability_graph(AGENT_CAPABILITIES)
    return {
        "status": "ok",
        "agents": AGENT_CAPABILITIES,
        "graph": serialize_graph(G)
    }

from planner.ai_work_planner import plan_workflow


@app.post("/plan_workflow")
async def plan_workflow_api(request: Request):
    data = await request.json()
    user_prompt = data.get("prompt", "")
    workflow_id = data.get("workflow_id", "manual_plan")
    structured_spec_final = data.get("structured_spec_final")
    user_id = data.get("user_id") or data.get("uid") or "anonymous"

    plan = await plan_workflow(
       user_prompt,
       structured_spec_final=structured_spec_final,
       user_id=user_id,
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

    

        # Support both flat and nested payloads
        wf = data.get("workflow", {})
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
          data.get("workflow", {}).get("workflow_name")
          or data.get("workflow", {}).get("name")
          or "Untitled Workflow"
        )

        name= str(name).strip()
        logger.info(f"💾 Normalized workflow name: '{name}' (type={type(name)})")
        
        goal = wf.get("goal", "")
        summary = wf.get("summary") or wf.get("data", {}).get("summary", "")
        loop_type = wf.get("loop_type", "system")
        nodes = wf.get("nodes") or wf.get("data", {}).get("nodes", [])
        edges = wf.get("edges") or wf.get("data", {}).get("edges", [])

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
    """Convert voice to summarized spec via Whisper + Notion"""
    try:
        import openai, tempfile
        from notion_client import Client

        # --- Step 1: Save temporary audio file ---
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        # --- Step 2: Transcribe using Whisper ---
        openai.api_key = os.getenv("OPENAI_API_KEY")
        transcript = openai.Audio.transcriptions.create(
            model="whisper-1", file=open(tmp_path, "rb")
        )
        text = transcript.text.strip()

        # --- Step 3: Append to Notion ---
        notion = Client(auth=os.getenv("NOTION_API_KEY"))
        db_id = os.getenv("NOTION_DATABASE_ID")
        notion.pages.create(
            parent={"database_id": db_id},
            properties={"Name": {"title": [{"text": {"content": text[:100]}}]}},
        )

        # --- Step 4: Summarize spec (optional) ---
        from utils.llm_utils import run_llm_fallback
        summary_prompt = f"Summarize and structure this design spec:\n{text}"
        summary = run_llm_fallback(summary_prompt)

        return {"summary": summary, "coverage": 65, "mode": "voice"}

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


    user_id = (await request.json()).get("user_id") or None

    q = supabase.table("agents") \
        .select("id") \
        .eq("agent_name", old_name) \
        .eq("is_custom", True)
    q = q.eq("user_id", user_id) if user_id else q.is_("user_id", None)
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
    }
    out_state = validation_test_plan_agent(state)

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

        defn = _load_workflow_def_by_name(template_workflow_name, user_id=user_id)
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

        append_log_workflow(workflow_id, f"▶️ Phase: {template_workflow_name}", phase="start")
        append_log_run(run_id, f"▶️ Phase: {template_workflow_name}")

        defn = _load_workflow_def_by_name(template_workflow_name, user_id=user_id)
        nodes = _definition_to_executor_nodes(defn)

        _run_nodes_with_shared_state(
            workflow_id=workflow_id,
            run_id=run_id,
            loop_type="embedded",
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




