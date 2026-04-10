
import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Package Audit Agent"
OUTPUT_SUBDIR = "system/software_validation/package"

REPORT_JSON = "package_audit_report.json"
SUMMARY_MD = "package_audit_summary.md"
DEBUG_JSON = "package_audit_debug.json"


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


def _present(item: Dict[str, Any]) -> bool:
    return bool((item or {}).get("exists"))


def _norm(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _derive_adapter_required_files(package_manifest: Dict[str, Any], discovered_assets: Dict[str, Any]) -> List[str]:
    adapter_manifest = discovered_assets.get("adapter_manifest") or {}
    adapter_path = (
        adapter_manifest.get("path")
        or adapter_manifest.get("artifact_path")
        or adapter_manifest.get("resolved_path")
        or ""
    )
    adapter_path = _norm(adapter_path)

    if adapter_path.endswith(".json"):
        adapter_path = adapter_path.rsplit("/", 1)[0]

    adapter_crate = str(
        adapter_manifest.get("adapter_crate")
        or package_manifest.get("adapter_crate")
        or adapter_manifest.get("adapter_package_name")
        or ""
    ).strip()

    if not adapter_path:
        adapter_path = f"system/software/adapter/{adapter_crate}"
    elif adapter_path.endswith("/adapter"):
        adapter_path = f"{adapter_path}/{adapter_crate}"
    elif adapter_crate and not adapter_path.endswith(f"/{adapter_crate}"):    
        adapter_path = f"system/software/adapter/{adapter_crate}"

    adapter_path = adapter_path.rstrip("/")
    return [
        f"{adapter_path}/Cargo.toml",
        f"{adapter_path}/src/lib.rs",
    ]


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = str(state.get("workflow_dir") or "").strip()

    print(f"\n📦 Running {AGENT_NAME}")

    validation_manifest = state.get("system_software_validation_manifest") or {}
    package_manifest = state.get("system_software_package") or {}
    package_file_checks = state.get("system_software_validation_package_file_checks") or []

    discovered_assets = (validation_manifest.get("discovered_assets") or {})
    required_assets = ((validation_manifest.get("validation_inputs") or {}).get("required_assets")) or []
    optional_assets = ((validation_manifest.get("validation_inputs") or {}).get("optional_assets")) or []

    missing_required_assets = [x.get("name") for x in required_assets if x.get("status") == "missing_required"]
    missing_optional_assets = [x.get("name") for x in optional_assets if x.get("status") == "missing_optional"]

    missing_required_files = [x.get("path") for x in package_file_checks if x.get("required") and not x.get("exists")]
    missing_optional_files = [x.get("path") for x in package_file_checks if (not x.get("required")) and not x.get("exists")]

    expected_sections = {
        "sdk": _present(discovered_assets.get("sdk_manifest") or {}),
        "apps": _present(discovered_assets.get("application_manifest") or {}),
        "build": _present(discovered_assets.get("build_manifest") or {}),
        "input_contract": _present(discovered_assets.get("input_contract") or {}),
        "api_contract": _present(discovered_assets.get("api_contract") or {}),
        "tests": _present(discovered_assets.get("test_manifest") or {}),
        "mock": _present(discovered_assets.get("mock_manifest") or {}),
        "tools": _present(discovered_assets.get("tools_manifest") or {}),
        "adapter": _present(discovered_assets.get("adapter_manifest") or {}),
        "services": _present(discovered_assets.get("services_manifest") or {}),
        "executive_summary": _present(discovered_assets.get("executive_summary") or {}),
    }

    package_files = {_norm(p) for p in (package_manifest.get("files") or []) if _norm(p)}
    required_adapter_files = _derive_adapter_required_files(package_manifest, discovered_assets)
    missing_adapter_package_files = []

    for path in required_adapter_files:
        if workflow_dir:
            full_path = os.path.join(workflow_dir, path)
            if not os.path.isfile(full_path):
                missing_adapter_package_files.append(path)


    package_status = "complete"
    if missing_required_assets or missing_required_files or missing_adapter_package_files:
        package_status = "incomplete"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "package_status": package_status,
        "artifact_count": package_manifest.get("artifact_count") or len(package_manifest.get("files") or []),
        "missing_required_assets": missing_required_assets,
        "missing_optional_assets": missing_optional_assets,
        "missing_required_package_files": missing_required_files,
        "missing_optional_package_files": missing_optional_files,
        "missing_required_adapter_package_files": missing_adapter_package_files,
        "required_adapter_package_files": required_adapter_files,
        "expected_sections": expected_sections,
    }

    summary_lines = [
        "# Package Audit Summary",
        "",
        f"- Status: **{package_status}**",
        f"- Artifact count: `{report['artifact_count']}`",
        "",
        "## Missing required assets",
    ]
    if missing_required_assets:
        summary_lines.extend([f"- `{x}`" for x in missing_required_assets])
    else:
        summary_lines.append("- none")

    summary_lines.extend(["", "## Missing required package files"])
    if missing_required_files:
        summary_lines.extend([f"- `{x}`" for x in missing_required_files])
    else:
        summary_lines.append("- none")

    summary_lines.extend(["", "## Missing required adapter package files"])
    if missing_adapter_package_files:
        summary_lines.extend([f"- `{x}`" for x in missing_adapter_package_files])
    else:
        summary_lines.append("- none")

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "package_manifest_present": bool(package_manifest),
        "package_file_check_count": len(package_file_checks),
        "expected_sections": expected_sections,
        "required_adapter_package_files": required_adapter_files,
        "missing_required_adapter_package_files": missing_adapter_package_files,
    }

    _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record_text(workflow_id, SUMMARY_MD, "\n".join(summary_lines) + "\n")
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_package_audit"] = report
    state["package_status"] = package_status
    state["status"] = "✅ package audit complete" if package_status == "complete" else "⚠️ package audit found missing required items"
    return state
