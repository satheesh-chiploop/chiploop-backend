import json
import os
from pathlib import Path
from typing import Any, Dict

from utils.artifact_utils import save_text_artifact_and_record

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
    }
    loaded = {key: _read_json(path) if path else {} for key, path in files.items()}

    out_dir = workflow_dir / "verify_closure"
    manifest = {
        "type": "verify_closure_ingest",
        "source_verify_workflow_id": source_workflow_id,
        "source_workflow_dir": str(source_dir),
        "found_files": {key: str(path) if path else None for key, path in files.items()},
        "coverage_targets": state.get("coverage_targets"),
        "seed_count": state.get("seed_count"),
        "toolchain": state.get("toolchain") if isinstance(state.get("toolchain"), dict) else {},
    }
    for key, value in loaded.items():
        state[f"source_{key}"] = value

    text = json.dumps(manifest, indent=2)
    _write(out_dir / "verify_closure_ingest.json", text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "verify_closure_ingest.json", text)

    state["source_verify_workflow_dir"] = str(source_dir)
    state["verify_closure_ingest_json"] = str(out_dir / "verify_closure_ingest.json")
    return state
