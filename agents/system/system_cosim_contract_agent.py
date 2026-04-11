"""
System CoSim Contract Agent
Production-oriented cross-layer contract validation for System Software -> Firmware -> RTL.

Validates:
- presence/readiness of prior L2 ingest manifest
- L1 software validation status when available
- register map linkage
- interrupt consistency
- DMA expectation consistency
- software entry / firmware ELF / RTL top readiness
Produces:
- structured contract report
- issues / warnings / passes
"""

import datetime
import json
from typing import Any, Dict, List

AGENT_NAME = "System CoSim Contract Agent"
OUTPUT_SUBDIR = "system/validation/l2/contract"

REPORT_JSON = "system_cosim_contract_report.json"
SUMMARY_MD = "system_cosim_contract_summary.md"
DEBUG_JSON = "system_cosim_contract_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id: str, filename: str, content: str):
    try:
        from utils.artifact_utils import save_text_artifact_and_record

        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=OUTPUT_SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _add_issue(items: List[Dict[str, Any]], severity: str, code: str, message: str, recommendation: str):
    items.append({
        "severity": severity,
        "code": code,
        "message": message,
        "recommendation": recommendation,
    })


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    print(f"\n⚙️ Running {AGENT_NAME}")

    manifest = state.get("system_cosim_manifest") or {}

    issues: List[Dict[str, Any]] = []
    passes: List[str] = []

    if not manifest:
        _add_issue(
            issues, "error", "MISSING_MANIFEST",
            "system_cosim_manifest is missing from state.",
            "Run System CoSim Ingest Agent before this agent.",
        )
    else:
        readiness = manifest.get("readiness") or {}
        sw = manifest.get("software") or {}
        fw = manifest.get("firmware") or {}
        rtl = manifest.get("rtl") or {}

        if readiness.get("ready_for_system_l2_contract"):
            passes.append("L2 ingest manifest is present and marked ready for contract validation.")
        else:
            _add_issue(
                issues, "error", "INGEST_NOT_READY",
                "CoSim ingest did not reach ready_for_system_l2_contract = true.",
                "Inspect ingest manifest and restore missing packages or RTL sim readiness.",
            )

        l1_status = sw.get("l1_validation_status")
        if l1_status == "ready":
            passes.append("System Software L1 validation is ready.")
        elif l1_status == "not_ready":
            _add_issue(
                issues, "error", "SOFTWARE_L1_NOT_READY",
                "System Software L1 validation is not ready.",
                "Fix L1 build/test/mock/package issues before enabling L2 execution.",
            )
        else:
            _add_issue(
                issues, "warning", "SOFTWARE_L1_STATUS_UNKNOWN",
                "System Software L1 validation status could not be determined.",
                "Prefer exporting system_software_validation_summary.json into the workflow.",
            )

        harness = state.get("system_software_cosim_harness_manifest") or {}
        harness_entry = harness.get("software_entry") or {}

        if sw.get("entry") or harness_entry:
            passes.append("Software entry point resolved.")
        else:
            _add_issue(
                issues, "warning", "SOFTWARE_ENTRY_MISSING",
                "Software entry point was not resolved.",
                "Populate software package with a default app entry or binary name.",
            )

        if fw.get("elf"):
            passes.append("Firmware ELF resolved.")
        else:
            _add_issue(
                issues, "error", "FIRMWARE_ELF_MISSING",
                "Firmware ELF is missing from the co-sim manifest.",
                "Ensure firmware packaging exports firmware_elf or elf.",
            )

        if fw.get("register_map"):
            passes.append("Register map linkage resolved.")
        else:
            _add_issue(
                issues, "error", "REGISTER_MAP_MISSING",
                "Register map was not found in software/firmware package data.",
                "Export register_map path in firmware package and software package.",
            )

        interrupts = fw.get("interrupts") or []
        if isinstance(interrupts, list):
            passes.append(f"Interrupt list normalized with {len(interrupts)} entries.")
        else:
            _add_issue(
                issues, "error", "INTERRUPTS_INVALID",
                "Interrupt information is not a normalized list.",
                "Export interrupts as a list in firmware manifest/package.",
            )

        dma_present = fw.get("dma_present")
        if isinstance(dma_present, bool):
            passes.append("DMA expectation resolved to a boolean.")
        else:
            _add_issue(
                issues, "warning", "DMA_UNRESOLVED",
                "DMA presence could not be resolved cleanly.",
                "Export dma_present explicitly in firmware manifest or RTL package.",
            )

        if rtl.get("top"):
            passes.append("RTL top module resolved.")
        else:
            _add_issue(
                issues, "error", "RTL_TOP_MISSING",
                "RTL top module for sim is missing.",
                "Ensure RTL package exports top.sim.",
            )

        if rtl.get("compile_sim") == "pass":
            passes.append("RTL sim compile status is pass.")
        else:
            _add_issue(
                issues, "error", "RTL_COMPILE_FAIL",
                "RTL sim compile did not pass.",
                "Fix System RTL packaging or compile flow before L2 execution.",
            )

        sim_filelist = ((rtl.get("filelists") or {}).get("sim")) or []
        if isinstance(sim_filelist, list) and sim_filelist:
            passes.append(f"RTL sim filelist present with {len(sim_filelist)} entries.")
        else:
            _add_issue(
                issues, "error", "RTL_FILELIST_EMPTY",
                "RTL sim filelist is empty.",
                "Fix System RTL package generation to emit populated sim filelist.",
            )

    blocking_errors = [i for i in issues if i["severity"] == "error"]
    overall_status = "ready" if not blocking_errors else "not_ready"

    report = {
        "validation_scope": "full_stack",
        "generated_at": _now(),
        "agent": AGENT_NAME,
        "passes": passes,
        "issues": issues,
        "overall_status": overall_status,
        "blocking_error_count": len(blocking_errors),
    }

    summary_lines = [
        "# CoSim Contract Summary",
        "",
        f"- Overall status: {overall_status}",
        f"- Pass count: {len(passes)}",
        f"- Issue count: {len(issues)}",
        f"- Blocking error count: {len(blocking_errors)}",
    ]
    if issues:
        summary_lines.append("")
        summary_lines.append("## Open Issues")
        for item in issues:
            summary_lines.append(f"- [{item['severity']}] {item['code']}: {item['message']}")
    summary = "\n".join(summary_lines) + "\n"

    debug = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "manifest_present": bool(manifest),
        "pass_count": len(passes),
        "issue_count": len(issues),
        "blocking_error_count": len(blocking_errors),
    }

    _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record(workflow_id, SUMMARY_MD, summary)
    _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))

    state["system_cosim_contract_report"] = report
    state["status"] = "✅ CoSim contract ready" if overall_status == "ready" else "⚠️ CoSim contract has blocking issues"
    return state
