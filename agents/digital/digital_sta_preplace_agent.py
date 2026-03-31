

import os, json, glob, shutil, subprocess, re, logging
from datetime import datetime
from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger("chiploop")

AGENT_NAME = "Digital STA PrePlace Agent"
STAGE_NAME = "sta_preplace"
OPENLANE_TO = "OpenROAD.STAPrePNR"

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

def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None

def _latest_run(run_work_dir: str) -> str | None:
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




def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _resolve_preplace_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    synth_state = digital.get("synth") or {}

    candidates = [
        synth_state.get("netlist"),
        synth_state.get("final_netlist"),
        synth_state.get("synth_netlist"),
    ]

    xs = synth_state.get("rtl_files")
    if isinstance(xs, list):
        existing = [p for p in xs if p and os.path.exists(p)]
        if len(existing) == 1:
            logger.info(f"{AGENT_NAME}: selected synth netlist from state.digital.synth.rtl_files -> {existing[0]}")
            return existing[0]

    candidates.extend([
        os.path.join(workflow_dir, "digital", "synth", "netlist", "digital_subsystem_synth.v"),
        os.path.join(workflow_dir, "digital", "synth", "netlist", "digital_subsystem.v"),
    ])

    cand = _first_existing(candidates)
    if cand:
        logger.info(f"{AGENT_NAME}: selected preplace synth netlist -> {cand}")
        return cand

    xs = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    if len(xs) == 1:
        logger.info(f"{AGENT_NAME}: selected single synth netlist fallback -> {xs[0]}")
        return xs[0]

    logger.warning(f"{AGENT_NAME}: no deterministic synth netlist found")
    return None

