import os


os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital.digital_drc_agent import _drc_status
from agents.digital.digital_failure_debug_agent import run_agent as failure_debug_agent
from agents.digital import digital_spec2rtl_conformance_agent as spec2rtl_agent
from agents.digital.digital_logic_equivalence_agent import _generated_stdcell_model, _missing_stdcell_models, _yosys_script
from agents.digital.digital_lvs_agent import _lvs_status
from agents.digital.digital_scan_atpg_agent import _adapter_log_has_execution_error, _generate_full_scan_bench, _metrics_show_real_atpg_result, _pattern_count_from_file
from agents.digital.digital_tapeout_lec_agent import PHYSICAL_ONLY_TOP_PORTS, _top_ports
from agents.digital.digital_tapeout_agent import _blocking_xor_difference_count, _copy_xor_report, _tapeout_failure_reasons, _xor_difference_count, _xor_layer_counts


def test_atpg_zero_pattern_metrics_are_not_success():
    assert not _metrics_show_real_atpg_result(
        pattern_count=0,
        stuck_at_coverage_pct=None,
        faults_detected=None,
        faults_undetected=None,
        faults_aborted=None,
    )


def test_failure_debug_is_log_first_and_proposal_only(tmp_path):
    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "enable_failure_debug": True,
        "failure_debug_options": {
            "enabled": True,
            "log_only_first": True,
            "generate_vcd_if_inconclusive": True,
            "auto_apply_rtl_fixes": False,
        },
        "failure_triage": {
            "failures": [
                {
                    "testcase": "constrained_random_sanity",
                    "seed": 7,
                    "classification": "rtl_or_golden_model_mismatch",
                    "stdout_tail": ["scoreboard mismatch expected 3 actual 2"],
                    "stderr_tail": [],
                }
            ]
        },
    }

    out = failure_debug_agent(state)
    item = out["failure_debug"]["items"][0]

    assert item["root_cause_classification"] == "rtl_or_reference_mismatch"
    assert item["patch_policy"] == "proposal_only"
    assert item["targeted_rerun"]["testcase"] == "constrained_random_sanity"


