"""
ChipLoop Verification & Validation - System Functional Coverage Agent

Design goals:
- system_integration_intent.json is the primary source of truth for system-level verification intent
- soc_top_sim.sv is used to recover exact top-level port widths/signatures when available
- register/control metadata from upstream digital handoff artifacts is consumed when available
- this agent is dedicated to system-level runs (no digital/system mode switching)
- generated coverage collateral remains lightweight, simulation-friendly, and generic
- no hardcoded protocol-specific signal assumptions in generated coverage intent
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


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

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



def _safe_read_text(path: Optional[str]) -> str:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        pass
    return ""



def _safe_read_lines(path: Optional[str]) -> List[str]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        pass
    return []



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



def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)


# -----------------------------------------------------------------------------
# Artifact discovery
# -----------------------------------------------------------------------------

def _find_system_integration_json(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "system_integration_intent.json"),
        os.path.join(workflow_dir, "system_integration_intent.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _find_soc_top_sim_path(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "soc_top_sim.sv"),
        os.path.join(workflow_dir, "soc_top_sim.sv"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _find_system_rtl_filelist(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "system_rtl_filelist_sim.txt"),
        os.path.join(workflow_dir, "system_rtl_filelist_sim.txt"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _find_regmap_json(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "digital", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "digital_regmap.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _collect_rtl_files_from_filelist(path: Optional[str]) -> List[str]:
    files: List[str] = []
    for ln in _safe_read_lines(path):
        if not isinstance(ln, str):
            continue
        p = ln.strip()
        if not p:
            continue
        files.append(os.path.abspath(p))
    return list(dict.fromkeys(files))



def _collect_system_rtl_files(workflow_dir: str) -> List[str]:
    filelist = _find_system_rtl_filelist(workflow_dir)
    if filelist:
        listed = _collect_rtl_files_from_filelist(filelist)
        if listed:
            return listed

    exts = (".v", ".sv", ".vh", ".svh")
    scan_dirs = [
        os.path.join(workflow_dir, "system", "integration"),
        os.path.join(workflow_dir, "digital", "rtl_refactored"),
        os.path.join(workflow_dir, "analog"),
    ]
    rtl: List[str] = []
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    rtl.append(os.path.abspath(os.path.join(root, fn)))
    return sorted(set(rtl))


# -----------------------------------------------------------------------------
# Structural extraction
# -----------------------------------------------------------------------------

def _normalize_direction(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in ("input", "in", "i"):
        return "input"
    if s in ("output", "out", "o"):
        return "output"
    if s in ("inout", "io"):
        return "inout"
    return s



def _range_to_width_expr(msb: Optional[str], lsb: Optional[str]) -> str:
    if msb is None or lsb is None:
        return "1"
    return f"(({msb}) - ({lsb}) + 1)"



def _parse_sv_port_declarations(sv_text: str) -> List[Dict[str, Any]]:
    ports: List[Dict[str, Any]] = []
    seen = set()

    port_re = re.compile(
        r"^\s*(input|output|inout)\s+"
        r"(?:wire|logic|reg\s+)?"
        r"(?:signed\s+)?"
        r"(?:\[(?P<msb>[^:\]]+)\s*:\s*(?P<lsb>[^\]]+)\]\s+)?"
        r"(?P<names>[^;]+?)\s*[,;]\s*$",
        re.IGNORECASE,
    )

    for raw in sv_text.splitlines():
        line = re.sub(r"//.*$", "", raw).strip()
        if not line:
            continue
        m = port_re.match(line)
        if not m:
            continue

        direction = _normalize_direction(m.group(1))
        msb = m.group("msb")
        lsb = m.group("lsb")
        width_expr = _range_to_width_expr(msb.strip(), lsb.strip()) if msb and lsb else "1"

        names_blob = m.group("names")
        for part in names_blob.split(","):
            name = part.strip()
            if not name:
                continue
            name = re.sub(r"\s*=.*$", "", name).strip()
            name = re.sub(r"\[[^\]]+\]", "", name).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            ports.append(
                {
                    "name": name,
                    "direction": direction,
                    "width_expr": width_expr,
                }
            )

    return ports



def _ports_from_integration_json(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    ports: Dict[str, Dict[str, Any]] = {}

    for conn in intent.get("connections", []):
        if not isinstance(conn, dict):
            continue
        src = str(conn.get("from", "")).strip()
        dst = str(conn.get("to", "")).strip()

        if src.startswith("top."):
            name = src.split(".", 1)[1].split("[", 1)[0]
            ports.setdefault(name, {"name": name, "direction": "input", "width_expr": "1"})
        if dst.startswith("top."):
            name = dst.split(".", 1)[1].split("[", 1)[0]
            ports.setdefault(name, {"name": name, "direction": "output", "width_expr": "1"})

    return [ports[k] for k in sorted(ports.keys())]



def _merge_ports_with_sv_widths(base_ports: List[Dict[str, Any]], sv_ports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    for p in base_ports:
        nm = str(p.get("name", "")).strip()
        if not nm:
            continue
        merged[nm] = dict(p)
        merged[nm].setdefault("width_expr", "1")

    for p in sv_ports:
        nm = str(p.get("name", "")).strip()
        if not nm:
            continue
        if nm in merged:
            merged[nm]["direction"] = p.get("direction") or merged[nm].get("direction")
            merged[nm]["width_expr"] = p.get("width_expr") or merged[nm].get("width_expr", "1")
        else:
            merged[nm] = dict(p)

    return [merged[k] for k in sorted(merged.keys())]



def _infer_clocks_resets(ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    for p in ports:
        nm = str(p.get("name", ""))
        if re.search(r"(?:^|_)(clk|clock)(?:$|_)", nm, re.IGNORECASE):
            clocks.append(nm)
        if re.search(r"(?:^|_)(rst|reset)(?:$|_)", nm, re.IGNORECASE):
            resets.append(
                {
                    "name": nm,
                    "active_low": bool(re.search(r"(_n$|^rst_n$|^reset_n$|^por_n$)", nm, re.IGNORECASE)),
                    "async": False,
                }
            )

    clocks = list(dict.fromkeys([c for c in clocks if c.strip()]))

    uniq_resets: List[Dict[str, Any]] = []
    seen = set()
    for rr in resets:
        nm = rr.get("name")
        if nm and nm not in seen:
            seen.add(nm)
            uniq_resets.append(rr)

    return clocks, uniq_resets



def _derive_system_top_module(intent: Dict[str, Any], soc_top_text: str) -> str:
    sim_module = ((intent.get("top") or {}).get("sim_module") or "").strip()
    if sim_module:
        return sim_module

    m = re.search(r"\bmodule\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b", soc_top_text)
    if m:
        return m.group(1)
    return "soc_top_sim"



def _collect_connection_names(intent: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for conn in intent.get("connections", []):
        if not isinstance(conn, dict):
            continue
        for key in ("from", "to"):
            ep = str(conn.get(key, "")).strip()
            if "." not in ep:
                continue
            obj, sig = ep.split(".", 1)
            if obj == "top":
                continue
            sig = sig.split("[", 1)[0].strip()
            if sig:
                names.append(sig)
    return sorted(set(names))


# -----------------------------------------------------------------------------
# Regmap interpretation
# -----------------------------------------------------------------------------

def _parse_int_like(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        try:
            if s.startswith("0x"):
                return int(s, 16)
            return int(s, 10)
        except Exception:
            return default
    return default



def _field_mask(field: Dict[str, Any]) -> int:
    lsb = _parse_int_like(field.get("lsb"), 0)
    msb = _parse_int_like(field.get("msb"), lsb)
    width = max(msb - lsb + 1, 1)
    return ((1 << width) - 1) << lsb



def _regmap_summary(regmap_obj: Dict[str, Any]) -> Dict[str, Any]:
    regmap = regmap_obj.get("regmap") or {}
    regs = regmap.get("registers") or []

    reg_points: List[Dict[str, Any]] = []
    field_points: List[Dict[str, Any]] = []

    for reg in regs:
        if not isinstance(reg, dict):
            continue
        reg_name = str(reg.get("name", "")).strip()
        if not reg_name:
            continue

        reg_points.append(
            {
                "name": reg_name,
                "offset": reg.get("offset", "0x00"),
                "access": str(reg.get("access", "RW")).upper(),
            }
        )

        for field in reg.get("fields") or []:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name", "")).strip()
            if not field_name:
                continue
            field_points.append(
                {
                    "register": reg_name,
                    "name": field_name,
                    "access": str(field.get("access", reg.get("access", "RW"))).upper(),
                    "mask": hex(_field_mask(field)),
                    "lsb": _parse_int_like(field.get("lsb"), 0),
                    "msb": _parse_int_like(field.get("msb"), _parse_int_like(field.get("lsb"), 0)),
                    "reset": _parse_int_like(field.get("reset"), 0),
                }
            )

    return {
        "has_regmap": bool(reg_points),
        "bus": str(regmap.get("bus", "")).strip(),
        "addr_width": _parse_int_like(regmap.get("addr_width"), 0),
        "data_width": _parse_int_like(regmap.get("data_width"), 0),
        "register_points": reg_points[:32],
        "field_points": field_points[:64],
    }


# -----------------------------------------------------------------------------
# Contract resolution
# -----------------------------------------------------------------------------

def _resolve_cov_contract(state: Dict[str, Any], workflow_dir: str, log_path: str) -> Dict[str, Any]:
    integration_json_path = (
        state.get("system_integration_intent_json")
        or state.get("integration_json_path")
        or _find_system_integration_json(workflow_dir)
    )
    soc_top_sim_path = (
        state.get("soc_top_sim_path")
        or state.get("system_top_sim_path")
        or _find_soc_top_sim_path(workflow_dir)
    )
    system_rtl_filelist_path = (
        state.get("system_rtl_filelist_sim")
        or state.get("system_rtl_filelist_path")
        or _find_system_rtl_filelist(workflow_dir)
    )
    regmap_json_path = (
        state.get("digital_regmap_json")
        or state.get("regmap_json")
        or _find_regmap_json(workflow_dir)
    )

    rtl_files = _collect_rtl_files_from_filelist(system_rtl_filelist_path)
    rtl_source = "system_rtl_filelist_sim"
    if not rtl_files:
        rtl_files = _collect_system_rtl_files(workflow_dir)
        rtl_source = "fallback_scan"

    intent = _safe_read_json(integration_json_path)
    soc_top_text = _safe_read_text(soc_top_sim_path)
    top_module = (
        state.get("soc_top_sim_module")
        or state.get("soc_top_name")
        or _derive_system_top_module(intent, soc_top_text)
    )

    contract = {
        "integration_json_path": integration_json_path,
        "soc_top_sim_path": soc_top_sim_path,
        "system_rtl_filelist_path": system_rtl_filelist_path,
        "regmap_json_path": regmap_json_path,
        "rtl_files": rtl_files,
        "rtl_source": rtl_source,
        "top_module": top_module,
    }

    _log_kv(
        log_path,
        "resolved_contract",
        {
            "integration_json_path": integration_json_path,
            "soc_top_sim_path": soc_top_sim_path,
            "system_rtl_filelist_path": system_rtl_filelist_path,
            "regmap_json_path": regmap_json_path,
            "rtl_source": rtl_source,
            "rtl_file_count": len(rtl_files),
            "top_module": top_module,
        },
    )
    return contract


# -----------------------------------------------------------------------------
# Coverage point construction
# -----------------------------------------------------------------------------

def _build_coverage_points(
    intent: Dict[str, Any],
    soc_top_text: str,
    regmap_obj: Dict[str, Any],
    top_module: str,
) -> Dict[str, Any]:
    integration_ports = _ports_from_integration_json(intent)
    sv_ports = _parse_sv_port_declarations(soc_top_text)
    top_ports = _merge_ports_with_sv_widths(integration_ports, sv_ports)

    clocks, resets = _infer_clocks_resets(top_ports)
    regmap_summary = _regmap_summary(regmap_obj)

    input_points: List[Dict[str, Any]] = []
    output_points: List[Dict[str, Any]] = []

    for p in top_ports:
        direction = _normalize_direction(p.get("direction"))
        point = {
            "name": str(p.get("name", "")).strip(),
            "direction": direction,
            "width_expr": str(p.get("width_expr", "1")),
        }
        if not point["name"]:
            continue
        if direction == "output":
            output_points.append(point)
        elif direction in ("input", "inout"):
            input_points.append(point)

    input_points = input_points[:24]
    output_points = output_points[:24]

    connection_names = _collect_connection_names(intent)[:64]

    return {
        "top_module": top_module,
        "input_points": input_points,
        "output_points": output_points,
        "clock_names": clocks,
        "reset_names": [r["name"] for r in resets],
        "integration_connection_names": connection_names,
        "register_model": regmap_summary,
    }


# -----------------------------------------------------------------------------
# Generated collateral
# -----------------------------------------------------------------------------

def _gen_coverage_model(cov_spec: Dict[str, Any]) -> str:
    top = cov_spec["top_module"]

    template = '''"""Auto-generated lightweight system functional coverage model for {top}

