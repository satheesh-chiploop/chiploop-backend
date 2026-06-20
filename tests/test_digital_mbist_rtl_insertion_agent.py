import json
import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")

from agents.digital import digital_mbist_rtl_insertion_agent as agent


def test_mbist_rtl_insertion_disabled_by_default(tmp_path):
    workflow_dir = tmp_path / "wf"
    rtl_dir = workflow_dir / "rtl"
    rtl_dir.mkdir(parents=True)
    (rtl_dir / "top.sv").write_text("module top(input logic clk); endmodule\n", encoding="utf-8")

    state = {
        "workflow_id": "wf1",
        "workflow_dir": str(workflow_dir),
        "toggles": {},
    }

    out = agent.run_agent(state)

    summary_path = workflow_dir / "digital" / "mbist_rtl_insertion" / "mbist_rtl_insertion_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["enabled"] is False
    assert summary["status"] == "disabled"
    assert out["digital"]["mbist_rtl_insertion"]["status"] == "disabled"


def test_mbist_rtl_insertion_enabled_without_openram_skips(tmp_path):
    workflow_dir = tmp_path / "wf"
    rtl_dir = workflow_dir / "rtl"
    rtl_dir.mkdir(parents=True)
    (rtl_dir / "top.sv").write_text("module top(input logic clk); endmodule\n", encoding="utf-8")

    state = {
        "workflow_id": "wf1",
        "workflow_dir": str(workflow_dir),
        "toggles": {"insert_mbist": True},
    }

    out = agent.run_agent(state)

    summary = out["digital"]["mbist_rtl_insertion"]
    assert summary["enabled"] is True
    assert summary["status"] == "skipped_no_openram_sram_detected"
    assert summary["detected_memory"] is None


def test_detects_openram_instance_and_dimensions(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        """
module top(input logic clk);
  logic [7:0] addr;
  logic [31:0] din;
  logic [31:0] dout;
  logic sram_web;
  logic sram_csb;
  openram_sram_256x32 u_sram(.clk(clk), .csb(sram_csb), .web(sram_web), .addr(addr), .din(din), .dout(dout));
endmodule
""",
        encoding="utf-8",
    )

    detected = agent._detect_openram_memory([str(rtl)])

    assert detected is not None
    assert detected["cell"] == "openram_sram_256x32"
    assert detected["instance"] == "u_sram"
    assert detected["addr_width"] == 8
    assert detected["data_width"] == 32
    assert detected["depth"] == 256
    assert detected["ports"]["we"] == "web"
    assert detected["ports"]["csb"] == "csb"


def test_detects_multiple_openram_instances(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        """
module top(input logic clk);
  logic [5:0] addr_a;
  logic [7:0] addr_b;
  logic [31:0] din_a, dout_a;
  logic [15:0] din_b, dout_b;
  openram_sram_64x32 u_sram_a(.clk(clk), .web(1'b1), .addr(addr_a), .din(din_a), .dout(dout_a));
  openram_sram_256x16 u_sram_b(.clk(clk), .web(1'b1), .addr(addr_b), .din(din_b), .dout(dout_b));
endmodule
""",
        encoding="utf-8",
    )

    detected = agent._detect_openram_memories([str(rtl)])

    assert [item["cell"] for item in detected] == ["openram_sram_64x32", "openram_sram_256x16"]
    assert detected[0]["addr_width"] == 6
    assert detected[0]["data_width"] == 32
    assert detected[1]["addr_width"] == 8
    assert detected[1]["data_width"] == 16


def test_detects_sram_module_definition_without_splitting_name(tmp_path):
    rtl = tmp_path / "demo_sram_32x64.v"
    rtl.write_text(
        """
module demo_sram_32x64 (
    clk,
    csb,
    web,
    addr,
    din,
    dout
);
input clk;
input csb;
input web;
input [5:0] addr;
input [31:0] din;
output [31:0] dout;
endmodule
""",
        encoding="utf-8",
    )

    detected = agent._detect_openram_memory([str(rtl)])

    assert detected is not None
    assert detected["kind"] == "memory_module_definition"
    assert detected["cell"] == "demo_sram_32x64"
    assert detected["instance"] is None
    assert detected["addr_width"] == 6
    assert detected["data_width"] == 32
    assert detected["depth"] == 64
    assert detected["ports"] == {
        "clk": "clk",
        "csb": "csb",
        "we": "web",
        "addr": "addr",
        "din": "din",
        "dout": "dout",
    }


