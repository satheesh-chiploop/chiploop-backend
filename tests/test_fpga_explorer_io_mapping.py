from pathlib import Path
import shutil
import subprocess

from agents.fpga import fpga_explorer_io_mapping_agent as mapping_agent
from agents.fpga import fpga_target_explorer_agent as explorer
from agents.fpga.fpga_common import BOARD_REGISTRY
from agents.fpga import fpga_serial_transport


def test_wide_core_gets_fpga_only_spi_transport(tmp_path, monkeypatch):
    rtl = tmp_path / "adaptive_aero_control_top.sv"
    rtl.write_text(
        "module adaptive_aero_control_top(input logic clk, input logic rst_n, "
        "input logic [127:0] s_axis_cmd, input logic s_axis_valid, "
        "output logic [127:0] m_axis_resp, output logic fault_flag); "
        "assign m_axis_resp=s_axis_cmd; assign fault_flag=~s_axis_valid; endmodule\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fpga_serial_transport, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "fpga": {"top_module": "adaptive_aero_control_top", "rtl_files": [str(rtl)]},
    }

    report = fpga_serial_transport.add_spi_transport_if_needed(state)

    assert report["status"] == "generated"
    assert report["original_top_level_io_bits"] == 260
    assert report["fpga_top_level_io_bits"] == 7
    assert Path(report["wrapper_rtl"]).is_absolute()
    assert state["fpga"]["core_top_module"] == "adaptive_aero_control_top"
    assert state["fpga"]["top_module"] == "adaptive_aero_control_top_spi_fpga_top"
    wrapper = Path(report["wrapper_rtl"]).read_text(encoding="utf-8")
    assert "input  logic spi_sclk" in wrapper
    assert "adaptive_aero_control_top u_core" in wrapper
    assert ".s_axis_cmd(core_s_axis_cmd)" in wrapper
    if shutil.which("iverilog"):
        completed = subprocess.run(
            ["iverilog", "-g2012", "-s", state["fpga"]["top_module"], "-o", str(tmp_path / "wrapper.out"), str(rtl), report["wrapper_rtl"]],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_ulx3s_has_verified_spi_wrapper_pin_mapping(tmp_path, monkeypatch):
    rtl = tmp_path / "spi_top.sv"
    rtl.write_text(
        "module spi_top(input logic clk,input logic reset_n,input logic spi_sclk,"
        "input logic spi_cs_n,input logic spi_mosi,output logic spi_miso,"
        "output logic fault_indicator); endmodule\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mapping_agent, "publish_json", lambda *_args: None)
    state = {"candidate_boards": ["ulx3s_ecp5_45f"], "fpga": {"top_module": "spi_top", "rtl_files": [str(rtl)]}}

    mapping_agent.run_agent(state)

    mapping = state["fpga_explorer_io_mapping"]["mappings"][0]
    assert mapping["programming_ready"] is True
    assert mapping["unmapped_ports"] == []
    assert mapping["mapped_ports"] == ["clk", "reset_n", "spi_sclk", "spi_cs_n", "spi_mosi", "spi_miso", "fault_indicator"]


def test_gowin_mapping_reports_every_unmapped_top_level_port(tmp_path, monkeypatch):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top(input clk, input reset_n, input uart_rx, output uart_tx); endmodule\n",
        encoding="utf-8",
    )
    published = {}
    monkeypatch.setattr(mapping_agent, "publish_json", lambda _state, _agent, _subdir, _name, data: published.update(data))
    state = {
        "candidate_boards": ["gowin_tang_nano_9k"],
        "target_frequency_mhz": 30,
        "fpga": {"top_module": "top", "rtl_files": [str(rtl)]},
    }

    mapping_agent.run_agent(state)

    board = published["mappings"][0]
    assert board["mapped_ports"] == ["clk"]
    assert board["unmapped_ports"] == ["reset_n", "uart_rx", "uart_tx"]
    assert board["exploration_policy"] == "core_only"
    assert board["programming_ready"] is False


def test_gowin_explorer_synthesis_disables_iopads(tmp_path, monkeypatch):
    rtl = tmp_path / "top.v"
    rtl.write_text("module top(input clk, output uart_tx); assign uart_tx = clk; endmodule\n", encoding="utf-8")
    commands = []
    monkeypatch.setattr(explorer, "fpga_dir", lambda _state, *_parts: str(tmp_path))
    monkeypatch.setattr(explorer, "_yosys_help", lambda _cmd: "options: -noflatten -noiopads")
    monkeypatch.setattr(explorer, "_yosys_version", lambda: "test")
    monkeypatch.setattr(explorer, "run_cmd", lambda cmd, **_kwargs: commands.append(cmd) or {"ok": False, "cmd": cmd, "stderr_tail": "test"})
    monkeypatch.setattr(explorer, "_record_file", lambda *_args: None)
    state = {"fpga": {"rtl_files": [str(rtl)], "top_module": "top"}}

    result = explorer._run_synthesis(state, "gowin_tang_nano_9k", BOARD_REGISTRY["gowin_tang_nano_9k"], "baseline")

    script = Path(result["script"]).read_text(encoding="utf-8")
    assert "synth_gowin -top top -noflatten -noiopads" in script
    assert commands[0][0] == "yosys"


def test_failed_implementation_has_no_fake_resource_headroom():
    board = {"label": "Gowin", "family": "gowin", "resources": {"logic_cells": 8640}}
    result = explorer._summarize_board(
        "gowin", board, [{"status": "completed"}],
        [{"status": "failed", "error": "ERROR: Unconstrained IO:uart_tx_OBUF_O"}], 30,
    )

    assert result["status"] == "implementation_failed"
    assert result["failure_kind"] == "unconstrained_io"
    assert result["logic_utilization_percent"] is None
    assert result["resource_headroom_percent"] is None


def test_explorer_workflow_registers_io_mapping_agent():
    root = Path(__file__).parents[2]
    main = (root / "backend" / "main.py").read_text(encoding="utf-8")
    migration = (root / "backend" / "supabase" / "migrations" / "phase_20260729_fpga_explorer_io_mapping.sql").read_text(encoding="utf-8")
    dashboard = (root / "frontend" / "components" / "WorkflowEvidenceDashboard.tsx").read_text(encoding="utf-8")

    assert '"FPGA Explorer I/O Mapping Agent": fpga_explorer_io_mapping_agent' in main
    assert '"FPGA Explorer I/O Mapping Agent",\n    "FPGA Target Explorer Agent"' in main
    assert "FPGA Explorer I/O Mapping Agent" in migration
    assert "implementation failed" in dashboard
    assert "unmapped_ports" in dashboard

def test_io_mapping_enumerates_every_bus_bit(tmp_path, monkeypatch):
    rtl = tmp_path / "bus_top.v"
    rtl.write_text(
        "module bus_top(input clk, input [3:0] debug_addr, output [3:0] led); endmodule\n",
        encoding="utf-8",
    )
    published = {}
    monkeypatch.setattr(mapping_agent, "publish_json", lambda _state, _agent, _subdir, _name, data: published.update(data))
    mapping_agent.run_agent({
        "candidate_boards": ["gowin_tang_nano_9k"],
        "fpga": {"top_module": "bus_top", "rtl_files": [str(rtl)]},
    })

    assert published["top_level_ports"] == [
        "clk", "debug_addr[3]", "debug_addr[2]", "debug_addr[1]", "debug_addr[0]",
        "led[3]", "led[2]", "led[1]", "led[0]",
    ]
    board = published["mappings"][0]
    assert board["mapped_ports"] == ["clk", "led[3]", "led[2]", "led[1]", "led[0]"]
    assert board["unmapped_ports"] == ["debug_addr[3]", "debug_addr[2]", "debug_addr[1]", "debug_addr[0]"]


def test_core_only_netlist_removes_only_top_level_ports(tmp_path):
    netlist = tmp_path / "core.json"
    netlist.write_text(
        '{"modules":{"top":{"ports":{"clk":{"direction":"input","bits":[2]},'
        '"uart_tx":{"direction":"output","bits":[3]}},"cells":{"ff":{"type":"DFF"}},'
        '"netnames":{"clk":{"bits":[2]},"uart_tx":{"bits":[3]}}}}}',
        encoding="utf-8",
    )
    removed = explorer._make_core_only_netlist(str(netlist), "top")
    payload = __import__("json").loads(netlist.read_text(encoding="utf-8"))
    assert removed == ["clk", "uart_tx"]
    assert payload["modules"]["top"]["ports"] == {}
    assert payload["modules"]["top"]["cells"] == {"ff": {"type": "DFF"}}
    assert set(payload["modules"]["top"]["netnames"]) == {"clk", "uart_tx"}


def test_capacity_failure_is_classified_explicitly():
    result = explorer._summarize_board(
        "upduino_v3", BOARD_REGISTRY["upduino_v3"], [{"status": "completed"}],
        [{"status": "failed", "logic_cells_used": 5572, "logic_cells_available": 5280,
          "logic_utilization_percent": 105.53,
          "error": "ERROR: Unable to find a placement location for cell sensor_data[14]$sb_io"}], 15,
    )
    assert result["status"] == "implementation_failed"
    assert result["failure_kind"] == "capacity_exceeded"

def test_capacity_classification_covers_icestick_and_gowin_wording():
    cases = [
        ("icestick", BOARD_REGISTRY["icestick"], "Info: ICESTORM_LC: 5572/1280 435%\nERROR: Unable to place cell x, no BELs remaining"),
        ("gowin_tang_nano_9k", BOARD_REGISTRY["gowin_tang_nano_9k"], "Info: Pack IOBs...\nInfo: LUT4: 13545/8640 156%\nERROR: Unable to find legal placement for cell x, check constraints and utilisation"),
    ]
    for key, board, error in cases:
        result = explorer._summarize_board(key, board, [{"status": "completed"}], [{"status": "failed", "error": error}], 15)
        assert result["failure_kind"] == "capacity_exceeded"
        assert result["logic_utilization_percent"] > 100


def test_pack_iobs_phase_without_illegal_port_is_not_io_packing_failure():
    result = explorer._summarize_board(
        "gowin_tang_nano_9k", BOARD_REGISTRY["gowin_tang_nano_9k"], [{"status": "completed"}],
        [{"status": "failed", "error": "Info: Pack IOBs...\nERROR: router crashed"}], 15,
    )
    assert result["failure_kind"] == "implementation_failed"
