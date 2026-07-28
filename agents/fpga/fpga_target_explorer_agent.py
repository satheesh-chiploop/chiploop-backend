import json
import os
import statistics
from copy import deepcopy

from .fpga_common import BOARD_REGISTRY, fpga_dir, publish_json, read_text, run_cmd, write_text
from .fpga_nextpnr_place_route_agent import (
    _nextpnr_effort_policy,
    _nextpnr_help,
    _nextpnr_version,
    _parse_nextpnr,
    _parse_nextpnr_report,
)
from .fpga_yosys_synthesis_agent import _yosys_help, _yosys_version


CANDIDATE_BOARDS = [
    "icestick",
    "icebreaker",
    "upduino_v3",
    "ice40_hx8k_breakout",
    "colorlight_5a_75b",
    "ulx3s_ecp5_45f",
    "orangecrab_ecp5_85f",
]
PROFILE_KEYS = {"best_overall", "best_performance", "best_low_cost", "best_for_growth"}


def _progress(state: dict, message: str) -> None:
    callback = state.get("_progress_callback")
    if callable(callback):
        try:
            callback(message)
        except Exception:
            pass


def _record_file(state: dict, board_key: str, stage: str, path: str | None) -> None:
    if not path or not os.path.exists(path):
        return
    workflow_id = str(state.get("workflow_id") or "")
    if not workflow_id:
        return
    try:
        from utils.artifact_utils import save_text_artifact_and_record

        save_text_artifact_and_record(
            workflow_id,
            "FPGA Target Explorer Agent",
            f"fpga/target_explorer/{board_key}/{stage}".rstrip("/"),
            os.path.basename(path),
            read_text(path),
        )
    except Exception:
        pass


def _implementation_key(board: dict) -> str:
    return ":".join(str(board.get(key) or "") for key in ("family", "device", "package"))


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _synthesis_options(strategy: str, help_text: str) -> list[str]:
    if strategy == "closure_retime" and "-noabc9" in help_text and "-retime" in help_text:
        return ["-noabc9", "-retime"]
    if strategy == "closure_flatten" and "-flatten" in help_text:
        return ["-flatten"]
    if strategy == "baseline" and "-noflatten" in help_text:
        return ["-noflatten"]
    return []


def _run_synthesis(state: dict, board_key: str, board: dict, strategy: str) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    rtl_files = [str(path) for path in fpga.get("rtl_files") or []]
    top = str(fpga.get("top_module") or state.get("top_module") or "top")
    family = str(board.get("family") or "ice40").lower()
    synth_cmd = "synth_ecp5" if family == "ecp5" else "synth_ice40"
    out_dir = fpga_dir(state, "target_explorer", board_key, strategy, "synth")
    netlist = os.path.abspath(os.path.join(out_dir, f"{top}_{family}.json"))
    script_path = os.path.abspath(os.path.join(out_dir, "synth.ys"))
    log_path = os.path.abspath(os.path.join(out_dir, "yosys.log"))
    help_text = _yosys_help(synth_cmd)
    options = _synthesis_options(strategy, help_text)
    steps = [f"read_verilog -sv {path}" for path in rtl_files]
    option_text = " ".join(options)
    steps.append(f"{synth_cmd} -top {top} {option_text} -json {netlist}".replace("  ", " "))
    write_text(script_path, "\n".join(steps) + "\n")
    result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=900)
    for artifact in (script_path, log_path, netlist if os.path.exists(netlist) else None):
        _record_file(state, board_key, f"{strategy}/synth", artifact)
    return {
        "status": "completed" if result.get("ok") and os.path.exists(netlist) else "failed",
        "strategy": strategy,
        "netlist": netlist if os.path.exists(netlist) else None,
        "script": script_path,
        "log": log_path,
        "command": result.get("cmd"),
        "effective_options": options,
        "tool_version": _yosys_version(),
        "error": None if result.get("ok") else result.get("stderr_tail") or result.get("stdout_tail"),
    }


