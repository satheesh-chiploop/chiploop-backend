import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


RTL_EXTENSIONS = {".v", ".sv", ".vh", ".svh"}
REPORT_EXTENSIONS = {".rpt", ".log", ".txt", ".json"}
ARTIFACT_BUCKET = "artifacts"


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except Exception:
        return str(value)


def normalize_pasted_files(value: Any) -> List[Tuple[str, str]]:
    files: List[Tuple[str, str]] = []
    if isinstance(value, list):
        for idx, item in enumerate(value):
            if isinstance(item, dict):
                content = as_text(item.get("content") or item.get("text"))
                path = str(item.get("path") or item.get("name") or f"pasted_{idx}.sv")
                if content.strip():
                    files.append((path, content))
            elif isinstance(item, str) and item.strip():
                files.append((f"pasted_{idx}.sv", item))
    elif isinstance(value, dict):
        for key, content in value.items():
            text = as_text(content)
            if text.strip():
                files.append((str(key), text))
    return files


def read_text_file(path: Path, max_bytes: int = 800_000) -> str:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _storage_paths(value: Any) -> List[str]:
    paths: List[str] = []
    if isinstance(value, dict):
        for child in value.values():
            paths.extend(_storage_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_storage_paths(child))
    elif isinstance(value, str):
        paths.append(value.replace("\\", "/"))
    return paths


def _list_storage_tree(client: Any, folder: str, depth: int = 0, max_depth: int = 5) -> List[str]:
    if depth > max_depth:
        return []
    try:
        entries = client.storage.from_(ARTIFACT_BUCKET).list(folder) or []
    except Exception:
        return []
    paths: List[str] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            continue
        path = f"{folder.rstrip('/')}/{name}"
        paths.append(path)
        paths.extend(_list_storage_tree(client, path, depth + 1, max_depth))
    return paths


def _download_text(client: Any, path: str, max_bytes: int = 1_000_000) -> str:
    try:
        raw = client.storage.from_(ARTIFACT_BUCKET).download(path)
    except Exception:
        return ""
    if not raw or len(raw) > max_bytes:
        return ""
    return raw.decode("utf-8", errors="replace")


def collect_source_storage_texts(state: Dict[str, Any], predicate, limit: int = 32) -> List[Dict[str, str]]:
    client = state.get("supabase_client")
    upstream = state.get("upstream_workflows") if isinstance(state.get("upstream_workflows"), dict) else {}
    source_workflow_id = (
        state.get("from_workflow_id")
        or state.get("source_workflow_id")
        or state.get("source_arch2rtl_workflow_id")
        or upstream.get("arch2rtl")
        or upstream.get("synthesis")
        or upstream.get("arch2synthesis")
        or upstream.get("tapeout")
    )
    if not client or not source_workflow_id:
        return []
    try:
        row = (
            client.table("workflows")
            .select("artifacts")
            .eq("id", str(source_workflow_id))
            .single()
            .execute()
        ).data or {}
    except Exception:
        row = {}
    paths = _storage_paths(row.get("artifacts") or {})
    paths.extend(_list_storage_tree(client, f"backend/workflows/{source_workflow_id}"))
    selected: List[Dict[str, str]] = []
    for path in dict.fromkeys(paths):
        if not predicate(path.lower()):
            continue
        content = _download_text(client, path)
        if content.strip():
            selected.append({"path": path, "content": content})
        if len(selected) >= limit:
            break
    return selected


