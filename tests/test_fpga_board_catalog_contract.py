from agents.fpga.fpga_common import BOARD_REGISTRY
from agents.fpga.fpga_explorer_io_mapping_agent import _mapping_for_board
from agents.fpga.fpga_target_explorer_agent import CANDIDATE_BOARDS


RUNNABLE = [
    key for key in CANDIDATE_BOARDS
    if str(BOARD_REGISTRY[key].get("support_tier") or "production").lower() != "unavailable"
]


def test_every_runnable_board_has_complete_open_source_tool_contract():
    for key in RUNNABLE:
        board = BOARD_REGISTRY[key]
        assert board["label"].lower().startswith(("lattice ", "gowin "))
        assert board["family"] in {"ice40", "ecp5", "nexus", "gowin"}
        assert board["device"] and board["package"]
        assert board["constraint_format"] in {"pcf", "lpf", "pdc", "cst"}
        assert float(board["default_frequency_mhz"]) > 0
        assert int(board["resources"]["logic_cells"]) > 0
        assert board.get("programmer_board") or board.get("programming_note")

        if board["family"] in {"nexus", "gowin"}:
            assert board.get("nextpnr_tool")
            assert board.get("nextpnr_device_args")
            assert board.get("bitstream_tool")
            assert board.get("pnr_output_ext")
            assert board.get("bitstream_ext")
        else:
            assert board.get("nextpnr_device_flag")
            assert board.get("nextpnr_package")


def test_pwm_reference_io_is_verified_for_every_runnable_board():
    for key in RUNNABLE:
        mapping = _mapping_for_board(
            key, BOARD_REGISTRY[key], "pwm_fpga_demo", ["clk", "led"], 25.0
        )
        assert mapping["all_ports_mapped"], f"{key}: {mapping['unmapped_ports']}"
        assert mapping["mapped_ports"] == ["clk", "led"]
        assert mapping["constraint_preview"].strip()