def test_spec2rtl_reports_matched_and_missing_requirements(tmp_path, monkeypatch):
    rtl = tmp_path / "pwm_controller.sv"
    rtl.write_text(
        """
module pwm_controller(input logic clk, input logic reset_n, input logic enable, input logic [7:0] duty_cycle, output logic pwm_out);
  logic [7:0] counter_value;
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) counter_value <= 8'd0;
    else if (enable) counter_value <= counter_value + 1'b1;
  end
  assign pwm_out = counter_value < duty_cycle;
endmodule
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(spec2rtl_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    out = spec2rtl_agent.run_agent({
        "workflow_id": "wf",
        "artifact_dir": str(tmp_path),
        "top_module": "pwm_controller",
        "spec_text": """
Inputs: clk, reset_n, enable, duty_cycle[7:0]
Outputs: pwm_out, irq
Counter increments when enable is high.
pwm_out is high when counter_value is less than duty_cycle.
IRQ shall assert when period completes.
""",
        "rtl_files": [str(rtl)],
    })

    report = out["spec2rtl_conformance"]
    assert report["status"] in {"partial", "issues"}
    assert report["interface"]["missing_ports"] == ["irq"]
    assert report["summary"]["matched"] >= 1
    assert report["summary"]["missing"] >= 1


def test_atpg_positive_patterns_or_coverage_are_success():
    assert _metrics_show_real_atpg_result(
        pattern_count=1,
        stuck_at_coverage_pct=None,
        faults_detected=None,
        faults_undetected=None,
        faults_aborted=None,
    )
    assert _metrics_show_real_atpg_result(
        pattern_count=0,
        stuck_at_coverage_pct=84.5,
        faults_detected=None,
        faults_undetected=None,
        faults_aborted=None,
    )


def test_atpg_adapter_log_execution_errors_are_failures():
    assert _adapter_log_has_execution_error("/opt/chiploop-tools/run_atalanta_atpg.sh: line 1: No such file or directory")
    assert not _adapter_log_has_execution_error("Atalanta completed with 12 test patterns")


def test_lec_script_uses_configurable_induction_depth(monkeypatch):
    monkeypatch.setenv("CHIPLOOP_LEC_INDUCT_DEPTH", "96")

    script = _yosys_script(["rtl.sv"], "gate.v", "top", ["cells.v"])

    assert "equiv_induct -undef -seq 96" in script


def test_tapeout_lec_script_can_ignore_physical_gate_ports():
    script = _yosys_script(["rtl.sv"], "gate.v", "top", ["cells.v"], gate_ignore_ports=["VPWR", "VGND"])

    assert "delete -port w:VPWR w:VGND" in script
    assert script.index("delete -port w:VPWR w:VGND") < script.index("rename -top gate")


def test_tapeout_physical_only_port_detection(tmp_path):
    rtl = tmp_path / "rtl.sv"
    gate = tmp_path / "gate.v"
    rtl.write_text("module top(input clk, output y); endmodule\n", encoding="utf-8")
    gate.write_text("module top(clk, VPWR, VGND, y); input clk; inout VPWR; inout VGND; output y; endmodule\n", encoding="utf-8")

    ignored = (_top_ports(str(gate), "top") - _top_ports(str(rtl), "top")) & PHYSICAL_ONLY_TOP_PORTS

    assert ignored == {"VPWR", "VGND"}


def test_tapeout_status_is_signoff_gated():
    reasons = _tapeout_failure_reasons(
        rc=0,
        log="Total XOR differences: 1",
        drc_status="clean",
        lvs_status="clean",
        klayout_gds="/tmp/top.klayout.gds",
        magic_gds=None,
    )

    assert "drc_not_clean" not in reasons
    assert "lvs_not_clean" not in reasons
    assert "xor_differences_found" in reasons


def test_tapeout_requires_gds_output():
    reasons = _tapeout_failure_reasons(
        rc=0,
        log="Total XOR differences: 0",
        drc_status="ok",
        lvs_status="ok",
        klayout_gds=None,
        magic_gds=None,
    )

    assert reasons == ["gds_not_produced"]


def test_tapeout_xor_difference_parser():
    assert _xor_difference_count("Total XOR differences: 0") == 0
    assert _xor_difference_count("One or more deferred errors encountered: 1 XOR differences found.") == 1


def test_tapeout_xor_report_is_copied_to_reports(tmp_path):
    run_dir = tmp_path / "runs" / "run1" / "161-klayout-xor"
    run_dir.mkdir(parents=True)
    source = run_dir / "xor.xml"
    source.write_text("<report-database />", encoding="utf-8")
    stage_dir = tmp_path / "stage"

    copied = _copy_xor_report(str(tmp_path / "runs" / "run1"), str(stage_dir))

    assert copied == str(stage_dir / "reports" / "xor.xml")
    assert (stage_dir / "reports" / "xor.xml").read_text(encoding="utf-8") == "<report-database />"


def test_boundary_only_xor_is_nonblocking(tmp_path):
    report = tmp_path / "xor.xml"
    report.write_text(
        """
<report-database>
  <items>
    <item><category>'235/4'</category><cell>top</cell></item>
  </items>
</report-database>
""",
        encoding="utf-8",
    )

    layer_counts = _xor_layer_counts(str(report))

    assert layer_counts == {"235/4": 1}
    assert _blocking_xor_difference_count(1, layer_counts, {"235/4"}) == 0


def test_tapeout_lec_generated_model_covers_physical_sky130_cells(tmp_path):
    netlist = tmp_path / "gate.v"
    netlist.write_text(
        """
module top(input A, output X);
  sky130_fd_sc_hd__tapvpwrvgnd_1 tap(.VPWR(A), .VGND(A), .VPB(A), .VNB(A));
  sky130_fd_sc_hd__decap_3 decap(.VPWR(A), .VGND(A), .VPB(A), .VNB(A));
  sky130_ef_sc_hd__decap_12 efdecap(.VPWR(A), .VGND(A), .VPB(A), .VNB(A));
  sky130_fd_sc_hd__fill_1 fill(.VPWR(A), .VGND(A), .VPB(A), .VNB(A));
  sky130_fd_sc_hd__dlymetal6s2s_1 dly(.A(A), .X(X), .VPWR(A), .VGND(A), .VPB(A), .VNB(A));
  sky130_fd_sc_hd__bufinv_16 clkload(.A(A), .VPWR(A), .VGND(A), .VPB(A), .VNB(A));
  sky130_fd_sc_hd__clkinv_2 clkinv_load(.A(A), .VPWR(A), .VGND(A), .VPB(A), .VNB(A));
  sky130_fd_sc_hd__clkbuf_8 clkbuf_load(.A(A), .VPWR(A), .VGND(A), .VPB(A), .VNB(A));
endmodule
""",
        encoding="utf-8",
    )

    model = _generated_stdcell_model(str(netlist), str(tmp_path))

    assert model is not None
    text = open(model, "r", encoding="utf-8").read()
    assert "module sky130_fd_sc_hd__tapvpwrvgnd_1" in text
    assert "module sky130_fd_sc_hd__decap_3" in text
    assert "module sky130_ef_sc_hd__decap_12" in text
    assert "module sky130_fd_sc_hd__fill_1" in text
    assert "module sky130_fd_sc_hd__bufinv_16" in text
    assert "module sky130_fd_sc_hd__clkinv_2" in text
    assert "module sky130_fd_sc_hd__clkbuf_8" in text
    assert "assign X = A;" in text
    assert _missing_stdcell_models(str(netlist), [model]) == []


def test_drc_lvs_deferred_xor_does_not_mask_clean_check():
    assert _drc_status(2, 0, "One or more deferred errors were encountered: 1 XOR differences found.") == "clean"
    assert _lvs_status(2, None, "Final result:\nCircuits match uniquely.\nOne or more deferred errors were encountered: 1 XOR differences found.") == "clean"


def test_atpg_pattern_count_can_be_inferred_from_adapter_pattern_file(tmp_path):
    pattern_file = tmp_path / "atalanta.test"
    pattern_file.write_text("# atalanta\nTEST 1 0101\nTEST 2 1010\n", encoding="utf-8")

    assert _pattern_count_from_file(str(pattern_file)) == 2


def test_atpg_bench_filters_unused_scan_control_inputs():
    bench, meta = _generate_full_scan_bench(
        """
module top(clk, reset_n, scan_en, scan_in_0, a, y);
  input clk;
  input reset_n;
  input scan_en;
  input scan_in_0;
  input a;
  output y;
  wire q;
  sky130_fd_sc_hd__sdfrtp_1 flop(.CLK(clk), .RESET_B(reset_n), .SCE(scan_en), .D(a), .Q(q));
  sky130_fd_sc_hd__buf_1 outbuf(.A(q), .X(y));
endmodule
"""
    )

    assert meta["status"] == "generated"
    assert "INPUT(clk)" not in bench
    assert "INPUT(reset_n)" not in bench
    assert "INPUT(scan_en)" not in bench
    assert "INPUT(scan_in_0)" not in bench
    assert "INPUT(q)" in bench


def test_atpg_bench_does_not_emit_undriven_primary_outputs():
    bench, meta = _generate_full_scan_bench(
        """
module top(a, y, floating_out);
  input a;
  output y;
  output floating_out;
  sky130_fd_sc_hd__buf_1 outbuf(.A(a), .X(y));
endmodule
"""
    )

    assert meta["status"] == "generated"
    assert "OUTPUT(y)" in bench
    assert "OUTPUT(floating_out)" not in bench
