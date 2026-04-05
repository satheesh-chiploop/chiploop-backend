import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software CLI / Tooling Agent"
OUTPUT_SUBDIR = "system/software/tools"
MANIFEST_JSON = "system_software_tools_manifest.json"
SUMMARY_MD = "system_software_tools_summary.md"
DEBUG_JSON = "system_software_tools_debug.json"


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


def _load_app_manifest(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_application_manifest")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (
        state.get("system_software_application_manifest_path"),
        "system/software/apps/system_software_application_manifest.json",
    ):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, str(candidate)))
            if loaded:
                return loaded
    return {}


def _load_sdk_manifest(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_sdk_manifest")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (
        state.get("system_software_sdk_manifest_path"),
        "system/software/sdk/system_software_sdk_manifest.json",
    ):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, str(candidate)))
            if loaded:
                return loaded
    return {}


def _crate_name(value: str) -> str:
    text = (value or "system_software_sdk").strip().lower().replace("-", "_")
    out = []
    for ch in text:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    crate = "".join(out).strip("_") or "system_software_sdk"
    if crate[0].isdigit():
        crate = f"sdk_{crate}"
    return crate


def _tool_names(state: Dict[str, Any]) -> List[str]:
    raw = state.get("tool_names") or ["reg_viewer", "diag_cli"]
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    if not isinstance(raw, list):
        return ["reg_viewer", "diag_cli"]
    out = [str(x).strip() for x in raw if str(x).strip()]
    return out or ["reg_viewer", "diag_cli"]


def _render_tool_main(crate_name: str, tool_name: str) -> str:
    if tool_name == "reg_viewer":
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
    Read {{ register: String }},
    Write {{ register: String, value: u32 }},
}}

fn main() {{
    let cli = Cli::parse();
    let sdk = Sdk::new();
    match cli.command {{
        Commands::Read {{ register }} => println!("{{:?}}", sdk.read_register(&register)),
        Commands::Write {{ register, value }} => println!("{{:?}}", sdk.write_register(&register, value)),
    }}
}}
'''
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


def _render_tool_cargo(crate_name: str, tool_name: str) -> str:
    return f'''[package]
name = "{tool_name}"
version = "0.1.0"
edition = "2021"

[dependencies]
clap = {{ version = "4", features = ["derive"] }}
{crate_name} = {{ path = "../../sdk/{crate_name}" }}
'''


def _build_manifest(source_workflow_id: str, crate_name: str, tool_names: List[str], files: List[str]) -> Dict[str, Any]:
    return {{
        "package_type": "system_software_tools_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "crate_name": crate_name,
        "tool_names": tool_names,
        "files": files,
    }}


def _markdown_summary(manifest: Dict[str, Any]) -> str:
    lines = [
        "# System Software Tooling",
        "",
        f"- Generated at: {{manifest.get('generated_at')}}",
        f"- Source workflow id: {{manifest.get('source_workflow_id') or 'unavailable'}}",
        f"- SDK crate name: `{{manifest.get('crate_name')}}`",
        "",
        "## Tools",
        "",
    ]
    for name in manifest.get("tool_names") or []:
        lines.append(f"- `{{name}}`")
    lines.extend(["", "## Files", ""])
    for item in manifest.get("files") or []:
        lines.append(f"- `{{item}}`")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""

    app_manifest = _load_app_manifest(state, workflow_dir)
    sdk_manifest = _load_sdk_manifest(state, workflow_dir)
    if not app_manifest:
        state["status"] = "❌ system software application manifest missing"
        return state
    if not sdk_manifest:
        state["status"] = "❌ system software sdk manifest missing"
        return state

    crate_name = _crate_name(str(sdk_manifest.get("crate_name") or "system_software_sdk"))
    tool_names = _tool_names(state)
    written: List[str] = []

    for tool_name in tool_names:
        _record_text(workflow_id, "Cargo.toml", _render_tool_cargo(crate_name, tool_name), subdir=f"{{OUTPUT_SUBDIR}}/{{tool_name}}")
        written.append(f"{{OUTPUT_SUBDIR}}/{{tool_name}}/Cargo.toml")

        _record_text(workflow_id, "main.rs", _render_tool_main(crate_name, tool_name), subdir=f"{{OUTPUT_SUBDIR}}/{{tool_name}}/src")
        written.append(f"{{OUTPUT_SUBDIR}}/{{tool_name}}/src/main.rs")

    manifest = _build_manifest(
        source_workflow_id=str(app_manifest.get("source_workflow_id") or ""),
        crate_name=crate_name,
        tool_names=tool_names,
        files=written,
    )

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "tool_names": tool_names,
        "crate_name": crate_name,
        "written_file_count": len(written),
    }, indent=2))

    state["system_software_tools_manifest"] = manifest
    state["system_software_tools_manifest_path"] = f"{{OUTPUT_SUBDIR}}/{{MANIFEST_JSON}}"
    state["status"] = "✅ System software tooling generated"
    return state