def test_sram_module_definition_widths_are_scoped_to_memory_module(tmp_path):
    rtl = tmp_path / "combined.v"
    rtl.write_text(
        """
module top(input clk);
  wire [15:0] addr;
  wire [63:0] din;
  wire [63:0] dout;
endmodule

module openram_sram_64x32 (
    clk,
    csb,
    web,
    addr,
    din,
    dout
);
input clk;
input csb;
input web;
input [5:0] addr;
input [31:0] din;
output [31:0] dout;
endmodule
""",
        encoding="utf-8",
    )

    detected = agent._detect_openram_memory([str(rtl)])

    assert detected is not None
    assert detected["cell"] == "openram_sram_64x32"
    assert detected["addr_width"] == 6
    assert detected["data_width"] == 32
    assert detected["depth"] == 64


def test_sram_module_definition_parses_ansi_port_widths(tmp_path):
    rtl = tmp_path / "demo_sram_32x64.v"
    rtl.write_text(
        """
module demo_sram_32x64(
    input wire clk,
    input wire csb,
    input wire web,
    input wire [5:0] addr,
    input wire [31:0] din,
    output wire [31:0] dout
);
assign dout = 32'h0;
endmodule
""",
        encoding="utf-8",
    )

    detected = agent._detect_openram_memory([str(rtl)])

    assert detected is not None
    assert detected["addr_width"] == 6
    assert detected["data_width"] == 32
    assert detected["depth"] == 64


def test_sram_named_controller_is_not_detected_as_memory_definition(tmp_path):
    rtl = tmp_path / "sram_mbist_demo_controller.v"
    rtl.write_text(
        """
module sram_mbist_demo_controller (
    input clk,
    input reset_n,
    output sram_csb,
    output sram_web,
    output [5:0] sram_addr,
    output [31:0] sram_din,
    input [31:0] sram_dout
);
assign sram_csb = 1'b1;
assign sram_web = 1'b1;
assign sram_addr = 6'h0;
assign sram_din = 32'h0;
endmodule
""",
        encoding="utf-8",
    )

    assert agent._detect_openram_memory([str(rtl)]) is None


def test_sram_named_controller_with_internal_array_is_not_detected_as_memory_definition(tmp_path):
    rtl = tmp_path / "sram_mbist_demo_controller.v"
    rtl.write_text(
        """
module sram_mbist_demo_controller(
  input clk,
  input csb,
  input web,
  input [7:0] addr,
  input [31:0] din,
  output reg [31:0] dout
);
  reg [31:0] mem [0:255];
  always @(posedge clk) begin
    if (!csb) begin
      if (!web) mem[addr] <= din;
      dout <= mem[addr];
    end
  end
endmodule
""",
        encoding="utf-8",
    )

    assert agent._detect_openram_memory([str(rtl)]) is None


def test_autombist_config_uses_detected_sram_ports():
    memory = {
        "cell": "demo_sram_32x64",
        "addr_width": 6,
        "data_width": 32,
        "ports": {"clk": "clk", "csb": "csb", "we": "web", "addr": "addr", "din": "din", "dout": "dout"},
    }

    config = agent._patch_autombist_config("memory_name: sample\nports:\n  clk: clk0\n", memory)

    assert "memory_name: demo_sram_32x64" in config
    assert "wrapper_module_name: demo_sram_32x64_mbist" in config
    assert "addr_width: 6" in config
    assert "data_width: 32" in config
    assert "clk0" not in config
    assert "  we: web" in config
    assert "  csb: csb" in config


def test_openram_config_uses_detected_memory_dimensions():
    memory = {"cell": "demo_sram_32x64", "addr_width": 6, "data_width": 32, "depth": 64}

    config = agent._patch_openram_config("word_size: 8\nnum_words: 16\n", memory)

    assert "memory_name: demo_sram_32x64" in config
    assert "word_size: 32" in config
    assert "num_words: 64" in config
    assert "addr_width: 6" in config


