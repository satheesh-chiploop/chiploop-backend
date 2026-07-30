from pathlib import Path

from agents.fpga import fpga_explorer_io_mapping_agent as mapping_agent
from agents.fpga import fpga_target_explorer_agent as explorer
from agents.fpga.fpga_common import BOARD_REGISTRY


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