"""
ChipLoop Verification & Validation - Digital Functional Coverage Agent

Design goals:
- spec_json / digital_spec_json is the primary source of truth
- no hardcoded DUT signal names
- support both digital-only and future system/SoC runs
- log decisions to backend logger and persistent artifact log
- generate lightweight, simulation-friendly functional coverage collateral
"""

import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger("chiploop")


def _now() -> str:
    return datetime.now().isoformat()


def _log(path: str, msg: str, level: str = "info") -> None:
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{_now()}] [{level.upper()}] {msg}\n")


def _log_kv(path: str, key: str, value: Any, level: str = "info") -> None:
    try:
        rendered = json.dumps(value, indent=2, default=str)
    except Exception:
        rendered = str(value)
    _log(path, f"{key}={rendered}", level=level)


def _safe_read_json(path: Optional[str]) -> Dict[str, Any]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
    except Exception:
        pass
    return {}


def _ensure_dirs(workflow_id: str, workflow_dir: str) -> Tuple[str, str]:
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)
    return workflow_id, workflow_dir


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _record_text(
    workflow_id: str,
    agent_name: str,
    subdir: str,
    filename: str,
    content: str,
) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None



def _collect_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv", ".vh", ".svh")

    handoff_dirs = [
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "rtl"),
        os.path.join(workflow_dir, "handoff", "rtl"),
    ]

    for d in handoff_dirs:
        if not os.path.isdir(d):
            continue

        rtl: List[str] = []
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    rtl.append(os.path.abspath(os.path.join(root, fn)))

        rtl = sorted(set(rtl))
        if rtl:
            return rtl

    return []


def _find_fallback_spec_json(workflow_dir: str) -> Optional[str]:
    preferred: List[str] = []
    fallback: List[str] = []

    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            if not fn.endswith("_spec.json") and "spec" not in fn.lower():
                continue

            path = os.path.join(root, fn)
            norm = path.replace("\\", "/").lower()

            if "/digital/" in norm:
                preferred.append(path)
            elif "/analog/" in norm:
                continue
            else:
                fallback.append(path)

    if preferred:
        preferred.sort()
        return preferred[0]
    if fallback:
        fallback.sort()
        return fallback[0]
    return None


def _pick_top_module(spec: Dict[str, Any], rtl_files: List[str], state_top: Optional[str]) -> str:
    top = (spec.get("top_module") or {}).get("name")
    if isinstance(top, str) and top.strip():
        return top.strip()

    hierarchy = spec.get("hierarchy") or {}
    top2 = hierarchy.get("top_module")
    if isinstance(top2, dict):
        nm = top2.get("name")
        if isinstance(nm, str) and nm.strip():
            return nm.strip()
    elif isinstance(top2, str) and top2.strip():
        return top2.strip()

    if state_top and isinstance(state_top, str) and state_top.strip():
        return state_top.strip()

    mod_re = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b")
    for f in rtl_files:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = mod_re.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            continue

    return "top"