def test_spec_memory_macro_contract_is_preferred_for_dimensions(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({
            "memory_macros": [
                {
                    "name": "openram_sram_64x32",
                    "kind": "openram_sram",
                    "depth": 64,
                    "data_width": 32,
                    "addr_width": 6,
                    "instance_name": "u_sram",
                    "ports": {"clk": "clk", "csb": "csb", "we": "web", "addr": "addr", "din": "din", "dout": "dout"},
                    "requires_mbist": True,
                }
            ]
        }),
        encoding="utf-8",
    )
    rtl_detected = {
        "kind": "memory_instance",
        "cell": "openram_sram_64x32",
        "instance": "u_sram",
        "source_file": "top.v",
        "connections": {"clk": "clk"},
        "ports": {"clk": "clk"},
        "addr_width": 8,
        "data_width": 8,
        "depth": 256,
    }

    macros = agent._spec_memory_macros({"digital_spec_json": str(spec)})
    merged = agent._merge_spec_memory_with_rtl_detection(macros, rtl_detected)

    assert merged["addr_width"] == 6
    assert merged["data_width"] == 32
    assert merged["depth"] == 64
    assert merged["instance"] == "u_sram"
    assert merged["source_file"] == "top.v"
    assert merged["ports"]["we"] == "web"


def test_multiple_spec_memory_macros_merge_with_matching_rtl_instances(tmp_path):
    spec_macros = [
        {"kind": "spec_memory_macro", "cell": "openram_sram_64x32", "instance": "u_a", "addr_width": 6, "data_width": 32, "depth": 64, "ports": {"clk": "clk", "we": "web", "addr": "addr", "din": "din", "dout": "dout"}},
        {"kind": "spec_memory_macro", "cell": "openram_sram_128x16", "instance": "u_b", "addr_width": 7, "data_width": 16, "depth": 128, "ports": {"clk": "clk", "we": "web", "addr": "addr", "din": "din", "dout": "dout"}},
    ]
    detected = [
        {"kind": "memory_instance", "cell": "openram_sram_64x32", "instance": "u_a", "source_file": "top.v", "connections": {}, "ports": {}, "addr_width": 8, "data_width": 8, "depth": 256},
        {"kind": "memory_instance", "cell": "openram_sram_128x16", "instance": "u_b", "source_file": "top.v", "connections": {}, "ports": {}, "addr_width": 8, "data_width": 32, "depth": 256},
    ]

    merged = agent._merge_spec_memories_with_rtl_detection(spec_macros, detected)

    assert len(merged) == 2
    assert merged[0]["instance"] == "u_a"
    assert merged[0]["addr_width"] == 6
    assert merged[1]["instance"] == "u_b"
    assert merged[1]["data_width"] == 16


def test_spec_openram_macro_ignores_fallback_model_macro(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({
            "memory_macros": [
                {"name": "demo_sram_32x64", "kind": "openram_sram", "depth": 64, "data_width": 32, "addr_width": 6},
                {"name": "demo_sram_32x64_model", "kind": "synthesizable_sram_model", "depth": 64, "data_width": 32, "addr_width": 6},
            ]
        }),
        encoding="utf-8",
    )

    macros = agent._spec_memory_macros({"digital_spec_json": str(spec)})

    assert [item["cell"] for item in macros] == ["demo_sram_32x64"]


def test_spec_openram_macro_merges_rtl_model_instance_without_second_target():
    spec_macros = [
        {
            "kind": "spec_memory_macro",
            "cell": "demo_sram_32x64",
            "openram_cell": "demo_sram_32x64",
            "instance": "u_sram",
            "addr_width": 6,
            "data_width": 32,
            "depth": 64,
            "ports": {"clk": "clk", "we": "web", "addr": "addr", "din": "din", "dout": "dout"},
        }
    ]
    detected = [
        {
            "kind": "memory_instance",
            "cell": "demo_sram_32x64_model",
            "instance": "u_sram_model",
            "source_file": "top.v",
            "connections": {"clk": "clk", "addr": "addr", "din": "din", "dout": "dout"},
            "ports": {"clk": "clk", "we": "web", "addr": "addr", "din": "din", "dout": "dout"},
            "addr_width": 1,
            "data_width": 1,
            "depth": 2,
        }
    ]

    merged = agent._merge_spec_memories_with_rtl_detection(spec_macros, detected)

    assert len(merged) == 1
    assert merged[0]["cell"] == "demo_sram_32x64"
    assert merged[0]["openram_cell"] == "demo_sram_32x64"
    assert merged[0]["rtl_cell"] == "demo_sram_32x64_model"
    assert merged[0]["source_file"] == "top.v"
    assert merged[0]["addr_width"] == 6
    assert merged[0]["data_width"] == 32


