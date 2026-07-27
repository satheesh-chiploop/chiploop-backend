import json
import os
import re
import subprocess
from functools import lru_cache
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, read_text, run_cmd


def _publish_text(state: dict, agent: str, subdir: str, filename: str, content: str) -> None:
    if not content:
        return
    try:
        from utils.artifact_utils import save_text_artifact_and_record

        save_text_artifact_and_record(
            workflow_id=str(state.get("workflow_id") or ""),
            agent_name=agent,
            subdir=f"fpga/{subdir}".rstrip("/"),
            filename=filename,
            content=content,
        )
    except Exception:
        pass


def _parse_nextpnr(log: str) -> dict:
    text = read_text(log)
    out = {
        "timing_met": None,
        "max_frequency_mhz": None,
        "wns_ns": None,
        "tns_ns": None,
        "timing_violation_count": None,
        "warnings": text.lower().count("warning"),
        "errors": text.lower().count("error"),
    }
    freq = re.findall(r"Max frequency.*?([0-9]+(?:\.[0-9]+)?)\s*MHz", text, flags=re.IGNORECASE)
    if freq:
        out["max_frequency_mhz"] = float(freq[-1])
    util = re.findall(r"(?:ICESTORM_LC|TRELLIS_SLICE|SB_LUT4|Logic cells).*?([0-9]+)\s*/\s*([0-9]+)", text, flags=re.IGNORECASE)
    if util:
        used, available = util[-1]
        out["logical_cells_used"] = int(used)
        out["logical_cells_available"] = int(available)
        out["logic_utilization_percent"] = round((int(used) / max(int(available), 1)) * 100.0, 3)
    lut_only = re.findall(r"([0-9]+)\s+LCs used as LUT4 only", text, flags=re.IGNORECASE)
    lut_with_ff = re.findall(r"([0-9]+)\s+LCs used as LUT4 and DFF", text, flags=re.IGNORECASE)
    if lut_only or lut_with_ff:
        out["routed_lut4_cells"] = int(lut_only[-1]) if lut_only else 0
        out["routed_lut4_cells"] += int(lut_with_ff[-1]) if lut_with_ff else 0
    dff_only = re.findall(r"([0-9]+)\s+LCs used as DFF only", text, flags=re.IGNORECASE)
    if lut_with_ff or dff_only:
        out["routed_flip_flops"] = int(lut_with_ff[-1]) if lut_with_ff else 0
        out["routed_flip_flops"] += int(dff_only[-1]) if dff_only else 0
    lowered = text.lower()
    if "timing met" in lowered or re.search(r"\bPASS\s+at\s+[0-9]+(?:\.[0-9]+)?\s*MHz", text, flags=re.IGNORECASE):
        out["timing_met"] = True
    if "failed to meet timing" in lowered or "timing failed" in lowered or re.search(r"\bFAIL\s+at\s+[0-9]+(?:\.[0-9]+)?\s*MHz", text, flags=re.IGNORECASE):
        out["timing_met"] = False
    slack_values = [
        float(value)
        for value in re.findall(r"(?:slack|WNS).*?(-?[0-9]+(?:\.[0-9]+)?)\s*ns", text, flags=re.IGNORECASE)
    ]
    if slack_values:
        out["wns_ns"] = round(min(slack_values), 3)
        out["timing_violation_count"] = sum(1 for value in slack_values if value < 0)
        out["tns_ns"] = round(sum(value for value in slack_values if value < 0), 3)
    elif out["timing_met"] is True:
        out["timing_violation_count"] = 0
        out["tns_ns"] = 0
    return out


def _as_number(value):
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", value)
        if match:
            number = float(match.group(0))
            return int(number) if number.is_integer() else number
    return None


