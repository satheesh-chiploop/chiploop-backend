import os
import json


os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital import digital_drc_agent, digital_lvs_agent, digital_tapeout_agent
from agents.digital.digital_drc_agent import _drc_status, _macro_blackbox_deferred as _drc_macro_blackbox_deferred
from agents.digital.digital_failure_debug_agent import run_agent as failure_debug_agent
from agents.digital import digital_spec2rtl_conformance_agent as spec2rtl_agent
from agents.digital import digital_fill_agent, digital_sta_postfill_agent
from agents.digital.digital_logic_equivalence_agent import _generated_stdcell_model, _missing_stdcell_models, _prepare_golden_rtl_for_yosys, _yosys_script
from agents.digital.digital_lvs_agent import _lvs_status, _macro_blackbox_deferred as _lvs_macro_blackbox_deferred
from agents.digital.digital_scan_atpg_agent import _adapter_log_has_execution_error, _generate_full_scan_bench, _metrics_show_real_atpg_result, _pattern_count_from_file
from agents.digital import digital_tapeout_lec_agent as tapeout_lec_agent
from agents.digital.digital_tapeout_lec_agent import PHYSICAL_ONLY_TOP_PORTS, _top_ports
from agents.digital.digital_tapeout_agent import _blocking_xor_difference_count, _copy_xor_report, _tapeout_failure_reasons, _xor_difference_count, _xor_layer_counts, _xor_signoff_status


def test_physical_stage_selects_one_physical_top_netlist():
    chosen = digital_fill_agent._select_single_top_netlist([
        "/work/inputs/netlist/temp_monitor_soc_phys_synth.v",
        "/work/inputs/netlist/temp_monitor_soc_phys.nl.v",
    ])

    assert chosen == ["/work/inputs/netlist/temp_monitor_soc_phys_synth.v"]


def test_physical_stage_clears_stale_local_netlists(tmp_path):
    stale_a = tmp_path / "top_synth.v"
    stale_b = tmp_path / "top.nl.v"
    stale_a.write_text("module top(); endmodule\n", encoding="utf-8")
    stale_b.write_text("module top(); endmodule\n", encoding="utf-8")

    digital_fill_agent._clear_stage_netlists(str(tmp_path))

    assert not stale_a.exists()
    assert not stale_b.exists()


def test_sta_postfill_sanitizes_inherited_duplicate_netlists():
    chosen = digital_sta_postfill_agent._select_single_top_netlist_ref([
        "inputs/netlist/temp_monitor_soc_phys_synth.v",
        "inputs/netlist/temp_monitor_soc_phys.nl.v",
    ])

    assert chosen == ["inputs/netlist/temp_monitor_soc_phys_synth.v"]


def test_signoff_config_resolution_prefers_latest_physical_config_over_global(tmp_path):
    workflow_dir = tmp_path
    impl_cfg = workflow_dir / "digital" / "impl_setup" / "openlane" / "config.json"
    fill_cfg = workflow_dir / "digital" / "fill" / "config.json"
    impl_cfg.parent.mkdir(parents=True)
    fill_cfg.parent.mkdir(parents=True)
    impl_cfg.write_text(json.dumps({"FP_CORE_UTIL": 20}), encoding="utf-8")
    fill_cfg.write_text(json.dumps({"FP_CORE_UTIL": 10, "MACRO_PLACEMENT_CFG": "dir::inputs/macros/macro_placement.cfg"}), encoding="utf-8")

    state = {"digital": {"openlane_config": str(impl_cfg)}}

    assert digital_drc_agent._resolve_config_from_state(state, str(workflow_dir)) == str(fill_cfg)
    assert digital_lvs_agent._resolve_config_from_state(state, str(workflow_dir)) == str(fill_cfg)
    assert digital_tapeout_agent._resolve_config_from_state(state, str(workflow_dir)) == str(fill_cfg)


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
    assert report["summary"]["checked"] == (
        report["summary"]["matched"]
        + report["summary"]["partial"]
        + report["summary"]["missing"]
        + report["summary"]["inconclusive"]
    )


