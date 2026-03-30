import os
import json
import glob
import shutil
import subprocess
import re
import logging
logger = logging.getLogger("chiploop")

from datetime import datetime

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Floorplan Agent"

DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _run(cmd: list[str], cwd: str) -> tuple[int, str]:
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    return p.returncode, out


def _latest_run_dir(run_work_dir: str) -> str | None:
    runs_dir = os.path.join(run_work_dir, "runs")
    if not os.path.isdir(runs_dir):
        return None
    dirs = [os.path.join(runs_dir, d) for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d))]
    if not dirs:
        return None
    dirs.sort(key=lambda p: os.path.getmtime(p))
    return dirs[-1]


def _copy_metrics(latest: str | None, stage_dir: str) -> str | None:
    if not latest:
        return None
    src = os.path.join(latest, "final", "metrics.json")
    dst = os.path.join(stage_dir, "metrics.json")
    if os.path.exists(src):
        shutil.copy2(src, dst)
        return dst
    return None


def _copy_primary_def(latest: str | None, stage_dir: str) -> str | None:
    if not latest:
        return None
    dst = os.path.join(stage_dir, "primary.def")
    candidates = []
    candidates += glob.glob(os.path.join(latest, "final", "def", "*.def"))
    candidates += glob.glob(os.path.join(latest, "results", "**", "*.def"), recursive=True)
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getsize(p))
    shutil.copy2(candidates[-1], dst)
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

def _resolve_macro_files_from_workflow(workflow_dir: str, exts: tuple[str, ...]) -> list[str]:
    hits = []
    for ext in exts:
        hits.extend(glob.glob(os.path.join(workflow_dir, "**", f"*{ext}"), recursive=True))

    out = []
    seen = set()
    for p in sorted(hits):
        base = os.path.basename(p).lower()

        # reject debug/raw scratch artifacts
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



def _generate_macro_placement(macro_master: str, macro_name: str = "u_analog") -> str:
    """
    Generate a simple DEF fragment for macro placement.
    macro_name   = instance name in the top netlist
    macro_master = LEF macro/master cell name
    """
    return f"""VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN floorplan_macro_template ;
UNITS DISTANCE MICRONS 1000 ;
COMPONENTS 1 ;
- {macro_name} {macro_master} + FIXED ( 100 100 ) N ;
END COMPONENTS
END DESIGN
"""

