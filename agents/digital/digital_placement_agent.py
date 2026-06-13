import os
import json
import glob
import shutil
import subprocess
import re
import logging
from datetime import datetime
logger = logging.getLogger("chiploop")

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Placement Agent"

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


def _run(cmd: list[str], cwd: str, state: dict | None = None) -> tuple[int, str]:
    p = run_command(state or {}, "digital_placement", [str(x) for x in cmd], cwd=cwd, timeout_sec=1800)
    return p.returncode if p.returncode is not None else 1, (p.stdout or "") + (p.stderr or "")


def _latest_run_dir(run_work_dir: str) -> str | None:
    run_roots = [
        os.path.join(run_work_dir, "runs"),
        os.path.join(run_work_dir, "place", "runs"),
    ]
    dirs = []
    for runs_dir in run_roots:
        if not os.path.isdir(runs_dir):
            continue
        dirs.extend(os.path.join(runs_dir, d) for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d)))
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


def _copy_primary_def(latest: str | None, stage_dir: str) -> str | None:
    if not latest:
        return None
    dst = os.path.join(stage_dir, "primary.def")
    candidates = []
    candidates += glob.glob(os.path.join(latest, "final", "def", "*.def"))
    candidates += glob.glob(os.path.join(latest, "results", "**", "*.def"), recursive=True)
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getsize(p))
    shutil.copy2(candidates[-1], dst)
    return dst

def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None


def _macro_names_from_lefs(lef_paths: list[str]) -> list[str]:
    names = []
    for path in lef_paths or []:
        try:
            text = open(path, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for match in re.finditer(r"^\s*MACRO\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE):
            name = match.group(1)
            if name not in names:
                names.append(name)
    return names


def _macro_instances_from_netlists(netlist_paths: list[str], macro_names: list[str]) -> list[str]:
    if not macro_names:
        return []
    macro_re = "|".join(re.escape(name) for name in macro_names)
    inst_re = re.compile(rf"\b(?:{macro_re})\s+([A-Za-z_\\][A-Za-z0-9_$\\.]*)\s*\(", flags=re.MULTILINE)
    instances = []
    for path in netlist_paths or []:
        try:
            text = open(path, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for match in inst_re.finditer(text):
            inst = match.group(1).strip().lstrip("\\")
            if inst and inst not in instances:
                instances.append(inst)
    return instances


def _die_area_xy(cfg: dict) -> tuple[float, float]:
    raw = str(cfg.get("DIE_AREA") or "").strip()
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", raw)]
    if len(nums) >= 4:
        llx, lly, urx, ury = nums[:4]
        return (llx + max((urx - llx) * 0.15, 10.0), lly + max((ury - lly) * 0.15, 10.0))
    return 20.0, 20.0


def _write_macro_placement_cfg_if_needed(
    cfg: dict,
    work_stage_dir: str,
    stage_netlists: list[str],
    macro_lef_paths: list[str],
) -> str | None:
    if cfg.get("MACRO_PLACEMENT_CFG"):
        return None
    macro_names = _macro_names_from_lefs(macro_lef_paths)
    instances = _macro_instances_from_netlists(stage_netlists, macro_names)
    if not instances:
        return None

    placement_dir = os.path.join(work_stage_dir, "inputs", "macros")
    _ensure_dir(placement_dir)
    placement_path = os.path.join(placement_dir, "macro_placement.cfg")
    x0, y0 = _die_area_xy(cfg)
    lines = []
    for idx, inst in enumerate(instances):
        lines.append(f"{inst} {x0 + idx * 20.0:.3f} {y0 + idx * 20.0:.3f} N")
    _write_text(placement_path, "\n".join(lines) + "\n")
    cfg["MACRO_PLACEMENT_CFG"] = "dir::inputs/macros/macro_placement.cfg"
    return placement_path


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    floorplan_state = digital.get("floorplan") or {}

    cand = floorplan_state.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital.floorplan -> {cand}")
        return cand

    cand = digital.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital -> {cand}")
        return cand

    floorplan_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "floorplan", "constraints", "*.sdc")))
    for cand in floorplan_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from floorplan -> {cand}")
            return cand

    impl_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "impl_setup", "constraints", "*.sdc")))
    for cand in impl_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from impl_setup -> {cand}")
            return cand

    synth_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "constraints", "*.sdc")))
    for cand in synth_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected SDC from synth -> {cand}")
            return cand

    legacy = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))
    for cand in legacy:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected legacy SDC -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None




