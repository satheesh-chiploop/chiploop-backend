import os
import json
import glob
import shutil
import subprocess
import re
import logging
from datetime import datetime
from xml.etree import ElementTree

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger("chiploop")

AGENT_NAME = "Digital Tapeout Agent"
DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))
DEFAULT_NONBLOCKING_XOR_LAYERS = {
    item.strip()
    for item in os.getenv("CHIPLOOP_XOR_NONBLOCKING_LAYERS", "235/4").split(",")
    if item.strip()
}
DEFAULT_XOR_SIGNOFF_MODE = os.getenv("CHIPLOOP_XOR_SIGNOFF_MODE", "not_applicable").strip().lower()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


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
    p = run_command(state or {}, "digital_tapeout", [str(x) for x in cmd], cwd=cwd, timeout_sec=1800)
    return p.returncode if p.returncode is not None else 1, (p.stdout or "") + (p.stderr or "")


def _latest_run_dir(run_work_dir: str) -> str | None:
    run_roots = [
        os.path.join(run_work_dir, "runs"),
        os.path.join(run_work_dir, "tapeout", "runs"),
    ]
    dirs = []
    for runs_dir in run_roots:
        if not os.path.isdir(runs_dir):
            continue
        dirs.extend(
            os.path.join(runs_dir, d)
            for d in os.listdir(runs_dir)
            if os.path.isdir(os.path.join(runs_dir, d))
        )
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


def _copy_xor_report(latest: str | None, stage_dir: str) -> str | None:
    if not latest:
        return None
    candidates = sorted(glob.glob(os.path.join(latest, "**", "xor.xml"), recursive=True))
    if not candidates:
        candidates = sorted(glob.glob(os.path.join(latest, "**", "*xor*.xml"), recursive=True))
    if not candidates:
        return None
    src = candidates[-1]
    reports_dir = os.path.join(stage_dir, "reports")
    _ensure_dir(reports_dir)
    dst = os.path.join(reports_dir, "xor.xml")
    shutil.copy2(src, dst)
    return dst


def _xor_layer_counts(report_path: str | None) -> dict[str, int]:
    if not report_path or not os.path.exists(report_path):
        return {}
    try:
        root = ElementTree.parse(report_path).getroot()
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for item in root.findall(".//item"):
        category = (item.findtext("category") or "").strip().strip("'\"")
        if category:
            counts[category] = counts.get(category, 0) + 1
    return counts


def _blocking_xor_difference_count(total_count: int | None, layer_counts: dict[str, int], nonblocking_layers: set[str] | None = None) -> int | None:
    if not layer_counts:
        return total_count
    ignored = nonblocking_layers or DEFAULT_NONBLOCKING_XOR_LAYERS
    return sum(count for layer, count in layer_counts.items() if layer not in ignored)


def _xor_signoff_status(mode: str | None = None) -> str:
    value = (mode or DEFAULT_XOR_SIGNOFF_MODE or "not_applicable").strip().lower()
    if value in {"blocking", "gate", "gated"}:
        return "enabled"
    return "not_applicable"


def _pick_gds(latest: str | None) -> tuple[str | None, str | None]:
    if not latest:
        return (None, None)

    gds_files = glob.glob(os.path.join(latest, "**", "*.gds"), recursive=True)
    if not gds_files:
        return (None, None)

    klayout_gds = None
    magic_gds = None

    for path in gds_files:
        lp = path.lower()
        if "klayout" in lp and klayout_gds is None:
            klayout_gds = path
        if "magic" in lp and magic_gds is None:
            magic_gds = path

    if klayout_gds is None:
        klayout_gds = gds_files[0]
    if magic_gds is None and len(gds_files) > 1:
        for cand in gds_files:
            if cand != klayout_gds:
                magic_gds = cand
                break

    return (klayout_gds, magic_gds)


