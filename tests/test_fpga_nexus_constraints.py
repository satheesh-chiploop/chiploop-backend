import pytest

from agents.fpga.fpga_common import BOARD_REGISTRY
from agents.fpga.fpga_constraint_setup_agent import (
    _constrained_ports_from_text,
    _starter_cst,
    _starter_lpf,
    _starter_pcf,
    _starter_pdc,
)


def test_certus_nx_versa_starter_pdc_maps_default_clock_and_led() -> None:
    text, constrained = _starter_pdc(
        "pwm_fpga_demo", 15.0, "certus_nx_versa_40", ["clk", "led"]
    )

    assert constrained == ["clk", "led"]
    assert "ldc_set_location -site {G13} [get_ports {clk}]" in text
    assert "ldc_set_location -site {B3} [get_ports {led}]" in text
    assert "# target_frequency_mhz 15.0" in text
    assert _constrained_ports_from_text("pdc", text) == ["clk", "led"]


def test_crosslink_nx_eval_starter_pdc_maps_default_clock_and_led() -> None:
    text, constrained = _starter_pdc(
        "pwm_fpga_demo", 12.0, "crosslink_nx_eval_40", ["clk", "led"]
    )

    assert constrained == ["clk", "led"]
    assert "ldc_set_location -site {L13} [get_ports {clk}]" in text
    assert "ldc_set_location -site {E17} [get_ports {led}]" in text
    assert _constrained_ports_from_text("pdc", text) == ["clk", "led"]


RUNNABLE_PHYSICAL_BOARDS = (
    "icebreaker",
    "upduino_v3",
    "icestick",
    "ice40_hx8k_breakout",
    "ulx3s_ecp5_45f",
    "orangecrab_ecp5_85f",
    "colorlight_5a_75b",
    "certus_nx_versa_40",
    "crosslink_nx_eval_40",
    "gowin_tang_nano_9k",
    "gowin_tang_nano_20k",
    "gowin_tang_primer_20k",
)


@pytest.mark.parametrize("board_key", RUNNABLE_PHYSICAL_BOARDS)
def test_every_selectable_physical_board_maps_reference_clk_and_led(board_key: str) -> None:
    board = BOARD_REGISTRY[board_key]
    generator = {
        "pcf": _starter_pcf,
        "lpf": _starter_lpf,
        "pdc": _starter_pdc,
        "cst": _starter_cst,
    }[board["constraint_format"]]

    _, constrained = generator(
        "pwm_fpga_demo",
        float(board["default_frequency_mhz"]),
        board_key,
        ["clk", "led"],
    )

    assert constrained == ["clk", "led"]