def _ports_from_spec(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    tm = spec.get("top_module") or {}
    ports = tm.get("ports")
    if isinstance(ports, list) and ports:
        return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    hierarchy = spec.get("hierarchy") or {}
    htop = hierarchy.get("top_module")
    if isinstance(htop, dict):
        ports = htop.get("ports")
        if isinstance(ports, list) and ports:
            return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    ports = spec.get("ports")
    if isinstance(ports, list) and ports:
        return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    io = spec.get("io")
    if isinstance(io, dict):
        for dkey, direction in [("inputs", "input"), ("outputs", "output"), ("inouts", "inout")]:
            arr = io.get(dkey)
            if isinstance(arr, list):
                for p in arr:
                    if isinstance(p, dict) and p.get("name"):
                        q = dict(p)
                        q.setdefault("direction", direction)
                        out.append(q)

    return out


def _normalize_direction(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in ("input", "in", "i"):
        return "input"
    if s in ("output", "out", "o"):
        return "output"
    if s in ("inout", "io"):
        return "inout"
    return s


def _port_width_expr(port: Dict[str, Any]) -> str:
    width = port.get("width")
    msb = port.get("msb")
    lsb = port.get("lsb")
    rng = port.get("range")

    if isinstance(width, int) and width >= 1:
        return str(width)
    if isinstance(width, str) and width.strip():
        return width.strip()
    if msb is not None and lsb is not None:
        return f"(({msb}) - ({lsb}) + 1)"
    if isinstance(rng, str):
        m = re.match(r"\[\s*(.+?)\s*:\s*(.+?)\s*\]", rng.strip())
        if m:
            return f"(({m.group(1)}) - ({m.group(2)}) + 1)"
    return "1"


def _infer_clocks_resets(spec: Dict[str, Any], ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    clk_spec = spec.get("clocks") or (spec.get("clocking") or {}).get("clocks")
    if isinstance(clk_spec, list):
        for c in clk_spec:
            if isinstance(c, dict) and c.get("name"):
                clocks.append(str(c["name"]))
            elif isinstance(c, str):
                clocks.append(c)

    rst_spec = spec.get("resets") or (spec.get("reset") or {})
    if isinstance(rst_spec, list):
        for r in rst_spec:
            if isinstance(r, dict) and r.get("name"):
                resets.append(dict(r))
            elif isinstance(r, str):
                resets.append({"name": r})
    elif isinstance(rst_spec, dict) and rst_spec.get("name"):
        resets.append(dict(rst_spec))

    if not clocks:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"(?:^|_)(clk|clock)(?:$|_)", nm, re.IGNORECASE):
                clocks.append(nm)

    if not resets:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"(?:^|_)(rst|reset)(?:$|_)", nm, re.IGNORECASE):
                resets.append({"name": nm})

    clocks = [c for c in clocks if isinstance(c, str) and c.strip()]
    clocks = list(dict.fromkeys(clocks))

    norm_resets: List[Dict[str, Any]] = []
    nm = str(r.get("name"))
    is_name_active_low = bool(re.search(r"(rst_n|reset_n|por_n)", nm, re.IGNORECASE))
    for r in resets:
        if isinstance(r, dict) and r.get("name"):
            rr = {
                "name": str(r.get("name")),
                "active_low": bool(
                    r.get("active_low", False)
                    or is_name_active_low
                    or str(r.get("polarity", "")).lower() in ("active_low", "low", "0")
                ),
                "async": bool(
                    str(r.get("type", "")).lower() in ("async", "asynchronous")
                    or r.get("async", False)
                ),
            }
            norm_resets.append(rr)

    uniq_resets: List[Dict[str, Any]] = []
    seen = set()
    for rr in norm_resets:
        if rr["name"] not in seen:
            seen.add(rr["name"])
            uniq_resets.append(rr)

    return clocks, uniq_resets


def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)


def _resolve_cov_mode(state: Dict[str, Any]) -> str:
    if (
        state.get("soc_top_sim_module")
        or state.get("soc_top_name")
        or state.get("system_integration_intent_json")
        or state.get("soc_top_sim_path")
    ):
        return "system"
    return "digital"


