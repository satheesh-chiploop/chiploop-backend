import json

from physical_ai.motor_control import build_motor_control_package


def test_motor_control_package_emits_nvidia_and_fpga_contracts(tmp_path):
    summary = build_motor_control_package(
        {
            "model_policy": {"mode": "smart", "selected_model": "chiploop_default"},
            "board": "orangecrab_ecp5_85f",
            "control_loop_hz": 20_000,
        },
        str(tmp_path),
    )
    assert summary["contract"]["physics_model"]["mode"] == "equation"
    assert summary["simulation"]["metrics"]["solver"] == "chiploop_pmsm_dq_equation_v1"
    assert summary["simulation"]["metrics"]["checks"]["finite_outputs"] is True
    assert summary["simulation"]["metrics"]["steady_state_speed_error_percent"] < 2.0
    assert summary["operating_sweep"]["total_cases"] == 15
    assert 0 < summary["operating_sweep"]["feasible_cases"] <= 15
    assert summary["fixed_point"]["passed"] is True
    assert summary["fixed_point"]["total_overflow_count"] == 0
    assert summary["rtl_numeric_contract"]["top_module"] == "motor_control_top"
    assert summary["rtl"]["verification"]["compiled"] is True
    assert summary["rtl"]["verification"]["smoke_passed"] is True
    regmap = json.loads(open(summary["files"]["digital_regmap"], encoding="utf-8").read())
    assert regmap["top_module"] == "motor_control_mmio_top"
    assert regmap["safety"]["reset_state"] == "disabled"
    assert summary["status"] == "rtl_smoke_verified"
    assert summary["fpga_handoff"]["board"] == "orangecrab_ecp5_85f"
    assert summary["agent_workflow"]["runtime"] == "nvidia_nemo_agent_toolkit"
    assert summary["agent_workflow"]["agents"]["Physics Surrogate Agent"]["model"] == "nvidia_nemotron"
    for path in summary["files"].values():
        if path.endswith(".json"):
            assert json.loads(open(path, encoding="utf-8").read())
        elif path.endswith(".svg"):
            assert open(path, encoding="utf-8").read().startswith("<svg")
        elif path.endswith(".sv"):
            source = open(path, encoding="utf-8").read()
            assert "module" in source
        else:
            assert "," in open(path, encoding="utf-8").readline()


def test_main_registers_physical_ai_endpoint():
    main = open("main.py", encoding="utf-8").read()
    assert '@app.post("/apps/physical-ai/motor-control/run")' in main