def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    floorplan_state = digital.get("floorplan") or {}

    # 1) Prefer floorplan-owned config first
    cand = floorplan_state.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital.floorplan -> {cand}")
        return cand

    # 2) Then generic digital state
    cand = digital.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital -> {cand}")
        return cand

    # 3) Then concrete floorplan config on disk
    for cand in [
        os.path.join(workflow_dir, "digital", "floorplan", "config.json"),
        os.path.join(workflow_dir, "digital", "impl_setup", "openlane", "config.json"),
        os.path.join(workflow_dir, "digital", "synth", "config.json"),
        os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json"),
    ]:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config fallback -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no OpenLane config found")
    return None

def _resolve_macro_files_from_workflow(workflow_dir: str, exts: tuple[str, ...]) -> list[str]:
    hits = []
    for ext in exts:
        hits.extend(glob.glob(os.path.join(workflow_dir, "**", f"*{ext}"), recursive=True))

    out = []
    seen = set()
    for p in sorted(hits):
        base = os.path.basename(p).lower()

        # reject debug/raw scratch artifacts
        if base.endswith("_llm_lef_raw.lef"):
            continue
        if base.endswith("_raw.lef"):
            continue
        if "debug" in base:
            continue

        ap = os.path.abspath(p)
        if ap in seen:
            continue
        seen.add(ap)
        out.append(ap)
    return out

def _stage_macro_inputs(state: dict, workflow_dir: str, work_stage_dir: str) -> tuple[list[str], list[str], list[str]]:
    digital = state.get("digital") or {}

    macro_lefs = list(dict.fromkeys(p for p in (digital.get("macro_lefs") or []) if p and os.path.exists(p)))
    macro_libs = list(dict.fromkeys(p for p in (digital.get("macro_libs") or []) if p and os.path.exists(p)))
    macro_gds  = list(dict.fromkeys(p for p in (digital.get("macro_gds") or []) if p and os.path.exists(p)))

    inputs_dir = os.path.join(work_stage_dir, "inputs", "macros")
    lef_dir = os.path.join(inputs_dir, "lef")
    lib_dir = os.path.join(inputs_dir, "lib")
    gds_dir = os.path.join(inputs_dir, "gds")
    _ensure_dir(lef_dir)
    _ensure_dir(lib_dir)
    _ensure_dir(gds_dir)

    staged_lefs, staged_libs, staged_gds = [], [], []
    seen_staged_lefs, seen_staged_libs, seen_staged_gds = set(), set(), set()

    for src in macro_lefs:
        basename = os.path.basename(src)
        dst = os.path.join(lef_dir, basename)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        rel = f"dir::inputs/macros/lef/{basename}"
        if rel not in seen_staged_lefs:
            staged_lefs.append(rel)
            seen_staged_lefs.add(rel)

    for src in macro_libs:
        basename = os.path.basename(src)
        dst = os.path.join(lib_dir, basename)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        rel = f"dir::inputs/macros/lib/{basename}"
        if rel not in seen_staged_libs:
            staged_libs.append(rel)
            seen_staged_libs.add(rel)

    for src in macro_gds:
        basename = os.path.basename(src)
        dst = os.path.join(gds_dir, basename)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        rel = f"dir::inputs/macros/gds/{basename}"
        if rel not in seen_staged_gds:
            staged_gds.append(rel)
            seen_staged_gds.add(rel)

    return staged_lefs, staged_libs, staged_gds