def _resolve_cov_contract(state: Dict[str, Any], workflow_dir: str, log_path: str) -> Dict[str, Any]:
    mode = _resolve_cov_mode(state)

    spec_path = state.get("spec_json") or state.get("digital_spec_json")
    spec_source = "state"
    if not spec_path:
        spec_path = _find_fallback_spec_json(workflow_dir)
        spec_source = "fallback_scan"

    spec = _safe_read_json(spec_path)

    rtl_files = state.get("rtl_files")
    rtl_source = "state.rtl_files"
    if not isinstance(rtl_files, list) or not rtl_files:
        rtl_files = _collect_rtl_files(workflow_dir)
        rtl_source = "fallback_scan"

    rtl_files = [os.path.abspath(p) for p in rtl_files if isinstance(p, str)]

    if mode == "system":
        top = (
            state.get("soc_top_sim_module")
            or state.get("soc_top_name")
            or _pick_top_module(spec, rtl_files, state.get("top_module"))
        )
    else:
        top = state.get("top_module") or _pick_top_module(spec, rtl_files, state.get("top_module"))

    contract = {
        "mode": mode,
        "spec_path": spec_path,
        "spec_source": spec_source,
        "rtl_files": rtl_files,
        "rtl_source": rtl_source,
        "top_module": top,
        "soc_mode": bool(mode == "system"),
    }

    _log_kv(
        log_path,
        "resolved_contract",
        {
            "mode": contract["mode"],
            "spec_path": contract["spec_path"],
            "spec_source": contract["spec_source"],
            "rtl_source": contract["rtl_source"],
            "rtl_file_count": len(contract["rtl_files"]),
            "top_module": contract["top_module"],
            "soc_mode": contract["soc_mode"],
        },
    )
    return contract


def _build_coverage_points(
    spec: Dict[str, Any],
    top: str,
    soc_mode: bool = False,
) -> Dict[str, Any]:
    ports = [] if soc_mode else _ports_from_spec(spec)

    output_points: List[Dict[str, Any]] = []
    input_points: List[Dict[str, Any]] = []

    for p in ports:
        name = p.get("name")
        direction = _normalize_direction(p.get("direction"))
        if not name:
            continue

        point = {
            "name": str(name),
            "direction": direction,
            "width_expr": _port_width_expr(p),
        }

        if direction == "output":
            output_points.append(point)
        elif direction in ("input", "inout"):
            input_points.append(point)

    output_points = output_points[:16]
    input_points = input_points[:16]

    return {
        "top_module": top,
        "output_points": output_points,
        "input_points": input_points,
    }


