"""
ChipLoop Open-Source Signoff / Handoff Agents

Design goals:
- deterministic outputs
- derive intent from prior artifacts (`digital_spec_json`, RTL outputs, architecture) rather than hardcoding
- best-effort integration with open-source tools (Yosys/OpenROAD/Verilator/etc.)
- always produce: (1) human report (MD) and (2) machine findings (JSON)
"""

import os
import re
import json
import shutil
import hashlib
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)

def _run(cmd: List[str], cwd: Optional[str]=None, timeout: int=300) -> Dict[str, Any]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout or "", "stderr": p.stderr or ""}
    except Exception as e:
        return {"cmd": cmd, "returncode": -1, "stdout": "", "stderr": str(e)}

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

def _pick_top(spec: Dict[str, Any], rtl_files: List[str], state_top: Optional[str]) -> str:
    if state_top:
        return state_top
    top = (spec.get("top_module") or {}).get("name")
    if isinstance(top, str) and top.strip():
        return top.strip()
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

# ----------------------------
# IP Packaging & Handoff Agent
# ----------------------------

import zipfile

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _gather_deliverables(workflow_dir: str) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {
        "rtl": [], "spec": [], "arch": [], "microarch": [], "regmap": [],
        "constraints": [], "power_intent": [], "verification": [], "reports": [], "other": []
    }
    buckets["rtl"] = _collect_rtl_files(workflow_dir)

    roots = [
        ("spec", os.path.join(workflow_dir, "digital")),
        ("arch", os.path.join(workflow_dir, "digital")),
        ("verification", os.path.join(workflow_dir, "vv")),
        ("constraints", os.path.join(workflow_dir, "constraints")),
        ("power_intent", os.path.join(workflow_dir, "signoff")),
        ("reports", os.path.join(workflow_dir, "signoff")),
        ("reports", os.path.join("artifact")),
    ]
    for key, root in roots:
        if not os.path.exists(root):
            continue
        for r, _, files in os.walk(root):
            for fn in files:
                p = os.path.join(r, fn)
                low = fn.lower()
                if key in ("spec","arch") and low.endswith((".json",".md")):
                    buckets[key].append(p)
                elif key == "constraints" and low.endswith((".sdc",".xdc",".tcl")):
                    buckets[key].append(p)
                elif key == "power_intent" and low.endswith(".upf"):
                    buckets[key].append(p)
                elif key == "verification" and low.endswith((".py",".md",".sby",".sv",".json",".yml",".yaml")):
                    buckets[key].append(p)
                elif key == "reports" and low.endswith((".md",".json",".log",".txt")):
                    buckets[key].append(p)

    for r, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.lower() in ("regmap.json","regmap.md"):
                buckets["regmap"].append(os.path.join(r, fn))

    for k in buckets:
        buckets[k] = sorted(list(dict.fromkeys(buckets[k])))
    return buckets