def _stage_status(state: dict, stage: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    stage_state = digital.get(stage) if isinstance(digital.get(stage), dict) else {}
    if stage == "drc":
        value = stage_state.get("drc_status") or stage_state.get("status")
    elif stage == "lvs":
        value = stage_state.get("lvs_status") or stage_state.get("status")
    else:
        value = stage_state.get("status")
    return str(value).strip().lower() if value is not None else None


def _stage_is_clean(stage: str, status: str | None) -> bool:
    if stage == "drc":
        return status in {"ok", "clean", "completed"}
    if stage == "lvs":
        return status in {"ok", "clean", "completed"}
    return status == "ok"


def _xor_difference_count(log: str) -> int | None:
    counts = [int(match.group(1)) for match in re.finditer(r"Total XOR differences:\s*(\d+)", log or "", flags=re.IGNORECASE)]
    if counts:
        return counts[-1]
    match = re.search(r"(\d+)\s+XOR differences found", log or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _tapeout_failure_reasons(
    *,
    rc: int,
    log: str,
    drc_status: str | None,
    lvs_status: str | None,
    klayout_gds: str | None,
    magic_gds: str | None,
    blocking_xor_count: int | None = None,
) -> list[str]:
    reasons: list[str] = []
    if rc != 0:
        reasons.append("streamout_command_failed")
    if not _stage_is_clean("drc", drc_status):
        reasons.append("drc_not_clean")
    if not _stage_is_clean("lvs", lvs_status):
        reasons.append("lvs_not_clean")
    xor_count = blocking_xor_count if blocking_xor_count is not None else _xor_difference_count(log)
    if _xor_signoff_status() == "enabled" and xor_count is not None and xor_count > 0:
        reasons.append("xor_differences_found")
    if not (klayout_gds or magic_gds):
        reasons.append("gds_not_produced")
    return reasons


def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:


    # Prefer latest explicit digital-level propagated SDC


    digital = state.get("digital") or {}
    lvs_state = digital.get("lvs") or {}

    cand = lvs_state.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital.lvs -> {cand}")
        return cand

    cand = digital.get("constraints_sdc")

    # Prefer later physical stages first
    for stage in [
        "lvs",
        "drc",
        "fill",
        "route",
        "sta_postroute",
        "sta_postcts",
        "cts",
        "place",
        "floorplan",
        "impl_setup",
        "synth",
    ]:
        cands = sorted(glob.glob(os.path.join(workflow_dir, "digital", stage, "constraints", "*.sdc")))
        for cand in cands:
            if os.path.exists(cand):
                logger.info(f"{AGENT_NAME}: selected SDC from {stage} -> {cand}")
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
    lvs_state = digital.get("lvs") or {}

    cand = lvs_state.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital.lvs -> {cand}")
        return cand

    cand = digital.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital -> {cand}")
        return cand

    for cand in [
        os.path.join(workflow_dir, "digital", "lvs", "config.json"),
        os.path.join(workflow_dir, "digital", "drc", "config.json"),
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

    out, seen = [], set()
    for p in sorted(hits):
        base = os.path.basename(p).lower()
        if base.endswith("_llm_lef_raw.lef") or base.endswith("_raw.lef") or "debug" in base:
            continue
        ap = os.path.abspath(p)
        if ap in seen:
            continue
        seen.add(ap)
        out.append(ap)
    return out


def _stage_macro_inputs(state: dict, workflow_dir: str, work_stage_dir: str):
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

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")
    logger.info(f"🏁 Running {AGENT_NAME}")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "tapeout")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")
    netlist_dir = os.path.join(stage_dir, "netlist")
    gds_dir = os.path.join(stage_dir, "gds")

    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)
    _ensure_dir(netlist_dir)
    _ensure_dir(gds_dir)

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

    upstream_netlists = _select_single_top_netlist(sorted(glob.glob(os.path.join(inputs_netlist_dir, "*.v"))))
    if not upstream_netlists:
        raise RuntimeError("Tapeout: missing run_work/inputs/netlist/*.v (synth/floorplan should populate it).")

    for nl in upstream_netlists:
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

    work_stage_dir = os.path.join(run_work_dir, "tapeout")
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
    input_log_path = os.path.join(logs_dir, "tapeout_input_resolution.log")
    _write_text(input_log_path, input_log)

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
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to KLayout.StreamOut tapeout/config.json'

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {openlane_image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to Magic.StreamOut tapeout/config.json || true'

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {openlane_image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to KLayout.XOR tapeout/config.json || true'
"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir, state=state)

    log_path = os.path.join(logs_dir, "openlane_tapeout.log")
    _write_text(log_path, out)

    latest = _latest_run_dir(work_stage_dir)
    metrics_path = _copy_metrics(latest, stage_dir)
    xor_report_path = _copy_xor_report(latest, stage_dir)
    xor_layer_counts = _xor_layer_counts(xor_report_path)
    xor_total = _xor_difference_count(out)
    raw_blocking_xor_count = _blocking_xor_difference_count(xor_total, xor_layer_counts)
    xor_status = _xor_signoff_status()
    blocking_xor_count = raw_blocking_xor_count if xor_status == "enabled" else None

    kl_src, mg_src = _pick_gds(latest)
    kl_dst = os.path.join(gds_dir, "klayout.gds") if kl_src else None
    mg_dst = os.path.join(gds_dir, "magic.gds") if mg_src else None

    if kl_src and kl_dst:
        shutil.copy2(kl_src, kl_dst)
    if mg_src and mg_dst:
        shutil.copy2(mg_src, mg_dst)

    drc_status = _stage_status(state, "drc")
    lvs_status = _stage_status(state, "lvs")
    failure_reasons = _tapeout_failure_reasons(
        rc=rc,
        log=out,
        drc_status=drc_status,
        lvs_status=lvs_status,
        klayout_gds=kl_dst,
        magic_gds=mg_dst,
        blocking_xor_count=blocking_xor_count,
    )

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if not failure_reasons else "failed",
        "return_code": rc,
        "failure_reasons": failure_reasons,
        "signoff_inputs": {
            "drc_status": drc_status,
            "lvs_status": lvs_status,
            "xor_status": xor_status,
            "xor_differences": blocking_xor_count,
            "xor_differences_observed": raw_blocking_xor_count,
            "xor_differences_total": xor_total,
            "xor_layer_counts": xor_layer_counts,
            "xor_nonblocking_layers": sorted(DEFAULT_NONBLOCKING_XOR_LAYERS),
            "gds_produced": bool(kl_dst or mg_dst),
        },
        "outputs": {
            "sdc": f"digital/tapeout/constraints/{sdc_basename}",
            "metrics_json": "digital/tapeout/metrics.json" if metrics_path else None,
            "xor_report_xml": "digital/tapeout/reports/xor.xml" if xor_report_path else None,
            "klayout_gds": "digital/tapeout/gds/klayout.gds" if kl_dst else None,
            "magic_gds": "digital/tapeout/gds/magic.gds" if mg_dst else None,
            "log": "digital/tapeout/logs/openlane_tapeout.log",
            "input_resolution_log": "digital/tapeout/logs/tapeout_input_resolution.log",
            "openlane_run_dir": latest,
        },
    }

    _write_text(os.path.join(stage_dir, "tapeout_summary.json"), json.dumps(summary, indent=2))
    _write_text(
        os.path.join(stage_dir, "tapeout_summary.md"),
        f"# Tapeout\n\n- status: `{summary['status']}` (rc={rc})\n"
    )

    try:
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/config.json", json.dumps(cfg, indent=2)
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", f"tapeout/constraints/{sdc_basename}", sdc_text
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/run.sh", run_sh
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/logs/openlane_tapeout.log", out
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/logs/tapeout_input_resolution.log", input_log
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/tapeout_summary.json", json.dumps(summary, indent=2)
        )

        if metrics_path and os.path.exists(metrics_path):
            with open(metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id, AGENT_NAME, "digital", "tapeout/metrics.json", f.read()
                )
        if xor_report_path and os.path.exists(xor_report_path):
            with open(xor_report_path, "r", encoding="utf-8", errors="ignore") as f:
                save_text_artifact_and_record(
                    workflow_id, AGENT_NAME, "digital", "tapeout/reports/xor.xml", f.read()
                )
        # GDS is binary; keep local and record path in summary/state
    except Exception as e:
        print(f"⚠️ {AGENT_NAME} upload failed: {e}")

    state.setdefault("digital", {})["tapeout"] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": config_path,
        "input_resolution_log": input_log_path,
        "gds_klayout": kl_dst,
        "gds_magic": mg_dst,
        "openlane_run_dir": latest,
    }

    return state
