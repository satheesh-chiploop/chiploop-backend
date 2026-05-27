import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Verification Handoff Ingest Agent"
ARTIFACT_BUCKET = "artifacts"


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


def _download(client: Any, path: str) -> Optional[bytes]:
    try:
        return client.storage.from_(ARTIFACT_BUCKET).download(path)
    except Exception:
        return None


def _write_local(workflow_dir: str, rel_path: str, raw: bytes) -> str:
    target = Path(workflow_dir) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return str(target.resolve())


def _first_download(client: Any, candidates: List[str]) -> tuple[str, bytes]:
    for path in dict.fromkeys(candidates):
        raw = _download(client, path)
        if raw:
            return path, raw
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


def _list_storage_tree(client: Any, folder: str, depth: int = 0, max_depth: int = 6) -> List[str]:
    if depth > max_depth:
        return []
    paths: List[str] = []
    for path in _list_storage_folder(client, folder):
        paths.append(path)
        paths.extend(_list_storage_tree(client, path, depth + 1, max_depth))
    return paths


def _first_local(workflow_id: str, subdir: str, suffixes: tuple[str, ...]) -> tuple[str, bytes]:
    base = Path("backend") / "workflows" / workflow_id / subdir
    if not base.exists():
        return "", b""
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.name.lower().endswith(suffixes):
            return str(path.resolve()), path.read_bytes()
    return "", b""


