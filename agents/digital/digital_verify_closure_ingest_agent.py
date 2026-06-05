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


def _storage_json_by_filename(source_workflow_id: str, filename: str) -> tuple[str | None, Dict[str, Any]]:
    prefix = f"backend/workflows/{source_workflow_id}"
    for path in _list_storage_tree(prefix):
        if Path(path).name == filename:
            obj = _download_json(path)
            if obj:
                return path, obj
    return None, {}


def _storage_text_by_filename(source_workflow_id: str, filename: str) -> tuple[str | None, str]:
    prefix = f"backend/workflows/{source_workflow_id}"
    for path in _list_storage_tree(prefix):
        if Path(path).name == filename:
            text = _download_text(path)
            if text:
                return path, text
    return None, ""


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    workflow_dir.mkdir(parents=True, exist_ok=True)
    state["workflow_dir"] = str(workflow_dir)

    source_workflow_id = str(state.get("source_verify_workflow_id") or state.get("parent_workflow_id") or "").strip()
    if not source_workflow_id:
        raise RuntimeError("source_verify_workflow_id is required for Verify closure analysis")

    source_dir = Path("backend") / "workflows" / source_workflow_id
    files = {
        "simulation_summary_coverage": _find(source_dir, "simulation_summary_coverage.json"),
        "simulation_execution_summary": _find(source_dir, "simulation_execution_summary.json"),
        "functional_coverage_summary": _find(source_dir, "functional_coverage_summary.json"),
        "code_coverage_summary": _find(source_dir, "code_coverage_summary.json"),
        "coverage_spec": _find(source_dir, "coverage_spec.json"),
        "testcases": _find(source_dir, "testcases.json"),
        "verification_plan": _find(source_dir, "verification_plan.md"),
        "monitor_checker_plan": _find(source_dir, "monitor_checker_plan.md"),
        "coverage_point_plan": _find(source_dir, "coverage_point_plan.md"),
    }
    storage_files: Dict[str, str | None] = {}
    filename_by_key = {
        "simulation_summary_coverage": "simulation_summary_coverage.json",
        "simulation_execution_summary": "simulation_execution_summary.json",
        "functional_coverage_summary": "functional_coverage_summary.json",
        "code_coverage_summary": "code_coverage_summary.json",
        "coverage_spec": "coverage_spec.json",
        "testcases": "testcases.json",
    }
    loaded: Dict[str, Dict[str, Any]] = {}
    for key, filename in filename_by_key.items():
        storage_path, obj = _storage_json_by_filename(source_workflow_id, filename)
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

    out_dir = workflow_dir / "verify_closure"
    manifest = {
        "type": "verify_closure_ingest",
        "source_verify_workflow_id": source_workflow_id,
        "source_workflow_dir": str(source_dir),
        "found_files": {key: str(path) if path else None for key, path in files.items()},
        "found_storage_files": storage_files,
        "found_text_storage_files": text_storage_files,
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
