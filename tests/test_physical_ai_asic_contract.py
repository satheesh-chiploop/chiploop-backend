import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital.digital_floorplan_sizing import implementation_die_area, top_level_io_bits
from agents.digital.digital_synthesis_agent import (
    _instantiated_sram_requirements,
    _suspicious_flat_storage_registers,
)
from agents.digital.digital_rtl_agent import (
    _merge_rtl_repair_output,
    _parse_named_verilog_blocks,
    _sanitize_child_output_instance_connections,
    _validate_connectivity_contract,
)
from physical_ai.handoff import resolve_design_identity


def test_die_area_scales_for_scalar_equivalent_io_width(tmp_path):
    rtl = tmp_path / "motor_control_top.sv"
    rtl.write_text(
        "module motor_control_top (\n"
        " input logic clk,\n"
        " input logic reset_n,\n"
        " input logic [31:0] request_data,\n"
        " output logic [31:0] response_data\n"
        "); endmodule\n",
        encoding="utf-8",
    )

    assert top_level_io_bits([str(rtl)], "motor_control_top") == 66
    assert implementation_die_area([str(rtl)], "motor_control_top") == ("0 0 160 160", 66, 160.0)


def test_flattened_physical_ai_history_registers_are_detected(tmp_path):
    rtl = tmp_path / "bad_storage.sv"
    lines = ["module bad_storage;"]
    lines.extend(f"reg [63:0] seq_history{i};" for i in range(32))
    lines.append("endmodule")
    rtl.write_text("\n".join(lines), encoding="utf-8")

    registers = _suspicious_flat_storage_registers([str(rtl)])

    assert len(registers) == 32
    assert sum(int(item["estimated_bits"]) for item in registers) == 2048


def test_explicit_sky130_sram_instance_is_discovered(tmp_path):
    rtl = tmp_path / "motor_control_top.sv"
    rtl.write_text(
        "module motor_control_top;\n"
        "sky130_sram_1kbyte_1rw1r_32x256_8 u_payload (.clk0(clk));\n"
        "endmodule\n",
        encoding="utf-8",
    )

    requirements = _instantiated_sram_requirements([str(rtl)])

    assert requirements == [{
        "file": "motor_control_top.sv",
        "path": str(rtl),
        "macro_name_requested": "sky130_sram_1kbyte_1rw1r_32x256_8",
        "width_bits": 32,
        "depth": 256,
        "estimated_bits": 8192,
    }]


def test_physical_ai_hem_uses_selected_model_design_identity():
    aero = {
        "implementation_path": "digital_ip_asic",
        "model_top_module": "adaptive_aero_control_top",
        "model_project_name": "adaptive_aero_control",
        "rtl_spec_text": "Implement active-aero request validation, command limits, watchdog, and fallback.",
    }

    assert resolve_design_identity(aero) == ("adaptive_aero_control_top", "adaptive_aero_control")

    main_source = Path("main.py").read_text(encoding="utf-8")
    assert 'top_module, project_name = resolve_design_identity(payload)' in main_source
    assert 'f"The required synthesizable top module is {top_module}' in main_source
    assert '"top_module": top_module' in main_source
    assert "motor-control request/response" not in main_source


def test_physical_ai_motor_model_keeps_motor_top():
    motor = {"implementation_path": "fpga_prototype", "model_top_module": "motor_control_top", "model_project_name": "pmsm_motor_control"}

    assert resolve_design_identity(motor) == ("motor_control_top", "pmsm_motor_control")


def test_rtl_repair_overlay_preserves_unchanged_hierarchy_files():
    previous = (
        "---BEGIN motor_control_top.v---\nmodule motor_control_top; bad u(); endmodule\n---END motor_control_top.v---\n"
        "---BEGIN child.v---\nmodule child; endmodule\n---END child.v---"
    )
    repair = (
        "---BEGIN motor_control_top.v---\nmodule motor_control_top; child u(); endmodule\n---END motor_control_top.v---"
    )

    merged = _merge_rtl_repair_output(previous, repair, ["motor_control_top.v", "child.v"])
    files = _parse_named_verilog_blocks(merged)

    assert list(files) == ["motor_control_top.v", "child.v"]
    assert "child u()" in files["motor_control_top.v"]
    assert files["child.v"] == "module child; endmodule"


def test_structural_child_output_removes_invalid_module_scope_alias():
    files = {
        "top.v": (
            "module top(output service_req);\n"
            "assign service_req = request_fsm.service_req;\n"
            "request_fsm u_request_fsm (.service_req(service_req));\n"
            "endmodule"
        ),
        "request_fsm.v": "module request_fsm(output service_req); assign service_req = 1'b1; endmodule",
    }

    sanitized = _sanitize_child_output_instance_connections(files)

    assert "request_fsm.service_req" not in sanitized["top.v"]
    assert ".service_req(service_req)" in sanitized["top.v"]


def test_top_level_safety_input_is_valid_external_signal_owner():
    spec = {
        "hierarchy": {
            "top_module": {
                "name": "motor_control_top",
                "ports": [{"name": "safe_override_in", "direction": "input", "width": 1}],
                "rtl_output_file": "motor_control_top.v",
            },
            "modules": [{
                "name": "safe_fallback_manager",
                "ports": [{"name": "safe_override_in", "direction": "input", "width": 1}],
                "rtl_output_file": "safe_fallback_manager.v",
            }],
        },
        "top_level_connections": [{
            "top_port": "safe_override_in",
            "connected_to": ["safe_fallback_manager.safe_override_in"],
        }],
        "inter_module_signals": [],
        "signal_ownership": [{
            "signal": "safe_override_in",
            "owner": "motor_control_top.safe_override_in",
        }],
    }

    assert _validate_connectivity_contract(spec, "hierarchical") == []
