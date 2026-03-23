import os
import json
import glob
import shutil
import subprocess
import re
import logging
from datetime import datetime

logger = logging.getLogger("chiploop")

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital CTS Agent"
DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))


def _read_json(p):
    try:
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}


def _run(cmd, cwd):
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    return p.returncode, out



def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _latest_run_dir(run_work_dir: str) -> str | None:
    runs_dir = os.path.join(run_work_dir, "runs")
    if not os.path.isdir(runs_dir):
        return None
    dirs = [os.path.join(runs_dir, d) for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d))]
    if not dirs:
        return None
    dirs.sort(key=lambda p: os.path.getmtime(p))
    return dirs[-1]


def _copy_metrics(latest, stage_dir):
    if not latest: return None
    src = os.path.join(latest, "final", "metrics.json")
    dst = os.path.join(stage_dir, "metrics.json")
    if os.path.exists(src):
        shutil.copy2(src, dst); return dst
    return None

def _copy_def(latest, stage_dir):
    if not latest: return None
    dst = os.path.join(stage_dir, "primary.def")
    cands = glob.glob(os.path.join(latest, "final", "def", "*.def")) + glob.glob(os.path.join(latest, "results", "**", "*.def"), recursive=True)
    if not cands: return None
    cands.sort(key=lambda p: os.path.getsize(p))
    shutil.copy2(cands[-1], dst)
    return dst


def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    cand = digital.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital -> {cand}")
        return cand

    place_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "place", "constraints", "*.sdc")))
    for cand in place_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from place -> {cand}")
            return cand

    floorplan_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "floorplan", "constraints", "*.sdc")))
    for cand in floorplan_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from floorplan -> {cand}")
            return cand

    impl_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "impl_setup", "constraints", "*.sdc")))
    for cand in impl_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from impl_setup -> {cand}")
            return cand

    synth_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "constraints", "*.sdc")))
    for cand in synth_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from synth -> {cand}")
            return cand

    legacy = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))
    for cand in legacy:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected legacy SDC -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None
def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    cand = digital.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital -> {cand}")
        return cand

    for cand in [
        os.path.join(workflow_dir, "digital", "impl_setup", "openlane", "config.json"),
        os.path.join(workflow_dir, "digital", "synth", "config.json"),
        os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json"),
    ]:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config fallback -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no OpenLane config found")
    return None

def _copy_cts_netlist(latest, stage_dir):
    if not latest:
        return None

    netlist_dir = os.path.join(stage_dir, "netlist")
    _ensure_dir(netlist_dir)

    cands = []
    cands += glob.glob(os.path.join(latest, "final", "nl", "*.nl.v"))
    cands += glob.glob(os.path.join(latest, "final", "nl", "*.v"))
    cands += glob.glob(os.path.join(latest, "final", "pnl", "*.pnl.v"))
    cands += glob.glob(os.path.join(latest, "final", "pnl", "*.v"))

    if not cands:
        return None

    src = cands[0]
    dst = os.path.join(netlist_dir, os.path.basename(src))
    shutil.copy2(src, dst)
    return dst