def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "place")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")
    netlist_dir = os.path.join(stage_dir, "netlist")

    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)
    _ensure_dir(netlist_dir)

    synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    if not synth_netlists:
        synth_netlists = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "**", "*.v"), recursive=True))
    if not synth_netlists:
        raise RuntimeError("No synthesized netlist found. Expected digital/synth/netlist/*.v")

    for nl in synth_netlists:
        shutil.copy2(nl, os.path.join(netlist_dir, os.path.basename(nl)))
    # Single source-of-truth SDC
   
    logger.info(f"🏁 Running {AGENT_NAME}")
    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC: no constraints_sdc found in state, impl_setup, floorplan, synth, or legacy constraints.")

    sdc_basename = os.path.basename(upstream_sdc)
    stage_sdc = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)
    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    logger.info(f"{AGENT_NAME}: upstream_sdc={upstream_sdc}")
    logger.info(f"{AGENT_NAME}: staged_sdc={stage_sdc}")

    # Config baseline: implementation setup preferred

    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Missing OpenLane config: no config found in state, impl_setup, synth, or foundry.")
    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")
    


    cfg = _read_json(base_cfg_path)

    logger.info(f"{AGENT_NAME}: inherited FP_DEF_TEMPLATE={cfg.get('FP_DEF_TEMPLATE')}")
    logger.info(f"{AGENT_NAME}: inherited PL_SKIP_INITIAL_PLACEMENT={cfg.get('PL_SKIP_INITIAL_PLACEMENT')}")

    # PnR stages: avoid SYNTH_SDC_FILE (OpenLane2 warns unknown key)
    cfg.pop("SYNTH_SDC_FILE", None)

    # Stage-local SSOT SDC
   
    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"

    # Skip Verilator lint for macro-backed mixed-signal/system PD reuse
    cfg["RUN_LINTER"] = False

    # Explicit netlist list (avoid netlist/*.v glob validation failures)
    stage_netlists = sorted(glob.glob(os.path.join(netlist_dir, "*.v")))
    if not stage_netlists:
        raise RuntimeError(f"No .v files present under {netlist_dir}")
    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(p)}" for p in stage_netlists]

    inferred = None
    if str(cfg.get("DESIGN_NAME","")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(stage_netlists[0])
    if inferred:
        cfg["DESIGN_NAME"] = inferred
        state["design_name"] = inferred

    top_module = str(cfg.get("DESIGN_NAME", "")).strip() or "top"
    

    # ---- Docker/run.sh ----
    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "/root/chiploop-backend/backend/pdk"

    # -------------------------------------------------------
    # Shared OpenLane run workspace + run tag (generic)
    # MUST be defined BEFORE using work_stage_dir
    # -------------------------------------------------------
    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    

    work_stage_dir = os.path.join(run_work_dir, "place")
    _ensure_dir(work_stage_dir)


    staged_lefs, staged_libs, staged_gds = _stage_macro_inputs(state, workflow_dir, work_stage_dir)
    macro_placement_cfg = _write_macro_placement_cfg_if_needed(
        cfg=cfg,
        work_stage_dir=work_stage_dir,
        stage_netlists=stage_netlists,
        macro_lef_paths=(state.get("digital") or {}).get("macro_lefs") or [],
    )

    if staged_lefs:
        cfg["EXTRA_LEFS"] = staged_lefs
    if staged_libs:
        cfg["EXTRA_LIBS"] = staged_libs
    if staged_gds:
        cfg["EXTRA_GDS_FILES"] = staged_gds
    if not (staged_lefs or staged_libs or staged_gds):
        cfg.pop("EXTRA_LEFS", None)
        cfg.pop("EXTRA_LIBS", None)
        cfg.pop("EXTRA_GDS_FILES", None)
        cfg.pop("MACRO_PLACEMENT_CFG", None)
        cfg.pop("MACROS", None)
        cfg.pop("FP_DEF_TEMPLATE", None)

    logger.info(f"{AGENT_NAME}: staged macro LEFs -> {staged_lefs}")
    logger.info(f"{AGENT_NAME}: staged macro LIBs -> {staged_libs}")
    logger.info(f"{AGENT_NAME}: staged macro GDS -> {staged_gds}")

    # Write configs (contract + exec)
    config_path = os.path.join(stage_dir, "config.json")
    _write_text(config_path, json.dumps(cfg, indent=2))

    exec_config_path = os.path.join(work_stage_dir, "config.json")
    _write_text(exec_config_path, json.dumps(cfg, indent=2))

    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure_dir(inputs_constraints_dir)
    _ensure_dir(inputs_netlist_dir)

    shutil.copy2(stage_sdc, os.path.join(inputs_constraints_dir, sdc_basename))


    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"

    for p in stage_netlists:
        shutil.copy2(p, os.path.join(inputs_netlist_dir, os.path.basename(p)))

    input_log = "\n".join([
        f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={workflow_dir}",
        f"upstream_sdc={upstream_sdc}",
        f"sdc_basename={sdc_basename}",
        f"stage_sdc={stage_sdc}",
        f"base_cfg_path={base_cfg_path}",
        f"run_work_dir={run_work_dir}",
        f"run_tag={run_tag}",
        f"top_module={top_module}",
        f"netlist_count={len(stage_netlists)}",
        f"macro_lef_count={len(staged_lefs)}",
        f"macro_lib_count={len(staged_libs)}",
        f"macro_gds_count={len(staged_gds)}",
        f"fp_def_template={cfg.get('FP_DEF_TEMPLATE')}",
        f"pl_skip_initial_placement={cfg.get('PL_SKIP_INITIAL_PLACEMENT')}",
        f"macro_placement_cfg={cfg.get('MACRO_PLACEMENT_CFG')}",
        f"macro_placement_cfg_path={macro_placement_cfg}",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "placement_input_resolution.log"), input_log)


    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "PDK_VARIANT={pdk_variant}"
echo "OPENLANE_IMAGE={openlane_image}"
echo "WORKDIR=/work"

export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \
  -v "{pdk_root_host}":/pdk \
  -v "{run_work_dir}":/work \
  -e PDK={pdk_variant} \
  -e PDK_ROOT=/pdk \
  {openlane_image} \
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to OpenROAD.DetailedPlacement place/config.json'


"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir, state=state)
    log_path = os.path.join(logs_dir, "openlane_place.log")
    _write_text(log_path, out)

    latest = _latest_run_dir(run_work_dir)
    metrics_path = _copy_metrics(latest, stage_dir)
    def_path = _copy_primary_def(latest, stage_dir)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "outputs": {
            "sdc": f"digital/place/constraints/{sdc_basename}",
            "metrics_json": "digital/place/metrics.json" if metrics_path else None,
            "primary_def": "digital/place/primary.def" if def_path else None,
            "macro_placement_cfg": "digital/place/macro_placement.cfg" if macro_placement_cfg else None,
            "log": "digital/place/logs/openlane_place.log",
            "openlane_run_dir": latest,
        },
    }

    _write_text(os.path.join(stage_dir, "place_summary.json"), json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir, "place_summary.md"), f"# Placement\n\n- status: `{summary['status']}` (rc={rc})\n")

    # ---- Upload ----
    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "place/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"place/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "place/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "place/logs/openlane_place.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "place/place_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            "place/logs/placement_input_resolution.log",
            input_log,
        )
        if metrics_path and os.path.exists(metrics_path):
            with open(metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "place/metrics.json", f.read())
        if def_path and os.path.exists(def_path):
            with open(def_path, "r", encoding="utf-8", errors="ignore") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "place/primary.def", f.read())
        if macro_placement_cfg and os.path.exists(macro_placement_cfg):
            with open(macro_placement_cfg, "r", encoding="utf-8", errors="ignore") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "place/macro_placement.cfg", f.read())
    except Exception as e:
        print(f"⚠️ {AGENT_NAME} upload failed: {e}")

    state.setdefault("digital", {})["place"] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "primary_def": def_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": config_path,
        "input_resolution_log": os.path.join(logs_dir, "placement_input_resolution.log"),
        "openlane_run_dir": latest,
        "macro_placement_cfg": macro_placement_cfg,
    }

    if summary["status"] != "ok" or not def_path:
        raise RuntimeError(
            f"{AGENT_NAME}: placement failed before post-place STA "
            f"(status={summary['status']}, rc={rc}, def={'present' if def_path else 'missing'})"
        )

    
    return state