def _gen_coverage_model(cov_spec: Dict[str, Any]) -> str:
    top = cov_spec["top_module"]

    template = '''"""Auto-generated lightweight functional coverage model for {top}

Generated by ChipLoop Functional Coverage Agent.
- Coverage points are derived from spec-declared DUT ports
- No hardcoded DUT signal names
- Works without cocotb-coverage; uses lightweight hit tracking
"""

import json
import os

TB_ROOT = os.path.dirname(__file__)
REPORT_DIR = os.path.join(TB_ROOT, "reports")
os.makedirs(REPORT_DIR, exist_ok=True)


COVERAGE_SPEC = {coverage_spec_json}


def _safe_width_int(sig, width_expr: str) -> int:
    try:
        return max(int(width_expr), 1)
    except Exception:
        try:
            return max(len(sig.value.binstr), 1)
        except Exception:
            return 1


class CoverageModel:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.output_hits = {{
            p["name"]: {{"seen_values": set(), "samples": 0}}
            for p in COVERAGE_SPEC.get("output_points", [])
        }}
        self.input_hits = {{
            p["name"]: {{"seen_values": set(), "samples": 0}}
            for p in COVERAGE_SPEC.get("input_points", [])
        }}

    def start(self, dut=None):
        self.started = True
        self.dut = dut

    def _sample_group(self, dut, points, target):
        for p in points:
            name = p.get("name")
            if not name or not hasattr(dut, name):
                continue
            try:
                sig = getattr(dut, name)
                width = _safe_width_int(sig, str(p.get("width_expr", "1")))
                value = int(sig.value)
                bucket = target[name]
                bucket["samples"] += 1
                bucket["seen_values"].add(value)

                if width == 1:
                    bucket.setdefault("bin_0", 0)
                    bucket.setdefault("bin_1", 0)
                    if value == 0:
                        bucket["bin_0"] += 1
                    elif value == 1:
                        bucket["bin_1"] += 1
                else:
                    bucket.setdefault("bin_nonzero", 0)
                    bucket.setdefault("bin_zero", 0)
                    if value == 0:
                        bucket["bin_zero"] += 1
                    else:
                        bucket["bin_nonzero"] += 1
            except Exception:
                continue

    def sample(self):
        dut = getattr(self, "dut", None)
        if dut is None:
            return
        self._sample_group(dut, COVERAGE_SPEC.get("output_points", []), self.output_hits)
        self._sample_group(dut, COVERAGE_SPEC.get("input_points", []), self.input_hits)

    def stop(self):
        self.stopped = True

    def summary(self):
        total_bins = 0
        hit_bins = 0
        outputs = {{}}
        inputs = {{}}

        for _, source, target in [
            ("outputs", self.output_hits, outputs),
            ("inputs", self.input_hits, inputs),
        ]:
            for name, data in source.items():
                local_total = 0
                local_hit = 0

                if "bin_0" in data:
                    local_total += 2
                    if data.get("bin_0", 0) > 0:
                        local_hit += 1
                    if data.get("bin_1", 0) > 0:
                        local_hit += 1
                else:
                    local_total += 2
                    if data.get("bin_zero", 0) > 0:
                        local_hit += 1
                    if data.get("bin_nonzero", 0) > 0:
                        local_hit += 1

                total_bins += local_total
                hit_bins += local_hit

                target[name] = {{
                    "samples": data.get("samples", 0),
                    "seen_values": sorted(list(data.get("seen_values", set()))),
                    "hit_bins": local_hit,
                    "total_bins": local_total,
                }}

        pct = round(100.0 * hit_bins / max(total_bins, 1), 2)

        return {{
            "type": "functional_coverage_summary",
            "top_module": COVERAGE_SPEC.get("top_module"),
            "functional_coverage_pct": pct,
            "bins_hit": hit_bins,
            "total_bins": total_bins,
            "outputs": outputs,
            "inputs": inputs,
        }}

    def write_reports(self):
        summary = self.summary()

        json_path = os.path.join(REPORT_DIR, "functional_coverage_summary.json")
        md_path = os.path.join(REPORT_DIR, "COVERAGE.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        lines = []
        lines.append("# Functional Coverage Summary")
        lines.append("")
        lines.append(f"- Top module: {{summary.get('top_module')}}")
        lines.append(f"- Functional coverage: {{summary.get('functional_coverage_pct')}}%")
        lines.append(f"- Bins hit: {{summary.get('bins_hit')}}")
        lines.append(f"- Total bins: {{summary.get('total_bins')}}")
        lines.append("")
        lines.append("## Outputs")
        for name, data in summary.get("outputs", {{}}).items():
            lines.append(f"- {{name}}: samples={{data.get('samples')}}, bins={{data.get('hit_bins')}}/{{data.get('total_bins')}}")
        lines.append("")
        lines.append("## Inputs")
        for name, data in summary.get("inputs", {{}}).items():
            lines.append(f"- {{name}}: samples={{data.get('samples')}}, bins={{data.get('hit_bins')}}/{{data.get('total_bins')}}")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\\n".join(lines) + "\\n")
'''
    return template.format(
        top=top,
        coverage_spec_json=json.dumps(cov_spec, indent=2),
    )

