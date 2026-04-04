import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software SDK Scaffold Agent"
OUTPUT_SUBDIR = "system/software/sdk"
MANIFEST_JSON = "system_software_sdk_manifest.json"
SUMMARY_MD = "system_software_sdk_summary.md"
DEBUG_JSON = "system_software_sdk_debug.json"


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


def _load_api_contract(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    contract = state.get("system_software_api_contract")
    if isinstance(contract, dict) and contract:
        return contract
    for candidate in (
        state.get("system_software_api_contract_path"),
        "system/software/sdk/system_software_api_contract.json",
    ):
        if isinstance(candidate, str) and candidate.strip() and workflow_dir:
            obj = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if obj:
                return obj
    return {}


def _sanitize_crate_name(value: str) -> str:
    value = (value or "system_software_sdk").strip().lower().replace("-", "_")
    out = []
    for ch in value:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    crate = "".join(out).strip("_") or "system_software_sdk"
    if crate[0].isdigit():
        crate = f"sdk_{crate}"
    return crate


def _app_names(state: Dict[str, Any]) -> List[str]:
    apps = state.get("app_names") or []
    if isinstance(apps, str):
        apps = [x.strip() for x in apps.split(",") if x.strip()]
    if not isinstance(apps, list):
        return ["health_app", "diag_cli"]
    out = [str(x).strip() for x in apps if str(x).strip()]
    return out or ["health_app", "diag_cli"]


def _public_groups(api_contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups = api_contract.get("public_api_groups") or []
    return [g for g in groups if isinstance(g, dict)]


def _rust_type_for_return(ret: str) -> str:
    mapping = {
        "u8": "u8",
        "u16": "u16",
        "u32": "u32",
        "u64": "u64",
        "DeviceStatus": "DeviceStatus",
        "DmaStatus": "DmaStatus",
        "BootState": "BootState",
        "PlatformInfo": "PlatformInfo",
        "candidate": "String",
    }
    if ret in mapping:
        return mapping[ret]
    if ret.startswith("Result<"):
        return ret
    if ret == "Result<(), SoftwareError>":
        return ret
    return "String"


def _method_signature(method: Dict[str, Any]) -> str:
    name = method.get("name") or "unnamed"
    inputs = method.get("inputs") or []
    rendered_inputs = ["&self"]
    for raw in inputs:
        raw = str(raw)
        if ":" in raw:
            rendered_inputs.append(raw)
        else:
            rendered_inputs.append(f"{raw}: String")
    ret = _rust_type_for_return(str(method.get("returns") or "String"))
    if ret.startswith("Result<"):
        return f"    pub fn {name}({', '.join(rendered_inputs)}) -> {ret} {{\n        Err(SoftwareError::Unimplemented(\"{name}\".into()))\n    }}"
    if ret == "String":
        body = "String::new()"
    elif ret in ("u8", "u16", "u32", "u64"):
        body = "0"
    else:
        body = f"{ret}::default()"
    return f"    pub fn {name}({', '.join(rendered_inputs)}) -> Result<{ret}, SoftwareError> {{\n        Ok({body})\n    }}"


def _render_rust_lib(crate_name: str, api_contract: Dict[str, Any]) -> str:
    groups = _public_groups(api_contract)
    methods: List[str] = []
    seen_names = set()

    for group in groups:
        for method in group.get("methods") or []:
            if isinstance(method, dict):
                name = str(method.get("name") or "").strip()
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                methods.append(_method_signature(method))

    # Always provide a stable platform probe for examples / CLI / diagnostics.
    if "platform_probe" not in seen_names:
        methods.append(
            _method_signature({
                "name": "platform_probe",
                "inputs": [],
                "returns": "PlatformInfo",
            })
        )

    methods_block = (
        "\n\n".join(methods)
        if methods
        else "    pub fn platform_probe(&self) -> Result<PlatformInfo, SoftwareError> {\n"
             "        Ok(PlatformInfo::default())\n"
             "    }"
    )

    return f'''use serde::{{Deserialize, Serialize}};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DeviceConfig {{
    pub profile: String,
}}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DeviceStatus {{
    pub healthy: bool,
}}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DmaDescriptor {{
    pub channel: String,
}}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DmaToken {{
    pub id: String,
}}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DmaStatus {{
    pub complete: bool,
}}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PlatformInfo {{
    pub name: String,
}}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct BootState {{
    pub stage: String,
}}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ClockMode {{
    LowPower,
    Nominal,
    Performance,
}}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum PowerMode {{
    Sleep,
    Idle,
    Active,
}}

pub type InterruptHandler = fn();
pub type RuntimeBackend = String;

#[derive(Debug, thiserror::Error)]
pub enum SoftwareError {{
    #[error("unimplemented: {{0}}")]
    Unimplemented(String),
}}

#[derive(Debug, Default, Clone)]
pub struct Sdk {{}}

impl Sdk {{
    pub fn new() -> Self {{
        Self {{}}
    }}

{methods_block}
}}
'''




def _render_rust_cargo(crate_name: str) -> str:
    return f'''[package]
name = "{crate_name}"
version = "0.1.0"
edition = "2021"
license = "MIT"
description = "Generated System_Software Rust SDK"

[dependencies]
serde = {{ version = "1", features = ["derive"] }}
thiserror = "1"
clap = {{ version = "4", features = ["derive"] }}
'''


def _render_example_app(crate_name: str) -> str:
    return f'''use {crate_name}::Sdk;

fn main() {{
    let sdk = Sdk::new();
    let _ = sdk.get_status();
    println!("system software example app initialized");
}}
'''


def _render_cli(crate_name: str) -> str:
    return f'''use clap::{{Parser, Subcommand}};
use {crate_name}::Sdk;

#[derive(Parser)]
#[command(author, version, about)]
struct Cli {{
    #[command(subcommand)]
    command: Commands,
}}

#[derive(Subcommand)]
enum Commands {{
    Status,
    Probe,
}}

fn main() {{
    let cli = Cli::parse();
    let sdk = Sdk::new();
    match cli.command {{
        Commands::Status => println!("{{:?}}", sdk.get_status()),
        Commands::Probe => println!("{{:?}}", sdk.platform_probe()),
    }}
}}
'''


def _render_config_toml() -> str:
    return '''[system]
profile = "default"
log_level = "info"
'''


def _build_manifest(state: Dict[str, Any], api_contract: Dict[str, Any], crate_name: str, files: List[str]) -> Dict[str, Any]:
    return {
        "package_type": "system_software_sdk_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": api_contract.get("source_workflow_id"),
        "target_language": (api_contract.get("api_style") or {}).get("public_api_language") or "rust",
        "build_system": state.get("build_system") or "cargo",
        "sdk_style": state.get("sdk_style") or "rust_crate",
        "crate_name": crate_name,
        "app_names": _app_names(state),
        "files": files,
    }


def _markdown_summary(manifest: Dict[str, Any]) -> str:
    lines = [
        "# System Software SDK Scaffold Summary",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}",
        f"- Target language: `{manifest.get('target_language')}`",
        f"- Build system: `{manifest.get('build_system')}`",
        f"- Crate name: `{manifest.get('crate_name')}`",
        "",
        "## Generated files",
        "",
    ]
    for path in manifest.get("files") or []:
        lines.append(f"- `{path}`")
    return "\n".join(lines) + "\n"


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if isinstance(state.get("workflow_dir"), str) and state.get("workflow_dir").strip() else ""
    print(f"\n🛠️ Running {AGENT_NAME}")

    api_contract = _load_api_contract(state, workflow_dir)
    if not api_contract:
        state["status"] = "❌ system software API contract missing"
        return state

    target_language = str((state.get("target_language") or (api_contract.get("api_style") or {}).get("public_api_language") or "rust")).lower()
    build_system = str(state.get("build_system") or "cargo").lower()
    sdk_style = str(state.get("sdk_style") or "rust_crate").lower()
    if target_language != "rust" or build_system != "cargo":
        state["status"] = "❌ current SDK scaffold agent is intentionally Rust/Cargo-only for this stabilization phase"
        return state

    crate_name = _sanitize_crate_name(state.get("project_name") or "system_software_sdk")

    files_to_write = {
        f"{crate_name}/Cargo.toml": _render_rust_cargo(crate_name),
        f"{crate_name}/src/lib.rs": _render_rust_lib(crate_name, api_contract),
        f"{crate_name}/examples/basic.rs": _render_example_app(crate_name),
        f"{crate_name}/src/bin/diag_cli.rs": _render_cli(crate_name),
        f"{crate_name}/config/default.toml": _render_config_toml(),
    }

    written_files: List[str] = []
    for rel_path, content in files_to_write.items():
        subdir, filename = rel_path.rsplit("/", 1)
        _record_text(workflow_id, filename, content, subdir=f"{OUTPUT_SUBDIR}/{subdir}")
        written_files.append(f"{OUTPUT_SUBDIR}/{rel_path}")

    manifest = _build_manifest(state, api_contract, crate_name, written_files)
    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "crate_name": crate_name,
        "written_file_count": len(written_files),
        "sdk_style": sdk_style,
        "build_system": build_system,
    }

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_sdk_manifest"] = manifest
    state["system_software_sdk_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software SDK scaffold generated"
    return state
