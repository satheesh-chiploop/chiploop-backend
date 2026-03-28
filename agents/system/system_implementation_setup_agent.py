import os
import json
import glob
import shutil
import logging
from datetime import datetime

from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger("chiploop")

AGENT_NAME = "System Implementation Setup Agent"


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


def _normalize_list(x) -> list[str]:
    if not x:
        return []
    if isinstance(x, list):
        return [str(v) for v in x if v]
    return [str(x)]


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        norm = os.path.abspath(item)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(item)
    return out


def _existing_files(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        if p and os.path.exists(p):
            out.append(os.path.abspath(p))
    return _dedupe_keep_order(out)


def _resolve_foundry_profile(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    candidates = [
        digital.get("foundry_profile"),
        os.path.join(workflow_dir, "digital", "foundry", "foundry_profile.json"),
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected foundry profile -> {cand}")
            return os.path.abspath(cand)

    logger.warning(f"{AGENT_NAME}: no foundry profile found")
    return None


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
            return os.path.abspath(cand)

    spec_dir = os.path.join(workflow_dir, "spec")
    if os.path.isdir(spec_dir):
        files = sorted(glob.glob(os.path.join(spec_dir, "*_spec.json")))
        if files:
            logger.info(f"{AGENT_NAME}: selected spec_json from workflow/spec -> {files[0]}")
            return os.path.abspath(files[0])

    logger.warning(f"{AGENT_NAME}: no spec_json found")
    return None


def _resolve_top_module(state: dict, profile: dict, spec: dict) -> str:
    system = state.get("system") or {}
    digital = state.get("digital") or {}

    return (
        state.get("soc_top_phys_module")
        or system.get("soc_top_phys_module")
        or state.get("top_module_phys")
        or ((state.get("system_integration_intent") or {}).get("top") or {}).get("phys_module")
        or (((spec.get("hierarchy") or {}).get("top_module") or {}).get("name"))
        or spec.get("top_module")
        or spec.get("design_name")
        or digital.get("top_module")
        or profile.get("top_module")
        or "soc_top_phys"
    )


def _resolve_clock_reset(profile: dict, spec: dict) -> tuple[str, str, float]:
    timing = profile.get("timing") or {}
    oc = spec.get("operating_constraints") or {}
    cds = oc.get("clock_domains") or []
    rs = oc.get("reset_signals") or []

    clk_name = timing.get("clock_name") or "clk"
    reset_name = timing.get("reset_name") or "reset_n"
    clk_mhz = timing.get("target_clock_mhz") or 50.0

    if cds and isinstance(cds[0], dict):
        clk_name = cds[0].get("name") or clk_name
        if cds[0].get("frequency_mhz"):
            try:
                clk_mhz = float(cds[0]["frequency_mhz"])
            except Exception:
                pass
        elif cds[0].get("period_ns"):
            try:
                p = float(cds[0]["period_ns"])
                if p > 0:
                    clk_mhz = 1000.0 / p
            except Exception:
                pass

    if rs and isinstance(rs[0], dict):
        reset_name = rs[0].get("name") or reset_name

    try:
        clk_mhz = float(clk_mhz)
    except Exception:
        clk_mhz = 50.0

    return clk_name, reset_name, clk_mhz


def _resolve_system_phys_rtl(state: dict, workflow_dir: str) -> list[str]:
    system = state.get("system") or {}
    digital = state.get("digital") or {}

    candidates = []

    candidates.extend(_normalize_list(state.get("system_rtl_filelist_phys")))
    candidates.extend(_normalize_list(system.get("rtl_filelist_phys")))
    candidates.extend(_normalize_list(state.get("rtl_inputs_phys")))
    candidates.extend(_normalize_list(system.get("rtl_inputs_phys")))

    # Compatibility fallback: if someone already stuffed physical RTL into digital.rtl_files
    candidates.extend(_normalize_list(digital.get("rtl_files")))

    existing = _existing_files(candidates)
    if existing:
        logger.info(f"{AGENT_NAME}: selected physical RTL from state -> {len(existing)} files")
        return existing

    # Fallbacks from generated integration outputs and handoff dirs
    fallback = []

    phys_top = state.get("soc_top_phys_path") or system.get("soc_top_phys_path")
    if phys_top:
        abs_top = phys_top if os.path.isabs(phys_top) else os.path.join(workflow_dir, phys_top)
        if os.path.exists(abs_top):
            fallback.append(abs_top)

    for pat in [
        os.path.join(workflow_dir, "digital", "rtl_refactored", "*.v"),
        os.path.join(workflow_dir, "digital", "rtl_refactored", "*.sv"),
        os.path.join(workflow_dir, "system", "integration", "*_phys.sv"),
        os.path.join(workflow_dir, "analog", "**", "*stub*.v"),
        os.path.join(workflow_dir, "analog", "**", "*stub*.sv"),
        os.path.join(workflow_dir, "analog", "**", "*wrapper*.v"),
        os.path.join(workflow_dir, "analog", "**", "*wrapper*.sv"),
        os.path.join(workflow_dir, "analog", "**", "*macro*.v"),
        os.path.join(workflow_dir, "analog", "**", "*macro*.sv"),
    ]:
        fallback.extend(glob.glob(pat, recursive=True))

    existing = _existing_files(fallback)
    if existing:
        logger.info(f"{AGENT_NAME}: selected physical RTL from fallback discovery -> {len(existing)} files")
        return existing

    logger.warning(f"{AGENT_NAME}: no physical RTL files found")
    return []


def _discover_macro_files(
    workflow_dir: str,
    state_list: list[str],
    exts: tuple[str, ...],
) -> list[str]:
    hits = []
    hits.extend(_existing_files(state_list))

    if hits:
        return hits

    for ext in exts:
        hits.extend(glob.glob(os.path.join(workflow_dir, "**", f"*{ext}"), recursive=True))

    hits = _existing_files(hits)
    return hits


def _resolve_macro_lefs(state: dict, workflow_dir: str) -> list[str]:
    system = state.get("system") or {}

    state_candidates = []
    state_candidates.extend(_normalize_list(state.get("system_lef_filelist_phys")))
    state_candidates.extend(_normalize_list(system.get("lef_filelist_phys")))
    state_candidates.extend(_normalize_list(state.get("macro_lefs")))
    state_candidates.extend(_normalize_list(system.get("macro_lefs")))

    lefs = _discover_macro_files(workflow_dir, state_candidates, (".lef",))
    if lefs:
        logger.info(f"{AGENT_NAME}: resolved macro LEFs -> {len(lefs)} files")
    else:
        logger.warning(f"{AGENT_NAME}: no macro LEFs found")
    return lefs


def _resolve_macro_libs(state: dict, workflow_dir: str) -> list[str]:
    system = state.get("system") or {}

    state_candidates = []
    state_candidates.extend(_normalize_list(state.get("system_lib_filelist_phys")))
    state_candidates.extend(_normalize_list(system.get("lib_filelist_phys")))
    state_candidates.extend(_normalize_list(state.get("macro_libs")))
    state_candidates.extend(_normalize_list(system.get("macro_libs")))

    libs = _discover_macro_files(workflow_dir, state_candidates, (".lib", ".lib.gz", ".db"))
    if libs:
        logger.info(f"{AGENT_NAME}: resolved macro LIBs -> {len(libs)} files")
    else:
        logger.warning(f"{AGENT_NAME}: no macro LIBs found")
    return libs


def _resolve_macro_gds(state: dict, workflow_dir: str) -> list[str]:
    system = state.get("system") or {}

    state_candidates = []
    state_candidates.extend(_normalize_list(state.get("system_gds_filelist_phys")))
    state_candidates.extend(_normalize_list(system.get("gds_filelist_phys")))
    state_candidates.extend(_normalize_list(state.get("macro_gds")))
    state_candidates.extend(_normalize_list(system.get("macro_gds")))

    gds = _discover_macro_files(workflow_dir, state_candidates, (".gds", ".gds.gz"))
    if gds:
        logger.info(f"{AGENT_NAME}: resolved macro GDS -> {len(gds)} files")
    else:
        logger.info(f"{AGENT_NAME}: no macro GDS found (optional)")
    return gds


def _resolve_upstream_sdc(state: dict, workflow_dir: str) -> tuple[str | None, str]:
    system = state.get("system") or {}
    digital = state.get("digital") or {}

    candidates = [
        (system.get("constraints_sdc"), "system.constraints_sdc"),
        (state.get("system_constraints_sdc"), "state.system_constraints_sdc"),
        (digital.get("constraints_sdc"), "digital.constraints_sdc"),
        (state.get("constraints_sdc"), "state.constraints_sdc"),
    ]
    for cand, source in candidates:
        if cand and os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from {source} -> {cand}")
            return os.path.abspath(cand), source

    search_patterns = [
        (os.path.join(workflow_dir, "system", "constraints", "*.sdc"), "workflow/system/constraints"),
        (os.path.join(workflow_dir, "system", "integration", "*.sdc"), "workflow/system/integration"),
        (os.path.join(workflow_dir, "digital", "constraints", "*.sdc"), "workflow/digital/constraints"),
        (os.path.join(workflow_dir, "digital", "synth", "constraints", "*.sdc"), "workflow/digital/synth/constraints"),
    ]
    for pattern, source in search_patterns:
        files = sorted(glob.glob(pattern))
        for cand in files:
            if os.path.exists(cand):
                logger.info(f"{AGENT_NAME}: selected SDC from {source} -> {cand}")
                return os.path.abspath(cand), source

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None, "none"


def _build_fallback_sdc(clk_name: str, clk_mhz: float, reset_name: str) -> str:
    period_ns = 1000.0 / float(clk_mhz if clk_mhz else 50.0)
    return f"""# Auto-generated fallback by {AGENT_NAME}
create_clock -name {clk_name} -period {period_ns:.3f} [get_ports {clk_name}]
# set_false_path -from [get_ports {reset_name}]
"""


def _copy_inputs(src_files: list[str], dst_dir: str) -> tuple[list[str], list[str]]:
    _ensure_dir(dst_dir)
    copied_abs = []
    copied_rel = []

    for src in src_files:
        dst = os.path.join(dst_dir, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        copied_abs.append(os.path.abspath(dst))
        copied_rel.append(os.path.basename(dst))

    return copied_abs, copied_rel


def run_agent(state: dict) -> dict:
    try:
        print(f"\n🏁 Running {AGENT_NAME}...")
        logger.info(f"🏁 Running {AGENT_NAME}")

        workflow_id = state.get("workflow_id", "default")
        workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
        _ensure_dir(workflow_dir)

        stage_dir = os.path.join(workflow_dir, "system", "impl_setup")
        rtl_stage_dir = os.path.join(stage_dir, "rtl")
        constraints_dir = os.path.join(stage_dir, "constraints")
        macros_lef_dir = os.path.join(stage_dir, "macros", "lef")
        macros_lib_dir = os.path.join(stage_dir, "macros", "lib")
        macros_gds_dir = os.path.join(stage_dir, "macros", "gds")
        logs_dir = os.path.join(stage_dir, "logs")
        openlane_dir = os.path.join(stage_dir, "openlane")

        for d in [
            stage_dir,
            rtl_stage_dir,
            constraints_dir,
            macros_lef_dir,
            macros_lib_dir,
            macros_gds_dir,
            logs_dir,
            openlane_dir,
        ]:
            _ensure_dir(d)

        profile_path = _resolve_foundry_profile(state, workflow_dir)
        if not profile_path:
            raise RuntimeError("Missing foundry_profile.json. Run Digital Foundry Profile Agent first.")

        profile = _read_json_safe(profile_path)
        spec_json_path = _resolve_spec_json(state, workflow_dir)
        spec = _read_json_safe(spec_json_path)

        top_module = _resolve_top_module(state, profile, spec)
        clk_name, reset_name, clk_mhz = _resolve_clock_reset(profile, spec)
        clock_period_ns = 1000.0 / float(clk_mhz)

        rtl_files = _resolve_system_phys_rtl(state, workflow_dir)
        if not rtl_files:
            raise RuntimeError("No System PD physical RTL found. Run System Top Assembly Agent first.")

        macro_lefs = _resolve_macro_lefs(state, workflow_dir)
        macro_libs = _resolve_macro_libs(state, workflow_dir)
        macro_gds = _resolve_macro_gds(state, workflow_dir)

        upstream_sdc, sdc_source = _resolve_upstream_sdc(state, workflow_dir)

        copied_rtl_abs, copied_rtl_rel = _copy_inputs(rtl_files, rtl_stage_dir)
        copied_lef_abs, copied_lef_rel = _copy_inputs(macro_lefs, macros_lef_dir)
        copied_lib_abs, copied_lib_rel = _copy_inputs(macro_libs, macros_lib_dir)
        copied_gds_abs, copied_gds_rel = _copy_inputs(macro_gds, macros_gds_dir)

        filelist_path = os.path.join(stage_dir, "filelist.f")
        filelist_text = "\n".join(copied_rtl_abs) + "\n"
        _write_text(filelist_path, filelist_text)

        macro_lef_filelist_path = os.path.join(stage_dir, "macro_lefs.f")
        macro_lef_text = "\n".join(copied_lef_abs) + ("\n" if copied_lef_abs else "")
        _write_text(macro_lef_filelist_path, macro_lef_text)

        macro_lib_filelist_path = os.path.join(stage_dir, "macro_libs.f")
        macro_lib_text = "\n".join(copied_lib_abs) + ("\n" if copied_lib_abs else "")
        _write_text(macro_lib_filelist_path, macro_lib_text)

        macro_gds_filelist_path = os.path.join(stage_dir, "macro_gds.f")
        macro_gds_text = "\n".join(copied_gds_abs) + ("\n" if copied_gds_abs else "")
        _write_text(macro_gds_filelist_path, macro_gds_text)

        sdc_basename = os.path.basename(upstream_sdc) if upstream_sdc else f"{top_module}.sdc"
        canonical_sdc_path = os.path.join(constraints_dir, sdc_basename)

        if upstream_sdc and os.path.exists(upstream_sdc):
            shutil.copy2(upstream_sdc, canonical_sdc_path)
            with open(canonical_sdc_path, "r", encoding="utf-8") as f:
                sdc_text = f.read()
        else:
            sdc_text = _build_fallback_sdc(clk_name, clk_mhz, reset_name)
            _write_text(canonical_sdc_path, sdc_text)
            sdc_source = "fallback_generated"

        corners = profile.get("corners") or []
        corners_json = {
            "pdk": profile.get("pdk") or "sky130A",
            "pdk_root": profile.get("pdk_root"),
            "corners": corners,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "note": "System PD corner labels prepared for downstream OpenLane/OpenROAD stages.",
        }
        corners_path = os.path.join(stage_dir, "corners.json")
        _write_text(corners_path, json.dumps(corners_json, indent=2))

        # Keep config relative to the stage directory.
        verilog_sources = [f"dir::../rtl/{name}" for name in copied_rtl_rel]
        extra_lefs = [f"dir::../macros/lef/{name}" for name in copied_lef_rel]
        extra_libs = [f"dir::../macros/lib/{name}" for name in copied_lib_rel]
        extra_gds = [f"dir::../macros/gds/{name}" for name in copied_gds_rel]

        openlane_cfg = {
            "DESIGN_NAME": top_module,
            "PDK": profile.get("pdk") or "sky130A",
            "CLOCK_PORT": clk_name,
            "CLOCK_PERIOD": clock_period_ns,
            "VERILOG_FILES": verilog_sources,
            "SYNTH_SDC_FILE": f"dir::../constraints/{sdc_basename}",
            "PNR_SDC_FILE": f"dir::../constraints/{sdc_basename}",
            "EXTRA_LEFS": extra_lefs,
            "EXTRA_LIBS": extra_libs,
            "EXTRA_GDS_FILES": extra_gds,
            "CHIPLOOP_SOURCE_SPEC_JSON": spec_json_path,
            "CHIPLOOP_FILELIST": filelist_path,
            "CHIPLOOP_MACRO_LEF_FILELIST": macro_lef_filelist_path,
            "CHIPLOOP_MACRO_LIB_FILELIST": macro_lib_filelist_path,
            "CHIPLOOP_MACRO_GDS_FILELIST": macro_gds_filelist_path,
            "CHIPLOOP_UPSTREAM_SDC_SOURCE": sdc_source,
            "CHIPLOOP_IMPLEMENTATION_FLAVOR": "system_pd_over_digital_pd",
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
            f"clock_port={clk_name}",
            f"reset_name={reset_name}",
            f"clock_mhz={clk_mhz}",
            f"clock_period_ns={clock_period_ns}",
            f"rtl_count={len(copied_rtl_abs)}",
            f"macro_lef_count={len(copied_lef_abs)}",
            f"macro_lib_count={len(copied_lib_abs)}",
            f"macro_gds_count={len(copied_gds_abs)}",
            f"upstream_sdc={upstream_sdc}",
            f"sdc_source={sdc_source}",
            f"canonical_sdc={canonical_sdc_path}",
            f"filelist={filelist_path}",
            f"macro_lef_filelist={macro_lef_filelist_path}",
            f"macro_lib_filelist={macro_lib_filelist_path}",
            f"macro_gds_filelist={macro_gds_filelist_path}",
            f"corners_json={corners_path}",
            f"openlane_cfg={cfg_path}",
        ]) + "\n"

        input_log_path = os.path.join(logs_dir, "system_implementation_setup_input_resolution.log")
        _write_text(input_log_path, input_log)

        setup_log = "\n".join([
            f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
            "status=ok",
            f"profile_path={profile_path}",
            f"top_module={top_module}",
            f"rtl_files={len(copied_rtl_abs)}",
            f"macro_lefs={len(copied_lef_abs)}",
            f"macro_libs={len(copied_lib_abs)}",
            f"macro_gds={len(copied_gds_abs)}",
            f"sdc_source={sdc_source}",
            f"canonical_sdc={canonical_sdc_path}",
            f"filelist={filelist_path}",
            f"config={cfg_path}",
        ]) + "\n"

        setup_log_path = os.path.join(stage_dir, "system_implementation_setup.log")
        _write_text(setup_log_path, setup_log)

        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "ok",
            "mode": "system_pd_over_digital_pd",
            "outputs": {
                "filelist": "system/impl_setup/filelist.f",
                "macro_lefs": "system/impl_setup/macro_lefs.f",
                "macro_libs": "system/impl_setup/macro_libs.f",
                "macro_gds": "system/impl_setup/macro_gds.f",
                "sdc": f"system/impl_setup/constraints/{sdc_basename}",
                "corners_json": "system/impl_setup/corners.json",
                "openlane_config": "system/impl_setup/openlane/config.json",
                "input_resolution_log": "system/impl_setup/logs/system_implementation_setup_input_resolution.log",
                "setup_log": "system/impl_setup/system_implementation_setup.log",
            },
        }
        summary_path = os.path.join(stage_dir, "system_implementation_setup_summary.json")
        _write_text(summary_path, json.dumps(summary, indent=2))

        # Upload artifacts
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/filelist.f", filelist_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/macro_lefs.f", macro_lef_text or "(empty)\n")
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/macro_libs.f", macro_lib_text or "(empty)\n")
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/macro_gds.f", macro_gds_text or "(empty)\n")
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", f"impl_setup/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/corners.json", json.dumps(corners_json, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/openlane/config.json", json.dumps(openlane_cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/logs/system_implementation_setup_input_resolution.log", input_log)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/system_implementation_setup.log", setup_log)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "system", "impl_setup/system_implementation_setup_summary.json", json.dumps(summary, indent=2))

        # Compatibility handoff into digital state so downstream Digital Synthesis / PD stays unchanged.
        digital = state.setdefault("digital", {})
        digital["foundry_profile"] = profile_path
        digital["spec_json"] = spec_json_path or digital.get("spec_json")
        digital["top_module"] = top_module
        digital["rtl_files"] = copied_rtl_abs
        digital["constraints_sdc"] = canonical_sdc_path
        digital["impl_filelist"] = filelist_path
        digital["openlane_config"] = cfg_path
        digital["macro_lefs"] = copied_lef_abs
        digital["macro_libs"] = copied_lib_abs
        digital["macro_gds"] = copied_gds_abs

        digital["impl_setup"] = {
            "status": "ok",
            "mode": "system_pd_over_digital_pd",
            "stage_dir": stage_dir,
            "constraints_sdc": canonical_sdc_path,
            "impl_filelist": filelist_path,
            "openlane_config": cfg_path,
            "corners_json": corners_path,
            "input_resolution_log": input_log_path,
            "implementation_setup_log": setup_log_path,
            "summary_json": summary_path,
            "sdc_source": sdc_source,
            "macro_lefs": copied_lef_abs,
            "macro_libs": copied_lib_abs,
            "macro_gds": copied_gds_abs,
        }

        system = state.setdefault("system", {})
        system["impl_setup"] = {
            "status": "ok",
            "stage_dir": stage_dir,
            "rtl_files": copied_rtl_abs,
            "constraints_sdc": canonical_sdc_path,
            "macro_lefs": copied_lef_abs,
            "macro_libs": copied_lib_abs,
            "macro_gds": copied_gds_abs,
            "openlane_config": cfg_path,
            "summary_json": summary_path,
        }

        state["top_module"] = top_module
        state["constraints_sdc"] = canonical_sdc_path
        state["status"] = "✅ System implementation setup generated."
        return state

    except Exception as e:
        logger.exception(f"{AGENT_NAME}: failed")
        state["status"] = f"❌ {AGENT_NAME} failed: {type(e).__name__}: {e}"
        return state