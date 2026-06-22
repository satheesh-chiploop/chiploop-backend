import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_arch2rtl_dashboard_agent as dashboard_agent


def test_arch2rtl_dashboard_reports_interface_storage_and_clock_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    workflow_dir = tmp_path / "backend" / "workflows" / "wf"
    rtl_dir = workflow_dir / "rtl"
    rtl_dir.mkdir(parents=True)
    rtl = rtl_dir / "pwm_controller.sv"
    rtl.write_text(
        """
module pwm_controller(
  input logic clk,
  input logic reset_n,
  input logic enable,
  input logic [7:0] duty_cycle,
  output logic pwm_out
);
  logic [7:0] counter;
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) begin
      counter <= 8'd0;
      pwm_out <= 1'b0;
    end else if (enable) begin
      counter <= counter + 8'd1;
      pwm_out <= counter < duty_cycle;
    end
  end
endmodule
""",
        encoding="utf-8",
    )
    state = {
        "workflow_id": "wf",
        "workflow_dir": str(workflow_dir),
        "top_module": "pwm_controller",
        "rtl_files": [str(rtl)],
    }

    dashboard_agent.run_agent(state)

    report = json.loads((workflow_dir / "digital" / "arch2rtl_dashboard.json").read_text(encoding="utf-8"))
    assert report["top_module"] == "pwm_controller"
    assert report["interface"]["input_count"] == 11
    assert report["interface"]["output_count"] == 1
    assert report["clock_reset"]["primary_clock"] == "clk"
    assert report["clock_reset"]["primary_reset"] == "reset_n"
    assert report["storage"]["flipflop_count"] == 9
    assert report["storage"]["latch_count"] == 0
    assert report["timing"]["full_cycle_path_count"] >= 1
    assert state["arch2rtl_dashboard"]["type"] == "arch2rtl_dashboard"


def test_arch2rtl_dashboard_separates_mbist_package_from_functional_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    workflow_dir = tmp_path / "backend" / "workflows" / "wf"
    rtl_dir = workflow_dir / "rtl"
    handoff_rtl = workflow_dir / "handoff" / "pkg" / "rtl"
    summary_dir = workflow_dir / "digital" / "mbist_rtl_insertion"
    rtl_dir.mkdir(parents=True)
    handoff_rtl.mkdir(parents=True)
    summary_dir.mkdir(parents=True)

    source_top = rtl_dir / "top.sv"
    source_top.write_text(
        """
module top(input logic clk, input logic reset_n, input logic din, output logic dout);
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) dout <= 1'b0;
    else dout <= din;
  end
endmodule
""",
        encoding="utf-8",
    )
    (handoff_rtl / "top.sv").write_text(source_top.read_text(encoding="utf-8"), encoding="utf-8")
    (handoff_rtl / "sram_model.sv").write_text(
        """
module sram_model(input logic clk, input logic [7:0] addr, input logic [31:0] din, output logic [31:0] dout);
  logic [31:0] mem [0:255];
  always_ff @(posedge clk) begin
    mem[addr] <= din;
    dout <= mem[addr];
  end
endmodule
""",
        encoding="utf-8",
    )
    (summary_dir / "mbist_rtl_insertion_summary.json").write_text(
        json.dumps({
            "status": "mbist_rtl_generated_and_simulated",
            "deduped_functional_rtl": {"files": [str(source_top)]},
        }),
        encoding="utf-8",
    )
    state = {
        "workflow_id": "wf",
        "workflow_dir": str(workflow_dir),
        "top_module": "top",
        "rtl_files": [str(handoff_rtl / "top.sv"), str(handoff_rtl / "sram_model.sv")],
    }

    dashboard_agent.run_agent(state)

    report = json.loads((workflow_dir / "digital" / "arch2rtl_dashboard.json").read_text(encoding="utf-8"))
    assert report["metric_policy"]["primary_scope"] == "functional"
    assert report["rtl_file_count"] == 1
    assert report["module_count"] == 1
    assert report["storage"]["flipflop_count"] == 1
    assert report["storage"]["memory_bit_count"] == 0
    package = report["scopes"]["complete_package"]
    assert package["rtl_file_count"] == 2
    assert package["module_count"] == 2
    assert package["storage"]["flipflop_count"] == 1
    assert package["storage"]["memory_bit_count"] == 8192
