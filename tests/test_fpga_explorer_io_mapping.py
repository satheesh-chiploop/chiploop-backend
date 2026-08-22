from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import pytest

from agents.fpga import fpga_explorer_io_mapping_agent as mapping_agent
from agents.fpga import fpga_target_explorer_agent as explorer
from agents.fpga.fpga_common import BOARD_REGISTRY, _upstream_rtl_priority
from agents.fpga import fpga_rtl_handoff_ingest_agent as handoff_agent
from agents.fpga import fpga_serial_transport


def test_supabase_fpga_handoff_rtl_is_preferred_over_derived_outputs():
    packaged = "backend/workflows/wf/fpga/handoff/rtl/core.sv"
    assert _upstream_rtl_priority(packaged) == 0
    assert _upstream_rtl_priority(
        "backend/workflows/wf/fpga/target_explorer/interface_adapter/core_spi_fpga_top.sv"
    ) == 0
    assert _upstream_rtl_priority("backend/workflows/wf/fpga/build/core.sv") is None


def test_handoff_publishes_complete_rtl_package_with_collision_safe_names(tmp_path, monkeypatch):
    first = tmp_path / "core_a" / "core.sv"
    second = tmp_path / "core_b" / "core.sv"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("module core_a; endmodule\n", encoding="utf-8")
    second.write_text("module core_b; endmodule\n", encoding="utf-8")
    uploads = []
    monkeypatch.setattr(
        handoff_agent, "_save_rtl_artifact",
        lambda workflow_id, agent, filename, content: uploads.append(
            (workflow_id, agent, "fpga/handoff/rtl", filename, content)
        ) or f"backend/workflows/{workflow_id}/fpga/handoff/rtl/{filename}",
    )

    report = handoff_agent._publish_rtl_package(
        {"workflow_id": "wf"}, "FPGA RTL Handoff Ingest Agent", [str(first), str(second)]
    )

    assert report["status"] == "published"
    assert report["published_count"] == 2
    assert {item[2] for item in uploads} == {"fpga/handoff/rtl"}
    assert len({item[3].lower() for item in uploads}) == 2
    assert {item[4] for item in uploads} == {
        "module core_a; endmodule\n", "module core_b; endmodule\n"
    }


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
    assert "!spi_cs_n ? (spi_active ? tx_shift[FRAME_BITS-1] : tx_snapshot[FRAME_BITS-1]) : 1'bz" in wrapper
    assert "adaptive_aero_control_top u_core" in wrapper
    assert ".s_axis_cmd(core_s_axis_cmd)" in wrapper
    protocol = json.loads(Path(report["protocol_contract"]).read_text(encoding="utf-8"))
    assert protocol["schema"] == "chiploop.fpga.spi_transport.v1"

    assert protocol["mode"] == 0
    assert protocol["response_latency_frames"] == 2
    assert protocol["minimum_interframe_delay_us"] == 1
    assert protocol["frame_bits"] == 136
    assert protocol["frame_bytes"] == 17
    assert protocol["command_leading_padding_bits"] == 7
    assert protocol["response_trailing_padding_bits"] == 7
    assert "localparam integer FRAME_BITS = 136;" in wrapper
    assert "Bundled-data CDC" in wrapper
    assert report["protocol_contract_ready"] is True
    assert report["host_driver_ready"] is True
    spec = importlib.util.spec_from_file_location("generated_chiploop_spi_driver", report["host_driver"])
    assert spec and spec.loader
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    command = driver.pack_command({"s_axis_cmd": 0x1234, "s_axis_valid": 1})
    assert len(command) == protocol["frame_bytes"]
    response_value = (0x55 << 1) | 1
    response = driver.unpack_response((response_value << 7).to_bytes(17, "big"))
    assert response == {"m_axis_resp": 0x55, "fault_flag": 1}
    with pytest.raises(ValueError, match="exceeds"):
        driver.pack_command({"s_axis_valid": 2})
    if shutil.which("iverilog"):
        completed = subprocess.run(
            ["iverilog", "-g2012", "-s", state["fpga"]["top_module"], "-o", str(tmp_path / "wrapper.out"), str(rtl), report["wrapper_rtl"]],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    if shutil.which("yosys"):
        script = tmp_path / "wrapper_synth.ys"
        script.write_text(
            f"read_verilog -sv {rtl} {report['wrapper_rtl']}\n"
            f"synth_ecp5 -top {state['fpga']['top_module']} -json {tmp_path / 'wrapper.json'}\n",
            encoding="utf-8",
        )
        completed = subprocess.run(["yosys", "-s", str(script)], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr + completed.stdout[-2000:]


@pytest.mark.parametrize("deployment", ["fpga_onboard_cpu", "fpga_soft_cpu"])
def test_cpu_integrated_modes_never_receive_automatic_spi_shell(tmp_path, monkeypatch, deployment):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top(input clk, input rst_n, output done); assign done=rst_n; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(mapping_agent, "add_spi_transport_if_needed", lambda *_args, **_kwargs: pytest.fail("CPU-integrated modes must not invent SPI"))
    monkeypatch.setattr(mapping_agent, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "deployment_architecture": deployment,
        "candidate_boards": ["ulx3s_ecp5_45f"],
        "fpga": {"top_module": "top", "rtl_files": [str(rtl)]},
    }

    mapping_agent.run_agent(state)

    assert state["fpga_explorer_io_mapping"]["interface_adapter"] is None
    assert state["fpga_explorer_io_mapping"]["deployment_architecture"] == deployment


def test_external_host_forces_contracted_spi_endpoint_for_narrow_core(tmp_path, monkeypatch):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top(input clk, input rst_n, output done); assign done=rst_n; endmodule\n", encoding="utf-8")
    calls = []

    def fake_adapter(_state, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(mapping_agent, "add_spi_transport_if_needed", fake_adapter)
    monkeypatch.setattr(mapping_agent, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "deployment_architecture": "fpga_external_host",
        "host_interface_plan": {
            "protocol": "spi", "role": "fpga_peripheral", "clock_mhz": 10,
            "data_width_bits": 8, "framing": "register_command_response",
            "flow_control": "chip_select_and_status", "interrupt_signaling": "optional_gpio",
            "register_access": "addressed_read_write",
        },
        "candidate_boards": ["ulx3s_ecp5_45f"],
        "fpga": {"top_module": "top", "rtl_files": [str(rtl)]},
    }

    mapping_agent.run_agent(state)

    assert calls[0]["force_for_board_mapping"] is True


def test_ulx3s_onboard_esp32_forces_board_contracted_spi_endpoint(tmp_path, monkeypatch):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top(input clk, input reset_n, output done); assign done=reset_n; endmodule\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(mapping_agent, "add_spi_transport_if_needed", lambda _state, **kwargs: calls.append(kwargs) or None)
    monkeypatch.setattr(mapping_agent, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "deployment_architecture": "fpga_onboard_cpu",
        "candidate_boards": ["ulx3s_ecp5_45f_esp32"],
        "fpga": {"top_module": "top", "rtl_files": [str(rtl)]},
    }

    mapping_agent.run_agent(state)

    assert calls == [{"force_for_board_mapping": True}]
    assert state["fpga_explorer_io_mapping"]["onboard_spi_contract_boards"] == ["ulx3s_ecp5_45f_esp32"]


def test_io_mapping_reuses_handoff_spi_adapter_without_double_wrapping(tmp_path, monkeypatch):
    wrapper = tmp_path / "top_spi_fpga_top.sv"
    wrapper.write_text(
        "module top_spi_fpga_top(input clk,input reset_n,input spi_sclk,input spi_cs_n,input spi_mosi,output spi_miso,output fault_indicator); endmodule\n",
        encoding="utf-8",
    )
    existing = {
        "status": "generated", "fpga_top_module": "top_spi_fpga_top",
        "protocol_contract_ready": True, "host_driver_ready": True,
    }
    monkeypatch.setattr(mapping_agent, "add_spi_transport_if_needed", lambda *_args, **_kwargs: pytest.fail("must reuse existing adapter"))
    monkeypatch.setattr(mapping_agent, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "deployment_architecture": "fpga_onboard_cpu",
        "candidate_boards": ["ulx3s_ecp5_45f_esp32"],
        "fpga": {
            "top_module": "top_spi_fpga_top", "rtl_files": [str(wrapper)],
            "handoff_ingest": {"interface_adapter": existing},
        },
    }

    mapping_agent.run_agent(state)

    assert state["fpga"]["top_module"] == "top_spi_fpga_top"
    assert state["fpga_explorer_io_mapping"]["interface_adapter"] is existing


def test_onboard_cpu_rejects_incompatible_candidate_spi_contracts(tmp_path, monkeypatch):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top(input clk, input reset_n, output done); assign done=reset_n; endmodule\n", encoding="utf-8")
    monkeypatch.setitem(mapping_agent.BOARD_REGISTRY, "incompatible_spi_board", {
        "compute_host": {"fabric_interface": {
            "protocol": "spi_register", "mode": 1, "frame_order": "msb_first", "maximum_clock_mhz": 5,
        }}
    })
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "deployment_architecture": "fpga_onboard_cpu",
        "candidate_boards": ["ulx3s_ecp5_45f_esp32", "incompatible_spi_board"],
        "fpga": {"top_module": "top", "rtl_files": [str(rtl)]},
    }

    with pytest.raises(RuntimeError, match="incompatible SPI transport contracts"):
        mapping_agent.run_agent(state)



def test_non_ansi_reg_prefixed_ports_are_serialized_and_connected(tmp_path, monkeypatch):
    rtl = tmp_path / "legacy_control.v"
    rtl.write_text(
        "module legacy_control(clk, reset, reg_cs, reg_we, reg_re, payload, result);\n"
        "input clk; input reset; input reg_cs; input reg_we; input reg_re;\n"
        "input [127:0] payload; output [127:0] result; assign result = payload; endmodule\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fpga_serial_transport, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "fpga": {"top_module": "legacy_control", "rtl_files": [str(rtl)]},
    }
    report = fpga_serial_transport.add_spi_transport_if_needed(state)
    wrapper = Path(report["wrapper_rtl"]).read_text(encoding="utf-8")
    assert {item["port"] for item in report["input_bit_map"]} >= {"reg_cs", "reg_we", "reg_re"}
    assert ".reg_cs(core_reg_cs)" in wrapper
    assert ".reg_we(core_reg_we)" in wrapper
    assert ".reg_re(core_reg_re)" in wrapper


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


def test_native_interface_without_any_complete_board_map_gets_spi_adapter(tmp_path, monkeypatch):
    rtl = tmp_path / "adaptive_aero_control_top.v"
    rtl.write_text(
        "module adaptive_aero_control_top(input clk,input reset_n,"
        "input [15:0] safe_cmd_value,input [1:0] safe_cmd_mode,input safe_cmd_valid,"
        "output act_cmd_valid,output act_cmd_enable,output [15:0] act_cmd_value,"
        "output [1:0] act_cmd_mode); assign act_cmd_valid=safe_cmd_valid;"
        "assign act_cmd_enable=safe_cmd_valid; assign act_cmd_value=safe_cmd_value;"
        "assign act_cmd_mode=safe_cmd_mode; endmodule\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mapping_agent, "publish_json", lambda *_args: None)
    monkeypatch.setattr(fpga_serial_transport, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "candidate_boards": ["ulx3s_ecp5_45f", "orangecrab_ecp5_85f"],
        "fpga": {"top_module": "adaptive_aero_control_top", "rtl_files": [str(rtl)]},
    }

    mapping_agent.run_agent(state)

    summary = state["fpga_explorer_io_mapping"]
    assert summary["interface_adapter"]["status"] == "generated"
    assert summary["interface_adapter"]["generation_reason"] == (
        "no_candidate_board_had_a_complete_verified_native_pin_map"
    )
    assert summary["interface_adapter"]["original_top_level_io_bits"] == 41
    assert summary["top_module"] == "adaptive_aero_control_top_spi_fpga_top"
    assert summary["fully_mapped_board_count"] >= 1
    ulx3s = next(item for item in summary["mappings"] if item["board"] == "ulx3s_ecp5_45f")
    assert ulx3s["programming_ready"] is True
    assert ulx3s["unmapped_ports"] == []


def test_board_mapping_spi_adapter_honors_explicit_opt_out(tmp_path, monkeypatch):
    rtl = tmp_path / "parallel_top.v"
    rtl.write_text(
        "module parallel_top(input clk,input reset_n,input [15:0] command,output [15:0] response);"
        "assign response=command; endmodule\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mapping_agent, "publish_json", lambda *_args: None)
    state = {
        "candidate_boards": ["ulx3s_ecp5_45f"],
        "auto_serialize_wide_io": False,
        "fpga": {"top_module": "parallel_top", "rtl_files": [str(rtl)]},
    }

    mapping_agent.run_agent(state)

    summary = state["fpga_explorer_io_mapping"]
    assert summary["interface_adapter"] is None
    assert summary["fully_mapped_board_count"] == 0
    assert summary["mappings"][0]["programming_ready"] is False


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
        "auto_serialize_wide_io": False,
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
        "auto_serialize_wide_io": False,
        "fpga": {"top_module": "bus_top", "rtl_files": [str(rtl)]},
    })

    assert published["top_level_ports"] == [
        "clk", "debug_addr[3]", "debug_addr[2]", "debug_addr[1]", "debug_addr[0]",
        "led[3]", "led[2]", "led[1]", "led[0]",
    ]
    board = published["mappings"][0]
    assert board["mapped_ports"] == ["clk", "led[3]", "led[2]", "led[1]", "led[0]"]
    assert board["unmapped_ports"] == ["debug_addr[3]", "debug_addr[2]", "debug_addr[1]", "debug_addr[0]"]


def test_non_ansi_wide_top_generates_spi_adapter_and_counts_bus_bits(tmp_path, monkeypatch):
    rtl = tmp_path / "legacy_top.v"
    rtl.write_text(
        """module legacy_top(clk, rst_n, cfg_wdata, req_data, status);
input clk;
input rst_n;
input [31:0] cfg_wdata;
output [127:0] req_data;
output [31:0] status;
assign req_data = {4{cfg_wdata}};
assign status = cfg_wdata;
endmodule
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(fpga_serial_transport, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "fpga": {"top_module": "legacy_top", "rtl_files": [str(rtl)]},
    }

    report = fpga_serial_transport.add_spi_transport_if_needed(state)
    original_bits = mapping_agent._extract_port_bits_from_rtl([str(rtl)], "legacy_top")

    assert len(original_bits) == 194
    assert "cfg_wdata[31]" in original_bits
    assert "req_data[127]" in original_bits
    assert report["status"] == "generated"
    assert report["original_top_level_io_bits"] == 194
    assert state["fpga"]["top_module"] == "legacy_top_spi_fpga_top"


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
