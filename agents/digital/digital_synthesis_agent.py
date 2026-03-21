
import os
import json
import glob
import shutil
import subprocess
import logging
from datetime import datetime

logger = logging.getLogger("chiploop")


from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Synthesis Agent"

DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))

def _resolve_spec_json(state: dict, workflow_dir: str) -> str | None:
    for cand in [
        (state.get("digital") or {}).get("spec_json"),
        state.get("digital_spec_json"),
        state.get("spec_json"),
    ]:
        if cand and os.path.exists(cand):
            return cand

    files = sorted(glob.glob(os.path.join(workflow_dir, "spec", "*_spec.json")))
    return files[0] if files else None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    # 1) State is the primary source of truth
    cand = digital.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected constraints_sdc from state.digital -> {cand}")
        return cand

    # 2) Fallback to any impl_setup constraint file
    impl_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "impl_setup", "constraints", "*.sdc")))
    for cand in impl_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected constraints_sdc from impl_setup glob -> {cand}")
            return cand

    # 3) Legacy fallback
    legacy_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))
    for cand in legacy_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected constraints_sdc from legacy digital/constraints -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found in state, impl_setup, or legacy constraints")
    return None




def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _pick_clock(spec: dict) -> tuple[str, float]:
    """
    Returns (clock_name, clock_period_ns)
    Best-effort parsing, never fake precision.
    """
    # Try common shapes
    clk_name = "clk"
    clk_period = 10.0  # 100MHz default

    # examples you might have later:
    # spec["clock"]["name"], spec["clock"]["period_ns"], spec["clock"]["freq_mhz"]
    c = spec.get("clock") or {}
    if isinstance(c, dict):
        if c.get("name"):
            clk_name = str(c["name"])
        if c.get("period_ns"):
            try:
                clk_period = float(c["period_ns"])
            except Exception:
                pass
        elif c.get("freq_mhz"):
            try:
                mhz = float(c["freq_mhz"])
                if mhz > 0:
                    clk_period = 1000.0 / mhz
            except Exception:
                pass

    return clk_name, clk_period

