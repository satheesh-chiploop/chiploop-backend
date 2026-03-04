import os, json
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Executive Summary Agent"

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _pick_first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    # Stable metrics paths (contract artifacts)
    paths = {
        "synth": os.path.join(workflow_dir, "digital", "synth", "metrics.json"),

        # STA stages (all four)
        "sta_preplace":  os.path.join(workflow_dir, "digital", "sta_preplace",  "metrics.json"),
        "sta_postplace": os.path.join(workflow_dir, "digital", "sta_postplace", "metrics.json"),
        "sta_postcts":   os.path.join(workflow_dir, "digital", "sta_postcts",   "metrics.json"),
        "sta_postroute": os.path.join(workflow_dir, "digital", "sta_postroute", "metrics.json"),

        "drc": os.path.join(workflow_dir, "digital", "drc", "metrics.json"),
        "lvs": os.path.join(workflow_dir, "digital", "lvs", "metrics.json"),
        "tapeout": os.path.join(workflow_dir, "digital", "tapeout", "metrics.json"),
    }

    # Load only existing metric files
    metrics = {k: _read_json(v) for k, v in paths.items() if os.path.exists(v)}

    # Minimal, no-fake-precision extraction (best-effort keys)
    def _get(m, *keys):
        if not isinstance(m, dict):
            return None
        for k in keys:
            if k in m:
                return m[k]
        return None

    synth_m = metrics.get("synth") or {}
    drc_m   = metrics.get("drc") or {}
    lvs_m   = metrics.get("lvs") or {}

    # Choose the "best available" STA stage for headline timing
    sta_preferred_order = ["sta_postroute", "sta_postcts", "sta_postplace", "sta_preplace"]
    sta_best_key = None
    for k in sta_preferred_order:
        if isinstance(metrics.get(k), dict):
            sta_best_key = k
            break
    sta_best_m = metrics.get(sta_best_key) or {}

    # GDS paths should not be "promised" unless they exist
    klayout_gds = os.path.join(workflow_dir, "digital", "tapeout", "gds", "klayout.gds")
    magic_gds   = os.path.join(workflow_dir, "digital", "tapeout", "gds", "magic.gds")
    gds_paths = {
        "klayout": klayout_gds if os.path.exists(klayout_gds) else None,
        "magic": magic_gds if os.path.exists(magic_gds) else None,
    }

    summary_json = {
        "workflow_id": workflow_id,

        # Helpful context for debugging / linking runs
        "digital_run_tag": state.get("digital_run_tag"),
        "digital_run_work_dir": state.get("digital_run_work_dir"),

        # Core synthesis metrics
        "cell_count": _get(synth_m, "cells", "cell_count", "design__instance__count"),
        "area": _get(synth_m, "area", "design__instance__area"),

        # Headline STA timing: best available stage among postroute/postcts/postplace/preplace
        "sta_best_stage": sta_best_key,
        "worst_slack": _get(sta_best_m, "worst_slack", "timing__setup__ws"),
        "tns": _get(sta_best_m, "tns", "timing__setup__tns"),

        # DRC / LVS
        "drc_violations": _get(drc_m, "drc_violations", "klayout__drc__count", "magic__drc__count"),
        "lvs_status": _get(lvs_m, "lvs_status", "netgen__lvs__status"),

        # Per-stage STA metrics (useful for triage)
        "sta_stages": {
            "sta_preplace": {
                "worst_slack": _get(metrics.get("sta_preplace") or {}, "worst_slack", "timing__setup__ws"),
                "tns": _get(metrics.get("sta_preplace") or {}, "tns", "timing__setup__tns"),
            },
            "sta_postplace": {
                "worst_slack": _get(metrics.get("sta_postplace") or {}, "worst_slack", "timing__setup__ws"),
                "tns": _get(metrics.get("sta_postplace") or {}, "tns", "timing__setup__tns"),
            },
            "sta_postcts": {
                "worst_slack": _get(metrics.get("sta_postcts") or {}, "worst_slack", "timing__setup__ws"),
                "tns": _get(metrics.get("sta_postcts") or {}, "tns", "timing__setup__tns"),
            },
            "sta_postroute": {
                "worst_slack": _get(metrics.get("sta_postroute") or {}, "worst_slack", "timing__setup__ws"),
                "tns": _get(metrics.get("sta_postroute") or {}, "tns", "timing__setup__tns"),
            },
        },

        "gds_paths": gds_paths,

        "artifact_map": {
            "synth_metrics": "digital/synth/metrics.json" if os.path.exists(paths["synth"]) else None,

            "sta_preplace_metrics":  "digital/sta_preplace/metrics.json"  if os.path.exists(paths["sta_preplace"]) else None,
            "sta_postplace_metrics": "digital/sta_postplace/metrics.json" if os.path.exists(paths["sta_postplace"]) else None,
            "sta_postcts_metrics":   "digital/sta_postcts/metrics.json"   if os.path.exists(paths["sta_postcts"]) else None,
            "sta_postroute_metrics": "digital/sta_postroute/metrics.json" if os.path.exists(paths["sta_postroute"]) else None,

            "drc_metrics": "digital/drc/metrics.json" if os.path.exists(paths["drc"]) else None,
            "lvs_metrics": "digital/lvs/metrics.json" if os.path.exists(paths["lvs"]) else None,
            "tapeout_metrics": "digital/tapeout/metrics.json" if os.path.exists(paths["tapeout"]) else None,
        },

        "next_iteration_suggestions": [
            "If worst_slack < 0: relax constraints or improve synthesis/placement/CTS/route parameters.",
            "If DRC violations > 0: inspect DRC logs and rerun with adjusted floorplan/route settings.",
            "If LVS not clean: check extraction/streamout mismatch and netlist naming."
        ]
    }

    md = []
    md.append("# Digital Executive Summary")
    md.append(f"- workflow_id: `{workflow_id}`")
    md.append(f"- run_tag: `{summary_json.get('digital_run_tag')}`")
    md.append(f"- run_work_dir: `{summary_json.get('digital_run_work_dir')}`")
    md.append("")

    md.append("## Key Metrics (best-effort parsed)")
    md.append(f"- Cell count: `{summary_json['cell_count']}`")
    md.append(f"- Area: `{summary_json['area']}`")
    md.append(f"- STA stage used: `{summary_json['sta_best_stage']}`")
    md.append(f"- Worst slack: `{summary_json['worst_slack']}`")
    md.append(f"- TNS: `{summary_json['tns']}`")
    md.append(f"- DRC violations: `{summary_json['drc_violations']}`")
    md.append(f"- LVS status: `{summary_json['lvs_status']}`")
    md.append("")

    md.append("## STA Stage Breakdown")
    for k in ["sta_preplace", "sta_postplace", "sta_postcts", "sta_postroute"]:
        ws = summary_json["sta_stages"][k]["worst_slack"]
        tns = summary_json["sta_stages"][k]["tns"]
        md.append(f"- {k}: worst_slack=`{ws}`, tns=`{tns}`")
    md.append("")

    md.append("## GDS Paths (local, only if produced)")
    md.append(f"- KLayout GDS: `{summary_json['gds_paths']['klayout']}`")
    md.append(f"- Magic GDS: `{summary_json['gds_paths']['magic']}`")
    md.append("")

    md.append("## Artifact Map")
    for k, v in summary_json["artifact_map"].items():
        md.append(f"- {k}: `{v}`")
    md.append("")

    md.append("## Next Iteration Suggestions")
    for s in summary_json["next_iteration_suggestions"]:
        md.append(f"- {s}")

    summary_md = "\n".join(md)
    summary_json_text = json.dumps(summary_json, indent=2)

    # write locally
    out_json_path = os.path.join(workflow_dir, "digital", "executive_summary.json")
    out_md_path = os.path.join(workflow_dir, "digital", "executive_summary.md")
    os.makedirs(os.path.dirname(out_json_path), exist_ok=True)
    with open(out_json_path, "w", encoding="utf-8") as f:
        f.write(summary_json_text)
    with open(out_md_path, "w", encoding="utf-8") as f:
        f.write(summary_md)

    # upload
    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "executive_summary.json", summary_json_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "executive_summary.md", summary_md)
    except Exception as e:
        print(f"⚠️ upload failed: {e}")

    state.setdefault("digital", {})["executive_summary"] = {
        "status": "ok",
        "paths": {"json": out_json_path, "md": out_md_path}
    }
    return state