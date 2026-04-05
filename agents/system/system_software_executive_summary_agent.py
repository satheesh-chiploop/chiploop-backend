import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Executive Summary Agent"
OUTPUT_SUBDIR = "system/software/executive"
SUMMARY_MD = "system_software_executive_summary.md"
SUMMARY_JSON = "system_software_executive_summary.json"
DEBUG_JSON = "system_software_executive_summary_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(str(value).strip())


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _join_workflow_path(workflow_dir: str, rel_or_abs: str) -> str:
    if not rel_or_abs:
        return ""
    if os.path.isabs(rel_or_abs):
        return rel_or_abs
    return os.path.abspath(os.path.join(workflow_dir, rel_or_abs))


def _load_manifest(
    state: Dict[str, Any],
    workflow_dir: str,
    state_key: str,
    path_key: str,
    fallback: str,
) -> Dict[str, Any]:
    obj = state.get(state_key)
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get(path_key), fallback):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, str(candidate)))
            if loaded:
                return loaded
    return {}


def _load_all_manifests(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Dict[str, Any]]:
    return {
        "input": _load_manifest(
            state, workflow_dir,
            "system_software_input_contract",
            "system_software_input_contract_path",
            "system/software/input/system_software_input_contract.json",
        ),
        "capability": _load_manifest(
            state, workflow_dir,
            "system_software_capability_model",
            "system_software_capability_model_path",
            "system/software/model/system_software_capability_model.json",
        ),
        "api": _load_manifest(
            state, workflow_dir,
            "system_software_api_contract",
            "system_software_api_contract_path",
            "system/software/sdk/system_software_api_contract.json",
        ),
        "sdk": _load_manifest(
            state, workflow_dir,
            "system_software_sdk_manifest",
            "system_software_sdk_manifest_path",
            "system/software/sdk/system_software_sdk_manifest.json",
        ),
        "adapter": _load_manifest(
            state, workflow_dir,
            "system_software_adapter_manifest",
            "system_software_adapter_manifest_path",
            "system/software/adapter/system_software_adapter_manifest.json",
        ),
        "config": _load_manifest(
            state, workflow_dir,
            "system_software_config_schema",
            "system_software_config_schema_path",
            "system/software/config/system_software_config_schema.json",
        ),
        "architecture": _load_manifest(
            state, workflow_dir,
            "system_software_service_architecture",
            "system_software_service_architecture_path",
            "system/software/architecture/system_software_service_architecture.json",
        ),
        "services": _load_manifest(
            state, workflow_dir,
            "system_software_core_services_manifest",
            "system_software_core_services_manifest_path",
            "system/software/services/system_software_core_services_manifest.json",
        ),
        "apps": _load_manifest(
            state, workflow_dir,
            "system_software_application_manifest",
            "system_software_application_manifest_path",
            "system/software/apps/system_software_application_manifest.json",
        ),
        "tools": _load_manifest(
            state, workflow_dir,
            "system_software_tools_manifest",
            "system_software_tools_manifest_path",
            "system/software/tools/system_software_tools_manifest.json",
        ),
        "build": _load_manifest(
            state, workflow_dir,
            "system_software_build_manifest",
            "system_software_build_manifest_path",
            "system/software/build/system_software_build_manifest.json",
        ),
        "tests": _load_manifest(
            state, workflow_dir,
            "system_software_test_manifest",
            "system_software_test_manifest_path",
            "system/software/tests/system_software_test_manifest.json",
        ),
        "mock": _load_manifest(
            state, workflow_dir,
            "system_software_mock_manifest",
            "system_software_mock_manifest_path",
            "system/software/mock/system_software_mock_manifest.json",
        ),
        "package": _load_manifest(
            state, workflow_dir,
            "system_software_package",
            "system_software_package_path",
            "system/software/package/system_software_package.json",
        ),
    }


def _bool_ready(manifests: Dict[str, Dict[str, Any]], required: List[str]) -> bool:
    return all(bool(manifests.get(name)) for name in required)


def _artifact_count(manifest: Dict[str, Any]) -> int:
    files = manifest.get("files")
    if isinstance(files, list):
        return len(files)
    count = manifest.get("artifact_count")
    return int(count) if isinstance(count, int) else 0


def _package_quality(manifests: Dict[str, Dict[str, Any]]) -> str:
    required = ["sdk", "adapter", "services", "apps", "tools", "build", "tests", "mock", "package"]
    if not _bool_ready(manifests, required):
        return "incomplete"

    package = manifests["package"]
    artifact_count = _artifact_count(package)
    if artifact_count < 10:
        return "minimal"

    input_contract = manifests["input"]
    status = input_contract.get("input_status") if isinstance(input_contract, dict) else {}
    if isinstance(status, dict) and status.get("valid") is False:
        return "at_risk"

    return "ready"


