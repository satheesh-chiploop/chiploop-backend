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

AGENT_NAME="Digital Route Agent"
DEFAULT_PDK_VARIANT=os.getenv("CHIPLOOP_PDK_VARIANT","sky130A")
DEFAULT_OPENLANE_IMAGE=os.getenv("CHIPLOOP_OPENLANE_IMAGE","ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES=int(os.getenv("OPENLANE_NUM_CORES","2"))


def _read_json(p):
    try:
        with open(p,"r",encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def _run(cmd,cwd):
    p=subprocess.Popen(cmd,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    out,_=p.communicate(); return p.returncode,out

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
    src=os.path.join(latest,"final","metrics.json")
    dst=os.path.join(stage_dir,"metrics.json")
    if os.path.exists(src): shutil.copy2(src,dst); return dst
    return None
def _copy_def(latest, stage_dir):
    if not latest: return None
    dst=os.path.join(stage_dir,"primary.def")
    cands=glob.glob(os.path.join(latest,"final","def","*.def")) + glob.glob(os.path.join(latest,"results","**","*.def"), recursive=True)
    if not cands: return None
    cands.sort(key=lambda p: os.path.getsize(p))
    shutil.copy2(cands[-1], dst)
    return dst

def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None

def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    cts_state = digital.get("cts") or {}

    cand = cts_state.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital.cts -> {cand}")
        return cand

    cand = digital.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital -> {cand}")
        return cand

    for stage in ["cts", "place", "floorplan", "impl_setup", "synth"]:
        cand_dir = os.path.join(workflow_dir, "digital", stage, "constraints")
        for cand in sorted(glob.glob(os.path.join(cand_dir, "*.sdc"))):
            if os.path.exists(cand):
                logger.info(f"{AGENT_NAME}: selected SDC from {stage} -> {cand}")
                return cand

    legacy_dir = os.path.join(workflow_dir, "digital", "constraints")
    for cand in sorted(glob.glob(os.path.join(legacy_dir, "*.sdc"))):
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected legacy SDC -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None

def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    cts_state = digital.get("cts") or {}

    # 1) Prefer CTS-owned config first
    cand = cts_state.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital.cts -> {cand}")
        return cand

    # 2) Then generic digital state
    for key in ["openlane_config", "config_json"]:
        cand = digital.get(key)
        if cand and os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config from state.digital[{key}] -> {cand}")
            return cand

    # 3) Then concrete stage configs on disk
    for cand in [
        os.path.join(workflow_dir, "digital", "cts", "config.json"),
        os.path.join(workflow_dir, "digital", "place", "config.json"),
        os.path.join(workflow_dir, "digital", "floorplan", "config.json"),
        os.path.join(workflow_dir, "digital", "impl_setup", "openlane", "config.json"),
        os.path.join(workflow_dir, "digital", "synth", "config.json"),
        os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json"),
    ]:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config fallback -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no OpenLane config found")
    return None

def _copy_route_netlist(latest, stage_dir):
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


def _resolve_route_input_netlists(state: dict, workflow_dir: str) -> list[str]:
    """
    Route should start from the logical synthesized netlist,
    not from physical/signoff netlists like *.nl.v or *.pnl.v.
    """
    candidates = []

    # Strong preference: synth netlist directory
    synth_dir = os.path.join(workflow_dir, "digital", "synth", "netlist")

    preferred = sorted(glob.glob(os.path.join(synth_dir, "*_synth.v")))
    if preferred:
        return preferred

    fallback = sorted(glob.glob(os.path.join(synth_dir, "*.v")))
    fallback = [
        p for p in fallback
        if not p.endswith(".nl.v") and not p.endswith(".pnl.v")
    ]
    if fallback:
        return fallback

    raise RuntimeError(
        "Route: no valid synthesized netlist found under digital/synth/netlist"
    )

def _resolve_macro_files_from_workflow(workflow_dir: str, exts: tuple[str, ...]) -> list[str]:
    hits = []
    for ext in exts:
        hits.extend(glob.glob(os.path.join(workflow_dir, "**", f"*{ext}"), recursive=True))

    out = []
    seen = set()
    for p in sorted(hits):
        base = os.path.basename(p).lower()
        if base.endswith("_llm_lef_raw.lef"):
            continue
        if base.endswith("_raw.lef"):
            continue
        if "debug" in base:
            continue

        ap = os.path.abspath(p)
        if ap in seen:
            continue
        seen.add(ap)
        out.append(ap)
    return out


def _stage_macro_inputs(state: dict, workflow_dir: str, work_stage_dir: str) -> tuple[list[str], list[str], list[str]]:
    digital = state.get("digital") or {}

    macro_lefs = [p for p in (digital.get("macro_lefs") or []) if p and os.path.exists(p)]
    macro_libs = [p for p in (digital.get("macro_libs") or []) if p and os.path.exists(p)]
    macro_gds  = [p for p in (digital.get("macro_gds") or []) if p and os.path.exists(p)]

    if not macro_lefs:
        macro_lefs = _resolve_macro_files_from_workflow(workflow_dir, (".lef",))
    if not macro_libs:
        macro_libs = _resolve_macro_files_from_workflow(workflow_dir, (".lib", ".lib.gz", ".db"))
    if not macro_gds:
        macro_gds = _resolve_macro_files_from_workflow(workflow_dir, (".gds", ".gds.gz"))

    inputs_dir = os.path.join(work_stage_dir, "inputs", "macros")
    lef_dir = os.path.join(inputs_dir, "lef")
    lib_dir = os.path.join(inputs_dir, "lib")
    gds_dir = os.path.join(inputs_dir, "gds")
    _ensure_dir(lef_dir)
    _ensure_dir(lib_dir)
    _ensure_dir(gds_dir)

    staged_lefs, staged_libs, staged_gds = [], [], []

    for src in macro_lefs:
        dst = os.path.join(lef_dir, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        staged_lefs.append(f"dir::inputs/macros/lef/{os.path.basename(src)}")

    for src in macro_libs:
        dst = os.path.join(lib_dir, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        staged_libs.append(f"dir::inputs/macros/lib/{os.path.basename(src)}")

    for src in macro_gds:
        dst = os.path.join(gds_dir, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        staged_gds.append(f"dir::inputs/macros/gds/{os.path.basename(src)}")

    return staged_lefs, staged_libs, staged_gds

def run_agent(state: dict) -> dict:

    print(f"\n🏁 Running {AGENT_NAME}...")
    logger.info(f"🏁 Running {AGENT_NAME}")

    workflow_id=state.get("workflow_id","default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "route")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")
    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)


    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Route: missing upstream SDC")

    sdc_basename = os.path.basename(upstream_sdc)
    stage_sdc = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)

    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    logger.info(f"{AGENT_NAME}: upstream_sdc={upstream_sdc}")
    logger.info(f"{AGENT_NAME}: staged_sdc={stage_sdc}")


    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Route: missing OpenLane config")

    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")
    cfg = _read_json(base_cfg_path)

    cfg.pop("SYNTH_SDC_FILE", None)
    cfg["RUN_LINTER"] = False

    # --- Shared run_work_dir must be defined BEFORE using it ---
    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    # Option A: point SDC to shared inputs
    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"
    cfg["RUN_SPEF_EXTRACTION"] = True


    # --------------------------------------------------
    # NETLIST STAGING (FIX — same pattern as CTS)
    # --------------------------------------------------

    route_netlists = _resolve_route_input_netlists(state, workflow_dir)
    if not route_netlists:
        raise RuntimeError("Route: missing CTS netlist output")

    netlist_dir = os.path.join(stage_dir, "netlist")
    _ensure_dir(netlist_dir)

    # Copy to stage_dir
    for nl in route_netlists:
        shutil.copy2(nl, os.path.join(netlist_dir, os.path.basename(nl)))

    stage_netlists = sorted(glob.glob(os.path.join(netlist_dir, "*.v")))
    if not stage_netlists:
        raise RuntimeError("Route: no staged netlists found")

    # --------------------------------------------------
    # SHARED INPUTS (same as CTS)
    # --------------------------------------------------

    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")

    _ensure_dir(inputs_netlist_dir)
    _ensure_dir(inputs_constraints_dir)

    # Clean old netlists (IMPORTANT)
    for old in glob.glob(os.path.join(inputs_netlist_dir, "*.v")):
        try:
            os.remove(old)
        except Exception:
            pass

    # Copy netlists into shared inputs
    for nl in stage_netlists:
        shutil.copy2(nl, os.path.join(inputs_netlist_dir, os.path.basename(nl)))

    # Copy SDC
    shutil.copy2(stage_sdc, os.path.join(inputs_constraints_dir, sdc_basename))

    # --------------------------------------------------
    # CRITICAL: set VERILOG_FILES explicitly
    # --------------------------------------------------

    stage_netlists = [
        p for p in stage_netlists
        if not p.endswith(".nl.v") and not p.endswith(".pnl.v")
    ]

    if not stage_netlists:
        raise RuntimeError("Route: no valid logical netlists remain after filtering physical netlists")

    logger.info(
        f"{AGENT_NAME}: filtered route stage netlists -> "
        f"{[os.path.basename(p) for p in stage_netlists]}"
    )

    cfg["VERILOG_FILES"] = [
        f"inputs/netlist/{os.path.basename(p)}"
        for p in stage_netlists
    ]


    # Fix DESIGN_NAME if needed
    inferred = None
    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(stage_netlists[0])
    if inferred:
        cfg["DESIGN_NAME"] = inferred
        state["design_name"] = inferred

    top_module = str(cfg.get("DESIGN_NAME", "")).strip() or "top"
    # Write stage contract config
    _write_text(os.path.join(stage_dir, "config.json"), json.dumps(cfg, indent=2))
    

    pdk=state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    image=state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host

    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

        
    work_stage_dir = os.path.join(run_work_dir, "route")
    _ensure_dir(work_stage_dir)

    staged_lefs, staged_libs, staged_gds = _stage_macro_inputs(state, workflow_dir, work_stage_dir)

    if staged_lefs:
        cfg["EXTRA_LEFS"] = staged_lefs
    if staged_libs:
        cfg["EXTRA_LIBS"] = staged_libs
    if staged_gds:
        cfg["EXTRA_GDS_FILES"] = staged_gds

    logger.info(f"{AGENT_NAME}: staged macro LEFs -> {staged_lefs}")
    logger.info(f"{AGENT_NAME}: staged macro LIBs -> {staged_libs}")
    logger.info(f"{AGENT_NAME}: staged macro GDS -> {staged_gds}")
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
        f"verilog_files_mode=explicit_from_synth_only",
        f"verilog_files={','.join(cfg.get('VERILOG_FILES', []))}",
    ]) + "\n"

    
    _write_text(os.path.join(logs_dir, "route_input_resolution.log"), input_log)

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
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to OpenROAD.STAPostPNR route/config.json'
"""


    _write_text(os.path.join(stage_dir,"run.sh"), run_sh)
    os.chmod(os.path.join(stage_dir,"run.sh"), 0o755)

    rc,out=_run(["bash","-lc","./run.sh"], cwd=stage_dir)
    _write_text(os.path.join(logs_dir,"openlane_route.log"), out)

    latest=_latest_run_dir(run_work_dir)
    metrics_path =_copy_metrics(latest, stage_dir)
    def_path=_copy_def(latest, stage_dir)
    route_netlist_path = _copy_route_netlist(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "sdc": f"digital/route/constraints/{sdc_basename}",
            "metrics_json": "digital/route/metrics.json" if metrics_path else None,
            "primary_def": "digital/route/primary.def" if def_path else None,
            "route_netlist": f"digital/route/netlist/{os.path.basename(route_netlist_path)}" if route_netlist_path else None,
            "log": "digital/route/logs/openlane_route.log",
            "openlane_run_dir": latest,
        },
    }

    _write_text(os.path.join(stage_dir, "route_summary.json"), json.dumps(summary, indent=2))
    _write_text(
        os.path.join(stage_dir, "route_summary.md"),
        f"# Route\n\n- status: `{summary['status']}` (rc={rc})\n",
    )

    try:
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "route/config.json",
            json.dumps(cfg, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            f"route/constraints/{sdc_basename}",
            sdc_text,
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "route/logs/route_input_resolution.log",
            input_log,
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "route/run.sh",
            run_sh,
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "route/logs/openlane_route.log",
            out,
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "route/route_summary.json",
            json.dumps(summary, indent=2),
        )
        if metrics_path and os.path.exists(metrics_path):
            save_text_artifact_and_record(
                workflow_id,
                AGENT_NAME,
                "digital",
                "route/metrics.json",
                open(metrics_path, "r", encoding="utf-8").read(),
            )
        if def_path and os.path.exists(def_path):
            save_text_artifact_and_record(
                workflow_id,
                AGENT_NAME,
                "digital",
                "route/primary.def",
                open(def_path, "r", encoding="utf-8", errors="ignore").read(),
            )
    except Exception as e:
        logger.exception(f"{AGENT_NAME}: artifact upload failed: {e}")

    state.setdefault("digital", {})["route"] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "primary_def": def_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": os.path.join(work_stage_dir, "config.json"),
        "input_resolution_log": os.path.join(logs_dir, "route_input_resolution.log"),
        "openlane_run_dir": latest,
        "route_netlist": route_netlist_path,
        "final_netlist": route_netlist_path,
        "netlist": route_netlist_path,
    }

    
    return state
    
   