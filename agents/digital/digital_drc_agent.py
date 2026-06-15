import os
import json
import glob
import shutil
import subprocess
import re
import logging
from datetime import datetime
from xml.etree import ElementTree
logger = logging.getLogger("chiploop")

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital DRC Agent"
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


def _select_single_top_netlist(paths: list[str]) -> list[str]:
    if len(paths) <= 1:
        return paths
    physical = [p for p in paths if os.path.basename(p).endswith((".pnl.v", ".nl.v"))]
    if physical:
        return [sorted(physical, key=lambda p: (0 if ".pnl." in os.path.basename(p).lower() else 1, 0 if ".nl." in os.path.basename(p).lower() else 1, len(p)))[0]]
    return [sorted(paths, key=lambda p: (0 if "synth" in os.path.basename(p).lower() else 1, len(p)))[0]]


def _run(cmd: list[str], cwd: str, state: dict | None = None) -> tuple[int, str]:
    p = run_command(state or {}, "digital_drc", [str(x) for x in cmd], cwd=cwd, timeout_sec=1800)
    return p.returncode if p.returncode is not None else 1, (p.stdout or "") + (p.stderr or "")


def _latest_run_dir(run_work_dir: str) -> str | None:
    run_roots = [
        os.path.join(run_work_dir, "runs"),
        os.path.join(run_work_dir, "drc", "runs"),
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


def _copy_drc_report(latest: str | None, stage_dir: str) -> tuple[str | None, int | None]:
    if not latest:
        return None, None
    reports = sorted(glob.glob(os.path.join(latest, "**", "*drc*.xml"), recursive=True))
    if not reports:
        return None, None
    src = reports[-1]
    reports_dir = os.path.join(stage_dir, "reports")
    _ensure_dir(reports_dir)
    dst = os.path.join(reports_dir, os.path.basename(src))
    shutil.copy2(src, dst)
    return dst, _drc_violation_count(dst)


def _drc_violation_count(report_path: str | None) -> int | None:
    if not report_path or not os.path.exists(report_path):
        return None
    try:
        root = ElementTree.parse(report_path).getroot()
    except Exception:
        return None
    items = root.findall(".//item")
    if items:
        return len(items)
    values = root.findall(".//value")
    if values:
        return len(values)
    return 0


def _xor_difference_count(log: str) -> int | None:
    counts = [int(match.group(1)) for match in re.finditer(r"Total XOR differences:\s*(\d+)", log or "", flags=re.IGNORECASE)]
    if counts:
        return counts[-1]
    match = re.search(r"(\d+)\s+XOR differences found", log or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _drc_status(rc: int, violation_count: int | None, log: str) -> str:
    if violation_count is not None:
        return "clean" if violation_count == 0 else "violations_found"
    if rc == 0:
        return "completed"
    if _xor_difference_count(log):
        return "completed_with_deferred_xor_error"
    return "failed"


def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None

def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    fill_state = digital.get("fill") or {}

    cand = fill_state.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital.fill -> {cand}")
        return cand

    cand = digital.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital -> {cand}")
        return cand

    for stage in ["fill", "route", "sta_postroute", "sta_postcts", "cts", "place", "floorplan", "impl_setup", "synth"]:
        cands = sorted(glob.glob(os.path.join(workflow_dir, "digital", stage, "constraints", "*.sdc")))
        for cand in cands:
            if os.path.exists(cand):
                logger.info(f"{AGENT_NAME}: selected SDC from {stage} -> {cand}")
                return cand


def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    fill_state = digital.get("fill") or {}

    cand = fill_state.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital.fill -> {cand}")
        return cand

    cand = digital.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital -> {cand}")
        return cand

    for cand in [
        os.path.join(workflow_dir, "digital", "fill", "config.json"),
        os.path.join(workflow_dir, "digital", "route", "config.json"),
        os.path.join(workflow_dir, "digital", "cts", "config.json"),
        os.path.join(workflow_dir, "digital", "place", "config.json"),
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


def _stage_macro_placement_cfg_if_needed(cfg: dict, state: dict, workflow_dir: str, work_stage_dir: str) -> str | None:
    if not cfg.get("MACRO_PLACEMENT_CFG"):
        return None
    digital = state.get("digital") or {}
    place_state = digital.get("place") or {}
    candidates = [
        place_state.get("macro_placement_cfg"),
        os.path.join(workflow_dir, "digital", "place", "macro_placement.cfg"),
    ]
    src = next((p for p in candidates if isinstance(p, str) and os.path.exists(p)), None)
    if not src:
        cfg.pop("MACRO_PLACEMENT_CFG", None)
        return None
    dst = os.path.join(work_stage_dir, "inputs", "macros", "macro_placement.cfg")
    _ensure_dir(os.path.dirname(dst))
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    cfg["MACRO_PLACEMENT_CFG"] = "dir::inputs/macros/macro_placement.cfg"
    return dst


def _macro_blackbox_deferred(staged_lefs: list[str], staged_libs: list[str], staged_gds: list[str]) -> bool:
    return bool((staged_lefs or staged_libs) and not staged_gds)

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "drc")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")
    netlist_dir = os.path.join(stage_dir, "netlist")

    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)
    _ensure_dir(netlist_dir)

    logger.info(f"🏁 Running {AGENT_NAME}")

    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC: no constraints_sdc found in state or prior stages.")

    sdc_basename = os.path.basename(upstream_sdc)
    stage_sdc = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)
    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Missing OpenLane config: no config found in state or prior stages.")
    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")

    cfg = _read_json(base_cfg_path)
    cfg.pop("SYNTH_SDC_FILE", None)
    cfg["RUN_LINTER"] = False
    cfg["RUN_XOR"] = False

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure_dir(inputs_constraints_dir)
    _ensure_dir(inputs_netlist_dir)

    shutil.copy2(stage_sdc, os.path.join(inputs_constraints_dir, sdc_basename))
    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"

    stage_netlists = _select_single_top_netlist(sorted(glob.glob(os.path.join(inputs_netlist_dir, "*.v"))))
    if not stage_netlists:
        raise RuntimeError("DRC: missing run_work/inputs/netlist/*.v (synth/floorplan should populate it).")

    for nl in stage_netlists:
        shutil.copy2(nl, os.path.join(netlist_dir, os.path.basename(nl)))

    stage_netlists_local = sorted(glob.glob(os.path.join(netlist_dir, "*.v")))
    cfg["VERILOG_FILES"] = [f"inputs/netlist/{os.path.basename(p)}" for p in stage_netlists_local]

    inferred = None
    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(stage_netlists_local[0])
    if inferred:
        cfg["DESIGN_NAME"] = inferred
        state["design_name"] = inferred

    top_module = str(cfg.get("DESIGN_NAME", "")).strip() or "top"

    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host

    work_stage_dir = os.path.join(run_work_dir, "drc")
    _ensure_dir(work_stage_dir)

    staged_lefs, staged_libs, staged_gds = _stage_macro_inputs(state, workflow_dir, work_stage_dir)
    macro_placement_cfg = _stage_macro_placement_cfg_if_needed(cfg, state, workflow_dir, work_stage_dir)

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

    config_path = os.path.join(stage_dir, "config.json")
    _write_text(config_path, json.dumps(cfg, indent=2))

    if _macro_blackbox_deferred(staged_lefs, staged_libs, staged_gds):
        out = "Analog macro LEF/LIB collateral is present but macro GDS is unavailable; DRC is deferred in black-box mode.\n"
        log_path = os.path.join(logs_dir, "openlane_drc.log")
        _write_text(log_path, out)
        input_resolution_log = "\n".join([
            f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
            f"workflow_id={workflow_id}",
            f"workflow_dir={workflow_dir}",
            f"top_module={top_module}",
            f"macro_lef_count={len(staged_lefs)}",
            f"macro_lib_count={len(staged_libs)}",
            f"macro_gds_count={len(staged_gds)}",
            f"macro_placement_cfg={cfg.get('MACRO_PLACEMENT_CFG')}",
            f"macro_placement_cfg_path={macro_placement_cfg}",
            "status=blackbox_deferred",
            "reason=analog_macro_gds_missing",
        ]) + "\n"
        input_resolution_log_path = os.path.join(logs_dir, "drc_input_resolution.log")
        _write_text(input_resolution_log_path, input_resolution_log)
        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "blackbox_deferred",
            "drc_status": "blackbox_deferred",
            "drc_violations": None,
            "xor_differences": None,
            "return_code": 0,
            "blackbox_macros": (state.get("digital") or {}).get("macro_blackboxes") or [],
            "reason": "analog_macro_gds_missing",
            "outputs": {
                "sdc": f"digital/drc/constraints/{sdc_basename}",
                "metrics_json": None,
                "drc_report_xml": None,
                "log": "digital/drc/logs/openlane_drc.log",
                "openlane_run_dir": None,
            },
        }
        _write_text(os.path.join(stage_dir, "drc_summary.json"), json.dumps(summary, indent=2))
        _write_text(os.path.join(stage_dir, "drc_summary.md"), "# DRC (KLayout)\n\n- status: `blackbox_deferred`\n- reason: `analog_macro_gds_missing`\n")
        try:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/config.json", json.dumps(cfg, indent=2))
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"drc/constraints/{sdc_basename}", sdc_text)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/logs/openlane_drc.log", out)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/logs/drc_input_resolution.log", input_resolution_log)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/drc_summary.json", json.dumps(summary, indent=2))
        except Exception as e:
            print(f"⚠️ {AGENT_NAME} upload failed: {e}")
        state.setdefault("digital", {})["drc"] = {
            "status": "blackbox_deferred",
            "drc_status": "blackbox_deferred",
            "stage_dir": stage_dir,
            "constraints_sdc": stage_sdc,
            "openlane_config": config_path,
            "input_resolution_log": input_resolution_log_path,
            "blackbox_macros": summary["blackbox_macros"],
        }
        return state

    exec_config_path = os.path.join(work_stage_dir, "config.json")
    _write_text(exec_config_path, json.dumps(cfg, indent=2))

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
        f"netlist_count={len(stage_netlists_local)}",
        f"macro_placement_cfg={cfg.get('MACRO_PLACEMENT_CFG')}",
        f"macro_placement_cfg_path={macro_placement_cfg}",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "drc_input_resolution.log"), input_log)

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
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to KLayout.DRC drc/config.json'
"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir, state=state)
    log_path = os.path.join(logs_dir, "openlane_drc.log")
    _write_text(log_path, out)

    latest = _latest_run_dir(work_stage_dir)
    metrics_path = _copy_metrics(latest, stage_dir)
    drc_report_path, drc_violations = _copy_drc_report(latest, stage_dir)
    drc_status = _drc_status(rc, drc_violations, out)
    xor_differences = _xor_difference_count(out)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if drc_status in {"clean", "completed"} else drc_status,
        "drc_status": drc_status,
        "drc_violations": drc_violations,
        "xor_differences": xor_differences,
        "return_code": rc,
        "outputs": {
            "sdc": f"digital/drc/constraints/{sdc_basename}",
            "metrics_json": "digital/drc/metrics.json" if metrics_path else None,
            "drc_report_xml": f"digital/drc/reports/{os.path.basename(drc_report_path)}" if drc_report_path else None,
            "log": "digital/drc/logs/openlane_drc.log",
            "openlane_run_dir": latest,
        },
    }

    _write_text(os.path.join(stage_dir, "drc_summary.json"), json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir, "drc_summary.md"), f"# DRC (KLayout)\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"drc/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/logs/openlane_drc.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/drc_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/logs/drc_input_resolution.log", input_log)
        if drc_report_path and os.path.exists(drc_report_path):
            save_text_artifact_and_record(
                workflow_id,
                AGENT_NAME,
                "digital",
                f"drc/reports/{os.path.basename(drc_report_path)}",
                open(drc_report_path, "r", encoding="utf-8", errors="ignore").read(),
            )
        if metrics_path and os.path.exists(metrics_path):
            with open(metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "drc/metrics.json", f.read())
    except Exception as e:
        print(f"⚠️ {AGENT_NAME} upload failed: {e}")

    state.setdefault("digital", {})["drc"] = {
        "status": summary["status"],
        "drc_status": drc_status,
        "drc_violations": drc_violations,
        "xor_differences": xor_differences,
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": config_path,
        "input_resolution_log": os.path.join(logs_dir, "drc_input_resolution.log"),
        "openlane_run_dir": latest,
    }
    return state