def _run_pnr(state: dict, board_key: str, board: dict, synthesis: dict, seed: int, effort: str) -> dict:
    family = str(board.get("family") or "ice40").lower()
    tool = str(board.get("nextpnr_tool") or ("nextpnr-ecp5" if family == "ecp5" else "nextpnr-ice40"))
    out_dir = fpga_dir(state, "target_explorer", board_key, synthesis.get("strategy") or "baseline", f"seed_{seed}")
    routed_ext = ".config" if family == "ecp5" else ".asc"
    routed = os.path.abspath(os.path.join(out_dir, f"routed{routed_ext}"))
    report = os.path.abspath(os.path.join(out_dir, "nextpnr_report.json"))
    log = os.path.abspath(os.path.join(out_dir, "nextpnr.log"))
    help_text = _nextpnr_help(tool)
    policy_state = {"fpga_closure_mode": effort, "target_frequency_mhz": state.get("target_frequency_mhz")}
    policy = _nextpnr_effort_policy(policy_state, tool, help_text)
    cmd = [
        tool,
        str(board.get("nextpnr_device_flag")),
        "--package", str(board.get("nextpnr_package") or board.get("package")),
        "--json", str(synthesis.get("netlist")),
        "--report", report,
    ]
    cmd.extend(["--textcfg", routed] if family == "ecp5" else ["--asc", routed])
    unconstrained_flag = "--lpf-allow-unconstrained" if family == "ecp5" else "--pcf-allow-unconstrained"
    if unconstrained_flag in help_text:
        cmd.append(unconstrained_flag)
    if "--timing-allow-fail" in help_text:
        cmd.append("--timing-allow-fail")
    cmd.extend(policy.get("effective_args") or [])
    cmd.extend(["--seed", str(seed)])
    result = run_cmd(cmd, cwd=out_dir, log_path=log, timeout=1200)
    metrics = _parse_nextpnr(log)
    metrics.update(_parse_nextpnr_report(report, board))
    produced = os.path.exists(routed)
    for artifact in (log, report if os.path.exists(report) else None, routed if produced else None):
        _record_file(state, board_key, f"{synthesis.get('strategy') or 'baseline'}/seed_{seed}", artifact)
    return {
        "seed": seed,
        "effort": effort,
        "status": "completed" if produced else "failed",
        "timing_met": metrics.get("timing_met"),
        "max_frequency_mhz": metrics.get("max_frequency_mhz"),
        "logic_cells_used": metrics.get("logical_cells_used"),
        "logic_cells_available": metrics.get("logical_cells_available") or ((board.get("resources") or {}).get("logic_cells")),
        "logic_utilization_percent": metrics.get("logic_utilization_percent"),
        "routed_lut4_cells": metrics.get("routed_lut4_cells"),
        "routed_flip_flops": metrics.get("routed_flip_flops"),
        "routed_output": routed if produced else None,
        "report": report if os.path.exists(report) else None,
        "log": log,
        "command": result.get("cmd"),
        "effective_args": policy.get("effective_args") or [],
        "tool_version": _nextpnr_version(tool),
        "error": None if produced else result.get("stderr_tail") or result.get("stdout_tail"),
    }


def _summarize_board(board_key: str, board: dict, synthesis_runs: list[dict], pnr_runs: list[dict], target: float) -> dict:
    completed = [run for run in pnr_runs if run.get("status") == "completed" and _num(run.get("max_frequency_mhz")) > 0]
    frequencies = [_num(run.get("max_frequency_mhz")) for run in completed]
    best = max(completed, key=lambda run: _num(run.get("max_frequency_mhz"))) if completed else {}
    met_runs = [run for run in completed if _num(run.get("max_frequency_mhz")) >= target or run.get("timing_met") is True]
    available = int(best.get("logic_cells_available") or ((board.get("resources") or {}).get("logic_cells")) or 0)
    used = int(best.get("logic_cells_used") or best.get("routed_lut4_cells") or 0)
    utilization = best.get("logic_utilization_percent")
    if utilization is None and available:
        utilization = round((used / available) * 100.0, 3)
    best_fmax = max(frequencies) if frequencies else None
    relaxed = round(best_fmax * 0.9, 3) if best_fmax and not met_runs else None
    return {
        "board": board_key,
        "label": board.get("label") or board_key,
        "family": board.get("family"),
        "device": board.get("device"),
        "package": board.get("package"),
        "implementation_key": _implementation_key(board),
        "board_input_frequency_mhz": board.get("default_frequency_mhz"),
        "target_frequency_mhz": target,
        "status": "target_met" if met_runs else "target_missed" if completed else "implementation_failed",
        "target_met": bool(met_runs),
        "best_frequency_mhz": best_fmax,
        "median_frequency_mhz": round(statistics.median(frequencies), 3) if frequencies else None,
        "worst_frequency_mhz": min(frequencies) if frequencies else None,
        "timing_pass_rate": round(len(met_runs) / len(completed), 3) if completed else 0.0,
        "winning_seed": best.get("seed"),
        "logic_cells_used": used or None,
        "logic_cells_available": available or None,
        "logic_utilization_percent": utilization,
        "resource_headroom_percent": round(100.0 - _num(utilization), 3) if utilization is not None else None,
        "closure_used": len(synthesis_runs) > 1 or any(run.get("effort") == "advanced" for run in pnr_runs),
        "frequency_relaxation": {"eligible": bool(relaxed), "recommended_mhz": relaxed, "reason": "reported only after target closure failed" if relaxed else None},
        "constraint_scope": "capacity_and_timing_exploration; board pin compatibility must be confirmed in FPGA Prototyping",
        "synthesis_runs": synthesis_runs,
        "pnr_runs": pnr_runs,
        "winning_run": best or None,
    }


