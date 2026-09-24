import json
import ast
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_simulation_execution_agent as execution_agent
from agents.digital import digital_formal_verification_agent as formal_agent
from agents.digital import digital_simulation_summary_coverage_agent as summary_agent
from agents.digital import digital_testbench_generator_agent as tb_agent


def test_assertion_pass_rate_counts_unique_failed_checkers_not_repeated_events():
    sim = {"assertion_failures": [
        {"checker_id": checker}
        for _seed in range(4)
        for checker in ("a_req_002", "a_req_010", "a_req_011")
    ]}
    assert summary_agent._unique_failed_checker_count(sim, event_count=12) == 3


def test_verilator_makefile_enables_code_coverage():
    text = tb_agent._gen_makefile("pwm_controller")

    assert "override SIM := verilator" in text
    assert "EXTRA_ARGS += --coverage" in text


def test_parse_lcov_info_reports_line_and_branch_coverage(tmp_path):
    info = tmp_path / "code_coverage.info"
    info.write_text(
        "\n".join(
            [
                "SF:rtl/pwm_controller.v",
                "LF:10",
                "LH:8",
                "BRF:4",
                "BRH:3",
                "end_of_record",
            ]
        ),
        encoding="utf-8",
    )

    parsed = execution_agent._parse_lcov_info(str(info))

    assert parsed["line_coverage_pct"] == 80.0
    assert parsed["branch_coverage_pct"] == 75.0
    assert parsed["condition_coverage_pct"] == 75.0
    assert parsed["condition_source"] == "verilator_lcov_branch_proxy"
    assert parsed["toggle_coverage_pct"] is None


def test_parse_verilator_lcov_da_records_reports_line_coverage(tmp_path):
    info = tmp_path / "code_coverage.info"
    info.write_text(
        "\n".join(
            [
                "TN:verilator_coverage",
                "SF:../../handoff/rtl/pwm_controller.v",
                "DA:2,26",
                "DA:3,0",
                "DA:4,1",
                "BRF:4",
                "BRH:1",
                "end_of_record",
            ]
        ),
        encoding="utf-8",
    )

    parsed = execution_agent._parse_lcov_info(str(info))

    assert parsed["line_found"] == 3
    assert parsed["line_hit"] == 2
    assert parsed["line_coverage_pct"] == 66.67
    assert parsed["branch_coverage_pct"] == 25.0
    assert parsed["condition_coverage_pct"] == 25.0
    assert parsed["toggle_source"] == "not_reported_by_verilator_lcov"


def test_parse_verilator_lcov_excludes_declaration_points_from_rtl_branch_coverage(tmp_path):
    rtl_dir = tmp_path / "handoff" / "rtl"
    rtl_dir.mkdir(parents=True)
    rtl = rtl_dir / "controller.v"
    rtl.write_text(
        "\n".join([
            "module controller(input clk, input reset_n, input enable);",
            "  reg [7:0] counter;",
            "  always @(posedge clk) begin",
            "    if (!reset_n) counter <= 0;",
            "    else if (enable) counter <= counter + 1;",
            "  end",
            "endmodule",
        ]),
        encoding="utf-8",
    )
    reports = tmp_path / "tb" / "reports"
    reports.mkdir(parents=True)
    info = reports / "code_coverage.info"
    info.write_text(
        "\n".join([
            "TN:verilator_coverage",
            "SF:../../handoff/rtl/controller.v",
            "DA:1,10",
            "DA:4,5",
            "DA:5,4",
            "BRDA:1,0,0,10",
            "BRDA:1,0,1,0",
            "BRDA:2,0,0,5",
            "BRDA:4,0,0,5",
            "BRDA:4,0,1,1",
            "BRDA:5,0,0,4",
            "BRDA:5,0,1,2",
            "BRF:7",
            "BRH:5",
            "end_of_record",
        ]),
        encoding="utf-8",
    )

    parsed = execution_agent._parse_lcov_info(str(info))

    assert parsed["branch_found"] == 4
    assert parsed["branch_hit"] == 4
    assert parsed["branch_coverage_pct"] == 100.0
    assert parsed["branch_source"] == "rtl_control_flow_from_verilator_lcov"
    assert parsed["condition_coverage_pct"] is None
    assert parsed["condition_source"] == "unavailable_from_verilator_lcov"


