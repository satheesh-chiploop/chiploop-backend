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
  openram_sram_256x32 u_sram(.clk(clk), .addr(addr), .din(din), .dout(dout));
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
