import os, json
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME="Digital Executive Summary Agent"

def _read_json(path):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception: return None

def _pick(paths):
    for p in paths:
        if p and os.path.exists(p): return p
    return None

def run_agent(state: dict) -> dict:
    workflow_id=state.get("workflow_id","default")
    workflow_dir=state.get("workflow_dir") or f"backend/workflows/{workflow_id}"

    # Stable metrics paths
    paths = {
        "synth": os.path.join(workflow_dir,"digital","synth","metrics.json"),
        "sta_postroute": os.path.join(workflow_dir,"digital","sta_postroute","metrics.json"),
        "drc": os.path.join(workflow_dir,"digital","drc","metrics.json"),
        "lvs": os.path.join(workflow_dir,"digital","lvs","metrics.json"),
        "tapeout": os.path.join(workflow_dir,"digital","tapeout","metrics.json"),
    }

    metrics = {k: _read_json(v) for k,v in paths.items() if os.path.exists(v)}

    # Minimal, no-fake-precision extraction (best-effort keys)
    def _get(m, *keys):
        if not isinstance(m, dict): return None
        for k in keys:
            if k in m: return m[k]
        return None

    synth_m = metrics.get("synth") or {}
    sta_m = metrics.get("sta_postroute") or {}
    drc_m = metrics.get("drc") or {}
    lvs_m = metrics.get("lvs") or {}

    summary_json = {
        "workflow_id": workflow_id,
        "cell_count": _get(synth_m, "cells", "cell_count", "design__instance__count"),
        "area": _get(synth_m, "area", "design__instance__area"),
        "worst_slack": _get(sta_m, "worst_slack", "timing__setup__ws"),
        "tns": _get(sta_m, "tns", "timing__setup__tns"),
        "drc_violations": _get(drc_m, "drc_violations", "klayout__drc__count", "magic__drc__count"),
        "lvs_status": _get(lvs_m, "lvs_status", "netgen__lvs__status"),
        "gds_paths": {
            "klayout": os.path.join(workflow_dir,"digital","tapeout","gds","klayout.gds"),
            "magic": os.path.join(workflow_dir,"digital","tapeout","gds","magic.gds"),
        },
        "artifact_map": {
            "synth_metrics": "digital/synth/metrics.json" if os.path.exists(paths["synth"]) else None,
            "sta_metrics": "digital/sta_postroute/metrics.json" if os.path.exists(paths["sta_postroute"]) else None,
            "drc_metrics": "digital/drc/metrics.json" if os.path.exists(paths["drc"]) else None,
            "lvs_metrics": "digital/lvs/metrics.json" if os.path.exists(paths["lvs"]) else None,
            "tapeout_metrics": "digital/tapeout/metrics.json" if os.path.exists(paths["tapeout"]) else None,
        },
        "next_iteration_suggestions": [
            "If worst_slack < 0: relax constraints or improve synthesis/placement/CTS parameters.",
            "If DRC violations > 0: inspect DRC logs and rerun with adjusted floorplan/route settings.",
            "If LVS not clean: check extraction/streamout mismatch and netlist naming."
        ]
    }

    md = []
    md.append("# Digital Executive Summary")
    md.append(f"- workflow_id: `{workflow_id}`")
    md.append("")
    md.append("## Key Metrics (best-effort parsed)")
    md.append(f"- Cell count: `{summary_json['cell_count']}`")
    md.append(f"- Area: `{summary_json['area']}`")
    md.append(f"- Worst slack: `{summary_json['worst_slack']}`")
    md.append(f"- TNS: `{summary_json['tns']}`")
    md.append(f"- DRC violations: `{summary_json['drc_violations']}`")
    md.append(f"- LVS status: `{summary_json['lvs_status']}`")
    md.append("")
    md.append("## GDS Paths (local, Option A)")
    md.append(f"- KLayout GDS: `{summary_json['gds_paths']['klayout']}`")
    md.append(f"- Magic GDS: `{summary_json['gds_paths']['magic']}`")
    md.append("")
    md.append("## Artifact Map")
    for k,v in summary_json["artifact_map"].items():
        md.append(f"- {k}: `{v}`")
    md.append("")
    md.append("## Next Iteration Suggestions")
    for s in summary_json["next_iteration_suggestions"]:
        md.append(f"- {s}")

    summary_md = "\n".join(md)
    summary_json_text = json.dumps(summary_json, indent=2)

    # write locally
    out_json_path = os.path.join(workflow_dir,"digital","executive_summary.json")
    out_md_path = os.path.join(workflow_dir,"digital","executive_summary.md")
    os.makedirs(os.path.dirname(out_json_path), exist_ok=True)
    with open(out_json_path,"w",encoding="utf-8") as f: f.write(summary_json_text)
    with open(out_md_path,"w",encoding="utf-8") as f: f.write(summary_md)

    # upload
    try:
        save_text_artifact_and_record(workflow_id,AGENT_NAME,"digital","executive_summary.json", summary_json_text)
        save_text_artifact_and_record(workflow_id,AGENT_NAME,"digital","executive_summary.md", summary_md)
    except Exception as e:
        print(f"⚠️ upload failed: {e}")

    state.setdefault("digital", {})["executive_summary"]={"status":"ok","paths":{"json":out_json_path,"md":out_md_path}}
    return state