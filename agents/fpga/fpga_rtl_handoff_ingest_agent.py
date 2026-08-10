import hashlib
from pathlib import Path

from .fpga_common import board_config, detect_top_module, fpga_dir, manifest_update, publish_json, resolve_rtl_sources, tool_status
from .fpga_serial_transport import add_spi_transport_if_needed


def _save_rtl_artifact(workflow_id: str, agent: str, filename: str, content: str):
    from utils.artifact_utils import save_text_artifact_and_record

    return save_text_artifact_and_record(
        workflow_id, agent, "fpga/handoff/rtl", filename, content
    )


def _publish_rtl_package(state: dict, agent: str, sources: list[str]) -> dict:
    """Persist the exact FPGA RTL handoff in Supabase for child workflows."""
    workflow_id = str(state.get("workflow_id") or "").strip()
    result = {"status": "not_required", "expected_count": len(sources), "published_count": 0, "artifacts": []}
    if not workflow_id:
        return result

    used_names: set[str] = set()
    for index, source in enumerate(sources):
        path = Path(source)
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            result["artifacts"].append({"source": str(path), "status": "failed", "error": str(exc)})
            continue
        filename = path.name or f"source_{index}.sv"
        key = filename.lower()
        if key in used_names:
            digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
            filename = f"{path.stem}_{digest}{path.suffix or '.sv'}"
            key = filename.lower()
        used_names.add(key)
        try:
            storage_path = _save_rtl_artifact(workflow_id, agent, filename, content)
        except Exception as exc:
            storage_path = None
            result["artifacts"].append({
                "source": str(path), "filename": filename,
                "storage_path": None, "status": "failed", "error": str(exc),
            })
            continue
        durable = isinstance(storage_path, str) and storage_path.replace("\\", "/").startswith(
            f"backend/workflows/{workflow_id}/fpga/handoff/rtl/"
        )
        result["artifacts"].append({
            "source": str(path), "filename": filename,
            "storage_path": storage_path, "status": "published" if durable else "failed",
        })
        if durable:
            result["published_count"] += 1

    result["status"] = "published" if result["published_count"] == result["expected_count"] else "failed"
    if result["status"] == "failed":
        result["error"] = "The complete FPGA RTL handoff could not be persisted to Supabase Storage."
    return result


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
    rtl_package = _publish_rtl_package(state, agent, effective_sources)
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
        "rtl_package": rtl_package,
        "target": board,
        "tools": tool_status(state),
    }
    if not sources:
        summary["error"] = "No RTL sources found. Provide an upstream workflow ID, uploaded/pasted RTL, or repo path."
    elif not top:
        summary["error"] = "Top module could not be inferred. Provide top_module."
    elif not board.get("supported"):
        summary["error"] = board.get("unsupported_reason")
    elif rtl_package.get("status") == "failed":
        summary["status"] = "blocked"
        summary["error"] = rtl_package.get("error")
    publish_json(state, agent, "handoff", "fpga_handoff_ingest.json", summary)
    manifest_update(state, "handoff_ingest", summary)
    if summary["status"] != "ok":
        state["status"] = summary["error"]
    return state
