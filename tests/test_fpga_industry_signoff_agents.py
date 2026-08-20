from pathlib import Path

import pytest

from agents.fpga.fpga_board_bringup_validation_agent import run_agent as run_bringup
from agents.fpga.fpga_constraint_cdc_signoff_agent import run_agent as run_constraint_signoff
from agents.fpga.fpga_power_device_qualification_agent import run_agent as run_power_qualification


def test_constraint_signoff_passes_single_clock_complete_constraints(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top(input clk, output reg led); always @(posedge clk) led <= ~led; endmodule")
    state = {
        "workflow_id": "signoff-pass",
        "workflow_dir": str(tmp_path),
        "target_frequency_mhz": 25,
        "fpga": {
            "rtl_files": [str(rtl)],
            "constraints": {
                "status": "ok",
                "constraint_format": "pcf",
                "constraint_path": str(tmp_path / "top.pcf"),
                "unconstrained_ports": [],
                "target_frequency_mhz": 25,
            },
        },
    }
    result = run_constraint_signoff(state)
    assert result["fpga"]["constraint_cdc_signoff"]["status"] == "pass"
    assert result["fpga"]["constraint_cdc_signoff"]["detected_clocks"] == ["clk"]


def test_resetless_design_is_an_advisory_not_a_signoff_review(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top(input clk, output reg led); always @(posedge clk) led <= ~led; endmodule")
    digital = tmp_path / "digital"
    digital.mkdir()
    (digital / "reset_integrity_findings.json").write_text(
        '{"findings":[{"type":"no_reset_detected","severity":"warning","msg":"No reset signal detected by heuristic."}]}',
        encoding="utf-8",
    )
    state = {
        "workflow_id": "resetless-signoff",
        "workflow_dir": str(tmp_path),
        "target_frequency_mhz": 25,
        "fpga": {
            "rtl_files": [str(rtl)],
            "constraints": {"unconstrained_ports": [], "target_frequency_mhz": 25},
        },
    }
    result = run_constraint_signoff(state)
    summary = result["fpga"]["constraint_cdc_signoff"]
    assert summary["status"] == "pass"
    assert summary["warnings"] == []
    assert summary["advisories"] == ["No reset signal detected by heuristic."]
def test_constraint_signoff_blocks_unconstrained_ports(tmp_path):
    state = {
        "workflow_id": "signoff-fail",
        "workflow_dir": str(tmp_path),
        "fpga": {"constraints": {"unconstrained_ports": ["led"]}},
    }
    with pytest.raises(RuntimeError, match="constraint/CDC signoff failed"):
        run_constraint_signoff(state)


def test_constraint_signoff_blocks_secondary_clock_without_frequency(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        "module top(input clk, input spi_sclk, output reg q); "
        "always @(posedge clk) q <= 1'b0; always @(posedge spi_sclk) q <= 1'b1; endmodule"
    )
    state = {
        "workflow_id": "signoff-two-clocks",
        "workflow_dir": str(tmp_path),
        "target_frequency_mhz": 25,
        "fpga": {
            "rtl_files": [str(rtl)],
            "constraints": {
                "unconstrained_ports": [],
                "target_frequency_mhz": 25,
                "clock_constraints_mhz": {"clk": 25},
            },
        },
    }
    with pytest.raises(RuntimeError, match="spi_sclk"):
        run_constraint_signoff(state)


def test_hardware_validation_defaults_to_not_requested(tmp_path):
    state = {"workflow_id": "bringup-disabled", "workflow_dir": str(tmp_path), "fpga": {}}
    result = run_bringup(state)
    assert result["fpga"]["hardware_validation"]["status"] == "not_requested"
    assert result["fpga"]["hardware_validation"]["tool"] == "openFPGALoader"


def test_power_qualification_reports_headroom_and_estimate(tmp_path):
    state = {
        "workflow_id": "power",
        "workflow_dir": str(tmp_path),
        "target_frequency_mhz": 50,
        "fpga": {
            "target": {"board": "icebreaker"},
            "synthesis": {"logical_cells_used": 1000, "logical_cells_available": 5280, "flip_flops": 500},
            "place_route": {
                "status": "completed",
                "logical_cells_used": 1100,
                "logical_cells_available": 5280,
                "logic_utilization_percent": 20.833,
            },
        },
    }
    result = run_power_qualification(state)
    summary = result["fpga"]["power_device_qualification"]
    assert summary["status"] == "pass"
    assert summary["resource_headroom_percent"] == pytest.approx(79.167)
    assert summary["estimated_total_power_mw"] > 0


def test_backend_and_supabase_and_frontend_wiring():
    root = Path(__file__).resolve().parents[2]
    main = (root / "backend" / "main.py").read_text(encoding="utf-8")
    migration = (root / "backend" / "supabase" / "migrations" / "phase_20260730_fpga_industry_signoff_apps.sql").read_text(encoding="utf-8")
    app_catalog = (root / "frontend" / "app" / "apps" / "page.tsx").read_text(encoding="utf-8")
    for name in (
        "FPGA Constraint and CDC/RDC Signoff Agent",
        "FPGA Board Bring-up and Hardware Validation Agent",
        "FPGA Power and Device Qualification Agent",
    ):
        assert name in main
        assert name in migration
    for slug in ("fpga-constraint-signoff", "fpga-board-bringup", "fpga-power-qualification"):
        assert slug in app_catalog
