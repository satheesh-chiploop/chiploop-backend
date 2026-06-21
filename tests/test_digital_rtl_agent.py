import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_rtl_agent as agent


def test_module_code_for_name_extracts_top_when_file_contains_children():
    code = """
module register_file(
  output reg [7:0] rd_data
);
  always @(*) begin
    rd_data = 8'h00;
  end
endmodule

module temp_monitor_digital(
  output [7:0] rd_data
);
  wire [7:0] rd_data_w;
  assign rd_data = rd_data_w;
endmodule
"""

    top_code = agent._module_code_for_name(code, "temp_monitor_digital")

    assert "module temp_monitor_digital" in top_code
    assert "module register_file" not in top_code
    assert "rd_data = 8'h00" not in top_code


def test_align_preserves_same_file_helper_modules_needed_by_top():
    code = """
module sky130_sram_1kbyte_1rw1r_32x256_8(
  input clk,
  input csb,
  input web,
  input [7:0] addr,
  input [31:0] din,
  output reg [31:0] dout
);
endmodule

module sram_mbist_demo_controller(input clk);
  wire [31:0] dout;
  sky130_sram_1kbyte_1rw1r_32x256_8 u_sram(
    .clk(clk),
    .csb(1'b0),
    .web(1'b1),
    .addr(8'h00),
    .din(32'h0),
    .dout(dout)
  );
endmodule
"""
    spec = {
        "hierarchy": {
            "top_module": {
                "name": "sram_mbist_demo_controller",
                "rtl_output_file": "sram_mbist_demo_controller.v",
            },
            "modules": [],
        }
    }

    out = agent._align_verilog_map_to_expected_modules({"sram_mbist_demo_controller.v": code}, spec, "hierarchical")
    text = out["sram_mbist_demo_controller.v"]

    assert "module sky130_sram_1kbyte_1rw1r_32x256_8" in text
    assert "module sram_mbist_demo_controller" in text


def test_align_splits_expected_child_modules_from_bundled_output():
    code = """
module top(input clk);
  child u_child(.clk(clk));
endmodule

module child(input clk);
endmodule

module helper(input clk);
endmodule
"""
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "rtl_output_file": "top.v"},
            "modules": [{"name": "child", "rtl_output_file": "child.v", "ports": [{"name": "clk", "direction": "input"}]}],
        }
    }

    out = agent._align_verilog_map_to_expected_modules({"top.v": code}, spec, "hierarchical")

    assert "module top" in out["top.v"]
    assert "module child" not in out["top.v"]
    assert "module child" in out["child.v"]


def test_align_fills_missing_expected_sram_model_with_compatible_macro_adapter():
    code = """
module top(input clk);
endmodule

module sky130_sram_1kbyte_1rw1r_32x256_8(
  input clk,
  input csb,
  input we,
  input [7:0] addr,
  input [31:0] din,
  output [31:0] dout
);
endmodule
"""
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "rtl_output_file": "top.v"},
            "modules": [
                {
                    "name": "demo_sram_32x256_model",
                    "rtl_output_file": "demo_sram_32x256_model.v",
                    "ports": [
                        {"name": "clk", "direction": "input", "width": 1},
                        {"name": "csb", "direction": "input", "width": 1},
                        {"name": "web", "direction": "input", "width": 1},
                        {"name": "addr", "direction": "input", "width": 8},
                        {"name": "din", "direction": "input", "width": 32},
                        {"name": "dout", "direction": "output", "width": 32},
                    ],
                }
            ],
        }
    }

    out = agent._align_verilog_map_to_expected_modules({"top.v": code}, spec, "hierarchical")
    adapter = out["demo_sram_32x256_model.v"]

    assert "module demo_sram_32x256_model" in adapter
    assert "sky130_sram_1kbyte_1rw1r_32x256_8 u_backing_macro" in adapter
    assert ".we(web)" in adapter


def test_stages_prebuilt_sram_model_for_rtl_validation(tmp_path, monkeypatch):
    root = tmp_path / "sram_macros"
    verilog = root / "verilog"
    verilog.mkdir(parents=True)
    model = verilog / "sky130_sram_1kbyte_1rw1r_32x256_8.v"
    model.write_text('module sky130_sram_1kbyte_1rw1r_32x256_8; initial $display("scope %m"); endmodule\n', encoding="utf-8")
    monkeypatch.setenv("CHIPLOOP_SRAM_MACRO_ROOTS", str(root))

    spec = {
        "memory_macros": [
            {
                "kind": "prebuilt_sky130_sram",
                "name": "sky130_sram_1kbyte_1rw1r_32x256_8",
                "depth": 256,
                "data_width": 32,
                "addr_width": 8,
            }
        ]
    }

    staged = agent._stage_memory_macro_models_for_rtl_validation(spec, str(tmp_path / "rtl"))

    assert len(staged) == 1
    staged_text = open(staged[0], encoding="utf-8").read()
    assert "module sky130_sram_1kbyte_1rw1r_32x256_8" in staged_text
    assert "%m" not in staged_text
    assert "%m" in model.read_text(encoding="utf-8")


