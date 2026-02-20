# backend/agents/digital_smoke_preflight_agent.py

import os
import re
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()

def _record(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None

def _collect_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv", ".vh", ".svh")
    out: List[str] = []
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.lower().endswith(exts):
                out.append(os.path.join(root, fn))
    out.sort()
    return out

def _pick_top(rtl_files: List[str], state_top: Optional[str]) -> str:
    if state_top and isinstance(state_top, str) and state_top.strip():
        return state_top.strip()

    mod_re = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b")
    for f in rtl_files:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = mod_re.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    return "top"

def run_agent(state: dict) -> dict:
    agent_name = "Digital Smoke Preflight Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    rtl_files = _collect_rtl_files(workflow_dir)
    top = _pick_top(rtl_files, state.get("top_module"))

    manifest = {
        "type": "digital_smoke_preflight",
        "generated_at": _now(),
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
        "top_module": top,
        "rtl_file_count": len(rtl_files),
        "rtl_files": [os.path.relpath(p, workflow_dir).replace("\\", "/") for p in rtl_files],
        "simulator": state.get("simulator") or state.get("sim_type") or "verilator",
        "time_budget": state.get("time_budget") or "fast",
        "waveform": bool(state.get("enable_waveform", False)),
    }

    # Write locally (optional but helps debugging)
    out_dir = os.path.join(workflow_dir, "vv", "smoke")
    os.makedirs(out_dir, exist_ok=True)
    preflight_path = os.path.join(out_dir, "smoke_preflight.json")
    with open(preflight_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    _record(workflow_id, agent_name, "vv/smoke", "smoke_preflight.json", json.dumps(manifest, indent=2))

    state["smoke_preflight"] = manifest
    state["top_module"] = top
    state["status"] = "✅ Smoke preflight complete"
    return state
