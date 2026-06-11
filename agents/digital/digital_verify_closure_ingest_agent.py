import json
import os
from pathlib import Path
from typing import Any, Dict

from utils.artifact_utils import ARTIFACT_BUCKET, save_text_artifact_and_record, supabase

AGENT_NAME = "Digital Verify Closure Ingest Agent"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _find(parent_dir: Path, filename: str) -> Path | None:
    if not parent_dir.exists():
        return None
    for path in sorted(parent_dir.rglob(filename)):
        if path.is_file():
            return path
    return None


def _find_any(parent_dir: Path, filenames: list[str]) -> Path | None:
    for filename in filenames:
        found = _find(parent_dir, filename)
        if found:
            return found
    return None


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _list_storage_folder(folder: str) -> list[str]:
    try:
        entries = supabase.storage.from_(ARTIFACT_BUCKET).list(folder) or []
    except Exception:
        return []
    out: list[str] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            out.append(f"{folder.rstrip('/')}/{name}")
    return out


def _list_storage_tree(folder: str, depth: int = 0, max_depth: int = 6) -> list[str]:
    if depth > max_depth:
        return []
    paths: list[str] = []
    for path in _list_storage_folder(folder):
        if "." in Path(path).name:
            paths.append(path)
        else:
            paths.extend(_list_storage_tree(path, depth + 1, max_depth))
    return paths


def _download_json(path: str) -> Dict[str, Any]:
    try:
        raw = supabase.storage.from_(ARTIFACT_BUCKET).download(path)
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _download_text(path: str) -> str:
    try:
        raw = supabase.storage.from_(ARTIFACT_BUCKET).download(path)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    except Exception:
        return ""


def _indexed_storage_paths_for_workflow(source_workflow_id: str) -> list[str]:
    paths: list[str] = []
    user_id = ""
    try:
        row = (
            supabase.table("workflows")
            .select("id,user_id,artifacts")
            .eq("id", source_workflow_id)
            .single()
            .execute()
        ).data or {}
        user_id = str(row.get("user_id") or "").strip()

        def _collect(obj: Any) -> None:
            if isinstance(obj, dict):
                for value in obj.values():
                    _collect(value)
            elif isinstance(obj, list):
                for value in obj:
                    _collect(value)
            elif isinstance(obj, str):
                value = obj.strip().replace("\\", "/")
                if value:
                    paths.append(value)

        _collect(row.get("artifacts") or {})
    except Exception:
        pass

    prefixes = [f"backend/workflows/{source_workflow_id}"]
    if user_id:
        prefixes.extend([
            f"artifacts/{user_id}/{source_workflow_id}",
            f"{user_id}/{source_workflow_id}",
        ])
    for prefix in prefixes:
        paths.extend(_list_storage_tree(prefix))
    return list(dict.fromkeys(paths))


def _storage_json_by_filename(source_workflow_id: str, filename: str) -> tuple[str | None, Dict[str, Any]]:
    for path in _indexed_storage_paths_for_workflow(source_workflow_id):
        if Path(path).name == filename:
            obj = _download_json(path)
            if obj:
                return path, obj
    return None, {}


def _storage_json_by_filenames(source_workflow_id: str, filenames: list[str]) -> tuple[str | None, Dict[str, Any]]:
    for filename in filenames:
        path, obj = _storage_json_by_filename(source_workflow_id, filename)
        if obj:
            return path, obj
    return None, {}


def _storage_text_by_filename(source_workflow_id: str, filename: str) -> tuple[str | None, str]:
    for path in _indexed_storage_paths_for_workflow(source_workflow_id):
        if Path(path).name == filename:
            text = _download_text(path)
            if text:
                return path, text
    return None, ""


def _storage_text_by_suffix(source_workflow_id: str, suffix: str) -> tuple[str | None, str]:
    wanted = suffix.strip().replace("\\", "/").lstrip("/")
    if not wanted:
        return None, ""
    for path in _indexed_storage_paths_for_workflow(source_workflow_id):
        norm = path.replace("\\", "/")
        if norm.endswith(wanted) or Path(norm).name == Path(wanted).name:
            text = _download_text(path)
            if text:
                return path, text
    return None, ""


def _read_filelist_paths(filelist_path: Path) -> list[str]:
    out: list[str] = []
    try:
        for raw in filelist_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("+"):
                continue
            if line.startswith("-"):
                continue
            out.append(line)
    except Exception:
        pass
    return list(dict.fromkeys(out))


def _resolve_filelist_entry(filelist_path: Path, entry: str) -> str:
    candidate = Path(str(entry).strip())
    if candidate.is_absolute():
        return str(candidate)
    if candidate.is_file():
        return str(candidate.resolve())
    return str((filelist_path.parent / candidate).resolve())


def _materialize_storage_text(path: Path, content: str) -> Path | None:
    if not content:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
    except Exception:
        return None