def test_connectivity_contract_allows_top_internal_signals_to_child_memory_ports():
    spec = {
        "hierarchy": {
            "top_module": {
                "name": "sram_mbist_demo_controller",
                "rtl_output_file": "sram_mbist_demo_controller.v",
                "ports": [
                    {"name": "clk", "direction": "input", "width": 1},
                    {"name": "rd_data", "direction": "output", "width": 32},
                ],
            },
            "modules": [
                {
                    "name": "demo_sram_32x256_wrapper",
                    "rtl_output_file": "demo_sram_32x256_wrapper.v",
                    "ports": [
                        {"name": "clk", "direction": "input", "width": 1},
                        {"name": "csb", "direction": "input", "width": 1},
                        {"name": "web", "direction": "input", "width": 1},
                        {"name": "addr", "direction": "input", "width": 8},
                        {"name": "din", "direction": "input", "width": 32},
                        {"name": "dout", "direction": "output", "width": 32},
                    ],
                }
            ],
        },
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["demo_sram_32x256_wrapper.clk"]}
        ],
        "inter_module_signals": [
            {
                "name": "mem_csb",
                "width": 1,
                "source": "sram_mbist_demo_controller.mem_csb",
                "destinations": ["demo_sram_32x256_wrapper.csb"],
            },
            {
                "name": "mem_dout",
                "width": 32,
                "source": "demo_sram_32x256_wrapper.dout",
                "destinations": ["sram_mbist_demo_controller.mem_dout"],
            },
        ],
        "signal_ownership": [
            {"signal": "mem_csb", "owner": "sram_mbist_demo_controller.mem_csb"},
            {"signal": "mem_dout", "owner": "demo_sram_32x256_wrapper.dout"},
        ],
    }

    assert agent._validate_connectivity_contract(spec, "hierarchical") == []


def test_align_repairs_expected_memory_wrapper_port_widths_from_spec():
    code = """
module demo_sram_32x256_wrapper (
  input clk,
  input csb,
  input web,
  input [7:0] addr,
  input [31:0] din,
  output dout
);
wire dout_int;
assign dout = dout_int;
endmodule
"""
    spec = {
        "hierarchy": {
            "top_module": {"name": "demo_sram_32x256_wrapper", "rtl_output_file": "demo_sram_32x256_wrapper.v"},
            "modules": [
                {
                    "name": "demo_sram_32x256_wrapper",
                    "rtl_output_file": "demo_sram_32x256_wrapper.v",
                    "ports": [
                        {"name": "clk", "direction": "input", "width": 1},
                        {"name": "csb", "direction": "input", "width": 1},
                        {"name": "web", "direction": "input", "width": 1},
                        {"name": "addr", "direction": "input", "width": 8},
                        {"name": "din", "direction": "input", "width": 32},
                        {"name": "dout", "direction": "output", "width": 32},
                    ],
                }
            ],
        }
    }

    out = agent._align_verilog_map_to_expected_modules({"demo_sram_32x256_wrapper.v": code}, spec, "hierarchical")
    text = out["demo_sram_32x256_wrapper.v"]

    assert "output [31:0] dout" in text
    assert "wire [31:0] dout_int;" in text
    issues, _, _ = agent._validate_spec_vs_rtl(spec, "hierarchical", out)
    assert not [issue for issue in issues if "width mismatch" in issue]


def test_validate_catches_scalar_ansi_port_after_wide_input():
    code = """
module demo_sram_32x256_wrapper (
  input [31:0] din,
  output dout
);
endmodule
"""
    spec = {
        "hierarchy": {
            "top_module": {"name": "demo_sram_32x256_wrapper", "rtl_output_file": "demo_sram_32x256_wrapper.v"},
            "modules": [
                {
                    "name": "demo_sram_32x256_wrapper",
                    "rtl_output_file": "demo_sram_32x256_wrapper.v",
                    "ports": [
                        {"name": "din", "direction": "input", "width": 32},
                        {"name": "dout", "direction": "output", "width": 32},
                    ],
                }
            ],
        }
    }

    issues, _, _ = agent._validate_spec_vs_rtl(spec, "hierarchical", {"demo_sram_32x256_wrapper.v": code})

    assert any("port 'dout' width mismatch: spec=32, rtl=1" in issue for issue in issues)


