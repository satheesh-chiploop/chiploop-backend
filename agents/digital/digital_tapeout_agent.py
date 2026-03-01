import os, json, glob, shutil, subprocess
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME="Digital Tapeout Agent"
DEFAULT_PDK_VARIANT=os.getenv("CHIPLOOP_PDK_VARIANT","sky130A")
DEFAULT_OPENLANE_IMAGE=os.getenv("CHIPLOOP_OPENLANE_IMAGE","ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES=int(os.getenv("OPENLANE_NUM_CORES","2"))

def _ensure(p): os.makedirs(p, exist_ok=True)
def _read_json(p):
    try:
        with open(p,"r",encoding="utf-8") as f: return json.load(f)
    except Exception: return {}
def _write(p,s):
    _ensure(os.path.dirname(p))
    with open(p,"w",encoding="utf-8") as f: f.write(s)
def _run(cmd,cwd):
    p=subprocess.Popen(cmd,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    out,_=p.communicate(); return p.returncode,out
def _latest_run(stage_dir):
    runs=os.path.join(stage_dir,"runs")
    if not os.path.isdir(runs): return None
    ds=[os.path.join(runs,d) for d in os.listdir(runs) if os.path.isdir(os.path.join(runs,d))]
    if not ds: return None
    ds.sort(key=lambda x: os.path.getmtime(x)); return ds[-1]
def _copy_metrics(latest, stage_dir):
    if not latest: return None
    src=os.path.join(latest,"final","metrics.json")
    dst=os.path.join(stage_dir,"metrics.json")
    if os.path.exists(src): shutil.copy2(src,dst); return dst
    return None

def _pick_gds(latest):
    if not latest: return (None, None)
    # best-effort: search for any *.gds under latest
    gds = glob.glob(os.path.join(latest, "**", "*.gds"), recursive=True)
    if not gds: return (None, None)
    # heuristics for klayout vs magic
    kl = None; mg = None
    for p in gds:
        lp = p.lower()
        if "klayout" in lp and kl is None: kl = p
        if "magic" in lp and mg is None: mg = p
    if kl is None: kl = gds[0]
    if mg is None and len(gds) > 1: mg = gds[1]
    return (kl, mg)

def run_agent(state: dict) -> dict:
    workflow_id=state.get("workflow_id","default")
    workflow_dir=state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    stage_dir=os.path.join(workflow_dir,"digital","tapeout")
    logs_dir=os.path.join(stage_dir,"logs")
    gds_dir=os.path.join(stage_dir,"gds")
    _ensure(stage_dir); _ensure(logs_dir); _ensure(gds_dir)

    impl_cfg=os.path.join(workflow_dir,"digital","foundry","openlane","config.json")
    synth_cfg=os.path.join(workflow_dir,"digital","synth","config.json")
    base_cfg=impl_cfg if os.path.exists(impl_cfg) else synth_cfg
    if not os.path.exists(base_cfg): raise RuntimeError("Missing config.json (foundry/openlane or synth).")

    cfg=_read_json(base_cfg)
    _write(os.path.join(stage_dir,"config.json"), json.dumps(cfg, indent=2))

    pdk=state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    image=state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host=os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "/root/chiploop-backend/backend/pdk"
    run_tag=f"tapeout_{workflow_id}"

    # Do KLayout.StreamOut then Magic.StreamOut then XOR (best-effort)
    run_sh=f"""#!/usr/bin/env bash
set -euo pipefail
export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "$(pwd)":/work \\
  -e PDK={pdk} \\
  -e PDK_ROOT=/pdk \\
  {image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --tag {run_tag} --to KLayout.StreamOut config.json'

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "$(pwd)":/work \\
  -e PDK={pdk} \\
  -e PDK_ROOT=/pdk \\
  {image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --tag {run_tag} --to Magic.StreamOut config.json || true'

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "$(pwd)":/work \\
  -e PDK={pdk} \\
  -e PDK_ROOT=/pdk \\
  {image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --tag {run_tag} --to KLayout.XOR config.json || true'
"""
    _write(os.path.join(stage_dir,"run.sh"), run_sh)
    os.chmod(os.path.join(stage_dir,"run.sh"), 0o755)

    rc,out=_run(["bash","-lc","./run.sh"], cwd=stage_dir)
    _write(os.path.join(logs_dir,"openlane_tapeout.log"), out)

    latest=_latest_run(stage_dir)
    metrics=_copy_metrics(latest, stage_dir)

    kl_src, mg_src = _pick_gds(latest)
    kl_dst = os.path.join(gds_dir, "klayout.gds") if kl_src else None
    mg_dst = os.path.join(gds_dir, "magic.gds") if mg_src else None
    if kl_src: shutil.copy2(kl_src, kl_dst)
    if mg_src: shutil.copy2(mg_src, mg_dst)

    summary={"workflow_id":workflow_id,"agent":AGENT_NAME,"status":"ok" if rc==0 else "failed","return_code":rc,
             "outputs":{"metrics_json":"digital/tapeout/metrics.json" if metrics else None,
                        "klayout_gds":"digital/tapeout/gds/klayout.gds" if kl_dst else None,
                        "magic_gds":"digital/tapeout/gds/magic.gds" if mg_dst else None,
                        "log":"digital/tapeout/logs/openlane_tapeout.log","openlane_run_dir":latest}}
    _write(os.path.join(stage_dir,"tapeout_summary.json"), json.dumps(summary, indent=2))
    _write(os.path.join(stage_dir,"tapeout_summary.md"), f"# Tapeout\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id,AGENT_NAME,"digital","tapeout/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id,AGENT_NAME,"digital","tapeout/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id,AGENT_NAME,"digital","tapeout/logs/openlane_tapeout.log", out)
        save_text_artifact_and_record(workflow_id,AGENT_NAME,"digital","tapeout/tapeout_summary.json", json.dumps(summary, indent=2))
        if metrics and os.path.exists(metrics):
            save_text_artifact_and_record(workflow_id,AGENT_NAME,"digital","tapeout/metrics.json", open(metrics,"r",encoding="utf-8").read())
        # GDS is binary; leave local per Option A (we only record paths in summary)
    except Exception as e:
        print(f"⚠️ upload failed: {e}")

    state.setdefault("digital",{})["tapeout"]={"status":summary["status"],"stage_dir":stage_dir,"metrics_json":metrics,"gds_klayout":kl_dst,"gds_magic":mg_dst}
    return state