def _write_local(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout

def _resolve_rtl_files_from_state(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") or {}

    if isinstance(digital.get("rtl_files"), list):
        xs = [p for p in digital["rtl_files"] if p and os.path.exists(p)]
        if xs:
            return xs

    fl = digital.get("impl_filelist")
    if fl and os.path.exists(fl):
        xs = []
        with open(fl, "r", encoding="utf-8") as f:
            for line in f:
                p = line.strip()
                if p and os.path.exists(p):
                    xs.append(p)
        if xs:
            return xs

    xs = sorted(glob.glob(os.path.join(workflow_dir, "digital", "rtl_refactored", "*.v")))
    if xs:
        return xs

    return []

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    _ensure_dir(workflow_dir)

    logger.info(f"🏁 Running {AGENT_NAME}.")
    rtl_files = _resolve_rtl_files_from_state(state, workflow_dir)

    if not rtl_files:
        artifact_list = state.get("artifact_list") or []
        if isinstance(artifact_list, list) and artifact_list:
            rtl_files = [p for p in artifact_list if p and os.path.exists(p)]

    if not rtl_files:
        single = state.get("artifact")
        if single and os.path.exists(single):
            rtl_files = [single]

    if not rtl_files:
        raise FileNotFoundError(f"No RTL found for synthesis in {workflow_dir}")

    logger.info(f"{AGENT_NAME}: rtl_count={len(rtl_files)}")


    # ---------- Spec JSON (optional) ----------

    spec = {}
    spec_json = _resolve_spec_json(state, workflow_dir)
    if spec_json and os.path.exists(spec_json):
        spec = _read_json(spec_json)

    logger.info(f"{AGENT_NAME}: spec_json={spec_json}")

    clk_name, clk_period_ns = _pick_clock(spec)

    # ---------- Stage folder ----------
    stage_dir = os.path.join(workflow_dir, "digital", "synth")
    rtl_dir = os.path.join(stage_dir, "rtl")
    constraints_dir = os.path.join(stage_dir, "constraints")
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(rtl_dir)
    _ensure_dir(constraints_dir)
    _ensure_dir(logs_dir)

    # Copy RTL into deterministic local folder (avoid container path issues)
    copied = []
    for f in rtl_files:
        dst = os.path.join(rtl_dir, os.path.basename(f))
        if os.path.abspath(f) != os.path.abspath(dst):
            shutil.copy2(f, dst)
        copied.append(dst)

    # Pick top module name best-effort:
    # - If your spec later contains block.name or top_module, use it.

    top_module = (
        (state.get("digital") or {}).get("top_module")
        or (spec.get("design_name") if isinstance(spec, dict) else None)
        or (((spec.get("hierarchy") or {}).get("top_module") or {}).get("name") if isinstance(spec, dict) else None)
        or (spec.get("top_module") if isinstance(spec, dict) else None)
        or (spec.get("name") if isinstance(spec, dict) else None)
        or ((spec.get("block") or {}).get("name") if isinstance(spec.get("block"), dict) else None)
        or state.get("top_module")
        or "top"
    )
    top_module = str(top_module).strip()
    logger.info(f"{AGENT_NAME}: top_module={top_module}")


    state["design_name"] = top_module
    # ---------- SDC (single source of truth) ----------

    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        impl_glob = sorted(glob.glob(os.path.join(workflow_dir, "digital", "impl_setup", "constraints", "*.sdc")))
        legacy_glob = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))

        msg = (
            "Missing upstream SDC: checked "
            f"state.digital.constraints_sdc={(state.get('digital') or {}).get('constraints_sdc')}, "
            f"impl_setup_candidates={impl_glob}, "
            f"legacy_candidates={legacy_glob}"
        )
        exec_log_path = os.path.join(logs_dir, "openlane_synth.log")
        _write_local(exec_log_path, msg + "\n")
        _write_local(os.path.join(stage_dir, "synth_input_resolution.log"), msg + "\n")

        summary = {"status": "failed", "return_code": 2, "error": msg}
        _write_local(os.path.join(stage_dir, "synth_summary.json"), json.dumps(summary, indent=2))
        _write_local(os.path.join(stage_dir, "synth_summary.md"), f"# Digital Synthesis Summary\n\n- **Status**: failed\n- **Reason**: {msg}\n")
        _write_local(os.path.join(stage_dir, "metrics.json"), json.dumps({"status": "failed", "reason": msg}, indent=2))

        logger.error(f"{AGENT_NAME}: {msg}")
        state["status"] = f"{AGENT_NAME}: failed (missing SDC)"
        return state

    logger.info(f"{AGENT_NAME}: using upstream_sdc={upstream_sdc}")

    sdc_basename = os.path.basename(upstream_sdc)
    sdc_path = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, sdc_path)

    logger.info(f"{AGENT_NAME}: copied SDC into synth stage -> {sdc_path}")
    logger.info(f"{AGENT_NAME}: synth sdc exists={os.path.exists(sdc_path)}")
    logger.info(f"{AGENT_NAME}: synth sdc size={os.path.getsize(sdc_path) if os.path.exists(sdc_path) else -1}")
 
    with open(sdc_path, "r", encoding="utf-8") as f:
       sdc_text = f.read()

    input_log = "\n".join([
        f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={os.path.abspath(workflow_dir)}",
        f"spec_json={spec_json}",
        f"top_module={top_module}",
        f"rtl_count={len(rtl_files)}",
        f"upstream_sdc={upstream_sdc}",
        f"sdc_basename={sdc_basename}",
        f"synth_sdc_path={sdc_path}",
        f"state_constraints_sdc={(state.get('digital') or {}).get('constraints_sdc')}",
        f"pdk_variant={state.get('pdk_variant') or DEFAULT_PDK_VARIANT}",
    ]) + "\n"

    input_log_path = os.path.join(stage_dir, "synth_input_resolution.log")
    _write_local(input_log_path, input_log)
  

    # ---------- OpenLane2 config.json ----------
    # Keep it minimal and explicit.
    # PDK selection is via CLI/env; do NOT hardcode absolute host paths here.
    config_path = os.path.join(stage_dir, "config.json")

    # OpenLane2 supports JSON configs; we keep sources relative inside the mounted /work
    verilog_sources = [f"rtl/{os.path.basename(p)}" for p in copied]

    config = {
        "DESIGN_NAME": top_module,
        "VERILOG_FILES": verilog_sources,
        "CLOCK_PORT": clk_name,
        "CLOCK_PERIOD": clk_period_ns,
        "SYNTH_STRATEGY": "AREA 0",
        "SYNTH_SDC_FILE": f"constraints/{sdc_basename}",
        "PNR_SDC_FILE": f"constraints/{sdc_basename}",

        # ChipLoop provenance (OpenLane ignores unknown top-level keys)
        "CHIPLOOP_WORKFLOW_ID": workflow_id,
        "CHIPLOOP_GENERATED_BY": AGENT_NAME,
        "CHIPLOOP_GENERATED_AT": datetime.utcnow().isoformat() + "Z",
    }

    
    _write_local(config_path, json.dumps(config, indent=2))

    # ---------- Docker run.sh (rerunnable contract) ----------
    # Host PDK root: your real path is backend/pdk (you already created it)
    # We keep it configurable.
    default_pdk_host = os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "/root/chiploop-backend/backend/pdk"
    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE

    # Runs folder inside stage_dir
    # OpenLane2 will create a "runs/<tag>" directory under CWD by default.
    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag


    run_sh_path = os.path.join(stage_dir, "run.sh")
    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "PDK_VARIANT={pdk_variant}"
