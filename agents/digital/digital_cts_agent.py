import os, json, glob, shutil, subprocess
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

def _latest_run(stage_dir):
    runs = os.path.join(stage_dir, "runs")
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

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id","default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"

    stage_dir = os.path.join(workflow_dir, "digital", "cts")
    logs_dir = os.path.join(stage_dir, "logs")
    cons_dir = os.path.join(stage_dir, "constraints")
    _ensure(stage_dir); _ensure(logs_dir); _ensure(cons_dir)

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
    cfg["SYNTH_SDC_FILE"] = "constraints/top.sdc"
    cfg["PNR_SDC_FILE"] = "constraints/top.sdc"
    config_path = os.path.join(stage_dir, "config.json")
    _write(config_path, json.dumps(cfg, indent=2))

    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "/root/chiploop-backend/backend/pdk"
    run_tag = f"cts_{workflow_id}"

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail
export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}
docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "$(pwd)":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --to OpenROAD.CTS config.json'
"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write(run_sh_path, run_sh); os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash","-lc","./run.sh"], cwd=stage_dir)
    log_path = os.path.join(logs_dir, "openlane_cts.log")
    _write(log_path, out)

    latest = _latest_run(stage_dir)
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