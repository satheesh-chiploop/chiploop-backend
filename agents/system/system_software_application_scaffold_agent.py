
import datetime
import json
import os
from typing import Any, Dict, List, Optional
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Application Scaffold Agent"
OUTPUT_SUBDIR = "system/software/apps"
MANIFEST_JSON = "system_software_application_manifest.json"
SUMMARY_MD = "system_software_application_summary.md"
DEBUG_JSON = "system_software_application_debug.json"

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(workflow_id=workflow_id, agent_name=AGENT_NAME, subdir=subdir, filename=filename, content=content)
    except Exception:
        return None

def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

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

def _load_sdk_manifest(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_sdk_manifest")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get("system_software_sdk_manifest_path"), "system/software/sdk/system_software_sdk_manifest.json"):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if loaded:
                return loaded
    return {}

def _load_arch(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_service_architecture")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get("system_software_service_architecture_path"), "system/software/architecture/system_software_service_architecture.json"):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if loaded:
                return loaded
    return {}

def _app_names(state: Dict[str, Any], sdk_manifest: Dict[str, Any]) -> List[str]:
    apps = state.get("app_names") or sdk_manifest.get("app_names") or ["health_app", "diag_console"]
    if isinstance(apps, str):
        apps = [x.strip() for x in apps.split(",") if x.strip()]
    if not isinstance(apps, list):
        return ["health_app", "diag_console"]
    out = [str(x).strip() for x in apps if str(x).strip()]
    return out or ["health_app", "diag_console"]

def _render_app(crate_name: str, app_name: str) -> str:
    return f'''
use {crate_name}::Sdk;
use std::env;

fn main() {{
    let args: Vec<String> = env::args().collect();
    let scenario = args.iter()
        .position(|x| x == "--scenario")
        .and_then(|i| args.get(i+1))
        .map(|s| s.as_str())
        .unwrap_or("default");

    let sdk = Sdk::new();
    let status = sdk.get_status();

    println!("app={app_name} scenario={{}} status={{:?}}", scenario, status);

    if scenario.contains("register") {{
        println!("register_write=0x10");
    }}

    if scenario.contains("interrupt") {{
        println!("interrupt_triggered=1");
    }}
}}
'''

def _manifest(source_workflow_id: str, crate_name: str, files: List[str], app_names: List[str]) -> Dict[str, Any]:
    default_application = app_names[0] if app_names else ""
    default_package = _app_package_name(default_application) if default_application else ""

    applications = []
    for name in app_names:
        cargo_package = _app_package_name(name)
        applications.append(
            {
                "app_name": name,
                "cargo_package": cargo_package,
                "package_name": cargo_package,
                "binary_name": cargo_package,
                "path": f"{OUTPUT_SUBDIR}/{name}",
                "is_default": name == default_application,
                "is_entry": name == default_application,
            }
        )

    return {
        "package_type": "system_software_application_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "crate_name": crate_name,
        "application_names": app_names,
        "applications": applications,
        "default_application": default_application,
        "entry_application": default_application,
        "entry_package": default_package,
        "binary_name": default_package,
        "entry_binary": default_package,
        "files": files,
    }

def _markdown(manifest: Dict[str, Any]) -> str:
    lines = ["# System Software Applications", "", f"- Generated at: {manifest.get('generated_at')}", f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}", f"- Crate name: `{manifest.get('crate_name')}`", "", "## Applications", ""]
    for item in manifest.get("applications") or []:
        lines.append(f"- `{item.get('app_name')}` → package `{item.get('package_name')}`")

    lines.extend(["", "## Files", ""])
    for path in manifest.get("files") or []:
        lines.append(f"- `{path}`")
    lines.append("")
    return "\n".join(lines)

def _cargo_safe_name(value: str) -> str:
    text = (value or "app").strip().lower().replace("-", "_")
    out = []
    for ch in text:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    name = "".join(out).strip("_") or "app"
    if name[0].isdigit():
        name = f"app_{name}"
    return name


def _app_package_name(app_name: str) -> str:
    return f"ss_app_{_cargo_safe_name(app_name)}"

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""
    print(f"\n📱 Running {AGENT_NAME}")
    sdk_manifest = _load_sdk_manifest(state, workflow_dir)
    if not sdk_manifest:
        state["status"] = "❌ system software sdk manifest missing"
        return state
    arch = _load_arch(state, workflow_dir)
    if not arch:
        state["status"] = "❌ system software architecture missing"
        return state

    crate_name = str(sdk_manifest.get("crate_name") or "system_software_sdk").strip()
    app_names = _app_names(state, sdk_manifest)
    written = []

    for app_name in app_names:
        subdir = f"{OUTPUT_SUBDIR}/{app_name}/src"

        # main.rs
        _record_text(workflow_id, "main.rs", _render_app(crate_name, app_name), subdir=subdir)
        written.append(f"{subdir}/main.rs")


        app_pkg_name = _app_package_name(app_name)

        _record_text(workflow_id, "Cargo.toml", f"""[package]
        name = "{app_pkg_name}"
        version = "0.1.0"
        edition = "2021"

        [dependencies]
        {crate_name} = {{ path = "../../sdk/{crate_name}" }}
        system_services = {{ path = "../../services" }}
        """, subdir=f"{OUTPUT_SUBDIR}/{app_name}")

        # FIX: Cargo.toml per app
        written.append(f"{OUTPUT_SUBDIR}/{app_name}/Cargo.toml")

    manifest = _manifest(arch.get("source_workflow_id"), crate_name, written, app_names)
    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({"agent": AGENT_NAME, "generated_at": _now(), "app_names": app_names, "crate_name": crate_name}, indent=2))


    default_application = str(manifest.get("default_application") or "").strip()
    entry_package = str(manifest.get("entry_package") or "").strip()

    state["system_software_application_manifest"] = manifest
    state["system_software_default_application"] = default_application
    state["system_software_entry_application"] = str(manifest.get("entry_application") or default_application).strip()
    state["system_software_entry_package"] = entry_package
    state["system_software_entry_binary"] = str(manifest.get("entry_binary") or entry_package).strip()
    state["system_software_application_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software applications generated"
    
    return state
    