echo "OPENLANE_IMAGE={openlane_image}"
echo "WORKDIR=/work"
echo

docker run --rm \\
  -v "{default_pdk_host}:/pdk" \\
  -v "{os.path.abspath(stage_dir)}:/work" \\
  -e PDK_ROOT=/pdk \\
  -e PDK={pdk_variant} \\
  -e OPENLANE_NUM_CORES={DEFAULT_NUM_CORES} \\
  "{openlane_image}" \\
  bash -lc 'set -e; echo "PDK listing:"; ls -la /pdk | head -n 50; \
  test -f /pdk/sky130A/libs.tech/openlane/config.tcl; \
  cd /work && openlane --pdk {pdk_variant} --run-tag {run_tag} --flow Classic --to Yosys.Synthesis config.json'


echo
echo "Done. Inspect /work/runs/{run_tag} or latest run folder under /work/runs/"
"""
    _write_local(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    # ---------- Execute synthesis (best-effort) ----------
    # This is the first production step; we run it now and capture logs.
    exec_log_path = os.path.join(logs_dir, "openlane_synth.log")

    rc, out = _run(["bash", "./run.sh"], cwd=stage_dir)
    _write_local(exec_log_path, out)

    # ---------- Collect best-effort outputs ----------
    # We don’t fake timing. We only parse what exists.
    runs_dir = os.path.join(stage_dir, "runs")
    metrics_json = None
    stable_metrics_path = os.path.join(stage_dir, "metrics.json")
    netlist_candidates = []
    stable_netlist_path = None


    if os.path.isdir(runs_dir):
        # find newest run folder
        run_folders = [os.path.join(runs_dir, d) for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d))]
        run_folders.sort(key=lambda p: os.path.getmtime(p))
        latest = run_folders[-1] if run_folders else None

        if latest:
            mj = os.path.join(latest, "final", "metrics.json")
            if os.path.exists(mj):
                metrics_json = mj

            # Always export stable metrics path if available
            stable_metrics_path = os.path.join(stage_dir, "metrics.json")
            if latest:
                mj = os.path.join(latest, "final", "metrics.json")
                if os.path.exists(mj):
                    shutil.copy2(mj, stable_metrics_path)

            # synthesis step folders often contain gate-level netlists


            # synthesis step folders often contain gate-level netlists
            netlist_candidates = glob.glob(os.path.join(latest, "*yosys-synthesis", "*.v")) + \
                                 glob.glob(os.path.join(latest, "*yosys-synthesis", "outputs", "*.v"))

            # ---------- Persist primary synthesized netlist (stable path) ----------
            stable_netlist_path = None
            if netlist_candidates:
                try:
                    # pick the first candidate deterministically
                    primary = netlist_candidates[0]
                    netlist_dir = os.path.join(stage_dir, "netlist")
                    _ensure_dir(netlist_dir)
                    stable_netlist_path = os.path.join(netlist_dir, f"{top_module}_synth.v")
                    shutil.copy2(primary, stable_netlist_path)
                except Exception as e:
                    print(f"⚠️ Failed to persist netlist: {e}")
                    stable_netlist_path = None

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "inputs": {
            "rtl_files": [os.path.basename(x) for x in copied],
            "top_module": top_module,
            "clock_port": clk_name,
            "clock_period_ns": clk_period_ns,
            "pdk_variant": pdk_variant,
            "openlane_image": openlane_image,
            "pdk_root_host": default_pdk_host,
        },
        "outputs": {
            "stage_dir": stage_dir,
            "config_json": config_path,
            "sdc": sdc_path,
            "run_sh": run_sh_path,
            "exec_log": exec_log_path,
            "metrics_json": stable_metrics_path if os.path.exists(stable_metrics_path) else None,
            "netlist": stable_netlist_path,
            "netlist_candidates": netlist_candidates[:10],
        }
    }

    summary_json_path = os.path.join(stage_dir, "synth_summary.json")
    summary_md_path = os.path.join(stage_dir, "synth_summary.md")
    _write_local(summary_json_path, json.dumps(summary, indent=2))

    md = f"""# Digital Synthesis Summary

