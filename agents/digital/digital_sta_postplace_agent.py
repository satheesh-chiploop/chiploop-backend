import os, json, glob, shutil, subprocess, re, logging
from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger("chiploop")

AGENT_NAME = "Digital STA PostPlace Agent"
STAGE_NAME = "sta_postplace"
OPENLANE_TO = "OpenROAD.STAMidPNR"
EXEC_STAGE_DIR = "place"

DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))

def _ensure(p): os.makedirs(p, exist_ok=True)

def _read_json(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _write(p, s):
    _ensure(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)

def _run(cmd, cwd):
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    return p.returncode, out

def _latest_run(run_work_dir):
    runs = os.path.join(run_work_dir, "runs")
    if not os.path.isdir(runs):
        return None
    ds = [os.path.join(runs, d) for d in os.listdir(runs) if os.path.isdir(os.path.join(runs, d))]
    if not ds:
        return None
    ds.sort(key=lambda x: os.path.getmtime(x))
    return ds[-1]

def _copy_metrics(latest, stage_dir):
    if not latest:
        return None
    src = os.path.join(latest, "final", "metrics.json")
    dst = os.path.join(stage_dir, "metrics.json")
    if os.path.exists(src):
        shutil.copy2(src, dst)
        return dst
    return None

def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None

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
            return hits[0]

    return None

def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    place_state = digital.get("place") or {}

    cand = _first_existing([
        place_state.get("openlane_config"),
        place_state.get("config_json"),
        os.path.join(workflow_dir, "digital", "place", "config.json"),
    ])
    if cand:
        return cand

    for cand in [
        os.path.join(workflow_dir, "digital", "impl_setup", "openlane", "config.json"),
        os.path.join(workflow_dir, "digital", "synth", "config.json"),
        os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json"),
    ]:
        if os.path.exists(cand):
            return cand

    return None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    place_state = digital.get("place") or {}

    cand = _first_existing([
        place_state.get("constraints_sdc"),
        os.path.join(workflow_dir, "digital", "place", "constraints", "digital_subsystem.sdc"),
    ])
    if cand:
        return cand

    for stage in ["place", "floorplan", "impl_setup", "synth"]:
        cand_dir = os.path.join(workflow_dir, "digital", stage, "constraints")
        for cand in sorted(glob.glob(os.path.join(cand_dir, "*.sdc"))):
            if os.path.exists(cand):
                return cand

    legacy = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))
    for cand in legacy:
        if os.path.exists(cand):
            return cand

    return None


def _resolve_postplace_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    place_state = digital.get("place") or {}

    candidates = [
        place_state.get("netlist"),
        place_state.get("final_netlist"),
        place_state.get("place_netlist"),
    ]

    latest_run = place_state.get("openlane_run_dir")
    picked = _pick_stage_netlist(latest_run) if latest_run else None
    if picked:
        candidates.append(picked)

    # Also inspect shared run_work/place runs directly
    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    place_runs = sorted(
        glob.glob(os.path.join(run_work_dir, "place", "runs", "*")),
        key=os.path.getmtime
    )
    if place_runs:
        picked = _pick_stage_netlist(place_runs[-1])
        if picked:
            candidates.append(picked)

    candidates.extend([
        os.path.join(workflow_dir, "digital", "place", "netlist", "digital_subsystem_place.v"),
        os.path.join(workflow_dir, "digital", "place", "netlist", "digital_subsystem.v"),
    ])

    cand = _first_existing(candidates)
    if cand:
        logger.info(f"{AGENT_NAME}: selected post-place netlist -> {cand}")
    else:
        logger.warning(f"{AGENT_NAME}: no post-place netlist found")
    return cand
    

