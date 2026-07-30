from copy import deepcopy

from .fpga_common import BOARD_REGISTRY, publish_json
from .fpga_constraint_setup_agent import (
    _extract_port_bits_from_rtl, _starter_cst, _starter_lpf, _starter_pcf, _starter_pdc,
)


def _mapping_for_board(board_key: str, board: dict, top: str, ports: list[str], frequency: float) -> dict:
    fmt = str(board.get("constraint_format") or "pcf").lower()
    generator = {"pcf": _starter_pcf, "lpf": _starter_lpf, "pdc": _starter_pdc, "cst": _starter_cst}.get(fmt)
    constraint_text, mapped = generator(top, frequency, board_key, ports) if generator else ("", [])
    unmapped = [port for port in ports if port not in mapped]
    return {
        "board": board_key, "label": board.get("label") or board_key,
        "constraint_format": fmt, "mapped_ports": mapped, "unmapped_ports": unmapped,
        "all_ports_mapped": not unmapped, "programming_ready": not unmapped,
        "exploration_policy": "core_only" if unmapped else "board_mapped_io",
        "constraint_preview": constraint_text,
        "note": ("All top-level ports have verified ChipLoop board-pin mappings." if not unmapped else "Explorer will compare core capacity and timing without I/O pads. FPGA Prototyping will require verified pins for every unmapped port."),
    }


def run_agent(state: dict) -> dict:
    agent = "FPGA Explorer I/O Mapping Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    rtl_files = [str(path) for path in fpga.get("rtl_files") or []]
    top = str(fpga.get("top_module") or state.get("top_module") or "top")
    ports = _extract_port_bits_from_rtl(rtl_files, top)
    frequency = float(state.get("target_frequency_mhz") or 75.0)
    requested = state.get("candidate_boards") if isinstance(state.get("candidate_boards"), list) else []
    mappings = []
    for board_key in requested:
        if board_key not in BOARD_REGISTRY:
            continue
        board = deepcopy(BOARD_REGISTRY[board_key])
        if str(board.get("support_tier") or "").lower() == "unavailable":
            continue
        mappings.append(_mapping_for_board(board_key, board, top, ports, frequency))
    summary = {
        "agent": agent, "status": "completed", "top_module": top, "top_level_ports": ports,
        "board_count": len(mappings), "fully_mapped_board_count": sum(1 for item in mappings if item["all_ports_mapped"]),
        "mappings": mappings,
        "policy": "Never invent physical pins. Unmapped I/O is explicit; Explorer uses core-only implementation while Prototyping blocks until every physical I/O is verified.",
    }
    publish_json(state, agent, "target_explorer", "fpga_explorer_io_mapping.json", summary)
    state["fpga_explorer_io_mapping"] = summary
    return state