def test_memory_macro_cell_name_can_repeat_only_for_same_config():
    ok = [
        {"cell": "openram_sram_64x32", "depth": 64, "data_width": 32, "addr_width": 6},
        {"cell": "openram_sram_64x32", "depth": 64, "data_width": 32, "addr_width": 6},
    ]
    bad = [
        {"cell": "openram_sram_shared", "depth": 64, "data_width": 32, "addr_width": 6},
        {"cell": "openram_sram_shared", "depth": 128, "data_width": 32, "addr_width": 7},
    ]

    assert agent._validate_memory_config_names(ok) == []
    assert agent._validate_memory_config_names(bad)


def test_openram_generation_discovers_behavioral_model_and_macro_collateral(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    stage_dir = tmp_path / "stage"
    project_dir.mkdir()
    stage_dir.mkdir()
    (project_dir / "openram.yml").write_text("word_size: 8\nnum_words: 16\n", encoding="utf-8")
    rtl_src = tmp_path / "top.v"
    rtl_src.write_text("module top; endmodule\n", encoding="utf-8")
    memory = {"cell": "demo_sram_32x64", "addr_width": 6, "data_width": 32, "depth": 64, "source_file": str(rtl_src)}

    def fake_run(cmd, cwd, timeout=600, env=None):
        assert "sram_compiler.py" in cmd[1]
        out_dir = stage_dir / "openram_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "demo_sram_32x64.v").write_text(
            "module demo_sram_32x64(input clk,input [5:0] addr,output reg [31:0] dout);"
            "reg [31:0] mem [0:63]; always @(posedge clk) dout <= mem[addr]; endmodule\n",
            encoding="utf-8",
        )
        (out_dir / "demo_sram_32x64.lib").write_text("library(demo_sram_32x64) {}\n", encoding="utf-8")
        (out_dir / "demo_sram_32x64.lef").write_text("MACRO demo_sram_32x64\nEND demo_sram_32x64\n", encoding="utf-8")
        (out_dir / "demo_sram_32x64.gds").write_text("gds\n", encoding="utf-8")
        return 0, "ok\n"

    monkeypatch.setattr(agent, "_run", fake_run)
    monkeypatch.setattr(agent, "_find_openram_compiler", lambda state: "/tools/sram_compiler.py")

    result = agent._generate_openram_collateral(memory, "autombist", str(project_dir), str(stage_dir), "wf1")

    assert result["status"] == "validated"
    assert result["generator"] == "openram"
    assert memory["source_file"] == str(rtl_src)
    assert memory["openram_behavioral_model"].endswith("demo_sram_32x64.v")
    assert result["validation"]["status"] == "clean"
    assert result["collateral"]["lib"]
    assert result["collateral"]["lef"]
    assert result["collateral"]["gds"]


