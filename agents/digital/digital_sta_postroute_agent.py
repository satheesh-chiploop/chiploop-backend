import os, json, glob, shutil, subprocess, re
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital STA PostRoute Agent"
STAGE_NAME = "sta_postroute"
OPENLANE_TO = "OpenROAD.STAPostPNR"

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

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", STAGE_NAME)
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure(stage_dir); _ensure(logs_dir)

    impl_cfg = os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json")
    synth_cfg = os.path.join(workflow_dir, "digital", "synth", "config.json")
    base_cfg = impl_cfg if os.path.exists(impl_cfg) else synth_cfg
    if not os.path.exists(base_cfg):
        raise RuntimeError("Missing base config.json (foundry/openlane or synth).")

    cfg = _read_json(base_cfg)
    cfg.pop("SYNTH_SDC_FILE", None)

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    upstream_sdc = os.path.join(workflow_dir, "digital", "constraints", "top.sdc")
    if not os.path.exists(upstream_sdc):
        raise RuntimeError("Missing upstream SDC: digital/constraints/top.sdc")

    inputs_constraints_dir = os.path.join(run_work_dir, "inputs", "constraints")
    _ensure(inputs_constraints_dir)
    stage_sdc = os.path.join(inputs_constraints_dir, "top.sdc")
    shutil.copy2(upstream_sdc, stage_sdc)
    sdc_text = open(stage_sdc, "r", encoding="utf-8").read()

    cfg["PNR_SDC_FILE"] = "inputs/constraints/top.sdc"

    inputs_netlist_dir = os.path.join(run_work_dir, "inputs", "netlist")
    stage_netlists = sorted(glob.glob(os.path.join(inputs_netlist_dir, "*.v")))
    if not stage_netlists:
        raise RuntimeError("STA postroute: missing run_work/inputs/netlist/*.v.")
    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(p)}" for p in stage_netlists]

    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(stage_netlists[0])
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

    work_stage_dir = os.path.join(run_work_dir, STAGE_NAME)
    _ensure(work_stage_dir)
    _write(os.path.join(work_stage_dir, "config.json"), json.dumps(cfg, indent=2))

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail
export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}
docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk} \\
  -e PDK_ROOT=/pdk \\
  {image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --to {OPENLANE_TO} {STAGE_NAME}/config.json'
"""
    _write(os.path.join(stage_dir, "run.sh"), run_sh)
    os.chmod(os.path.join(stage_dir, "run.sh"), 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir)
    _write(os.path.join(logs_dir, "openlane_sta_postroute.log"), out)

    latest = _latest_run(run_work_dir)
    metrics = _copy_metrics(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "metrics_json": "digital/sta_postroute/metrics.json" if metrics else None,
            "log": "digital/sta_postroute/logs/openlane_sta_postroute.log",
            "openlane_run_dir": latest,
        },
    }
    _write(os.path.join(stage_dir, "sta_postroute_summary.json"), json.dumps(summary, indent=2))
    _write(os.path.join(stage_dir, "sta_postroute_summary.md"),
           f"# STA PostRoute\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/openlane_sta_postroute.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/constraints/top.sdc", sdc_text)
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
    }
    return state