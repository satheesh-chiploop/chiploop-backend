import json
import os
from pathlib import Path
from typing import Any, Dict, List

from ._embedded_common import ensure_workflow_dir, write_artifact

AGENT_NAME = "Embedded Digital RTL Handoff Ingest Agent"
ARTIFACT_BUCKET = "artifacts"
DEBUG_PATH = "firmware/handoff/digital_handoff_ingest.json"
RTL_PACKAGE_PATH = "system/package/system_rtl_package.json"


def _storage_paths(artifacts: Any) -> List[str]:
    paths: List[str] = []
    if isinstance(artifacts, dict):
        for value in artifacts.values():
            paths.extend(_storage_paths(value))
    elif isinstance(artifacts, list):
        for value in artifacts:
            paths.extend(_storage_paths(value))
    elif isinstance(artifacts, str):
        paths.append(artifacts.replace("\\", "/"))
    return paths


def _download(client, path: str) -> bytes:
    return client.storage.from_(ARTIFACT_BUCKET).download(path)


def _write_local(workflow_dir: str, rel_path: str, raw: bytes) -> str:
    absolute = Path(workflow_dir) / Path(rel_path)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(raw)
    return str(absolute.resolve())


def _first_download(client, paths: List[str]) -> tuple[str, bytes]:
    for path in paths:
        try:
            return path, _download(client, path)
        except Exception:
            continue
    return "", b""


def _list_storage_folder(client: Any, folder: str) -> List[str]:
    try:
        entries = client.storage.from_(ARTIFACT_BUCKET).list(folder) or []
    except Exception:
        return []
    paths: List[str] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            paths.append(f"{folder.rstrip('/')}/{name}")
    return paths


def _first_local(workflow_id: str, subdir: str, suffixes: tuple[str, ...]) -> tuple[str, bytes]:
    base = Path("backend") / "workflows" / workflow_id / subdir
    if not base.exists():
        return "", b""
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.name.lower().endswith(suffixes):
            return str(path.resolve()), path.read_bytes()
    return "", b""


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    source_workflow_id = str(state.get("from_workflow_id") or state.get("source_digital_workflow_id") or "").strip()
    if not source_workflow_id:
        state["status"] = "Embedded digital handoff not requested; using standalone firmware input."
        return state

    workflow_dir = ensure_workflow_dir(state)
    client = state.get("supabase_client")
    if client is None:
        raise RuntimeError("supabase_client missing while importing Arch2RTL handoff")

    requested_top = str(state.get("top_module") or "").strip()
    response = (
        client.table("workflows")
        .select("id,artifacts")
        .eq("id", source_workflow_id)
        .single()
        .execute()
    )
    source_row = response.data or {}
    indexed_paths = _storage_paths(source_row.get("artifacts") or {})
    prefix = f"backend/workflows/{source_workflow_id}"
    stored_rtl_paths = _list_storage_folder(client, f"{prefix}/rtl")
    stored_digital_paths = _list_storage_folder(client, f"{prefix}/digital")

    rtl_candidates = [
        path for path in indexed_paths + stored_rtl_paths
        if path.lower().endswith((".sv", ".v")) and "/rtl/" in path.replace("\\", "/").lower()
    ]
    if requested_top:
        rtl_candidates.extend([
            f"{prefix}/rtl/{requested_top}.sv",
            f"{prefix}/rtl/{requested_top}.v",
        ])
    regmap_candidates = [
        path for path in indexed_paths + stored_digital_paths
        if path.lower().endswith("digital_regmap.json")
    ]
    regmap_candidates.append(f"{prefix}/digital/digital_regmap.json")

    rtl_source_path, rtl_raw = _first_download(client, list(dict.fromkeys(rtl_candidates)))
    regmap_source_path, regmap_raw = _first_download(client, list(dict.fromkeys(regmap_candidates)))
    if not rtl_raw:
        rtl_source_path, rtl_raw = _first_local(source_workflow_id, "rtl", (".sv", ".v"))
    if not regmap_raw:
        regmap_source_path, regmap_raw = _first_local(source_workflow_id, "", ("digital_regmap.json",))
    if not rtl_raw:
        raise RuntimeError(f"No generated RTL artifact found in Arch2RTL workflow {source_workflow_id}")
    if not regmap_raw:
        raise RuntimeError(f"No generated register map found in Arch2RTL workflow {source_workflow_id}")

    rtl_name = os.path.basename(rtl_source_path) or f"{requested_top or 'top'}.sv"
    source_top = requested_top or Path(rtl_name).stem
    local_rtl = _write_local(workflow_dir, f"digital/rtl/{rtl_name}", rtl_raw)
    local_regmap = _write_local(workflow_dir, "digital/digital_regmap.json", regmap_raw)
    try:
        digital_regmap = json.loads(regmap_raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Imported Arch2RTL register map is invalid JSON: {exc}") from exc

    state["top_module"] = source_top
    state["rtl_top"] = source_top
    state["rtl_inputs"] = [local_rtl]
    state["system_rtl_files"] = [local_rtl]
    state["soc_top_sim_module"] = source_top
    state["soc_top_sim_path"] = f"digital/rtl/{rtl_name}"
    state["system_top_sim_path"] = f"digital/rtl/{rtl_name}"
    state["digital_regmap"] = digital_regmap
    state["digital_regmap_path"] = local_regmap
    state["digital_register_map_path"] = local_regmap
    state.setdefault("digital", {}).update({
        "regmap": digital_regmap,
        "digital_regmap": digital_regmap,
        "digital_regmap_path": local_regmap,
        "rtl_files": [local_rtl],
    })

    rtl_package = {
        "package_type": "system_rtl",
        "package_version": "1.0",
        "source_workflow_id": source_workflow_id,
        "source_verification_workflow_id": state.get("source_verification_workflow_id"),
        "top": {"sim": source_top, "phys": source_top},
        "filelists": {"sim": [local_rtl], "phys": [local_rtl], "libs": []},
        "register_map_path": "digital/digital_regmap.json",
        "ready_for_cosim": True,
        "compile": {"sim": "imported_from_verified_digital_flow"},
        "notes": [
            "RTL and register map were imported from the specified Arch2RTL workflow.",
            "Verification evidence remains associated with source_verification_workflow_id when supplied.",
        ],
    }
    write_artifact(state, "digital/rtl/" + rtl_name, rtl_raw.decode("utf-8", errors="replace"), key=rtl_name)
    write_artifact(state, "digital/digital_regmap.json", json.dumps(digital_regmap, indent=2), key="digital_regmap.json")
    write_artifact(state, RTL_PACKAGE_PATH, json.dumps(rtl_package, indent=2), key="system_rtl_package.json")
    write_artifact(state, DEBUG_PATH, json.dumps({
        "source_workflow_id": source_workflow_id,
        "source_verification_workflow_id": state.get("source_verification_workflow_id"),
        "rtl_source_path": rtl_source_path,
        "regmap_source_path": regmap_source_path,
        "local_rtl_path": local_rtl,
        "local_regmap_path": local_regmap,
    }, indent=2), key="digital_handoff_ingest.json")

    state["system_rtl_package"] = rtl_package
    state["status"] = "Imported Arch2RTL RTL and register map for Embedded firmware generation."
    return state
