import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from ._embedded_common import ensure_workflow_dir, write_artifact
from ._rtl_top_utils import apply_resolved_top, resolve_rtl_top_from_files
from agents.digital.clock_intent import build_clock_intent

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


def _all_local_in_roots(roots: List[Path], suffixes: tuple[str, ...]) -> List[tuple[str, bytes]]:
    out: List[tuple[str, bytes]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not path.name.lower().endswith(suffixes):
                continue
            norm = str(path.resolve())
            if norm in seen:
                continue
            seen.add(norm)
            out.append((norm, path.read_bytes()))
    return out


def _read_first_text(paths: List[str]) -> str:
    for path in paths:
        try:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
    return ""


def _module_names_from_text(text: str) -> List[str]:
    names: List[str] = []
    for match in re.finditer(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text or ""):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _rtl_preference(path: str) -> tuple[int, str]:
    stem = Path(path).stem.lower()
    is_intermediate = bool(re.search(r"(?:^|_)pass\d+$", stem) or re.search(r"_pass\d+(?:_|$)", stem))
    return (1 if is_intermediate else 0, Path(path).name.lower())


def _dedupe_downloaded_rtl(items: List[tuple[str, bytes]]) -> List[tuple[str, bytes]]:
    unique_by_path: Dict[str, tuple[str, bytes]] = {}
    for path, raw in items:
        if path and raw:
            unique_by_path.setdefault(path.replace("\\", "/"), (path, raw))
    ordered = list(unique_by_path.values())
    modules_by_path: Dict[str, List[str]] = {}
    for path, raw in ordered:
        modules_by_path[path] = _module_names_from_text(raw.decode("utf-8", errors="replace"))
    owner_by_module: Dict[str, str] = {}
    for path, modules in modules_by_path.items():
        for module in modules:
            prior = owner_by_module.get(module)
            if not prior or _rtl_preference(path) < _rtl_preference(prior):
                owner_by_module[module] = path
    out: List[tuple[str, bytes]] = []
    for path, raw in ordered:
        modules = modules_by_path.get(path) or []
        if modules and not any(owner_by_module.get(module) == path for module in modules):
            continue
        out.append((path, raw))
    return out


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
    stored_source_paths = _list_storage_tree(client, prefix)

    rtl_candidates = [
        path for path in indexed_paths + stored_source_paths
        if path.lower().endswith((".sv", ".v")) and "/rtl/" in path.replace("\\", "/").lower()
    ]
    if requested_top:
        rtl_candidates.extend([
            f"{prefix}/rtl/{requested_top}.sv",
            f"{prefix}/rtl/{requested_top}.v",
        ])
    regmap_candidates = [
        path for path in indexed_paths + stored_source_paths
        if path.lower().endswith("digital_regmap.json")
    ]
    regmap_candidates.append(f"{prefix}/digital/digital_regmap.json")
    sdc_candidates = [
        path for path in indexed_paths + stored_source_paths
        if path.lower().endswith(".sdc")
    ]

    rtl_downloads: List[tuple[str, bytes]] = []
    for candidate in list(dict.fromkeys(rtl_candidates)):
        raw = _first_download(client, [candidate])[1]
        if raw:
            rtl_downloads.append((candidate, raw))
    regmap_source_path, regmap_raw = _first_download(client, list(dict.fromkeys(regmap_candidates)))
    if not rtl_downloads:
        rtl_downloads = _all_local_in_roots(_source_local_roots(client, source_workflow_id), (".sv", ".v"))
    if not regmap_raw:
        regmap_source_path, regmap_raw = _first_local(source_workflow_id, "", ("digital_regmap.json",))
    rtl_downloads = _dedupe_downloaded_rtl(rtl_downloads)
    if not rtl_downloads:
        raise RuntimeError(f"No generated RTL artifact found in Arch2RTL workflow {source_workflow_id}")
    if not regmap_raw:
        raise RuntimeError(f"No generated register map found in Arch2RTL workflow {source_workflow_id}")

    local_rtl_files: List[str] = []
    rtl_source_paths: List[str] = []
    for idx, (rtl_source_path, rtl_raw) in enumerate(rtl_downloads):
        rtl_name = os.path.basename(rtl_source_path) or f"{requested_top or 'rtl'}_{idx}.sv"
        if not rtl_name.lower().endswith((".sv", ".v")):
            rtl_name = f"{rtl_name}.sv"
        local_rtl_files.append(_write_local(workflow_dir, f"digital/rtl/{rtl_name}", rtl_raw))
        rtl_source_paths.append(rtl_source_path)
    primary_rtl = next(
        (
            path for path in local_rtl_files
            if requested_top and requested_top in _module_names_from_text(Path(path).read_text(encoding="utf-8", errors="replace"))
        ),
        local_rtl_files[0],
    )
    source_top = requested_top or Path(primary_rtl).stem
    local_regmap = _write_local(workflow_dir, "digital/digital_regmap.json", regmap_raw)
    local_sdc_files: List[str] = []
    for source_path in dict.fromkeys(sdc_candidates):
        sdc_raw = _first_download(client, [source_path])[1]
        if not sdc_raw:
            continue
        local_sdc_files.append(_write_local(workflow_dir, f"digital/constraints/{os.path.basename(source_path)}", sdc_raw))
        if len(local_sdc_files) >= 4:
            break
    source_top, rtl_top_debug = resolve_rtl_top_from_files(source_top, local_rtl_files)
    try:
        digital_regmap = json.loads(regmap_raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Imported Arch2RTL register map is invalid JSON: {exc}") from exc

    state["top_module"] = source_top
    state["rtl_top"] = source_top
    state["rtl_inputs"] = list(local_rtl_files)
    state["system_rtl_files"] = list(local_rtl_files)
    state["soc_top_sim_module"] = source_top
    state["soc_top_sim_path"] = os.path.relpath(primary_rtl, workflow_dir).replace("\\", "/")
    state["system_top_sim_path"] = os.path.relpath(primary_rtl, workflow_dir).replace("\\", "/")
    apply_resolved_top(state, source_top)
    state["digital_regmap"] = digital_regmap
    state["digital_regmap_path"] = local_regmap
    state["digital_register_map_path"] = local_regmap
    state.setdefault("digital", {}).update({
        "regmap": digital_regmap,
        "digital_regmap": digital_regmap,
        "digital_regmap_path": local_regmap,
        "rtl_files": list(local_rtl_files),
    })
    clock_intent = build_clock_intent(
        spec={},
        rtl_files=local_rtl_files,
        sdc_text=_read_first_text(local_sdc_files),
        requested=state.get("clock_constraints") or state.get("clocks"),
    )
    state["clock_intent"] = clock_intent
    state.setdefault("digital", {})["clock_intent"] = clock_intent
    if local_sdc_files:
        state["constraints_sdc"] = local_sdc_files[0]
        state.setdefault("digital", {})["constraints_sdc"] = local_sdc_files[0]

    rtl_package = {
        "package_type": "system_rtl",
        "package_version": "1.0",
        "source_workflow_id": source_workflow_id,
        "source_verification_workflow_id": state.get("source_verification_workflow_id"),
        "top": {"sim": source_top, "phys": source_top},
        "filelists": {"sim": list(local_rtl_files), "phys": list(local_rtl_files), "libs": []},
        "register_map_path": "digital/digital_regmap.json",
        "clock_intent_path": "digital/timing/clock_intent.json",
        "clock_intent": clock_intent,
        "ready_for_cosim": True,
        "compile": {"sim": "imported_from_verified_digital_flow"},
        "notes": [
            "RTL and register map were imported from the specified Arch2RTL workflow.",
            "Verification evidence remains associated with source_verification_workflow_id when supplied.",
        ],
    }
    for local_rtl in local_rtl_files:
        rel_rtl = os.path.relpath(local_rtl, workflow_dir).replace("\\", "/")
        write_artifact(state, rel_rtl, Path(local_rtl).read_text(encoding="utf-8", errors="replace"), key=os.path.basename(local_rtl))
    write_artifact(state, "digital/digital_regmap.json", json.dumps(digital_regmap, indent=2), key="digital_regmap.json")
    write_artifact(state, "digital/timing/clock_intent.json", json.dumps(clock_intent, indent=2), key="clock_intent.json")
    write_artifact(state, RTL_PACKAGE_PATH, json.dumps(rtl_package, indent=2), key="system_rtl_package.json")
    write_artifact(state, DEBUG_PATH, json.dumps({
        "source_workflow_id": source_workflow_id,
        "source_verification_workflow_id": state.get("source_verification_workflow_id"),
        "rtl_source_paths": rtl_source_paths,
        "rtl_top_resolution": rtl_top_debug,
        "regmap_source_path": regmap_source_path,
        "local_sdc_files": local_sdc_files,
        "clock_intent": clock_intent,
        "local_rtl_paths": local_rtl_files,
        "local_regmap_path": local_regmap,
    }, indent=2), key="digital_handoff_ingest.json")

    state["system_rtl_package"] = rtl_package
    state["status"] = "Imported Arch2RTL RTL and register map for Embedded firmware generation."
    return state