def _materialize_system_rtl_from_storage(
    source_workflow_id: str,
    workflow_dir: Path,
    filelist_text: str,
) -> tuple[Path | None, list[str], dict[str, str]]:
    if not filelist_text:
        return None, [], {}

    integration_dir = workflow_dir / "system" / "integration"
    imported_dir = workflow_dir / "system" / "imported_rtl"
    integration_dir.mkdir(parents=True, exist_ok=True)
    imported_dir.mkdir(parents=True, exist_ok=True)

    materialized: list[str] = []
    source_paths: dict[str, str] = {}
    resolved_lines: list[str] = []
    for raw in filelist_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("+") or line.startswith("-"):
            continue
        suffix = line.replace("\\", "/").lstrip("/")
        storage_path, rtl_text = _storage_text_by_suffix(source_workflow_id, suffix)
        if not rtl_text:
            storage_path, rtl_text = _storage_text_by_filename(source_workflow_id, Path(suffix).name)
        if rtl_text:
            dest = imported_dir / Path(suffix).name
            written = _materialize_storage_text(dest, rtl_text)
            if written:
                materialized.append(str(written))
                source_paths[str(written)] = storage_path or ""
                resolved_lines.append(str(written))
                continue
        resolved_lines.append(line)

    filelist_path = integration_dir / "system_rtl_filelist_sim.txt"
    _materialize_storage_text(filelist_path, "\n".join(resolved_lines) + ("\n" if resolved_lines else ""))
    return filelist_path, materialized, source_paths


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    workflow_dir.mkdir(parents=True, exist_ok=True)
    state["workflow_dir"] = str(workflow_dir)

    source_workflow_id = str(state.get("source_verify_workflow_id") or state.get("parent_workflow_id") or "").strip()
    if not source_workflow_id:
        raise RuntimeError("source_verify_workflow_id is required for Verify closure analysis")

    source_dir = Path("backend") / "workflows" / source_workflow_id
    file_candidates = {
        "simulation_summary_coverage": ["simulation_summary_coverage.json", "system_sim_dashboard.json"],
        "simulation_execution_summary": ["simulation_execution_summary.json", "system_sim_execution.json"],
        "functional_coverage_summary": ["functional_coverage_summary.json"],
        "code_coverage_summary": ["code_coverage_summary.json"],
        "coverage_spec": ["coverage_spec.json"],
        "testcases": ["testcases.json"],
        "simulation_manifest": ["simulation_manifest.json"],
        "tb_contract": ["tb_contract.json"],
        "verification_source_handoff": ["verification_source_handoff.json"],
    }
    files = {
        "simulation_summary_coverage": _find_any(source_dir, file_candidates["simulation_summary_coverage"]),
        "simulation_execution_summary": _find_any(source_dir, file_candidates["simulation_execution_summary"]),
        "functional_coverage_summary": _find(source_dir, "functional_coverage_summary.json"),
        "code_coverage_summary": _find(source_dir, "code_coverage_summary.json"),
        "coverage_spec": _find(source_dir, "coverage_spec.json"),
        "testcases": _find(source_dir, "testcases.json"),
        "simulation_manifest": _find(source_dir, "simulation_manifest.json"),
        "tb_contract": _find(source_dir, "tb_contract.json"),
        "verification_source_handoff": _find(source_dir, "verification_source_handoff.json"),
        "verification_plan": _find(source_dir, "verification_plan.md"),
        "monitor_checker_plan": _find(source_dir, "monitor_checker_plan.md"),
        "coverage_point_plan": _find(source_dir, "coverage_point_plan.md"),
    }
    storage_files: Dict[str, str | None] = {}
    filename_by_key = {key: filenames[0] for key, filenames in file_candidates.items()}
    loaded: Dict[str, Dict[str, Any]] = {}
    for key, filename in filename_by_key.items():
        storage_path, obj = _storage_json_by_filenames(source_workflow_id, file_candidates.get(key, [filename]))
        storage_files[key] = storage_path
        if obj:
            loaded[key] = obj
        else:
            loaded[key] = _read_json(files.get(key)) if files.get(key) else {}

    text_artifacts: Dict[str, str] = {}
    text_storage_files: Dict[str, str | None] = {}
    for key, filename in {
        "verification_plan": "verification_plan.md",
        "monitor_checker_plan": "monitor_checker_plan.md",
        "coverage_point_plan": "coverage_point_plan.md",
    }.items():
        storage_path, text_value = _storage_text_by_filename(source_workflow_id, filename)
        text_storage_files[key] = storage_path
        if not text_value and files.get(key):
            try:
                text_value = Path(files[key]).read_text(encoding="utf-8")
            except Exception:
                text_value = ""
        if text_value:
            text_artifacts[key] = text_value

    system_rtl_filelist = _find(source_dir, "system_rtl_filelist_sim.txt")
    system_top_sim = _find(source_dir, "soc_top_sim.sv")
    storage_system_rtl_filelist: str | None = None
    storage_soc_top_sim: str | None = None
    materialized_rtl_files: list[str] = []
    materialized_rtl_sources: dict[str, str] = {}

    if not system_rtl_filelist:
        storage_system_rtl_filelist, filelist_text = _storage_text_by_filename(source_workflow_id, "system_rtl_filelist_sim.txt")
        if not filelist_text:
            manifest_rtl = loaded.get("simulation_manifest", {}).get("rtl_files")
            contract_rtl = loaded.get("tb_contract", {}).get("rtl_files")
            rtl_items = manifest_rtl if isinstance(manifest_rtl, list) and manifest_rtl else contract_rtl
            if isinstance(rtl_items, list) and rtl_items:
                filelist_text = "\n".join(str(item) for item in rtl_items if str(item).strip())
        if filelist_text:
            system_rtl_filelist, materialized_rtl_files, materialized_rtl_sources = _materialize_system_rtl_from_storage(
                source_workflow_id,
                workflow_dir,
                filelist_text,
            )

    if not system_top_sim:
        storage_soc_top_sim, soc_top_text = _storage_text_by_filename(source_workflow_id, "soc_top_sim.sv")
        if not soc_top_text:
            for key in ("simulation_manifest", "tb_contract"):
                top_path = str(loaded.get(key, {}).get("soc_top_sim_path") or loaded.get(key, {}).get("top_sv_path") or "").strip()
                if top_path:
                    storage_soc_top_sim, soc_top_text = _storage_text_by_suffix(source_workflow_id, top_path)
                    if soc_top_text:
                        break
        if soc_top_text:
            system_top_sim = _materialize_storage_text(
                workflow_dir / "system" / "integration" / "soc_top_sim.sv",
                soc_top_text,
            )

    if system_rtl_filelist:
        state["system_rtl_filelist_sim"] = str(system_rtl_filelist)
        state["source_system_rtl_filelist_sim"] = str(system_rtl_filelist)
        rtl_from_filelist = [
            _resolve_filelist_entry(system_rtl_filelist, p)
            for p in _read_filelist_paths(system_rtl_filelist)
        ]
        rtl_from_filelist = [p for p in rtl_from_filelist if p]
        if rtl_from_filelist:
            state["system_rtl_files"] = rtl_from_filelist
            state["rtl_inputs"] = rtl_from_filelist
            state["rtl_files"] = rtl_from_filelist
            state["source_rtl_files"] = rtl_from_filelist
            digital_state = state.setdefault("digital", {})
            if isinstance(digital_state, dict):
                digital_state["rtl_files"] = rtl_from_filelist
    if system_top_sim:
        state["soc_top_sim_path"] = str(system_top_sim)
        state["source_soc_top_sim_path"] = str(system_top_sim)
    for key in ("simulation_manifest", "tb_contract"):
        top_module = str(loaded.get(key, {}).get("top_module") or "").strip()
        if top_module:
            state["soc_top_sim_module"] = top_module
            state["top_module"] = top_module
            break

    out_dir = workflow_dir / "verify_closure"
    manifest = {
        "type": "verify_closure_ingest",
        "source_verify_workflow_id": source_workflow_id,
        "source_workflow_dir": str(source_dir),
        "found_files": {key: str(path) if path else None for key, path in files.items()},
        "found_storage_files": storage_files,
        "found_text_storage_files": text_storage_files,
        "system_rtl_filelist_sim": str(system_rtl_filelist) if system_rtl_filelist else None,
        "soc_top_sim_path": str(system_top_sim) if system_top_sim else None,
        "storage_system_rtl_filelist_sim": storage_system_rtl_filelist,
        "storage_soc_top_sim_path": storage_soc_top_sim,
        "materialized_rtl_files": materialized_rtl_files,
        "materialized_rtl_sources": materialized_rtl_sources,
        "coverage_targets": state.get("coverage_targets"),
        "seed_count": state.get("seed_count"),
        "toolchain": state.get("toolchain") if isinstance(state.get("toolchain"), dict) else {},
    }
    for key, value in loaded.items():
        state[f"source_{key}"] = value
    for key, value in text_artifacts.items():
        state[f"source_{key}"] = value

    text = json.dumps(manifest, indent=2)
    _write(out_dir / "verify_closure_ingest.json", text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "verify_closure_ingest.json", text)
    for key, value in loaded.items():
        if not value:
            continue
        filename = filename_by_key.get(key) or f"{key}.json"
        payload = json.dumps(value, indent=2)
        _write(out_dir / "source_verify_artifacts" / filename, payload)
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "verify_closure/source_verify_artifacts",
            filename,
            payload,
        )
    for key, payload in text_artifacts.items():
        filename = {
            "verification_plan": "verification_plan.md",
            "monitor_checker_plan": "monitor_checker_plan.md",
            "coverage_point_plan": "coverage_point_plan.md",
        }.get(key, f"{key}.md")
        _write(out_dir / "source_verify_artifacts" / filename, payload)
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "verify_closure/source_verify_artifacts",
            filename,
            payload,
        )

    state["source_verify_workflow_dir"] = str(source_dir)
    state["verify_closure_ingest_json"] = str(out_dir / "verify_closure_ingest.json")
    return state
