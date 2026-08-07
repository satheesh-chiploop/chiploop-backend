import json
import os
import re
import statistics
import threading
import time
from copy import deepcopy

from .fpga_common import BOARD_REGISTRY, fpga_dir, publish_json, read_text, run_cmd, write_text
from .fpga_nextpnr_place_route_agent import (
    _nextpnr_effort_policy,
    _nextpnr_help,
    _himbaechel_uarch_args,
    _nextpnr_version,
    _parse_nextpnr,
    _parse_nextpnr_report,
)
from .fpga_yosys_synthesis_agent import (
    _architecture_synth_options,
    _rtl_memory_intent,
    _yosys_cell_metrics,
    _yosys_help,
    _yosys_version,
)


CANDIDATE_BOARDS = [
    "icestick",
    "icebreaker",
    "upduino_v3",
    "ice40_hx8k_breakout",
    "colorlight_5a_75b",
    "ulx3s_ecp5_45f",
    "orangecrab_ecp5_85f",
    "certus_nx_versa_40",
    "crosslink_nx_eval_40",
    "certuspro_nx_versa_100",
    "gowin_tang_nano_9k",
    "gowin_tang_nano_20k",
    "gowin_tang_primer_20k",
    "gowin_gw5a_25_starter",
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


def _make_core_only_netlist(netlist: str, top: str) -> list[str]:
    """Remove top-level port declarations after synthesis for core-only exploration."""
    try:
        with open(netlist, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        modules = payload.get("modules") if isinstance(payload, dict) else None
        module = modules.get(top) if isinstance(modules, dict) else None
        if not isinstance(module, dict) or not isinstance(module.get("ports"), dict):
            return []
        stripped = list(module["ports"])
        module["ports"] = {}
        with open(netlist, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return stripped
    except (OSError, ValueError, TypeError):
        return []


def _run_synthesis(state: dict, board_key: str, board: dict, strategy: str) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    rtl_files = [str(path) for path in fpga.get("rtl_files") or []]
    top = str(fpga.get("top_module") or state.get("top_module") or "top")
    family = str(board.get("family") or "ice40").lower()
    synth_cmd = {
        "ecp5": "synth_ecp5",
        "nexus": "synth_nexus",
        "gowin": "synth_gowin",
    }.get(family, "synth_ice40")
    out_dir = fpga_dir(state, "target_explorer", board_key, strategy, "synth")
    netlist = os.path.abspath(os.path.join(out_dir, f"{top}_{family}.json"))
    script_path = os.path.abspath(os.path.join(out_dir, "synth.ys"))
    log_path = os.path.abspath(os.path.join(out_dir, "yosys.log"))
    help_text = _yosys_help(synth_cmd)
    options = _architecture_synth_options(board, help_text) + _synthesis_options(strategy, help_text)
    if family == "gowin" and "-noiopads" in help_text and "-noiopads" not in options:
        options.append("-noiopads")
    elif family == "nexus" and "-noiopad" in help_text and "-noiopad" not in options:
        options.append("-noiopad")
    steps = [f"read_verilog -sv {path}" for path in rtl_files]
    option_text = " ".join(options)
    steps.append(f"{synth_cmd} -top {top} {option_text} -json {netlist}".replace("  ", " "))
    write_text(script_path, "\n".join(steps) + "\n")
    result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=900, state=state)
    core_only_ports = _make_core_only_netlist(netlist, top) if family in {"gowin", "nexus"} and result.get("ok") and os.path.exists(netlist) else []
    for artifact in (script_path, log_path, netlist if os.path.exists(netlist) else None):
        _record_file(state, board_key, f"{strategy}/synth", artifact)
    completed = bool(result.get("ok") and os.path.exists(netlist))
    metrics = _yosys_cell_metrics(netlist, board) if completed else {}
    memory_intent = _rtl_memory_intent(
        rtl_files,
        max(1, int(state.get("fpga_block_memory_threshold_bits") or 4096)),
    )
    native_ram_required = bool(memory_intent.get("requires_block_ram"))
    native_ram_supported = bool(((board.get("resources") or {}).get("block_ram_primitive")))
    native_ram_mapped = int(metrics.get("block_ram_blocks_used") or 0) > 0
    gate_enforced = native_ram_required and native_ram_supported
    gate_passed = not gate_enforced or native_ram_mapped
    status = "completed" if completed and gate_passed else "failed"
    mapping_error = None
    if completed and not gate_passed:
        mapping_error = (
            "Substantial RTL memory did not map to native "
            f"{metrics.get('block_ram_primitive') or 'block RAM'} on this candidate."
        )
    return {
        **metrics,
        "status": status,
        "strategy": strategy,
        "netlist": netlist if os.path.exists(netlist) else None,
        "script": script_path,
        "log": log_path,
        "command": result.get("cmd"),
        "effective_options": options,
        "core_only_ports_removed": core_only_ports,
        "tool_version": _yosys_version(),
        "memory_intent": memory_intent,
        "memory_mapping_gate": {
            "status": "pass" if gate_passed else "fail",
            "enforced": gate_enforced,
            "required": native_ram_required,
            "supported": native_ram_supported,
            "mapped": native_ram_mapped,
            "primitive": metrics.get("block_ram_primitive"),
        },
        "error": mapping_error or (None if result.get("ok") else result.get("stderr_tail") or result.get("stdout_tail")),
    }


def _run_pnr(state: dict, board_key: str, board: dict, synthesis: dict, seed: int, effort: str) -> dict:
    family = str(board.get("family") or "ice40").lower()
    tool = str(board.get("nextpnr_tool") or ("nextpnr-ecp5" if family == "ecp5" else "nextpnr-ice40"))
    out_dir = fpga_dir(state, "target_explorer", board_key, synthesis.get("strategy") or "baseline", f"seed_{seed}")
    routed_ext = str(board.get("pnr_output_ext") or (".config" if family == "ecp5" else ".asc"))
    routed = os.path.abspath(os.path.join(out_dir, f"routed{routed_ext}"))
    report = os.path.abspath(os.path.join(out_dir, "nextpnr_report.json"))
    log = os.path.abspath(os.path.join(out_dir, "nextpnr.log"))
    help_text = _nextpnr_help(tool)
    policy_state = {"fpga_closure_mode": effort, "target_frequency_mhz": state.get("target_frequency_mhz")}
    policy = _nextpnr_effort_policy(policy_state, tool, help_text)
    if family in {"nexus", "gowin"}:
        cmd = [tool]
        cmd.extend(_himbaechel_uarch_args(tool, family, help_text))
        cmd.extend(str(arg) for arg in (board.get("nextpnr_device_args") or []))
        cmd.extend(["--json", str(synthesis.get("netlist")), "--report", report])
        cmd.extend(["--fasm", routed] if family == "nexus" else ["--write", routed])
    else:
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
    timeout_seconds = 1200
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        started = time.monotonic()
        while not heartbeat_stop.wait(60):
            elapsed_minutes = max(1, int((time.monotonic() - started) // 60))
            _progress(
                state,
                f"{board_key}: {effort} P&R seed {seed} still running "
                f"({elapsed_minutes} min elapsed; timeout {timeout_seconds // 60} min).",
            )

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        result = run_cmd(cmd, cwd=out_dir, log_path=log, timeout=timeout_seconds, state=state)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
    metrics = _parse_nextpnr(log)
    metrics.update(_parse_nextpnr_report(report, board))
    produced = os.path.exists(routed)
    for artifact in (log, report if os.path.exists(report) else None, routed if produced else None):
        _record_file(state, board_key, f"{synthesis.get('strategy') or 'baseline'}/seed_{seed}", artifact)
    return {
        "seed": seed,
        "effort": effort,
        "synthesis_strategy": synthesis.get("strategy"),
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


def _capacity_preflight(synthesis: dict, board: dict, io_required: int) -> dict:
    """Reject a candidate before P&R when synthesis proves it cannot fit."""
    resources = board.get("resources") if isinstance(board.get("resources"), dict) else {}
    cell_counts = synthesis.get("cell_type_counts") if isinstance(synthesis.get("cell_type_counts"), dict) else {}
    dsp_used = sum(
        int(_num(count)) for cell, count in cell_counts.items()
        if any(marker in str(cell).upper() for marker in ("MULT18", "DSP", "ALU54"))
    )
    checks = {
        "io_cells": {"required": int(io_required), "available": resources.get("io_cells")},
        "logic_cells": {"required": int(_num(synthesis.get("logical_cells_used"))), "available": resources.get("logic_cells")},
        "block_ram_blocks": {"required": int(_num(synthesis.get("block_ram_blocks_used"))), "available": resources.get("block_ram_blocks")},
        "dsp_blocks": {"required": dsp_used, "available": resources.get("dsp_blocks")},
    }
    failures = []
    for resource, check in checks.items():
        available = check.get("available")
        required = int(check.get("required") or 0)
        check["status"] = "unknown" if available is None else ("pass" if required <= int(available) else "fail")
        if check["status"] == "fail":
            failures.append(f"{resource} requires {required}, board provides {int(available)}")
    return {
        "status": "reject" if failures else "pass",
        "checks": checks,
        "failure_reasons": failures,
        "policy": "P&R runs only when every known synthesized resource requirement fits the device.",
    }


def _summarize_board(board_key: str, board: dict, synthesis_runs: list[dict], pnr_runs: list[dict], target: float, capacity_preflight: dict | None = None) -> dict:
    completed = [run for run in pnr_runs if run.get("status") == "completed" and _num(run.get("max_frequency_mhz")) > 0]
    frequencies = [_num(run.get("max_frequency_mhz")) for run in completed]
    best = max(completed, key=lambda run: _num(run.get("max_frequency_mhz"))) if completed else {}
    met_runs = [run for run in completed if _num(run.get("max_frequency_mhz")) >= target or run.get("timing_met") is True]
    errors = [str(run.get("error") or "") for run in pnr_runs if run.get("status") == "failed"]
    error_text = "\n".join(errors).lower()
    diagnostic = best or max(pnr_runs, key=lambda run: _num(run.get("logic_utilization_percent")), default={})
    available = int(diagnostic.get("logic_cells_available") or ((board.get("resources") or {}).get("logic_cells")) or 0)
    used = int(diagnostic.get("logic_cells_used") or diagnostic.get("routed_lut4_cells") or 0) if diagnostic else 0
    utilization = diagnostic.get("logic_utilization_percent")
    resource_match = re.search(r"(?:ICESTORM_LC|LUT4):\s*(\d+)\s*/\s*(\d+)\s+(\d+(?:\.\d+)?)%", "\n".join(errors), re.IGNORECASE)
    if resource_match and not used:
        used, available = int(resource_match.group(1)), int(resource_match.group(2))
        utilization = round((used / available) * 100.0, 3) if available else None
    if utilization is None and available and used:
        utilization = round((used / available) * 100.0, 3)
    best_fmax = max(frequencies) if frequencies else None
    timing_margin_percent = round(((best_fmax - target) / target) * 100.0, 3) if best_fmax and target else None
    relaxed = round(best_fmax * 0.9, 3) if best_fmax and not met_runs else None
    placement_capacity_error = any(marker in error_text for marker in (
        "unable to find a placement location", "unable to find legal placement",
        "unable to place cell", "no bels remaining", "check constraints and utilisation",
    ))
    capacity_failed = placement_capacity_error and utilization is not None and _num(utilization) > 100
    failure_kind = (
        "capacity_exceeded" if capacity_failed
        else "unconstrained_io" if "unconstrained io:" in error_text
        else "io_packing" if "driven by illegal port" in error_text
        else "implementation_failed" if errors
        else None
    )
    rejected = bool(capacity_preflight and capacity_preflight.get("status") == "reject")
    return {
        "board": board_key,
        "label": board.get("label") or board_key,
        "family": board.get("family"),
        "vendor": board.get("vendor"),
        "product_family": board.get("product_family") or board.get("family"),
        "support_tier": board.get("support_tier") or "production",
        "segments": board.get("segments") or [],
        "device": board.get("device"),
        "package": board.get("package"),
        "implementation_key": _implementation_key(board),
        "board_input_frequency_mhz": board.get("default_frequency_mhz"),
        "target_frequency_mhz": target,
        "status": "capacity_rejected" if rejected else "target_met" if met_runs else "target_missed" if completed else "implementation_failed",
        "target_met": bool(met_runs),
        "best_frequency_mhz": best_fmax,
        "timing_margin_percent": timing_margin_percent,
        "median_frequency_mhz": round(statistics.median(frequencies), 3) if frequencies else None,
        "worst_frequency_mhz": min(frequencies) if frequencies else None,
        "timing_pass_rate": round(len(met_runs) / len(completed), 3) if completed else 0.0,
        "winning_seed": best.get("seed"),
        "logic_cells_used": used or None,
        "logic_cells_available": available or None,
        "logic_utilization_percent": utilization,
        "resource_headroom_percent": round(100.0 - _num(utilization), 3) if best and utilization is not None else None,
        "failure_kind": "capacity_exceeded" if rejected else failure_kind,
        "failure_reason": "; ".join(capacity_preflight.get("failure_reasons") or []) if rejected else next((run.get("error") for run in pnr_runs if run.get("status") == "failed" and run.get("error")), None),
        "capacity_preflight": capacity_preflight,
        "closure_used": len(synthesis_runs) > 1 or any(run.get("effort") == "advanced" for run in pnr_runs),
        "frequency_relaxation": {"eligible": bool(relaxed), "recommended_mhz": relaxed, "reason": "reported only after target closure failed" if relaxed else None},
        "constraint_scope": "capacity_and_timing_exploration; board pin compatibility must be confirmed in FPGA Prototyping",
        "constraint_confidence": "exploration_only",
        "toolchain_confidence": "qualified" if str(board.get("support_tier") or "production").lower() == "production" else str(board.get("support_tier") or "experimental").lower(),
        "synthesis_runs": synthesis_runs,
        "pnr_runs": pnr_runs,
        "winning_run": best or None,
    }


def _recommend(results: list[dict]) -> dict:
    viable = [item for item in results if item.get("target_met") and item.get("programming_ready") is not False]
    pool = viable or [item for item in results if _num(item.get("best_frequency_mhz")) > 0 and item.get("programming_ready") is not False]
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


def _recommendation_details(results: list[dict], recommendations: dict, target: float) -> dict:
    by_board = {str(item.get("board")): item for item in results}
    details = {}
    for profile, board_key in recommendations.items():
        item = by_board.get(str(board_key)) if board_key else None
        if not item:
            details[profile] = None
            continue
        margin = _num(item.get("timing_margin_percent"))
        headroom = _num(item.get("resource_headroom_percent"))
        details[profile] = {
            "board": board_key,
            "label": item.get("label"),
            "target_met": bool(item.get("target_met")),
            "target_frequency_mhz": target,
            "best_frequency_mhz": item.get("best_frequency_mhz"),
            "timing_margin_percent": item.get("timing_margin_percent"),
            "resource_headroom_percent": item.get("resource_headroom_percent"),
            "toolchain_confidence": item.get("toolchain_confidence"),
            "constraint_confidence": item.get("constraint_confidence"),
            "why": (f"Meets {target:g} MHz with {margin:.1f}% timing margin and {headroom:.1f}% logic headroom." if item.get("target_met") else f"Best available result is {item.get('best_frequency_mhz')} MHz; the {target:g} MHz target was not met."),
            "next_step": "Continue to FPGA Prototyping to apply verified board pins, rerun implementation, verify, and generate the bitstream.",
        }
    return details

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
    board_keys = list(dict.fromkeys(
        key for key in requested_boards
        if key in CANDIDATE_BOARDS
        and key in BOARD_REGISTRY
        and str(BOARD_REGISTRY[key].get("support_tier") or "production").lower() != "unavailable"
    ))
    if not board_keys:
        raise RuntimeError("Select at least one supported FPGA board/device to explore.")
    baseline_seed_count = max(1, min(int(_num(state.get("baseline_seed_count"), 1)), 10))
    closure_seed_count = max(1, min(int(_num(state.get("closure_seed_count"), 1)), 10))
    baseline_seeds = list(range(1, baseline_seed_count + 1))
    closure_seeds = list(range(baseline_seed_count + 1, baseline_seed_count + closure_seed_count + 1))
    io_mapping = state.get("fpga_explorer_io_mapping") if isinstance(state.get("fpga_explorer_io_mapping"), dict) else {}
    io_required = len(io_mapping.get("top_level_ports") or [])
    mappings_by_board = {
        str(item.get("board")): item for item in (io_mapping.get("mappings") or [])
        if isinstance(item, dict) and item.get("board")
    }
    _progress(state, f"Explorer plan: {len(board_keys)} selected board(s), target {target:g} MHz, {baseline_seed_count} baseline seed(s) + {closure_seed_count} conditional closure seed(s).")
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
        io_available = ((board.get("resources") or {}).get("io_cells"))
        if io_required and io_available is not None and io_required > int(io_available):
            capacity_preflight = _capacity_preflight({}, board, io_required)
            summary = _summarize_board(board_key, board, [], [], target, capacity_preflight)
            implementation_cache[implementation_key] = deepcopy(summary)
            results.append(summary)
            _progress(
                state,
                f"Board {board_index}/{len(board_keys)}: {board_key} rejected before synthesis/P&R; "
                f"top-level I/O requires {io_required}, device provides {int(io_available)}.",
            )
            continue
        _progress(state, f"{board_key}: baseline synthesis started.")
        baseline = _run_synthesis(state, board_key, board, "baseline")
        _progress(state, f"{board_key}: baseline synthesis {baseline.get('status')}.")
        synthesis_runs = [baseline]
        pnr_runs: list[dict] = []
        capacity_preflight = _capacity_preflight(baseline, board, io_required) if baseline.get("status") == "completed" else None
        if capacity_preflight and capacity_preflight.get("status") == "reject":
            reasons = "; ".join(capacity_preflight.get("failure_reasons") or [])
            _progress(state, f"{board_key}: rejected before P&R because synthesized capacity cannot fit ({reasons}).")
        elif baseline.get("status") == "completed":
            for seed_index, seed in enumerate(baseline_seeds, start=1):
                _progress(state, f"{board_key}: baseline P&R {seed_index}/{baseline_seed_count} (seed {seed}) started; long placements can take up to 20 minutes and progress is reported every minute.")
                run = _run_pnr(state, board_key, board, baseline, seed, "balanced")
                pnr_runs.append(run)
                fmax = run.get("max_frequency_mhz")
                detail = f", Fmax {float(fmax):.3f} MHz" if fmax is not None else ""
                _progress(state, f"{board_key}: baseline seed {seed} {run.get('status')}{detail}.")
        routed_baseline = [run for run in pnr_runs if run.get("status") == "completed"]
        met = any(_num(run.get("max_frequency_mhz")) >= target or run.get("timing_met") is True for run in routed_baseline)
        if not met and routed_baseline:
            _progress(state, f"{board_key}: target missed after baseline; starting synthesis/P&R closure.")
            closure_synth_cmd = {
                "ecp5": "synth_ecp5",
                "nexus": "synth_nexus",
                "gowin": "synth_gowin",
            }.get(str(board.get("family") or "ice40"), "synth_ice40")
            help_text = _yosys_help(closure_synth_cmd)
            closure_strategy = "closure_retime" if "-noabc9" in help_text and "-retime" in help_text else "closure_flatten"
            _progress(state, f"{board_key}: {closure_strategy} synthesis started.")
            closure_synth = _run_synthesis(state, board_key, board, closure_strategy)
            synthesis_runs.append(closure_synth)
            _progress(state, f"{board_key}: {closure_strategy} synthesis {closure_synth.get('status')}.")
            if closure_synth.get("status") == "completed":
                for seed_index, seed in enumerate(closure_seeds, start=1):
                    _progress(state, f"{board_key}: closure P&R {seed_index}/{closure_seed_count} (seed {seed}) started; long placements can take up to 20 minutes and progress is reported every minute.")
                    run = _run_pnr(state, board_key, board, closure_synth, seed, "advanced")
                    pnr_runs.append(run)
                    fmax = run.get("max_frequency_mhz")
                    detail = f", Fmax {float(fmax):.3f} MHz" if fmax is not None else ""
                    _progress(state, f"{board_key}: closure seed {seed} {run.get('status')}{detail}.")
        elif not routed_baseline and baseline.get("status") == "completed":
            _progress(state, f"{board_key}: no baseline route completed; closure seeds skipped because capacity/I/O/tool failures are not timing failures.")
        summary = _summarize_board(board_key, board, synthesis_runs, pnr_runs, target, capacity_preflight)
        board_mapping = mappings_by_board.get(board_key) or {}
        summary["programming_ready"] = board_mapping.get("programming_ready")
        summary["mapped_ports"] = board_mapping.get("mapped_ports") or []
        summary["unmapped_ports"] = board_mapping.get("unmapped_ports") or []
        implementation_cache[implementation_key] = deepcopy(summary)
        results.append(summary)
        outcome = "target met" if summary.get("target_met") else summary.get("status")
        best = summary.get("best_frequency_mhz")
        best_text = f" at {float(best):.3f} MHz" if best is not None else ""
        _progress(state, f"Board {board_index}/{len(board_keys)}: {board_key} {outcome}{best_text}; winning seed {summary.get('winning_seed') or 'n/a'}.")
    recommendations = _recommend(results)
    recommendation_details = _recommendation_details(results, recommendations, target)
    selected_board = recommendations.get(requested_profile)
    selected_result = next((item for item in results if item.get("board") == selected_board), None)
    winning_run = (selected_result or {}).get("winning_run") if isinstance((selected_result or {}).get("winning_run"), dict) else {}
    summary = {
        "type": "fpga_target_explorer",
        "status": "completed" if results else "failed",
        "top_module": top,
        "rtl_file_count": len(rtl_files),
        "design_intent_provided": bool(str(state.get("spec_text") or state.get("spec") or "").strip()),
        "design_intent": str(state.get("spec_text") or state.get("spec") or "").strip() or None,
        "target_frequency_mhz": target,
        "requested_profile": requested_profile,
        "seed_policy": {"baseline_seed_count": baseline_seed_count, "closure_seed_count": closure_seed_count, "closure_is_conditional": True},
        "selected_recommendation": selected_board,
        "recommendations": recommendations,
        "recommendation_details": recommendation_details,
        "recommendation_policy": {
            "best_overall": "target met, timing stability, useful headroom, performance, then smallest viable target",
            "best_performance": "highest robust median Fmax",
            "best_low_cost": "smallest viable FPGA capacity proxy; live board pricing is not assumed",
            "best_for_growth": "largest remaining logic headroom",
        },
        "results": results,
        "candidate_count": len(results),
        "preflight_rejected_count": sum(1 for item in results if item.get("status") == "capacity_rejected"),
        "unique_implementation_count": len(implementation_cache),
        "frequency_relaxation_policy": "reported only for candidates that fail the requested target after closure",
        "continuation": {
            "app": "fpga-bitstream",
            "label": "Continue to FPGA Prototyping",
            "selected_board": selected_board,
            "target_frequency_mhz": target,
            "source_workflow_id": state.get("workflow_id"),
            "top_module": top,
            "winning_configuration": {
                "seed": winning_run.get("seed"),
                "synthesis_strategy": winning_run.get("synthesis_strategy"),
                "tool_effort": winning_run.get("effort"),
                "achieved_frequency_mhz": (selected_result or {}).get("best_frequency_mhz"),
                "timing_margin_percent": (selected_result or {}).get("timing_margin_percent"),
            },
        },
    }
    _progress(state, f"Exploration complete: {len(results)} board result(s), {len(implementation_cache)} unique implementation(s); {requested_profile} recommends {selected_board or 'no viable target'}.")
    publish_json(state, agent, "target_explorer", "fpga_target_explorer.json", summary)
    state["fpga_target_explorer"] = summary
    return state