def _source_local_roots(client: Any, workflow_id: str) -> List[Path]:
    roots: List[Path] = [Path("backend") / "workflows" / workflow_id]
    try:
        response = (
            client.table("runs")
            .select("artifacts_path")
            .eq("workflow_id", workflow_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        rows = response.data or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if not isinstance(row, dict) or not row.get("artifacts_path"):
                continue
            root = Path(str(row["artifacts_path"]))
            roots.extend([root / "arch2rtl", root])
    except Exception:
        pass
    return roots


def _first_local_in_roots(roots: List[Path], suffixes: tuple[str, ...]) -> tuple[str, bytes]:
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name.lower().endswith(suffixes):
                return str(path.resolve()), path.read_bytes()
    return "", b""


def _expected_rtl_names(spec: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    if isinstance(spec.get("rtl_output_file"), str):
        names.append(os.path.basename(str(spec["rtl_output_file"])).lower())
    hierarchy = spec.get("hierarchy")
    if isinstance(hierarchy, dict):
        modules = [hierarchy.get("top_module")] + list(hierarchy.get("modules") or [])
        for module in modules:
            if isinstance(module, dict) and isinstance(module.get("rtl_output_file"), str):
                names.append(os.path.basename(str(module["rtl_output_file"])).lower())
    return list(dict.fromkeys(names))


def _top_module(spec: Dict[str, Any], rtl_path: str) -> str:
    hierarchy = spec.get("hierarchy")
    if isinstance(hierarchy, dict):
        top = hierarchy.get("top_module")
        if isinstance(top, dict) and str(top.get("name") or "").strip():
            return str(top["name"]).strip()
    if str(spec.get("name") or "").strip():
        return str(spec["name"]).strip()
    return Path(rtl_path).stem


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    source_mode = str(state.get("rtl_source_mode") or "").strip()
    source_workflow_id = str(state.get("from_workflow_id") or "").strip()
    if source_mode != "from_arch2rtl" or not source_workflow_id:
        raise RuntimeError(
            "Verification requires a completed Arch2RTL workflow source. "
            "Choose Use Arch2RTL output and provide its workflow ID."
        )

    client = state.get("supabase_client")
    if client is None:
        raise RuntimeError("supabase_client missing while importing Arch2RTL verification source")

    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    state["workflow_dir"] = workflow_dir

    source_response = (
        client.table("workflows")
        .select("id,artifacts")
        .eq("id", source_workflow_id)
        .single()
        .execute()
    )
    source_row = source_response.data or {}
    indexed_paths = _storage_paths(source_row.get("artifacts") or {})
    prefix = f"backend/workflows/{source_workflow_id}"
    stored_spec_paths = _list_storage_folder(client, f"{prefix}/spec")
    stored_source_paths = _list_storage_tree(client, prefix)

    spec_candidates = [
        path for path in indexed_paths + stored_spec_paths
        if path.lower().endswith("_spec.json") and "/spec/" in path.lower()
    ]
    spec_candidates.extend([
        path for path in indexed_paths + stored_spec_paths
        if path.lower().endswith("_spec.json")
    ])
    spec_source_path, spec_raw = _first_download(client, spec_candidates)
    if not spec_raw:
        spec_source_path, spec_raw = _first_local(source_workflow_id, "spec", ("_spec.json",))
    if not spec_raw:
        raise RuntimeError(
            f"No generated digital spec artifact found in Arch2RTL workflow {source_workflow_id}. "
            "Run Arch2RTL again after deploying the current backend if this is an older workflow."
        )
    try:
        spec = json.loads(spec_raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Imported Arch2RTL digital spec is invalid JSON: {exc}") from exc

    available_paths = list(dict.fromkeys(indexed_paths + stored_source_paths))
    expected_rtl_names = set(_expected_rtl_names(spec))
    rtl_candidates = [
        path for path in available_paths
        if os.path.basename(path).lower() in expected_rtl_names
        and path.lower().endswith((".sv", ".v"))
    ]
    rtl_candidates.extend([
        path for path in available_paths
        if path.lower().endswith((".sv", ".v")) and "/rtl/" in path.lower()
    ])
    rtl_files: List[str] = []
    imported_rtl_paths: List[str] = []
    for source_path in dict.fromkeys(rtl_candidates):
        raw = _download(client, source_path)
        if not raw:
            continue
        rtl_name = os.path.basename(source_path)
        local_rtl = _write_local(workflow_dir, f"handoff/rtl/{rtl_name}", raw)
        rtl_files.append(local_rtl)
        imported_rtl_paths.append(source_path)
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "verification/handoff/rtl", rtl_name,
            raw.decode("utf-8", errors="replace"),
        )
    if not rtl_files:
        local_rtl_path, local_rtl_raw = _first_local_in_roots(
            _source_local_roots(client, source_workflow_id), (".sv", ".v")
        )
        if local_rtl_raw:
            rtl_name = os.path.basename(local_rtl_path)
            local_rtl = _write_local(workflow_dir, f"handoff/rtl/{rtl_name}", local_rtl_raw)
            rtl_files.append(local_rtl)
            imported_rtl_paths.append(local_rtl_path)
            save_text_artifact_and_record(
                workflow_id, AGENT_NAME, "verification/handoff/rtl", rtl_name,
                local_rtl_raw.decode("utf-8", errors="replace"),
            )
    if not rtl_files:
        raise RuntimeError(f"No generated RTL artifact found in Arch2RTL workflow {source_workflow_id}")

    spec_name = os.path.basename(spec_source_path) or "source_spec.json"
    local_spec = _write_local(workflow_dir, f"spec/{spec_name}", spec_raw)
    save_text_artifact_and_record(
        workflow_id, AGENT_NAME, "verification/handoff/spec", spec_name,
        json.dumps(spec, indent=2),
    )

    top_module = str(state.get("top_module") or "").strip() or _top_module(spec, rtl_files[0])
    manifest = {
        "type": "digital_verification_source_handoff",
        "source_workflow_id": source_workflow_id,
        "spec_source_path": spec_source_path,
        "rtl_source_paths": imported_rtl_paths,
        "top_module": top_module,
        "local_spec_path": local_spec,
        "local_rtl_files": rtl_files,
    }
    save_text_artifact_and_record(
        workflow_id, AGENT_NAME, "verification/handoff", "verification_source_handoff.json",
        json.dumps(manifest, indent=2),
    )

    state["spec_json"] = local_spec
    state["digital_spec_json"] = local_spec
    state["rtl_files"] = rtl_files
    state["rtl_inputs"] = rtl_files
    state["top_module"] = top_module
    state["verification_source_handoff"] = manifest
    state["status"] = "Imported Arch2RTL spec and RTL for digital verification."
    return state