Generated by ChipLoop System Functional Coverage Agent.
- Coverage points are derived from system integration intent and soc_top_sim.sv
- Optional register/control model coverage is derived from upstream digital regmap artifacts
- No hardcoded protocol-specific DUT signal names are required by the public contract
- Works without cocotb-coverage; uses lightweight hit tracking plus optional register event hooks
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
        self.register_hits = {{
            p["name"]: {{"writes": 0, "reads": 0, "offset": p.get("offset"), "access": p.get("access")}}
            for p in (COVERAGE_SPEC.get("register_model", {{}}).get("register_points", []) or [])
        }}
        self.field_hits = {{
            f"{{p['register']}}.{{p['name']}}": {{"writes_non_reset": 0, "reads": 0, "mask": p.get("mask"), "reset": p.get("reset", 0)}}
            for p in (COVERAGE_SPEC.get("register_model", {{}}).get("field_points", []) or [])
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
                    bucket.setdefault("bin_zero", 0)
                    bucket.setdefault("bin_nonzero", 0)
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

    def note_register_write(self, reg_name: str, value: int = 0):
        if reg_name in self.register_hits:
            self.register_hits[reg_name]["writes"] += 1

        for point in (COVERAGE_SPEC.get("register_model", {{}}).get("field_points", []) or []):
            if point.get("register") != reg_name:
                continue
            key = f"{{point['register']}}.{{point['name']}}"
            try:
                mask = int(str(point.get("mask", "0x0")), 16)
            except Exception:
                mask = 0
            reset = int(point.get("reset", 0) or 0)
            if key not in self.field_hits:
                continue
            field_value = (int(value) & mask)
            if field_value != (reset << int(point.get("lsb", 0) or 0)):
                self.field_hits[key]["writes_non_reset"] += 1

    def note_register_read(self, reg_name: str, value: int = 0):
        if reg_name in self.register_hits:
            self.register_hits[reg_name]["reads"] += 1
        for point in (COVERAGE_SPEC.get("register_model", {{}}).get("field_points", []) or []):
            if point.get("register") != reg_name:
                continue
            key = f"{{point['register']}}.{{point['name']}}"
            if key in self.field_hits:
                self.field_hits[key]["reads"] += 1

    def _summarize_io_group(self, source):
        group = {{}}
        total_bins = 0
        hit_bins = 0
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
            group[name] = {{
                "samples": data.get("samples", 0),
                "seen_values": sorted(list(data.get("seen_values", set()))),
                "hit_bins": local_hit,
                "total_bins": local_total,
            }}
        return group, hit_bins, total_bins

    def _summarize_registers(self):
        group = {{}}
        total_bins = 0
        hit_bins = 0
        for name, data in self.register_hits.items():
            local_total = 0
            local_hit = 0
            access = str(data.get("access", "")).upper()
            if "W" in access:
                local_total += 1
                if data.get("writes", 0) > 0:
                    local_hit += 1
            if "R" in access:
                local_total += 1
                if data.get("reads", 0) > 0:
                    local_hit += 1
            if local_total == 0:
                local_total = 1
                if (data.get("writes", 0) + data.get("reads", 0)) > 0:
                    local_hit = 1
            total_bins += local_total
            hit_bins += local_hit
            group[name] = {{
                "offset": data.get("offset"),
                "access": access,
                "writes": data.get("writes", 0),
                "reads": data.get("reads", 0),
                "hit_bins": local_hit,
                "total_bins": local_total,
            }}
        return group, hit_bins, total_bins

    def _summarize_fields(self):
        group = {{}}
        total_bins = 0
        hit_bins = 0
        for name, data in self.field_hits.items():
            local_total = 2
            local_hit = 0
            if data.get("writes_non_reset", 0) > 0:
                local_hit += 1
            if data.get("reads", 0) > 0:
                local_hit += 1
            total_bins += local_total
            hit_bins += local_hit
            group[name] = {{
                "writes_non_reset": data.get("writes_non_reset", 0),
                "reads": data.get("reads", 0),
                "hit_bins": local_hit,
                "total_bins": local_total,
            }}
        return group, hit_bins, total_bins

    def summary(self):
        outputs, out_hit, out_total = self._summarize_io_group(self.output_hits)
        inputs, in_hit, in_total = self._summarize_io_group(self.input_hits)
        registers, reg_hit, reg_total = self._summarize_registers()
        fields, field_hit, field_total = self._summarize_fields()

        total_bins = out_total + in_total + reg_total + field_total
        hit_bins = out_hit + in_hit + reg_hit + field_hit
        pct = round(100.0 * hit_bins / max(total_bins, 1), 2)

        return {{
            "type": "system_functional_coverage_summary",
            "top_module": COVERAGE_SPEC.get("top_module"),
            "functional_coverage_pct": pct,
            "bins_hit": hit_bins,
            "total_bins": total_bins,
            "outputs": outputs,
            "inputs": inputs,
            "registers": registers,
            "fields": fields,
            "clock_names": COVERAGE_SPEC.get("clock_names", []),
            "reset_names": COVERAGE_SPEC.get("reset_names", []),
            "integration_connection_names": COVERAGE_SPEC.get("integration_connection_names", []),
            "register_model": {{
                "has_regmap": bool(COVERAGE_SPEC.get("register_model", {{}}).get("has_regmap")),
                "bus": COVERAGE_SPEC.get("register_model", {{}}).get("bus"),
                "addr_width": COVERAGE_SPEC.get("register_model", {{}}).get("addr_width"),
                "data_width": COVERAGE_SPEC.get("register_model", {{}}).get("data_width"),
            }},
        }}

    def stop(self):
        self.stopped = True

    def write_reports(self):
        summary = self.summary()

        json_path = os.path.join(REPORT_DIR, "functional_coverage_summary.json")
        md_path = os.path.join(REPORT_DIR, "COVERAGE.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        lines = []
        lines.append("# System Functional Coverage Summary")
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
        lines.append("")
        lines.append("## Registers")
        for name, data in summary.get("registers", {{}}).items():
            lines.append(f"- {{name}}: reads={{data.get('reads')}}, writes={{data.get('writes')}}, bins={{data.get('hit_bins')}}/{{data.get('total_bins')}}")
        lines.append("")
        lines.append("## Fields")
        for name, data in summary.get("fields", {{}}).items():
            lines.append(f"- {{name}}: writes_non_reset={{data.get('writes_non_reset')}}, reads={{data.get('reads')}}, bins={{data.get('hit_bins')}}/{{data.get('total_bins')}}")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\\n".join(lines) + "\\n")
'''
    return template.format(
        top=top,
        coverage_spec_json=json.dumps(cov_spec, indent=2),
    )


# -----------------------------------------------------------------------------
# Agent entry point
# -----------------------------------------------------------------------------

def run_agent(state: dict) -> dict:
    agent_name = "System Functional Coverage Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "system_functional_coverage_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System Functional Coverage Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    contract = _resolve_cov_contract(state, workflow_dir, log_path)
    integration_json_path = contract["integration_json_path"]
    soc_top_sim_path = contract["soc_top_sim_path"]
    regmap_json_path = contract["regmap_json_path"]
    rtl_files = contract["rtl_files"]
    top_module = contract["top_module"]

    intent = _safe_read_json(integration_json_path)
    soc_top_text = _safe_read_text(soc_top_sim_path)
    regmap_obj = _safe_read_json(regmap_json_path)

    cov_spec = _build_coverage_points(intent, soc_top_text, regmap_obj, top_module)

    _log(log_path, f"integration_json_path={integration_json_path}")
    _log(log_path, f"soc_top_sim_path={soc_top_sim_path}")
    _log(log_path, f"regmap_json_path={regmap_json_path}")
    _log(log_path, f"top_module={top_module}")
    _log(log_path, f"rtl_file_count={len(rtl_files)}")
    _log_kv(log_path, "clock_names", cov_spec.get("clock_names", []))
    _log_kv(log_path, "reset_names", cov_spec.get("reset_names", []))
    _log_kv(
        log_path,
        "coverage_points",
        {
            "input_points": [p["name"] for p in cov_spec.get("input_points", [])],
            "output_points": [p["name"] for p in cov_spec.get("output_points", [])],
            "register_points": [p["name"] for p in (cov_spec.get("register_model", {}).get("register_points", []) or [])],
            "field_points": [f"{p['register']}.{p['name']}" for p in (cov_spec.get("register_model", {}).get("field_points", []) or [])],
        },
    )

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    reports_dir = os.path.join(tb_root, "reports")
    os.makedirs(tb_root, exist_ok=True)
    os.makedirs(reports_dir, exist_ok=True)

    cov_model_py = _gen_coverage_model(cov_spec)
    cov_spec_txt = json.dumps(cov_spec, indent=2)

    readme = """# System Functional Coverage (Cocotb)

Generated:
- `coverage_model.py`       : lightweight system functional coverage sampler
- `coverage_spec.json`      : resolved coverage points derived from integration + top RTL + regmap
- `coverage_generation_report.json` : concise agent output report

Usage (in cocotb tests):
- cov = CoverageModel()
- cov.start(dut)
- cov.sample() at transaction boundaries or checkpoints
- cov.note_register_write(reg_name, value) after software-visible writes when available
- cov.note_register_read(reg_name, value) after software-visible reads when available
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
        "type": "system_functional_coverage_generation",
        "version": "1.0",
        "top_module": top_module,
        "integration_json_path": integration_json_path,
        "soc_top_sim_path": soc_top_sim_path,
        "regmap_json_path": regmap_json_path,
        "rtl_file_count": len(rtl_files),
        "clock_names": cov_spec.get("clock_names", []),
        "reset_names": cov_spec.get("reset_names", []),
        "generated_dir": "vv/tb",
        "reports_dir": "vv/tb/reports",
        "runtime_reports_expected": [
            "vv/tb/reports/functional_coverage_summary.json",
            "vv/tb/reports/COVERAGE.md",
        ],
        "coverage_points": {
            "input_points": [p["name"] for p in cov_spec.get("input_points", [])],
            "output_points": [p["name"] for p in cov_spec.get("output_points", [])],
            "register_points": [p["name"] for p in (cov_spec.get("register_model", {}).get("register_points", []) or [])],
            "field_points": [f"{p['register']}.{p['name']}" for p in (cov_spec.get("register_model", {}).get("field_points", []) or [])],
        },
        "register_model": {
            "has_regmap": bool(cov_spec.get("register_model", {}).get("has_regmap")),
            "bus": cov_spec.get("register_model", {}).get("bus"),
            "addr_width": cov_spec.get("register_model", {}).get("addr_width"),
            "data_width": cov_spec.get("register_model", {}).get("data_width"),
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
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "system_functional_coverage_agent.log", log_text)

    state.setdefault("vv", {})
    state["vv"]["coverage"] = report
    state["vv"]["coverage_spec"] = cov_spec

    state["coverage_model_py"] = os.path.join(tb_root, "coverage_model.py")
    state["coverage_spec_json"] = os.path.join(tb_root, "coverage_spec.json")
    state["functional_coverage_summary_json"] = os.path.join(reports_dir, "functional_coverage_summary.json")
    state["functional_coverage_md"] = os.path.join(reports_dir, "COVERAGE.md")

    _log(log_path, f"{agent_name} completed successfully.")
    return state
