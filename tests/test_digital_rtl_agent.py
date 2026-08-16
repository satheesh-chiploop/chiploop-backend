import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_rtl_agent as agent


def test_generated_complexity_rejects_constant_output_shell():
    rtl = """
module sensor_hub(
  input clk, input reset_n, input wr_en, input [15:0] sensor_data,
  output [31:0] rd_data, output alert_irq
);
assign rd_data = 32'd0;
assign alert_irq = 1'b0;
endmodule
"""

    issues = agent._validate_generated_complexity({}, "flat", {"sensor_hub.v": rtl})

    assert any("constant-output shell" in issue for issue in issues)


def test_generation_and_repair_prompts_require_functional_verifiable_rtl():
    spec = {
        "name": "sensor_hub",
        "rtl_output_file": "sensor_hub.v",
        "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset_n", "direction": "input", "width": 1},
            {"name": "sensor_valid", "direction": "input", "width": 1},
            {"name": "sample_count", "direction": "output", "width": 32},
        ],
    }
    prompt = agent._build_generation_prompt(spec, "flat", None, None, None)
    repair = agent._build_rtl_repair_prompt(prompt, "", "compile failed", "", ["sensor_hub.v"])

    assert "production-intent, functional, synthesizable, and directly verifiable RTL" in prompt
    assert "constant-output shell" in prompt
    assert "compile and lint success are necessary but not sufficient" in prompt.lower()
    assert "A required memory must survive synthesis as functional storage" in prompt
    assert "connect dout to an *_unused wire" in prompt
    assert "Never repair an error by tying a functional output" in repair
    assert "without reducing functionality or verifiability" in repair
    assert "REQUIRED-MEMORY REPAIR EXAMPLES" in repair
    assert "functionally unreachable required memory" in repair


def test_memory_macro_contract_rejects_invented_fallback_module():
    spec = {
        "memory_macros": [{
            "name": "sky130_sram_1kbyte_1rw1r_32x256_8",
            "instance_name": "u_sram",
        }],
    }
    bad = {"top.v": "module top; demo_sram_32x256_model u_sram(); endmodule"}
    good = {"top.v": "module top; sky130_sram_1kbyte_1rw1r_32x256_8 u_sram(); endmodule"}

    assert any("Required memory macro instance mismatch" in issue for issue in agent._validate_memory_macro_instances(spec, bad))
    assert agent._validate_memory_macro_instances(spec, good) == []


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


def test_storage_gate_allows_bounded_tolerance_for_approximate_scale_target():
    spec = {
        "design_name": "image_pipeline",
        "requirements": "Target roughly 25,000 flip-flops using register-based buffers.",
    }
    rtl = """
module image_pipeline(input clk, input [7:0] din);
reg [7:0] line0 [0:255];
reg [7:0] line1 [0:255];
reg [7:0] line2 [0:255];
reg [15:0] histogram [0:255];
always @(posedge clk) begin
  line0[0] <= din;
  line1[0] <= line0[0];
  line2[0] <= line1[0];
  histogram[din] <= histogram[din] + 1'b1;
end
endmodule
"""

    assert agent._assigned_storage_bits(rtl)[0] == 10240
    issues = agent._validate_generated_complexity(spec, "flat", {"image_pipeline.v": rtl})
    assert not any("materially below the spec scale" in issue for issue in issues)
    assert any("too few state elements" in issue for issue in issues)


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


def test_materializes_declared_fpga_bram_as_deliverable_rtl(tmp_path):
    spec = {
        "memory_macros": [{
            "kind": "fpga_bram",
            "name": "telemetry_history_wrapper",
            "depth": 64,
            "data_width": 32,
            "addr_width": 6,
            "ports": {"clk": "clock", "csb": "enable_n", "we": "write_en", "addr": "index", "din": "write_data", "dout": "read_data"},
        }]
    }

    paths = agent._materialize_declared_fpga_bram_wrappers(spec, str(tmp_path))

    assert [os.path.basename(path) for path in paths] == ["telemetry_history_wrapper.v"]
    rtl = open(paths[0], encoding="utf-8").read()
    assert "module telemetry_history_wrapper" in rtl
    assert "reg [31:0] mem [0:63]" in rtl
    assert "if (!enable_n)" in rtl
    assert "mem[index] <= write_data" in rtl


