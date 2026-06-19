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


def test_autombist_config_uses_detected_sram_ports():
    memory = {
        "cell": "demo_sram_32x64",
        "addr_width": 6,
        "data_width": 32,
        "ports": {"clk": "clk", "csb": "csb", "we": "web", "addr": "addr", "din": "din", "dout": "dout"},
    }

    config = agent._patch_autombist_config("memory_name: sample\nports:\n  clk: clk0\n", memory)

    assert "memory_name: demo_sram_32x64" in config
    assert "wrapper_module_name: demo_sram_32x64" in config
    assert "addr_width: 6" in config
    assert "data_width: 32" in config
    assert "clk0" not in config
    assert "  we: web" in config
    assert "  csb: csb" in config


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


def test_autombist_fault_text_does_not_make_successful_run_fail(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report.txt").write_text("Fault injection complete. Faults detected. Simulation: PASS\n", encoding="utf-8")

    assert agent._simulation_passed(str(reports), "generated fault masks; fail pin observed during test", rc=0) is True
