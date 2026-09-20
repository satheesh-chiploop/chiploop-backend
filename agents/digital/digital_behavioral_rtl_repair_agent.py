import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record
from .digital_rtl_agent import (
    _complete_rtl_text,
    _merge_rtl_repair_output,
    _validate_and_materialize_rtl,
)


AGENT_NAME = "Digital Behavioral RTL Repair Agent"


def _rtl_files(state: Dict[str, Any]) -> List[str]:
    return [
        os.path.abspath(path) for path in (state.get("rtl_files") or [])
        if isinstance(path, str) and os.path.isfile(path) and path.lower().endswith((".v", ".sv"))
    ]


def _named_rtl(files: List[str]) -> str:
    blocks = []
    for path in files:
        blocks.append(
            f"---BEGIN {os.path.basename(path)}---\n"
            + Path(path).read_text(encoding="utf-8", errors="ignore")
            + f"\n---END {os.path.basename(path)}---"
        )
    return "\n".join(blocks)


def _repair_context_files(files: List[str], request: Dict[str, Any]) -> List[str]:
    """Prefer owning-module sources while retaining a safe all-RTL fallback."""
    owners = {
        str(item.get("owner_module") or "").strip()
        for item in request.get("failures") or [] if isinstance(item, dict)
    } - {""}
    if not owners:
        return files
    selected: List[str] = []
    for path in files:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        if any(re.search(rf"\bmodule\s+{re.escape(owner)}\b", text) for owner in owners):
            selected.append(path)
    return selected or files


def _repair_prompt(request: Dict[str, Any], rtl: str) -> str:
    return f"""You are repairing synthesizable RTL after executable verification found behavioral defects.

RULES:
- Repair only behavior proven faulty by the requirement-linked assertion evidence below.
- Preserve every module interface, filename, register address, and unrelated behavior.
- Make the smallest complete semantic change in the owning module.
- Respect nonblocking assignment priority; use one explicit decision chain when events compete.
- Return complete changed files using ---BEGIN filename--- / ---END filename--- blocks.
- Do not modify assertions or testbench code to hide the failure.

REPAIR REQUEST:
{json.dumps(request, indent=2)}

CURRENT RTL:
{rtl}
"""


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    request = state.get("rtl_repair_request") if isinstance(state.get("rtl_repair_request"), dict) else {}
    if not request.get("required"):
        state["behavioral_rtl_repair"] = {"status": "not_required"}
        return state
    if request.get("status") == "blocked_nonconvergent":
        raise RuntimeError(
            f"Behavioral RTL repair stopped after repeated failure fingerprint {request.get('fingerprint')}"
        )

    files = _rtl_files(state)
    if not files:
        raise RuntimeError("Behavioral RTL repair requires materialized rtl_files from verification handoff")

    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    repair_root = workflow_dir / "verify_closure" / "behavioral_rtl_repair"
    repair_root.mkdir(parents=True, exist_ok=True)
    previous = _named_rtl(files)
    repair_context = _named_rtl(_repair_context_files(files, request))
    prompt = _repair_prompt(request, repair_context)
    candidate = _complete_rtl_text(
        prompt,
        agent_name=AGENT_NAME,
        state=state,
        stage_label=f"behavioral_repair_{request.get('attempt') or 1}",
    )
    expected_files = [os.path.basename(path) for path in files]
    merged = _merge_rtl_repair_output(previous, candidate, expected_files)

    spec = state.get("digital_spec") if isinstance(state.get("digital_spec"), dict) else None
    if spec is None:
        spec_path = state.get("spec_json") or state.get("digital_spec_json")
        if isinstance(spec_path, str) and os.path.isfile(spec_path):
            spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise RuntimeError("Behavioral RTL repair requires the digital specification JSON")

    result = _validate_and_materialize_rtl(
        llm_output=merged,
        rtl_dir=str(repair_root),
        spec_json=spec,
        mode=str(state.get("mode") or "digital"),
        suffix=f"behavioral_repair_{request.get('attempt') or 1}",
        materialize_subdir="candidate",
        state=state,
    )
    if not result.get("ok"):
        raise RuntimeError(
            "Behavioral RTL repair candidate failed compile/lint/structural closure: "
            + "; ".join(str(item) for item in (result.get("issues") or [])[:12])
        )

    repaired_files = [str(Path(path).resolve()) for path in result.get("artifact_list") or []]
    repaired_artifacts = []
    for path in repaired_files:
        rtl_text = Path(path).read_text(encoding="utf-8", errors="ignore")
        repaired_artifacts.append(save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "verify_closure/rtl_repair",
            os.path.basename(path),
            rtl_text,
        ))
    report = {
        "type": "behavioral_rtl_repair",
        "status": "candidate_validated_pending_focused_verification",
        "fingerprint": request.get("fingerprint"),
        "attempt": request.get("attempt"),
        "requirements": sorted({
            item.get("requirement_id") for item in request.get("failures") or [] if item.get("requirement_id")
        }),
        "rtl_files": repaired_files,
        "repaired_rtl_artifacts": repaired_artifacts,
        "compile_passed": result.get("compile_passed"),
        "lint_passed": result.get("lint_passed"),
        "static_spec2rtl_passed": result.get("static_spec2rtl_passed"),
    }
    report_text = json.dumps(report, indent=2)
    report_path = repair_root / "behavioral_rtl_repair.json"
    report_path.write_text(report_text, encoding="utf-8")
    save_text_artifact_and_record(
        workflow_id, AGENT_NAME, "verify_closure", "behavioral_rtl_repair.json", report_text
    )
    state["rtl_files"] = repaired_files
    state["rtl_inputs"] = repaired_files
    state["behavioral_rtl_repair"] = report
    state["behavioral_rtl_repair_report"] = str(report_path)
    return state
