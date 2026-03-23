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

AGENT_NAME = "Digital STA PostCTS Agent"
STAGE_NAME = "sta_postcts"
OPENLANE_TO = "OpenROAD.STAMidPNR"

DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))


def _read_json(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _run(cmd, cwd):
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    return p.returncode, out
def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

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
    if not latest:
        return None
    src = os.path.join(latest, "final", "metrics.json")
    dst = os.path.join(stage_dir, "metrics.json")
    if os.path.exists(src):
        shutil.copy2(src, dst)
        return dst
    return None

def _pick_stage_netlist(latest_run: str) -> str | None:
    if not latest_run:
        return None

    patterns = [
        os.path.join(latest_run, "final", "nl", "*.nl.v"),
        os.path.join(latest_run, "final", "nl", "*.v"),
        os.path.join(latest_run, "final", "pnl", "*.pnl.v"),
        os.path.join(latest_run, "final", "pnl", "*.v"),
    ]

    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            logger.info(f"{AGENT_NAME}: selected stage netlist from run dir -> {hits[0]}")
            return hits[0]

    return None

def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None

def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    cts_state = digital.get("cts") or {}

    # Prefer CTS-owned SDC first
    cand = _first_existing([
        cts_state.get("constraints_sdc"),
        os.path.join(workflow_dir, "digital", "cts", "constraints", "digital_subsystem.sdc"),
    ])
    if cand:
        logger.info(f"{AGENT_NAME}: selected CTS SDC -> {cand}")
        return cand

    # Then safe fallbacks
    for stage in ["cts", "place", "floorplan", "impl_setup", "synth"]:
        cand_dir = os.path.join(workflow_dir, "digital", stage, "constraints")
        for cand in sorted(glob.glob(os.path.join(cand_dir, "*.sdc"))):
            if os.path.exists(cand):
                logger.info(f"{AGENT_NAME}: selected SDC from {stage} -> {cand}")
                return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None


def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    cts_state = digital.get("cts") or {}

    cand = _first_existing([
        cts_state.get("openlane_config"),
        cts_state.get("config_json"),
        os.path.join(workflow_dir, "digital", "cts", "config.json"),
    ])
    if cand:
        logger.info(f"{AGENT_NAME}: selected CTS config -> {cand}")
        return cand

    for cand in [
        os.path.join(workflow_dir, "digital", "place", "config.json"),
        os.path.join(workflow_dir, "digital", "impl_setup", "openlane", "config.json"),
        os.path.join(workflow_dir, "digital", "synth", "config.json"),
        os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json"),
    ]:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config fallback -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no OpenLane config found")
    return None

def _resolve_postcts_netlist(state: dict, workflow_dir: str) -> str | None:
    """
    STA post-CTS should use the logical synthesized netlist as VERILOG_FILES,
    not physical CTS netlists like *.nl.v / *.pnl.v.
    """
    synth_dir = os.path.join(workflow_dir, "digital", "synth", "netlist")

    candidates = []
    candidates += sorted(glob.glob(os.path.join(synth_dir, "*_synth.v")))
    candidates += [
        p for p in sorted(glob.glob(os.path.join(synth_dir, "*.v")))
        if not p.endswith(".nl.v") and not p.endswith(".pnl.v")
    ]

    cand = _first_existing(candidates)
    if cand:
        logger.info(f"{AGENT_NAME}: selected post-CTS logical netlist -> {cand}")
    else:
        logger.warning(f"{AGENT_NAME}: no synthesized netlist found for STA post-CTS")
    return cand

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")
    logger.info(f"🏁 Running {AGENT_NAME}")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", STAGE_NAME)
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(stage_dir); _ensure_dir(logs_dir)

    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Missing OpenLane config from state/cts/place/impl_setup/synth/foundry.")

    cfg = _read_json(base_cfg_path)
    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")


    cfg.pop("SYNTH_SDC_FILE", None)

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC from state/cts/place/floorplan/impl_setup/synth/legacy constraints.")

    sdc_basename = os.path.basename(upstream_sdc)

    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure_dir(inputs_constraints_dir)
    _ensure_dir(inputs_netlist_dir)

    stage_sdc = os.path.join(inputs_constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)
    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"

    logger.info(f"{AGENT_NAME}: upstream_sdc={upstream_sdc}")
    logger.info(f"{AGENT_NAME}: staged_sdc={stage_sdc}")

    for old_v in glob.glob(os.path.join(inputs_netlist_dir, "*.v")):
        try:
            os.remove(old_v)
        except Exception:
            pass

    postcts_netlist = _resolve_postcts_netlist(state, workflow_dir)
    if not postcts_netlist:
        raise RuntimeError("STA postcts: missing CTS netlist output.")

    if postcts_netlist.endswith(".nl.v") or postcts_netlist.endswith(".pnl.v"):
        raise RuntimeError(
            f"STA postcts: invalid logical netlist selected for VERILOG_FILES: {postcts_netlist}"
        )

    staged_postcts_netlist = os.path.join(inputs_netlist_dir, os.path.basename(postcts_netlist))
    shutil.copy2(postcts_netlist, staged_postcts_netlist)

    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(staged_postcts_netlist)}"]

    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(staged_postcts_netlist)
        if inferred:
            cfg["DESIGN_NAME"] = inferred
            state["design_name"] = inferred

    top_module = str(cfg.get("DESIGN_NAME", "")).strip() or "top"

    _write_text(os.path.join(stage_dir, "config.json"), json.dumps(cfg, indent=2))

    explicit = state.get("digital_run_tag") or state.get("run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    pdk = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE

    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host

    work_stage_dir = os.path.join(run_work_dir, STAGE_NAME)
    _ensure_dir(work_stage_dir)
    _write_text(os.path.join(work_stage_dir, "config.json"), json.dumps(cfg, indent=2))


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
        f"resolved_postcts_netlist={postcts_netlist}",
        f"staged_postcts_netlist={staged_postcts_netlist}",
        f"verilog_files_mode=explicit_from_synth_only",
        f"netlist_count=1",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "sta_postcts_input_resolution.log"), input_log)


    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "PDK_VARIANT={pdk}"
