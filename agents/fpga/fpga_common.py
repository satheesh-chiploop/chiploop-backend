import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


FPGA_DIR = "fpga"
ARTIFACT_BUCKET = "artifacts"
RTL_EXTENSIONS = (".sv", ".v", ".svh", ".vh")


BOARD_REGISTRY: Dict[str, Dict[str, Any]] = {
    "icebreaker": {
        "label": "iCEBreaker",
        "vendor": "lattice",
        "family": "ice40",
        "device": "up5k",
        "package": "sg48",
        "nextpnr_device_flag": "--up5k",
        "nextpnr_package": "sg48",
        "constraint_format": "pcf",
        "programmer_board": "icebreaker",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 5280},
    },
    "upduino_v3": {
        "label": "UPduino v3",
        "vendor": "lattice",
        "family": "ice40",
        "device": "up5k",
        "package": "sg48",
        "nextpnr_device_flag": "--up5k",
        "nextpnr_package": "sg48",
        "constraint_format": "pcf",
        "programmer_board": "upduino3",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 5280},
    },
    "icestick": {
        "label": "Lattice iCEstick",
        "vendor": "lattice",
        "family": "ice40",
        "device": "hx1k",
        "package": "tq144",
        "nextpnr_device_flag": "--hx1k",
        "nextpnr_package": "tq144",
        "constraint_format": "pcf",
        "programmer_board": "icestick",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 1280},
    },
    "custom_ice40": {
        "label": "Custom iCE40",
        "vendor": "lattice",
        "family": "ice40",
        "device": "hx8k",
        "package": "ct256",
        "nextpnr_device_flag": "--hx8k",
        "nextpnr_package": "ct256",
        "constraint_format": "pcf",
        "programmer_board": None,
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 7680},
    },
}


def workflow_dir(state: Dict[str, Any]) -> str:
    wid = str(state.get("workflow_id") or "default")
    root = str(state.get("workflow_dir") or f"backend/workflows/{wid}")
    os.makedirs(root, exist_ok=True)
    return root