def test_parse_verilator_annotated_points_reports_toggle_coverage(tmp_path):
    annotated = tmp_path / "annotated"
    annotated.mkdir()
    (annotated / "pwm_controller.sv").write_text(
        "\n".join(
            [
                " 000001 logic pwm_out;",
                "+000003 point: type=toggle comment=pwm_out[0] hier=TOP.pwm_controller",
                "-000000 point: type=toggle comment=counter_value[0] hier=TOP.pwm_controller",
                "+000001 point: type=line comment=assign",
            ]
        ),
        encoding="utf-8",
    )

    parsed = execution_agent._parse_verilator_annotated_toggle_coverage(str(annotated))

    assert parsed["toggle_found"] == 2
    assert parsed["toggle_hit"] == 1
    assert parsed["toggle_coverage_pct"] == 50.0
    assert parsed["toggle_source"] == "verilator_coverage_annotate_points"
    assert parsed["missed_toggle_points"][0]["point"].startswith("comment=counter_value")


def test_testbench_generator_can_select_directed_random_or_both():
    assert tb_agent._selected_default_tests("directed") == ["smoke_test"]


def test_spi_wrapper_gets_complete_frame_directed_test(tmp_path):
    rtl = tmp_path / "wrapper.sv"
    rtl.write_text(
        "module wrapper(input clk,input reset_n,input spi_sclk,input spi_cs_n,input spi_mosi,output spi_miso); "
        "localparam integer FRAME_BITS = 224; assign spi_miso=1'b0; endmodule",
        encoding="utf-8",
    )
    ports = [
        {"name": "clk", "direction": "input"},
        {"name": "reset_n", "direction": "input"},
        {"name": "spi_sclk", "direction": "input"},
        {"name": "spi_cs_n", "direction": "input"},
        {"name": "spi_mosi", "direction": "input"},
        {"name": "spi_miso", "direction": "output"},
    ]
    spec = {"ports": ports}

    tests = tb_agent._detected_directed_tests(ports, spec, [str(rtl)])
    generated = tb_agent._gen_cocotb_test(
        spec, "wrapper", ["clk", "spi_sclk"], [{"name": "reset_n", "active_low": True}], [str(rtl)]
    )

    assert "spi_transport_frame_directed" in tests
    assert "for bit in range(224 - 1, -1, -1)" in generated
    assert "application_vectors" in generated
    assert "frame_mask" in generated
    manifest = tb_agent._build_testcases_manifest(
        "wrapper", ["clk", "spi_sclk"], [{"name": "reset_n"}], "digital", "both", tests
    )
    spi_case = next(item for item in manifest["tests"] if item["name"] == "spi_transport_frame_directed")
    assert spi_case["timeout_ns"] == 120000
    assert "spi_transport_frame_directed" in manifest["default_tests"]


def test_application_spec_generates_boundary_stimulus_instead_of_fixed_cases():
    ports = [
        {"name": "clk", "direction": "input"},
        {"name": "reset_n", "direction": "input"},
        {"name": "mode", "direction": "input", "width": 2},
        {"name": "threshold", "direction": "input", "width": 8},
        {"name": "alarm", "direction": "output"},
    ]
    spec = {"ports": ports, "requirements": [{"name": "Raise alarm above threshold"}]}
    clocks, resets = tb_agent._infer_clocks_resets(spec, ports)
    plan = tb_agent._application_stimulus_plan(spec, ports, clocks, resets)
    generated = tb_agent._gen_cocotb_test(spec, "monitor", clocks, resets)

    assert [point["name"] for point in plan] == ["mode", "threshold"]
    assert plan[0]["values"] == [0, 1, 3, 2]
    assert 255 in plan[1]["values"] and 128 in plan[1]["values"] and 85 in plan[1]["values"]
    assert "application_spec_boundary_directed" in generated
    assert "stimulus_plan" in generated
    advance = generated.split("async def _advance_time", 1)[1].split("def _safe_drive_random", 1)[0]
    assert advance.index("await RisingEdge") < advance.index('await Timer(1, units="ns")')