echo "OPENLANE_IMAGE={image}"
echo "WORKDIR=/work"

export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \
  -v "{pdk_root_host}":/pdk \
  -v "{run_work_dir}":/work \
  -e PDK={pdk} \
  -e PDK_ROOT=/pdk \
  {image} \
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --to {OPENLANE_TO} {STAGE_NAME}/config.json'
"""

    _write_text(os.path.join(stage_dir, "run.sh"), run_sh)
    os.chmod(os.path.join(stage_dir, "run.sh"), 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir)
    _write_text(os.path.join(logs_dir, "openlane_sta_postcts.log"), out)

    latest = _latest_run_dir(run_work_dir)
    metrics_path = _copy_metrics(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "sdc": f"digital/{STAGE_NAME}/constraints/{sdc_basename}",
            "metrics_json": f"digital/{STAGE_NAME}/metrics.json" if metrics_path else None,
            "log": f"digital/{STAGE_NAME}/logs/openlane_sta_postcts.log",
            "openlane_run_dir": latest,
        }
    }
    _write_text(os.path.join(stage_dir, "sta_postcts_summary.json"), json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir, "sta_postcts_summary.md"),
           f"# STA PostCTS\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/openlane_sta_postcts.log", out)
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/constraints/{sdc_basename}", sdc_text
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/sta_postcts_input_resolution.log", input_log
        )
        if metrics_path and os.path.exists(metrics_path):
            save_text_artifact_and_record(
                workflow_id,
                AGENT_NAME,
                "digital",
                f"{STAGE_NAME}/metrics.json",
                open(metrics_path, "r", encoding="utf-8").read(),
        )

    except Exception as e:
        print(f"⚠️ upload failed: {e}")

    state.setdefault("digital", {})[STAGE_NAME] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": os.path.join(work_stage_dir, "config.json"),
        "input_resolution_log": os.path.join(logs_dir, "sta_postcts_input_resolution.log"),
        "openlane_run_dir": latest,
        "netlist": staged_postcts_netlist,
    }

        
    return state