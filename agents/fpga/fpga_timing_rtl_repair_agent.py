import json
import os
import re
from pathlib import Path
from typing import Any

from model_gateway import complete_text

from .fpga_common import fpga_dir, manifest_update, publish_json, read_text


def _json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else {}
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(cleaned[start : end + 1])
                return value if isinstance(value, dict) else {}
            except Exception:
                pass
    return {}


def _module_interfaces(text: str) -> dict[str, list[tuple[str, str]]]:
    try:
        from agents.system.system_top_assembly_agent import _extract_module_ports_from_text

        parsed = _extract_module_ports_from_text(text)
    except Exception:
        return {}
    interfaces: dict[str, list[tuple[str, str]]] = {}
    for module, ports in (parsed or {}).items():
        if not isinstance(ports, dict):
            continue
        interfaces[str(module)] = sorted(
            (str(name), str((meta or {}).get("dir") or ""))
            for name, meta in ports.items()
            if isinstance(meta, dict)
        )
    return interfaces


def run_agent(state: dict) -> dict:
    agent = "FPGA Timing RTL Repair Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = Path(fpga_dir(state, "closure", "rtl_repair")).resolve()
    repaired_dir = out_dir / "repaired"
    original_dir = out_dir / "original"
    repaired_dir.mkdir(parents=True, exist_ok=True)
    original_dir.mkdir(parents=True, exist_ok=True)

    rtl_files = [str(path) for path in fpga.get("rtl_files") or [] if os.path.exists(str(path))]
    timing = fpga.get("timing_drc") if isinstance(fpga.get("timing_drc"), dict) else {}
    pnr = fpga.get("place_route") if isinstance(fpga.get("place_route"), dict) else {}
    target = state.get("target_frequency_mhz")
    observed = timing.get("max_frequency_mhz") or pnr.get("max_frequency_mhz")
    report: dict[str, Any] = {
        "agent": agent,
        "enabled": bool(state.get("allow_automatic_rtl_timing_repair")),
        "attempted": False,
        "applied": False,
        "accepted": False,
        "target_frequency_mhz": target,
        "before_max_frequency_mhz": observed,
        "original_rtl_files": rtl_files,
    }
    if not report["enabled"]:
        report["reason"] = "automatic_rtl_repair_disabled"
    elif not rtl_files:
        report["reason"] = "no_rtl_files"
    else:
        sources = []
        original_interfaces_by_file: dict[str, dict[str, list[tuple[str, str]]]] = {}
        basename_counts: dict[str, int] = {}
        for rtl_file in rtl_files:
            basename = os.path.basename(rtl_file)
            basename_counts[basename] = basename_counts.get(basename, 0) + 1
        original_by_name: dict[str, str] = {}
        for index, rtl_file in enumerate(rtl_files):
            content = read_text(rtl_file)
            if not content:
                continue
            basename = os.path.basename(rtl_file) or f"rtl_{index}.sv"
            name = basename if basename_counts.get(basename, 0) == 1 else f"{index}_{basename}"
            (original_dir / name).write_text(content, encoding="utf-8")
            sources.append({"path": name, "original_path": rtl_file, "content": content})
            original_interfaces_by_file[name] = _module_interfaces(content)
            original_by_name[name] = rtl_file
        timing_tail = str(((pnr.get("command") or {}).get("stdout_tail") if isinstance(pnr.get("command"), dict) else "") or "")[-8000:]
        prompt = f"""You are repairing synthesizable FPGA RTL to close timing.
Return JSON only with this schema:
{{"summary":"...","latency_change_cycles":0,"files":[{{"path":"name.v","content":"complete repaired RTL"}}]}}

Rules:
- Preserve every module name and external port name/direction.
- Preserve functional behavior. You may add internal pipeline/register structure only when protocol behavior remains valid.
- Do not add vendor-specific primitives unless already used.
- Keep changes minimal and synthesizable by Yosys.
- Target frequency: {target} MHz. Observed maximum: {observed} MHz.
- Focus on excessive combinational depth, large muxes, arithmetic chains, and high-fanout logic.

Timing evidence:
{timing_tail}

RTL files:
{json.dumps(sources)}
"""
        report["attempted"] = True
        try:
            response = complete_text(prompt, capability="default", temperature=0.1)
        except Exception as exc:
            response = ""
            report["error"] = f"{type(exc).__name__}: {exc}"
        proposal = _json_object(response)
        proposed_files = proposal.get("files") if isinstance(proposal.get("files"), list) else []
        repaired_files: list[str] = []
        interface_valid = True
        changed = 0
        for item in proposed_files:
            if not isinstance(item, dict):
                continue
            name = os.path.basename(str(item.get("path") or ""))
            content = str(item.get("content") or "")
            if not name or name not in original_by_name or not content.strip():
                continue
            if _module_interfaces(content) != original_interfaces_by_file.get(name, {}):
                interface_valid = False
                break
            output = repaired_dir / name
            output.write_text(content.strip() + "\n", encoding="utf-8")
            repaired_files.append(str(output))
            if content.strip() != read_text(original_by_name[name]).strip():
                changed += 1
        if interface_valid and changed > 0 and len(repaired_files) == len(rtl_files):
            report.update({
                "applied": True,
                "summary": proposal.get("summary") or "Automatic timing-focused RTL repair applied.",
                "latency_change_cycles": proposal.get("latency_change_cycles"),
                "repaired_rtl_files": repaired_files,
            })
            state["_fpga_pre_timing_repair_rtl_files"] = list(rtl_files)
            state["fpga_timing_rtl_repair_used"] = True
            fpga["rtl_files"] = repaired_files
            state["rtl_files"] = repaired_files
        else:
            report["reason"] = "proposal_invalid_or_no_semantic_change"
            report["interface_validation_passed"] = interface_valid

    publish_json(state, agent, "closure/rtl_repair", "fpga_timing_rtl_repair.json", report)
    manifest_update(state, "timing_rtl_repair", report)
    return state


def finalize_repair(
    state: dict,
    *,
    accepted: bool,
    after_max_frequency_mhz: float | None,
    timing_met: bool,
    verification_passed: bool | None = None,
    verification_reason: str | None = None,
) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    report = fpga.get("timing_rtl_repair") if isinstance(fpga.get("timing_rtl_repair"), dict) else {}
    report.update({
        "accepted": bool(accepted),
        "after_max_frequency_mhz": after_max_frequency_mhz,
        "timing_met_after_repair": bool(timing_met),
        "outcome": "accepted" if accepted else "rejected_and_original_restored",
        "verification_passed": verification_passed,
        "verification_reason": verification_reason,
    })
    publish_json(state, "FPGA Timing RTL Repair Agent", "closure/rtl_repair", "fpga_timing_rtl_repair.json", report)
    manifest_update(state, "timing_rtl_repair", report)
    return report