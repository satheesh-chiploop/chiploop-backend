import os, json, glob, shutil, subprocess, re
from utils.artifact_utils import save_text_artifact_and_record

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

    # ---- Shared run tag (same as place/cts/route) ----
    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    # ---- Base config (implementation setup preferred) ----
    impl_cfg = os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json")
    synth_cfg = os.path.join(workflow_dir, "digital", "synth", "config.json")
    base_cfg_path = impl_cfg if os.path.exists(impl_cfg) else synth_cfg
    if not os.path.exists(base_cfg_path):
        raise RuntimeError("Missing OpenLane config. Expected digital/foundry/openlane/config.json or digital/synth/config.json")

    cfg = _read_json(base_cfg_path)
    cfg.pop("SYNTH_SDC_FILE", None)

    # ---- SSOT SDC -> run_work/inputs/constraints/top.sdc ----
    upstream_sdc = os.path.join(workflow_dir, "digital", "constraints", "top.sdc")
    if not os.path.exists(upstream_sdc):
        raise RuntimeError("Missing upstream SDC: digital/constraints/top.sdc")

    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure_dir(inputs_constraints_dir)
    _ensure_dir(inputs_netlist_dir)

    stage_sdc = os.path.join(inputs_constraints_dir, "top.sdc")
    shutil.copy2(upstream_sdc, stage_sdc)
    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    cfg["PNR_SDC_FILE"] = "inputs/constraints/top.sdc"

    # ---- Netlist from run_work/inputs/netlist/*.v (must already exist) ----
    stage_netlists = sorted(glob.glob(os.path.join(inputs_netlist_dir, "*.v")))
    if not stage_netlists:
        raise RuntimeError("STA preplace: missing run_work/inputs/netlist/*.v (synth must populate it).")

    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(p)}" for p in stage_netlists]

    # Fix DESIGN_NAME if base config says "top" or empty
    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(stage_netlists[0])
        if inferred:
            cfg["DESIGN_NAME"] = inferred
            state["design_name"] = inferred

    # Write contract config (visible under digital/sta_preplace/config.json)
    _write_text(os.path.join(stage_dir, "config.json"), json.dumps(cfg, indent=2))

    # Write exec config under /work/sta_preplace/config.json
    work_stage_dir = os.path.join(run_work_dir, STAGE_NAME)
    _ensure_dir(work_stage_dir)
    _write_text(os.path.join(work_stage_dir, "config.json"), json.dumps(cfg, indent=2))

    # ---- Docker/run.sh (mount run_work_dir) ----
    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "/root/chiploop-backend/backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host

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
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --to {OPENLANE_TO} {STAGE_NAME}/config.json'
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
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/constraints/top.sdc", sdc_text)
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
    }
    return state