def fpga_dir(state: Dict[str, Any], *parts: str) -> str:
    path = os.path.join(workflow_dir(state), FPGA_DIR, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def write_text(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    return path


def write_json(path: str, data: Dict[str, Any]) -> str:
    return write_text(path, json.dumps(data, indent=2, sort_keys=True))


def publish_json(state: Dict[str, Any], agent: str, subdir: str, filename: str, data: Dict[str, Any]) -> str:
    path = write_json(f"{fpga_dir(state, subdir)}/{filename}", data)
    workflow_id = str(state.get("workflow_id") or "")
    if workflow_id:
        try:
            from utils.artifact_utils import save_text_artifact_and_record

            save_text_artifact_and_record(
                workflow_id,
                agent,
                f"fpga/{subdir}".rstrip("/"),
                filename,
                json.dumps(data, indent=2, sort_keys=True),
            )
        except Exception:
            pass
    return path


def read_text(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def detect_top_module(paths: List[str]) -> Optional[str]:
    for path in paths:
        text = read_text(path)
        match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)
        if match:
            return match.group(1)
    return None


def _safe_rel(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return "rtl/source.sv"
    parts = Path(normalized).parts
    if any(part in {"..", ""} for part in parts) or Path(normalized).is_absolute():
        return os.path.basename(normalized) or "source.sv"
    return normalized


def _copy_tree_rtl(source_dir: str, dest_dir: str) -> List[str]:
    copied: List[str] = []
    for pattern in ("**/*.v", "**/*.sv"):
        for src in glob.glob(os.path.join(source_dir, pattern), recursive=True):
            if any(skip in src.replace("\\", "/").lower() for skip in ("/sim_build/", "/node_modules/", "/.git/")):
                continue
            rel = os.path.relpath(src, source_dir)
            dst = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(dst)
    return copied


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


def _list_storage_tree(client: Any, folder: str, depth: int = 0, max_depth: int = 6) -> List[str]:
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


def _copy_storage_rtl(state: Dict[str, Any], source_workflow_id: str, dest_dir: str) -> List[str]:
    client = state.get("supabase_client")
    if not client:
        return []
    paths: List[str] = []
    try:
        row = (
            client.table("workflows")
            .select("artifacts")
            .eq("id", source_workflow_id)
            .single()
            .execute()
        ).data or {}
        paths.extend(_storage_paths(row.get("artifacts") or {}))
    except Exception:
        pass
    paths.extend(_list_storage_tree(client, f"backend/workflows/{source_workflow_id}"))
    rtl_paths = [
        path for path in list(dict.fromkeys(paths))
        if path.lower().endswith(RTL_EXTENSIONS)
    ][:512]
    copied: List[str] = []
    for index, path in enumerate(rtl_paths):
        try:
            raw = client.storage.from_(ARTIFACT_BUCKET).download(path)
        except Exception:
            raw = None
        if not raw:
            continue
        rel = _safe_rel(f"upstream/{os.path.basename(path) or f'source_{index}.sv'}")
        target = os.path.join(dest_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_bytes(raw)
        copied.append(target)
    return copied


def resolve_rtl_sources(state: Dict[str, Any]) -> List[str]:
    out_dir = fpga_dir(state, "src")
    sources: List[str] = []
    mode = str(state.get("rtl_source_mode") or state.get("source") or "").strip().lower()
    pasted = state.get("pasted_rtl_files")
    if isinstance(pasted, list):
        for index, item in enumerate(pasted):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "")
            if not content.strip():
                continue
            rel = str(item.get("path") or f"rtl/source_{index}.sv").replace("\\", "/").lstrip("/")
            if not rel.endswith((".v", ".sv")):
                rel += ".sv"
            sources.append(write_text(os.path.join(out_dir, rel), content))
    rtl_text = str(state.get("rtl_text") or "")
    if rtl_text.strip():
        sources.append(write_text(os.path.join(out_dir, "rtl", "top.sv"), rtl_text))
    repo_path = state.get("repo_path")
    if mode == "repo_path" and isinstance(repo_path, str) and repo_path.strip() and os.path.exists(repo_path):
        base = repo_path
        subdir = state.get("repo_subdir")
        if isinstance(subdir, str) and subdir.strip():
            base = os.path.join(base, subdir.strip())
        sources.extend(_copy_tree_rtl(base, out_dir))
    source_wf = state.get("from_workflow_id") or state.get("source_arch2rtl_workflow_id") or state.get("source_workflow_id")
    if source_wf:
        sources.extend(_copy_storage_rtl(state, str(source_wf), out_dir))
        source_root = os.path.join("backend", "workflows", str(source_wf))
        if os.path.exists(source_root):
            sources.extend(_copy_tree_rtl(source_root, out_dir))
    return sorted(dict.fromkeys(os.path.abspath(path) for path in sources if os.path.exists(path)))


def board_config(state: Dict[str, Any]) -> Dict[str, Any]:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board_key = str(state.get("board") or fpga.get("board") or "icebreaker").strip().lower()
    base = dict(BOARD_REGISTRY.get(board_key) or BOARD_REGISTRY["custom_ice40"])
    base["board"] = board_key
    for key in ("family", "device", "package", "programmer_board"):
        if state.get(key):
            base[key] = state.get(key)
        if fpga.get(key):
            base[key] = fpga.get(key)
    if base.get("family") != "ice40":
        base["supported"] = False
        base["unsupported_reason"] = "First production slice supports Lattice iCE40 open-source flow only."
    else:
        base["supported"] = True
    if str(base.get("device", "")).lower() in {"up5k", "u4k"}:
        base["nextpnr_device_flag"] = "--up5k"
    elif str(base.get("device", "")).lower() in {"hx1k", "lp1k"}:
        base["nextpnr_device_flag"] = "--hx1k"
    elif str(base.get("device", "")).lower() in {"hx8k", "lp8k"}:
        base["nextpnr_device_flag"] = "--hx8k"
    return base


def tool_status() -> Dict[str, Any]:
    tools = {
        "yosys": shutil.which("yosys"),
        "nextpnr-ice40": shutil.which("nextpnr-ice40"),
        "icepack": shutil.which("icepack"),
        "icetime": shutil.which("icetime"),
        "openFPGALoader": shutil.which("openFPGALoader"),
    }
    return {name: {"available": bool(path), "path": path} for name, path in tools.items()}


def run_cmd(cmd: List[str], cwd: str, log_path: str, timeout: int = 600) -> Dict[str, Any]:
    started = None
    try:
        started = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = started.stdout or ""
        write_text(log_path, output)
        return {"cmd": cmd, "returncode": started.returncode, "ok": started.returncode == 0, "log": log_path, "stdout_tail": output[-4000:]}
    except FileNotFoundError as exc:
        msg = f"Tool not found: {cmd[0]} ({exc})"
        write_text(log_path, msg)
        return {"cmd": cmd, "returncode": 127, "ok": False, "log": log_path, "error": msg}
    except subprocess.TimeoutExpired as exc:
        msg = f"Timed out after {timeout}s: {' '.join(cmd)}\n{exc.stdout or ''}"
        write_text(log_path, msg)
        return {"cmd": cmd, "returncode": 124, "ok": False, "log": log_path, "error": msg}


def manifest_update(state: Dict[str, Any], key: str, value: Any) -> None:
    fpga = state.setdefault("fpga", {})
    if isinstance(fpga, dict):
        fpga[key] = value