def test_materializes_fpga_block_ram_kind_alias(tmp_path):
    spec = {
        "memory_macros": [{
            "kind": "fpga_block_ram",
            "name": "fpga_block_ram_generic",
            "depth": 16,
            "data_width": 128,
            "addr_width": 4,
            "ports": {"clk": "clk", "csb": "mem_csb", "we": "mem_we", "addr": "mem_addr", "din": "mem_din", "dout": "mem_dout"},
        }]
    }

    paths = agent._materialize_declared_fpga_bram_wrappers(spec, str(tmp_path))

    assert [os.path.basename(path) for path in paths] == ["fpga_block_ram_generic.v"]
    rtl = open(paths[0], encoding="utf-8").read()
    assert "reg [127:0] mem [0:15]" in rtl


def test_required_memory_reachability_rejects_inactive_unconsumed_instance():
    spec = {"memory_macros": [{
        "kind": "fpga_bram",
        "name": "history_ram",
        "instance_name": "u_history",
        "ports": {"clk": "clk", "csb": "csb", "we": "we", "addr": "addr", "din": "din", "dout": "dout"},
    }]}
    files = {"top.v": """
module top(input clk);
  wire mem_csb; wire [31:0] mem_dout_unused;
  assign mem_csb = 1'b1;
  history_ram u_history(.clk(clk), .csb(mem_csb), .we(1'b0), .addr(8'h00), .din(32'h0), .dout(mem_dout_unused));
endmodule
"""}

    issues = agent._validate_memory_macro_reachability(spec, files)

    assert len(issues) == 1
    assert "functionally unreachable" in issues[0]
    assert "permanently inactive" in issues[0]
    assert "unconsumed signal" in issues[0]


def test_required_memory_reachability_accepts_input_driven_observable_instance():
    spec = {"memory_macros": [{
        "kind": "fpga_bram",
        "name": "history_ram",
        "instance_name": "u_history",
        "ports": {"clk": "clk", "csb": "csb", "we": "we", "addr": "addr", "din": "din", "dout": "dout"},
    }]}
    files = {"top.v": """
module top(input clk, input request_valid, input [7:0] request_addr, output [31:0] readback);
  wire mem_csb; wire [31:0] mem_dout;
  assign mem_csb = ~request_valid;
  assign readback = mem_dout;
  history_ram u_history(.clk(clk), .csb(mem_csb), .we(1'b0), .addr(request_addr), .din(32'h0), .dout(mem_dout));
endmodule
"""}

    assert agent._validate_memory_macro_reachability(spec, files) == []


def test_aligns_memory_role_labels_to_declared_concrete_ports():
    spec = {
        "memory_macros": [{
            "name": "fpga_block_ram_generic",
            "instance_name": "u_history",
            "ports": {"clk": "clock_i", "csb": "enable_n", "we": "write_en", "addr": "index", "din": "write_data", "dout": "read_data"},
        }]
    }
    files = {
        "top.v": """module top; fpga_block_ram_generic u_history (
            .clk(clk), .csb(csb), .we(we), .addr(addr), .din(din), .dout(dout)); endmodule"""
    }

    repaired = agent._align_memory_macro_instance_ports(files, spec)["top.v"]

    for concrete in ("clock_i", "enable_n", "write_en", "index", "write_data", "read_data"):
        assert f".{concrete}(" in repaired


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


def test_single_driver_sanitizer_removes_conditional_comb_write_to_sequential_reg():
    code = """
module top(input clk, input rst_n, input clamp, output status);
reg status_r;
assign status = status_r;
always @(*) begin
  status_r = 1'b0;
  if (clamp) status_r = 1'b1;
end
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_r <= 1'b0;
  else if (clamp) status_r <= 1'b1;
end
endmodule
"""

    out = agent._sanitize_single_driver_rtl({"top.v": code})["top.v"]

    assert "status_r =" not in out
    assert "status_r <= 1'b1;" in out