def _top_risks(manifests: Dict[str, Dict[str, Any]]) -> List[str]:
    risks: List[str] = []

    input_contract = manifests.get("input") or {}
    input_status = input_contract.get("input_status") or {}
    if isinstance(input_status, dict):
        for item in input_status.get("blocking_gaps") or []:
            risks.append(f"Input contract blocking gap: {item}")
        for item in input_status.get("assumptions") or []:
            risks.append(f"Input contract assumption: {item}")

    package_quality = _package_quality(manifests)
    if package_quality != "ready":
        risks.append(f"Package quality is {package_quality}")

    if not manifests.get("build"):
        risks.append("Build system manifest missing")
    if not manifests.get("tests"):
        risks.append("Unit test manifest missing")
    if not manifests.get("mock"):
        risks.append("Mock runtime manifest missing")

    risks.append("Generated code is scaffold-grade; compile/test execution evidence is not embedded in this summary")
    risks.append("Many generated implementations remain stubs and require downstream functional hardening")

    deduped: List[str] = []
    seen = set()
    for item in risks:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped[:12]


def _build_summary(manifests: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    capability = manifests.get("capability") or {}
    hardware_model = capability.get("hardware_model") or {}
    register_summary = hardware_model.get("register_summary") or {}
    software_services = capability.get("software_services") or {}
    apps_manifest = manifests.get("apps") or {}
    tools_manifest = manifests.get("tools") or {}
    package_manifest = manifests.get("package") or {}
    services_manifest = manifests.get("services") or {}

    readiness_required = ["sdk", "adapter", "services", "apps", "tools", "build", "tests", "mock", "package"]

    return {
        "package_type": "system_software_executive_summary",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": (
            package_manifest.get("source_workflow_id")
            or apps_manifest.get("source_workflow_id")
            or (manifests.get("sdk") or {}).get("source_workflow_id")
            or ""
        ),
        "readiness": {
            "required_manifests_present": {name: bool(manifests.get(name)) for name in readiness_required},
            "overall_ready": _bool_ready(manifests, readiness_required),
            "package_quality": _package_quality(manifests),
        },
        "delivery_overview": {
            "application_names": apps_manifest.get("application_names") or [],
            "tool_names": tools_manifest.get("tool_names") or [],
            "service_names": services_manifest.get("service_names") or [],
            "package_artifact_count": _artifact_count(package_manifest),
        },
        "technical_overview": {
            "register_count": register_summary.get("register_count"),
            "field_count": register_summary.get("field_count"),
            "bus": register_summary.get("bus"),
            "service_flags": software_services,
        },
        "top_risks": _top_risks(manifests),
        "recommended_next_steps": [
            "Run workspace-level cargo build and cargo test on the packaged software stack",
            "Replace scaffold stubs with functional implementations for platform-specific behavior",
            "Add CI validation for SDK, services, apps, and tools",
            "Tie executive summary status to build/test execution evidence in future hardening passes",
        ],
    }


def _markdown_summary(summary: Dict[str, Any]) -> str:
    readiness = summary.get("readiness") or {}
    delivery = summary.get("delivery_overview") or {}
    tech = summary.get("technical_overview") or {}

    lines = [
        "# System Software Executive Summary",
        "",
        f"- Generated at: {summary.get('generated_at')}",
        f"- Source workflow id: {summary.get('source_workflow_id') or 'unavailable'}",
        f"- Overall ready: `{readiness.get('overall_ready')}`",
        f"- Package quality: `{readiness.get('package_quality')}`",
        "",
        "## Delivery overview",
        "",
        f"- Applications: `{', '.join(delivery.get('application_names') or []) or 'none'}`",
        f"- Tools: `{', '.join(delivery.get('tool_names') or []) or 'none'}`",
        f"- Services: `{', '.join(delivery.get('service_names') or []) or 'none'}`",
        f"- Packaged artifact count: `{delivery.get('package_artifact_count')}`",
        "",
        "## Technical overview",
        "",
        f"- Register count: `{tech.get('register_count')}`",
        f"- Field count: `{tech.get('field_count')}`",
        f"- Bus: `{tech.get('bus') or 'unavailable'}`",
        "",
        "## Risks",
        "",
    ]

    risks = summary.get("top_risks") or []
    if risks:
        for item in risks:
            lines.append(f"- {item}")
    else:
        lines.append("- No material risks recorded")

    lines.extend(["", "## Recommended next steps", ""])
    for item in summary.get("recommended_next_steps") or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""

    manifests = _load_all_manifests(state, workflow_dir)
    if not manifests.get("package"):
        state["status"] = "❌ system software package missing"
        return state

    summary = _build_summary(manifests)

    _record_text(workflow_id, SUMMARY_JSON, json.dumps(summary, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(summary))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "manifests_present": {k: bool(v) for k, v in manifests.items()},
        "package_quality": (summary.get("readiness") or {}).get("package_quality"),
    }, indent=2))

    state["system_software_executive_summary"] = summary
    state["system_software_executive_summary_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_JSON}"
    state["status"] = "✅ System software executive summary generated"
    return state