def test_structured_feature_contract_generates_monitor_checker_and_traceability():
    ports = [
        {"name": "clk", "direction": "input"},
        {"name": "reset_n", "direction": "input"},
        {"name": "request", "direction": "input"},
        {"name": "mode", "direction": "input", "width": 2},
        {"name": "done", "direction": "output"},
        {"name": "result", "direction": "output", "width": 8},
    ]
    spec = {
        "ports": ports,
        "features": [{
            "id": "mode_one_completion",
            "description": "Mode one request completes with the specified result.",
            "stimulus": {"request": 1, "mode": 1},
            "expected": {"done": 1, "result": {"min": 1, "max": 255}},
            "within_cycles": 4,
        }],
    }
    contracts = tb_agent.compile_feature_contracts(spec, ports)
    generated = tb_agent._gen_cocotb_test(spec, "feature_top", ["clk"], [{"name": "reset_n", "active_low": True}])

    assert contracts[0]["status"] == "executable"
    assert contracts[0]["monitors"][:2] == ["done", "result"]
    assert {"request", "mode"}.issubset(contracts[0]["monitors"])
    assert contracts[0]["coverage_bins"] == [
        "mode_one_completion.stimulus_applied", "mode_one_completion.expected_observed"
    ]
    assert "async def feature_contract_directed" in generated
    assert 'if "min" in rule' in generated
    assert "'executable': True" in generated
    assert '"executable": true' not in generated
    feature_loop = generated.split("for feature in feature_contracts:", 1)[1]
    assert feature_loop.index('getattr(dut, "reset_n").value = 0') < feature_loop.index('for step in feature.get("stimulus_steps", [])')
    reset_prefix = feature_loop.split('for step in feature.get("stimulus_steps", [])', 1)[0]
    assert "await _advance_time(dut)" in reset_prefix
    ast.parse(generated)


def test_feature_contract_compiler_normalizes_multicycle_port_suffixes_to_steps():
    ports = [
        {"name": "wr_en", "direction": "input"},
        {"name": "wr_addr", "direction": "input", "width": 8},
        {"name": "done", "direction": "output"},
    ]
    contracts = tb_agent.compile_feature_contracts({"feature_contracts": [{
        "id": "two_writes", "stimulus": {
            "wr_en": 1, "wr_addr": 4, "wr_en_2": 1, "wr_addr_2": 8,
        }, "expected": {"done": 1}, "within_cycles": 1,
    }]}, ports)
    assert contracts[0]["status"] == "executable"
    assert contracts[0]["stimulus_steps"] == [
        {"signals": {"wr_en": 1, "wr_addr": 4}, "cycles": 1},
        {"signals": {"wr_en": 1, "wr_addr": 8}, "cycles": 1},
    ]


def test_feature_contract_deadline_does_not_double_count_stimulus_cycles():
    ports = [
        {"name": "enable", "direction": "input"},
        {"name": "count", "direction": "output", "width": 8},
    ]
    contracts = tb_agent.compile_feature_contracts({"feature_contracts": [{
        "id": "two_enabled_cycles",
        "stimulus": {"steps": [
            {"signals": {"enable": 1}, "cycles": 1},
            {"signals": {"enable": 1}, "cycles": 1},
        ]},
        "expected": {"count": 2},
        "within_cycles": 2,
    }]}, ports)

    assert contracts[0]["stimulus_cycles"] == 2
    assert contracts[0]["deadline_cycles"] == 2
    assert contracts[0]["wait_cycles"] == 0


def test_feature_contract_compiler_accepts_explicit_empty_wait_step():
    ports = [{"name": "done", "direction": "output"}]
    contracts = tb_agent.compile_feature_contracts({"feature_contracts": [{
        "id": "wait_only",
        "stimulus": {"steps": [{"signals": {}, "cycles": 2}]},
        "expected": {"done": {"eq": 1}},
    }]}, ports)

    assert contracts[0]["executable"] is True
    assert contracts[0]["unresolved_bindings"] == []
    assert contracts[0]["stimulus_steps"] == [{"signals": {}, "cycles": 2}]


def test_feature_contract_compiler_canonicalizes_duplicated_signals_wrapper_as_wait():
    ports = [
        {"name": "enable", "direction": "input"},
        {"name": "done", "direction": "output"},
    ]
    contracts = tb_agent.compile_feature_contracts({"feature_contracts": [{
        "id": "settle_then_done",
        "stimulus": {"steps": [
            {"signals": {"enable": 1}, "cycles": 1},
            {"signals": {"signals": {}}, "cycles": 3},
        ]},
        "expected": {"done": 1},
    }]}, ports)

    assert contracts[0]["executable"] is True
    assert contracts[0]["unresolved_bindings"] == []
    assert contracts[0]["stimulus_steps"] == [
        {"signals": {"enable": 1}, "cycles": 1},
        {"signals": {}, "cycles": 3},
    ]


def test_free_text_feature_is_traceable_but_does_not_invent_checker():
    ports = [{"name": "alarm", "direction": "output"}]
    contracts = tb_agent.compile_feature_contracts(
        {"requirements": ["Alarm shall indicate a hazardous condition."]}, ports
    )
    assert contracts[0]["status"] == "trace_only"
    assert contracts[0]["monitors"] == ["alarm"]
    assert contracts[0]["expected"] == {}
    assert contracts[0]["non_executable_reason"]


