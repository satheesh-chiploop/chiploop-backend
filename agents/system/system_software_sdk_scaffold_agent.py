import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software SDK Scaffold Agent"
OUTPUT_SUBDIR = "system/software/sdk"
MANIFEST_JSON = "system_software_sdk_manifest.json"
README_MD = "README.md"
BUILD_JSON = "build_manifest.json"
PUBLIC_HEADER = "include/system_software_sdk.h"
RUST_LIB = "src/lib.rs"
EXAMPLE_C = "examples/example_app.c"


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _record_text(workflow_id: str, filename: str, content: str) -> Optional[str]:
    subdir = OUTPUT_SUBDIR
    name = filename
    if "/" in filename:
        subdir = f"{OUTPUT_SUBDIR}/{filename.rsplit('/', 1)[0]}"
        name = filename.rsplit("/", 1)[1]
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=name,
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
    if isinstance(contract, dict):
        return contract
    for candidate in (
        state.get("system_software_api_contract_path"),
        "system/software/sdk/system_software_api_contract.json",
    ):
        if isinstance(candidate, str) and candidate.strip():
            obj = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if obj:
                return obj
    return {}


def _c_method_decl(method: Dict[str, Any]) -> str:
    name = method.get("name") or "unnamed"
    inputs = method.get("inputs") or []
    if not inputs:
        return f"int {name}(void);"
    params = ", ".join([f"const void* {inp}" for inp in inputs])
    return f"int {name}({params});"


def _render_public_header(api_contract: Dict[str, Any]) -> str:
    lines = [
        "#pragma once",
        "",
        "/* Auto-generated System Software SDK public header */",
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
    ]

    for group in api_contract.get("public_api_groups") or []:
        lines.append(f"/* {group.get('name')} */")
        for method in group.get("methods") or []:
            lines.append(_c_method_decl(method))
        lines.append("")

    lines.extend([
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
    ])
    return "\n".join(lines)


def _render_rust_lib(api_contract: Dict[str, Any]) -> str:
    lines = [
        "// Auto-generated System Software SDK Rust facade",
        "",
    ]
    for group in api_contract.get("public_api_groups") or []:
        lines.append(f"pub mod {group.get('name')} {{")
        for method in group.get("methods") or []:
            fn_name = str(method.get("name") or "unnamed")
            args = ", ".join([f"_{inp}: usize" for inp in (method.get('inputs') or [])])
            if not args:
                args = ""
            lines.append(f"    pub fn {fn_name}({args}) -> i32 {{")
            lines.append("        0")
            lines.append("    }")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def _render_example(api_contract: Dict[str, Any]) -> str:
    first_group = (api_contract.get("public_api_groups") or [{}])[0]
    methods = first_group.get("methods") or []
    first_call = None
    if methods:
        method = methods[0]
        args = ", ".join(["0"] * len(method.get("inputs") or []))
        first_call = f'    int rc = {method.get("name")}({args});\n    return rc;'
    else:
        first_call = "    return 0;"
    return (
        '#include "system_software_sdk.h"\n\n'
        "int main(void) {\n"
        f"{first_call}\n"
        "}\n"
    )


def _render_readme(api_contract: Dict[str, Any]) -> str:
    groups = api_contract.get("public_api_groups") or []
    lines = [
        "# System Software SDK Scaffold",
        "",
        "This SDK scaffold is generated from the system software API contract.",
        "",
        "## Public API groups",
        "",
    ]
    for group in groups:
        lines.append(f"- `{group.get('name')}`: {group.get('description')}")
    lines.extend([
        "",
        "## Layout",
        "",
        "- `include/` public C header façade",
        "- `src/` Rust façade stubs",
        "- `examples/` sample application",
        "",
    ])
    return "\n".join(lines)


def _build_manifest(api_contract: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "package_type": "system_software_sdk_scaffold",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": api_contract.get("source_workflow_id"),
        "sdk_layout": {
            "public_header": f"{OUTPUT_SUBDIR}/{PUBLIC_HEADER}",
            "rust_lib": f"{OUTPUT_SUBDIR}/{RUST_LIB}",
            "example_app": f"{OUTPUT_SUBDIR}/{EXAMPLE_C}",
        },
        "api_groups": [group.get("name") for group in (api_contract.get("public_api_groups") or [])],
    }


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = state.get("workflow_dir")
    print(f"\n🧱 Running {AGENT_NAME}")

    if not workflow_dir:
        state["status"] = "❌ workflow_dir missing for SDK scaffold"
        return state

    api_contract = _load_api_contract(state, os.path.abspath(workflow_dir))
    if not api_contract:
        state["status"] = "❌ API contract missing"
        return state

    public_header = _render_public_header(api_contract)
    rust_lib = _render_rust_lib(api_contract)
    example_c = _render_example(api_contract)
    readme = _render_readme(api_contract)
    manifest = _build_manifest(api_contract)
    build_manifest = {
        "build_systems": ["cmake_or_make", "cargo_optional"],
        "primary_public_header": f"{OUTPUT_SUBDIR}/{PUBLIC_HEADER}",
        "primary_rust_lib": f"{OUTPUT_SUBDIR}/{RUST_LIB}",
    }

    _record_text(workflow_id, PUBLIC_HEADER, public_header)
    _record_text(workflow_id, RUST_LIB, rust_lib)
    _record_text(workflow_id, EXAMPLE_C, example_c)
    _record_text(workflow_id, README_MD, readme)
    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, BUILD_JSON, json.dumps(build_manifest, indent=2))

    state["system_software_sdk_manifest"] = manifest
    state["system_software_sdk_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software SDK scaffold generated"
    return state