def _local_roots(state: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for key in ("workflow_dir", "artifact_dir"):
        value = str(state.get(key) or "").strip()
        if value:
            roots.append(Path(value))
    workflow_id = str(state.get("workflow_id") or "").strip()
    if workflow_id:
        roots.append(Path("backend") / "workflows" / workflow_id)
    return roots


def collect_rtl_sources(state: Dict[str, Any]) -> List[Dict[str, str]]:
    sources: List[Dict[str, str]] = []
    for key in ("rtl_text", "pasted_rtl", "verilog_text", "systemverilog_text"):
        text = as_text(state.get(key))
        if text.strip():
            sources.append({"path": f"{key}.sv", "content": text})

    for path, content in normalize_pasted_files(state.get("pasted_rtl_files")):
        sources.append({"path": path, "content": content})

    repo_path = str(state.get("repo_path") or "").strip()
    repo_subdir = str(state.get("repo_subdir") or "").strip()
    if repo_path:
        root = Path(repo_path)
        if repo_subdir:
            root = root / repo_subdir
        if root.exists():
            for file_path in root.rglob("*"):
                if file_path.suffix.lower() in RTL_EXTENSIONS:
                    content = read_text_file(file_path)
                    if content.strip():
                        sources.append({"path": str(file_path), "content": content})

    for root in _local_roots(state):
        if not root.exists():
            continue
        for file_path in root.rglob("*"):
            lowered = str(file_path).lower()
            if file_path.suffix.lower() in RTL_EXTENSIONS and any(token in lowered for token in ("rtl", "source", "handoff")):
                content = read_text_file(file_path)
                if content.strip():
                    sources.append({"path": str(file_path), "content": content})

    sources.extend(collect_source_storage_texts(
        state,
        lambda path: path.endswith(tuple(RTL_EXTENSIONS)) and any(token in path for token in ("rtl", "source", "handoff", "mbist")),
        limit=64,
    ))

    seen = set()
    unique: List[Dict[str, str]] = []
    for item in sources:
        key = (item["path"], item["content"][:200])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:64]


def collect_sdc_text(state: Dict[str, Any]) -> str:
    direct = as_text(state.get("constraints_sdc") or state.get("sdc_text"))
    if direct.strip():
        return direct
    clock_constraints = state.get("clock_constraints")
    if clock_constraints:
        return as_text(clock_constraints)
    for root in _local_roots(state):
        if not root.exists():
            continue
        for file_path in root.rglob("*.sdc"):
            text = read_text_file(file_path)
            if text.strip():
                return text
    storage_sdc = collect_source_storage_texts(state, lambda path: path.endswith(".sdc"), limit=4)
    if storage_sdc:
        return storage_sdc[0]["content"]
    return ""


def collect_timing_reports(state: Dict[str, Any]) -> List[Dict[str, str]]:
    reports: List[Dict[str, str]] = []
    text = as_text(state.get("timing_report_text"))
    if text.strip():
        reports.append({"path": "timing_report_text.rpt", "content": text})
    for root in _local_roots(state):
        if not root.exists():
            continue
        for file_path in root.rglob("*"):
            lowered = str(file_path).lower()
            if file_path.suffix.lower() in REPORT_EXTENSIONS and any(token in lowered for token in ("sta", "timing", "wns", "tns", "setup", "hold")):
                content = read_text_file(file_path)
                if content.strip():
                    reports.append({"path": str(file_path), "content": content})
    reports.extend(collect_source_storage_texts(
        state,
        lambda path: path.endswith(tuple(REPORT_EXTENSIONS)) and any(token in path for token in ("sta", "timing", "wns", "tns", "setup", "hold", "openroad")),
        limit=48,
    ))
    return reports[:48]


def rtl_clock_signals(rtl_sources: Iterable[Dict[str, str]]) -> List[str]:
    clocks = set()
    pattern = re.compile(r"always(?:_ff)?\s*@\s*\(([^)]*)\)", re.IGNORECASE)
    for source in rtl_sources:
        for event in pattern.findall(source.get("content", "")):
            for signal in re.findall(r"(?:posedge|negedge)\s+([A-Za-z_][A-Za-z0-9_$]*)", event):
                clocks.add(signal)
    return sorted(clocks)


def markdown_table(headers: List[str], rows: List[List[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |")
    return "\n".join(lines)