def _generate_macro_placement_cfg(macro_name: str = "u_analog") -> str:
    """
    OpenLane macro placement config format:
    <instance_name> <x> <y> <orientation>
    """
    return f"{macro_name} 100 100 N\n"

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "floorplan")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")

    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)


    

    # Single source-of-truth SDC from Arch2RTL

    logger.info(f"🏁 Running {AGENT_NAME}")
    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC: no constraints_sdc found in state, impl_setup, synth, or legacy constraints.")

    sdc_basename = os.path.basename(upstream_sdc)
    stage_sdc = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)
    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    logger.info(f"{AGENT_NAME}: upstream_sdc={upstream_sdc}")
    logger.info(f"{AGENT_NAME}: staged_sdc={stage_sdc}")


    # Use Implementation Setup config if present; else synth config
    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Missing OpenLane config: no config found in state, impl_setup, synth, or foundry.")
    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")
 

    # Add near the top with other dirs
    netlist_dir = os.path.join(stage_dir, "netlist")
    _ensure_dir(netlist_dir)

    cfg = _read_json(base_cfg_path)

    # 1) Do NOT set SYNTH_SDC_FILE here (OpenLane2 warns unknown in your logs)
    cfg.pop("SYNTH_SDC_FILE", None)

    # 2) Always stage-local SSOT SDC
    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"

    # 3) Skip Verilator lint for macro-backed mixed-signal/system PD reuse
    cfg["RUN_LINTER"] = False

    # 3) Ensure netlist exists locally (so VERILOG_FILES resolves inside /work mount)
    synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    if not synth_netlists:
        synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "**", "*.v"), recursive=True))
    if not synth_netlists:
        raise RuntimeError("No synthesized netlist found. Expected digital/synth/netlist/*.v")

    for nl in synth_netlists:
       shutil.copy2(nl, os.path.join(netlist_dir, os.path.basename(nl)))

    stage_netlists = sorted(glob.glob(os.path.join(netlist_dir, "*.v")))
    if not stage_netlists:
       raise RuntimeError(f"No netlists copied into {netlist_dir}")

    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(p)}" for p in stage_netlists]

    
    spec_dir = os.path.join(workflow_dir, "spec")
    spec_jsons = sorted(glob.glob(os.path.join(spec_dir, "*_spec.json")))
    spec = _read_json(spec_jsons[0]) if spec_jsons else {}

    top_module = (
        spec.get("design_name")
        or spec.get("top_module")
        or state.get("design_name")
        or cfg.get("DESIGN_NAME")
        or "top"
    )
    top_module = str(top_module).strip() or "top"

    # If still defaulting to 'top', infer from the synthesized netlist in this stage
    inferred = None
    if top_module == "top":
        stage_netlists = sorted(glob.glob(os.path.join(netlist_dir, "*.v")))
        inferred = _infer_top_from_netlist(stage_netlists[0]) if stage_netlists else None
    if inferred:
        top_module = inferred

    cfg["DESIGN_NAME"] = top_module

 

    # ---- Docker/run.sh ----
    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "/root/chiploop-backend/backend/pdk"

    # -------------------------------------------------------
    # Shared OpenLane run workspace + run tag (generic)
    # -------------------------------------------------------

    # -------------------------------------------------------
    # Shared OpenLane run workspace + run tag (generic)
    # MUST be defined BEFORE using work_stage_dir
    # -------------------------------------------------------
    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    work_stage_dir = os.path.join(run_work_dir, "floorplan")
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

    # DEBUG HARDCODE: prove macro handoff works first
    macro_inst_name = "u_analog"
    macro_master_name = "analog_subsystem"
    macro_x = 100
    macro_y = 100
    macro_orient = "N"

    # 1) legacy macro placement cfg
    macro_cfg_path = os.path.join(work_stage_dir, "macro_placement.cfg")
    macro_cfg = f"{macro_inst_name} {macro_x} {macro_y} {macro_orient}\n"
    _write_text(macro_cfg_path, macro_cfg)

   

    logger.info(f"{AGENT_NAME}: macro placement CFG generated -> {macro_cfg_path}")
    

    # Force all three for debug
    cfg["MACRO_PLACEMENT_CFG"] = "floorplan/macro_placement.cfg"
    cfg["PL_SKIP_INITIAL_PLACEMENT"] = True
    cfg.pop("FP_DEF_TEMPLATE", None)

    # Optional: also force MACROS for debug only
    #cfg["MACROS"] = {
    #   macro_inst_name: {
    #        "location": [macro_x, macro_y],
    #       "orientation": macro_orient
    #   }
    #}
    cfg.pop("MACROS", None)

    logger.info(f"{AGENT_NAME}: cfg['MACRO_PLACEMENT_CFG']={cfg.get('MACRO_PLACEMENT_CFG')}")
    logger.info(f"{AGENT_NAME}: cfg['FP_DEF_TEMPLATE']={cfg.get('FP_DEF_TEMPLATE')}")
    logger.info(f"{AGENT_NAME}: cfg['PL_SKIP_INITIAL_PLACEMENT']={cfg.get('PL_SKIP_INITIAL_PLACEMENT')}")
    logger.info(f"{AGENT_NAME}: cfg['MACROS']={cfg.get('MACROS')}")

    # Now safe to write execution config into shared workspace
    exec_config_path = os.path.join(work_stage_dir, "config.json")
    _write_text(exec_config_path, json.dumps(cfg, indent=2))

    # Also keep a copy in stage_dir for contract/debug
    config_path = os.path.join(stage_dir, "config.json")
    _write_text(config_path, json.dumps(cfg, indent=2))

    # Now it is safe to copy constraints/netlists into work_stage_dir
    # Shared inputs live at /work/inputs so relative paths resolve from cd /work
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
        f"macro_lef_count={len(staged_lefs)}",
        f"macro_lib_count={len(staged_libs)}",
        f"macro_gds_count={len(staged_gds)}",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "floorplan_input_resolution.log"), input_log)


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
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to OpenROAD.Floorplan floorplan/config.json'


"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir)
    log_path = os.path.join(logs_dir, "openlane_floorplan.log")
    _write_text(log_path, out)

    latest = _latest_run_dir(run_work_dir)
    metrics_path = _copy_metrics(latest, stage_dir)
    def_path = _copy_primary_def(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "sdc": f"digital/floorplan/constraints/{sdc_basename}",
            "metrics_json": "digital/floorplan/metrics.json" if metrics_path else None,
            "primary_def": "digital/floorplan/primary.def" if def_path else None,
            "log": "digital/floorplan/logs/openlane_floorplan.log",
            "openlane_run_dir": latest,
        },
    }

    _write_text(os.path.join(stage_dir, "floorplan_summary.json"), json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir, "floorplan_summary.md"), f"# Floorplan\n\n- status: `{summary['status']}` (rc={rc})\n")

    # ---- Upload text artifacts (DEF is text; ok to upload for small designs) ----
    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "floorplan/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"floorplan/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "floorplan/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "floorplan/logs/openlane_floorplan.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "floorplan/floorplan_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "floorplan/macro_placement.cfg", macro_cfg)
        if metrics_path and os.path.exists(metrics_path):
            with open(metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "floorplan/metrics.json", f.read())
        if def_path and os.path.exists(def_path):
            with open(def_path, "r", encoding="utf-8", errors="ignore") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "floorplan/primary.def", f.read())
    except Exception as e:
        print(f"⚠️ {AGENT_NAME} upload failed: {e}")

    state.setdefault("digital", {})["floorplan"] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "primary_def": def_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": config_path,
        "input_resolution_log": os.path.join(logs_dir, "floorplan_input_resolution.log"),
        "openlane_run_dir": latest,
    }

    return state