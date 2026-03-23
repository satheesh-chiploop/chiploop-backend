import os
import json
import glob
import shutil
import logging
from datetime import datetime

logger = logging.getLogger("chiploop")

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Implementation Setup Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read_json_safe(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _resolve_spec_json(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    candidates = [
        digital.get("spec_json"),
        state.get("digital_spec_json"),
        state.get("spec_json"),
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected spec_json from state -> {cand}")
            return cand

    spec_dir = os.path.join(workflow_dir, "spec")
    files = sorted(glob.glob(os.path.join(spec_dir, "*_spec.json")))
    for cand in files:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected spec_json from spec dir -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no spec_json found")
    return None


def _resolve_top_module(spec: dict, state: dict) -> str:
    digital = state.get("digital") or {}
    return (
        (((spec.get("hierarchy") or {}).get("top_module") or {}).get("name"))
        or spec.get("design_name")
        or spec.get("top_module")
        or spec.get("name")
        or digital.get("top_module")
        or state.get("top_module")
        or "top"
    )


def _resolve_rtl_files(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") or {}

    cands = digital.get("rtl_files")
    if isinstance(cands, list):
        xs = [p for p in cands if p and os.path.exists(p)]
        if xs:
            logger.info(f"{AGENT_NAME}: selected rtl_files from state.digital -> {len(xs)} files")
            return xs

    xs = sorted(glob.glob(os.path.join(workflow_dir, "digital", "rtl_refactored", "*.v")))
    if xs:
        logger.info(f"{AGENT_NAME}: selected rtl_files from rtl_refactored -> {len(xs)} files")
        return xs

    xs = sorted(glob.glob(os.path.join(workflow_dir, "digital", "rtl", "*.v")))
    if xs:
        logger.info(f"{AGENT_NAME}: selected rtl_files from rtl -> {len(xs)} files")
        return xs

    logger.warning(f"{AGENT_NAME}: no rtl files found")
    return []


def _resolve_profile_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    cand = digital.get("foundry_profile")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected profile from state.digital -> {cand}")
        return cand

    cand = os.path.join(workflow_dir, "digital", "foundry", "foundry_profile.json")
    if os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected profile from foundry stage -> {cand}")
        return cand

    logger.warning(f"{AGENT_NAME}: no foundry profile found")
    return None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> tuple[str | None, str]:
    digital = state.get("digital") or {}

    cand = digital.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital -> {cand}")
        return cand, "state.digital.constraints_sdc"

    cand = state.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state -> {cand}")
        return cand, "state.constraints_sdc"

    for cand in sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc"))):
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected legacy workflow SDC -> {cand}")
            return cand, "digital/constraints"

    for cand in sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "constraints", "*.sdc"))):
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected synth SDC -> {cand}")
            return cand, "digital/synth/constraints"

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None, "none"


def _build_fallback_sdc(clk_name: str, clk_mhz: float, reset_name: str) -> str:
    mhz = float(clk_mhz) if clk_mhz else 50.0
    period_ns = 1000.0 / mhz
    return f"""# Auto-generated fallback by {AGENT_NAME}
create_clock -name {clk_name} -period {period_ns:.3f} [get_ports {clk_name}]
# set_false_path -from [get_ports {reset_name}]
"""


def _resolve_clock_reset(profile: dict, spec: dict) -> tuple[str, str, float]:
    timing = profile.get("timing") or {}
    spec_clock = spec.get("clock") or {}
    clk_name = timing.get("clock_name") or spec_clock.get("name") or "clk"
    reset_name = timing.get("reset_name") or spec.get("reset") or "reset_n"
    clk_mhz = timing.get("target_clock_mhz") or spec_clock.get("target_mhz") or 50
    try:
        clk_mhz = float(clk_mhz)
    except Exception:
        clk_mhz = 50.0
    return clk_name, reset_name, clk_mhz


def run_agent(state: dict) -> dict:
    try:
        print(f"\n🏁 Running {AGENT_NAME}...")
        logger.info(f"🏁 Running {AGENT_NAME}")

        workflow_id = state.get("workflow_id", "default")
        workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
        _ensure_dir(workflow_dir)

        stage_dir = os.path.join(workflow_dir, "digital", "impl_setup")
        logs_dir = os.path.join(stage_dir, "logs")
        constraints_dir = os.path.join(stage_dir, "constraints")
        openlane_dir = os.path.join(stage_dir, "openlane")

        _ensure_dir(stage_dir)
        _ensure_dir(logs_dir)
        _ensure_dir(constraints_dir)
        _ensure_dir(openlane_dir)

        logger.info(f"{AGENT_NAME}: workflow_id={workflow_id}")
        logger.info(f"{AGENT_NAME}: workflow_dir={workflow_dir}")
        logger.info(f"{AGENT_NAME}: stage_dir={stage_dir}")
        logger.info(f"{AGENT_NAME}: constraints_dir={constraints_dir}")
        logger.info(f"{AGENT_NAME}: openlane_dir={openlane_dir}")

        profile_path = _resolve_profile_from_state(state, workflow_dir)
        if not profile_path:
            raise RuntimeError("Missing foundry_profile.json. Run Digital Foundry Profile Agent first.")

        profile = _read_json_safe(profile_path)
        spec_json_path = _resolve_spec_json(state, workflow_dir)
        spec = _read_json_safe(spec_json_path)

        top_module = _resolve_top_module(spec, state)
        clk_name, reset_name, clk_mhz = _resolve_clock_reset(profile, spec)
        period_ns = 1000.0 / float(clk_mhz)

        rtl_files = _resolve_rtl_files(state, workflow_dir)
        if not rtl_files:
            raise RuntimeError("No RTL files found for implementation setup.")

        upstream_sdc, sdc_source = _resolve_sdc_from_state(state, workflow_dir)

        logger.info(f"{AGENT_NAME}: profile_path={profile_path}")
        logger.info(f"{AGENT_NAME}: spec_json_path={spec_json_path}")
        logger.info(f"{AGENT_NAME}: top_module={top_module}")
        logger.info(f"{AGENT_NAME}: clk_name={clk_name}")
        logger.info(f"{AGENT_NAME}: reset_name={reset_name}")
        logger.info(f"{AGENT_NAME}: clk_mhz={clk_mhz}")
        logger.info(f"{AGENT_NAME}: rtl_file_count={len(rtl_files)}")
        logger.info(f"{AGENT_NAME}: upstream_sdc={upstream_sdc}")
        logger.info(f"{AGENT_NAME}: sdc_source={sdc_source}")

        filelist_path = os.path.join(stage_dir, "filelist.f")
        filelist_text = "\n".join(rtl_files) + "\n"
        _write_text(filelist_path, filelist_text)

        sdc_basename = os.path.basename(upstream_sdc) if upstream_sdc else f"{top_module}.sdc"
        canonical_sdc_path = os.path.join(constraints_dir, sdc_basename)

        if upstream_sdc and os.path.exists(upstream_sdc):
            shutil.copy2(upstream_sdc, canonical_sdc_path)
            with open(canonical_sdc_path, "r", encoding="utf-8") as f:
                sdc_text = f.read()
            logger.info(f"{AGENT_NAME}: using upstream SDC -> {canonical_sdc_path}")
        else:
            sdc_text = _build_fallback_sdc(clk_name, clk_mhz, reset_name)
            _write_text(canonical_sdc_path, sdc_text)
            sdc_source = "fallback_generated"
            logger.warning(f"{AGENT_NAME}: upstream SDC missing, generated fallback -> {canonical_sdc_path}")

        logger.info(f"{AGENT_NAME}: sdc_basename={sdc_basename}")
        logger.info(
            f"{AGENT_NAME}: canonical_sdc_exists={os.path.exists(canonical_sdc_path)} "
            f"size={os.path.getsize(canonical_sdc_path) if os.path.exists(canonical_sdc_path) else -1}"
        )

        corners = profile.get("corners") or []
        corners_json = {
            "pdk": profile.get("pdk") or "sky130A",
            "pdk_root": profile.get("pdk_root"),
            "corners": corners,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "note": "Corner names are intent labels; downstream OpenLane config will map them as needed.",
        }
        corners_path = os.path.join(stage_dir, "corners.json")
        _write_text(corners_path, json.dumps(corners_json, indent=2))

        openlane_cfg = {
            "DESIGN_NAME": top_module,
            "PDK": profile.get("pdk") or "sky130A",
            "CLOCK_PORT": clk_name,
            "CLOCK_PERIOD": period_ns,
            "VERILOG_FILES": [f"dir::../filelist.f"],
            "SYNTH_SDC_FILE": f"dir::../constraints/{sdc_basename}",
            "PNR_SDC_FILE": f"dir::../constraints/{sdc_basename}",
            "CHIPLOOP_SOURCE_SPEC_JSON": spec_json_path,
            "CHIPLOOP_FILELIST": filelist_path,
            "CHIPLOOP_UPSTREAM_SDC_SOURCE": sdc_source,
            "FP_CORE_UTIL": 20,
            "PL_TARGET_DENSITY": 0.25,
            "DIE_AREA": "0 0 120 120",
        }

        cfg_path = os.path.join(openlane_dir, "config.json")
        _write_text(cfg_path, json.dumps(openlane_cfg, indent=2))

        input_log = "\n".join([
            f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
            f"workflow_id={workflow_id}",
            f"workflow_dir={workflow_dir}",
            f"profile_path={profile_path}",
            f"spec_json={spec_json_path}",
            f"top_module={top_module}",
            f"rtl_count={len(rtl_files)}",
            f"clock_port={clk_name}",
            f"reset_name={reset_name}",
            f"clock_mhz={clk_mhz}",
            f"clock_period_ns={period_ns}",
            f"upstream_sdc={upstream_sdc}",
            f"sdc_source={sdc_source}",
            f"sdc_basename={sdc_basename}",
            f"canonical_sdc={canonical_sdc_path}",
            f"filelist={filelist_path}",
            f"pdk={openlane_cfg['PDK']}",
            f"pdk_root={profile.get('pdk_root')}",
            f"openlane_cfg={cfg_path}",
            f"corners={corners_path}",
        ]) + "\n"

        input_log_path = os.path.join(logs_dir, "implementation_setup_input_resolution.log")
        _write_text(input_log_path, input_log)

        setup_log = "\n".join([
            f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
            "status=ok",
            f"profile_path={profile_path}",
            f"spec_json={spec_json_path}",
            f"top_module={top_module}",
            f"rtl_files={len(rtl_files)}",
            f"sdc_source={sdc_source}",
            f"canonical_sdc={canonical_sdc_path}",
            f"filelist={filelist_path}",
            f"corners_json={corners_path}",
            f"openlane_config={cfg_path}",
            f"input_resolution_log={input_log_path}",
        ]) + "\n"

        setup_log_path = os.path.join(stage_dir, "implementation_setup.log")
        _write_text(setup_log_path, setup_log)

        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "ok",
            "outputs": {
                "filelist": "digital/impl_setup/filelist.f",
                "sdc": f"digital/impl_setup/constraints/{sdc_basename}",
                "corners_json": "digital/impl_setup/corners.json",
                "openlane_config": "digital/impl_setup/openlane/config.json",
                "input_resolution_log": "digital/impl_setup/logs/implementation_setup_input_resolution.log",
                "setup_log": "digital/impl_setup/implementation_setup.log",
            },
        }
        summary_path = os.path.join(stage_dir, "implementation_setup_summary.json")
        _write_text(summary_path, json.dumps(summary, indent=2))

        logger.info(
            f"{AGENT_NAME}: wrote filelist exists={os.path.exists(filelist_path)} "
            f"path={filelist_path}"
        )
        logger.info(
            f"{AGENT_NAME}: wrote sdc exists={os.path.exists(canonical_sdc_path)} "
            f"path={canonical_sdc_path}"
        )
        logger.info(
            f"{AGENT_NAME}: wrote corners exists={os.path.exists(corners_path)} "
            f"path={corners_path}"
        )
        logger.info(
            f"{AGENT_NAME}: wrote config exists={os.path.exists(cfg_path)} "
            f"path={cfg_path}"
        )
        logger.info(
            f"{AGENT_NAME}: wrote input_log exists={os.path.exists(input_log_path)} "
            f"path={input_log_path}"
        )
        logger.info(
            f"{AGENT_NAME}: wrote setup_log exists={os.path.exists(setup_log_path)} "
            f"path={setup_log_path}"
        )

        logger.info(f"{AGENT_NAME}: uploading artifacts...")
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "impl_setup/filelist.f", filelist_text)
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            f"impl_setup/constraints/{sdc_basename}",
            sdc_text,
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "impl_setup/openlane/config.json",
            json.dumps(openlane_cfg, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "impl_setup/corners.json",
            json.dumps(corners_json, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "impl_setup/logs/implementation_setup_input_resolution.log",
            input_log,
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "impl_setup/implementation_setup.log",
            setup_log,
        )
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "impl_setup/implementation_setup_summary.json",
            json.dumps(summary, indent=2),
        )
        logger.info(f"{AGENT_NAME}: artifact upload complete")

        digital = state.setdefault("digital", {})
        digital["spec_json"] = spec_json_path or digital.get("spec_json")
        digital["top_module"] = top_module
        digital["rtl_files"] = rtl_files
        digital["constraints_sdc"] = canonical_sdc_path
        digital["impl_filelist"] = filelist_path
        digital["openlane_config"] = cfg_path
        digital["implementation_setup_log"] = setup_log_path
        digital["foundry_profile"] = profile_path

        digital["impl_setup"] = {
            "status": "ok",
            "stage_dir": stage_dir,
            "constraints_sdc": canonical_sdc_path,
            "impl_filelist": filelist_path,
            "openlane_config": cfg_path,
            "corners_json": corners_path,
            "input_resolution_log": input_log_path,
            "implementation_setup_log": setup_log_path,
            "summary_json": summary_path,
            "sdc_source": sdc_source,
        }

        logger.info(f"{AGENT_NAME}: state.digital keys={list(digital.keys())}")
        state["status"] = "✅ Digital implementation setup generated."
        return state

    except Exception as e:
        logger.exception(f"{AGENT_NAME}: failed")
        state["status"] = f"❌ {AGENT_NAME} failed: {type(e).__name__}: {e}"
        return state