def test_spec2rtl_uses_structured_spec_and_regmap_without_prose_port_false_misses(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    digital_dir = tmp_path / "digital"
    rtl_dir = tmp_path / "rtl"
    spec_dir.mkdir()
    digital_dir.mkdir()
    rtl_dir.mkdir()
    spec_path = spec_dir / "pwm_controller_spec.json"
    regmap_path = digital_dir / "digital_regmap.json"
    spec_path.write_text(json.dumps({
        "hierarchy": {
            "top_module": {
                "name": "pwm_controller",
                "ports": [
                    {"name": "clk", "direction": "input", "width": 1},
                    {"name": "reset_n", "direction": "input", "width": 1},
                    {"name": "wr_en", "direction": "input", "width": 1},
                    {"name": "wr_addr", "direction": "input", "width": 8},
                    {"name": "wr_data", "direction": "input", "width": 8},
                    {"name": "rd_en", "direction": "input", "width": 1},
                    {"name": "rd_addr", "direction": "input", "width": 8},
                    {"name": "rd_data", "direction": "output", "width": 8},
                    {"name": "pwm_out", "direction": "output", "width": 1},
                    {"name": "counter_value", "direction": "output", "width": 8},
                ],
                "responsibilities": [
                    "Accept memory-mapped register write and read transactions.",
                    "Return addressed register values on rd_data.",
                ],
                "behavior_rules": [
                    "Reads to 0x00 return CONTROL with bit 0 reflecting ENABLE and other bits zero.",
                    "Reads to 0x0C return the live counter_value.",
                    "Reads to unmapped addresses return zero.",
                ],
                "must_drive": ["rd_data", "pwm_out", "counter_value"],
                "must_receive": ["clk", "reset_n", "wr_en", "wr_addr", "wr_data", "rd_en", "rd_addr"],
                "reset_behavior": "All registers and outputs reset to zero when reset_n is low.",
                "rtl_output_file": "pwm_controller.v",
            },
            "modules": [],
        },
        "register_contract": {
            "registers": [
                {"name": "CONTROL", "address": "0x00", "fields": [{"name": "ENABLE"}]},
                {"name": "COUNTER_VALUE", "address": "0x0C", "fields": [{"name": "counter_value"}]},
            ]
        },
    }), encoding="utf-8")
    regmap_path.write_text(json.dumps({
        "regmap": {
            "registers": [
                {"name": "CONTROL", "offset": "0x00", "fields": [{"name": "ENABLE"}]},
                {"name": "COUNTER_VALUE", "offset": "0x0C", "fields": [{"name": "counter_value"}]},
            ]
        }
    }), encoding="utf-8")
    rtl = rtl_dir / "pwm_controller.v"
    rtl.write_text(
        """
module pwm_controller(input clk, input reset_n, input wr_en, input [7:0] wr_addr, input [7:0] wr_data,
  input rd_en, input [7:0] rd_addr, output [7:0] rd_data, output pwm_out, output [7:0] counter_value);
reg enable_r;
reg [7:0] rd_data_r;
reg [7:0] counter_value_r;
assign rd_data = rd_data_r;
assign counter_value = counter_value_r;
assign pwm_out = enable_r && (counter_value_r < 8'h04);
always @(posedge clk or negedge reset_n) begin
  if (!reset_n) begin
    enable_r <= 1'b0;
    rd_data_r <= 8'h00;
    counter_value_r <= 8'h00;
  end else begin
    if (wr_en && wr_addr == 8'h00) enable_r <= wr_data[0];
    counter_value_r <= counter_value_r + 8'h01;
    if (rd_en) begin
      case (rd_addr)
        8'h00: rd_data_r <= {7'b0000000, enable_r};
        8'h0C: rd_data_r <= counter_value_r;
        default: rd_data_r <= 8'h00;
      endcase
    end
  end
end
endmodule
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(spec2rtl_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    out = spec2rtl_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "artifact_dir": str(digital_dir),
        "top_module": "pwm_controller",
        "spec_json": str(spec_path),
        "digital_regmap_json": str(regmap_path),
        "rtl_files": [str(rtl)],
        "spec_text": "All registers and outputs reset to zero when reset_n is low.",
    })

    report = out["spec2rtl_conformance"]
    assert report["status"] == "pass"
    assert report["interface"]["missing_ports"] == []
    assert report["register_map"]["missing"] == []
    assert report["summary"]["checked"] == report["summary"]["matched"]


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
    assert _adapter_log_has_execution_error("Fatal error: Error in circuit file")
    assert not _adapter_log_has_execution_error("Atalanta completed with 12 test patterns")


def test_lec_script_uses_configurable_induction_depth(monkeypatch):
    monkeypatch.setenv("CHIPLOOP_LEC_INDUCT_DEPTH", "96")

    script = _yosys_script(["rtl.sv"], "gate.v", "top", ["cells.v"])

    assert "equiv_induct -undef -seq 96" in script


def test_tapeout_lec_script_can_ignore_physical_gate_ports():
    script = _yosys_script(["rtl.sv"], "gate.v", "top", ["cells.v"], gate_ignore_ports=["VPWR", "VGND"])

    assert "delete -port w:VPWR w:VGND" in script
    assert script.index("delete -port w:VPWR w:VGND") < script.index("rename -top gate")


def test_lec_script_reads_macro_blackbox_before_gate_netlist():
    script = _yosys_script(["rtl.sv", "macro_blackbox.v"], "gate.v", "top", ["cells.v"], gate_blackbox_verilog=["macro_blackbox.v"])

    assert script.count('read_verilog -sv "macro_blackbox.v"') == 2
    gate_read = 'read_verilog -sv "gate.v"'
    assert script.rindex('read_verilog -sv "macro_blackbox.v"') < script.index(gate_read)


def test_tapeout_physical_only_port_detection(tmp_path):
    rtl = tmp_path / "rtl.sv"
    gate = tmp_path / "gate.v"
    rtl.write_text("module top(input clk, output y); endmodule\n", encoding="utf-8")
    gate.write_text("module top(clk, VPWR, VGND, y); input clk; inout VPWR; inout VGND; output y; endmodule\n", encoding="utf-8")

    ignored = (_top_ports(str(gate), "top") - _top_ports(str(rtl), "top")) & PHYSICAL_ONLY_TOP_PORTS

    assert ignored == {"VPWR", "VGND"}


def test_tapeout_lec_blocks_when_tapeout_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(tapeout_lec_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(tapeout_lec_agent, "run_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("yosys should not run")))
    monkeypatch.setattr(tapeout_lec_agent, "tool_path", lambda name, state: "/usr/bin/yosys" if name == "yosys" else None)

    state = tapeout_lec_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "digital": {"tapeout": {"status": "failed"}},
    })

    summary = json.loads((tmp_path / "digital" / "tapeout_lec" / "tapeout_lec_summary.json").read_text(encoding="utf-8"))
    assert state["digital"]["tapeout_lec"]["status"] == "blocked"
    assert summary["failure_reason"] == "blocked_by_tapeout_failure"
    assert summary["upstream_tapeout_status"] == "failed"


def test_tapeout_status_ignores_xor_by_default():
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
    assert "xor_differences_found" not in reasons
    assert _xor_signoff_status() == "not_applicable"


def test_tapeout_xor_can_be_configured_as_blocking(monkeypatch):
    monkeypatch.setattr("agents.digital.digital_tapeout_agent.DEFAULT_XOR_SIGNOFF_MODE", "blocking")
    reasons = _tapeout_failure_reasons(
        rc=0,
        log="Total XOR differences: 1",
        drc_status="clean",
        lvs_status="clean",
        klayout_gds="/tmp/top.klayout.gds",
        magic_gds=None,
    )

    assert "xor_differences_found" in reasons
    assert _xor_signoff_status() == "enabled"


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


def test_tapeout_reports_analog_macro_gds_missing_when_signoff_deferred():
    reasons = _tapeout_failure_reasons(
        rc=0,
        log="",
        drc_status="blackbox_deferred",
        lvs_status="blackbox_deferred",
        klayout_gds=None,
        magic_gds=None,
    )

    assert "analog_macro_gds_missing" in reasons
    assert "drc_not_clean" in reasons
    assert "lvs_not_clean" in reasons


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
  sky130_fd_sc_hd__clkinv_2 clkinv_load(.A(A), .X(X), .VPWR(A), .VGND(A), .VPB(A), .VNB(A));
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


def test_lec_generated_model_covers_scan_dff_without_reset(tmp_path):
    netlist = tmp_path / "scan.v"
    netlist.write_text(
        """
module top(input clk, input d, input scd, input sce, output q);
  sky130_fd_sc_hd__sdfxtp_2 flop(.CLK(clk), .D(d), .SCD(scd), .SCE(sce), .Q(q));
endmodule
""",
        encoding="utf-8",
    )

    model = _generated_stdcell_model(str(netlist), str(tmp_path))

    assert model is not None
    text = open(model, "r", encoding="utf-8").read()
    assert "module sky130_fd_sc_hd__sdfxtp_2" in text
    assert "wire chiploop_d = D;" in text
    assert "wire chiploop_d = (SCE ? SCD : D);" in text
    assert "q_reg <= chiploop_d;" in text
    assert _missing_stdcell_models(str(netlist), [model]) == []


def test_lec_replaces_preserved_macro_rtl_with_blackbox_stub(tmp_path):
    macro_rtl = tmp_path / "analog_model.v"
    macro_rtl.write_text(
        """
module analog_model(input clk, input sample_req, output adc_valid);
  always @(posedge clk or posedge sample_req) begin
  end
endmodule
""",
        encoding="utf-8",
    )
    top_rtl = tmp_path / "top.v"
    top_rtl.write_text(
        """
module top(input clk, input sample_req, output adc_valid);
  analog_model u_analog(.clk(clk), .sample_req(sample_req), .adc_valid(adc_valid));
endmodule
""",
        encoding="utf-8",
    )
    gate = tmp_path / "gate.v"
    gate.write_text(
        """
module top(input clk, input sample_req, output adc_valid);
  analog_model u_analog(.clk(clk), .sample_req(sample_req), .adc_valid(adc_valid));
endmodule
""",
        encoding="utf-8",
    )

    prepared, stubs = _prepare_golden_rtl_for_yosys([str(macro_rtl), str(top_rtl)], str(gate), str(tmp_path), "top")

    assert str(macro_rtl) not in prepared
    assert str(top_rtl) in prepared
    assert len(stubs) == 1
    assert "(* blackbox *)" in open(stubs[0], "r", encoding="utf-8").read()


def test_lec_blackbox_stub_preserves_old_style_bus_and_gate_instance_ports(tmp_path):
    macro_rtl = tmp_path / "temp_sensor_adc_model.v"
    macro_rtl.write_text(
        """
module temp_sensor_adc_model(sample_req, sensor_temp_celsius, adc_code, avdd, avss);
  input sample_req;
  input [15:0] sensor_temp_celsius;
  output [11:0] adc_code;
  inout avdd, avss;
endmodule
""",
        encoding="utf-8",
    )
    top_rtl = tmp_path / "top.v"
    top_rtl.write_text("module top(input sample_req); endmodule\n", encoding="utf-8")
    gate = tmp_path / "gate.v"
    gate.write_text(
        """
module top(input sample_req, input [15:0] sensor_temp_celsius, output [11:0] adc_code, output adc_valid);
  temp_sensor_adc_model u_analog(
    .sample_req(sample_req),
    .sensor_temp_celsius(sensor_temp_celsius),
    .adc_code(adc_code),
    .adc_valid(adc_valid),
    .avdd(1'b1),
    .avss(1'b0)
  );
endmodule
""",
        encoding="utf-8",
    )

    _prepared, stubs = _prepare_golden_rtl_for_yosys([str(macro_rtl), str(top_rtl)], str(gate), str(tmp_path), "top")
    text = open(stubs[0], "r", encoding="utf-8").read()

    assert "module temp_sensor_adc_model(sample_req, sensor_temp_celsius, adc_code, avdd, avss, adc_valid);" in text
    assert "input sensor_temp_celsius;" in text
    assert "output adc_code;" in text
    assert "input adc_valid;" in text


def test_drc_lvs_deferred_xor_does_not_mask_clean_check():
    assert _drc_status(2, 0, "One or more deferred errors were encountered: 1 XOR differences found.") == "clean"
    assert _lvs_status(2, None, "Final result:\nCircuits match uniquely.\nOne or more deferred errors were encountered: 1 XOR differences found.") == "clean"
    assert _lvs_status(2, None, "Final result:\nTop level cell failed pin matching.\nOne or more deferred errors were encountered: 1 XOR differences found.") == "mismatch"


def test_drc_lvs_blackbox_deferred_when_macro_gds_missing():
    assert _drc_macro_blackbox_deferred(["dir::inputs/macros/lef/ana.lef"], ["dir::inputs/macros/lib/ana.lib"], [])
    assert _lvs_macro_blackbox_deferred(["dir::inputs/macros/lef/ana.lef"], ["dir::inputs/macros/lib/ana.lib"], [])
    assert not _drc_macro_blackbox_deferred(["dir::inputs/macros/lef/ana.lef"], [], ["dir::inputs/macros/gds/ana.gds"])


def test_signoff_macro_staging_ignores_directory_collateral(tmp_path):
    bad_dir = tmp_path / "digital"
    bad_dir.mkdir()
    lef = tmp_path / "ana.lef"
    lef.write_text("MACRO ana\nEND ana\n", encoding="utf-8")
    state = {
        "digital": {
            "macro_lefs": [str(bad_dir), str(lef)],
            "macro_libs": [str(bad_dir)],
            "macro_gds": [str(bad_dir)],
        }
    }

    for module in (digital_drc_agent, digital_lvs_agent, digital_tapeout_agent):
        staged_lefs, staged_libs, staged_gds = module._stage_macro_inputs(
            state,
            str(tmp_path),
            str(tmp_path / module.AGENT_NAME.replace(" ", "_")),
        )
        assert staged_lefs == ["dir::inputs/macros/lef/ana.lef"]
        assert staged_libs == []
        assert staged_gds == []


def test_lvs_and_tapeout_spice_staging_ignore_directories(tmp_path):
    bad_dir = tmp_path / "digital"
    bad_dir.mkdir()
    spice = tmp_path / "ana.spice"
    spice.write_text(".subckt ana A B\n.ends ana\n", encoding="utf-8")

    for module in (digital_lvs_agent, digital_tapeout_agent):
        staged_stdcell, staged_extra = module._stage_spice_models(
            str(tmp_path / module.AGENT_NAME.replace(" ", "_")),
            [str(bad_dir)],
            [str(bad_dir), str(spice)],
        )
        assert staged_stdcell == []
        assert staged_extra == ["dir::inputs/spice/ana.spice"]


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


def test_atpg_bench_accepts_scan_dff_without_reset():
    bench, meta = _generate_full_scan_bench(
        """
module top(clk, scan_en, scan_in_0, a, y);
  input clk;
  input scan_en;
  input scan_in_0;
  input a;
  output y;
  wire q;
  sky130_fd_sc_hd__sdfxtp_2 flop(.CLK(clk), .SCE(scan_en), .SCD(scan_in_0), .D(a), .Q(q));
  sky130_fd_sc_hd__buf_1 outbuf(.A(q), .X(y));
endmodule
"""
    )

    assert meta["status"] == "generated"
    assert meta["unsupported_cells"] == []
    assert "INPUT(q)" in bench


def test_atpg_bench_promotes_floating_macro_outputs():
    bench, meta = _generate_full_scan_bench(
        """
module top(a, y);
  input a;
  output y;
  sky130_fd_sc_hd__and2_1 u0(.A(a), .B(macro_ready), .X(y));
endmodule
"""
    )

    assert meta["status"] == "generated"
    assert meta["floating_inputs_promoted"] == ["macro_ready"]
    assert "INPUT(macro_ready)" in bench


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
