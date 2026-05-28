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
    assert report["interface"]["input_count"] == 4
    assert report["interface"]["output_count"] == 1
    assert report["clock_reset"]["primary_clock"] == "clk"
    assert report["clock_reset"]["primary_reset"] == "reset_n"
    assert report["storage"]["flipflop_count"] == 2
    assert report["storage"]["latch_count"] == 0
    assert report["timing"]["full_cycle_path_count"] >= 1
    assert state["arch2rtl_dashboard"]["type"] == "arch2rtl_dashboard"