def _recommend(results: list[dict]) -> dict:
    viable = [item for item in results if item.get("target_met")]
    pool = viable or [item for item in results if _num(item.get("best_frequency_mhz")) > 0]
    if not pool:
        return {key: None for key in PROFILE_KEYS}
    performance = max(pool, key=lambda item: (_num(item.get("median_frequency_mhz")), _num(item.get("best_frequency_mhz"))))
    growth = max(pool, key=lambda item: (_num(item.get("resource_headroom_percent")), _num(item.get("logic_cells_available"))))
    low_cost = min(pool, key=lambda item: (_num(item.get("logic_cells_available"), 1e12), -_num(item.get("median_frequency_mhz"))))
    overall = max(
        pool,
        key=lambda item: (
            1 if item.get("target_met") else 0,
            _num(item.get("timing_pass_rate")),
            min(_num(item.get("resource_headroom_percent")), 60.0),
            _num(item.get("median_frequency_mhz")),
            -_num(item.get("logic_cells_available")),
        ),
    )
    return {
        "best_overall": overall.get("board"),
        "best_performance": performance.get("board"),
        "best_low_cost": low_cost.get("board"),
        "best_for_growth": growth.get("board"),
    }


def run_agent(state: dict) -> dict:
    agent = "FPGA Target Explorer Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    rtl_files = fpga.get("rtl_files") or []
    top = fpga.get("top_module") or state.get("top_module")
    if not rtl_files or not top:
        raise RuntimeError("FPGA Target Explorer requires ingested RTL and a top module.")
    target = _num(state.get("target_frequency_mhz"), 75.0)
    if target <= 0:
        raise RuntimeError("Target frequency must be greater than zero.")
    requested_profile = str(state.get("recommendation_profile") or "best_overall").strip().lower()
    if requested_profile not in PROFILE_KEYS:
        requested_profile = "best_overall"
    requested_boards = state.get("candidate_boards") if isinstance(state.get("candidate_boards"), list) else CANDIDATE_BOARDS
    board_keys = list(dict.fromkeys(key for key in requested_boards if key in CANDIDATE_BOARDS and key in BOARD_REGISTRY))
    if not board_keys:
        raise RuntimeError("Select at least one supported FPGA board/device to explore.")
    _progress(state, f"Explorer plan: {len(board_keys)} selected board(s), target {target:g} MHz, baseline seeds 1-3; closure seeds 4-6 only for misses.")
    implementation_cache: dict[str, dict] = {}
    results: list[dict] = []
    for board_index, board_key in enumerate(board_keys, start=1):
        board = deepcopy(BOARD_REGISTRY[board_key])
        _progress(state, f"Board {board_index}/{len(board_keys)}: {board.get('label') or board_key} ({board.get('family')} {board.get('device')}) started.")
        implementation_key = _implementation_key(board)
        if implementation_key in implementation_cache:
            reused = deepcopy(implementation_cache[implementation_key])
            reused.update({
                "board": board_key,
                "label": board.get("label") or board_key,
                "board_input_frequency_mhz": board.get("default_frequency_mhz"),
                "reused_implementation_from": implementation_cache[implementation_key].get("board"),
            })
            results.append(reused)
            _progress(state, f"Board {board_index}/{len(board_keys)}: reused identical {implementation_key} implementation from {reused.get('reused_implementation_from')}.")
            continue
        _progress(state, f"{board_key}: baseline synthesis started.")
        baseline = _run_synthesis(state, board_key, board, "baseline")
        _progress(state, f"{board_key}: baseline synthesis {baseline.get('status')}.")
        synthesis_runs = [baseline]
        pnr_runs: list[dict] = []
        if baseline.get("status") == "completed":
            for seed in (1, 2, 3):
                _progress(state, f"{board_key}: baseline P&R seed {seed}/3 started.")
                run = _run_pnr(state, board_key, board, baseline, seed, "balanced")
                pnr_runs.append(run)
                fmax = run.get("max_frequency_mhz")
                detail = f", Fmax {float(fmax):.3f} MHz" if fmax is not None else ""
                _progress(state, f"{board_key}: baseline seed {seed} {run.get('status')}{detail}.")
        routed_baseline = [run for run in pnr_runs if run.get("status") == "completed"]
        met = any(_num(run.get("max_frequency_mhz")) >= target or run.get("timing_met") is True for run in routed_baseline)
        if not met and routed_baseline:
            _progress(state, f"{board_key}: target missed after baseline; starting synthesis/P&R closure.")
            help_text = _yosys_help("synth_ecp5" if board.get("family") == "ecp5" else "synth_ice40")
            closure_strategy = "closure_retime" if "-noabc9" in help_text and "-retime" in help_text else "closure_flatten"
            _progress(state, f"{board_key}: {closure_strategy} synthesis started.")
            closure_synth = _run_synthesis(state, board_key, board, closure_strategy)
            synthesis_runs.append(closure_synth)
            _progress(state, f"{board_key}: {closure_strategy} synthesis {closure_synth.get('status')}.")
            if closure_synth.get("status") == "completed":
                for seed in (4, 5, 6):
                    _progress(state, f"{board_key}: closure P&R seed {seed - 3}/3 (seed {seed}) started.")
                    run = _run_pnr(state, board_key, board, closure_synth, seed, "advanced")
                    pnr_runs.append(run)
                    fmax = run.get("max_frequency_mhz")
                    detail = f", Fmax {float(fmax):.3f} MHz" if fmax is not None else ""
                    _progress(state, f"{board_key}: closure seed {seed} {run.get('status')}{detail}.")
        elif not routed_baseline and baseline.get("status") == "completed":
            _progress(state, f"{board_key}: no baseline route completed; closure seeds skipped because capacity/I/O/tool failures are not timing failures.")
        summary = _summarize_board(board_key, board, synthesis_runs, pnr_runs, target)
        implementation_cache[implementation_key] = deepcopy(summary)
        results.append(summary)
        outcome = "target met" if summary.get("target_met") else summary.get("status")
        best = summary.get("best_frequency_mhz")
        best_text = f" at {float(best):.3f} MHz" if best is not None else ""
        _progress(state, f"Board {board_index}/{len(board_keys)}: {board_key} {outcome}{best_text}; winning seed {summary.get('winning_seed') or 'n/a'}.")
    recommendations = _recommend(results)
    selected_board = recommendations.get(requested_profile)
    summary = {
        "type": "fpga_target_explorer",
        "status": "completed" if results else "failed",
        "top_module": top,
        "rtl_file_count": len(rtl_files),
        "design_intent_provided": bool(str(state.get("spec_text") or state.get("spec") or "").strip()),
        "design_intent": str(state.get("spec_text") or state.get("spec") or "").strip() or None,
        "target_frequency_mhz": target,
        "requested_profile": requested_profile,
        "selected_recommendation": selected_board,
        "recommendations": recommendations,
        "recommendation_policy": {
            "best_overall": "target met, timing stability, useful headroom, performance, then smallest viable target",
            "best_performance": "highest robust median Fmax",
            "best_low_cost": "smallest viable FPGA capacity proxy; live board pricing is not assumed",
            "best_for_growth": "largest remaining logic headroom",
        },
        "results": results,
        "candidate_count": len(results),
        "unique_implementation_count": len(implementation_cache),
        "frequency_relaxation_policy": "reported only for candidates that fail the requested target after closure",
        "continuation": {
            "app": "fpga-bitstream",
            "label": "Continue to FPGA Prototyping",
            "selected_board": selected_board,
            "target_frequency_mhz": target,
            "source_workflow_id": state.get("workflow_id"),
        },
    }
    _progress(state, f"Exploration complete: {len(results)} board result(s), {len(implementation_cache)} unique implementation(s); {requested_profile} recommends {selected_board or 'no viable target'}.")
    publish_json(state, agent, "target_explorer", "fpga_target_explorer.json", summary)
    state["fpga_target_explorer"] = summary
    return state
