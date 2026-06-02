import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital RTL Handoff Ingest Agent"
ARTIFACT_BUCKET = "artifacts"
RTL_EXTENSIONS = (".sv", ".v", ".svh", ".vh")
MAX_RTL_FILES = 512
MAX_RTL_FILE_BYTES = 5 * 1024 * 1024
MAX_RTL_TOTAL_BYTES = 50 * 1024 * 1024


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


def _write_local(workflow_dir: str, rel_path: str, raw: bytes) -> str:
    safe_rel = _safe_rel_path(rel_path)
    target = Path(workflow_dir) / safe_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return str(target.resolve())


def _safe_rel_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized or normalized in {".", ".."}:
        raise RuntimeError("Invalid empty RTL artifact path")
    parts = Path(normalized).parts
    if any(part in {"..", ""} for part in parts) or Path(normalized).is_absolute():
        raise RuntimeError(f"Unsafe RTL artifact path: {path}")
    return normalized


def _json_load(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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


def _find_local_files(roots: Iterable[Path], predicate) -> List[Path]:
    found: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and predicate(path):
                found.append(path)
    return found


def _artifact_candidates(client: Any, source_workflow_id: str) -> Tuple[List[str], List[str], List[str]]:
    response = (
        client.table("workflows")
        .select("artifacts")
        .eq("id", source_workflow_id)
        .single()
        .execute()
    )
    artifacts = (response.data or {}).get("artifacts") or {}
    paths = _storage_paths(artifacts)
    prefix = f"backend/workflows/{source_workflow_id}"
    paths.extend(_list_storage_tree(client, prefix))

    unique = list(dict.fromkeys(paths))
    rtl = [p for p in unique if p.lower().endswith(RTL_EXTENSIONS)]
    spec = [
        p for p in unique
        if p.lower().endswith(".json") and ("spec" in os.path.basename(p).lower() or "/spec/" in p.lower())
    ]
    regmap = [
        p for p in unique
        if p.lower().endswith(".json") and any(token in p.lower() for token in ("regmap", "register_map", "digital_regmap"))
    ]
    return rtl, spec, regmap


def _copy_supabase_arch2rtl(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    client = state.get("supabase_client")
    upstream = state.get("upstream_workflows") if isinstance(state.get("upstream_workflows"), dict) else {}
    source_workflow_id = (
        state.get("from_workflow_id")
        or state.get("source_arch2rtl_workflow_id")
        or upstream.get("arch2rtl")
        or state.get("source_workflow_id")
    )
    if not client:
        raise RuntimeError("Supabase client is required for Arch2RTL handoff ingest")
    if not source_workflow_id:
        raise RuntimeError("from_workflow_id is required for Arch2RTL handoff ingest")

    rtl_paths, spec_paths, regmap_paths = _artifact_candidates(client, str(source_workflow_id))
    roots = _source_local_roots(client, str(source_workflow_id))

    imported_rtl: List[str] = []
    for path in rtl_paths:
        raw = _download(client, path)
        if not raw:
            continue
        imported_rtl.append(_write_local(workflow_dir, f"handoff/rtl/{os.path.basename(path)}", raw))

    if not imported_rtl:
        for local_path in _find_local_files(roots, lambda p: p.name.lower().endswith(RTL_EXTENSIONS)):
            raw = local_path.read_bytes()
            imported_rtl.append(_write_local(workflow_dir, f"handoff/rtl/{local_path.name}", raw))

    if not imported_rtl:
        raise RuntimeError(f"No RTL artifact found in Arch2RTL workflow {source_workflow_id}")

    spec_json = _first_json_from_storage_or_local(client, spec_paths, roots, ("_spec.json", "digital_spec.json"))
    regmap_json = _first_json_from_storage_or_local(client, regmap_paths, roots, ("digital_regmap.json", "regmap.json", "register_map.json"))

    if spec_json:
        spec_path = _write_local(workflow_dir, "handoff/spec/digital_spec.json", json.dumps(spec_json, indent=2).encode("utf-8"))
        state["spec_json"] = spec_json
        state["digital_spec_json"] = spec_json
        state["spec_json_path"] = spec_path
    if regmap_json:
        regmap_path = _write_local(workflow_dir, "handoff/spec/digital_regmap.json", json.dumps(regmap_json, indent=2).encode("utf-8"))
        state["digital_regmap_json"] = regmap_json
        state["regmap_json"] = regmap_json
        state["digital_regmap_json_path"] = regmap_path

    return {
        "source_mode": "from_arch2rtl",
        "source_workflow_id": str(source_workflow_id),
        "rtl_files": imported_rtl,
        "spec_imported": bool(spec_json),
        "regmap_imported": bool(regmap_json),
    }


def _first_json_from_storage_or_local(
    client: Any,
    storage_paths: List[str],
    local_roots: List[Path],
    preferred_names: Tuple[str, ...],
) -> Optional[Dict[str, Any]]:
    def priority(path: str) -> int:
        base = os.path.basename(path).lower()
        for idx, name in enumerate(preferred_names):
            if base == name.lower() or base.endswith(name.lower()):
                return idx
        return len(preferred_names)

    for path in sorted(dict.fromkeys(storage_paths), key=priority):
        raw = _download(client, path)
        if raw:
            data = _json_load(raw)
            if data is not None:
                return data

    local_files = _find_local_files(
        local_roots,
        lambda p: p.name.lower().endswith(".json")
        and any(token.replace(".json", "") in p.name.lower() for token in preferred_names),
    )
    for path in local_files:
        data = _json_load(path.read_bytes())
        if data is not None:
            return data
    return None


def _copy_pasted_rtl(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    files = state.get("pasted_rtl_files") or []
    if isinstance(files, dict):
        files = [files]
    if not isinstance(files, list) or not files:
        raise RuntimeError("No pasted RTL files were provided")

    imported: List[str] = []
    for index, item in enumerate(files):
        if isinstance(item, str):
            name = f"pasted_{index + 1}.sv"
            content = item
        elif isinstance(item, dict):
            name = item.get("path") or item.get("name") or f"pasted_{index + 1}.sv"
            content = item.get("content") or item.get("rtl") or ""
        else:
            continue
        if not str(name).lower().endswith(RTL_EXTENSIONS):
            name = f"{Path(str(name)).stem or f'pasted_{index + 1}'}.sv"
        raw = str(content).encode("utf-8")
        _enforce_size_limits([(str(name), raw)])
        imported.append(_write_local(workflow_dir, f"handoff/rtl/{os.path.basename(str(name))}", raw))
    if not imported:
        raise RuntimeError("No valid pasted RTL files were provided")
    return {"source_mode": "paste", "rtl_files": imported}


def _copy_repo_rtl(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    repo_path = str(state.get("repo_path") or "").strip()
    if not repo_path:
        raise RuntimeError("repo_path is required for repo-based RTL ingest")

    source_root: Path
    if _is_git_url(repo_path):
        source_root = _clone_git_repo(repo_path, state, workflow_dir)
    else:
        source_root = _validate_local_source_path(Path(repo_path).expanduser())

    repo_subdir = str(state.get("repo_subdir") or "").strip().strip("/\\")
    if repo_subdir:
        source_root = (source_root / repo_subdir).resolve()
        if not source_root.exists() or not source_root.is_dir():
            raise RuntimeError(f"repo_subdir does not exist: {repo_subdir}")

    if source_root.is_file():
        candidates = [source_root] if source_root.name.lower().endswith(RTL_EXTENSIONS) else []
        rel_root = source_root.parent
    else:
        candidates = [p for p in sorted(source_root.rglob("*")) if p.is_file() and p.name.lower().endswith(RTL_EXTENSIONS)]
        rel_root = source_root
    if not candidates:
        raise RuntimeError(f"No RTL files found under {source_root}")

    payloads: List[Tuple[str, bytes]] = [(str(p.relative_to(rel_root)), p.read_bytes()) for p in candidates]
    _enforce_size_limits(payloads)

    imported: List[str] = []
    for rel, raw in payloads:
        imported.append(_write_local(workflow_dir, f"handoff/rtl/{rel}", raw))
    return {"source_mode": "repo_path", "repo_path": repo_path, "repo_subdir": repo_subdir, "rtl_files": imported}


def _is_git_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("https://") and (lowered.endswith(".git") or "github.com/" in lowered or "gitlab.com/" in lowered)


def _clone_git_repo(repo_url: str, state: Dict[str, Any], workflow_dir: str) -> Path:
    if not (tool_path("git", state) or shutil.which("git")):
        raise RuntimeError("git is not installed on this backend, so repo-based RTL ingest cannot clone URLs")
    if not repo_url.lower().startswith("https://"):
        raise RuntimeError("Only HTTPS git repository URLs are supported for backend RTL ingest")
    dest = Path(workflow_dir) / "source_repo"
    cmd = ["git", "clone", "--depth", "1"]
    ref = str(state.get("repo_ref") or "").strip()
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend([repo_url, str(dest)])
    proc = run_command(state, "rtl_handoff_git_clone", cmd, cwd=workflow_dir, timeout_sec=300)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {(proc.stderr or proc.stdout)[-1200:]}")
    return dest.resolve()


def _validate_local_source_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.exists():
        raise RuntimeError(f"repo_path does not exist: {path}")
    forbidden = {Path("/"), Path("/root"), Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/var")}
    if os.name == "nt":
        forbidden = {Path(resolved.anchor)}
    if resolved in forbidden:
        raise RuntimeError(f"Refusing to scan unsafe repo_path: {resolved}")
    return resolved


def _enforce_size_limits(files: List[Tuple[str, bytes]]) -> None:
    if len(files) > MAX_RTL_FILES:
        raise RuntimeError(f"Too many RTL files ({len(files)}); limit is {MAX_RTL_FILES}")
    total = 0
    for name, raw in files:
        if len(raw) > MAX_RTL_FILE_BYTES:
            raise RuntimeError(f"RTL file is too large: {name}")
        total += len(raw)
    if total > MAX_RTL_TOTAL_BYTES:
        raise RuntimeError("RTL source set is too large for handoff ingest")


def _infer_top_module(state: Dict[str, Any], rtl_files: List[str]) -> str:
    if state.get("top_module"):
        return str(state["top_module"])
    spec = state.get("spec_json") or state.get("digital_spec_json") or {}
    if isinstance(spec, dict):
        for key in ("top_module", "module_name", "name"):
            if isinstance(spec.get(key), str) and spec[key].strip():
                return spec[key].strip()
        hierarchy = spec.get("hierarchy")
        if isinstance(hierarchy, dict) and isinstance(hierarchy.get("top_module"), str):
            return hierarchy["top_module"].strip()
    for path in rtl_files:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)
        if match:
            return match.group(1)
    return ""


def _existing_rtl_files(state: Dict[str, Any], workflow_dir: str) -> List[str]:
    existing = state.get("rtl_files")
    if isinstance(existing, list) and existing:
        return [str(p) for p in existing]
    return [str(p.resolve()) for p in Path(workflow_dir).rglob("*") if p.is_file() and p.name.lower().endswith(RTL_EXTENSIONS)]


def _record_outputs(state: Dict[str, Any], workflow_dir: str, manifest: Dict[str, Any]) -> None:
    workflow_id = str(state.get("workflow_id") or "")
    if not workflow_id:
        return
    for path in manifest.get("rtl_files") or []:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = Path(path).relative_to(Path(workflow_dir)).as_posix()
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/handoff/rtl", os.path.basename(rel), text)
    save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        "digital/handoff",
        "rtl_handoff_ingest_manifest.json",
        json.dumps(manifest, indent=2),
    )


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_dir = state.get("workflow_dir") or state.get("artifact_dir") or os.getcwd()
    Path(workflow_dir).mkdir(parents=True, exist_ok=True)
    mode = str(state.get("rtl_source_mode") or "").strip().lower()

    if mode == "from_arch2rtl":
        manifest = _copy_supabase_arch2rtl(state, workflow_dir)
    elif mode == "paste":
        manifest = _copy_pasted_rtl(state, workflow_dir)
    elif mode == "repo_path":
        manifest = _copy_repo_rtl(state, workflow_dir)
    else:
        rtl_files = _existing_rtl_files(state, workflow_dir)
        if not rtl_files and str(state.get("app_name") or "").lower() in {"dqa", "smoke", "integrate"}:
            raise RuntimeError("Select an RTL source before running this workflow")
        manifest = {"source_mode": "existing_workflow" if rtl_files else "no_source_selected", "rtl_files": rtl_files}

    rtl_files = [str(Path(p).resolve()) for p in manifest.get("rtl_files") or []]
    state["workflow_dir"] = workflow_dir
    state["rtl_files"] = rtl_files
    state["rtl_inputs"] = rtl_files
    state["source_rtl_files"] = rtl_files
    top_module = _infer_top_module(state, rtl_files)
    if top_module:
        state["top_module"] = top_module
        manifest["top_module"] = top_module

    manifest.update({
        "agent": AGENT_NAME,
        "workflow_id": state.get("workflow_id"),
        "run_id": state.get("run_id"),
        "parent_workflow_id": state.get("parent_workflow_id"),
        "source_arch2rtl_workflow_id": state.get("source_arch2rtl_workflow_id") or state.get("from_workflow_id"),
        "upstream_workflows": state.get("upstream_workflows") if isinstance(state.get("upstream_workflows"), dict) else {},
        "rtl_file_count": len(rtl_files),
        "status": "ok",
    })
    state["digital_rtl_handoff_manifest"] = manifest
    _record_outputs(state, workflow_dir, manifest)
    return state
