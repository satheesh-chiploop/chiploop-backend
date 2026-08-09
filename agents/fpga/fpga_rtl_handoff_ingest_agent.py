from .fpga_common import board_config, detect_top_module, fpga_dir, manifest_update, publish_json, resolve_rtl_sources, tool_status
from .fpga_serial_transport import add_spi_transport_if_needed


def run_agent(state: dict) -> dict:
    agent = "FPGA RTL Handoff Ingest Agent"
    out_dir = fpga_dir(state, "handoff")
    sources = resolve_rtl_sources(state)
    top = state.get("top_module") or detect_top_module(sources)
    board = board_config(state)
    # Establish the canonical FPGA handoff before applying an FPGA-only board
    # adapter. This keeps the verified core unchanged and makes the same
    # serialized top available to Explorer and implementation workflows.
    manifest_update(state, "rtl_files", sources)
    manifest_update(state, "top_module", top)
    manifest_update(state, "target", board)
    adapter = add_spi_transport_if_needed(state) if sources and top and board.get("supported") else None
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    effective_sources = [str(path) for path in fpga.get("rtl_files") or sources]
    effective_top = str(fpga.get("top_module") or state.get("top_module") or top)
    summary = {
        "agent": agent,
        "status": "ok" if sources and top and board.get("supported") else "blocked",
        "rtl_file_count": len(effective_sources),
        "rtl_files": effective_sources,
        "ignored_rtl_file_count": len(state.get("fpga_rtl_ignored_sources") or []),
        "ignored_rtl_files": state.get("fpga_rtl_ignored_sources") or [],
        "top_module": effective_top,
        "core_top_module": top,
        "interface_adapter": adapter,
        "target": board,
        "tools": tool_status(state),
    }
    if not sources:
        summary["error"] = "No RTL sources found. Provide an upstream workflow ID, uploaded/pasted RTL, or repo path."
    elif not top:
        summary["error"] = "Top module could not be inferred. Provide top_module."
    elif not board.get("supported"):
        summary["error"] = board.get("unsupported_reason")
    publish_json(state, agent, "handoff", "fpga_handoff_ingest.json", summary)
    manifest_update(state, "handoff_ingest", summary)
    if summary["status"] != "ok":
        state["status"] = summary["error"]
    return state