def _stage_macro_inputs(state: dict, work_stage_dir: str):
    digital = state.get("digital") or {}

    macro_lefs = [p for p in (digital.get("macro_lefs") or []) if p and os.path.exists(p)]
    macro_libs = [p for p in (digital.get("macro_libs") or []) if p and os.path.exists(p)]
    macro_gds  = [p for p in (digital.get("macro_gds") or []) if p and os.path.exists(p)]

    inputs_macros_dir = os.path.join(work_stage_dir, "inputs", "macros")
    lef_dir = os.path.join(inputs_macros_dir, "lef")
    lib_dir = os.path.join(inputs_macros_dir, "lib")
    gds_dir = os.path.join(inputs_macros_dir, "gds")

    _ensure_dir(lef_dir)
    _ensure_dir(lib_dir)
    _ensure_dir(gds_dir)

    staged_lefs = []
    staged_libs = []
    staged_gds = []

    for src in macro_lefs:
        dst = os.path.join(lef_dir, os.path.basename(src))
        shutil.copy2(src, dst)
        staged_lefs.append(f"dir::inputs/macros/lef/{os.path.basename(src)}")

    for src in macro_libs:
        dst = os.path.join(lib_dir, os.path.basename(src))
        shutil.copy2(src, dst)
        staged_libs.append(f"dir::inputs/macros/lib/{os.path.basename(src)}")

    for src in macro_gds:
        dst = os.path.join(gds_dir, os.path.basename(src))
        shutil.copy2(src, dst)
        staged_gds.append(f"dir::inputs/macros/gds/{os.path.basename(src)}")

    return staged_lefs, staged_libs, staged_gds

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    # Contract dirs (what you see under digital/sta_preplace)
    stage_dir = os.path.join(workflow_dir, "digital", STAGE_NAME)
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)

    # ---- Shared OpenLane run workspace (Option A) ----
    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir



    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure_dir(inputs_constraints_dir)
    _ensure_dir(inputs_netlist_dir)

    # ---- Shared run tag (same as place/cts/route) ----
    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    # ---- Base config (implementation setup preferred) ----

    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Missing OpenLane config: no config found in state, impl_setup, synth, or foundry.")
    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")
 
    cfg = _read_json(base_cfg_path)
    cfg.pop("SYNTH_SDC_FILE", None)
    cfg["RUN_LINTER"] = False

    work_stage_dir = os.path.join(run_work_dir, STAGE_NAME)
    _ensure_dir(work_stage_dir)

    staged_lefs, staged_libs, staged_gds = _stage_macro_inputs(state, work_stage_dir)

    if staged_lefs:
        cfg["EXTRA_LEFS"] = staged_lefs
    if staged_libs:
        cfg["EXTRA_LIBS"] = staged_libs
    if staged_gds:
        cfg["EXTRA_GDS_FILES"] = staged_gds


    # ---- SSOT SDC -> run_work/inputs/constraints/top.sdc ----
    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC: no constraints_sdc found in state, impl_setup, place, floorplan, synth, or legacy constraints.")

    sdc_basename = os.path.basename(upstream_sdc)
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

    preplace_netlist = _resolve_preplace_netlist(state, workflow_dir)
    if not preplace_netlist:
        raise RuntimeError("STA preplace: missing synthesized netlist output.")

    staged_preplace_netlist = os.path.join(inputs_netlist_dir, os.path.basename(preplace_netlist))
    shutil.copy2(preplace_netlist, staged_preplace_netlist)

    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(staged_preplace_netlist)}"]

    # Fix DESIGN_NAME if base config says "top" or empty
    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(staged_preplace_netlist)
        if inferred:
            cfg["DESIGN_NAME"] = inferred
            state["design_name"] = inferred

    top_module = str(cfg.get("DESIGN_NAME", "")).strip() or "top"

    # Write contract config (visible under digital/sta_preplace/config.json)
    
    _write_text(os.path.join(work_stage_dir, "config.json"), json.dumps(cfg, indent=2))

    # ---- Docker/run.sh (mount run_work_dir) ----
    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "/root/chiploop-backend/backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host

    stage_constraints_dir = os.path.join(stage_dir, "constraints")
    _ensure_dir(stage_constraints_dir)
    stage_contract_sdc = os.path.join(stage_constraints_dir, sdc_basename)
    shutil.copy2(stage_sdc, stage_contract_sdc)

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
        f"resolved_preplace_netlist={preplace_netlist}",
        f"staged_preplace_netlist={staged_preplace_netlist}",
        f"netlist_count=1",
        f"macro_lef_count={len(staged_lefs)}",
        f"macro_lib_count={len(staged_libs)}",
        f"macro_gds_count={len(staged_gds)}",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "sta_preplace_input_resolution.log"), input_log)

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "PDK_VARIANT={pdk_variant}"
echo "OPENLANE_IMAGE={openlane_image}"
echo "WORKDIR=/work"

export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {openlane_image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to {OPENLANE_TO} {STAGE_NAME}/config.json'
"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir)
    log_path = os.path.join(logs_dir, "openlane_sta_preplace.log")
    _write_text(log_path, out)

    latest = _latest_run(run_work_dir)
    metrics_path = _copy_metrics(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "metrics_json": f"digital/{STAGE_NAME}/metrics.json" if metrics_path else None,
            "log": f"digital/{STAGE_NAME}/logs/openlane_sta_preplace.log",
            "openlane_run_dir": latest,
        },
    }
    _write_text(os.path.join(stage_dir, "sta_summary.json"), json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir, "sta_summary.md"),
                f"# STA PrePlace\n\n- status: `{summary['status']}` (rc={rc})\n")

    # ---- Upload (best-effort) ----
    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/openlane_sta_preplace.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/sta_summary.json", json.dumps(summary, indent=2))
        if metrics_path and os.path.exists(metrics_path):
            with open(metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/metrics.json", f.read())
    except Exception as e:
        print(f"⚠️ {AGENT_NAME} upload failed: {e}")


    state.setdefault("digital", {})[STAGE_NAME] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "openlane_run_dir": latest,
        "constraints_sdc": stage_contract_sdc,
        "openlane_config": os.path.join(stage_dir, "config.json"),
        "input_resolution_log": os.path.join(logs_dir, "sta_preplace_input_resolution.log"),
        "netlist": staged_preplace_netlist,
    }

    
    return state