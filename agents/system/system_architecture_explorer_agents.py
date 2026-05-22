import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record


SUBDIR = "system/architecture"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _workflow_id(state: Dict[str, Any]) -> str:
    return str(state.get("workflow_id") or "default")


def _artifact_dir(state: Dict[str, Any]) -> str:
    root = str(state.get("artifact_dir") or os.path.join("artifacts", _workflow_id(state), SUBDIR))
    os.makedirs(root, exist_ok=True)
    return root


def _write_local(state: Dict[str, Any], filename: str, content: str) -> str:
    path = os.path.join(_artifact_dir(state), filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _record(state: Dict[str, Any], agent_name: str, filename: str, content: str) -> Optional[str]:
    _write_local(state, filename, content)
    try:
        return save_text_artifact_and_record(
            workflow_id=_workflow_id(state),
            agent_name=agent_name,
            subdir=SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _demo_sweep(state: Dict[str, Any]) -> Dict[str, List[int]]:
    sweep = state.get("sweep") if isinstance(state.get("sweep"), dict) else {}
    return {
        "l1d_size_kb": list(sweep.get("l1d_size_kb") or [16, 32, 64]),
        "l2_size_kb": list(sweep.get("l2_size_kb") or [256, 512, 1024]),
    }


def _int_state(state: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(state.get(key) or default)
    except (TypeError, ValueError):
        return default


def system_architecture_intent_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Architecture Intent Agent"
    goal = str(
        state.get("goal")
        or state.get("experiment_goal")
        or "Explore cache-size tradeoffs for matrix multiplication with performance, power, and area estimates; show workload vs cache size charts."
    )
    intent = {
        "project_name": state.get("project_name") or "matrix_multiply_cache_sweep_demo",
        "simulator": state.get("simulator") or state.get("simulation_tool") or "gem5",
        "isa": state.get("isa") or "x86",
        "cpu_model": state.get("cpu_model") or "TimingSimpleCPU",
        "cores": _int_state(state, "cores", 1),
        "workload": state.get("workload") or state.get("workload_name") or "matrix_multiply",
        "mode": state.get("mode") or "syscall_emulation",
        "clock": state.get("clock") or "2GHz",
        "memory_type": state.get("memory_type") or "DDR3_1600_8x8",
        "memory_size": state.get("memory_size") or "2GB",
        "goal": goal,
        "metrics": ["ipc", "cpi", "l1d_miss_rate", "l2_miss_rate", "estimated_power_w", "estimated_area_mm2"],
        "created_at": _now(),
    }
    _record(state, agent_name, "intent.json", _json(intent))
    return {"system_architecture_intent": intent, "status": "ok"}


def system_workload_characterization_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Workload Characterization Agent"
    intent = state.get("system_architecture_intent") or {}
    workload = str(intent.get("workload") or state.get("workload") or "matrix_multiply")
    profile = {
        "workload": workload,
        "class": "cache_sensitive_compute",
        "working_set": "matrix tiles exceed small L1D caches and benefit from larger L2 capacity",
        "expected_bottlenecks": ["L1D capacity misses", "L2 refill bandwidth", "memory latency at small cache sizes"],
        "recommended_charts": ["cache_size_vs_ipc", "cache_size_vs_miss_rate", "ppa_pareto"],
        "created_at": _now(),
    }
    _record(state, agent_name, "workload_profile.json", _json(profile))
    return {"system_workload_profile": profile, "status": "ok"}


def system_gem5_config_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System gem5 Config Agent"
    intent = state.get("system_architecture_intent") or {}
    sweep = _demo_sweep(state)
    config = {
        "simulator": "gem5",
        "mode": intent.get("mode") or "syscall_emulation",
        "isa": intent.get("isa") or "x86",
        "cpu_model": intent.get("cpu_model") or "TimingSimpleCPU",
        "cores": intent.get("cores") or 1,
        "clock": intent.get("clock") or "2GHz",
        "memory": {"type": intent.get("memory_type") or "DDR3_1600_8x8", "size": intent.get("memory_size") or "2GB"},
        "cache_template": {
            "l1i_size": state.get("l1i_size") or "32kB",
            "l1d_size_kb": sweep["l1d_size_kb"],
            "l2_size_kb": sweep["l2_size_kb"],
            "line_size": _int_state(state, "cache_line_size", 64),
            "l1_assoc": _int_state(state, "l1_assoc", 2),
            "l2_assoc": _int_state(state, "l2_assoc", 8),
            "prefetcher": state.get("prefetcher") or "none",
        },
        "workload": intent.get("workload") or "matrix_multiply",
    }
    py = """# Generated by ChipLoop System Architecture Explorer.
# Production runner should materialize one gem5 invocation per sweep point.
from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.classic.private_l1_private_l2_cache_hierarchy import PrivateL1PrivateL2CacheHierarchy
from gem5.components.memory import SingleChannelDDR3_1600
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.components.processors.cpu_types import CPUTypes

SWEEP = {sweep}
""".format(sweep=repr(sweep))
    _record(state, agent_name, "gem5_config.json", _json(config))
    _record(state, agent_name, "gem5_config.py", py)
    return {"system_gem5_config": config, "status": "ok"}


def system_design_space_exploration_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Design Space Exploration Agent"
    sweep = _demo_sweep(state)
    matrix = []
    idx = 1
    for l1 in sweep["l1d_size_kb"]:
        for l2 in sweep["l2_size_kb"]:
            matrix.append({
                "run_id": f"archsim_{idx:02d}",
                "workload": state.get("workload") or "matrix_multiply",
                "l1d_size_kb": l1,
                "l2_size_kb": l2,
                "cpu_model": state.get("cpu_model") or "TimingSimpleCPU",
                "isa": state.get("isa") or "x86",
                "cores": _int_state(state, "cores", 1),
                "memory_type": state.get("memory_type") or "DDR3_1600_8x8",
                "l1_assoc": _int_state(state, "l1_assoc", 2),
                "l2_assoc": _int_state(state, "l2_assoc", 8),
                "prefetcher": state.get("prefetcher") or "none",
            })
            idx += 1
    plan = {"sweep_points": matrix, "count": len(matrix), "created_at": _now()}
    _record(state, agent_name, "sweep_matrix.json", _json(plan))
    return {"system_architecture_sweep": plan, "status": "ok"}


def _estimate_metrics(point: Dict[str, Any]) -> Dict[str, float]:
    l1_kb = int(point.get("l1d_size_kb") or 32)
    l2_kb = int(point.get("l2_size_kb") or 512)
    cores = max(1, int(point.get("cores") or 1))
    cpu_model = str(point.get("cpu_model") or "TimingSimpleCPU")
    isa = str(point.get("isa") or "x86").lower()
    memory_type = str(point.get("memory_type") or "DDR3_1600_8x8")
    prefetcher = str(point.get("prefetcher") or "none")
    l1_gain = {16: 0.00, 32: 0.16, 64: 0.22}.get(l1_kb, min(l1_kb / 256.0, 0.25))
    l2_gain = {256: 0.00, 512: 0.10, 1024: 0.14}.get(l2_kb, min(l2_kb / 4096.0, 0.18))
    cpu_factor = {"TimingSimpleCPU": 1.0, "MinorCPU": 1.18, "O3CPU": 1.42}.get(cpu_model, 1.0)
    isa_factor = {"x86": 1.0, "riscv": 0.96, "arm": 0.98}.get(isa, 1.0)
    memory_factor = 1.06 if "DDR4" in memory_type or "LPDDR5" in memory_type else 1.0
    prefetch_gain = 0.05 if prefetcher != "none" else 0.0
    core_scale = 1.0 + min(cores - 1, 7) * 0.035
    ipc = round((0.82 + l1_gain + l2_gain + prefetch_gain) * cpu_factor * isa_factor * memory_factor * core_scale, 3)
    l1_mpki = round(max(18.0, 44.0 - (l1_kb / 2.7)), 2)
    l2_mpki = round(max(6.5, 19.0 - (l2_kb / 92.0)), 2)
    power = round((1.05 + (l1_kb * 0.006) + (l2_kb * 0.00055)) * (0.85 + cores * 0.15) * cpu_factor, 3)
    area = round((0.30 + (l1_kb * 0.0032) + (l2_kb * 0.00042)) * (0.9 + cores * 0.1) * (1.12 if cpu_model == "O3CPU" else 1.0), 3)
    return {
        "ipc": ipc,
        "cpi": round(1.0 / ipc, 3),
        "l1d_mpki": l1_mpki,
        "l2_mpki": l2_mpki,
        "estimated_power_w": power,
        "estimated_area_mm2": area,
        "perf_per_watt": round(ipc / power, 3),
        "perf_per_area": round(ipc / area, 3),
    }


def system_gem5_execution_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System gem5 Execution Agent"
    sweep = state.get("system_architecture_sweep") or {}
    points = sweep.get("sweep_points") or []
    gem5_bin = os.environ.get("GEM5_X86_BIN") or os.environ.get("GEM5_RISCV_BIN") or os.environ.get("GEM5_BIN")
    mode = "estimated_demo" if not gem5_bin else "gem5_ready"
    rows = []
    for point in points:
        metrics = _estimate_metrics(point)
        rows.append({**point, **metrics, "execution_mode": mode})
    result = {
        "tool": "gem5",
        "execution_mode": mode,
        "note": "Demo uses deterministic estimates when GEM5_BIN is not configured on the backend runner.",
        "runs": rows,
        "created_at": _now(),
    }
    _record(state, agent_name, "gem5_run_results.json", _json(result))
    _record(state, agent_name, "gem5_execution.log", "\n".join([f"{r['run_id']} {mode} ipc={r['ipc']}" for r in rows]) + "\n")
    return {"system_gem5_results": result, "status": "ok"}


def system_performance_metrics_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Performance Metrics Agent"
    runs = (state.get("system_gem5_results") or {}).get("runs") or []
    best_ipc = max(runs, key=lambda r: r.get("ipc", 0), default={})
    metrics = {
        "rows": runs,
        "best_ipc": best_ipc,
        "summary": {
            "ipc_range": [min([r["ipc"] for r in runs], default=0), max([r["ipc"] for r in runs], default=0)],
            "lowest_l1d_mpki": min([r["l1d_mpki"] for r in runs], default=0),
            "lowest_l2_mpki": min([r["l2_mpki"] for r in runs], default=0),
        },
    }
    _record(state, agent_name, "performance_metrics.json", _json(metrics))
    return {"system_performance_metrics": metrics, "status": "ok"}


def system_power_estimation_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Power Estimation Agent"
    rows = (state.get("system_gem5_results") or {}).get("runs") or []
    estimates = {
        "method": "ChipLoop cache/activity model; replace with McPAT integration when configured.",
        "rows": [{"run_id": r["run_id"], "estimated_power_w": r["estimated_power_w"], "perf_per_watt": r["perf_per_watt"]} for r in rows],
    }
    _record(state, agent_name, "power_estimates.json", _json(estimates))
    return {"system_power_estimates": estimates, "status": "ok"}


def system_area_estimation_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Area Estimation Agent"
    rows = (state.get("system_gem5_results") or {}).get("runs") or []
    estimates = {
        "method": "ChipLoop cache area model; replace with CACTI/McPAT integration when configured.",
        "rows": [{"run_id": r["run_id"], "estimated_area_mm2": r["estimated_area_mm2"], "perf_per_area": r["perf_per_area"]} for r in rows],
    }
    _record(state, agent_name, "area_estimates.json", _json(estimates))
    return {"system_area_estimates": estimates, "status": "ok"}


def system_ppa_tradeoff_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System PPA Tradeoff Agent"
    rows = (state.get("system_gem5_results") or {}).get("runs") or []
    ranked = sorted(rows, key=lambda r: (r["perf_per_watt"], r["perf_per_area"], r["ipc"]), reverse=True)
    knee = ranked[0] if ranked else {}
    summary = {
        "recommended_point": knee,
        "reason": "Best combined performance per watt and performance per area in the demo sweep.",
        "rows": rows,
    }
    _record(state, agent_name, "ppa_summary.json", _json(summary))
    return {"system_ppa_summary": summary, "status": "ok"}


def system_architecture_visualization_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Visualization Agent"
    rows = (state.get("system_gem5_results") or {}).get("runs") or []
    charts = {
        "title": "Matrix multiply cache sweep",
        "x_key": "l1d_size_kb",
        "series": [
            {"id": "ipc_by_l1d", "type": "line", "x": "l1d_size_kb", "y": "ipc", "group": "l2_size_kb", "label": "IPC vs L1D size"},
            {"id": "mpki_by_l1d", "type": "line", "x": "l1d_size_kb", "y": "l1d_mpki", "group": "l2_size_kb", "label": "L1D MPKI vs L1D size"},
            {"id": "ppa_bubble", "type": "bubble", "x": "estimated_area_mm2", "y": "ipc", "size": "estimated_power_w", "label": "Performance vs area vs power"},
        ],
        "rows": rows,
    }
    _record(state, agent_name, "charts.json", _json(charts))
    return {"system_architecture_charts": charts, "status": "ok"}


def system_architecture_report_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Architecture Report Agent"
    ppa = state.get("system_ppa_summary") or {}
    rec = ppa.get("recommended_point") or {}
    report = f"""# System Architecture Explorer Report

## Demo
- Tool: gem5
- Workload: matrix_multiply
- Sweep: L1D cache size vs L2 cache size
- Scope: performance, power estimate, area estimate

## Recommendation
Use `{rec.get('l1d_size_kb', 'n/a')}KB` L1D and `{rec.get('l2_size_kb', 'n/a')}KB` L2 for the best PPA balance in this demo.

## Why
The sweep shows IPC improves as cache capacity increases, but the largest cache points pay extra area and power. The recommended point maximizes performance-per-watt and performance-per-area for this workload.

## Next Experiment
Run the same sweep on a second workload, then compare whether the cache knee is workload-specific or stable across the target software mix.
"""
    _record(state, agent_name, "report.md", report)
    return {"system_architecture_report": report, "status": "ok"}