def test_module_procedural_assignment_check_ignores_continuous_wiring():
    continuous_top = """
module temp_monitor_digital(output [7:0] rd_data);
  wire [7:0] rd_data_w;
  assign rd_data = rd_data_w;
endmodule
"""
    procedural_top = """
module temp_monitor_digital(output reg [7:0] rd_data);
  always @(*) begin
    rd_data = 8'h00;
  end
endmodule
"""

    assert agent._module_procedurally_assigns_signal(continuous_top, "rd_data") is False
    assert agent._module_procedurally_assigns_signal(procedural_top, "rd_data") is True


def test_sanitize_converts_procedurally_assigned_wire_to_reg():
    code = """
module top(output y);
wire y;
always @(*) begin
  y = 1'b0;
end
endmodule
"""

    out = agent._sanitize_single_driver_rtl({"top.v": code})["top.v"]

    assert "reg y;" in out
    assert "wire y;" not in out


def test_iverilog_port_width_warnings_are_structural_failures():
    output = """
top.v:47: warning: Port 4 (addr) of demo_sram_32x64_model expects 6 bits, got 1.
top.v:47:        : Padding 5 high bits of the port.
top.v:49: warning: Port 5 (din) of demo_sram_32x64_model expects 32 bits, got 1.
"""

    assert agent._has_structural_width_warnings(output) is True
    assert agent._has_structural_width_warnings("Icarus compile completed cleanly.") is False


def test_verilator_lint_preserves_pass2_relative_subdir(tmp_path, monkeypatch):
    rtl_dir = tmp_path / "rtl"
    pass2_dir = rtl_dir / "pass2"
    pass2_dir.mkdir(parents=True)
    rtl_file = pass2_dir / "temp_monitor_digital.v"
    rtl_file.write_text("module temp_monitor_digital; endmodule\n", encoding="utf-8")
    captured = {}

    class Result:
        stdout = ""
        stderr = ""
        error = ""
        returncode = 0
        status = "ok"

        def to_dict(self):
            return {"returncode": self.returncode, "status": self.status}

    def fake_run_tool(state, capability, tool, args, cwd=None, metadata=None):
        captured["args"] = args
        captured["cwd"] = cwd
        return Result()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)

    ok, _path, _output, _result = agent._run_verilator_lint(
        rtl_dir=str(rtl_dir),
        verilog_files=[str(rtl_file.relative_to(tmp_path))],
        top_module="temp_monitor_digital",
        suffix="pass2",
        state={},
    )

    assert ok is True
    assert captured["cwd"] == str(rtl_dir)
    assert "pass2/temp_monitor_digital.v" in captured["args"]
    assert "temp_monitor_digital.v" not in captured["args"][-1:]


def test_sanitize_child_output_does_not_drive_parent_input():
    code = """
module top(
  input [31:0] mem_dout,
  input clk
);
  wire csb;
  wire web;
  wire [7:0] addr;
  wire [31:0] din;
  demo_sram_32x256_wrapper u_sram (
    .clk(clk),
    .csb(csb),
    .web(web),
    .addr(addr),
    .din(din),
    .dout(mem_dout)
  );
endmodule

module demo_sram_32x256_wrapper(
  input clk,
  input csb,
  input web,
  input [7:0] addr,
  input [31:0] din,
  output [31:0] dout
);
endmodule
"""

    out = agent._sanitize_child_output_instance_connections({"top.v": code})["top.v"]

    assert "wire [31:0] mem_dout_from_u_sram;" in out
    assert ".dout(mem_dout_from_u_sram)" in out
    assert ".dout(mem_dout)" not in out


def test_sanitize_child_output_resizes_unused_placeholder_wire():
    code = """
module top(input clk);
  wire csb;
  wire web;
  wire [7:0] addr;
  wire [31:0] din;
  wire mem_dout_unused;
  demo_sram_32x256_wrapper u_sram (
    .clk(clk),
    .csb(csb),
    .web(web),
    .addr(addr),
    .din(din),
    .dout(mem_dout_unused)
  );
endmodule

module demo_sram_32x256_wrapper(
  input clk,
  input csb,
  input web,
  input [7:0] addr,
  input [31:0] din,
  output [31:0] dout
);
endmodule
"""

    out = agent._sanitize_child_output_instance_connections({"top.v": code})["top.v"]

    assert "wire [31:0] mem_dout_unused;" in out
    assert ".dout(mem_dout_unused)" in out
