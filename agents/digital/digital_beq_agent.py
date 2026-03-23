import os
import json
import glob
import shutil
import subprocess
import re
import logging
from datetime import datetime

logger = logging.getLogger("chiploop")

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital BEQ Agent"
STAGE_NAME = "beq"

DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _run(cmd: list[str], cwd: str) -> tuple[int, str]:
    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out, _ = p.communicate()
    return p.returncode, out


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(", txt, flags=re.MULTILINE)
    return m.group(1) if m else None


def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    for key in ["openlane_config", "config_json"]:
        cand = digital.get(key)
        if cand and os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config from state.digital[{key}] -> {cand}")
            return cand

    for cand in [
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


def _resolve_rtl_files(workflow_dir: str) -> list[str]:
    candidates = []

    # Primary location from your RTL agent
    candidates += sorted(glob.glob(os.path.join(workflow_dir, "rtl", "*.v")))
    candidates += sorted(glob.glob(os.path.join(workflow_dir, "rtl", "*.sv")))

    # Optional fallback
    candidates += sorted(glob.glob(os.path.join(workflow_dir, "digital", "rtl", "*.v")))
    candidates += sorted(glob.glob(os.path.join(workflow_dir, "digital", "rtl", "*.sv")))

    # De-dupe preserving order
    out = []
    seen = set()
    for p in candidates:
        if p not in seen and os.path.exists(p):
            seen.add(p)
            out.append(p)
    return out


def _pick_stage_netlist(latest_run: str | None) -> str | None:
    if not latest_run:
        return None

    patterns = [
        os.path.join(latest_run, "final", "nl", "*.nl.v"),
        os.path.join(latest_run, "final", "nl", "*.v"),
        os.path.join(latest_run, "final", "pnl", "*.pnl.v"),
        os.path.join(latest_run, "final", "pnl", "*.v"),
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def _resolve_synth_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    synth_state = digital.get("synth") or {}

    candidates = [
        synth_state.get("netlist"),
        synth_state.get("final_netlist"),
        synth_state.get("synth_netlist"),
    ]

    synth_dir = os.path.join(workflow_dir, "digital", "synth", "netlist")
    candidates += sorted(glob.glob(os.path.join(synth_dir, "*_synth.v")))
    candidates += [
        p for p in sorted(glob.glob(os.path.join(synth_dir, "*.v")))
        if not p.endswith(".nl.v") and not p.endswith(".pnl.v")
    ]

    return _first_existing(candidates)


def _resolve_cts_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    cts_state = digital.get("cts") or {}

    candidates = [
        cts_state.get("netlist"),
        cts_state.get("final_netlist"),
        cts_state.get("cts_netlist"),
    ]

    picked = _pick_stage_netlist(cts_state.get("openlane_run_dir"))
    if picked:
        candidates.append(picked)

    candidates += sorted(glob.glob(os.path.join(workflow_dir, "digital", "cts", "netlist", "*.v")))
    return _first_existing(candidates)


def _resolve_route_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    route_state = digital.get("route") or {}

    candidates = [
        route_state.get("netlist"),
        route_state.get("final_netlist"),
        route_state.get("route_netlist"),
    ]

    picked = _pick_stage_netlist(route_state.get("openlane_run_dir"))
    if picked:
        candidates.append(picked)

    candidates += sorted(glob.glob(os.path.join(workflow_dir, "digital", "route", "netlist", "*.v")))
    return _first_existing(candidates)


def _resolve_beq_target_netlist(state: dict, workflow_dir: str) -> tuple[str | None, str]:
    requested = str(state.get("beq_target_stage", "")).strip().lower()

    if requested == "synth":
        return _resolve_synth_netlist(state, workflow_dir), "synth"
    if requested == "cts":
        return _resolve_cts_netlist(state, workflow_dir), "cts"
    if requested == "route":
        return _resolve_route_netlist(state, workflow_dir), "route"

    # Default preference order
    route_netlist = _resolve_route_netlist(state, workflow_dir)
    if route_netlist:
        return route_netlist, "route"

    cts_netlist = _resolve_cts_netlist(state, workflow_dir)
    if cts_netlist:
        return cts_netlist, "cts"

    synth_netlist = _resolve_synth_netlist(state, workflow_dir)
    if synth_netlist:
        return synth_netlist, "synth"

    return None, "unknown"


def _resolve_top_module(state: dict, cfg: dict, rtl_files: list[str], gate_netlist: str | None) -> str:
    cfg_top = str(cfg.get("DESIGN_NAME", "")).strip()
    if cfg_top and cfg_top != "top":
        return cfg_top

    state_top = str(state.get("design_name", "")).strip()
    if state_top and state_top != "top":
        return state_top

    for p in rtl_files:
        inferred = _infer_top_from_netlist(p)
        if inferred:
            return inferred

    if gate_netlist:
        inferred = _infer_top_from_netlist(gate_netlist)
        if inferred:
            return inferred

    return "top"


def _resolve_stdcell_model(pdk_root_host: str) -> str | None:
    patterns = [
        os.path.join(pdk_root_host, "**", "cells_sim.v"),
        os.path.join(pdk_root_host, "**", "sky130_fd_sc_hd.v"),
        os.path.join(pdk_root_host, "**", "sky130_fd_sc_hd__*.v"),
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            return hits[0]
    return None


def _resolve_liberty(pdk_root_host: str) -> str | None:
    patterns = [
        os.path.join(pdk_root_host, "**", "sky130_fd_sc_hd__tt_025C_1v80.lib"),
        os.path.join(pdk_root_host, "**", "*.lib"),
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            return hits[0]
    return None


def _copy_stage_inputs(stage_dir: str, rtl_files: list[str], gate_netlist: str, stdcell_model: str | None, liberty: str | None) -> tuple[list[str], str, str | None, str | None]:
    rtl_stage_dir = os.path.join(stage_dir, "rtl")
    netlist_stage_dir = os.path.join(stage_dir, "netlist")
    lib_stage_dir = os.path.join(stage_dir, "lib")

    _ensure_dir(rtl_stage_dir)
    _ensure_dir(netlist_stage_dir)
    _ensure_dir(lib_stage_dir)

    staged_rtl = []
    for src in rtl_files:
        dst = os.path.join(rtl_stage_dir, os.path.basename(src))
        shutil.copy2(src, dst)
        staged_rtl.append(dst)

    staged_gate = os.path.join(netlist_stage_dir, os.path.basename(gate_netlist))
    shutil.copy2(gate_netlist, staged_gate)

    staged_stdcell = None
    if stdcell_model and os.path.exists(stdcell_model):
        staged_stdcell = os.path.join(lib_stage_dir, os.path.basename(stdcell_model))
        shutil.copy2(stdcell_model, staged_stdcell)

    staged_liberty = None
    if liberty and os.path.exists(liberty):
        staged_liberty = os.path.join(lib_stage_dir, os.path.basename(liberty))
        shutil.copy2(liberty, staged_liberty)

    return staged_rtl, staged_gate, staged_stdcell, staged_liberty


def _write_yosys_script(stage_dir: str, top_module: str, staged_rtl: list[str], staged_gate: str, staged_stdcell: str | None, staged_liberty: str | None) -> str:
    rtl_read_lines = []
    for p in staged_rtl:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".sv":
            rtl_read_lines.append(f'read_verilog -sv "digital/beq/rtl/{os.path.basename(p)}"')
        else:
            rtl_read_lines.append(f'read_verilog "digital/beq/rtl/{os.path.basename(p)}"')

    lib_lines = []
    if staged_liberty:
        lib_lines.append(f'read_liberty -lib "digital/beq/lib/{os.path.basename(staged_liberty)}"')
    if staged_stdcell:
        lib_lines.append(f'read_verilog -lib "digital/beq/lib/{os.path.basename(staged_stdcell)}"')

    ys = f"""# Auto-generated by {AGENT_NAME}

# -------- GOLD (RTL) --------
{"\n".join(rtl_read_lines)}
hierarchy -check -top {top_module}
proc
opt
memory
opt
flatten
opt_clean -purge
rename {top_module} gold

# -------- GATE --------
{"\n".join(lib_lines)}
read_verilog "digital/beq/netlist/{os.path.basename(staged_gate)}"
hierarchy -check -top {top_module}
flatten
opt_clean -purge
rename {top_module} gate

# -------- EQUIVALENCE --------
equiv_make gold gate equiv
hierarchy -top equiv
opt_clean -purge
equiv_simple -undef
equiv_status -assert
"""
    ys_path = os.path.join(stage_dir, "beq.ys")
    _write_text(ys_path, ys)
    return ys_path


def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}.")
    logger.info(f"🏁 Running {AGENT_NAME}")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", STAGE_NAME)
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)

    rtl_files = _resolve_rtl_files(workflow_dir)
    if not rtl_files:
        raise RuntimeError("BEQ: no RTL files found under workflow_dir/rtl or workflow_dir/digital/rtl")

    target_netlist, target_stage = _resolve_beq_target_netlist(state, workflow_dir)
    if not target_netlist:
        raise RuntimeError("BEQ: no target netlist found from route/cts/synth stages")

    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    cfg = _read_json(base_cfg_path) if base_cfg_path else {}

    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host

    stdcell_model = _resolve_stdcell_model(pdk_root_host)
    liberty = _resolve_liberty(pdk_root_host)

    staged_rtl, staged_gate, staged_stdcell, staged_liberty = _copy_stage_inputs(
        stage_dir=stage_dir,
        rtl_files=rtl_files,
        gate_netlist=target_netlist,
        stdcell_model=stdcell_model,
        liberty=liberty,
    )

    top_module = _resolve_top_module(state, cfg, staged_rtl, staged_gate)
    state["design_name"] = top_module

    ys_path = _write_yosys_script(
        stage_dir=stage_dir,
        top_module=top_module,
        staged_rtl=staged_rtl,
        staged_gate=staged_gate,
        staged_stdcell=staged_stdcell,
        staged_liberty=staged_liberty,
    )

    image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE

    input_log = "\n".join([
        f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={workflow_dir}",
        f"top_module={top_module}",
        f"target_stage={target_stage}",
        f"target_netlist={target_netlist}",
        f"rtl_count={len(staged_rtl)}",
        f"rtl_files={','.join(os.path.basename(p) for p in staged_rtl)}",
        f"stdcell_model={staged_stdcell or ''}",
        f"liberty={staged_liberty or ''}",
        f"yosys_script={ys_path}",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "beq_input_resolution.log"), input_log)

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "OPENLANE_IMAGE={image}"
echo "WORKFLOW_DIR=/workdir"
echo "TOP_MODULE={top_module}"
echo "TARGET_STAGE={target_stage}"

docker run --rm \\
  -v "{workflow_dir}":/workdir \\
  -v "{pdk_root_host}":/pdk \\
  -e PDK_ROOT=/pdk \\
  {image} \\
  bash -lc 'set -e; cd /workdir && yosys -s digital/beq/beq.ys'
"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir)
    log_path = os.path.join(logs_dir, "openlane_beq.log")
    _write_text(log_path, out)

    passed = (rc == 0) and ("Equivalence successfully proven" in out or "Found 0 unproven $equiv cells" in out or "Equivalence successfully proven!" in out)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if passed else "failed",
        "return_code": rc,
        "top_module": top_module,
        "target_stage": target_stage,
        "outputs": {
            "log": f"digital/{STAGE_NAME}/logs/openlane_beq.log",
            "yosys_script": f"digital/{STAGE_NAME}/beq.ys",
            "gate_netlist": f"digital/{STAGE_NAME}/netlist/{os.path.basename(staged_gate)}",
        },
    }

    summary_json_path = os.path.join(stage_dir, "beq_summary.json")
    summary_md_path = os.path.join(stage_dir, "beq_summary.md")

    _write_text(summary_json_path, json.dumps(summary, indent=2))
    _write_text(
        summary_md_path,
        "\n".join([
            "# Digital BEQ",
            "",
            f"- status: `{summary['status']}` (rc={rc})",
            f"- top_module: `{top_module}`",
            f"- target_stage: `{target_stage}`",
            f"- gate_netlist: `{os.path.basename(staged_gate)}`",
        ]) + "\n",
    )

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/beq_input_resolution.log", input_log)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/beq.ys", open(ys_path, "r", encoding="utf-8").read())
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/logs/openlane_beq.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"{STAGE_NAME}/beq_summary.json", json.dumps(summary, indent=2))

        if os.path.exists(staged_gate):
            save_text_artifact_and_record(
                workflow_id,
                AGENT_NAME,
                "digital",
                f"{STAGE_NAME}/netlist/{os.path.basename(staged_gate)}",
                open(staged_gate, "r", encoding="utf-8", errors="ignore").read(),
            )
    except Exception as e:
        logger.exception(f"{AGENT_NAME}: artifact upload failed: {e}")

    state.setdefault("digital", {})[STAGE_NAME] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "top_module": top_module,
        "target_stage": target_stage,
        "target_netlist": staged_gate,
        "rtl_files": staged_rtl,
        "yosys_script": ys_path,
        "log": log_path,
        "summary_json": summary_json_path,
        "summary_md": summary_md_path,
    }

    return state