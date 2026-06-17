import json
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict

from tooling.runner import run_command
from agents.analog.analog_abstract_views_agent import _build_lib_stub
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Liberty Characterization Agent"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds", "generate_gds"}


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _pdk_root_host(state: dict) -> str:
    return os.path.abspath(str(
        state.get("pdk_root_host")
        or os.getenv("CHIPLOOP_PDK_ROOT_HOST")
        or "/root/chiploop-backend/backend/pdk"
    ))


def _prepare_ngspice_spice(src: str, dst: str) -> None:
    text = open(src, "r", encoding="utf-8", errors="ignore").read()
    lines = []
    has_sky130_lib = bool(re.search(r"^\s*\.lib\s+.*sky130\.lib\.spice\"?\s+tt\b", text, flags=re.IGNORECASE | re.MULTILINE))
    for line in text.splitlines():
        if has_sky130_lib and re.search(r"^\s*\.include\s+.*sky130\.lib\.spice\"?", line, flags=re.IGNORECASE):
            continue
        lines.append(line)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def _contract_spec(state: dict, module_name: str) -> Dict[str, Any]:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    ports = contract.get("ports") if isinstance(contract.get("ports"), list) else []
    merged: Dict[str, Dict[str, Any]] = {}

    def width_value(value: Any) -> int:
        if isinstance(value, int):
            return max(value, 1)
        text = str(value or "").strip()
        if not text or text.lower() == "unknown":
            return 1
        if text.isdigit():
            return max(int(text), 1)
        match = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", text)
        if match:
            return abs(int(match.group(1)) - int(match.group(2))) + 1
        return 1

    def norm_direction(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"input", "output", "inout"}:
            return text
        text = str(value or "").strip().upper()
        if text in {"INPUT", "OUTPUT", "INOUT"}:
            return text.lower()
        return ""

    for port in ports:
        if not isinstance(port, dict) or not port.get("name"):
            continue
        raw_name = str(port.get("name") or "").strip()
        bit_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_$]*)\[(\d+)\]", raw_name)
        name = bit_match.group(1) if bit_match else raw_name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            continue
        entry = merged.setdefault(name, {"name": name, "direction": "", "width": 1})
        direction = (
            norm_direction(port.get("verilog_direction"))
            or norm_direction(port.get("direction"))
            or norm_direction(port.get("lef_direction"))
            or norm_direction(port.get("lib_direction"))
        )
        if direction and not entry["direction"]:
            entry["direction"] = direction
        if bit_match:
            entry["width"] = max(int(entry["width"]), int(bit_match.group(2)) + 1)
        else:
            entry["width"] = max(int(entry["width"]), width_value(port.get("width")))
    normalized = []
    for item in merged.values():
        name = str(item["name"])
        low = name.lower()
        if low in {"vpwr", "vdd", "avdd", "dvdd"} or "vdd" in low or "pwr" in low:
            item["direction"] = "inout"
        elif low in {"vgnd", "vss", "avss", "dvss", "gnd"} or "vss" in low or "gnd" in low:
            item["direction"] = "inout"
        elif not item["direction"]:
            item["direction"] = "input"
        normalized.append(item)
    return {
        "module_name": contract.get("macro_name") or module_name,
        "ports": sorted(normalized, key=lambda p: str(p.get("name") or "")),
        "clock_period_ns": 1000.0,
        "area_um2": _macro_area_um2(state),
    }