def run_agent(state: dict) -> dict:

    print(f"\n🏁 Running {AGENT_NAME}...")
    logger.info(f"🏁 Running {AGENT_NAME}")

    workflow_id = state.get("workflow_id","default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "cts")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")
    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)

    netlist_dir = os.path.join(stage_dir, "netlist")
    _ensure_dir(netlist_dir)

    synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    if not synth_netlists:
        synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "**", "*.v"), recursive=True))
    if not synth_netlists:
        raise RuntimeError("No synthesized netlist found. Expected digital/synth/netlist/*.v")

    for nl in synth_netlists:
        shutil.copy2(nl, os.path.join(netlist_dir, os.path.basename(nl)))

   

    
    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC: no constraints_sdc found in state, place, floorplan, impl_setup, synth, or legacy constraints.")

    sdc_basename = os.path.basename(upstream_sdc)
    stage_sdc = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)
    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    logger.info(f"{AGENT_NAME}: upstream_sdc={upstream_sdc}")
    logger.info(f"{AGENT_NAME}: staged_sdc={stage_sdc}")

    # Config baseline


    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Missing OpenLane config: no config found in state, impl_setup, synth, or foundry.")

    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")

    cfg = _read_json(base_cfg_path)
    cfg.pop("SYNTH_SDC_FILE", None)
    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"


    stage_netlists = sorted(glob.glob(os.path.join(netlist_dir, "*.v")))
    if not stage_netlists:
        raise RuntimeError(f"No .v files present under {netlist_dir}")
    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(p)}" for p in stage_netlists]

    # Match Placement behavior: fix DESIGN_NAME if base config says "top"
    inferred = None
    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
       inferred = _infer_top_from_netlist(stage_netlists[0])
    if inferred:
       cfg["DESIGN_NAME"] = inferred
       state["design_name"] = inferred  # propagate downstream

    top_module = str(cfg.get("DESIGN_NAME", "")).strip() or "top"

    config_path = os.path.join(stage_dir, "config.json")
    _write_text(config_path, json.dumps(cfg, indent=2))




    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host


    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    flow_run_tag = explicit or f"{wf_name}_{workflow_id}"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag


    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    work_stage_dir = os.path.join(run_work_dir, "cts")
    _ensure_dir(work_stage_dir)

    exec_config_path = os.path.join(work_stage_dir, "config.json")
    _write_text(exec_config_path, json.dumps(cfg, indent=2))

    # Option A: shared inputs under /work/inputs

    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure_dir(inputs_constraints_dir)
    _ensure_dir(inputs_netlist_dir)

    shutil.copy2(stage_sdc, os.path.join(inputs_constraints_dir, sdc_basename))
    for p in stage_netlists:
        shutil.copy2(p, os.path.join(inputs_netlist_dir, os.path.basename(p)))

    input_log = "\n".join([
        f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={workflow_dir}",
        f"upstream_sdc={upstream_sdc}",
        f"sdc_basename={sdc_basename}",
        f"stage_sdc={stage_sdc}",
        f"base_cfg_path={base_cfg_path}",
        f"run_work_dir={run_work_dir}",
        f"run_tag={run_tag}",
        f"top_module={top_module}",
        f"netlist_count={len(stage_netlists)}",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "cts_input_resolution.log"), input_log)

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "PDK_VARIANT={pdk_variant}"
echo "OPENLANE_IMAGE={openlane_image}"
echo "WORKDIR=/work"

export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \
  -v "{pdk_root_host}":/pdk \
  -v "{run_work_dir}":/work \
  -e PDK={pdk_variant} \
  -e PDK_ROOT=/pdk \
  {openlane_image} \
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --to OpenROAD.CTS cts/config.json'

"""


    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh); os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash","-lc","./run.sh"], cwd=stage_dir)
    log_path = os.path.join(logs_dir, "openlane_cts.log")
    _write_text(log_path, out)

    latest = _latest_run_dir(work_stage_dir)
    metrics_path = _copy_metrics(latest, stage_dir)
    def_path = _copy_def(latest, stage_dir)
    cts_netlist_path = _copy_cts_netlist(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "sdc": f"digital/cts/constraints/{sdc_basename}",
            "metrics_json": "digital/cts/metrics.json" if metrics_path else None,
            "primary_def": "digital/cts/primary.def" if def_path else None,
            "cts_netlist": f"digital/cts/netlist/{os.path.basename(cts_netlist_path)}" if cts_netlist_path else None,
            "log": "digital/cts/logs/openlane_cts.log",
            "openlane_run_dir": latest,

        }
    }


    _write_text(os.path.join(stage_dir,"cts_summary.json"), json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir,"cts_summary.md"), f"# CTS\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"cts/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/logs/cts_input_resolution.log", input_log)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/logs/openlane_cts.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/cts_summary.json", json.dumps(summary, indent=2))
        if metrics_path and os.path.exists(metrics_path):
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/metrics.json", open(metrics_path,"r",encoding="utf-8").read())
        if def_path and os.path.exists(def_path):
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/primary.def", open(def_path,"r",encoding="utf-8",errors="ignore").read())
    except Exception as e:
        print(f"⚠️ upload failed: {e}")

    state.setdefault("digital", {})["cts"] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "primary_def": def_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": config_path,
        "input_resolution_log": os.path.join(logs_dir, "cts_input_resolution.log"),
        "openlane_run_dir": latest,
        "cts_netlist": cts_netlist_path,
        "netlist": cts_netlist_path,
    }

    
    return state