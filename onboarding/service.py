from typing import Any, Dict

from .models import ARCH2RTL_DEMO_LIMIT, SYSTEM_ARCHITECTURE_DEMO_LIMIT, OnboardingState, utcnow_iso
from .repository import OnboardingRepository


ARCH2RTL_DEMO_PROJECT = "pwm_controller_onboarding"
ARCH2RTL_DEMO_TOP_MODULE = "pwm_controller"
ARCH2RTL_DEMO_SPEC_MARKERS = (
    "Design a parameterized PWM controller.",
    "duty_cycle[7:0]",
    "Generate synthesizable SystemVerilog",
)

SYSTEM_ARCHITECTURE_DEMO_PROJECT = "matrix_multiply_cache_sweep_demo"
SYSTEM_ARCHITECTURE_DEMO_WORKLOAD = "matrix_multiply"
SYSTEM_ARCHITECTURE_DEMO_SIMULATOR = "gem5"
SYSTEM_ARCHITECTURE_DEMO_GOAL_MARKERS = (
    "Explore cache-size tradeoffs for matrix multiplication",
    "performance, power, and area",
    "show workload vs cache size charts",
)


class OnboardingService:
    def __init__(self, repository: OnboardingRepository):
        self.repository = repository

    def get_state(self, user_id: str) -> OnboardingState:
        return self.repository.get(user_id) or OnboardingState(user_id=user_id)

    def update_state(self, user_id: str, payload: Dict[str, Any]) -> OnboardingState:
        state = self.get_state(user_id)
        action = str(payload.get("action") or "").strip().lower()
        now = utcnow_iso()

        if action == "start":
            state.demo_started_at = state.demo_started_at or now
            state.last_step = str(payload.get("last_step") or "started")
        elif action == "complete":
            state.completed_at = state.completed_at or now
            state.last_step = str(payload.get("last_step") or "downloaded")
            if payload.get("workflow_id"):
                state.first_workflow_id = str(payload["workflow_id"])
        elif action == "skip":
            state.skipped_at = state.skipped_at or now
            state.last_step = str(payload.get("last_step") or "skipped")
        elif payload.get("last_step"):
            state.last_step = str(payload["last_step"])

        if payload.get("metadata") and isinstance(payload["metadata"], dict):
            state.metadata = {**state.metadata, **payload["metadata"]}

        state.updated_at = now
        return self.repository.save(state)

    def arch2rtl_demo_usage(self, user_id: str) -> Dict[str, int | bool]:
        state = self.get_state(user_id)
        return {
            "limit": ARCH2RTL_DEMO_LIMIT,
            "runs_used": state.arch2rtl_demo_runs,
            "runs_remaining": state.arch2rtl_demo_runs_remaining,
            "can_run": state.arch2rtl_demo_runs_remaining > 0,
        }

    def can_run_arch2rtl_demo(self, user_id: str) -> bool:
        return self.get_state(user_id).arch2rtl_demo_runs_remaining > 0

    def record_arch2rtl_demo_run(self, user_id: str, workflow_id: str | None = None) -> OnboardingState:
        state = self.get_state(user_id)
        now = utcnow_iso()
        state.demo_started_at = state.demo_started_at or now
        state.last_step = "arch2rtl_demo_run_started"
        state.metadata = {
            **state.metadata,
            "arch2rtl_demo_runs": state.arch2rtl_demo_runs + 1,
            "arch2rtl_last_demo_run_at": now,
        }
        if workflow_id and not state.first_workflow_id:
            state.first_workflow_id = workflow_id
        state.updated_at = now
        return self.repository.save(state)

    def system_architecture_demo_usage(self, user_id: str) -> Dict[str, int | bool]:
        state = self.get_state(user_id)
        return {
            "limit": SYSTEM_ARCHITECTURE_DEMO_LIMIT,
            "runs_used": state.system_architecture_demo_runs,
            "runs_remaining": state.system_architecture_demo_runs_remaining,
            "can_run": state.system_architecture_demo_runs_remaining > 0,
        }

    def can_run_system_architecture_demo(self, user_id: str) -> bool:
        return self.get_state(user_id).system_architecture_demo_runs_remaining > 0

    def record_system_architecture_demo_run(self, user_id: str, workflow_id: str | None = None) -> OnboardingState:
        state = self.get_state(user_id)
        now = utcnow_iso()
        state.demo_started_at = state.demo_started_at or now
        state.last_step = "system_architecture_demo_run_started"
        state.metadata = {
            **state.metadata,
            "system_architecture_demo_runs": state.system_architecture_demo_runs + 1,
            "system_architecture_last_demo_run_at": now,
        }
        if workflow_id and not state.first_workflow_id:
            state.first_workflow_id = workflow_id
        state.updated_at = now
        return self.repository.save(state)