def _used_available(item):
    if isinstance(item, dict):
        used = _as_number(item.get("used"))
        available = _as_number(item.get("available") or item.get("total"))
        if used is not None:
            return int(used), int(available) if available is not None else None
    if isinstance(item, (list, tuple)) and item:
        used = _as_number(item[0])
        available = _as_number(item[1]) if len(item) > 1 else None
        if used is not None:
            return int(used), int(available) if available is not None else None
    if isinstance(item, str):
        match = re.search(r"([0-9]+)\s*/\s*([0-9]+)", item)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def _parse_nextpnr_report(report_path: str, board: dict) -> dict:
    if not os.path.exists(report_path):
        return {}
    try:
        with open(report_path, "r", encoding="utf-8", errors="ignore") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    out: dict = {"report": report_path, "utilization_source": "nextpnr_report"}
    utilization = data.get("utilization") if isinstance(data, dict) else {}
    if isinstance(utilization, dict):
        out["utilization"] = utilization
        family = str(board.get("family") or "").lower()
        logic_keys = (
            "ICESTORM_LC",
            "TRELLIS_COMB",
            "TRELLIS_SLICE",
            "SLICE",
            "LUT4",
        ) if family == "ecp5" else (
            "ICESTORM_LC",
            "SB_LUT4",
            "TRELLIS_COMB",
            "TRELLIS_SLICE",
            "LUT4",
        )
        for key in logic_keys:
            item = utilization.get(key)
            used, available = _used_available(item)
            if used is not None:
                if available is None:
                    available = (((board.get("resources") or {}).get("logic_cells")) or 0)
                out["logical_cells_used"] = used
                out["logical_cells_available"] = available
                out["routed_resource"] = key
                if available:
                    out["logic_utilization_percent"] = round((used / available) * 100.0, 3)
                break
        for key in ("SB_LUT4", "LUT4", "TRELLIS_COMB"):
            lut_used, lut_available = _used_available(utilization.get(key))
            if lut_used is not None:
                out["routed_lut4_cells"] = lut_used
                if lut_available is not None:
                    out["routed_lut4_cells_available"] = lut_available
                break
        ff_used, ff_available = None, None
        for key in ("TRELLIS_FF", "DFF", "SB_DFF", "SB_DFFE", "FF"):
            ff_used, ff_available = _used_available(utilization.get(key))
            if ff_used is not None:
                out["routed_flip_flops"] = ff_used
                if ff_available is not None:
                    out["routed_flip_flops_available"] = ff_available
                break
    fmax = data.get("fmax") if isinstance(data, dict) else {}
    if isinstance(fmax, dict) and fmax:
        out["fmax"] = fmax
        achieved = [
            value.get("achieved")
            for value in fmax.values()
            if isinstance(value, dict) and isinstance(value.get("achieved"), (int, float))
        ]
        constraints = [
            value.get("constraint")
            for value in fmax.values()
            if isinstance(value, dict) and isinstance(value.get("constraint"), (int, float))
        ]
        if achieved:
            out["max_frequency_mhz"] = round(float(min(achieved)), 3)
        if achieved and constraints:
            out["timing_met"] = min(achieved) >= max(constraints)
    return out


@lru_cache(maxsize=4)
def _nextpnr_help(tool: str) -> str:
    try:
        result = subprocess.run([tool, "--help"], capture_output=True, text=True, timeout=15, check=False)
        return f"{result.stdout or ''}\n{result.stderr or ''}"
    except Exception:
        return ""


@lru_cache(maxsize=4)
def _nextpnr_version(tool: str) -> str | None:
    try:
        result = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=15, check=False)
        return (result.stdout or result.stderr or "").strip() or None
    except Exception:
        return None


def _nextpnr_choice_available(help_text: str, option: str, value: str) -> bool:
    pattern = rf"{re.escape(option)}[^\n]*(?:available|values?|choices?)[^:\n]*:\s*([^\n;]+)"
    match = re.search(pattern, help_text, flags=re.IGNORECASE)
    if not match:
        return False
    choices = {item.lower() for item in re.findall(r"[A-Za-z0-9_-]+", match.group(1))}
    return value.lower() in choices


def _nextpnr_effort_policy(state: dict, tool: str, help_text: str | None = None) -> dict:
    mode = str(state.get("fpga_closure_mode") or "balanced").strip().lower()
    mode = mode if mode in {"balanced", "advanced"} else "balanced"
    available = help_text if help_text is not None else _nextpnr_help(tool)
    args: list[str] = []
    requested = ["timing_driven", "target_frequency", "seed_exploration"]
    if mode == "advanced":
        requested.extend(["heap_placer", "router2", "higher_timing_weight", "timing_ripup"])
        heap_available = _nextpnr_choice_available(available, "--placer", "heap")
        router2_available = _nextpnr_choice_available(available, "--router", "router2")
        if heap_available:
            args.extend(["--placer", "heap"])
            if "--placer-heap-timingweight" in available:
                args.extend(["--placer-heap-timingweight", "20"])
            if "--placer-heap-critexp" in available:
                args.extend(["--placer-heap-critexp", "4"])
        if router2_available:
            args.extend(["--router", "router2"])
            if "--tmg-ripup" in available:
                args.append("--tmg-ripup")
            elif "--router2-tmg-ripup" in available:
                args.append("--router2-tmg-ripup")
            if "--router2-alt-weights" in available:
                args.append("--router2-alt-weights")
    if "--detailed-timing-report" in available:
        args.append("--detailed-timing-report")
    target = state.get("target_frequency_mhz")
    if target not in (None, "") and "--freq" in available:
        args.extend(["--freq", str(target)])
    return {
        "mode": mode,
        "goal": "high_timing_effort" if mode == "advanced" else "balanced_timing",
        "requested": requested,
        "effective_args": args,
        "capability_checked": True,
        "tool_version": _nextpnr_version(tool) if help_text is None else None,
    }


