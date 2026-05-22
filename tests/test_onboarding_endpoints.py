import sys
from pathlib import Path

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_auth
import browser_routes
from onboarding import (
    InMemoryOnboardingRepository,
    OnboardingService,
    is_arch2rtl_guided_demo_payload,
    is_system_architecture_guided_demo_payload,
)


JWT_SECRET = "onboarding-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1") -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _client(service: OnboardingService | None = None) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.onboarding_service = service or OnboardingService(InMemoryOnboardingRepository())
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_onboarding_requires_session():
    response = _client().get("/settings/onboarding")
    assert response.status_code == 401


def test_onboarding_defaults_to_incomplete():
    response = _client().get("/settings/onboarding", headers=_auth())
    assert response.status_code == 200
    onboarding = response.json()["onboarding"]
    assert onboarding["completed"] is False
    assert onboarding["completed_at"] is None
    assert onboarding["demo"]["arch2rtl"]["limit"] == 3
    assert onboarding["demo"]["arch2rtl"]["runs_remaining"] == 3
    assert onboarding["demo"]["system_architecture"]["limit"] == 3
    assert onboarding["demo"]["system_architecture"]["runs_remaining"] == 3


def test_onboarding_row_excludes_response_only_fields():
    service = OnboardingService(InMemoryOnboardingRepository())
    state = service.record_arch2rtl_demo_run("user-1", workflow_id="wf-1")

    as_response = state.to_dict()
    as_row = state.to_row()

    assert "demo" in as_response
    assert "completed" in as_response
    assert "demo" not in as_row
    assert "completed" not in as_row
    assert as_row["metadata"]["arch2rtl_demo_runs"] == 1


def test_onboarding_complete_records_workflow_id():
    client = _client()
    response = client.post(
        "/settings/onboarding",
        headers=_auth("user-1"),
        json={"action": "complete", "workflow_id": "wf-123", "last_step": "downloaded"},
    )
    assert response.status_code == 200
    onboarding = response.json()["onboarding"]
    assert onboarding["completed"] is True
    assert onboarding["first_workflow_id"] == "wf-123"
    assert onboarding["last_step"] == "downloaded"


def test_onboarding_is_user_scoped():
    service = OnboardingService(InMemoryOnboardingRepository())
    client = _client(service)
    client.post("/settings/onboarding", headers=_auth("user-1"), json={"action": "skip"})

    user_1 = client.get("/settings/onboarding", headers=_auth("user-1")).json()["onboarding"]
    user_2 = client.get("/settings/onboarding", headers=_auth("user-2")).json()["onboarding"]

    assert user_1["completed"] is True
    assert user_2["completed"] is False


def test_arch2rtl_demo_usage_limit():
    service = OnboardingService(InMemoryOnboardingRepository())

    assert service.can_run_arch2rtl_demo("user-1") is True
    service.record_arch2rtl_demo_run("user-1", workflow_id="wf-1")
    service.record_arch2rtl_demo_run("user-1", workflow_id="wf-2")
    state = service.record_arch2rtl_demo_run("user-1", workflow_id="wf-3")

    assert state.arch2rtl_demo_runs == 3
    assert state.arch2rtl_demo_runs_remaining == 0
    assert state.first_workflow_id == "wf-1"
    assert service.can_run_arch2rtl_demo("user-1") is False


def test_arch2rtl_demo_payload_detection():
    assert is_arch2rtl_guided_demo_payload({
        "project_name": "pwm_controller_onboarding",
        "top_module": "pwm_controller",
        "design_language": "systemverilog",
        "spec_text": "Design a parameterized PWM controller.\nduty_cycle[7:0]\nGenerate synthesizable SystemVerilog",
        "toggles": {"gen_packaging": True, "gen_upf_lite": True},
    }) is True

    assert is_arch2rtl_guided_demo_payload({
        "project_name": "custom_project",
        "top_module": "pwm_controller",
        "design_language": "systemverilog",
        "spec_text": "Design a parameterized PWM controller.\nduty_cycle[7:0]\nGenerate synthesizable SystemVerilog",
    }) is False


def test_system_architecture_demo_usage_limit():
    service = OnboardingService(InMemoryOnboardingRepository())

    assert service.can_run_system_architecture_demo("user-1") is True
    service.record_system_architecture_demo_run("user-1", workflow_id="wf-1")
    service.record_system_architecture_demo_run("user-1", workflow_id="wf-2")
    state = service.record_system_architecture_demo_run("user-1", workflow_id="wf-3")

    assert state.system_architecture_demo_runs == 3
    assert state.system_architecture_demo_runs_remaining == 0
    assert state.first_workflow_id == "wf-1"
    assert service.can_run_system_architecture_demo("user-1") is False


def test_system_architecture_demo_payload_detection():
    payload = {
        "project_name": "matrix_multiply_cache_sweep_demo",
        "workload": "matrix_multiply",
        "simulator": "gem5",
        "isa": "x86",
        "cpu_model": "TimingSimpleCPU",
        "goal": (
            "Explore cache-size tradeoffs for matrix multiplication with performance, power, "
            "and area estimates; show workload vs cache size charts."
        ),
        "sweep": {
            "l1d_size_kb": [16, 32, 64],
            "l2_size_kb": [256, 512, 1024],
        },
        "toggles": {
            "enable_power_estimation": True,
            "enable_area_estimation": True,
            "enable_visualizations": True,
        },
    }

    assert is_system_architecture_guided_demo_payload(payload) is True

    invalid_payload = {**payload, "workload": "custom_binary"}
    assert is_system_architecture_guided_demo_payload(invalid_payload) is False
