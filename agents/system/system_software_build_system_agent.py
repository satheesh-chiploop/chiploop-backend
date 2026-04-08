import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Build System Agent"
OUTPUT_SUBDIR = "system/software/build"
MANIFEST_JSON = "system_software_build_manifest.json"
SUMMARY_MD = "system_software_build_summary.md"
DEBUG_JSON = "system_software_build_debug.json"


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


def _load_manifest(state: Dict[str, Any], workflow_dir: str, state_key: str, path_key: str, fallback: str) -> Dict[str, Any]:
    obj = state.get(state_key)
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get(path_key), fallback):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, str(candidate)))
            if loaded:
                return loaded
    return {}


def _render_workspace(root_members: List[str]) -> str:
    members = ",\n".join([f'  "{m}"' for m in root_members])
    return f'''[workspace]
resolver = "2"
members = [
{members}
]
'''


def _render_makefile() -> str:
    return '''.PHONY: fmt build test

fmt:
\tcargo fmt --all

build:
\tcargo build --workspace

test:
\tcargo test --workspace
'''


def _build_manifest(source_workflow_id: str, members: List[str], files: List[str]) -> Dict[str, Any]:
    return {
        "package_type": "system_software_build_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "workspace_members": members,
        "files": files,
    }


def _markdown_summary(manifest: Dict[str, Any]) -> str:
    lines = [
        "# System Software Build System",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}",
        "",
        "## Workspace members",
        "",
    ]
    for item in manifest.get("workspace_members") or []:
        lines.append(f"- `{item}`")
    lines.extend(["", "## Files", ""])
    for item in manifest.get("files") or []:
        lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)


def _package_name_from_cargo_toml(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                in_package = False
                for line in f:
                    s = line.strip()
                    if s.startswith("["):
                        in_package = (s == "[package]")
                        continue
                    if in_package and s.startswith("name") and "=" in s:
                        rhs = s.split("=", 1)[1].strip().strip('"').strip("'")
                        if rhs:
                            return rhs
    except Exception:
        pass
    return ""


def _member_cargo_toml_path(workflow_dir: str, member: str) -> str:
    if not workflow_dir:
        return ""
    return os.path.abspath(os.path.join(workflow_dir, OUTPUT_SUBDIR, member, "Cargo.toml"))


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""

    sdk_manifest = _load_manifest(state, workflow_dir, "system_software_sdk_manifest", "system_software_sdk_manifest_path", "system/software/sdk/system_software_sdk_manifest.json")
    app_manifest = _load_manifest(state, workflow_dir, "system_software_application_manifest", "system_software_application_manifest_path", "system/software/apps/system_software_application_manifest.json")
    tools_manifest = _load_manifest(state, workflow_dir, "system_software_tools_manifest", "system_software_tools_manifest_path", "system/software/tools/system_software_tools_manifest.json")
    adapter_manifest = _load_manifest(state, workflow_dir, "system_software_adapter_manifest", "system_software_adapter_manifest_path", "system/software/adapter/system_software_adapter_manifest.json")

    if not sdk_manifest:
        state["status"] = "❌ system software sdk manifest missing"
        return state
    if not app_manifest:
        state["status"] = "❌ system software application manifest missing"
        return state
    if not adapter_manifest:
        state["status"] = "❌ system software adapter manifest missing"
        return state

    sdk_crate = str(sdk_manifest.get("crate_name") or "system_software_sdk").strip()
    adapter_crate = str(adapter_manifest.get("adapter_crate") or sdk_crate).strip()
    adapter_member = f"../adapter/{adapter_crate}"

    applications = app_manifest.get("applications") or []
    tools = tools_manifest.get("tools") or []

    app_names = [str(x).strip() for x in (app_manifest.get("application_names") or []) if str(x).strip()]
    tool_names = [str(x).strip() for x in (tools_manifest.get("tool_names") or []) if str(x).strip()]

    members = [f"../sdk/{sdk_crate}", "../services", adapter_member]

    if applications:
        members.extend([f"../apps/{item['app_name']}" for item in applications if isinstance(item, dict) and item.get("app_name")])
    else:
        members.extend([f"../apps/{name}" for name in app_names])

    if tools:
        members.extend([f"../tools/{item['tool_name']}" for item in tools if isinstance(item, dict) and item.get("tool_name")])
    else:
        members.extend([f"../tools/{name}" for name in tool_names])

    missing_members = []
    package_name_to_member = {}
    duplicates = []

    for member in members:
        cargo_toml_path = _member_cargo_toml_path(workflow_dir, member)
        if not cargo_toml_path or not os.path.isfile(cargo_toml_path):
            missing_members.append({
                "member": member,
                "expected_cargo_toml": cargo_toml_path,
            })
            continue

        pkg_name = _package_name_from_cargo_toml(cargo_toml_path)
        if not pkg_name:
            missing_members.append({
                "member": member,
                "expected_cargo_toml": cargo_toml_path,
                "reason": "package_name_missing",
            })
            continue

        if pkg_name in package_name_to_member:
            duplicates.append({
                "package_name": pkg_name,
                "members": [package_name_to_member[pkg_name], member],
            })
        else:
            package_name_to_member[pkg_name] = member

    if missing_members:
        debug_payload = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "missing_workspace_members": missing_members,
            "workspace_members": members,
        }
        _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))
        state["status"] = "❌ missing workspace member Cargo.toml detected"
        return state

    if duplicates:
        debug_payload = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "duplicate_package_names": duplicates,
            "workspace_members": members,
        }
        _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))
        state["status"] = f"❌ duplicate Cargo package names detected: {', '.join(sorted({d['package_name'] for d in duplicates}))}"
        return state

    written: List[str] = []
    _record_text(workflow_id, "Cargo.toml", _render_workspace(members))
    written.append(f"{OUTPUT_SUBDIR}/Cargo.toml")

    _record_text(workflow_id, "Makefile", _render_makefile())
    written.append(f"{OUTPUT_SUBDIR}/Makefile")

    build_plan = {
        "sdk_crate": sdk_crate,
        "adapter_crate": adapter_crate,
        "app_names": app_names,
        "tool_names": tool_names,
        "workspace_members": members,
        "workspace_package_names": package_name_to_member,
        "default_targets": ["fmt", "build", "test"],
    }
    _record_text(workflow_id, "build_plan.json", json.dumps(build_plan, indent=2))
    written.append(f"{OUTPUT_SUBDIR}/build_plan.json")

    manifest = _build_manifest(
        source_workflow_id=str(app_manifest.get("source_workflow_id") or sdk_manifest.get("source_workflow_id") or ""),
        members=members,
        files=written,
    )

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "workspace_member_count": len(members),
        "workspace_package_names": package_name_to_member,
        "adapter_crate": adapter_crate,
    }, indent=2))

    state["system_software_build_manifest"] = manifest
    state["system_software_build_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software build system generated"
    return state