def test_single_driver_sanitizer_removes_case_mux_writes_to_sequential_reg():
    code = """
module top(input clk, input [7:0] addr, output [7:0] rd_data);
reg [7:0] rd_data_reg;
assign rd_data = rd_data_reg;
always @(posedge clk) begin
  if (addr == 8'h10) rd_data_reg <= 8'h5a;
end
always @(*) begin
  rd_data_reg = 8'h00;
  case (addr)
    8'h00: rd_data_reg = 8'h11;
    8'h04: rd_data_reg = 8'h22;
    default: rd_data_reg = 8'h00;
  endcase
end
endmodule
"""

    out = agent._sanitize_single_driver_rtl({"top.v": code})["top.v"]

    assert "rd_data_reg =" not in out
    assert "rd_data_reg <= 8'h5a;" in out


def test_iverilog_port_width_warnings_are_structural_failures():
    output = """
top.v:47: warning: Port 4 (addr) of demo_sram_32x64_model expects 6 bits, got 1.
top.v:47:        : Padding 5 high bits of the port.
top.v:49: warning: Port 5 (din) of demo_sram_32x64_model expects 32 bits, got 1.
"""

    assert agent._has_structural_width_warnings(output) is True
    assert agent._has_structural_width_warnings("Icarus compile completed cleanly.") is False


def test_sanitize_restores_full_bus_connection_for_wide_child_port():
    code = """
module child(input [1:0] select);
endmodule

module top;
  wire [1:0] fallback_select;
  child u_child (.select(fallback_select[0]));
endmodule
"""

    repaired = agent._sanitize_child_output_instance_connections({"top.v": code})["top.v"]

    assert ".select(fallback_select)" in repaired
    assert ".select(fallback_select[0])" not in repaired


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


def test_inter_module_repair_never_drives_top_level_input():
    files = {
        "top.v": """
module top(input rst_n);
  wire fallback_active;
  safety u_safety(.fallback_o(fallback_active));
  consumer u_consumer(.reset_i(rst_n));
endmodule
""",
        "safety.v": "module safety(output fallback_o); endmodule",
        "consumer.v": "module consumer(input reset_i); endmodule",
    }
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "rtl_output_file": "top.v", "ports": [{"name": "rst_n", "direction": "input", "width": 1}]},
            "modules": [{"name": "safety"}, {"name": "consumer"}],
        },
        "inter_module_signals": [{"source": "safety.fallback_o", "destinations": ["consumer.reset_i"]}],
    }

    out = agent._connect_spec_inter_module_signals(files, spec, "hierarchical")

    assert "assign rst_n" not in out["top.v"]


def test_inter_module_repair_never_drives_net_owned_by_child_output():
    files = {
        "top.v": """
module top;
  wire parsed_health;
  wire health_fault;
  response_mgr u_response(.parsed_health(parsed_health), .health_fault(health_fault));
  safety_mgr u_safety(.health_fault(health_fault));
endmodule
""",
        "response_mgr.v": "module response_mgr(output parsed_health, output health_fault); endmodule",
        "safety_mgr.v": "module safety_mgr(input health_fault); endmodule",
    }
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "rtl_output_file": "top.v", "ports": []},
            "modules": [
                {"name": "response_mgr", "ports": [
                    {"name": "parsed_health", "direction": "output"},
                    {"name": "health_fault", "direction": "output"},
                ]},
                {"name": "safety_mgr", "ports": [{"name": "health_fault", "direction": "input"}]},
            ],
        },
        "inter_module_signals": [{
            "source": "response_mgr.parsed_health",
            "destinations": ["safety_mgr.health_fault"],
        }],
    }

    out = agent._connect_spec_inter_module_signals(files, spec, "hierarchical")

    assert "assign health_fault = parsed_health;" not in out["top.v"]


def test_trim_zero_padding_repairs_concat_without_dropping_payload_bits():
    code = """
module top(output [127:0] resp_data, output [63:0] act_data);
wire [63:0] status_summary;
wire [7:0] safe_mode, mode_bits;
wire safe_indicator, clamp_hit;
wire [15:0] clamped_value, safe_value;
assign resp_data = {96'h000000000000000000000000, status_summary};
assign act_data = {16'h0000, safe_mode, mode_bits, safe_indicator, clamp_hit, clamped_value, safe_value};
endmodule
"""

    out = agent._trim_zero_padded_assign_concats({"top.v": code})["top.v"]

    assert "{64'h0000000000000000, status_summary}" in out
    assert "{14'h0000, safe_mode" in out


