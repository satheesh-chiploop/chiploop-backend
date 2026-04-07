
import datetime
import json
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Validation Summary Agent"
OUTPUT_SUBDIR = "system/software_validation/summary"

REPORT_JSON = "system_software_validation_summary.json"
SUMMARY_MD = "system_software_validation_summary.md"
EXECUTIVE_JSON = "system_software_validation_executive.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_text(workflow_id: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=OUTPUT_SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _normalize_status(value: Any, valid_values: List[str], default: str) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return text if text in valid_values else default


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    source_software_workflow_id = state.get("source_software_workflow_id") or state.get("system_software_workflow_id") or ""

    print(f"\n📋 Running {AGENT_NAME}")


    build_status = _normalize_status(
        state.get("build_status"),
        ["pass", "fail", "not_present", "environment_missing"],
        "not_present",
    )
    test_status = _normalize_status(
        state.get("test_status"),
        ["pass", "fail", "not_present", "environment_missing"],
        "not_present",
    )
    contract_status = _normalize_status(
        state.get("contract_status"),
        ["pass", "fail", "not_present"],
        "not_present",
    )
    mock_runtime_status = _normalize_status(
        state.get("mock_runtime_status"),
        ["pass", "fail", "not_present", "environment_missing"],
        "not_present",
    )
    package_status = _normalize_status(
        state.get("package_status"),
        ["complete", "incomplete", "not_present"],
        "not_present",
    )

    validation_manifest = state.get("system_software_validation_manifest") or {}
    readiness = validation_manifest.get("validation_readiness") or {}

    missing_required_assets = readiness.get("missing_required_assets") or []
    missing_required_package_files = readiness.get("missing_required_package_files") or []

    blocking_issues: List[str] = []
    if missing_required_assets:
        blocking_issues.extend([f"missing_required_asset:{x}" for x in missing_required_assets])
    if missing_required_package_files:
        blocking_issues.extend([f"missing_required_package_file:{x}" for x in missing_required_package_files])

    if build_status == "fail":
        blocking_issues.append("build_failed")
    elif build_status == "environment_missing":
        blocking_issues.append("build_environment_missing")
    elif build_status == "not_present":
        blocking_issues.append("build_not_present")

    if contract_status == "fail":
        blocking_issues.append("contract_validation_failed")
    elif contract_status == "not_present":
        blocking_issues.append("contract_validation_not_present")

    if package_status == "incomplete":
        blocking_issues.append("package_incomplete")
    elif package_status == "not_present":
        blocking_issues.append("package_not_present")

    if test_status == "fail":
        blocking_issues.append("tests_failed")
    elif test_status == "environment_missing":
        blocking_issues.append("test_environment_missing")

    if mock_runtime_status == "fail":
        blocking_issues.append("mock_runtime_failed")
    elif mock_runtime_status == "environment_missing":
        blocking_issues.append("mock_runtime_environment_missing")

    overall_status = "ready" if not blocking_issues else "not_ready"

    report = {
        "package_type": "system_software_validation_summary",
        "package_version": "1.0",
        "generated_at": _now(),
        "validation_scope": "software_only",
        "co_sim_enabled": False,
        "source_software_workflow_id": source_software_workflow_id,
        "build_status": build_status,
        "test_status": test_status,
        "contract_status": contract_status,
        "mock_runtime_status": mock_runtime_status,
        "package_status": package_status,
        "overall_status": overall_status,
        "blocking_issues": blocking_issues,
    }

    executive = {
        "ready_for_next_stage": overall_status == "ready",
        "overall_status": overall_status,
        "headline": (
            "System software validation passed. Software stack is ready for next-stage integration."
            if overall_status == "ready"
            else "System software validation found blocking issues. Fix required before integration."
        ),
        "key_findings": [
            f"Build status: {build_status}",
            f"Test status: {test_status}",
            f"Contract status: {contract_status}",
            f"Mock runtime status: {mock_runtime_status}",
            f"Package status: {package_status}",
        ],
        "blocking_issues": blocking_issues,
    }

    summary_lines = [
        "# System Software Validation Summary",
        "",
        f"- Overall status: **{overall_status}**",
        f"- Build status: `{build_status}`",
        f"- Test status: `{test_status}`",
        f"- Contract status: `{contract_status}`",
        f"- Mock runtime status: `{mock_runtime_status}`",
        f"- Package status: `{package_status}`",
        "",
        "## Blocking issues",
    ]
    if blocking_issues:
        summary_lines.extend([f"- `{x}`" for x in blocking_issues])
    else:
        summary_lines.append("- none")

    _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record_text(workflow_id, SUMMARY_MD, "\n".join(summary_lines) + "\n")
    _record_text(workflow_id, EXECUTIVE_JSON, json.dumps(executive, indent=2))

    state["system_software_validation_summary"] = report
    state["system_software_validation_summary_path"] = f"{OUTPUT_SUBDIR}/{REPORT_JSON}"
    state["overall_status"] = overall_status
    state["status"] = "✅ system software validation summary complete" if overall_status == "ready" else "⚠️ system software validation not ready"
    return state