def _macro_area_um2(state: dict) -> float | None:
    for value in (
        state.get("analog_macro_area_um2"),
        state.get("macro_area_um2"),
    ):
        try:
            if value is not None:
                return max(float(value), 1.0)
        except Exception:
            pass
    lef_path = state.get("analog_macro_lef")
    if not isinstance(lef_path, str) or not os.path.exists(lef_path):
        return None
    text = open(lef_path, "r", encoding="utf-8", errors="ignore").read()
    match = re.search(r"\bSIZE\s+([0-9]+(?:\.[0-9]+)?)\s+BY\s+([0-9]+(?:\.[0-9]+)?)\s*;", text, flags=re.IGNORECASE)
    if not match:
        return None
    return max(float(match.group(1)) * float(match.group(2)), 1.0)


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "lib_char")
    os.makedirs(stage_dir, exist_ok=True)

    module_name = _module_name(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    prior_lib = state.get("analog_macro_lib")
    pdk_root_host = _pdk_root_host(state)
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": module_name,
        "spice": spice_path,
        "prior_lib": prior_lib,
        "pdk_root_host": pdk_root_host,
    }

    if not _enabled(state):
        summary.update({"status": "skipped", "reason": "analog_physical_mode_not_generate_gds"})
    elif not isinstance(spice_path, str) or not os.path.exists(spice_path):
        summary.update({"status": "failed", "reason": "analog_spice_missing"})
    else:
        ngspice_bin = shutil.which("ngspice")
        deck_path = os.path.join(stage_dir, "characterize_ngspice.sp")
        staged_spice = os.path.join(stage_dir, os.path.basename(spice_path) or f"{module_name}.spice")
        if os.path.abspath(spice_path) != os.path.abspath(staged_spice):
            _prepare_ngspice_spice(spice_path, staged_spice)
        else:
            _prepare_ngspice_spice(spice_path, staged_spice)
        deck = "\n".join([
            f'.include "{staged_spice}"',
            "* Placeholder characterization deck. Real Liberty requires block-specific stimuli and measurements.",
            ".control",
            "op",
            "write op.raw",
            "quit",
            ".endc",
            ".end",
            "",
        ])
        with open(deck_path, "w", encoding="utf-8") as fh:
            fh.write(deck)

        if not ngspice_bin:
            summary.update({
                "status": "failed",
                "reason": "ngspice_not_installed",
                "characterization_deck": deck_path,
                "note": "Liberty characterization did not run because ngspice is not installed or not on PATH.",
            })
        else:
            cp = run_command(
                state,
                "analog_lib_char_ngspice",
                [ngspice_bin, "-b", deck_path],
                cwd=stage_dir,
                timeout_sec=1800,
                env={"PDK_ROOT": pdk_root_host},
            )
            log = (cp.stdout or "") + (cp.stderr or "")
            log_path = os.path.join(stage_dir, "ngspice_characterization.log")
            with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(log)
            if cp.returncode == 0:
                normalized_lib = ""
                normalized_lib_path = ""
                contract_spec = _contract_spec(state, module_name)
                if contract_spec.get("ports"):
                    normalized_lib = _build_lib_stub(contract_spec)
                    if normalized_lib:
                        normalized_lib_path = os.path.join(stage_dir, f"{module_name}.lib")
                        with open(normalized_lib_path, "w", encoding="utf-8") as fh:
                            fh.write(normalized_lib)

                effective_lib = normalized_lib_path if normalized_lib_path else prior_lib
                if isinstance(effective_lib, str) and os.path.exists(effective_lib):
                    state["analog_macro_lib"] = effective_lib
                    digital = state.setdefault("digital", {})
                    if isinstance(digital, dict):
                        digital["macro_libs"] = list(dict.fromkeys((digital.get("macro_libs") or []) + [effective_lib]))
                summary.update({
                    "status": "validated",
                    "reason": "liberty_not_produced",
                    "return_code": cp.returncode,
                    "log": log_path,
                    "characterization_deck": deck_path,
                    "generated_liberty": False,
                    "generated_abstract_liberty": bool(normalized_lib_path),
                    "lib": effective_lib if isinstance(effective_lib, str) and os.path.exists(effective_lib) else "",
                    "note": "ngspice ran successfully. No measured Liberty was produced. The flow records SPICE validation and uses a conservative abstract Liberty for macro PnR if a macro interface contract is available.",
                })
            else:
                summary.update({
                    "status": "failed",
                    "reason": "ngspice_failed",
                    "return_code": cp.returncode,
                    "log": log_path,
                    "characterization_deck": deck_path,
                    "log_tail": log[-2000:],
                })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "liberty_characterization_summary.json", json.dumps(summary, indent=2))
    if os.path.exists(os.path.join(stage_dir, "characterize_ngspice.sp")):
        with open(os.path.join(stage_dir, "characterize_ngspice.sp"), "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "characterize_ngspice.sp", fh.read())
    if os.path.exists(os.path.join(stage_dir, "ngspice_characterization.log")):
        with open(os.path.join(stage_dir, "ngspice_characterization.log"), "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "ngspice_characterization.log", fh.read())
    lib_path = summary.get("lib")
    if isinstance(lib_path, str) and os.path.exists(lib_path) and os.path.dirname(os.path.abspath(lib_path)) == os.path.abspath(stage_dir):
        with open(lib_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", os.path.basename(lib_path), fh.read())

    state["analog_liberty_characterization"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if _enabled(state) and summary["status"] not in {"generated", "validated"}:
        detail = summary.get("log_tail") or ""
        raise RuntimeError(
            f"Analog Liberty characterization failed: {summary.get('reason') or summary['status']}"
            + (f"\nngspice log tail:\n{detail}" if detail else "")
        )
    return state