def test_openram_generation_fails_when_compiler_missing(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    stage_dir = tmp_path / "stage"
    project_dir.mkdir()
    stage_dir.mkdir()
    memory = {"cell": "demo_sram_32x64", "addr_width": 6, "data_width": 32, "depth": 64}

    monkeypatch.setattr(agent, "_find_openram_compiler", lambda state: None)

    result = agent._generate_openram_collateral(memory, "autombist", str(project_dir), str(stage_dir), "wf1")

    assert result["status"] == "failed"
    assert result["reason"] == "openram_compiler_not_found"
    assert result["attempts"] == []


def test_openram_generation_reports_disk_full_root_cause(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    stage_dir = tmp_path / "stage"
    project_dir.mkdir()
    stage_dir.mkdir()
    memory = {"cell": "demo_sram_32x64", "addr_width": 6, "data_width": 32, "depth": 64}

    monkeypatch.setattr(agent, "_find_openram_compiler", lambda state: "/tools/sram_compiler.py")
    monkeypatch.setattr(agent, "_run", lambda cmd, cwd, timeout=600, env=None: (1, "error: No space left on device\n"))

    result = agent._generate_openram_collateral(memory, "autombist", str(project_dir), str(stage_dir), "wf1")

    assert result["status"] == "failed"
    assert result["reason"] == "openram_no_space_left_on_device"
    assert result["collateral"]["verilog"] == []
    assert result["validation"]["missing"] == ["behavioral_model", "lib", "lef", "gds"]


def test_openram_generation_reports_missing_pdk_root_root_cause(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    stage_dir = tmp_path / "stage"
    project_dir.mkdir()
    stage_dir.mkdir()
    memory = {"cell": "demo_sram_32x64", "addr_width": 6, "data_width": 32, "depth": 64}

    monkeypatch.setattr(agent, "_find_openram_compiler", lambda state: "/tools/sram_compiler.py")
    monkeypatch.setattr(agent, "_run", lambda cmd, cwd, timeout=600, env=None: (1, "SystemError: Unable to find open_pdks tech file. Set PDK_ROOT.\n"))

    result = agent._generate_openram_collateral(memory, "autombist", str(project_dir), str(stage_dir), "wf1")

    assert result["status"] == "failed"
    assert result["reason"] == "openram_pdk_root_not_set"


def test_openram_generation_reports_missing_custom_cell_spice(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    stage_dir = tmp_path / "stage"
    project_dir.mkdir()
    stage_dir.mkdir()
    memory = {"cell": "demo_sram_32x64", "addr_width": 6, "data_width": 32, "depth": 64}
    log = "Custom cell pin names do not match spice file:\n['BL', 'BR', 'VGND', 'VPWR', 'VPB', 'VNB', 'WL'] vs []\n"

    monkeypatch.setattr(agent, "_find_openram_compiler", lambda state: "/tools/sram_compiler.py")
    monkeypatch.setattr(agent, "_run", lambda cmd, cwd, timeout=600, env=None: (1, log))

    result = agent._generate_openram_collateral(memory, "autombist", str(project_dir), str(stage_dir), "wf1")

    assert result["status"] == "failed"
    assert result["reason"] == "openram_custom_cell_spice_missing"


def _write_precompiled_macro(root: Path, cell: str):
    for subdir in ("verilog", "lef", "gds", "spice", "lib"):
        (root / subdir).mkdir(parents=True, exist_ok=True)
    (root / "verilog" / f"{cell}.v").write_text(
        f"module {cell}(input clk,input [7:0] addr,input [31:0] din,output reg [31:0] dout);"
        "reg [31:0] mem [0:255]; always @(posedge clk) dout <= mem[addr]; endmodule\n",
        encoding="utf-8",
    )
    (root / "lef" / f"{cell}.lef").write_text(f"MACRO {cell}\nEND {cell}\n", encoding="utf-8")
    (root / "gds" / f"{cell}.gds").write_text("gds\n", encoding="utf-8")
    (root / "spice" / f"{cell}.spice").write_text(f".subckt {cell} clk addr din dout\n.ends\n", encoding="utf-8")
    (root / "lib" / f"{cell}_TT_1p8V_25C.lib").write_text(f"library({cell}) {{ cell({cell}) {{}} }}\n", encoding="utf-8")


def test_selects_precompiled_sram_macro_with_larger_depth(tmp_path, monkeypatch):
    root = tmp_path / "sky130_sram_macros"
    cell = "sky130_sram_1kbyte_1rw1r_32x256_8"
    _write_precompiled_macro(root, cell)
    monkeypatch.setattr(agent, "_precompiled_sram_roots", lambda stage_dir: [str(root)])

    selected = agent._select_precompiled_sram_macro(
        {"cell": "demo_sram_32x64", "data_width": 32, "depth": 64, "addr_width": 6},
        str(tmp_path / "stage"),
    )

    assert selected["cell"] == cell
    assert selected["data_width"] == 32
    assert selected["depth"] == 256


def test_openram_custom_cell_failure_uses_precompiled_sram_macro(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    stage_dir = tmp_path / "stage"
    project_dir.mkdir()
    stage_dir.mkdir()
    root = tmp_path / "sky130_sram_macros"
    cell = "sky130_sram_1kbyte_1rw1r_32x256_8"
    _write_precompiled_macro(root, cell)
    memory = {"cell": "demo_sram_32x64", "addr_width": 6, "data_width": 32, "depth": 64}
    log = "Custom cell pin names do not match spice file:\n['BL', 'BR'] vs []\n"

    monkeypatch.setattr(agent, "_precompiled_sram_roots", lambda stage_dir_arg: [str(root)])
    monkeypatch.setattr(agent, "_find_openram_compiler", lambda state: "/tools/sram_compiler.py")
    monkeypatch.setattr(agent, "_run", lambda cmd, cwd, timeout=600, env=None: (1, log))

    result = agent._generate_openram_collateral(memory, "autombist", str(project_dir), str(stage_dir), "wf1")

    assert result["status"] == "validated"
    assert result["generator"] == "precompiled_sram_macro"
    assert result["selected"]["cell"] == cell
    assert result["depth_match"] is False
    assert memory["openram_behavioral_model"].endswith(f"{cell}.v")
    assert memory["logical_depth"] == 64
    assert memory["depth"] == 256


def test_openram_env_infers_backend_pdk_root(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    pdk = root / "backend" / "pdk"
    stage_dir = root / "backend" / "workflows" / "wf1" / "digital" / "mbist_rtl_insertion"
    pdk.mkdir(parents=True)
    stage_dir.mkdir(parents=True)
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("CHIPLOOP_PDK_ROOT", raising=False)
    monkeypatch.delenv("OPEN_PDKS_ROOT", raising=False)

    env = agent._openram_env(str(stage_dir))

    assert env["PDK_ROOT"] == str(pdk.resolve())


def test_openram_validation_rejects_partial_collateral(tmp_path):
    model = tmp_path / "demo_sram_32x64.v"
    model.write_text(
        "module demo_sram_32x64(input clk,input [5:0] addr,output reg [31:0] dout);"
        "reg [31:0] mem [0:63]; always @(posedge clk) dout <= mem[addr]; endmodule\n",
        encoding="utf-8",
    )
    collateral = {
        "behavioral_model": str(model),
        "lib": [],
        "lef": [],
        "gds": [],
    }

    result = agent._validate_openram_collateral(collateral, {"cell": "demo_sram_32x64"})

    assert result["status"] == "issues"
    assert result["missing"] == ["lib", "lef", "gds"]


def test_rejects_abstract_memory_shell_without_openram_behavioral_model(tmp_path):
    prefix = tmp_path / "chiploop-dft"
    bin_dir = prefix / "bin"
    hardware_dir = prefix / "lib" / "python3.13" / "site-packages" / "autombist" / "tests" / "hardware"
    bin_dir.mkdir(parents=True)
    hardware_dir.mkdir(parents=True)
    autombist = bin_dir / "autombist"
    autombist.write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "demo_sram_32x64.v"
    src.write_text("module demo_sram_32x64; endmodule\n", encoding="utf-8")
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    result = agent._stage_memory_model_for_autombist(
        {"cell": "demo_sram_32x64", "source_file": str(src)},
        str(autombist),
        str(stage_dir),
    )

    assert result["status"] == "openram_behavioral_model_missing"
    assert result["simulation_model_source"] == "missing_openram_behavioral_model"
    assert not (hardware_dir / "demo_sram_32x64.v").exists()


def test_stages_generated_sim_model_only_when_explicitly_allowed(tmp_path):
    prefix = tmp_path / "chiploop-dft"
    bin_dir = prefix / "bin"
    hardware_dir = prefix / "lib" / "python3.13" / "site-packages" / "autombist" / "tests" / "hardware"
    bin_dir.mkdir(parents=True)
    hardware_dir.mkdir(parents=True)
    autombist = bin_dir / "autombist"
    autombist.write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "demo_sram_32x64.v"
    src.write_text("module demo_sram_32x64; endmodule\n", encoding="utf-8")
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    result = agent._stage_memory_model_for_autombist(
        {"cell": "demo_sram_32x64", "source_file": str(src), "ports": {"clk": "clk", "addr": "addr", "din": "din", "dout": "dout", "we": "web"}, "addr_width": 6, "data_width": 32},
        str(autombist),
        str(stage_dir),
        allow_generated_sim_model=True,
    )

    assert result["status"] == "staged"
    assert result["simulation_model_source"] == "generated_behavioral_sram"
    assert "reg [31:0] mem [0:63]" in (hardware_dir / "demo_sram_32x64.v").read_text(encoding="utf-8")


def test_stages_real_memory_model_when_source_has_storage(tmp_path):
    prefix = tmp_path / "chiploop-dft"
    bin_dir = prefix / "bin"
    hardware_dir = prefix / "lib" / "python3.13" / "site-packages" / "autombist" / "tests" / "hardware"
    bin_dir.mkdir(parents=True)
    hardware_dir.mkdir(parents=True)
    autombist = bin_dir / "autombist"
    autombist.write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "demo_sram_32x64.v"
    src.write_text(
        "module demo_sram_32x64(input clk,input [5:0] addr,input [31:0] din,output reg [31:0] dout);"
        "reg [31:0] mem [0:63]; always @(posedge clk) dout <= mem[addr]; endmodule\n",
        encoding="utf-8",
    )
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    result = agent._stage_memory_model_for_autombist(
        {"cell": "demo_sram_32x64", "source_file": str(src), "ports": {"clk": "clk", "addr": "addr", "din": "din", "dout": "dout", "we": "web"}, "addr_width": 6, "data_width": 32},
        str(autombist),
        str(stage_dir),
    )

    assert result["status"] == "staged"
    assert result["simulation_model_source"] == "detected_rtl_source"
    assert (hardware_dir / "demo_sram_32x64.v").read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_stages_openram_behavioral_model_for_spec_memory_without_source_file(tmp_path):
    prefix = tmp_path / "chiploop-dft"
    bin_dir = prefix / "bin"
    hardware_dir = prefix / "lib" / "python3.13" / "site-packages" / "autombist" / "tests" / "hardware"
    bin_dir.mkdir(parents=True)
    hardware_dir.mkdir(parents=True)
    autombist = bin_dir / "autombist"
    autombist.write_text("#!/bin/sh\n", encoding="utf-8")
    model = tmp_path / "sky130_sram_1kbyte_1rw1r_32x256_8.v"
    model.write_text(
        "module sky130_sram_1kbyte_1rw1r_32x256_8(input clk,input [7:0] addr,input [31:0] din,output reg [31:0] dout);"
        "reg [31:0] mem [0:255]; always @(posedge clk) dout <= mem[addr]; endmodule\n",
        encoding="utf-8",
    )
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    result = agent._stage_memory_model_for_autombist(
        {
            "cell": "sky130_sram_1kbyte_1rw1r_32x256_8",
            "openram_behavioral_model": str(model),
            "ports": {"clk": "clk", "addr": "addr", "din": "din", "dout": "dout", "we": "web"},
            "addr_width": 8,
            "data_width": 32,
        },
        str(autombist),
        str(stage_dir),
    )

    assert result["status"] == "staged"
    assert result["simulation_model_source"] == "detected_rtl_source"
    assert (hardware_dir / "sky130_sram_1kbyte_1rw1r_32x256_8.v").read_text(encoding="utf-8") == model.read_text(encoding="utf-8")


def test_replaces_openram_instance_with_wrapper(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        """
module top(input logic clk);
  logic reset_n;
  logic [7:0] addr;
  logic [31:0] din;
  logic [31:0] dout;
  openram_sram_256x32 u_sram(.clk(clk), .addr(addr), .din(din), .dout(dout));
endmodule
""",
        encoding="utf-8",
    )
    memory = agent._detect_openram_memory([str(rtl)])
    out_dir = tmp_path / "out"

    patched = agent._replace_memory_instance_with_wrapper(
        memory,
        "openram_sram_256x32_mbist",
        ["clk", "reset_n", "addr", "din", "dout", "bist_start", "bist_done"],
        str(out_dir),
    )

    assert patched is not None
    text = Path(patched).read_text(encoding="utf-8")
    assert "openram_sram_256x32_mbist u_sram" in text
    assert ".addr(addr)" in text
    assert ".bist_start(1'b0)" in text


def test_replaces_multiple_memory_instances_in_same_source(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        """
module top(input logic clk);
  logic [5:0] addr_a;
  logic [7:0] addr_b;
  logic [31:0] din_a, dout_a;
  logic [15:0] din_b, dout_b;
  openram_sram_64x32 u_a(.clk(clk), .addr(addr_a), .din(din_a), .dout(dout_a));
  openram_sram_256x16 u_b(.clk(clk), .addr(addr_b), .din(din_b), .dout(dout_b));
endmodule
""",
        encoding="utf-8",
    )
    memories = agent._detect_openram_memories([str(rtl)])
    out_dir = tmp_path / "out"

    patched = agent._replace_memory_instances_with_wrappers([
        {"memory": memories[0], "wrapper_module": "openram_sram_64x32_mbist", "wrapper_ports": ["clk", "addr", "din", "dout"]},
        {"memory": memories[1], "wrapper_module": "openram_sram_256x16_mbist", "wrapper_ports": ["clk", "addr", "din", "dout"]},
    ], str(out_dir))

    assert len(patched) == 1
    text = Path(patched[0]).read_text(encoding="utf-8")
    assert "openram_sram_64x32_mbist u_a" in text
    assert "openram_sram_256x16_mbist u_b" in text
    assert "openram_sram_64x32 u_a" not in text
    assert "openram_sram_256x16 u_b" not in text


def test_replaces_rtl_model_instance_with_openram_wrapper(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        """
module top(input logic clk);
  logic [5:0] addr;
  logic [31:0] din;
  logic [31:0] dout;
  demo_sram_32x64_model u_sram_model(.clk(clk), .addr(addr), .din(din), .dout(dout));
endmodule
""",
        encoding="utf-8",
    )
    memory = {
        "kind": "memory_instance",
        "cell": "demo_sram_32x64",
        "openram_cell": "demo_sram_32x64",
        "rtl_cell": "demo_sram_32x64_model",
        "instance": "u_sram_model",
        "source_file": str(rtl),
        "connections": {"clk": "clk", "addr": "addr", "din": "din", "dout": "dout"},
    }

    patched = agent._replace_memory_instances_with_wrappers([
        {"memory": memory, "wrapper_module": "demo_sram_32x64_mbist", "wrapper_ports": ["clk", "addr", "din", "dout"]},
    ], str(tmp_path / "out"))

    assert len(patched) == 1
    text = Path(patched[0]).read_text(encoding="utf-8")
    assert "demo_sram_32x64_mbist u_sram_model" in text
    assert "demo_sram_32x64_model u_sram_model" not in text


def test_integrated_rtl_lint_requires_iverilog_and_verilator_pass(tmp_path, monkeypatch):
    rtl = tmp_path / "top.v"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")

    monkeypatch.setattr(agent, "tool_path", lambda tool, state: tool)

    def fake_run(cmd, cwd, timeout=600):
        if cmd[0] == "iverilog":
            return 0, ""
        if cmd[0] == "verilator":
            return 0, ""
        return 1, "unexpected"

    monkeypatch.setattr(agent, "_run", fake_run)
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)

    result = agent._run_integrated_rtl_lint({}, "wf1", str(tmp_path / "stage"), [str(rtl)])

    assert result["status"] == "pass"
    assert result["iverilog"]["status"] == "pass"
    assert result["verilator"]["status"] == "pass"


def test_integrated_rtl_lint_fails_on_iverilog_width_warning(tmp_path, monkeypatch):
    rtl = tmp_path / "top.v"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")

    monkeypatch.setattr(agent, "tool_path", lambda tool, state: tool)

    def fake_run(cmd, cwd, timeout=600):
        if cmd[0] == "iverilog":
            return 0, "top.v:2: warning: Port 4 (addr) of sram expects 6 bits, got 1.\n: Padding 5 high bits of the port.\n"
        if cmd[0] == "verilator":
            return 0, ""
        return 1, "unexpected"

    monkeypatch.setattr(agent, "_run", fake_run)
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)

    result = agent._run_integrated_rtl_lint({}, "wf1", str(tmp_path / "stage"), [str(rtl)])

    assert result["status"] == "failed"
    assert result["reason"] == "integrated_rtl_lint_failed"
    assert result["iverilog"]["status"] == "fail"
    assert result["iverilog"]["structural_width_warnings"] is True


def test_autombist_fault_text_does_not_make_successful_run_fail(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report.txt").write_text("Fault injection complete. Faults detected. Simulation: PASS\n", encoding="utf-8")

    assert agent._simulation_passed(str(reports), "generated fault masks; fail pin observed during test", rc=0) is True
