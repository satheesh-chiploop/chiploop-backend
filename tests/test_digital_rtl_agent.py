import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_rtl_agent as agent


def test_rtl_completion_retries_one_empty_provider_response(monkeypatch):
    calls = []

    def fake_complete(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise RuntimeError("Streamed response was empty provider=openai finish_reason=stop")
        return "---BEGIN top.v---\nmodule top; endmodule\n---END top.v---"

    monkeypatch.setattr(agent, "complete_text", fake_complete)

    output = agent._complete_rtl_text("prompt", agent_name="Digital RTL Agent", state={}, stage_label="pass1")

    assert "module top" in output
    assert len(calls) == 2


def test_storage_gate_ignores_optional_or_prohibited_fifo_language():
    spec = {
        "design_name": "small_control_ip",
        "_source_spec_text": (
            "Optional response payload buffering may use a FIFO only if necessary. "
            "Do not infer payload storage as flip-flop FIFOs. DMA is remote, not on-chip."
        ),
    }

    assert agent._minimum_expected_flops(spec, "flat") == 0


def test_storage_gate_keeps_explicit_register_bit_target():
    spec = {"design_name": "stateful_ip", "requirements": "Target 128 register bits."}

    assert agent._minimum_expected_flops(spec, "flat") == 64


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


def test_sanitize_child_output_resizes_internal_structural_wire():
    code = """
module top(input clk);
  wire clamped_value;
  clamp u_clamp(.clk(clk), .clamped_value(clamped_value));
  fallback u_fallback(.clk(clk), .clamped_value(clamped_value));
endmodule

module clamp(input clk, output [15:0] clamped_value);
  assign clamped_value = 16'h0000;
endmodule

module fallback(input clk, input [15:0] clamped_value);
endmodule
"""

    out = agent._sanitize_child_output_instance_connections({"top.v": code})["top.v"]

    assert "wire [15:0] clamped_value;" in out
    assert ".clamped_value(clamped_value)" in out


def test_connects_spec_inter_module_signal_when_repair_left_consumer_undriven():
    files = {
        "motor_control_top.v": """
module motor_control_top;
  wire fault_out;
  wire status_fault;
  motor_control_safety_fsm u_safety(.fault_latched(fault_out));
  motor_control_register_map u_regs(.status_fault(status_fault));
endmodule
""",
        "motor_control_safety_fsm.v": "module motor_control_safety_fsm(output fault_latched); endmodule",
        "motor_control_register_map.v": "module motor_control_register_map(input status_fault); endmodule",
    }
    spec = {
        "hierarchy": {
            "top_module": {"name": "motor_control_top", "rtl_output_file": "motor_control_top.v", "ports": []},
            "modules": [
                {"name": "motor_control_safety_fsm", "ports": []},
                {"name": "motor_control_register_map", "ports": []},
            ],
        },
        "inter_module_signals": [{
            "name": "fault_status",
            "source": "motor_control_safety_fsm.fault_latched",
            "destinations": ["motor_control_register_map.status_fault"],
        }],
    }

    out = agent._connect_spec_inter_module_signals(files, spec, "hierarchical")

    assert "assign status_fault = fault_out;" in out["motor_control_top.v"]


def test_direction_alignment_removes_reg_and_writes_from_spec_input():
    files = {"response_unpacker.v": """
module response_unpacker(input clk, output reg resp_ready);
always @(*) begin
  resp_ready = 1'b0;
  if (clk) resp_ready = 1'b1;
  if (resp_ready <= clk) begin
  end
end
endmodule
"""}
    spec = {"rtl_output_file": "response_unpacker.v", "name": "response_unpacker", "ports": [
        {"name": "clk", "direction": "input", "width": 1},
        {"name": "resp_ready", "direction": "input", "width": 1},
    ]}

    out = agent._repair_module_port_directions_from_spec(files, spec, "flat")
    out = agent._remove_writes_to_spec_input_ports(out, spec, "flat")["response_unpacker.v"]

    assert "input resp_ready" in out
    assert "input reg resp_ready" not in out
    assert "resp_ready =" not in out
    assert "resp_ready <= clk" in out


def test_directional_alias_repair_maps_input_base_and_output_suffix_to_spec():
    files = {
        "top.v": "module top; wire [7:0] code; fault_manager u_fault(.fault_code(code), .fault_code_out(code)); endmodule",
        "fault_manager.v": """
module fault_manager(input [7:0] fault_code, output reg [7:0] fault_code_out);
always @(*) fault_code_out = fault_code;
endmodule
""",
    }
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "rtl_output_file": "top.v", "ports": []},
            "modules": [{"name": "fault_manager", "rtl_output_file": "fault_manager.v", "ports": [
                {"name": "fault_code_in", "direction": "input", "width": 8},
                {"name": "fault_code", "direction": "output", "width": 8},
            ]}],
        }
    }

    out = agent._repair_directional_port_aliases_from_spec(files, spec, "hierarchical")

    assert "input [7:0] fault_code_in" in out["fault_manager.v"]
    assert "output reg [7:0] fault_code" in out["fault_manager.v"]
    assert "fault_code = fault_code_in" in out["fault_manager.v"]
    assert ".fault_code_in(code)" in out["top.v"]
    assert ".fault_code(code)" in out["top.v"]


