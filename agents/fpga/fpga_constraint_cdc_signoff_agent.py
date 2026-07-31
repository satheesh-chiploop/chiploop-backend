import json
import os
import re

from .fpga_common import manifest_update, publish_json


AGENT_NAME = "FPGA Constraint and CDC/RDC Signoff Agent"


def _rtl_text(paths: list[str]) -> str:
    chunks = []
    for path in paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                chunks.append(handle.read())
    return "\n".join(chunks)


def _findings(path: str, source: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
        return [{**item, "source": source} for item in report.get("findings") or [] if isinstance(item, dict)]
    except Exception:
        return []

def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    constraints = fpga.get("constraints") if isinstance(fpga.get("constraints"), dict) else {}
    enabled = bool(state.get("run_fpga_constraint_signoff", True))
    required = bool(state.get("require_fpga_constraint_signoff", True))
    rtl_files = [str(path) for path in fpga.get("rtl_files") or []]
    rtl = _rtl_text(rtl_files)
    sequential_clocks = sorted(set(re.findall(r"always(?:_ff)?\s*@?\s*\(\s*posedge\s+([A-Za-z_]\w*)", rtl)))
    sequential_clocks += sorted(set(re.findall(r"always(?:_ff)?\s*@?\s*\(\s*negedge\s+([A-Za-z_]\w*)", rtl)))
    sequential_clocks = sorted(set(sequential_clocks))
    async_resets = sorted(set(re.findall(r"or\s+(?:posedge|negedge)\s+([A-Za-z_]\w*)", rtl)))
    unconstrained_ports = list(constraints.get("unconstrained_ports") or [])
    target_frequency = constraints.get("target_frequency_mhz") or state.get("target_frequency_mhz")
    workflow_root = str(state.get("workflow_dir") or "")
    crossings = list(state.get("fpga_cdc_findings") or [])
    crossings.extend(_findings(str(state.get("cdc_report_path") or os.path.join(workflow_root, "digital", "cdc_findings.json")), "cdc"))
    crossings.extend(_findings(str(state.get("reset_integrity_report_path") or os.path.join(workflow_root, "digital", "reset_integrity_findings.json")), "rdc"))
    unsafe_crossings = [item for item in crossings if str((item or {}).get("severity", "")).lower() in {"error", "critical", "unsafe"}]
    warnings = []
    if len(sequential_clocks) > 1 and not crossings:
        warnings.append("Multiple RTL clock signals were detected; provide CDC classifications or run structural CDC analysis.")
    warnings.extend(str(item.get("msg") or item.get("message") or item.get("type")) for item in crossings if str(item.get("severity", "")).lower() == "warning")
    if async_resets and not state.get("fpga_rdc_reviewed"):
        warnings.append("Asynchronous reset usage was detected; reset release synchronization requires review.")
    errors = []
    if unconstrained_ports:
        errors.append(f"{len(unconstrained_ports)} top-level ports are unconstrained.")
    if sequential_clocks and not target_frequency:
        errors.append("No implementation clock frequency is defined.")
    if unsafe_crossings:
        errors.append(f"{len(unsafe_crossings)} unsafe CDC/RDC crossings were reported.")
    status = "disabled" if not enabled else "fail" if errors else "review" if warnings else "pass"
    summary = {
        "agent": AGENT_NAME,
        "status": status,
        "enabled": enabled,
        "required": required,
        "tool": "ChipLoop structural RTL/constraint analysis",
        "constraint_format": constraints.get("constraint_format"),
        "constraint_path": constraints.get("constraint_path"),
        "target_frequency_mhz": target_frequency,
        "rtl_file_count": len(rtl_files),
        "detected_clocks": sequential_clocks,
        "detected_async_resets": async_resets,
        "unconstrained_ports": unconstrained_ports,
        "cdc_rdc_findings": crossings,
        "unsafe_crossing_count": len(unsafe_crossings),
        "warnings": warnings,
        "errors": errors,
        "signoff": {
            "all_top_level_ports_constrained": not unconstrained_ports,
            "all_clocks_defined": bool(target_frequency) or not sequential_clocks,
            "cdc_rdc_review_complete": not warnings and not unsafe_crossings,
        },
    }
    if not enabled:
        summary["reason"] = "Constraint and CDC/RDC signoff disabled by user."
    publish_json(state, AGENT_NAME, "signoff", "fpga_constraint_cdc_signoff_summary.json", summary)
    manifest_update(state, "constraint_cdc_signoff", summary)
    state["fpga_constraint_cdc_signoff"] = summary
    if enabled and required and status == "fail":
        raise RuntimeError(f"FPGA constraint/CDC signoff failed: {' '.join(errors)}")
    return state
