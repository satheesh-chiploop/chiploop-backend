import os
import json
import glob
import shutil
import logging

logger = logging.getLogger("chiploop")

from datetime import datetime

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Implementation Setup Agent"

def _read_json_safe(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _resolve_spec_json(state: dict, workflow_dir: str) -> str | None:
    cand = (
        (state.get("digital") or {}).get("spec_json")
        or state.get("digital_spec_json")
        or state.get("spec_json")
    )
    if cand and os.path.exists(cand):
        return cand

    spec_dir = os.path.join(workflow_dir, "spec")
    files = sorted(glob.glob(os.path.join(spec_dir, "*_spec.json")))
    return files[0] if files else None

def _resolve_top_module(spec: dict, state: dict) -> str:
    return (
        (((spec.get("hierarchy") or {}).get("top_module") or {}).get("name"))
        or spec.get("design_name")
        or spec.get("top_module")
        or spec.get("name")
        or (state.get("digital") or {}).get("top_module")
        or state.get("top_module")
        or "top"
    )

def _resolve_rtl_files(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") or {}
    cands = digital.get("rtl_files")
    if isinstance(cands, list):
        xs = [p for p in cands if p and os.path.exists(p)]
        if xs:
            return xs

    xs = sorted(glob.glob(os.path.join(workflow_dir, "digital", "rtl_refactored", "*.v")))
    if xs:
        return xs

    xs = sorted(glob.glob(os.path.join(workflow_dir, "digital", "rtl", "*.v")))
    return xs

def _resolve_upstream_sdc(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    candidates = [
        digital.get("constraints_sdc"),
        state.get("constraints_sdc"),
        os.path.join(workflow_dir, "digital", "constraints", "top.sdc"),
    ]

    for cand in candidates:
        logger.info(f"{AGENT_NAME}: checking sdc candidate={cand}")
        if cand and os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected sdc candidate={cand}")
            return cand

    extra = sorted(glob.glob(os.path.join(workflow_dir, "digital", "*.sdc")))
    for cand in extra:
        logger.info(f"{AGENT_NAME}: checking fallback top-level sdc={cand}")
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected fallback top-level sdc={cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None


def _build_fallback_sdc(clk_name: str, clk_mhz: float, reset_name: str) -> str:
    period_ns = 1000.0 / float(clk_mhz)
    return f"""# Auto-generated fallback by {AGENT_NAME}
create_clock -name {clk_name} -period {period_ns:.3f} [get_ports {clk_name}]
# set_false_path -from [get_ports {reset_name}]
"""

def _read_json(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    workflow_dir = os.path.abspath(workflow_dir)
    os.makedirs(workflow_dir, exist_ok=True)

    digital_root = os.path.join(workflow_dir, "digital", "foundry")
    os.makedirs(digital_root, exist_ok=True)



    # Paths
    
    logger.info(f"🏁 Running {AGENT_NAME}")
    digital_root = os.path.join(workflow_dir, "digital")
    setup_root = os.path.join(digital_root, "impl_setup")
    constraints_dir = os.path.join(setup_root, "constraints")
    openlane_dir = os.path.join(setup_root, "openlane")
    os.makedirs(constraints_dir, exist_ok=True)
    os.makedirs(openlane_dir, exist_ok=True)

    profile_path = (state.get("digital") or {}).get("foundry_profile") or os.path.join(digital_root, "foundry", "foundry_profile.json")
    if not os.path.exists(profile_path):
        state["status"] = "❌ Missing foundry_profile.json. Run 'Digital Foundry Profile Agent' first."
        return state

    profile = _read_json(profile_path)
    spec_json_path = _resolve_spec_json(state, workflow_dir)
    spec = _read_json_safe(spec_json_path) if spec_json_path else {}

    top_module = _resolve_top_module(spec, state)
    clk_name = (profile.get("timing") or {}).get("clock_name", "clk")
    reset_name = (profile.get("timing") or {}).get("reset_name", "reset_n")
    clk_mhz = float((profile.get("timing") or {}).get("target_clock_mhz", 50))

    rtl_files = _resolve_rtl_files(state, workflow_dir)
    upstream_sdc = _resolve_upstream_sdc(state, workflow_dir)

    logger.info(f"{AGENT_NAME}: spec_json={spec_json_path}")
    logger.info(f"{AGENT_NAME}: top_module={top_module}")
    logger.info(f"{AGENT_NAME}: rtl_files={len(rtl_files)}")
    logger.info(f"{AGENT_NAME}: upstream_sdc={upstream_sdc}")

    corners_dir = setup_root
    setup_dir = setup_root

    # --- 1) Canonical filelist ---
    filelist_path = os.path.join(setup_root, "filelist.f")
    filelist_text = "\n".join(rtl_files) + ("\n" if rtl_files else "")
    with open(filelist_path, "w", encoding="utf-8") as f:
        f.write(filelist_text)

    # --- 2) Canonical SDC ---
    sdc_basename = os.path.basename(upstream_sdc) if upstream_sdc else f"{top_module}.sdc"
    canonical_sdc_path = os.path.join(constraints_dir, sdc_basename)
    if upstream_sdc and os.path.exists(upstream_sdc):
        shutil.copy2(upstream_sdc, canonical_sdc_path)
        sdc_source = upstream_sdc
        with open(canonical_sdc_path, "r", encoding="utf-8") as f:
            sdc_text = f.read()
        logger.info(f"{AGENT_NAME}: using upstream SDC -> {canonical_sdc_path}")
    else:
        sdc_text = _build_fallback_sdc(clk_name, clk_mhz, reset_name)
        with open(canonical_sdc_path, "w", encoding="utf-8") as f:
            f.write(sdc_text)
        sdc_source = "fallback_generated"
        logger.warning(f"{AGENT_NAME}: upstream SDC missing, generated fallback {canonical_sdc_path}")

    logger.info(f"{AGENT_NAME}: sdc_basename={sdc_basename}")
    logger.info(f"{AGENT_NAME}: canonical_sdc_exists={os.path.exists(canonical_sdc_path)}")
    logger.info(f"{AGENT_NAME}: canonical_sdc_size={os.path.getsize(canonical_sdc_path) if os.path.exists(canonical_sdc_path) else -1}")

    # --- 3) Corners canonical JSON ---
    corners = profile.get("corners") or []
    corners_json = {
        "pdk": profile.get("pdk"),
        "pdk_root": profile.get("pdk_root"),
        "corners": corners,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "note": "Corner names are intent labels; OpenLane2 config will map them later.",
    }

    corners_path = os.path.join(corners_dir, "corners.json")
    with open(corners_path, "w", encoding="utf-8") as f:
        json.dump(corners_json, f, indent=2)

    # --- 4) OpenLane config ---
    period_ns = 1000.0 / float(clk_mhz)
    openlane_cfg = {
        "DESIGN_NAME": top_module,
        "PDK": profile.get("pdk") or "sky130A",
        "CLOCK_PORT": clk_name,
        "CLOCK_PERIOD": period_ns,
        "SYNTH_SDC_FILE": f"constraints/{sdc_basename}",
        "PNR_SDC_FILE": f"constraints/{sdc_basename}",
        "CHIPLOOP_SOURCE_SPEC_JSON": spec_json_path,
        "CHIPLOOP_FILELIST": filelist_path,
        "CHIPLOOP_UPSTREAM_SDC_SOURCE": sdc_source,
    }

    cfg_path = os.path.join(openlane_dir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(openlane_cfg, f, indent=2)

    # --- 5) Setup log ---
    log_lines = [
        f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={os.path.abspath(workflow_dir)}",
        f"profile_path={profile_path}",
        f"spec_json={spec_json_path}",
        f"top_module={top_module}",
        f"rtl_count={len(rtl_files)}",
        f"sdc_source={sdc_source}",
        f"canonical_sdc={canonical_sdc_path}",
        f"filelist={filelist_path}",
        f"pdk={openlane_cfg['PDK']}",
        f"pdk_root={profile.get('pdk_root')}",
        f"openlane_cfg={cfg_path}",
        f"corners={corners_path}",
    ]
    setup_log = "\n".join(log_lines) + "\n"
    setup_log_path = os.path.join(setup_dir, "implementation_setup.log")
    with open(setup_log_path, "w", encoding="utf-8") as f:
        f.write(setup_log)

    logger.info(f"{AGENT_NAME}: canonical_sdc={canonical_sdc_path}")
    logger.info(f"{AGENT_NAME}: filelist={filelist_path}")
    logger.info(f"{AGENT_NAME}: cfg_path={cfg_path}")
    logger.info(f"{AGENT_NAME}: setup_log_path={setup_log_path}")

    # --- 6) Upload artifacts ---
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/impl_setup", "filelist.f", filelist_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/impl_setup/constraints", sdc_basename, sdc_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/impl_setup/openlane", "config.json", json.dumps(openlane_cfg, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/impl_setup", "corners.json", json.dumps(corners_json, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/impl_setup", "implementation_setup.log", setup_log)


    # --- 7) State handoff ---
    logger.info(f"{AGENT_NAME}: writing state.digital.constraints_sdc={canonical_sdc_path}")
    logger.info(f"{AGENT_NAME}: writing state.digital.impl_filelist={filelist_path}")
    logger.info(f"{AGENT_NAME}: writing state.digital.openlane_config={cfg_path}")
    digital = state.setdefault("digital", {})
    digital["spec_json"] = spec_json_path or digital.get("spec_json")
    digital["top_module"] = top_module
    digital["rtl_files"] = rtl_files
    digital["constraints_sdc"] = canonical_sdc_path
    digital["impl_filelist"] = filelist_path
    digital["openlane_config"] = cfg_path
    digital["implementation_setup_log"] = setup_log_path

    state["status"] = "✅ Digital implementation setup generated."
    return state
