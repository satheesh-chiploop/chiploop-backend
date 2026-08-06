import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital.digital_floorplan_sizing import implementation_die_area, top_level_io_bits
from agents.digital.digital_synthesis_agent import (
    _instantiated_sram_requirements,
    _suspicious_flat_storage_registers,
)


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


def test_physical_ai_hem_pins_motor_control_top_for_rtl_and_asic():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")

    assert source.count('payload.get("top_module") or "motor_control_top"') >= 2
    assert "ASIC MEMORY CONTRACT (mandatory)" in source
