import json
from pathlib import Path

from agents.fpga import fpga_dashboard_agent
from agents.fpga.fpga_nextpnr_place_route_agent import (
    _himbaechel_uarch_args,
    _nextpnr_effort_policy,
    _parse_nextpnr,
    _parse_nextpnr_report,
)


def test_nextpnr_report_exposes_routed_lut4_cells(tmp_path):
    report = tmp_path / "nextpnr.json"
    report.write_text(
        json.dumps(
            {
                "utilization": {
                    "ICESTORM_LC": {"used": 79, "available": 5280},
                    "SB_LUT4": {"used": 72, "available": 5280},
                    "SB_DFF": {"used": 33, "available": 5280},
                }
            }
        ),
        encoding="utf-8",
    )

    parsed = _parse_nextpnr_report(str(report), {"family": "ice40", "resources": {"logic_cells": 5280}})

    assert parsed["logical_cells_used"] == 79
    assert parsed["routed_lut4_cells"] == 72
    assert parsed["routed_lut4_cells_available"] == 5280


def test_fpga_dashboard_embeds_verification_and_authoritative_agent_count(tmp_path, monkeypatch):
    verification_path = tmp_path / "simulation_summary_coverage.json"
    verification = {
        "simulation": {"total": 20, "pass": 20, "fail": 0},
        "coverage": {
            "functional_coverage_pct": 0.0,
            "code": {"line_coverage_pct": 20.69, "branch_coverage_pct": 0.0, "toggle_coverage_pct": 1.39},
        },
        "formal": {"status": "pass"},
    }
    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    published = {}

    def capture(_state, _agent, _subdir, _filename, summary):
        published.update(summary)

    monkeypatch.setattr(fpga_dashboard_agent, "publish_json", capture)
    state = {
        "simulation_summary_coverage_json": str(verification_path),
        "target_frequency_mhz": 75.0,
        "_participating_agents": ["Agent A", "Agent B", "Agent A", "FPGA Dashboard Agent"],
        "fpga": {
            "target": {"resources": {"logic_cells": 5280}},
            "synthesis": {},
            "place_route": {"routed_lut4_cells": 72},
            "timing_drc": {},
            "bitstream": {"status": "completed"},
        },
    }

    fpga_dashboard_agent.run_agent(state)

    assert published["verification"] == verification
    assert published["agent_count"] == 3
    assert published["participating_agents"] == ["Agent A", "Agent B", "FPGA Dashboard Agent"]
    assert published["routed_result"]["routed_lut4_cells"] == 72
    assert published["routed_result"]["logical_cells_used"] == 72
    assert published["routed_result"]["logic_utilization_percent"] == 1.364
    assert published["timing_summary"]["target_frequency_mhz"] == 75.0

def test_fpga_verification_uses_rtl_ports_when_handoff_spec_has_no_ports(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPLOOP_DATABASE_PROVIDER", "postgres")
    monkeypatch.setenv("CHIPLOOP_STORAGE_PROVIDER", "local")
    monkeypatch.setenv("CHIPLOOP_AUTH_PROVIDER", "disabled")
    import sys
    import types
    artifact_utils = types.ModuleType("utils.artifact_utils")
    artifact_utils.save_text_artifact_and_record = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "utils.artifact_utils", artifact_utils)
    from agents.digital.digital_functional_coverage_agent import (
        _build_coverage_points,
        _ports_from_rtl_files as coverage_ports_from_rtl,
    )
    from agents.digital.digital_testbench_generator_agent import (
        _gen_cocotb_test,
        _infer_clocks_resets,
        _ports_from_rtl_files as testbench_ports_from_rtl,
    )

    rtl = tmp_path / "pwm_fpga_demo.v"
    rtl.write_text(
        "module pwm_fpga_demo (\n"
        "  input wire clk,\n"
        "  output wire led\n"
        ");\n"
        "reg [7:0] counter;\n"
        "always @(posedge clk) counter <= counter + 1'b1;\n"
        "assign led = counter[7];\n"
        "endmodule\n",
        encoding="utf-8",
    )
    coverage_ports = coverage_ports_from_rtl([str(rtl)], "pwm_fpga_demo")
    testbench_ports = testbench_ports_from_rtl([str(rtl)], "pwm_fpga_demo")
    assert {port["name"] for port in coverage_ports} == {"clk", "led"}
    assert testbench_ports == coverage_ports

    effective_spec = {"ports": coverage_ports}
    coverage = _build_coverage_points(effective_spec, "pwm_fpga_demo")
    assert [point["name"] for point in coverage["input_points"]] == ["clk"]
    assert [point["name"] for point in coverage["output_points"]] == ["led"]

    clocks, resets = _infer_clocks_resets(effective_spec, testbench_ports)
    generated_test = _gen_cocotb_test(effective_spec, "pwm_fpga_demo", clocks, resets, rtl_files=[str(rtl)])
    assert clocks == ["clk"]
    assert 'cocotb.start_soon(Clock(getattr(dut, "clk")' in generated_test
    assert '"led"' in generated_test


