import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from tooling.runner import run_command, tool_path
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


def _read_local_json(state: Dict[str, Any], filename: str) -> Dict[str, Any]:
    root = Path(_artifact_dir(state))
    candidates = [root / filename]
    if root.exists():
        candidates.extend(root.rglob(filename))
    for path in candidates:
        if path.exists() and path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _simple_yaml(data: Dict[str, Any], indent: int = 0) -> str:
    lines: List[str] = []
    pad = " " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_simple_yaml(value, indent + 2))
        elif isinstance(value, list):
            lines.append(f"{pad}{key}:")
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{pad}  -")
                    lines.append(_simple_yaml(item, indent + 4))
                else:
                    lines.append(f"{pad}  - {_yaml_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    return "\n".join(lines)


def _demo_sweep(state: Dict[str, Any]) -> Dict[str, List[int]]:
    sweep = state.get("sweep") if isinstance(state.get("sweep"), dict) else {}
    return {
        "l1d_size_kb": list(sweep.get("l1d_size_kb") or [16, 32, 64]),
        "l2_size_kb": list(sweep.get("l2_size_kb") or [256, 512, 1024]),
    }


def _str_list(state: Dict[str, Any], key: str, fallback: List[str]) -> List[str]:
    value = state.get(key)
    if isinstance(value, list):
        cleaned = [str(v).strip() for v in value if str(v).strip()]
        return cleaned or fallback
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return fallback


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
        "exploration_type": state.get("exploration_type") or "cache_tuning",
        "simulator": state.get("simulator") or state.get("simulation_tool") or "gem5",
        "isa": state.get("isa") or "x86",
        "isas": _str_list(state, "isas", [str(state.get("isa") or "x86")]),
        "cpu_model": state.get("cpu_model") or "TimingSimpleCPU",
        "cpu_models": _str_list(state, "cpu_models", [str(state.get("cpu_model") or "TimingSimpleCPU")]),
        "cores": _int_state(state, "cores", 1),
        "workload": state.get("workload") or state.get("workload_name") or "matrix_multiply",
        "mode": state.get("mode") or "syscall_emulation",
        "clock": state.get("clock") or "2GHz",
        "memory_type": state.get("memory_type") or "DDR3_1600_8x8",
        "memory_types": _str_list(state, "memory_types", [str(state.get("memory_type") or "DDR3_1600_8x8")]),
        "memory_size": state.get("memory_size") or "2GB",
        "prefetcher": state.get("prefetcher") or "none",
        "branch_predictor": state.get("branch_predictor") or "default",
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
            "branch_predictor": state.get("branch_predictor") or "default",
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
    exploration_type = str(state.get("exploration_type") or "cache_tuning")
    isas = _str_list(state, "isas", [str(state.get("isa") or "x86")])
    cpu_models = _str_list(state, "cpu_models", [str(state.get("cpu_model") or "TimingSimpleCPU")])
    memory_types = _str_list(state, "memory_types", [str(state.get("memory_type") or "DDR3_1600_8x8")])
    if exploration_type != "isa_compare":
        isas = [str(state.get("isa") or isas[0])]
    if exploration_type != "cpu_model":
        cpu_models = [str(state.get("cpu_model") or cpu_models[0])]
    if exploration_type != "memory_bottleneck":
        memory_types = [str(state.get("memory_type") or memory_types[0])]

    for isa in isas:
        for cpu_model in cpu_models:
            for memory_type in memory_types:
                for l1 in sweep["l1d_size_kb"]:
                    for l2 in sweep["l2_size_kb"]:
                        matrix.append({
                            "run_id": f"archsim_{idx:02d}",
                            "exploration_type": exploration_type,
                            "workload": state.get("workload") or "matrix_multiply",
                            "l1d_size_kb": l1,
                            "l2_size_kb": l2,
                            "cpu_model": cpu_model,
                            "isa": isa,
                            "cores": _int_state(state, "cores", 1),
                            "memory_type": memory_type,
                            "l1_assoc": _int_state(state, "l1_assoc", 2),
                            "l2_assoc": _int_state(state, "l2_assoc", 8),
                            "prefetcher": state.get("prefetcher") or "none",
                            "branch_predictor": state.get("branch_predictor") or "default",
                        })
                        idx += 1
    plan = {"sweep_points": matrix, "count": len(matrix), "created_at": _now()}
    _record(state, agent_name, "sweep_matrix.json", _json(plan))
    return {"system_architecture_sweep": plan, "status": "ok"}


GEM5_CONFIG_TEMPLATE = r'''
import argparse
import os
import m5
from m5.objects import *


class L1Cache(Cache):
    assoc = 2
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 4
    tgts_per_mshr = 20

    def connect_bus(self, bus):
        self.mem_side = bus.cpu_side_ports


class L1ICache(L1Cache):
    size = "32kB"

    def connect_cpu(self, cpu):
        self.cpu_side = cpu.icache_port


class L1DCache(L1Cache):
    size = "32kB"

    def connect_cpu(self, cpu):
        self.cpu_side = cpu.dcache_port


class L2Cache(Cache):
    size = "256kB"
    assoc = 8
    tag_latency = 20
    data_latency = 20
    response_latency = 20
    mshrs = 20
    tgts_per_mshr = 12

    def connect_cpu_side_bus(self, bus):
        self.cpu_side = bus.mem_side_ports

    def connect_mem_side_bus(self, bus):
        self.mem_side = bus.cpu_side_ports


def get_object_class(name):
    obj = globals().get(name)
    if obj is None:
        raise RuntimeError(f"gem5 object is not available in this build: {name}")
    return obj


def maybe_get_object_class(name):
    return globals().get(name)


parser = argparse.ArgumentParser()
parser.add_argument("--cmd", required=True)
parser.add_argument("--cpu-type", default="TimingSimpleCPU")
parser.add_argument("--num-cpus", type=int, default=1)
parser.add_argument("--clock", default="2GHz")
parser.add_argument("--mem-type", default="DDR3_1600_8x8")
parser.add_argument("--mem-size", default="2GB")
parser.add_argument("--l1d-size", default="32kB")
parser.add_argument("--l1i-size", default="32kB")
parser.add_argument("--l2-size", default="256kB")
parser.add_argument("--l1-assoc", type=int, default=2)
parser.add_argument("--l2-assoc", type=int, default=8)
parser.add_argument("--cacheline-size", type=int, default=64)
parser.add_argument("--prefetcher", default="none")
parser.add_argument("--branch-predictor", default="default")
parser.add_argument("--maxinsts", type=int, default=250000)
args = parser.parse_args()

cpu_map = {
    "TimingSimpleCPU": "TimingSimpleCPU",
    "MinorCPU": "MinorCPU",
    "O3CPU": "DerivO3CPU",
    "AtomicSimpleCPU": "AtomicSimpleCPU",
}
prefetch_map = {
    "stride": "StridePrefetcher",
    "tagged": "TaggedPrefetcher",
    "queued": "QueuedPrefetcher",
}
branch_map = {
    "local": "LocalBP",
    "tournament": "TournamentBP",
    "bi_mode": "BiModeBP",
    "ltage": "LTAGE",
}

if args.cpu_type == "KvmCPU":
    raise RuntimeError("KvmCPU is not supported by the ChipLoop gem5 SE runner.")

CPUClass = get_object_class(cpu_map.get(args.cpu_type, args.cpu_type))
cpus = [CPUClass() for _ in range(max(args.num_cpus, 1))]
for idx, cpu in enumerate(cpus):
    if hasattr(cpu, "cpu_id"):
        cpu.cpu_id = idx
    if hasattr(cpu, "max_insts_any_thread"):
        cpu.max_insts_any_thread = args.maxinsts

    bp_name = branch_map.get(args.branch_predictor)
    if bp_name:
        bp_cls = maybe_get_object_class(bp_name)
        if bp_cls is None:
            raise RuntimeError(f"Branch predictor is not available in this gem5 build: {args.branch_predictor}")
        if not hasattr(cpu, "branchPred"):
            raise RuntimeError(f"CPU model {args.cpu_type} does not expose a branch predictor in this gem5 build.")
        cpu.branchPred = bp_cls()

system = System()
system.clk_domain = SrcClockDomain(clock=args.clock, voltage_domain=VoltageDomain())
system.mem_mode = "atomic" if args.cpu_type == "AtomicSimpleCPU" else "timing"
system.mem_ranges = [AddrRange(args.mem_size)]
system.cache_line_size = args.cacheline_size
system.cpu = cpus
system.membus = SystemXBar()

pf_name = prefetch_map.get(args.prefetcher)
pf_cls = None
if pf_name:
    pf_cls = maybe_get_object_class(pf_name)
    if pf_cls is None:
        raise RuntimeError(f"Prefetcher is not available in this gem5 build: {args.prefetcher}")

system.l2bus = L2XBar()
for idx, cpu in enumerate(cpus):
    cpu.icache = L1ICache(size=args.l1i_size, assoc=args.l1_assoc)
    cpu.dcache = L1DCache(size=args.l1d_size, assoc=args.l1_assoc)
    if pf_cls is not None:
        cpu.dcache.prefetcher = pf_cls()
    cpu.icache.connect_cpu(cpu)
    cpu.dcache.connect_cpu(cpu)
    cpu.icache.connect_bus(system.l2bus)
    cpu.dcache.connect_bus(system.l2bus)

system.l2cache = L2Cache(size=args.l2_size, assoc=args.l2_assoc)
system.l2cache.connect_cpu_side_bus(system.l2bus)
system.l2cache.connect_mem_side_bus(system.membus)
system.system_port = system.membus.cpu_side_ports

mem_cls = maybe_get_object_class(args.mem_type)
if mem_cls is None:
    raise RuntimeError(f"Memory type is not available in this gem5 build: {args.mem_type}")
system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = mem_cls()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports

binary = os.path.abspath(args.cmd)
system.workload = SEWorkload.init_compatible(binary)
for idx, cpu in enumerate(cpus):
    process = Process(pid=100 + idx)
    process.cmd = [binary]
    cpu.workload = process
    cpu.createInterruptController()
    if cpu.interrupts:
        interrupt = cpu.interrupts[0]
        if hasattr(interrupt, "pio"):
            interrupt.pio = system.membus.mem_side_ports
        if hasattr(interrupt, "int_requestor"):
            interrupt.int_requestor = system.membus.cpu_side_ports
        if hasattr(interrupt, "int_responder"):
            interrupt.int_responder = system.membus.mem_side_ports
    cpu.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()
exit_event = m5.simulate()
print(f"Exiting @ tick {m5.curTick()} because {exit_event.getCause()}")
'''


MATRIX_BENCHMARK_C = r'''
#include <stdio.h>
#include <stdlib.h>

#ifndef CHIPLOOP_N
#define CHIPLOOP_N 24
#endif

int main(void) {
    const int n = CHIPLOOP_N;
    double *a = (double *)malloc((size_t)n * n * sizeof(double));
    double *b = (double *)malloc((size_t)n * n * sizeof(double));
    double *c = (double *)calloc((size_t)n * n, sizeof(double));
    if (!a || !b || !c) return 2;
    for (int i = 0; i < n * n; ++i) {
        a[i] = (double)((i % 17) + 1);
        b[i] = (double)(((i * 3) % 19) + 1);
    }
    for (int rep = 0; rep < 6; ++rep) {
        for (int i = 0; i < n; ++i) {
            for (int k = 0; k < n; ++k) {
                double aik = a[i * n + k];
                for (int j = 0; j < n; ++j) {
                    c[i * n + j] += aik * b[k * n + j];
                }
            }
        }
    }
    volatile double checksum = 0.0;
    for (int i = 0; i < n * n; ++i) checksum += c[i];
    printf("chiploop_matrix_checksum=%0.3f\n", checksum);
    free(a);
    free(b);
    free(c);
    return 0;
}
'''


def _find_gem5_bin(isa: str, state: Dict[str, Any] | None = None) -> str:
    isa_key = isa.upper()
    profile_key = "gem5_riscv" if isa_key in {"RISCV", "RISC-V"} else "gem5_x86"
    profiled = tool_path(profile_key, state or {})
    if profiled and os.path.isfile(profiled) and os.access(profiled, os.X_OK):
        return profiled
    candidates = [
        os.environ.get(f"GEM5_{isa_key}_BIN"),
        os.environ.get("GEM5_BIN"),
        f"/opt/gem5/build/{isa_key}/gem5.opt",
    ]
    if isa_key == "X86":
        candidates.append("/opt/gem5/build/X86/gem5.opt")
    if isa_key in {"RISCV", "RISC-V"}:
        candidates.append("/opt/gem5/build/RISCV/gem5.opt")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(f"gem5 binary not found for ISA {isa}. Build gem5.opt for that ISA on the backend runner.")


def _compiler_candidates(isa: str) -> List[str]:
    if isa.lower() in {"x86", "x86_64"}:
        return [os.environ.get("CC") or "", "gcc", "clang"]
    return [
        os.environ.get("RISCV_GCC") or "",
        "riscv64-linux-gnu-gcc",
        "riscv64-unknown-linux-gnu-gcc",
    ]


def _compile_matrix_workload(state: Dict[str, Any], isa: str) -> str:
    env_key = "GEM5_RISCV_WORKLOAD" if isa.lower().startswith("riscv") else "GEM5_X86_WORKLOAD"
    workload = state.get("workload_binary") or os.environ.get(env_key) or os.environ.get("GEM5_WORKLOAD")
    if workload:
        workload_path = str(workload)
        if os.path.isfile(workload_path):
            return workload_path
        raise RuntimeError(f"Configured workload binary does not exist: {workload_path}")

    out_dir = Path(_artifact_dir(state)) / "workloads" / isa.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    source = out_dir / "matrix_multiply.c"
    binary = out_dir / "matrix_multiply"
    source.write_text(MATRIX_BENCHMARK_C, encoding="utf-8")
    compiler = next(
        (
            tool_path(c.replace("-", "_"), state) or tool_path(c, state) or c
            for c in _compiler_candidates(isa)
            if c and (tool_path(c.replace("-", "_"), state) or tool_path(c, state))
        ),
        "",
    )
    if not compiler:
        raise RuntimeError(f"No compiler found to build the gem5 {isa} workload binary.")

    base_cmd = [compiler, "-O2", "-DCHIPLOOP_N=24", str(source), "-o", str(binary)]
    static_cmd = base_cmd[:3] + ["-static"] + base_cmd[3:]
    run = run_command(state, "gem5_workload_compile", static_cmd, timeout_sec=120)
    if run.returncode != 0:
        run = run_command(state, "gem5_workload_compile", base_cmd, timeout_sec=120)
    if run.returncode != 0:
        raise RuntimeError(f"Failed to compile gem5 workload for {isa}: {run.stderr[-1200:]}")
    return str(binary)


def _write_gem5_config(state: Dict[str, Any]) -> str:
    path = Path(_artifact_dir(state)) / "chiploop_gem5_se.py"
    path.write_text(GEM5_CONFIG_TEMPLATE, encoding="utf-8")
    return str(path)


def _parse_stat_value(stats: Dict[str, float], names: List[str]) -> float:
    for name in names:
        if name in stats:
            return stats[name]
    for key, value in stats.items():
        if any(name in key for name in names):
            return value
    return 0.0


def _read_gem5_stats(stats_path: str) -> Dict[str, float]:
    stats: Dict[str, float] = {}
    with open(stats_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            clean = line.split("#", 1)[0].strip()
            if not clean or clean.startswith("-"):
                continue
            parts = clean.split()
            if len(parts) < 2:
                continue
            try:
                stats[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return stats


def _metrics_from_gem5_stats(point: Dict[str, Any], stats_path: str) -> Dict[str, float]:
    stats = _read_gem5_stats(stats_path)
    sim_insts = _parse_stat_value(stats, ["simInsts", "system.cpu.committedInsts", "system.cpu.numInsts"])
    cycles = _parse_stat_value(stats, ["system.cpu.numCycles", "system.cpu.numCycles::total", "numCycles"])
    if sim_insts <= 0:
        raise RuntimeError(f"gem5 stats did not include simulated instructions: {stats_path}")
    ipc = (sim_insts / cycles) if cycles > 0 else _parse_stat_value(stats, ["system.cpu.ipc"])
    if ipc <= 0:
        ipc = sim_insts / max(_parse_stat_value(stats, ["simTicks"]), 1.0)
    l1_misses = _parse_stat_value(stats, [
        "system.cpu.dcache.overallMisses::total",
        "system.cpu.dcache.demandMisses::total",
        "system.cpu.dcache.overallMisses",
        "dcache.overallMisses::total",
        "dcache.demandMisses::total",
    ])
    l2_misses = _parse_stat_value(stats, [
        "system.l2cache.overallMisses::total",
        "system.l2cache.demandMisses::total",
        "system.l2cache.overallMisses",
    ])
    l1_mpki = (l1_misses * 1000.0) / sim_insts
    l2_mpki = (l2_misses * 1000.0) / sim_insts
    power, area = _power_area_from_gem5_activity(point, ipc, l1_mpki, l2_mpki)
    return {
        "ipc": round(ipc, 4),
        "cpi": round(1.0 / ipc, 4) if ipc > 0 else 0.0,
        "l1d_mpki": round(l1_mpki, 3),
        "l2_mpki": round(l2_mpki, 3),
        "sim_insts": round(sim_insts, 0),
        "sim_ticks": round(_parse_stat_value(stats, ["simTicks"]), 0),
        "estimated_power_w": power,
        "estimated_area_mm2": area,
        "perf_per_watt": round(ipc / power, 4) if power > 0 else 0.0,
        "perf_per_area": round(ipc / area, 4) if area > 0 else 0.0,
    }


def _power_area_from_gem5_activity(point: Dict[str, Any], ipc: float, l1_mpki: float, l2_mpki: float) -> tuple[float, float]:
    l1_kb = int(point.get("l1d_size_kb") or 32)
    l2_kb = int(point.get("l2_size_kb") or 512)
    cores = max(1, int(point.get("cores") or 1))
    cpu_factor = {"TimingSimpleCPU": 0.85, "MinorCPU": 1.05, "O3CPU": 1.35, "AtomicSimpleCPU": 0.65}.get(str(point.get("cpu_model") or ""), 1.0)
    cache_dynamic = (l1_mpki * 0.0025) + (l2_mpki * 0.009)
    power = (0.42 + ipc * 0.36 + cache_dynamic + l1_kb * 0.0028 + l2_kb * 0.00024) * cores * cpu_factor
    area = (0.22 + l1_kb * 0.0029 + l2_kb * 0.00038) * (0.95 + cores * 0.1) * (1.15 if point.get("cpu_model") == "O3CPU" else 1.0)
    return round(power, 4), round(area, 4)


def _run_gem5_point(state: Dict[str, Any], point: Dict[str, Any], config_path: str) -> Dict[str, Any]:
    isa = str(point.get("isa") or "x86")
    gem5_bin = _find_gem5_bin(isa, state)
    workload = _compile_matrix_workload(state, isa)
    run_dir = Path(_artifact_dir(state)) / "gem5_runs" / str(point.get("run_id") or "run")
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        gem5_bin,
        f"--outdir={run_dir}",
        config_path,
        "--cmd", workload,
        "--cpu-type", str(point.get("cpu_model") or "TimingSimpleCPU"),
        "--num-cpus", str(point.get("cores") or 1),
        "--clock", str(state.get("clock") or "2GHz"),
        "--mem-type", str(point.get("memory_type") or state.get("memory_type") or "DDR3_1600_8x8"),
        "--mem-size", str(state.get("memory_size") or "2GB"),
        "--l1d-size", f"{point.get('l1d_size_kb') or 32}kB",
        "--l1i-size", str(state.get("l1i_size") or "32kB"),
        "--l2-size", f"{point.get('l2_size_kb') or 512}kB",
        "--l1-assoc", str(point.get("l1_assoc") or 2),
        "--l2-assoc", str(point.get("l2_assoc") or 8),
        "--cacheline-size", str(state.get("cache_line_size") or 64),
        "--prefetcher", str(point.get("prefetcher") or "none"),
        "--branch-predictor", str(point.get("branch_predictor") or "default"),
        "--maxinsts", str(state.get("maxinsts") or os.environ.get("GEM5_MAX_INSTS") or 250000),
    ]
    timeout = int(os.environ.get("GEM5_RUN_TIMEOUT_SEC", "180"))
    proc = run_command(state, "gem5_run", cmd, timeout_sec=timeout)
    (run_dir / "stdout.log").write_text(proc.stdout, encoding="utf-8", errors="ignore")
    (run_dir / "stderr.log").write_text(proc.stderr, encoding="utf-8", errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(f"gem5 failed for {point.get('run_id')}: {proc.stderr[-1600:] or proc.stdout[-1600:]}")
    stats_path = run_dir / "stats.txt"
    if not stats_path.exists():
        raise RuntimeError(f"gem5 completed but stats.txt was not produced for {point.get('run_id')}")
    metrics = _metrics_from_gem5_stats(point, str(stats_path))
    return {
        **point,
        **metrics,
        "execution_mode": "gem5",
        "gem5_bin": gem5_bin,
        "workload_binary": workload,
        "stats_path": str(stats_path),
    }


def system_gem5_execution_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System gem5 Execution Agent"
    sweep = state.get("system_architecture_sweep") or {}
    points = sweep.get("sweep_points") or []
    if not points:
        raise RuntimeError("No gem5 sweep points were produced.")
    config_path = _write_gem5_config(state)
    rows = []
    log_lines = ["ChipLoop System Architecture Explorer: real gem5 execution"]
    for point in points:
        row = _run_gem5_point(state, point, config_path)
        rows.append(row)
        log_lines.append(
            f"{row['run_id']} gem5 ipc={row['ipc']} l1d_mpki={row['l1d_mpki']} "
            f"l2_mpki={row['l2_mpki']} stats={row['stats_path']}"
        )
    result = {
        "tool": "gem5",
        "execution_mode": "gem5",
        "note": "Charts are generated from gem5 stats.txt. Power and area are ChipLoop activity estimates driven by parsed gem5 metrics.",
        "runs": rows,
        "created_at": _now(),
    }
    _record(state, agent_name, "gem5_run_results.json", _json(result))
    _record(state, agent_name, "gem5_execution.log", "\n".join(log_lines) + "\n")
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
        "method": "ChipLoop power model driven by parsed gem5 activity metrics from stats.txt.",
        "rows": [{"run_id": r["run_id"], "estimated_power_w": r["estimated_power_w"], "perf_per_watt": r["perf_per_watt"]} for r in rows],
    }
    _record(state, agent_name, "power_estimates.json", _json(estimates))
    return {"system_power_estimates": estimates, "status": "ok"}


def system_area_estimation_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Area Estimation Agent"
    rows = (state.get("system_gem5_results") or {}).get("runs") or []
    estimates = {
        "method": "ChipLoop cache and CPU area model driven by the gem5 run configuration.",
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


def system_architecture_to_rtl_intent_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "System Architecture to RTL Intent Agent"
    gem5_results = state.get("system_gem5_results") or _read_local_json(state, "gem5_run_results.json")
    ppa_summary = state.get("system_ppa_summary") or _read_local_json(state, "ppa_summary.json")
    intent = state.get("system_architecture_intent") or _read_local_json(state, "intent.json")
    charts = state.get("system_architecture_charts") or _read_local_json(state, "charts.json")
    rows = gem5_results.get("runs") or []
    if not rows:
        raise RuntimeError("Cannot create RTL intent because gem5_run_results.json has no run rows.")

    selected_run_id = str(state.get("selected_run_id") or "").strip()
    selected = next((r for r in rows if str(r.get("run_id")) == selected_run_id), None)
    if not selected:
        selected = (ppa_summary.get("recommended_point") or {}) if isinstance(ppa_summary, dict) else {}
    if not selected:
        selected = sorted(rows, key=lambda r: (r.get("perf_per_watt", 0), r.get("perf_per_area", 0), r.get("ipc", 0)), reverse=True)[0]

    reviewer = str(state.get("reviewer") or state.get("user_id") or "chiploop_user")
    workflow_id = _workflow_id(state)
    project_name = str(intent.get("project_name") or state.get("project_name") or "system_architecture_to_rtl")
    top_module = str(state.get("top_module") or f"{project_name}_arch_wrapper").replace("-", "_")
    workload = selected.get("workload") or intent.get("workload") or "matrix_multiply"
    rtl_parameters = {
        "isa": selected.get("isa"),
        "cpu_model": selected.get("cpu_model"),
        "cores": selected.get("cores"),
        "l1d_size_kb": selected.get("l1d_size_kb"),
        "l2_size_kb": selected.get("l2_size_kb"),
        "l1_assoc": selected.get("l1_assoc"),
        "l2_assoc": selected.get("l2_assoc"),
        "prefetcher": selected.get("prefetcher"),
        "branch_predictor": selected.get("branch_predictor"),
        "memory_type": selected.get("memory_type"),
    }
    metrics = {
        "ipc": selected.get("ipc"),
        "l1d_mpki": selected.get("l1d_mpki"),
        "l2_mpki": selected.get("l2_mpki"),
        "estimated_power_w": selected.get("estimated_power_w"),
        "estimated_area_mm2": selected.get("estimated_area_mm2"),
        "perf_per_watt": selected.get("perf_per_watt"),
        "perf_per_area": selected.get("perf_per_area"),
    }
    trace_items = [
        {
            "rtl_parameter": key,
            "value": value,
            "source": {
                "workflow_id": workflow_id,
                "run_id": selected.get("run_id"),
                "artifact": "gem5_run_results.json",
                "workload": workload,
                "metric": "selected_sweep_point",
            },
            "review_status": "generated_from_architecture_evidence",
            "reviewer": reviewer,
        }
        for key, value in rtl_parameters.items()
        if value is not None
    ]
    rtl_intent = {
        "project_name": project_name,
        "top_module": top_module,
        "design_language": "systemverilog",
        "source": {
            "type": "system_architecture_gem5_handoff",
            "workflow_id": workflow_id,
            "selected_run_id": selected.get("run_id"),
            "workload": workload,
        },
        "rtl_scope": {
            "intent": "Create a synthesizable architecture configuration wrapper and control-plane RTL intent package for the selected system architecture point.",
            "non_goals": [
                "Do not implement a full CPU core from gem5.",
                "Do not claim RTL functional correctness from gem5 performance results alone.",
            ],
        },
        "parameters": rtl_parameters,
        "evidence_metrics": metrics,
        "interfaces": {
            "clock": "clk",
            "reset": "reset_n",
            "control": "AXI4-Lite or simple CSR interface for architecture configuration/status",
            "status_outputs": ["selected_run_id", "configuration_valid", "performance_profile_id"],
        },
        "traceability": trace_items,
    }
    rtl_yaml = _simple_yaml(rtl_intent) + "\n"
    decision_md = f"""# Architecture Decision

## Selected Sweep Point
- Workflow: `{workflow_id}`
- Run: `{selected.get('run_id')}`
- Workload: `{workload}`
- ISA: `{selected.get('isa')}`
- CPU model: `{selected.get('cpu_model')}`
- Cores: `{selected.get('cores')}`
- L1D: `{selected.get('l1d_size_kb')}KB`
- L2: `{selected.get('l2_size_kb')}KB`
- Prefetcher: `{selected.get('prefetcher')}`
- Branch predictor: `{selected.get('branch_predictor')}`

## Evidence
- IPC: `{metrics.get('ipc')}`
- L1D MPKI: `{metrics.get('l1d_mpki')}`
- L2 MPKI: `{metrics.get('l2_mpki')}`
- Estimated power: `{metrics.get('estimated_power_w')}W`
- Estimated area: `{metrics.get('estimated_area_mm2')}mm2`

## RTL Interpretation
This handoff converts gem5-backed architecture evidence into an Arch2RTL-ready intent package. It should be reviewed as architecture intent, not as proof of RTL correctness.
"""
    trace_md_lines = ["# Requirements Trace", "", "| RTL parameter | Value | Evidence | Reviewer |", "| --- | --- | --- | --- |"]
    for item in trace_items:
        src = item["source"]
        trace_md_lines.append(
            f"| `{item['rtl_parameter']}` | `{item['value']}` | `{src['artifact']}::{src['run_id']}` | `{reviewer}` |"
        )
    requirements_trace = "\n".join(trace_md_lines) + "\n"
    spec_text = f"""Design a synthesizable SystemVerilog architecture configuration wrapper named `{top_module}`.

The wrapper captures the selected system architecture point from a gem5-backed ChipLoop System Architecture run and exposes the configuration through a simple control/status interface.

Selected architecture:
- ISA: {selected.get('isa')}
- CPU model profile: {selected.get('cpu_model')}
- Cores: {selected.get('cores')}
- L1D cache size: {selected.get('l1d_size_kb')}KB
- L2 cache size: {selected.get('l2_size_kb')}KB
- L1 associativity: {selected.get('l1_assoc')}
- L2 associativity: {selected.get('l2_assoc')}
- Prefetcher profile: {selected.get('prefetcher')}
- Branch predictor profile: {selected.get('branch_predictor')}
- Memory type profile: {selected.get('memory_type')}

Evidence from gem5:
- Workload: {workload}
- Run ID: {selected.get('run_id')}
- IPC: {metrics.get('ipc')}
- L1D MPKI: {metrics.get('l1d_mpki')}
- L2 MPKI: {metrics.get('l2_mpki')}
- Estimated power: {metrics.get('estimated_power_w')}W
- Estimated area: {metrics.get('estimated_area_mm2')}mm2

Generate:
- Parameterized SystemVerilog RTL for the architecture configuration wrapper.
- Clear parameters/localparams for the selected architecture values.
- A CSR/register map for reading the selected configuration and evidence metrics.
- SDC constraints for `clk`.
- UPF-lite power intent.
- A handoff note preserving traceability to workflow `{workflow_id}` and run `{selected.get('run_id')}`.

Do not implement a full CPU core. Treat CPU model, prefetcher, and branch predictor as architecture profile parameters unless additional implementation detail is provided.
"""
    handoff = {
        "rtl_intent": rtl_intent,
        "architecture_decision": decision_md,
        "requirements_trace": requirements_trace,
        "arch2rtl_prefill": {
            "projectName": project_name,
            "topModule": top_module,
            "designLanguage": "systemverilog",
            "specText": spec_text,
            "toggles": {"genRegmap": True, "genUpfLite": True, "genPackaging": True},
        },
        "charts_available": bool(charts),
    }
    _record(state, agent_name, "rtl_intent.yaml", rtl_yaml)
    _record(state, agent_name, "architecture_decision.md", decision_md)
    _record(state, agent_name, "requirements_trace.md", requirements_trace)
    _record(state, agent_name, "arch2rtl_prefill.json", _json(handoff["arch2rtl_prefill"]))
    return {"system_architecture_to_rtl_handoff": handoff, "status": "ok"}