def test_concat_normalizer_zero_extends_undersized_continuous_and_procedural_values():
    code = """
module top(input [15:0] a, input [15:0] b, output [31:0] readback);
reg [127:0] packet;
assign readback = {a[14:0], b};
always @(*) packet = {a, b, 8'h00, 16'h0000, 64'h0};
endmodule
"""

    out = agent._trim_zero_padded_assign_concats({"top.v": code})["top.v"]

    assert "assign readback = {1'b0, a[14:0], b};" in out
    assert "packet = {8'b0, a, b, 8'h00, 16'h0000, 64'h0};" in out


def test_concat_normalizer_shrinks_trailing_reserved_zero_padding():
    code = """
module top(input [15:0] seq, input [15:0] velocity);
reg [127:0] packet;
reg [63:0] command;
always @(*) begin
  packet = {8'h01, seq, 32'h1, {16'h0000, velocity}, 16'h0, 24'h0, 40'h0};
  command = {seq, 8'h0, velocity, 16'h0, 16'h0};
end
endmodule
"""

    out = agent._trim_zero_padded_assign_concats({"top.v": code})["top.v"]

    assert "packet = {8'h01, seq, 32'h1, 16'h0000, velocity, 16'h0, 24'h0};" in out
    assert "command = {seq, 8'h0, velocity, 16'h0, 8'b0};" in out


def test_aligns_named_inter_module_wire_to_contract_width():
    files = {"top.v": """
module top;
wire child_cfg_word;
producer u_producer(.cfg_word(child_cfg_word));
consumer u_consumer(.cfg_word(child_cfg_word));
endmodule
"""}
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "rtl_output_file": "top.v", "ports": []},
            "modules": [{"name": "producer"}, {"name": "consumer"}],
        },
        "inter_module_signals": [{
            "name": "child_cfg_word", "width": 32,
            "source": "producer.cfg_word", "destinations": ["consumer.cfg_word"],
        }],
    }

    out = agent._align_spec_inter_module_wire_widths(files, spec, "hierarchical")

    assert "wire [31:0] child_cfg_word;" in out["top.v"]


def test_repairs_undriven_inflight_with_valid_ready_tracker_only():
    files = {"top.v": """
module top(
  input clk, input rst_n,
  output req_valid, input req_ready,
  input rsp_valid, output rsp_ready
);
wire request_inflight;
packager u_packager(.request_inflight(request_inflight), .req_valid(req_valid));
endmodule
""", "packager.v": """
module packager(input request_inflight, output req_valid);
assign req_valid = ~request_inflight;
endmodule
"""}
    spec = {"hierarchy": {"top_module": {
        "name": "top", "rtl_output_file": "top.v", "ports": []
    }}}

    out = agent._repair_undriven_inflight_state(files, spec, "hierarchical")["top.v"]

    assert "reg request_inflight;" in out
    assert "if (req_valid && req_ready) request_inflight <= 1'b1;" in out
    assert "if (rsp_valid && rsp_ready) request_inflight <= 1'b0;" in out


def test_repairs_last_accepted_observation_from_matching_response_handshake():
    files = {"top.v": """
module top(input clk, input rst_n);
wire [15:0] response_sequence_number;
wire response_accepted;
wire [15:0] last_sequence_accepted;
deframer u_deframer(.response_sequence_number(response_sequence_number), .response_accepted(response_accepted));
registers u_registers(.last_sequence_accepted(last_sequence_accepted));
endmodule
"""}
    spec = {"hierarchy": {
        "top_module": {"name": "top", "rtl_output_file": "top.v", "ports": []},
        "modules": [
            {"name": "deframer", "ports": [
                {"name": "response_sequence_number", "direction": "output", "width": 16},
                {"name": "response_accepted", "direction": "output", "width": 1},
            ]},
            {"name": "registers", "ports": [
                {"name": "last_sequence_accepted", "direction": "input", "width": 16},
            ]},
        ],
    }}

    out = agent._repair_undriven_last_accepted_observations(files, spec, "hierarchical")["top.v"]

    assert "reg [15:0] last_sequence_accepted;" in out
    assert "else if (response_accepted) last_sequence_accepted <= response_sequence_number;" in out


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


