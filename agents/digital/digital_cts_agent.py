import os, json, glob, shutil, subprocess, re
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital CTS Agent"
DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))

def _ensure(p): os.makedirs(p, exist_ok=True)

def _read_json(p):
    try:
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def _write(p, s):
    _ensure(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as f: f.write(s)

def _run(cmd, cwd):
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    return p.returncode, out

def _latest_run(run_work_dir):
    runs = os.path.join(run_work_dir, "runs")
    if not os.path.isdir(runs): return None
    ds = [os.path.join(runs, d) for d in os.listdir(runs) if os.path.isdir(os.path.join(runs, d))]
    if not ds: return None
    ds.sort(key=lambda x: os.path.getmtime(x))
    return ds[-1]

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

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id","default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "cts")
    logs_dir = os.path.join(stage_dir, "logs")
    cons_dir = os.path.join(stage_dir, "constraints")
    _ensure(stage_dir); _ensure(logs_dir); _ensure(cons_dir)
    netlist_dir = os.path.join(stage_dir, "netlist")
    _ensure(netlist_dir)

    synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    if not synth_netlists:
        synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "**", "*.v"), recursive=True))
    if not synth_netlists:
        raise RuntimeError("No synthesized netlist found. Expected digital/synth/netlist/*.v")

    for nl in synth_netlists:
        shutil.copy2(nl, os.path.join(netlist_dir, os.path.basename(nl)))

    # SDC: single source
    upstream_sdc = os.path.join(workflow_dir, "digital", "constraints", "top.sdc")
    if not os.path.exists(upstream_sdc):
        raise RuntimeError("Missing upstream SDC: digital/constraints/top.sdc")
    stage_sdc = os.path.join(cons_dir, "top.sdc")
    shutil.copy2(upstream_sdc, stage_sdc)
    sdc_text = open(stage_sdc, "r", encoding="utf-8").read()

    # Config baseline
    impl_cfg = os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json")
    synth_cfg = os.path.join(workflow_dir, "digital", "synth", "config.json")
    base_cfg_path = impl_cfg if os.path.exists(impl_cfg) else synth_cfg
    if not os.path.exists(base_cfg_path):
        raise RuntimeError("Missing config: digital/foundry/openlane/config.json or digital/synth/config.json")

    cfg = _read_json(base_cfg_path)
    cfg.pop("SYNTH_SDC_FILE", None)
    cfg["PNR_SDC_FILE"] = "inputs/constraints/top.sdc"

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

    config_path = os.path.join(stage_dir, "config.json")
    _write(config_path, json.dumps(cfg, indent=2))



    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE

    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host


    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    flow_run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = flow_run_tag
    run_tag = flow_run_tag

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    work_stage_dir = os.path.join(run_work_dir, "cts")
    _ensure(work_stage_dir)

    exec_config_path = os.path.join(work_stage_dir, "config.json")
    _write(exec_config_path, json.dumps(cfg, indent=2))

    # Option A: shared inputs under /work/inputs
    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure(inputs_constraints_dir)
    _ensure(inputs_netlist_dir)

    shutil.copy2(stage_sdc, os.path.join(inputs_constraints_dir, "top.sdc"))
    for p in stage_netlists:
        shutil.copy2(p, os.path.join(inputs_netlist_dir, os.path.basename(p)))

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail
export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \
  -v "{pdk_root_host}":/pdk \
  -v "{run_work_dir}":/work \
  -e PDK={pdk_variant} \
  -e PDK_ROOT=/pdk \
  {image} \
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --to OpenROAD.CTS cts/config.json'

"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write(run_sh_path, run_sh); os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash","-lc","./run.sh"], cwd=stage_dir)
    log_path = os.path.join(logs_dir, "openlane_cts.log")
    _write(log_path, out)

    latest = _latest_run(run_work_dir)
    metrics = _copy_metrics(latest, stage_dir)
    primary_def = _copy_def(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "metrics_json": "digital/cts/metrics.json" if metrics else None,
            "primary_def": "digital/cts/primary.def" if primary_def else None,
            "log": "digital/cts/logs/openlane_cts.log",
            "openlane_run_dir": latest
        }
    }
    _write(os.path.join(stage_dir,"cts_summary.json"), json.dumps(summary, indent=2))
    _write(os.path.join(stage_dir,"cts_summary.md"), f"# CTS\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/constraints/top.sdc", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/logs/openlane_cts.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/cts_summary.json", json.dumps(summary, indent=2))
        if metrics and os.path.exists(metrics):
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/metrics.json", open(metrics,"r",encoding="utf-8").read())
        if primary_def and os.path.exists(primary_def):
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "cts/primary.def", open(primary_def,"r",encoding="utf-8",errors="ignore").read())
    except Exception as e:
        print(f"⚠️ upload failed: {e}")

    state.setdefault("digital", {})["cts"] = {"status": summary["status"], "stage_dir": stage_dir, "metrics_json": metrics, "primary_def": primary_def}
    return state