def is_arch2rtl_guided_demo_payload(payload: Dict[str, object]) -> bool:
    project_name = str(payload.get("project_name") or "").strip()
    top_module = str(payload.get("top_module") or "").strip()
    design_language = str(payload.get("design_language") or "").strip().lower()
    spec_text = str(payload.get("spec_text") or "")
    toggles = payload.get("toggles") if isinstance(payload.get("toggles"), dict) else {}

    if project_name != ARCH2RTL_DEMO_PROJECT:
        return False
    if top_module != ARCH2RTL_DEMO_TOP_MODULE:
        return False
    if design_language != "systemverilog":
        return False
    if not all(marker in spec_text for marker in ARCH2RTL_DEMO_SPEC_MARKERS):
        return False
    if toggles:
        if toggles.get("gen_packaging") is not True:
            return False
        if toggles.get("gen_upf_lite") is not True:
            return False
    return True


def is_system_architecture_guided_demo_payload(payload: Dict[str, object]) -> bool:
    project_name = str(payload.get("project_name") or "").strip()
    workload = str(payload.get("workload") or payload.get("workload_name") or "").strip()
    simulator = str(payload.get("simulator") or payload.get("simulation_tool") or "").strip().lower()
    isa = str(payload.get("isa") or "").strip().lower()
    cpu_model = str(payload.get("cpu_model") or "").strip()
    goal = str(payload.get("goal") or payload.get("experiment_goal") or "")
    sweep = payload.get("sweep") if isinstance(payload.get("sweep"), dict) else {}
    toggles = payload.get("toggles") if isinstance(payload.get("toggles"), dict) else {}

    if project_name != SYSTEM_ARCHITECTURE_DEMO_PROJECT:
        return False
    if workload != SYSTEM_ARCHITECTURE_DEMO_WORKLOAD:
        return False
    if simulator != SYSTEM_ARCHITECTURE_DEMO_SIMULATOR:
        return False
    if isa not in {"x86", "riscv"}:
        return False
    if cpu_model not in {"TimingSimpleCPU", "MinorCPU", "O3CPU"}:
        return False
    if not all(marker in goal for marker in SYSTEM_ARCHITECTURE_DEMO_GOAL_MARKERS):
        return False

    l1d_sizes = sweep.get("l1d_size_kb") if isinstance(sweep, dict) else None
    l2_sizes = sweep.get("l2_size_kb") if isinstance(sweep, dict) else None
    if not isinstance(l1d_sizes, list) or not l1d_sizes:
        return False
    if not isinstance(l2_sizes, list) or not l2_sizes:
        return False
    if len(l1d_sizes) > 4 or len(l2_sizes) > 4:
        return False
    if any(v not in {16, 32, 64, 128} for v in l1d_sizes):
        return False
    if any(v not in {256, 512, 1024, 2048} for v in l2_sizes):
        return False

    if toggles:
        if toggles.get("enable_power_estimation") is not True:
            return False
        if toggles.get("enable_area_estimation") is not True:
            return False
        if toggles.get("enable_visualizations") is not True:
            return False
    return True