def test_promotes_procedurally_assigned_output_wire_without_prompt_repair():
    files = {
        "regs.v": """
module regs(clk, reg_rvalid, passthrough);
input clk;
output reg_rvalid;
output wire passthrough;
always @(posedge clk) reg_rvalid <= 1'b1;
assign passthrough = reg_rvalid;
endmodule
"""
    }

    repaired = agent._promote_procedurally_assigned_outputs(files)["regs.v"]

    assert "output reg reg_rvalid;" in repaired
    assert "output wire passthrough;" in repaired


def test_connects_top_status_output_source_to_same_named_child_status_input():
    files = {
        "top.v": """
module top(output status_irq);
wire status_irq_int;
wire status_irq_csr;
assign status_irq = status_irq_int;
status_regs u_regs(.status_irq(status_irq_csr));
status_logic u_logic(.status_irq(status_irq_int));
endmodule
""",
        "status_regs.v": "module status_regs(input status_irq); endmodule\n",
        "status_logic.v": "module status_logic(output status_irq); assign status_irq = 1'b0; endmodule\n",
    }
    spec = {"hierarchy": {"top_module": {"name": "top", "rtl_output_file": "top.v"}, "modules": []}}

    repaired = agent._connect_top_output_feedback_to_matching_child_input(files, spec, "hierarchical")["top.v"]

    assert "assign status_irq_csr = status_irq_int;" in repaired


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


def test_final_input_contract_cleanup_removes_feedback_assignment():
    spec = {
        "hierarchy": {
            "top_module": {
                "name": "adaptive_aero_control_top",
                "rtl_output_file": "adaptive_aero_control_top.v",
                "ports": [{"name": "host_cfg_req_seq", "direction": "input", "width": 16}],
            },
            "modules": [],
        }
    }
    rtl = {
        "adaptive_aero_control_top.v": (
            "module adaptive_aero_control_top(host_cfg_req_seq);\n"
            "input [15:0] host_cfg_req_seq;\n"
            "wire [15:0] req_seq_num_w;\n"
            "assign host_cfg_req_seq = req_seq_num_w;\n"
            "endmodule"
        )
    }
    cleaned = agent._remove_writes_to_spec_input_ports(rtl, spec, "hierarchical")
    assert "assign host_cfg_req_seq" not in cleaned["adaptive_aero_control_top.v"]
    assert "input [15:0] host_cfg_req_seq;" in cleaned["adaptive_aero_control_top.v"]


def test_repair_context_keeps_complete_files_named_by_latest_lint_log():
    previous = (
        "---BEGIN top.v---\nmodule top; child u_child(); endmodule\n---END top.v---\n"
        "---BEGIN child.v---\nmodule child(output ready); endmodule\n---END child.v---\n"
        "---BEGIN unrelated.v---\nmodule unrelated; endmodule\n---END unrelated.v---"
    )
    context, targets = agent._targeted_rtl_repair_context(
        previous,
        "",
        "%Warning-UNDRIVEN: pass3/child.v:1:21: Signal is not driven: 'ready'",
        ["top.v", "child.v", "unrelated.v"],
    )
    assert targets == ["child.v"]
    assert "---BEGIN child.v---" in context
    assert "module child(output ready)" in context
    assert "top.v" not in context
    assert "unrelated.v" not in context


def test_repair_context_falls_back_to_complete_hierarchy_when_log_has_no_file():
    previous = "---BEGIN top.v---\nmodule top; endmodule\n---END top.v---"
    context, targets = agent._targeted_rtl_repair_context(previous, "tool failed", "", ["top.v"])
    assert context == previous
    assert targets == ["top.v"]


def test_partial_repair_preserves_children_emitted_inside_original_top_block():
    previous = """---BEGIN top.v---
module top; child u_child(); endmodule
module child(output ready); assign ready = 1'b1; endmodule
---END top.v---"""
    repaired = """---BEGIN top.v---
module top; wire ready; child u_child(.ready(ready)); endmodule
---END top.v---"""

    merged = agent._merge_rtl_repair_output(previous, repaired, ["top.v", "child.v"])
    blocks = agent._parse_named_verilog_blocks(merged)

    assert set(blocks) == {"top.v", "child.v"}
    assert "wire ready" in blocks["top.v"]
    assert "module child(output ready)" in blocks["child.v"]


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