def test_sanitize_child_output_reroutes_net_already_driven_by_parent_assign():
    code = """
module top(input fallback_source, output fallback_status);
  wire fallback_status_i;
  assign fallback_status_i = fallback_source;
  safety_logic u_safety_logic(
    .fallback_active(fallback_status_i)
  );
  assign fallback_status = fallback_status_i;
endmodule

module safety_logic(output fallback_active);
  reg fallback_active;
  always @(*) fallback_active = 1'b1;
endmodule
"""

    out = agent._sanitize_child_output_instance_connections({"top.v": code})["top.v"]

    assert "wire fallback_status_i_unused_from_u_safety_logic_fallback_active;" in out
    assert ".fallback_active(fallback_status_i_unused_from_u_safety_logic_fallback_active)" in out
    assert "assign fallback_status_i = fallback_source;" in out


def test_verilator_undriven_warning_is_structural_failure_even_with_zero_exit():
    output = "%Warning-UNDRIVEN: motor_control_top.v:80: Wire status_i is not driven"

    assert agent._classify_verilator_result(True, output) == "fatal"


def test_sanitize_connects_contract_mirrored_top_outputs_from_child_inputs():
    code = """
module top(
  output [31:0] register_file_status_flags_in,
  output register_file_fault_in
);
  wire [31:0] u_status_flags;
  wire u_fault_in;
  assign u_status_flags = 32'h12;
  assign u_fault_in = 1'b0;
  register_file u_register_file(
    .status_flags_in(u_status_flags),
    .fault_in(u_fault_in)
  );
endmodule

module register_file(input [31:0] status_flags_in, input fault_in);
endmodule
"""

    out = agent._sanitize_child_output_instance_connections({"top.v": code})["top.v"]

    assert "assign register_file_status_flags_in = u_status_flags;" in out
    assert "assign register_file_fault_in = u_fault_in;" in out


def test_sanitize_child_output_reroutes_duplicate_child_drivers_semantically():
    code = """
module top(output sample_req, output alert_irq, output alert_status);
  mmio_block u_mmio(
    .sample_req(sample_req),
    .alert_irq(alert_irq),
    .alert_status(alert_status)
  );
  sample_ctrl u_sample_ctrl(
    .sample_req(sample_req)
  );
  alert_irq_block u_alert_irq(
    .alert_irq(alert_irq),
    .alert_status(alert_status)
  );
endmodule

module mmio_block(output sample_req, output alert_irq, output alert_status);
endmodule

module sample_ctrl(output sample_req);
endmodule

module alert_irq_block(output alert_irq, output alert_status);
endmodule
"""

    out = agent._sanitize_child_output_instance_connections({"top.v": code})["top.v"]

    assert ".sample_req(sample_req_unused_from_u_mmio_sample_req)" in out
    assert ".alert_irq(alert_irq_unused_from_u_mmio_alert_irq)" in out
    assert ".alert_status(alert_status_unused_from_u_mmio_alert_status)" in out
    assert "wire sample_req_unused_from_u_mmio_sample_req;" in out
    assert ".sample_req(sample_req)" in out
    assert ".alert_irq(alert_irq)" in out
