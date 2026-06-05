import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


CLOCK_NAME_RE = re.compile(r"(^|_)(clk|clock)($|_)", re.IGNORECASE)
RESET_NAME_RE = re.compile(r"(^|_)(rst|reset|resetn|rstn)($|_)", re.IGNORECASE)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _dedupe(items: Iterable[Dict[str, Any]], key: str = "port") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        value = str(item.get(key) or item.get("name") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(item)
    return out


def _period_from_frequency(value: Any) -> Optional[float]:
    try:
        mhz = float(value)
    except Exception:
        return None
    if mhz <= 0:
        return None
    return 1000.0 / mhz


def _frequency_from_period(value: Any) -> Optional[float]:
    try:
        period = float(value)
    except Exception:
        return None
    if period <= 0:
        return None
    return 1000.0 / period


def _normalize_clock(item: Any, *, source: str) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        port = item.strip()
        if not port:
            return None
        return {
            "name": port,
            "port": port,
            "frequency_mhz": None,
            "period_ns": None,
            "source": source,
            "needs_user_frequency": True,
        }
    if not isinstance(item, dict):
        return None
    port = str(item.get("port") or item.get("name") or item.get("clock") or item.get("clock_port") or "").strip()
    if not port:
        return None
    freq = item.get("frequency_mhz", item.get("target_mhz", item.get("target_frequency_mhz")))
    period = item.get("period_ns", item.get("period"))
    if period in (None, ""):
        period = _period_from_frequency(freq)
    if freq in (None, ""):
        freq = _frequency_from_period(period)
    try:
        freq_value = float(freq) if freq not in (None, "") else None
    except Exception:
        freq_value = None
    try:
        period_value = float(period) if period not in (None, "") else None
    except Exception:
        period_value = None
    return {
        "name": str(item.get("name") or port).strip(),
        "port": port,
        "frequency_mhz": freq_value,
        "period_ns": period_value,
        "source": str(item.get("source") or source),
        "needs_user_frequency": period_value is None,
    }


def _normalize_reset(item: Any, *, source: str) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        port = item.strip()
        if not port:
            return None
        return {"name": port, "port": port, "active_low": port.lower().endswith("_n"), "source": source}
    if not isinstance(item, dict):
        return None
    port = str(item.get("port") or item.get("name") or item.get("reset") or item.get("reset_port") or "").strip()
    if not port:
        return None
    active_low = item.get("active_low")
    if active_low is None:
        active_low = port.lower().endswith("_n")
    return {
        "name": str(item.get("name") or port).strip(),
        "port": port,
        "active_low": bool(active_low),
        "source": str(item.get("source") or source),
    }


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            path = Path(value)
            if path.exists() and path.is_file():
                try:
                    parsed = json.loads(path.read_text(encoding="utf-8"))
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}
    return {}


