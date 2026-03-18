"""
ChipLoop Open-Source Signoff / Handoff Agents

Design goals:
- deterministic outputs
- derive intent from prior artifacts (`digital_spec_json`, RTL outputs, architecture) rather than hardcoding
- best-effort integration with open-source tools
- always produce: (1) human report (MD) and (2) machine findings (JSON)
"""

import os
import re
import json
import shutil
import hashlib
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read_json(v) -> Dict[str, Any]:
    try:
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and os.path.exists(v):
            with open(v, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


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


def _collect_rtl_files(workflow_dir: str, state: dict) -> List[str]:
    out: List[str] = []

    artifact_list = state.get("artifact_list") or []
    if isinstance(artifact_list, list):
        for p in artifact_list:
            if isinstance(p, str) and os.path.exists(p) and p.lower().endswith((".v", ".sv", ".vh", ".svh")):
                out.append(p)

    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.lower().endswith((".v", ".sv", ".vh", ".svh")):
                out.append(os.path.join(root, fn))

    return sorted(list(dict.fromkeys(out)))


def _normalize_spec(spec: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    if isinstance(spec.get("hierarchy"), dict):
        hier = spec["hierarchy"]
        top = hier.get("top_module")
        modules = hier.get("modules", [])
        if isinstance(top, dict) and top.get("name"):
            return {
                "top_module": top["name"],
                "modules": [top] + modules,
                "raw": spec,
            }, "hierarchical"

    if spec.get("name") and spec.get("rtl_output_file"):
        return {
            "top_module": spec["name"],
            "modules": [spec],
            "raw": spec,
        }, "flat"

    return {"top_module": "top", "modules": [], "raw": spec}, "unknown"


def _pick_top(spec: Dict[str, Any], state_top: Optional[str]) -> str:
    norm, _ = _normalize_spec(spec)
    if state_top:
        return state_top
    if norm.get("top_module"):
        return norm["top_module"]
    return "top"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gather_deliverables(workflow_dir: str, state: dict) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {
        "rtl": [],
        "spec": [],
        "arch": [],
        "microarch": [],
        "regmap": [],
        "constraints": [],
        "power_intent": [],
        "verification": [],
        "reports": [],
        "other": [],
    }

    buckets["rtl"] = _collect_rtl_files(workflow_dir, state)

    known_paths = [
        state.get("digital_spec_json"),
        state.get("spec_json"),
        state.get("digital_architecture_json"),
        state.get("digital_architecture_path"),
        state.get("digital_microarchitecture_json"),
        state.get("digital_microarchitecture_path"),
        state.get("digital_regmap_json"),
        state.get("clock_reset_arch_path"),
        state.get("digital_sdc_path"),
        state.get("sdc_path"),
        state.get("rtl_refactor_plan_path"),
    ]

    for p in known_paths:
        if isinstance(p, str) and os.path.exists(p):
            norm = p.replace("\\", "/").lower()
            if "spec" in os.path.basename(norm):
                buckets["spec"].append(p)
            elif "microarch" in os.path.basename(norm):
                buckets["microarch"].append(p)
            elif "architecture" in os.path.basename(norm) or "arch" in os.path.basename(norm):
                buckets["arch"].append(p)
            elif "regmap" in os.path.basename(norm) or "register_map" in os.path.basename(norm):
                buckets["regmap"].append(p)
            elif norm.endswith(".sdc"):
                buckets["constraints"].append(p)
            else:
                buckets["other"].append(p)

    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            p = os.path.join(root, fn)
            norm = p.replace("\\", "/").lower()
            base = os.path.basename(norm)

            if p in buckets["rtl"]:
                continue

            if "regmap" in base or "register_map" in base:
                buckets["regmap"].append(p)
                continue

            if norm.endswith(".upf"):
                buckets["power_intent"].append(p)
                continue

            if norm.endswith(".sdc") or (norm.endswith((".xdc", ".tcl")) and "/constraints/" in norm):
                buckets["constraints"].append(p)
                continue

            if "/verification/" in norm or "/vv/" in norm:
                buckets["verification"].append(p)
                continue

            if base.endswith(".json") or base.endswith(".md"):
                if "microarch" in base:
                    buckets["microarch"].append(p)
                elif "architecture" in base or "arch" in base:
                    buckets["arch"].append(p)
                elif "spec" in base:
                    buckets["spec"].append(p)
                elif "/handoff/" in norm or "/signoff/" in norm:
                    buckets["reports"].append(p)
                else:
                    buckets["other"].append(p)
                continue

            if base.endswith(".log") or base.endswith(".txt"):
                if "/handoff/" in norm or "/artifact/" in norm or "/signoff/" in norm:
                    buckets["reports"].append(p)

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
            rel = os.path.relpath(p, pkg_dir).replace("\\", "/")
            try:
                manifest_files.append({
                    "path": rel,
                    "sha256": _sha256_file(p),
                    "bytes": os.path.getsize(p),
                })
            except Exception:
                pass
    manifest_files.sort(key=lambda x: x["path"])

    manifest = {
        "type": "chiploop_ip_package",
        "version": "1.0",
        "top_module": top,
        "generated_at": _now(),
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    _write(os.path.join(pkg_dir, "MANIFEST.json"), json.dumps(manifest, indent=2))

    checklist = f"""# IP Packaging & Handoff Checklist

Package: `{top}_ip_package`

## Must-have
- [ ] RTL (`rtl/`)
- [ ] Spec (`docs/spec/`)
- [ ] Architecture (`docs/arch/`)
- [ ] Microarchitecture (`docs/microarch/`)
- [ ] Register map (`docs/regmap/`)
- [ ] Constraints (`constraints/`)
- [ ] Power intent (`power/`)
- [ ] Verification collateral (`verification/`)
- [ ] Reports (`reports/`)

## Integration notes to include
- clock/reset requirements
- register/software expectations
- interface assumptions
- known limitations / TODOs
"""
    _write(os.path.join(pkg_dir, "DELIVERABLES.md"), checklist)

    zip_path = os.path.join(pkg_root, f"{top}_ip_package.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(pkg_dir):
            for fn in files:
                p = os.path.join(root, fn)
                arc = os.path.relpath(p, pkg_root).replace("\\", "/")
                z.write(p, arcname=arc)

    return pkg_dir, zip_path


def run_agent(state: dict) -> dict:
    agent_name = "IP Packaging & Handoff Agent"
    print("\n📦 Running IP Packaging & Handoff Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    spec = _read_json(state.get("digital_spec_json")) or _read_json(state.get("spec_json"))
    top = (
        state.get("soc_top_sim_module")
        or state.get("top_module")
    )
    top = _pick_top(spec, top)

    buckets = _gather_deliverables(workflow_dir, state)
    pkg_dir, zip_path = _package(workflow_dir, top, buckets)

    report = {
        "type": "ip_packaging_handoff",
        "version": "1.0",
        "top_module": top,
        "package_dir": pkg_dir,
        "zip_path": zip_path,
        "bucket_counts": {k: len(v) for k, v in buckets.items()},
    }

    md = [
        "# IP Packaging & Handoff Report\n",
        f"- Top: `{top}`",
        f"- Package: `{pkg_dir}`",
        f"- Zip: `{zip_path}`\n",
        "## Bucket counts"
    ]
    for k, v in report["bucket_counts"].items():
        md.append(f"- **{k}**: {v}")
    report_md = "\n".join(md) + "\n"

    out_root = os.path.join(workflow_dir, "handoff")
    os.makedirs(out_root, exist_ok=True)
    md_path = os.path.join(out_root, "ip_packaging_report.md")
    json_path = os.path.join(out_root, "ip_packaging_report.json")
    _write(md_path, report_md)
    _write(json_path, json.dumps(report, indent=2))

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
    state["status"] = "✅ IP packaging and handoff completed."
    return state