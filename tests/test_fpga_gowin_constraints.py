from agents.fpga.fpga_common import BOARD_REGISTRY
from agents.fpga.fpga_constraint_setup_agent import _constrained_cst_ports, _starter_cst
from agents.fpga.fpga_yosys_synthesis_agent import _architecture_synth_options
from agents.fpga import fpga_constraint_setup_agent as constraint_setup


def test_tang_nano_9k_generates_verified_clock_and_led_constraints():
    text, constrained = _starter_cst(
        "pwm_fpga_demo",
        75.0,
        "gowin_tang_nano_9k",
        ["clk", "led"],
    )

    assert 'IO_LOC "clk" 52;' in text
    assert 'IO_LOC "led" 10;' in text
    assert 'IO_PORT "clk" IO_TYPE=LVCMOS33;' in text
    assert 'IO_PORT "led" IO_TYPE=LVCMOS33;' in text
    assert constrained == ["clk", "led"]
    assert _constrained_cst_ports(text) == ["clk", "led"]


def test_ulx3s_esp32_variant_uses_onboard_shared_spi_traces():
    from agents.fpga.fpga_constraint_setup_agent import _starter_lpf

    text, constrained = _starter_lpf(
        "top_spi_fpga_top", 25.0, "ulx3s_ecp5_45f_esp32",
        ["clk", "spi_sclk", "spi_cs_n", "spi_mosi", "spi_miso", "led"],
    )
    assert constrained == ["clk", "spi_sclk", "spi_cs_n", "spi_mosi", "spi_miso", "led"]
    for port, pin in {"spi_sclk": "H2", "spi_cs_n": "K2", "spi_mosi": "J1", "spi_miso": "J3"}.items():
        assert f'LOCATE COMP "{port}" SITE "{pin}";' in text


def test_ulx3s_esp32_constraint_setup_times_board_spi_clock(tmp_path, monkeypatch):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        "module top(input clk,input reset_n,input spi_sclk,input spi_cs_n,input spi_mosi,output spi_miso,output led); endmodule\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(constraint_setup, "publish_json", lambda *_args: None)
    state = {
        "workflow_dir": str(tmp_path), "board": "ulx3s_ecp5_45f_esp32",
        "fpga": {"top_module": "top", "rtl_files": [str(rtl)]},
    }
    constraint_setup.run_agent(state)
    text = (tmp_path / "fpga" / "constraints" / "top.lpf").read_text(encoding="utf-8")
    assert 'FREQUENCY PORT "spi_sclk" 10 MHz;' in text
    assert state["fpga"]["constraints"]["clock_constraints_mhz"]["spi_sclk"] == 10.0


def test_tang_nano_9k_uses_qualified_apicula_and_himbaechel_arguments():
    board = BOARD_REGISTRY["gowin_tang_nano_9k"]

    assert board["apicula_family"] == "GW1N-9C"
    assert board["nextpnr_tool"] == "nextpnr-himbaechel"
    assert "family=GW1N-9C" in board["nextpnr_device_args"]


def test_tang_nano_20k_generates_upstream_clock_and_led_constraints():
    text, constrained = _starter_cst(
        "pwm_fpga_demo", 75.0, "gowin_tang_nano_20k", ["clk", "led"],
    )
    assert 'IO_LOC "clk" 4;' in text
    assert 'IO_LOC "led" 15;' in text
    assert constrained == ["clk", "led"]


def test_tang_primer_20k_generates_verified_dock_clock_and_led_constraints():
    text, constrained = _starter_cst(
        "pwm_fpga_demo", 16.0, "gowin_tang_primer_20k", ["clk", "led"],
    )
    assert 'IO_LOC "clk" H11;' in text
    assert 'IO_LOC "led" N16;' in text
    assert constrained == ["clk", "led"]
    assert _constrained_cst_ports(text) == ["clk", "led"]
    assert BOARD_REGISTRY["gowin_tang_primer_20k"]["default_frequency_mhz"] == 27.0

def test_new_architectures_use_board_specific_yosys_families_when_supported():
    help_with_family = "-family <device> supported values"
    assert _architecture_synth_options(BOARD_REGISTRY["gowin_tang_nano_20k"], help_with_family) == []
    assert _architecture_synth_options(BOARD_REGISTRY["gowin_tang_primer_20k"], help_with_family) == []
    assert _architecture_synth_options(BOARD_REGISTRY["certus_nx_versa_40"], help_with_family) == ["-family", "lfd2nx"]
    assert _architecture_synth_options(BOARD_REGISTRY["crosslink_nx_eval_40"], help_with_family) == ["-family", "lifcl"]
    assert _architecture_synth_options(BOARD_REGISTRY["gowin_tang_nano_20k"], "synth_gowin -top -json") == []
    assert BOARD_REGISTRY["certus_nx_versa_40"]["constraint_format"] == "pdc"
    assert BOARD_REGISTRY["crosslink_nx_eval_40"]["constraint_format"] == "pdc"