def _spec_clock_candidates(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    clocks: List[Dict[str, Any]] = []
    for key in ("clock_domains", "clocks"):
        value = spec.get(key)
        if isinstance(value, list):
            clocks.extend(filter(None, (_normalize_clock(v, source=f"spec.{key}") for v in value)))
    clock_obj = spec.get("clock")
    if clock_obj:
        clock = _normalize_clock(clock_obj, source="spec.clock")
        if clock:
            clocks.append(clock)
    for key in ("clock_name", "clock_port"):
        if isinstance(spec.get(key), str):
            clock = _normalize_clock(spec[key], source=f"spec.{key}")
            if clock:
                clocks.append(clock)
    return clocks


def _spec_reset_candidates(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    resets: List[Dict[str, Any]] = []
    for key in ("reset_signals", "resets"):
        value = spec.get(key)
        if isinstance(value, list):
            resets.extend(filter(None, (_normalize_reset(v, source=f"spec.{key}") for v in value)))
    for key in ("reset", "reset_name", "reset_port"):
        if isinstance(spec.get(key), str):
            reset = _normalize_reset(spec[key], source=f"spec.{key}")
            if reset:
                resets.append(reset)
    return resets


def _extract_declared_ports(text: str) -> List[Dict[str, str]]:
    clean = _strip_comments(text)
    ports: List[Dict[str, str]] = []
    for match in re.finditer(r"\b(input|output|inout)\b\s+([^;]+?)(?=;|\n\s*(?:input|output|inout)\b|\);)", clean, re.IGNORECASE | re.DOTALL):
        direction = match.group(1).lower()
        body = match.group(2)
        body = re.sub(r"\b(wire|logic|reg|signed|unsigned)\b", " ", body, flags=re.IGNORECASE)
        body = re.sub(r"\[[^\]]+\]", " ", body)
        for token in re.split(r",", body):
            name_match = re.search(r"([A-Za-z_][A-Za-z0-9_$]*)\s*$", token.strip())
            if name_match:
                ports.append({"name": name_match.group(1), "direction": direction})
    return ports


def infer_from_rtl_files(rtl_files: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    clocks: List[Dict[str, Any]] = []
    resets: List[Dict[str, Any]] = []
    for rtl_file in rtl_files:
        try:
            text = Path(str(rtl_file)).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for port in _extract_declared_ports(text):
            if port["direction"] != "input":
                continue
            name = port["name"]
            if CLOCK_NAME_RE.search(name):
                clocks.append({
                    "name": name,
                    "port": name,
                    "frequency_mhz": None,
                    "period_ns": None,
                    "source": "rtl_port_inference",
                    "needs_user_frequency": True,
                })
            elif RESET_NAME_RE.search(name):
                resets.append({
                    "name": name,
                    "port": name,
                    "active_low": name.lower().endswith("_n"),
                    "source": "rtl_port_inference",
                })
    return {"clocks": _dedupe(clocks), "resets": _dedupe(resets)}


def parse_sdc_clocks(sdc_text: str) -> List[Dict[str, Any]]:
    clocks: List[Dict[str, Any]] = []
    for line in sdc_text.splitlines():
        if "create_clock" not in line:
            continue
        period_match = re.search(r"-period\s+([0-9.]+)", line)
        port_match = re.search(r"get_ports\s+\{?([A-Za-z_][A-Za-z0-9_$]*)\}?", line)
        name_match = re.search(r"-name\s+\{?([A-Za-z_][A-Za-z0-9_$]*)\}?", line)
        if not period_match:
            continue
        port = port_match.group(1) if port_match else name_match.group(1) if name_match else ""
        if not port:
            continue
        period = float(period_match.group(1))
        clocks.append({
            "name": name_match.group(1) if name_match else port,
            "port": port,
            "frequency_mhz": _frequency_from_period(period),
            "period_ns": period,
            "source": "sdc",
            "needs_user_frequency": False,
        })
    return _dedupe(clocks)


def normalize_clock_constraints(value: Any) -> Dict[str, Any]:
    if not value:
        return {"clocks": [], "resets": [], "source": "none", "needs_user_review": False}
    if isinstance(value, list):
        clocks = [_normalize_clock(v, source="request.clock_constraints") for v in value]
        return {
            "clocks": _dedupe([c for c in clocks if c]),
            "resets": [],
            "source": "request.clock_constraints",
            "needs_user_review": any((c or {}).get("needs_user_frequency") for c in clocks),
        }
    if not isinstance(value, dict):
        return {"clocks": [], "resets": [], "source": "invalid", "needs_user_review": False}
    clocks = [_normalize_clock(v, source="request.clock_constraints") for v in value.get("clocks") or []]
    resets = [_normalize_reset(v, source="request.clock_constraints") for v in value.get("resets") or []]
    return {
        "clocks": _dedupe([c for c in clocks if c]),
        "resets": _dedupe([r for r in resets if r]),
        "relationships": value.get("relationships") if isinstance(value.get("relationships"), list) else [],
        "source": str(value.get("source") or "request.clock_constraints"),
        "needs_user_review": any((c or {}).get("needs_user_frequency") for c in clocks),
    }


def build_clock_intent(
    *,
    spec: Any = None,
    rtl_files: Optional[Iterable[str]] = None,
    sdc_text: Optional[str] = None,
    requested: Any = None,
    default_frequency_mhz: Optional[float] = None,
) -> Dict[str, Any]:
    requested_intent = normalize_clock_constraints(requested)
    spec_dict = _json_dict(spec)
    spec_clocks = _spec_clock_candidates(spec_dict)
    spec_resets = _spec_reset_candidates(spec_dict)
    rtl = infer_from_rtl_files(rtl_files or [])
    sdc_clocks = parse_sdc_clocks(sdc_text or "")

    clocks = requested_intent["clocks"] or sdc_clocks or spec_clocks or rtl["clocks"]
    resets = requested_intent["resets"] or spec_resets or rtl["resets"]

    if default_frequency_mhz:
        period = _period_from_frequency(default_frequency_mhz)
        patched = []
        for clock in clocks:
            c = dict(clock)
            if c.get("period_ns") is None and period:
                c["frequency_mhz"] = float(default_frequency_mhz)
                c["period_ns"] = period
                c["source"] = f"{c.get('source') or 'inferred'}+default_frequency"
                c["needs_user_frequency"] = False
            patched.append(c)
        clocks = patched

    primary_clock = clocks[0]["port"] if clocks else None
    primary_reset = resets[0]["port"] if resets else None
    return {
        "type": "digital_clock_intent",
        "clocks": _dedupe(clocks),
        "resets": _dedupe(resets),
        "primary_clock": primary_clock,
        "primary_reset": primary_reset,
        "needs_user_review": any(c.get("needs_user_frequency") for c in clocks),
        "provenance": {
            "requested": bool(requested_intent["clocks"]),
            "sdc_clocks": len(sdc_clocks),
            "spec_clocks": len(spec_clocks),
            "rtl_clocks": len(rtl["clocks"]),
            "spec_resets": len(spec_resets),
            "rtl_resets": len(rtl["resets"]),
        },
    }


def sdc_from_clock_intent(intent: Dict[str, Any]) -> Optional[str]:
    clocks = intent.get("clocks") if isinstance(intent, dict) else []
    if not isinstance(clocks, list) or not clocks:
        return None
    lines = ["# Auto-generated from ChipLoop clock intent"]
    for clock in clocks:
        if not isinstance(clock, dict):
            continue
        port = str(clock.get("port") or clock.get("name") or "").strip()
        if not port:
            continue
        period = clock.get("period_ns")
        if period in (None, ""):
            period = _period_from_frequency(clock.get("frequency_mhz"))
        try:
            period_value = float(period)
        except Exception:
            continue
        if period_value <= 0:
            continue
        name = str(clock.get("name") or port).strip()
        lines.append(f"create_clock -name {name} -period {period_value:.3f} [get_ports {{{port}}}]")
    return "\n".join(lines).strip() + "\n" if len(lines) > 1 else None
