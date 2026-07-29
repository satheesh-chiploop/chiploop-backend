from agents.fpga.fpga_common import BOARD_REGISTRY
from agents.fpga.fpga_constraint_setup_agent import _constrained_cst_ports, _starter_cst
from agents.fpga.fpga_yosys_synthesis_agent import _architecture_synth_options


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


def test_new_architectures_use_board_specific_yosys_families_when_supported():
    help_with_family = "-family <device> supported values"
    assert _architecture_synth_options(BOARD_REGISTRY["gowin_tang_nano_20k"], help_with_family) == ["-family", "gw2a"]
    assert _architecture_synth_options(BOARD_REGISTRY["gowin_tang_primer_20k"], help_with_family) == ["-family", "gw2a"]
    assert _architecture_synth_options(BOARD_REGISTRY["certus_nx_versa_40"], help_with_family) == ["-family", "lfd2nx"]
    assert _architecture_synth_options(BOARD_REGISTRY["crosslink_nx_eval_40"], help_with_family) == ["-family", "lifcl"]
    assert _architecture_synth_options(BOARD_REGISTRY["gowin_tang_nano_20k"], "synth_gowin -top -json") == []
    assert BOARD_REGISTRY["certus_nx_versa_40"]["constraint_format"] == "pdc"
    assert BOARD_REGISTRY["crosslink_nx_eval_40"]["constraint_format"] == "pdc"