def run_agent(state: dict) -> dict:
    agent = "FPGA nextpnr Place & Route Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board = board_config(state)
    out_dir = fpga_dir(state, "pnr")
    json_netlist = fpga.get("yosys_json")
    family = str(board.get("family") or "ice40").lower()
    constraint_path = fpga.get("constraints_lpf") if family == "ecp5" else fpga.get("constraints_pcf")
    output_ext = str(board.get("pnr_output_ext") or (".config" if family == "ecp5" else ".asc"))
    pnr_output = os.path.abspath(f"{out_dir}/{fpga.get('top_module') or 'top'}{output_ext}")
    log_path = os.path.abspath(f"{out_dir}/{board.get('nextpnr_tool') or 'nextpnr'}.log")
    report_path = os.path.abspath(f"{out_dir}/fpga_nextpnr_report.json")
    seed = state.get("fpga_nextpnr_seed") or state.get("nextpnr_seed")
    nextpnr_tool = str(board.get("nextpnr_tool") or ("nextpnr-ecp5" if family == "ecp5" else "nextpnr-ice40"))
    effort_policy = _nextpnr_effort_policy(state, nextpnr_tool)
    summary = {
        "agent": agent,
        "status": "blocked",
        "target": board,
        "planned_pnr_output": pnr_output,
        "pnr_output": None,
        "asc": None,
        "routed_config": None,
        "artifact_produced": False,
        "output_format": "textcfg" if family == "ecp5" else "asc",
        "closure_iteration": int(state.get("fpga_timing_closure_iteration_index") or 0),
        "seed": seed,
        "timing_driven": bool(state.get("fpga_nextpnr_timing_driven") or state.get("run_fpga_timing_closure_loop")),
        "tool_effort": effort_policy,
    }
    if not json_netlist or not os.path.exists(str(json_netlist)):
        summary["error"] = "Missing Yosys JSON netlist."
    else:
        cmd = [
            nextpnr_tool,
            str(board.get("nextpnr_device_flag") or "--hx8k"),
            "--package",
            str(board.get("nextpnr_package") or board.get("package") or "ct256"),
            "--json",
            str(json_netlist),
            "--report",
            report_path,
        ]
        if family == "ecp5":
            cmd.extend(["--textcfg", pnr_output])
        else:
            cmd.extend(["--asc", pnr_output])
        if constraint_path:
            resolved_constraint = os.path.abspath(str(constraint_path))
            if os.path.exists(resolved_constraint):
                cmd.extend(["--lpf" if family == "ecp5" else "--pcf", resolved_constraint])
            else:
                summary["constraint_warning"] = f"Constraint file not found: {constraint_path}"
        cmd.extend(effort_policy["effective_args"])
        if seed:
            cmd.extend(["--seed", str(seed)])
        result = run_cmd(cmd, cwd=out_dir, log_path=log_path, timeout=900)
        log_metrics = _parse_nextpnr(log_path)
        for key in ("logical_cells_used", "logical_cells_available", "logic_utilization_percent"):
            log_metrics.pop(key, None)
        summary.update(log_metrics)
        report_metrics = _parse_nextpnr_report(report_path, board)
        summary.update(report_metrics)
        produced = os.path.exists(pnr_output)
        summary.update({
            "status": "completed" if result["ok"] and produced else "warning" if produced else "failed",
            "command": result,
            "artifact_produced": produced,
            "pnr_output": pnr_output if produced else None,
            "asc": pnr_output if family == "ice40" and produced else None,
            "routed_config": pnr_output if family == "ecp5" and produced else None,
            "report": report_path if os.path.exists(report_path) else None,
        })
        if not produced:
            summary["error"] = f"nextpnr did not produce a {summary['output_format']} place-route output."
        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8", errors="ignore") as handle:
                    publish_json(state, agent, "pnr", "fpga_nextpnr_report.json", json.load(handle))
            except Exception:
                _publish_text(state, agent, "pnr", "fpga_nextpnr_report.json", read_text(report_path))
        _publish_text(state, agent, "pnr", os.path.basename(log_path), read_text(log_path))
    publish_json(state, agent, "pnr", "fpga_place_route_summary.json", summary)
    manifest_update(state, "place_route", summary)
    manifest_update(state, "pnr_output", pnr_output if os.path.exists(pnr_output) else None)
    manifest_update(state, "asc", pnr_output if family == "ice40" and os.path.exists(pnr_output) else None)
    manifest_update(state, "routed_config", pnr_output if family == "ecp5" and os.path.exists(pnr_output) else None)
    if summary["status"] == "failed":
        state["status"] = "FPGA place-and-route failed."
    return state
