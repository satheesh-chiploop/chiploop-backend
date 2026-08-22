from copy import deepcopy

from .fpga_common import BOARD_REGISTRY, publish_json
from .fpga_constraint_setup_agent import (
    _extract_port_bits_from_rtl, _starter_cst, _starter_lpf, _starter_pcf, _starter_pdc,
)
from .fpga_serial_transport import add_spi_transport_if_needed


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
    deployment = str(state.get("deployment_architecture") or "automatic").strip().lower()
    interface_plan = state.get("host_interface_plan") if isinstance(state.get("host_interface_plan"), dict) else {}
    requested_boards = state.get("candidate_boards") if isinstance(state.get("candidate_boards"), list) else []
    onboard_spi_boards = [
        board_key for board_key in requested_boards
        if str((((BOARD_REGISTRY.get(str(board_key)) or {}).get("compute_host") or {}).get("fabric_interface") or {}).get("protocol") or "").startswith("spi")
    ]
    onboard_spi_contract = deployment == "fpga_onboard_cpu" and bool(onboard_spi_boards)
    transport_allowed = deployment not in {"fpga_onboard_cpu", "fpga_soft_cpu"} or onboard_spi_contract
    if deployment == "fpga_external_host" and str(interface_plan.get("protocol") or "").lower() != "spi":
        raise RuntimeError("External-host FPGA integration requires the qualified SPI interface plan before exploration.")
    # An explicitly selected external host always needs the promised SPI
    # endpoint, even when the native core is narrow enough for board headers.
    handoff = fpga.get("handoff_ingest") if isinstance(fpga.get("handoff_ingest"), dict) else {}
    existing_adapter = handoff.get("interface_adapter") if isinstance(handoff.get("interface_adapter"), dict) else None
    adapter = existing_adapter or (
        add_spi_transport_if_needed(
            state,
            force_for_board_mapping=deployment == "fpga_external_host" or onboard_spi_contract,
        )
        if transport_allowed else None
    )
    if adapter and adapter.get("status") == "generated":
        fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
        rtl_files = [str(path) for path in fpga.get("rtl_files") or []]
        top = str(fpga.get("top_module") or state.get("top_module") or top)
    frequency = float(state.get("target_frequency_mhz") or 75.0)
    requested = state.get("candidate_boards") if isinstance(state.get("candidate_boards"), list) else []

    def build_mappings() -> tuple[list[str], list[dict]]:
        current_ports = _extract_port_bits_from_rtl(rtl_files, top)
        current_mappings = []
        for board_key in requested:
            if board_key not in BOARD_REGISTRY:
                continue
            board = deepcopy(BOARD_REGISTRY[board_key])
            if str(board.get("support_tier") or "").lower() == "unavailable":
                continue
            current_mappings.append(
                _mapping_for_board(board_key, board, top, current_ports, frequency)
            )
        return current_ports, current_mappings

    ports, mappings = build_mappings()
    # Width alone is not a sufficient deployability test. A modest parallel
    # interface can still have no complete verified mapping on any candidate
    # board. In automatic mode, preserve the verified core and add the standard
    # FPGA-only SPI shell, then evaluate the actual adapted interface.
    if (
        transport_allowed
        and adapter is None
        and not onboard_spi_contract
        and mappings
        and not any(item["all_ports_mapped"] for item in mappings)
        and state.get("auto_serialize_wide_io") is not False
    ):
        adapter = add_spi_transport_if_needed(state, force_for_board_mapping=True)
        if adapter and adapter.get("status") == "generated":
            fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
            rtl_files = [str(path) for path in fpga.get("rtl_files") or []]
            top = str(fpga.get("top_module") or state.get("top_module") or top)
            ports, mappings = build_mappings()
    summary = {
        "agent": agent, "status": "completed", "top_module": top, "top_level_ports": ports,
        "interface_adapter": adapter,
        "deployment_architecture": deployment,
        "host_interface_plan": interface_plan,
        "onboard_spi_contract_boards": onboard_spi_boards,
        "board_count": len(mappings), "fully_mapped_board_count": sum(1 for item in mappings if item["all_ports_mapped"]),
        "mappings": mappings,
        "policy": "Never invent physical pins. Unmapped I/O is explicit; Explorer uses core-only implementation while Prototyping blocks until every physical I/O is verified.",
    }
    publish_json(state, agent, "target_explorer", "fpga_explorer_io_mapping.json", summary)
    state["fpga_explorer_io_mapping"] = summary
    return state