def run_agent(state: dict) -> dict:
    agent_name = "Digital Functional Coverage Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "functional_coverage_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Digital Functional Coverage Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    contract = _resolve_cov_contract(state, workflow_dir, log_path)
    mode = contract["mode"]
    spec_path = contract["spec_path"]
    rtl_files = contract["rtl_files"]
    top = contract["top_module"]
    soc_mode = contract["soc_mode"]

    spec = _safe_read_json(spec_path)
    ports = [] if soc_mode else _ports_from_spec(spec)
    clocks, resets = _infer_clocks_resets(spec, ports)
    cov_spec = _build_coverage_points(spec, top, soc_mode=soc_mode)

    _log(log_path, f"resolved_mode={mode}")
    _log(log_path, f"spec_path={spec_path}")
    _log(log_path, f"top_module={top}")
    _log(log_path, f"rtl_file_count={len(rtl_files)}")
    _log_kv(log_path, "clock_candidates", clocks)
    _log_kv(log_path, "reset_candidates", resets)
    _log_kv(
        log_path,
        "coverage_points",
        {
            "output_points": [p["name"] for p in cov_spec["output_points"]],
            "input_points": [p["name"] for p in cov_spec["input_points"]],
        },
    )

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    reports_dir = os.path.join(tb_root, "reports")
    os.makedirs(tb_root, exist_ok=True)
    os.makedirs(reports_dir, exist_ok=True)

    cov_model_py = _gen_coverage_model(cov_spec)
    cov_spec_txt = json.dumps(cov_spec, indent=2)

    readme = """# Functional Coverage (Cocotb)

Generated:
- `coverage_model.py`       : lightweight functional coverage sampler
- `coverage_spec.json`      : resolved coverage points derived from spec
- `coverage_generation_report.json` : concise agent output report

Usage (in cocotb tests):
- cov = CoverageModel()
- cov.start(dut)
- cov.sample() at transaction boundaries or checkpoints
- cov.stop()
- cov.write_reports() at end of sim

Reports emitted by CoverageModel:
- `reports/functional_coverage_summary.json`
- `reports/COVERAGE.md`
"""

    _write_file(os.path.join(tb_root, "coverage_model.py"), cov_model_py)
    _write_file(os.path.join(tb_root, "coverage_spec.json"), cov_spec_txt)
    _write_file(os.path.join(tb_root, "COVERAGE.md"), readme)

    _log(log_path, "Generated coverage_model.py")
    _log(log_path, "Generated coverage_spec.json")
    _log(log_path, "Generated COVERAGE.md")

    artifacts: Dict[str, Any] = {}
    artifacts["coverage_model_py"] = _record_text(workflow_id, agent_name, "vv/tb", "coverage_model.py", cov_model_py)
    artifacts["coverage_spec_json"] = _record_text(workflow_id, agent_name, "vv/tb", "coverage_spec.json", cov_spec_txt)
    artifacts["coverage_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "COVERAGE.md", readme)

    report = {
        "type": "functional_coverage_generation",
        "version": "1.1",
        "mode": mode,
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "clock_names": clocks,
        "reset_names": [r["name"] for r in resets],
        "generated_dir": "vv/tb",
        "reports_dir": "vv/tb/reports",
        "runtime_reports_expected": [
            "vv/tb/reports/functional_coverage_summary.json",
            "vv/tb/reports/COVERAGE.md",
        ],
        "coverage_points": {
            "output_points": [p["name"] for p in cov_spec["output_points"]],
            "input_points": [p["name"] for p in cov_spec["input_points"]],
        },
        "tools_detected": {
            "python": True,
            "verilator": bool(_which("verilator")),
        },
        "artifacts": artifacts,
    }

    rep_txt = json.dumps(report, indent=2)
    _write_file(os.path.join(tb_root, "coverage_generation_report.json"), rep_txt)
    artifacts["report"] = _record_text(workflow_id, agent_name, "vv/tb", "coverage_generation_report.json", rep_txt)

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()
    except Exception:
        log_text = ""
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "functional_coverage_agent.log", log_text)

    state.setdefault("vv", {})
    state["vv"]["coverage"] = report
    state["vv"]["coverage_spec"] = cov_spec



    state["coverage_model_py"] = os.path.join(tb_root, "coverage_model.py")
    state["coverage_spec_json"] = os.path.join(tb_root, "coverage_spec.json")
    state["functional_coverage_summary_json"] = os.path.join(reports_dir, "functional_coverage_summary.json")
    state["functional_coverage_md"] = os.path.join(reports_dir, "COVERAGE.md")

    _log(log_path, f"{agent_name} completed successfully.")
    return state