def _package(workflow_dir: str, top: str, buckets: Dict[str, List[str]]) -> Tuple[str, str]:
    pkg_root = os.path.join(workflow_dir, "handoff")
    os.makedirs(pkg_root, exist_ok=True)
    pkg_dir = os.path.join(pkg_root, f"{top}_ip_package")
    os.makedirs(pkg_dir, exist_ok=True)

    layout = {
        "rtl": "rtl",
        "spec": "docs/spec",
        "arch": "docs/arch",
        "microarch": "docs/microarch",
        "regmap": "docs/regmap",
        "constraints": "constraints",
        "power_intent": "power",
        "verification": "verification",
        "reports": "reports",
        "other": "misc",
    }

    for bucket, files in buckets.items():
        dst_rel = layout.get(bucket, "misc")
        for src in files:
            if not os.path.exists(src):
                continue
            dst = os.path.join(pkg_dir, dst_rel, os.path.basename(src))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass

    manifest_files: List[Dict[str, Any]] = []
    for root, _, files in os.walk(pkg_dir):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, pkg_dir).replace("\\","/")
            try:
                manifest_files.append({"path": rel, "sha256": _sha256_file(p), "bytes": os.path.getsize(p)})
            except Exception:
                pass
    manifest_files.sort(key=lambda x: x["path"])

    manifest = {
        "type": "chiploop_ip_package",
        "version": "1.0",
        "top_module": top,
        "generated_at": _now(),
        "layout": layout,
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    _write(os.path.join(pkg_dir, "MANIFEST.json"), json.dumps(manifest, indent=2))

    checklist = f"""# IP Packaging & Handoff Checklist

Package: `{top}_ip_package`

## Must-have
- [ ] RTL (`rtl/`)
- [ ] Spec (`docs/spec/`)
- [ ] Architecture (`docs/arch/`, `docs/microarch/`)
- [ ] Register map (`docs/regmap/`)
- [ ] Constraints (`constraints/`) — add SDC/XDC if applicable
- [ ] Power intent (`power/`) — UPF if multi-power
- [ ] Verification collateral (`verification/`)
- [ ] Reports (`reports/`)

## Integration notes to include
- clock/reset requirements
- bus protocol expectations
- interrupt behavior
- known limitations / TODOs
"""
    _write(os.path.join(pkg_dir, "DELIVERABLES.md"), checklist)

    zip_path = os.path.join(pkg_root, f"{top}_ip_package.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(pkg_dir):
            for fn in files:
                p = os.path.join(root, fn)
                arc = os.path.relpath(p, pkg_root).replace("\\","/")
                z.write(p, arcname=arc)

    return pkg_dir, zip_path

def run_agent(state: dict) -> dict:
    agent_name = "IP Packaging & Handoff Agent"
    print("\n📦 Running IP Packaging & Handoff Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)

    spec_path = state.get("spec_json") or os.path.join(workflow_dir, "digital", "spec.json")
    spec = _read_json(spec_path)
    rtl_files = state.get("rtl_files") or _collect_rtl_files(workflow_dir)
    top = _pick_top(spec, rtl_files, state.get("top_module"))

    buckets = _gather_deliverables(workflow_dir)
    pkg_dir, zip_path = _package(workflow_dir, top, buckets)

    report = {
        "type": "ip_packaging_handoff",
        "version": "1.0",
        "top_module": top,
        "package_dir": pkg_dir,
        "zip_path": zip_path,
        "bucket_counts": {k: len(v) for k, v in buckets.items()},
    }

    md = ["# IP Packaging & Handoff Report\n",
          f"- Top: `{top}`",
          f"- Package: `{pkg_dir}`",
          f"- Zip: `{zip_path}`\n",
          "## Bucket counts"]
    for k, v in report["bucket_counts"].items():
        md.append(f"- **{k}**: {v}")
    report_md = "\n".join(md) + "\n"

    out_root = os.path.join(workflow_dir, "handoff")
    os.makedirs(out_root, exist_ok=True)
    _write(os.path.join(out_root, "ip_packaging_report.md"), report_md)
    _write(os.path.join(out_root, "ip_packaging_report.json"), json.dumps(report, indent=2))

    artifacts = {}
    artifacts["report_md"] = _record(workflow_id, agent_name, "handoff", "ip_packaging_report.md", report_md)
    artifacts["report_json"] = _record(workflow_id, agent_name, "handoff", "ip_packaging_report.json", json.dumps(report, indent=2))

    man_path = os.path.join(pkg_dir, "MANIFEST.json")
    if os.path.exists(man_path):
        with open(man_path, "r", encoding="utf-8", errors="ignore") as f:
            artifacts["manifest"] = _record(workflow_id, agent_name, "handoff", "MANIFEST.json", f.read())
    deliv_path = os.path.join(pkg_dir, "DELIVERABLES.md")
    if os.path.exists(deliv_path):
        with open(deliv_path, "r", encoding="utf-8", errors="ignore") as f:
            artifacts["deliverables"] = _record(workflow_id, agent_name, "handoff", "DELIVERABLES.md", f.read())

    report["artifacts"] = artifacts
    state.setdefault("handoff", {})
    state["handoff"]["ip_packaging"] = report
    return state