def _stage_macro_inputs(state: dict, work_stage_dir: str):
    digital = state.get("digital") or {}

    macro_lefs = [p for p in (digital.get("macro_lefs") or []) if p and os.path.exists(p)]
    macro_libs = [p for p in (digital.get("macro_libs") or []) if p and os.path.exists(p)]
    macro_gds  = [p for p in (digital.get("macro_gds") or []) if p and os.path.exists(p)]

    inputs_macros_dir = os.path.join(work_stage_dir, "inputs", "macros")
    lef_dir = os.path.join(inputs_macros_dir, "lef")
    lib_dir = os.path.join(inputs_macros_dir, "lib")
    gds_dir = os.path.join(inputs_macros_dir, "gds")

    _ensure(lef_dir)
    _ensure(lib_dir)
    _ensure(gds_dir)

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

    stage_dir = os.path.join(workflow_dir, "digital", STAGE_NAME)
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure(stage_dir); _ensure(logs_dir)

    base_cfg = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg:
        raise RuntimeError("Missing OpenLane config: no config found in place, impl_setup, synth, or foundry.")

    cfg = _read_json(base_cfg)  

    cfg.pop("SYNTH_SDC_FILE", None)
    cfg["RUN_LINTER"] = False

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    work_stage_dir = os.path.join(run_work_dir, EXEC_STAGE_DIR)
    _ensure(work_stage_dir)
    staged_lefs, staged_libs, staged_gds = _stage_macro_inputs(state, work_stage_dir)

    if staged_lefs:
        cfg["EXTRA_LEFS"] = staged_lefs
    if staged_libs:
        cfg["EXTRA_LIBS"] = staged_libs
    if staged_gds:
        cfg["EXTRA_GDS_FILES"] = staged_gds



    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC: no constraints_sdc found in state or prior digital stages.")

    sdc_basename = os.path.basename(upstream_sdc)

    inputs_constraints_dir = os.path.join(run_work_dir, "inputs", "constraints")
    _ensure(inputs_constraints_dir)
    stage_sdc = os.path.join(inputs_constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)
    sdc_text = open(stage_sdc, "r", encoding="utf-8").read()

    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"

    inputs_netlist_dir = os.path.join(run_work_dir, "inputs", "netlist")
    _ensure(inputs_netlist_dir)

    for old_v in glob.glob(os.path.join(inputs_netlist_dir, "*.v")):
        try:
            os.remove(old_v)
        except Exception:
            pass

    postplace_netlist = _resolve_postplace_netlist(state, workflow_dir)
    if not postplace_netlist:
        raise RuntimeError("STA postplace: missing place netlist output.")

    staged_postplace_netlist = os.path.join(inputs_netlist_dir, os.path.basename(postplace_netlist))
    shutil.copy2(postplace_netlist, staged_postplace_netlist)

    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(staged_postplace_netlist)}"]

    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(staged_postplace_netlist)
        if inferred:
            cfg["DESIGN_NAME"] = inferred
            state["design_name"] = inferred

    _write(os.path.join(stage_dir, "config.json"), json.dumps(cfg, indent=2))

    explicit = state.get("digital_run_tag") or state.get("run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    pdk = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE

    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host


    _write(os.path.join(work_stage_dir, "config.json"), json.dumps(cfg, indent=2))

    input_log = "\n".join([
        f"{AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={workflow_dir}",
        f"base_cfg_path={base_cfg}",
        f"upstream_sdc={upstream_sdc}",
        f"stage_sdc={stage_sdc}",
        f"run_work_dir={run_work_dir}",
        f"run_tag={run_tag}",
        f"resolved_postplace_netlist={postplace_netlist}",
        f"staged_postplace_netlist={staged_postplace_netlist}",
        f"netlist_count=1",
        f"macro_lef_count={len(staged_lefs)}",
        f"macro_lib_count={len(staged_libs)}",
        f"macro_gds_count={len(staged_gds)}",
    ]) + "\n"
    _write(os.path.join(logs_dir, "sta_postplace_input_resolution.log"), input_log)

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail
export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}
docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk} \\
  -e PDK_ROOT=/pdk \\
  {image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to {OPENLANE_TO} {EXEC_STAGE_DIR}/config.json'
"""
    _write(os.path.join(stage_dir, "run.sh"), run_sh)
    os.chmod(os.path.join(stage_dir, "run.sh"), 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir)
    _write(os.path.join(logs_dir, "openlane_sta_postplace.log"), out)

    latest = _latest_run(run_work_dir)
    metrics = _copy_metrics(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "metrics_json": "digital/sta_postplace/metrics.json" if metrics else None,
            "log": "digital/sta_postplace/logs/openlane_sta_postplace.log",
            "openlane_run_dir": latest,
        },
    }
    _write(os.path.join(stage_dir, "sta_postplace_summary.json"), json.dumps(summary, indent=2))
    _write(os.path.join(stage_dir, "sta_postplace_summary.md"),
           f"# STA PostPlace\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/openlane_sta_postplace.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/sta_postplace_input_resolution.log", input_log)
        if metrics and os.path.exists(metrics):
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/metrics.json",
                                          open(metrics, "r", encoding="utf-8").read())
    except Exception as e:
        print(f"⚠️ upload failed: {e}")

    state.setdefault("digital", {})[STAGE_NAME] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics,
        "openlane_run_dir": latest,
        "constraints_sdc": stage_sdc,
        "openlane_config": os.path.join(stage_dir, "config.json"),
        "input_resolution_log": os.path.join(logs_dir, "sta_postplace_input_resolution.log"),
        "netlist": staged_postplace_netlist,
    }
    
    return state