from .fpga_common import board_config, detect_top_module, fpga_dir, manifest_update, publish_json, resolve_rtl_sources, tool_status


def run_agent(state: dict) -> dict:
    agent = "FPGA RTL Handoff Ingest Agent"
    out_dir = fpga_dir(state, "handoff")
    sources = resolve_rtl_sources(state)
    top = state.get("top_module") or detect_top_module(sources)
    board = board_config(state)
    summary = {
        "agent": agent,
        "status": "ok" if sources and top and board.get("supported") else "blocked",
        "rtl_file_count": len(sources),
        "rtl_files": sources,
        "top_module": top,
        "target": board,
        "tools": tool_status(),
    }
    if not sources:
        summary["error"] = "No RTL sources found. Provide an upstream workflow ID, uploaded/pasted RTL, or repo path."
    elif not top:
        summary["error"] = "Top module could not be inferred. Provide top_module."
    elif not board.get("supported"):
        summary["error"] = board.get("unsupported_reason")
    publish_json(state, agent, "handoff", "fpga_handoff_ingest.json", summary)
    manifest_update(state, "rtl_files", sources)
    manifest_update(state, "top_module", top)
    manifest_update(state, "target", board)
    manifest_update(state, "handoff_ingest", summary)
    if summary["status"] != "ok":
        state["status"] = summary["error"]
    return state