def test_testbench_run_rejects_feature_without_executable_checker(tmp_path, monkeypatch):
    spec_path = tmp_path / "digital_spec.json"
    spec_path.write_text(json.dumps({
        "name": "alarm_top",
        "ports": [{"name": "alarm", "direction": "output", "width": 1}],
        "requirements": ["Alarm shall indicate a hazardous condition."],
    }), encoding="utf-8")
    monkeypatch.setattr(tb_agent, "_record_text", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="Every feature requires an explicit"):
        tb_agent.run_agent({
            "workflow_id": "strict-features", "workflow_dir": str(tmp_path),
            "digital_spec_json": str(spec_path), "top_module": "alarm_top",
            "rtl_files": [],
        })
    assert tb_agent._selected_default_tests("random") == ["constrained_random_sanity"]
    assert tb_agent._selected_default_tests("both") == ["smoke_test", "constrained_random_sanity"]


def test_formal_sby_uses_selected_solver():
    text = formal_agent._gen_sby("pwm_controller", ["rtl/pwm_controller.v"], "clk", None, "boolector")

    assert "smtbmc boolector" in text


def test_formal_sby_paths_are_relative_to_formal_workdir(tmp_path):
    workflow_dir = tmp_path / "backend" / "workflows" / "wf"
    rtl = workflow_dir / "handoff" / "rtl" / "pwm_controller.v"
    formal_dir = workflow_dir / "vv" / "formal"
    rtl.parent.mkdir(parents=True)
    formal_dir.mkdir(parents=True)
    rtl.write_text("module pwm_controller; endmodule\n", encoding="utf-8")

    text = formal_agent._gen_sby("pwm_controller", [str(rtl)], "clk", None, "z3", str(formal_dir))

    assert "backend/workflows/wf/handoff" not in text
    assert "read_verilog -sv pwm_controller.v" in text
    assert "../../handoff/rtl/pwm_controller.v" in text.replace("\\", "/")


def test_summary_agent_includes_code_assertion_formal_and_golden_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(summary_agent, "_record_text", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifact").mkdir()

    workflow_dir = tmp_path / "workflow"
    reports_dir = workflow_dir / "vv" / "tb" / "reports"
    run_logs_dir = reports_dir / "run_logs"
    run_logs_dir.mkdir(parents=True)
    (reports_dir / "simulation_execution_summary.json").write_text(
        json.dumps({"total": 2, "pass": 2, "fail": 0}),
        encoding="utf-8",
    )
    (reports_dir / "functional_coverage_summary.json").write_text(
        json.dumps({"functional_coverage_pct": 66.67, "bins_hit": 4, "total_bins": 6}),
        encoding="utf-8",
    )
    (reports_dir / "code_coverage_summary.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "line_coverage_pct": 81.25,
                "line_hit": 13,
                "line_found": 16,
                "branch_coverage_pct": 50.0,
                "branch_hit": 1,
                "branch_found": 2,
                "condition_coverage_pct": 50.0,
                "condition_hit": 1,
                "condition_found": 2,
                "condition_source": "verilator_lcov_branch_proxy",
                "toggle_coverage_pct": None,
                "toggle_source": "not_reported_by_verilator_lcov",
            }
        ),
        encoding="utf-8",
    )
    sva_path = workflow_dir / "vv" / "tb" / "pwm_sva.sv"
    sva_path.write_text(
        "a_one: assert property(p_one);\na_two: assert property(p_two);\n",
        encoding="utf-8",
    )

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(workflow_dir),
        "sva_assertions_path": str(sva_path),
        "vv": {
            "formal": {"run": {"available": True, "attempted": True, "returncode": 0}},
            "golden_model": {"top_module": "pwm_controller"},
        },
    }

    summary_agent.run_agent(state)

    summary = json.loads((reports_dir / "simulation_summary_coverage.json").read_text(encoding="utf-8"))
    assert summary["coverage"]["functional"]["coverage_pct"] == 66.67
    assert summary["coverage"]["code"]["line_coverage_pct"] == 81.25
    assert summary["coverage"]["code"]["branch_coverage_pct"] == 50.0
    assert summary["coverage"]["code"]["condition_coverage_pct"] == 50.0
    assert summary["coverage"]["code"]["toggle_coverage_pct"] is None
    assert summary["coverage"]["assertions"]["assertions_generated"] == 2
    assert summary["coverage"]["assertions"]["assertion_pass_pct"] == 100.0
    assert summary["formal"]["status"] == "pass"
    assert summary["golden_model"]["status"] == "generated"
    assert summary["toolchain"]["simulator"] == "verilator"
    assert summary["toolchain"]["code_coverage"] == "verilator_coverage"