def test_timing_closure_locks_passing_seed_and_winning_artifact(tmp_path, monkeypatch):
    from agents.fpga import fpga_timing_closure_agent as closure

    monkeypatch.setattr(closure, "fpga_dir", lambda _state, *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(closure, "publish_json", lambda *_args, **_kwargs: None)
    state = {
        "target_frequency_mhz": 75.0,
        "fpga_closure_mode": "balanced",
        "allow_automatic_rtl_timing_repair": False,
        "fpga": {"target": {"board": "icebreaker", "device": "up5k", "package": "sg48"}},
    }
    first = tmp_path / "seed_12.asc"
    first.write_text("first", encoding="utf-8")
    state["fpga"].update({
        "place_route": {"status": "completed", "seed": 12, "max_frequency_mhz": 70.0, "timing_met": False, "asc": str(first)},
        "timing_drc": {"max_frequency_mhz": 70.0, "timing_met": False},
    })
    closure.run_agent(state)
    first_winner = state["fpga"]["timing_closure"]["implementation_lock"]["winning_pnr_output"]

    second = tmp_path / "seed_45.asc"
    second.write_text("winner", encoding="utf-8")
    state["fpga_timing_closure_iteration_index"] = 1
    state["fpga"].update({
        "place_route": {"status": "completed", "seed": 45, "max_frequency_mhz": 80.0, "timing_met": True, "asc": str(second)},
        "timing_drc": {"max_frequency_mhz": 80.0, "timing_met": True},
    })
    closure.run_agent(state)

    lock = state["fpga"]["timing_closure"]["implementation_lock"]
    assert lock["status"] == "locked"
    assert lock["selected_seed"] == 45
    assert lock["achieved_frequency_mhz"] == 80.0
    assert Path(lock["winning_pnr_output"]).read_text(encoding="utf-8") == "winner"
    assert Path(first_winner).read_text(encoding="utf-8") == "first"
    assert first_winner != lock["winning_pnr_output"]
    assert lock["winning_pnr_sha256"]


def test_automatic_timing_repair_preserves_module_interface(tmp_path, monkeypatch):
    from agents.fpga import fpga_timing_rtl_repair_agent as repair

    rtl = tmp_path / "top.v"
    rtl.write_text("module top(input wire clk, output wire led); assign led = clk; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(repair, "fpga_dir", lambda _state, *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(repair, "publish_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repair, "complete_text", lambda *_args, **_kwargs: json.dumps({
        "summary": "registered internal path",
        "files": [{"path": "top.v", "content": "module top(input wire clk, output wire led); reg q; always @(posedge clk) q <= clk; assign led = q; endmodule"}],
    }))
    state = {
        "allow_automatic_rtl_timing_repair": True,
        "target_frequency_mhz": 100.0,
        "fpga": {"rtl_files": [str(rtl)], "timing_drc": {"max_frequency_mhz": 80.0}, "place_route": {}},
    }

    repair.run_agent(state)

    report = state["fpga"]["timing_rtl_repair"]
    assert report["applied"] is True
    assert state["fpga_timing_rtl_repair_used"] is True
    assert state["_fpga_pre_timing_repair_rtl_files"] == [str(rtl)]
    assert state["fpga"]["rtl_files"] != [str(rtl)]



def test_fpga_repair_verification_gate_requires_explicit_simulation_pass(tmp_path):
    from agents.fpga.fpga_verification_gate import verification_passed

    assert verification_passed({}) == (False, "simulation_summary_missing")
    summary = tmp_path / "simulation_summary_coverage.json"
    summary.write_text(json.dumps({"simulation": {"total": 2, "pass": 1, "fail": 1}}), encoding="utf-8")
    passed, reason = verification_passed({"simulation_summary_coverage_json": str(summary)})
    assert passed is False
    assert reason == "simulation_failed:1/2_passed"

    summary.write_text(json.dumps({"simulation": {"total": 2, "pass": 2, "fail": 0}}), encoding="utf-8")
    assert verification_passed({"simulation_summary_coverage_json": str(summary)}) == (True, "simulation_passed:2/2")


def test_fpga_repair_verification_gate_requires_formal_when_enabled(tmp_path):
    from agents.fpga.fpga_verification_gate import verification_passed

    summary = tmp_path / "simulation_summary_coverage.json"
    summary.write_text(json.dumps({
        "simulation": {"total": 1, "pass": 1, "fail": 0},
        "formal": {"status": "failed"},
    }), encoding="utf-8")
    state = {"simulation_summary_coverage_json": str(summary), "toggles": {"enable_formal": True}}
    passed, reason = verification_passed(state)
    assert passed is False
    assert reason == "formal_not_passed:failed"

    summary.write_text(json.dumps({
        "simulation": {"total": 1, "pass": 1, "fail": 0},
        "formal": {"status": "pass"},
    }), encoding="utf-8")
    assert verification_passed(state) == (True, "simulation_passed:1/1")

def test_inline_fpga_verification_closure_reruns_verification_before_judging():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    closure_block_start = main_source.index(
        'if app_name in {"fpga", "fpga2rtl", "fpga_implementation"} and bool(shared_state.get("run_fpga_verification_closure_loop")):'
    )
    closure_block_end = main_source.index(
        '# Run nodes (loop_type="digital" so it uses DIGITAL_AGENT_FUNCTIONS)',
        closure_block_start,
    )
    closure_block = main_source[closure_block_start:closure_block_end]

    assert "closure_iteration_nodes = closure_analysis_nodes + verify_rerun_nodes + closure_judge_nodes" in closure_block
    assert "nodes=closure_iteration_nodes" in closure_block
    assert 'shared_state.get("closure_iteration_judgement")' in closure_block
    assert 'judge.get("stop_reason") in {"closure_achieved", "no_measurable_improvement"}' in closure_block


def test_nextpnr_log_exposes_routed_lut4_and_flip_flop_counts(tmp_path):
    log = tmp_path / "nextpnr-ice40.log"
    log.write_text(
        "Info: 39 LCs used as LUT4 only\n"
        "Info: 33 LCs used as LUT4 and DFF\n"
        "Info: 0 LCs used as DFF only\n"
        "Info: ICESTORM_LC: 79/5280 1%\n",
        encoding="utf-8",
    )

    parsed = _parse_nextpnr(str(log))

    assert parsed["routed_lut4_cells"] == 72
    assert parsed["routed_flip_flops"] == 33


def test_fpga_dashboard_prefers_authoritative_formal_result(tmp_path, monkeypatch):
    simulation_path = tmp_path / "simulation_summary_coverage.json"
    simulation_path.write_text(json.dumps({
        "simulation": {"total": 1, "pass": 1, "fail": 0},
        "formal": {"status": "not_enabled"},
        "toolchain": {"formal": "none"},
    }), encoding="utf-8")
    published = {}
    monkeypatch.setattr(fpga_dashboard_agent, "publish_json", lambda _state, _agent, _subdir, _filename, summary: published.update(summary))
    state = {
        "simulation_summary_coverage_json": str(simulation_path),
        "vv": {"formal": {"status": "pass", "toolchain": {"formal": "symbiyosys", "formal_solver": "z3"}}},
        "_participating_agents": ["Digital Formal Verification Agent", "FPGA Dashboard Agent"],
        "fpga": {"target": {}, "bitstream": {"status": "completed"}},
    }

    fpga_dashboard_agent.run_agent(state)

    assert published["verification"]["formal"]["status"] == "pass"
    assert published["verification"]["toolchain"]["formal"] == "symbiyosys"


def test_balanced_tool_policy_uses_supported_baseline_and_reporting_knobs():
    from agents.fpga.fpga_yosys_synthesis_agent import _yosys_effort_policy

    yosys = _yosys_effort_policy(
        {"fpga_closure_mode": "balanced"}, "synth_ice40", "options: -noflatten -flatten -noabc9 -retime"
    )
    pnr = _nextpnr_effort_policy(
        {"fpga_closure_mode": "balanced", "target_frequency_mhz": 75},
        "nextpnr-ice40",
        "--freq --placer --router --detailed-timing-report",
    )

    assert yosys["effective_options"] == ["-noflatten"]
    assert yosys["strategy"] == "baseline"
    assert pnr["effective_args"] == ["--detailed-timing-report", "--freq", "75"]


def test_advanced_tool_policy_enables_only_architecture_advertised_knobs():
    pnr = _nextpnr_effort_policy(
        {"fpga_closure_mode": "advanced", "target_frequency_mhz": 100},
        "nextpnr-ice40",
        "--placer available: heap, sa; default: heap\n"
        "--router available: router1, router2; default: router1\n"
        "--freq --placer-heap-timingweight --placer-heap-critexp --tmg-ripup --router2-alt-weights",
    )

    assert pnr["effective_args"] == [
        "--placer", "heap",
        "--placer-heap-timingweight", "20",
        "--placer-heap-critexp", "4",
        "--router", "router2",
        "--tmg-ripup",
        "--router2-alt-weights",
        "--freq", "100",
    ]


def test_advanced_tool_policy_does_not_assume_placer_or_router_values():
    pnr = _nextpnr_effort_policy(
        {"fpga_closure_mode": "advanced"},
        "nextpnr-ecp5",
        "--placer available: sa; default: sa\n--router available: router1; default: router1\n"
        "--placer-heap-timingweight --tmg-ripup",
    )

    assert pnr["effective_args"] == []


def test_retime_strategy_disables_abc9_and_never_passes_abc9():
    from agents.fpga.fpga_yosys_synthesis_agent import _yosys_effort_policy

    policy = _yosys_effort_policy(
        {"fpga_closure_mode": "advanced", "fpga_yosys_retime": True},
        "synth_ice40",
        "options: -abc9 -noabc9 -retime",
    )

    assert policy["strategy"] == "retime"
    assert policy["effective_options"] == ["-noabc9", "-retime"]
    assert "-abc9" not in policy["effective_options"]


def test_fpga_frontend_prefers_requested_target_and_shows_simulation_counts():
    source = (Path(__file__).parents[2] / "frontend" / "components" / "WorkflowEvidenceDashboard.tsx").read_text(encoding="utf-8")

    target_block = source[source.index("const targetFrequencyMhz"):source.index("const boardInputFrequencyMhz")]
    assert "constraints.target_frequency_mhz" in target_block
    assert "target.target_frequency_mhz" in target_block
    assert "target.default_frequency_mhz" not in target_block
    assert 'title="Simulation Total"' in source
    assert 'title="Simulation Passed"' in source
    assert 'title="Simulation Failed"' in source
    assert 'title="Routing Utilization"' in source
    assert 'title="Board Input Clock"' in source
    assert 'title="Implementation Target"' in source
    assert 'title="Achieved Fmax"' in source


def test_missing_route_does_not_become_zero_utilization(monkeypatch):
    published = {}
    monkeypatch.setattr(fpga_dashboard_agent, "publish_json", lambda _state, _agent, _subdir, _filename, data: published.update(data))
    state = {
        "target_frequency_mhz": 12.0,
        "fpga": {
            "target": {"default_frequency_mhz": 27.0, "resources": {"logic_cells": 8640}},
            "synthesis": {"logical_cells_available": 8640},
            "place_route": {"status": "failed", "failure_kind": "tool_unavailable"},
            "timing_drc": {"status": "blocked"},
            "bitstream": {"status": "blocked"},
        },
    }

    fpga_dashboard_agent.run_agent(state)

    assert published["target"]["target_frequency_mhz"] == 12.0
    assert published["routed_result"]["logical_cells_used"] is None
    assert published["routed_result"]["logic_utilization_percent"] is None


def test_timing_closure_does_not_seed_sweep_when_nextpnr_is_unavailable(tmp_path, monkeypatch):
    from agents.fpga import fpga_timing_closure_agent as closure

    monkeypatch.setattr(closure, "fpga_dir", lambda _state, *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(closure, "publish_json", lambda *_args, **_kwargs: None)
    state = {
        "target_frequency_mhz": 12.0,
        "allow_nextpnr_seed_sweep": True,
        "fpga": {
            "target": {"board": "gowin_tang_nano_9k"},
            "synthesis": {"status": "completed"},
            "place_route": {
                "status": "failed",
                "command": {"status": "tool_unavailable", "returncode": 127, "error": "nextpnr is not configured"},
            },
            "timing_drc": {"status": "blocked"},
        },
    }

    closure.run_agent(state)

    plan = state["fpga"]["timing_closure"]["plan"]
    assert plan["status"] == "implementation_unavailable"
    assert plan["selected_restart_stage"] is None
    assert "fpga_nextpnr_seed" not in state
    assert state["fpga_implementation_unavailable_reason"] == "nextpnr is not configured"


def test_himbaechel_uarch_selector_is_version_aware():
    assert _himbaechel_uarch_args("/opt/chiploop-eda/bin/nextpnr-himbaechel", "gowin", "--device --vopt") == []
    assert _himbaechel_uarch_args("nextpnr-himbaechel", "gowin", "--uarch arg --device") == ["--uarch", "gowin"]


def test_timing_closure_does_not_retry_invalid_nextpnr_cli(tmp_path, monkeypatch):
    from agents.fpga import fpga_timing_closure_agent as closure

    monkeypatch.setattr(closure, "fpga_dir", lambda _state, *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(closure, "publish_json", lambda *_args, **_kwargs: None)
    state = {
        "target_frequency_mhz": 12.0,
        "allow_nextpnr_seed_sweep": True,
        "fpga": {
            "target": {"board": "gowin_tang_nano_9k"},
            "place_route": {
                "status": "failed",
                "command": {"returncode": 255, "stdout_tail": "unrecognised option '--uarch'\n"},
            },
        },
    }

    closure.run_agent(state)

    plan = state["fpga"]["timing_closure"]["plan"]
    assert plan["status"] == "implementation_unavailable"
    assert plan["selected_restart_stage"] is None
    assert "fpga_nextpnr_seed" not in state
    assert "unrecognised option" in state["fpga_implementation_unavailable_reason"]


def test_fpga_dashboard_api_preserves_generated_target_frequency():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    endpoint = source[source.index("def dashboard_json_artifact"):source.index("def _usage_number")]

    assert 'generated = parts.get("fpga_dashboard.json")' in endpoint
    assert "return generated" in endpoint
    assert 'constraints.get("target_frequency_mhz")' in endpoint
    assert 'target.get("default_frequency_mhz")' not in endpoint