- **Workflow**: {workflow_id}
- **Status**: `{summary["status"]}` (rc={rc})
- **Top module**: `{top_module}`
- **Clock**: `{clk_name}` @ **{clk_period_ns:.3f} ns**
- **PDK**: `{pdk_variant}`
- **Image**: `{openlane_image}`

## Deterministic outputs (rerunnable)
- `digital/synth/config.json`
- `digital/synth/constraints/top.sdc`
- `digital/synth/run.sh`
- `digital/synth/logs/openlane_synth.log`

## Parsed outputs (best-effort)
- metrics.json: `{metrics_json or "not found"}`
- netlist candidates: {len(netlist_candidates)} found
"""
    _write_local(summary_md_path, md)

    # ---------- Upload key artifacts to Supabase Storage ----------
    # (Option A: store heavy locally, upload key collaterals + summaries)
    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/config.json", json.dumps(config, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"synth/constraints/{sdc_basename}",sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/logs/openlane_synth.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_summary.md", md)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_input_resolution.log", input_log)
        # Upload synthesized netlist (gate-level) if present
        if stable_netlist_path and os.path.exists(stable_netlist_path):
            with open(stable_netlist_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id,
                    AGENT_NAME,
                    "digital",
                    f"synth/netlist/{top_module}_synth.v",
                    f.read()
                )

        # Upload metrics.json if present
        if os.path.exists(stable_metrics_path):
            with open(stable_metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id,
                    AGENT_NAME,
                    "digital",
                    "synth/metrics.json",
                    f.read()
                )
        print("✅ Uploaded synthesis artifacts to Supabase.")
    except Exception as e:
        print(f"⚠️ Synthesis artifact upload failed: {e}")

    # ---------- Update state for downstream workflow ----------

    digital = state.setdefault("digital", {})
    digital["synth"] = {
        "stage_dir": stage_dir,
        "summary_json": summary_json_path,
        "summary_md": summary_md_path,
        "metrics_json": metrics_json,
        "netlist": stable_netlist_path,
        "netlist_candidates": netlist_candidates[:10],
        "status": summary["status"],
        "input_resolution_log": input_log_path,
        "constraints_sdc": sdc_path,
        "upstream_constraints_sdc": upstream_sdc,
        "rtl_files": rtl_files,
        "top_module": top_module,
    }
    
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state