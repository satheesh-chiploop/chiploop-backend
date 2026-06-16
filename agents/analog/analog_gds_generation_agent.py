import json
import os
import re
import shlex
import shutil
from datetime import datetime
from typing import Any, Dict

from model_gateway import complete_text
from agents.analog.analog_sky130_spice_netlist_agent import (
    _generated_spice_layout_issues,
    _normalize_subckt_bus_pins,
    _port_specs,
)
from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog GDS Generation Agent"
ALIGN_DOCKER_IMAGE = "darpaalign/align-public:latest"
OPENLANE_DOCKER_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    pdk = str(state.get("analog_pdk") or state.get("pdk") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds"} or (mode == "generate_gds" and pdk.startswith("sky130"))


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _find_gds(root: str) -> str:
    hits = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".gds"):
                hits.append(os.path.join(dirpath, name))
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0] if hits else ""


def _prepare_magic_spice(src: str, dst: str) -> None:
    text = open(src, "r", encoding="utf-8", errors="ignore").read()
    text = _magic_spice_text(text)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(text)


def _magic_spice_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = match.group(2)
        suffix = match.group(3).lower()
        number = float(value)
        if suffix in {"u", "um"}:
            number = number
        elif suffix == "n":
            number = number / 1000.0
        elif suffix == "m":
            number = number * 1000.0
        if key.lower() == "w":
            number = max(number, 0.42)
        elif key.lower() == "l":
            number = max(number, 0.15)
        return f"{key}={number:g}"

    # Magic's Sky130 SPICE importer expects generator dimensions in microns and rejects
    # devices below Sky130 generator minima.
    text = re.sub(r"\b([WLwl])\s*=\s*([0-9]+(?:\.[0-9]+)?)(u|um|n|m)\b", repl, text)
    text = re.sub(
        r"\b([WLwl])\s*=\s*([0-9]+(?:\.[0-9]+)?)\b",
        lambda m: f"{m.group(1)}={max(float(m.group(2)), 0.42 if m.group(1).lower() == 'w' else 0.15):g}",
        text,
    )
    return text


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _has_real_sky130_mos(text: str) -> bool:
    return bool(re.search(
        r"^\s*M\S*\s+\S+\s+\S+\s+\S+\s+\S+\s+sky130_fd_pr__(?:nfet|pfet)_\S+(?=.*\bW\s*=)(?=.*\bL\s*=)",
        text or "",
        flags=re.IGNORECASE | re.MULTILINE,
    ))


def _repair_magic_feedback_spice(
    state: dict,
    *,
    module_name: str,
    original_spice: str,
    magic_log: str,
    feedback_text: str = "",
) -> str:
    prompt = f"""
Repair this generated Sky130 transistor-level SPICE so Magic can generate DRC-clean analog GDS.

Module/subckt name:
{module_name}

Magic log tail:
{_tail(magic_log, 5000)}

Magic feedback entries, if available:
{_tail(feedback_text, 5000)}

Original SPICE:
{original_spice}

Strict requirements:
- Return repaired SPICE text only. No Markdown.
- Preserve exactly one .subckt named {module_name}.
- Keep the same external macro intent and pin names.
- If a port is a bus, use either scalar bus pins or bit pins, not both.
- Do not use input pins as MOS drain/source/bulk terminals; input pins may drive MOS gates only.
- Supply pins may be MOS source/bulk terminals, not MOS drain terminals.
- Output pins may be MOS drain/source terminals.
- Use only M-device Sky130 MOS lines with sky130_fd_pr__nfet_01v8 and sky130_fd_pr__pfet_01v8.
- Do not use X lines for MOS devices.
- Every MOS device must have W >= 0.42u and L >= 0.15u.
- Prefer simple inverter/buffer style topology that Magic can place without geometry feedback problems.
- End with .ends {module_name}.
"""
    repaired = _strip_code_fences(complete_text(
        prompt,
        capability="analog_generation",
        agent_name=AGENT_NAME,
        state=state,
    ))
    return repaired if _has_real_sky130_mos(repaired) else ""


def _repair_lvs_mismatch_spice(
    state: dict,
    *,
    module_name: str,
    original_spice: str,
    lvs_summary: Dict[str, Any],
    lvs_log: str = "",
    extracted_spice: str = "",
) -> str:
    prompt = f"""
Repair this generated Sky130 transistor-level SPICE so Magic-generated layout can pass analog LVS.

Module/subckt name:
{module_name}

LVS summary:
{json.dumps(lvs_summary, indent=2)}

Netgen LVS log tail:
{_tail(lvs_log, 6000)}

Extracted layout SPICE tail, if available:
{_tail(extracted_spice, 4000)}

Original source SPICE:
{original_spice}

Strict requirements:
- Return repaired SPICE text only. No Markdown.
- Preserve exactly one .subckt named {module_name}.
- Preserve the same external macro pin names and pin order unless the source had duplicate scalar/bus aliases.
- For buses, use one convention consistently: scalarized bit pins like data[0] data[1], not both data and data[0].
- Every external .subckt pin must be represented by Magic as a real port/label after import.
- Do not use input pins as MOS drain/source/bulk terminals; input pins may drive MOS gates only.
- Supply pins may be MOS source/bulk terminals, not MOS drain terminals.
- Output pins may be MOS drain/source terminals.
- Use only M-device Sky130 MOS lines with sky130_fd_pr__nfet_01v8 and sky130_fd_pr__pfet_01v8.
- Do not use X lines for MOS devices.
- Every MOS device must have W >= 0.42u and L >= 0.15u.
- Keep the circuit compact but do not collapse a many-device source into a placeholder.
- End with .ends {module_name}.
"""
    repaired = _strip_code_fences(complete_text(
        prompt,
        capability="analog_generation",
        agent_name=AGENT_NAME,
        state=state,
    ))
    return repaired if _has_real_sky130_mos(repaired) else ""


def _preserve_and_clean_magic_layout_outputs(stage_dir: str, module_name: str) -> None:
    for name in os.listdir(stage_dir):
        lower = name.lower()
        if not (lower.endswith(".mag") or lower.endswith(".gds")):
            continue
        path = os.path.join(stage_dir, name)
        if not os.path.isfile(path):
            continue
        root, ext = os.path.splitext(name)
        preserved = os.path.join(stage_dir, f"{root}_pass1{ext}")
        if root == module_name and not os.path.exists(preserved):
            shutil.copy2(path, preserved)
        os.remove(path)


def _docker_available() -> bool:
    return bool(shutil.which("docker"))


def _tail(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _magic_layout_invalid(log: str) -> str:
    lowered = (log or "").lower()
    if _magic_feedback_problem_count(log):
        return "magic_feedback_problems"
    if "mos length must be" in lowered or "mos width must be" in lowered:
        return "magic_device_parameter_errors"
    if "generating output for cell /work/" in lowered or "cell /work/" in lowered:
        return "magic_path_qualified_top_cell"
    final_box = re.search(r"CHIPLOOP_FINAL_BOX=([^\n\r]*)", log or "")
    if final_box and re.search(r"\b0(?:\.0+)?\s+0(?:\.0+)?\s+0(?:\.0+)?\s+0(?:\.0+)?\b", final_box.group(1)):
        return "magic_zero_area_layout"
    if final_box and not final_box.group(1).strip():
        return "magic_placeholder_layout"
    return ""


def _magic_feedback_problem_count(log: str) -> int:
    matches = re.findall(r"(\d+)\s+problems?\s+occurred", log or "", flags=re.IGNORECASE)
    return max((int(value) for value in matches), default=0)


def _analog_signoff_summary(
    summary: Dict[str, Any],
    *,
    log_path: str | None = None,
    log: str = "",
    gds_path: str | None = None,
    invalid_reason: str = "",
    analog_lvs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    feedback_count = _magic_feedback_problem_count(log) if summary.get("backend") == "magic" else 0
    gds_status = str(summary.get("status") or "")
    blocked = gds_status != "generated"
    drc_status = "blocked"
    if not blocked:
        drc_status = "violations_found" if feedback_count else "clean"
    elif invalid_reason == "magic_feedback_problems" or feedback_count:
        drc_status = "violations_found"

    return {
        "workflow_id": summary.get("workflow_id"),
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": summary.get("module"),
        "pdk": summary.get("pdk"),
        "backend": summary.get("backend"),
        "gds": gds_path or summary.get("gds"),
        "log": log_path or summary.get("log"),
        "drc": {
            "status": drc_status,
            "tool": "magic" if summary.get("backend") == "magic" else summary.get("backend"),
            "feedback_problem_count": feedback_count,
            "reason": invalid_reason or summary.get("reason") or None,
        },
        "lvs": analog_lvs or {
            "status": "blocked" if blocked else "not_configured",
            "tool": "netgen",
            "reason": "gds_not_clean" if drc_status == "violations_found" else "analog_lvs_agent_not_configured",
        },
        "xor": {
            "status": "not_applicable",
            "tool": "magic/klayout",
            "difference_count": None,
            "reason": "analog_xor_not_applicable",
        },
    }


def _publish_analog_signoff(workflow_id: str, state: dict, summary: Dict[str, Any]) -> None:
    state["analog_signoff"] = summary
    save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        "analog/signoff",
        "analog_signoff_summary.json",
        json.dumps(summary, indent=2),
    )


def _analog_lvs_status(text: str, rc: int) -> str:
    lower = (text or "").lower()
    if "circuits match uniquely" in lower or "netlists match uniquely" in lower:
        return "clean"
    if (
        "failed pin matching" in lower
        or "pin matching failed" in lower
        or "top level cell failed" in lower
        or "netlists do not match" in lower
        or "circuits do not match" in lower
        or "property errors were found" in lower
    ):
        return "mismatch"
    return "completed" if rc == 0 else "failed"


def _analog_lvs_circuit_counts(text: str) -> Dict[str, Any]:
    counts: Dict[str, Any] = {}
    dev_match = re.search(
        r"Circuit\s+1\s+contains\s+(\d+)\s+devices?,\s*Circuit\s+2\s+contains\s+(\d+)\s+devices?",
        text or "",
        flags=re.IGNORECASE,
    )
    if dev_match:
        counts["extracted_devices"] = int(dev_match.group(1))
        counts["source_devices"] = int(dev_match.group(2))
    net_match = re.search(
        r"Circuit\s+1\s+contains\s+(\d+)\s+nets?,\s*Circuit\s+2\s+contains\s+(\d+)\s+nets?",
        text or "",
        flags=re.IGNORECASE,
    )
    if net_match:
        counts["extracted_nets"] = int(net_match.group(1))
        counts["source_nets"] = int(net_match.group(2))
    return counts


def _analog_lvs_failure_class(text: str) -> str:
    lower = (text or "").lower()
    if "failed pin matching" in lower or "pin matching failed" in lower or "top level cell failed" in lower:
        return "pin_mismatch"
    counts = _analog_lvs_circuit_counts(text)
    if counts.get("extracted_devices") is not None and counts.get("source_devices") is not None:
        if counts["extracted_devices"] != counts["source_devices"]:
            return "device_count_mismatch"
    if "netlists do not match" in lower or "circuits do not match" in lower:
        return "netlist_mismatch"
    if "property errors were found" in lower:
        return "property_mismatch"
    return ""


def _analog_lvs_should_repair(summary: Dict[str, Any]) -> bool:
    if summary.get("status") != "mismatch":
        return False
    failure_class = str(summary.get("failure_class") or "")
    if failure_class in {"pin_mismatch", "device_count_mismatch", "netlist_mismatch"}:
        return True
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    extracted = counts.get("extracted_devices")
    source = counts.get("source_devices")
    return isinstance(extracted, int) and isinstance(source, int) and extracted != source


def _lvs_pin_name(token: str) -> str:
    value = str(token or "").strip().strip('"').strip("'").strip("{}")
    value = value.replace("\\[", "[").replace("\\]", "]")
    return value


def _prepare_lvs_source_spice(src: str, dst: str, module_name: str) -> None:
    text = open(src, "r", encoding="utf-8", errors="ignore").read()
    lines = text.splitlines()
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if stripped.lower().startswith(".lib "):
            idx += 1
            continue
        if stripped.lower().startswith(".subckt "):
            subckt_lines = [stripped]
            idx += 1
            while idx < len(lines) and lines[idx].lstrip().startswith("+"):
                subckt_lines.append(lines[idx].lstrip()[1:].strip())
                idx += 1
            try:
                parts = shlex.split(" ".join(subckt_lines))
            except Exception:
                parts = " ".join(subckt_lines).split()
            pins: list[str] = []
            seen: set[str] = set()
            for raw in parts[2:]:
                pin = _lvs_pin_name(raw)
                if not pin or pin in seen:
                    continue
                seen.add(pin)
                pins.append(pin)
            out.append(".subckt " + module_name + " " + " ".join(pins))
            continue
        out.append(line.replace('"', ""))
        idx += 1
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip() + "\n")


def _write_magic_extract_tcl(stage_dir: str, module_name: str, pdk_variant: str, pdk_root_host: str, use_docker: bool) -> str:
    tech_tcl = _magic_paths(pdk_variant)[1] if use_docker else _host_magic_paths(pdk_root_host, pdk_variant)[1]
    out_spice = f"{module_name}_extracted.spice" if use_docker else os.path.join(stage_dir, f"{module_name}_extracted.spice")
    lines = [
        "drc off",
        f"source {tech_tcl}",
        f"load {module_name}",
        "extract all",
        "ext2spice lvs",
        "ext2spice cthresh 999999",
        "ext2spice extresist off",
        f"ext2spice -o {out_spice}",
        "quit -noprompt",
        "",
    ]
    path = os.path.join(stage_dir, "magic_extract_lvs.tcl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def _run_analog_lvs(
    state: dict,
    *,
    stage_dir: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    source_spice: str,
    docker_bin: str | None,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "status": "not_configured",
        "tool": "netgen",
        "reason": "analog_lvs_tool_not_available",
    }
    mag_path = os.path.join(stage_dir, f"{module_name}.mag")
    if not os.path.exists(mag_path):
        return {**summary, "status": "not_run", "reason": "magic_layout_mag_missing"}

    extract_tcl = _write_magic_extract_tcl(stage_dir, module_name, pdk_variant, pdk_root_host, bool(docker_bin))
    extract_log_path = os.path.join(stage_dir, "analog_lvs_magic_extract.log")
    extracted_spice = os.path.join(stage_dir, f"{module_name}_extracted.spice")
    if docker_bin:
        extract_cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "magic",
            "-dnull",
            "-noconsole",
            "-T",
            _magic_paths(pdk_variant)[0],
            "/work/magic_extract_lvs.tcl",
        ]
    else:
        magic_bin = shutil.which("magic")
        if not magic_bin:
            return summary
        host_tech, _host_tcl = _host_magic_paths(pdk_root_host, pdk_variant)
        extract_cmd = [magic_bin, "-dnull", "-noconsole", "-T", host_tech, extract_tcl]

    cp = run_command(state, "analog_magic_lvs_extract", extract_cmd, cwd=stage_dir, timeout_sec=1800)
    extract_log = (cp.stdout or "") + (cp.stderr or "")
    with open(extract_log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(extract_log)
    if cp.returncode not in (0, None) or not os.path.exists(extracted_spice):
        return {
            "status": "failed",
            "tool": "magic/netgen",
            "reason": "magic_extract_failed",
            "return_code": cp.returncode,
            "extracted_spice": extracted_spice if os.path.exists(extracted_spice) else None,
            "extract_log": extract_log_path,
        }

    setup_path = f"/pdk/{pdk_variant}/libs.tech/netgen/{pdk_variant}_setup.tcl"
    lvs_log_path = os.path.join(stage_dir, "analog_lvs_netgen.log")
    lvs_source_spice = os.path.join(stage_dir, f"{module_name}_lvs_source.spice")
    _prepare_lvs_source_spice(source_spice, lvs_source_spice, module_name)
    if docker_bin:
        source_name = os.path.basename(lvs_source_spice)
        lvs_cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "netgen",
            "-batch",
            "lvs",
            f"{module_name}_extracted.spice {module_name}",
            f"{source_name} {module_name}",
            setup_path,
            "analog_lvs_netgen.out",
        ]
    else:
        netgen_bin = shutil.which("netgen")
        if not netgen_bin:
            return {
                "status": "not_configured",
                "tool": "netgen",
                "reason": "netgen_not_available",
                "extracted_spice": extracted_spice,
                "extract_log": extract_log_path,
            }
        host_setup = os.path.join(pdk_root_host, pdk_variant, "libs.tech", "netgen", f"{pdk_variant}_setup.tcl")
        lvs_cmd = [
            netgen_bin,
            "-batch",
            "lvs",
            f"{extracted_spice} {module_name}",
            f"{lvs_source_spice} {module_name}",
            host_setup,
            os.path.join(stage_dir, "analog_lvs_netgen.out"),
        ]

    cp_lvs = run_command(state, "analog_netgen_lvs", lvs_cmd, cwd=stage_dir, timeout_sec=1800)
    lvs_log = (cp_lvs.stdout or "") + (cp_lvs.stderr or "")
    with open(lvs_log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(lvs_log)
    status = _analog_lvs_status(lvs_log, cp_lvs.returncode if cp_lvs.returncode is not None else 1)
    counts = _analog_lvs_circuit_counts(lvs_log)
    failure_class = _analog_lvs_failure_class(lvs_log)
    return {
        "status": status,
        "tool": "netgen",
        "reason": None if status == "clean" else status,
        "return_code": cp_lvs.returncode,
        "extracted_spice": extracted_spice,
        "extract_log": extract_log_path,
        "log": lvs_log_path,
        "counts": counts or None,
        "failure_class": failure_class or None,
    }


def _resolve_pdk_variant(state: dict) -> str:
    return str(
        state.get("pdk_variant")
        or state.get("analog_pdk")
        or state.get("pdk")
        or os.getenv("CHIPLOOP_PDK_VARIANT")
        or "sky130A"
    ).strip()


def _resolve_pdk_root_host(state: dict) -> str:
    pdk_root = (
        state.get("pdk_root_host")
        or os.getenv("CHIPLOOP_PDK_ROOT_HOST")
        or "/root/chiploop-backend/backend/pdk"
    )
    pdk_root = os.path.abspath(str(pdk_root))
    state["pdk_root_host"] = pdk_root
    return pdk_root


def _resolve_align_pdk_host(state: dict) -> str:
    pdk_root = state.get("align_pdk_host") or os.getenv("CHIPLOOP_ALIGN_PDK_HOST") or ""
    pdk_root = os.path.abspath(str(pdk_root)) if pdk_root else ""
    if pdk_root:
        state["align_pdk_host"] = pdk_root
    return pdk_root


def _host_align_pdk_arg(state: dict, pdk_variant: str, pdk_root_host: str) -> str:
    candidates = [
        _resolve_align_pdk_host(state),
        os.path.join(pdk_root_host, pdk_variant),
        os.path.join(pdk_root_host, "sky130"),
    ]
    for path in candidates:
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "primitive.py")):
            return path
    return "sky130"


def _gds_backend(state: dict) -> str:
    return str(state.get("analog_gds_backend") or os.getenv("CHIPLOOP_ANALOG_GDS_BACKEND") or "magic").strip().lower()


def _magic_paths(pdk_variant: str) -> tuple[str, str]:
    return (
        f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tech",
        f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tcl",
    )


def _host_magic_paths(pdk_root_host: str, pdk_variant: str) -> tuple[str, str]:
    return (
        os.path.join(pdk_root_host, pdk_variant, "libs.tech", "magic", f"{pdk_variant}.tech"),
        os.path.join(pdk_root_host, pdk_variant, "libs.tech", "magic", f"{pdk_variant}.tcl"),
    )


def _write_magic_import_tcl(
    stage_dir: str,
    spice_name: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    use_docker: bool,
) -> str:
    tech_tcl = _magic_paths(pdk_variant)[1] if use_docker else _host_magic_paths(pdk_root_host, pdk_variant)[1]
    spice_path = spice_name
    gds_path = f"{module_name}.gds" if use_docker else os.path.join(stage_dir, f"{module_name}.gds")
    mag_path = f"{module_name}.mag" if use_docker else os.path.join(stage_dir, f"{module_name}.mag")
    feedback_path = "magic_feedback.txt" if use_docker else os.path.join(stage_dir, "magic_feedback.txt")
    lines = [
        "drc off",
        f"source {tech_tcl}",
        f"magic::netlist_to_layout {spice_path} sky130",
        f"load {module_name}",
        "select top cell",
        "expand",
        "puts stdout \"CHIPLOOP_FINAL_BOX=[box values]\"",
        f"flatten {module_name}_flat",
        f"load {module_name}_flat",
        f"catch {{cellname delete {module_name}}} chiploop_delete_original_result",
        "puts stdout \"CHIPLOOP_DELETE_ORIGINAL_RESULT=$chiploop_delete_original_result\"",
        f"cellname rename {module_name}_flat {module_name}",
        f"load {module_name}",
        "select top cell",
        "expand",
        "puts stdout \"CHIPLOOP_FLAT_BOX=[box values]\"",
        "gds flatten true",
        "catch {gds flatglob *}",
        f"catch {{feedback save {feedback_path}}}",
        f"save {module_name}.mag",
        f"gds write {gds_path}",
        f"catch {{feedback save {feedback_path}}}",
        "quit -noprompt",
        "",
    ]
    path = os.path.join(stage_dir, "magic_import_spice.tcl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def _align_docker_script(spice_name: str, module_name: str, pdk_variant: str) -> str:
    return "\n".join([
        "set -eu",
        "PY_BIN=\"$(command -v python3 || command -v python)\"",
        "PDK_DIR=\"$(${PY_BIN} - <<'PY'",
        "from pathlib import Path",
        "import align",
        "import sys",
        f"variant = {pdk_variant!r}",
        "root = Path(align.__file__).resolve().parent",
        "candidates = [",
        "    Path('/align_pdk'),",
        "    Path('/pdk') / variant,",
        "    Path('/pdk/sky130'),",
        "    root / 'pdk' / 'sky130',",
        "    root / 'pdks' / 'sky130',",
        "    root.parent / 'pdks' / 'sky130',",
        "    root.parent / 'pdk' / 'sky130',",
        "    root.parent.parent / 'pdks' / 'sky130',",
        "    root.parent.parent / 'pdk' / 'sky130',",
        "    Path('/ALIGN-public/pdks/sky130'),",
        "    Path('/ALIGN-public/pdk/sky130'),",
        "    Path('/ALIGN-public/align/pdk/sky130'),",
        "    Path('/align/pdk/sky130'),",
        "    Path('/align/pdks/sky130'),",
        "    Path('/pdks/sky130'),",
        "]",
        "for path in candidates:",
        "    if path.exists() and (path / 'primitive.py').exists():",
        "        print(path)",
        "        sys.exit(0)",
        "search_roots = [root, root.parent, root.parent.parent, Path('/ALIGN-public'), Path('/align'), Path('/usr/local/lib')]",
        "seen = set()",
        "for search_root in search_roots:",
        "    if not search_root.exists() or search_root in seen:",
        "        continue",
        "    seen.add(search_root)",
        "    for primitive in search_root.rglob('primitive.py'):",
        "        parent = primitive.parent",
        "        name = parent.name.lower()",
        "        if name in {'sky130', 'sky130a'} or 'sky130' in str(parent).lower():",
        "            print(parent)",
        "            sys.exit(0)",
        "print('ALIGN Sky130 PDK directory with primitive.py not found in mounted /align_pdk, mounted /pdk, or container image', file=sys.stderr)",
        "sys.exit(2)",
        "PY",
        ")\"",
        "echo \"ALIGN_PDK_DIR=${PDK_DIR}\"",
        (
            "schematic2layout.py /work -p \"${PDK_DIR}\" "
            f"-f {shlex.quote(spice_name)} -s {shlex.quote(module_name)}"
        ),
    ])


def _run_magic_gds(
    state: dict,
    stage_dir: str,
    staged_spice: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    docker_bin: str | None,
) -> tuple[Any, str, str, str]:
    host_tech, host_tcl = _host_magic_paths(pdk_root_host, pdk_variant)
    if not os.path.exists(host_tech):
        raise RuntimeError(f"Magic GDS generation failed: missing Sky130 Magic tech file at {host_tech}")
    if not os.path.exists(host_tcl):
        raise RuntimeError(f"Magic GDS generation failed: missing Sky130 Magic Tcl file at {host_tcl}")

    magic_bin = shutil.which("magic")
    use_docker = not bool(magic_bin)
    tcl_path = _write_magic_import_tcl(
        stage_dir,
        os.path.basename(staged_spice),
        module_name,
        pdk_variant,
        pdk_root_host,
        use_docker,
    )
    if magic_bin:
        host_tcl_text = open(tcl_path, "r", encoding="utf-8").read()
        host_tcl_text = host_tcl_text.replace(f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tcl", host_tcl)
        with open(tcl_path, "w", encoding="utf-8") as fh:
            fh.write(host_tcl_text)
        cmd = [magic_bin, "-dnull", "-noconsole", "-T", host_tech, tcl_path]
        tool_mode = "host_magic"
        image = ""
    else:
        if not docker_bin:
            raise RuntimeError("Magic GDS generation failed: Magic is not installed and Docker is not available.")
        cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "magic",
            "-dnull",
            "-noconsole",
            "-T",
            _magic_paths(pdk_variant)[0],
            "/work/magic_import_spice.tcl",
        ]
        tool_mode = "docker_magic"
        image = OPENLANE_DOCKER_IMAGE
    cp = run_command(state, "analog_magic_gds", cmd, cwd=stage_dir, timeout_sec=3600)
    return cp, tcl_path, tool_mode, image


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "gds")
    os.makedirs(stage_dir, exist_ok=True)

    if not _enabled(state):
        state["analog_gds_generation"] = {"status": "skipped", "reason": "analog_physical_mode_not_generate_sky130_gds"}
        state["status"] = f"{AGENT_NAME}: skipped"
        return state

    module_name = _module_name(state)
    pdk_variant = _resolve_pdk_variant(state)
    pdk_root_host = _resolve_pdk_root_host(state)
    align_pdk_host = _resolve_align_pdk_host(state)
    backend = _gds_backend(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pdk": pdk_variant,
        "pdk_root_host": pdk_root_host,
        "align_pdk_host": align_pdk_host,
        "backend": backend,
        "module": module_name,
        "spice": spice_path,
    }

    if not isinstance(spice_path, str) or not os.path.exists(spice_path):
        summary.update({"status": "failed", "reason": "sky130_spice_missing"})
        _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="sky130_spice_missing"))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation requires a generated or provided Sky130 transistor-level SPICE netlist.")

    align_bin = shutil.which("schematic2layout.py") or shutil.which("align")
    docker_bin = shutil.which("docker")
    spice_base = os.path.basename(spice_path) or f"{module_name}.spice"
    spice_stem, spice_ext = os.path.splitext(spice_base)
    align_spice_name = spice_base if spice_ext.lower() in {".sp", ".cdl"} else f"{spice_stem or module_name}.sp"
    staged_spice = os.path.join(stage_dir, align_spice_name)
    if backend == "magic":
        _prepare_magic_spice(spice_path, staged_spice)
    elif os.path.abspath(spice_path) != os.path.abspath(staged_spice):
        shutil.copy2(spice_path, staged_spice)
    run_sh = os.path.join(stage_dir, "run_gds.sh")
    if backend == "magic":
        expected_cmd = (
            f"docker run --rm -v {pdk_root_host}:/pdk -v {os.path.abspath(stage_dir)}:/work -w /work "
            f"{OPENLANE_DOCKER_IMAGE} magic -dnull -noconsole -T /pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tech "
            "/work/magic_import_spice.tcl"
        )
    elif align_bin:
        host_pdk_arg = _host_align_pdk_arg(state, pdk_variant, pdk_root_host)
        expected_cmd = f"{align_bin} {os.path.abspath(stage_dir)} -p {host_pdk_arg} -f {os.path.basename(staged_spice)} -s {module_name}"
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        align_pdk_mount = f"-v {align_pdk_host}:/align_pdk " if align_pdk_host else ""
        expected_cmd = (
            f"docker run --rm {align_pdk_mount}-v {pdk_root_host}:/pdk -v {os.path.abspath(stage_dir)}:/work -w /work "
            f"{ALIGN_DOCKER_IMAGE} sh -lc {shlex.quote(docker_script)}"
        )
    run_text = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f'echo "ChipLoop {AGENT_NAME}"',
        f'echo "SPICE={staged_spice}"',
        f'echo "TOP={module_name}"',
        f'echo "PDK={pdk_variant}"',
        f'echo "PDK_ROOT_HOST={pdk_root_host}"',
        f'echo "ALIGN_PDK_HOST={align_pdk_host}"',
        expected_cmd,
        "",
    ])
    with open(run_sh, "w", encoding="utf-8") as fh:
        fh.write(run_text)
    try:
        os.chmod(run_sh, 0o755)
    except Exception:
        pass

    if backend not in {"magic", "align"}:
        summary.update({
            "status": "failed",
            "reason": "unsupported_gds_backend",
            "backend": backend,
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
        _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="unsupported_gds_backend"))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError(f"Analog GDS generation failed: unsupported backend {backend}")

    if backend == "align" and not align_bin and not docker_bin:
        summary.update({
            "status": "failed",
            "reason": "align_not_installed",
            "run_script": run_sh,
            "note": "No GDS was generated. Install ALIGN/schematic2layout.py on PATH or provide Docker with darpaalign/align-public:latest.",
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
        _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="align_not_installed"))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation failed: ALIGN/schematic2layout.py is not installed and Docker is not available.")

    if backend == "magic":
        try:
            cp, tcl_path, tool_mode, image = _run_magic_gds(
                state, stage_dir, staged_spice, module_name, pdk_variant, pdk_root_host, docker_bin
            )
        except RuntimeError as exc:
            summary.update({"status": "failed", "reason": "magic_setup_failed", "error": str(exc)})
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
            _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="magic_setup_failed"))
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
            state["analog_gds_generation"] = summary
            state["status"] = f"{AGENT_NAME}: failed"
            raise
    elif align_bin:
        host_pdk_arg = _host_align_pdk_arg(state, pdk_variant, pdk_root_host)
        cmd = [
            align_bin,
            os.path.abspath(stage_dir),
            "-p",
            host_pdk_arg,
            "-f",
            os.path.basename(staged_spice),
            "-s",
            module_name,
        ]
        tool_mode = "host"
        cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
        tcl_path = ""
        image = ""
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        cmd = [
            docker_bin,
            "run",
            "--rm",
        ]
        if align_pdk_host:
            cmd.extend(["-v", f"{align_pdk_host}:/align_pdk"])
        cmd.extend([
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-e",
            f"PDK={pdk_variant}",
            "-e",
            "PDK_ROOT=/pdk",
            "-w",
            "/work",
            ALIGN_DOCKER_IMAGE,
            "sh",
            "-lc",
            docker_script,
        ])
        tool_mode = "docker"
        cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
        tcl_path = ""
        image = ALIGN_DOCKER_IMAGE if tool_mode == "docker" else ""
    log = (cp.stdout or "") + (cp.stderr or "")
    log_name = "magic_gds.log" if backend == "magic" else "align.log"
    log_path = os.path.join(stage_dir, log_name)
    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(log)

    repair_attempted = False
    repair_applied = False
    repair_reason = ""
    repair_layout_issues = []
    lvs_repair_attempted = False
    lvs_repair_applied = False
    lvs_repair_reason = ""
    lvs_repair_layout_issues = []
    forced_failure_reason = ""
    pass1_feedback_problem_count = _magic_feedback_problem_count(log) if backend == "magic" else 0
    pass1_invalid_reason = _magic_layout_invalid(log) if backend == "magic" else ""
    sky130_summary = state.get("analog_sky130_spice") if isinstance(state.get("analog_sky130_spice"), dict) else {}
    can_repair_magic_spice = (
        backend == "magic"
        and pass1_invalid_reason == "magic_feedback_problems"
        and sky130_summary.get("generated") is True
    )
    if can_repair_magic_spice:
        repair_attempted = True
        pass1_log_path = os.path.join(stage_dir, "magic_gds_pass1.log")
        shutil.copy2(log_path, pass1_log_path)
        pass1_spice_path = os.path.join(stage_dir, "magic_input_pass1.sp")
        if os.path.exists(staged_spice):
            shutil.copy2(staged_spice, pass1_spice_path)
        feedback_text = ""
        feedback_path = os.path.join(stage_dir, "magic_feedback.txt")
        if os.path.exists(feedback_path):
            with open(feedback_path, "r", encoding="utf-8", errors="ignore") as fh:
                feedback_text = fh.read()
            shutil.copy2(feedback_path, os.path.join(stage_dir, "magic_feedback_pass1.txt"))
        with open(staged_spice, "r", encoding="utf-8", errors="ignore") as fh:
            original_spice = fh.read()
        repaired_spice = _repair_magic_feedback_spice(
            state,
            module_name=module_name,
            original_spice=original_spice,
            magic_log=log,
            feedback_text=feedback_text,
        )
        if repaired_spice:
            with open(os.path.join(stage_dir, "magic_input_repair.sp"), "w", encoding="utf-8") as fh:
                fh.write(repaired_spice)
            repair_layout_issues = _generated_spice_layout_issues(repaired_spice, _port_specs(state))
            if repair_layout_issues:
                repair_reason = "repair_spice_not_layout_safe"
                forced_failure_reason = repair_reason
            else:
                with open(staged_spice, "w", encoding="utf-8") as fh:
                    fh.write(_magic_spice_text(repaired_spice))
                _preserve_and_clean_magic_layout_outputs(stage_dir, module_name)
                cp, tcl_path, tool_mode, image = _run_magic_gds(
                    state, stage_dir, staged_spice, module_name, pdk_variant, pdk_root_host, docker_bin
                )
                log = (cp.stdout or "") + (cp.stderr or "")
                with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                    fh.write(log)
                repair_applied = _magic_layout_invalid(log) == ""
        else:
            repair_reason = "repair_pass_no_valid_mos_devices"

    gds_path = _find_gds(stage_dir)
    magic_feedback_problem_count = _magic_feedback_problem_count(log) if backend == "magic" else 0
    invalid_reason = forced_failure_reason or (_magic_layout_invalid(log) if backend == "magic" else "")
    if gds_path and not invalid_reason:
        final_gds = os.path.join(stage_dir, f"{module_name}.gds")
        if os.path.abspath(gds_path) != os.path.abspath(final_gds):
            shutil.copy2(gds_path, final_gds)
        summary.update({
            "status": "generated",
            "return_code": cp.returncode,
            "gds": final_gds,
            "log": log_path,
            "tool_mode": tool_mode,
            "image": image,
            "repair_attempted": repair_attempted,
            "repair_applied": repair_applied,
            "pass1_magic_feedback_problem_count": pass1_feedback_problem_count if repair_attempted else None,
        })
        with open(final_gds, "rb") as fh:
            # Store a small text breadcrumb instead of trying to upload binary through text helper.
            pass
        state["analog_macro_gds"] = final_gds
        digital = state.setdefault("digital", {})
        if isinstance(digital, dict):
            digital["macro_gds"] = list(dict.fromkeys((digital.get("macro_gds") or []) + [final_gds]))
            digital.pop("macro_blackbox_mode", None)
    else:
        reason = invalid_reason or ("magic_gds_not_produced" if backend == "magic" else "align_gds_not_produced")
        summary.update({
            "status": "failed",
            "return_code": cp.returncode,
            "reason": reason,
            "magic_feedback_problem_count": magic_feedback_problem_count,
            "log": log_path,
            "log_tail": _tail(log),
            "tool_mode": tool_mode,
            "image": image,
            "repair_attempted": repair_attempted,
            "repair_applied": repair_applied,
            "repair_reason": repair_reason or None,
            "repair_layout_issues": repair_layout_issues or None,
            "pass1_magic_feedback_problem_count": pass1_feedback_problem_count if repair_attempted else None,
        })

    analog_lvs = None
    if summary.get("status") == "generated" and backend == "magic":
        analog_lvs = _run_analog_lvs(
            state,
            stage_dir=stage_dir,
            module_name=module_name,
            pdk_variant=pdk_variant,
            pdk_root_host=pdk_root_host,
            source_spice=staged_spice,
            docker_bin=docker_bin,
        )
        pass1_analog_lvs = dict(analog_lvs) if isinstance(analog_lvs, dict) else {}
        if _analog_lvs_should_repair(analog_lvs) and sky130_summary.get("generated") is True:
            lvs_repair_attempted = True
            for src_name, dst_name in (
                ("analog_lvs_magic_extract.log", "analog_lvs_magic_extract_pass1.log"),
                ("analog_lvs_netgen.log", "analog_lvs_netgen_pass1.log"),
                (f"{module_name}_extracted.spice", f"{module_name}_extracted_pass1.spice"),
                (f"{module_name}_lvs_source.spice", f"{module_name}_lvs_source_pass1.spice"),
            ):
                src_path = os.path.join(stage_dir, src_name)
                if os.path.exists(src_path):
                    shutil.copy2(src_path, os.path.join(stage_dir, dst_name))
            pass1_log_path = os.path.join(stage_dir, "magic_gds_lvs_pass1.log")
            if os.path.exists(log_path):
                shutil.copy2(log_path, pass1_log_path)
            pass1_spice_path = os.path.join(stage_dir, "magic_input_lvs_pass1.sp")
            if os.path.exists(staged_spice):
                shutil.copy2(staged_spice, pass1_spice_path)

            with open(staged_spice, "r", encoding="utf-8", errors="ignore") as fh:
                original_spice = fh.read()
            lvs_log_text = ""
            lvs_log_file = analog_lvs.get("log") if isinstance(analog_lvs, dict) else None
            if isinstance(lvs_log_file, str) and os.path.exists(lvs_log_file):
                with open(lvs_log_file, "r", encoding="utf-8", errors="ignore") as fh:
                    lvs_log_text = fh.read()
            extracted_text = ""
            extracted_file = analog_lvs.get("extracted_spice") if isinstance(analog_lvs, dict) else None
            if isinstance(extracted_file, str) and os.path.exists(extracted_file):
                with open(extracted_file, "r", encoding="utf-8", errors="ignore") as fh:
                    extracted_text = fh.read()

            repaired_spice = _repair_lvs_mismatch_spice(
                state,
                module_name=module_name,
                original_spice=original_spice,
                lvs_summary=analog_lvs,
                lvs_log=lvs_log_text,
                extracted_spice=extracted_text,
            )
            if repaired_spice:
                repaired_spice = _normalize_subckt_bus_pins(repaired_spice)
                with open(os.path.join(stage_dir, "magic_input_lvs_repair.sp"), "w", encoding="utf-8") as fh:
                    fh.write(repaired_spice)
                lvs_repair_layout_issues = _generated_spice_layout_issues(repaired_spice, _port_specs(state))
                if lvs_repair_layout_issues:
                    lvs_repair_reason = "lvs_repair_spice_not_layout_safe"
                    analog_lvs = {
                        **analog_lvs,
                        "repair_attempted": True,
                        "repair_applied": False,
                        "repair_reason": lvs_repair_reason,
                        "repair_layout_issues": lvs_repair_layout_issues,
                    }
                else:
                    with open(staged_spice, "w", encoding="utf-8") as fh:
                        fh.write(_magic_spice_text(repaired_spice))
                    _preserve_and_clean_magic_layout_outputs(stage_dir, module_name)
                    cp, tcl_path, tool_mode, image = _run_magic_gds(
                        state, stage_dir, staged_spice, module_name, pdk_variant, pdk_root_host, docker_bin
                    )
                    log = (cp.stdout or "") + (cp.stderr or "")
                    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                        fh.write(log)
                    invalid_reason = _magic_layout_invalid(log)
                    gds_path = _find_gds(stage_dir)
                    if gds_path and not invalid_reason:
                        final_gds = os.path.join(stage_dir, f"{module_name}.gds")
                        if os.path.abspath(gds_path) != os.path.abspath(final_gds):
                            shutil.copy2(gds_path, final_gds)
                        summary.update({
                            "status": "generated",
                            "return_code": cp.returncode,
                            "gds": final_gds,
                            "log": log_path,
                            "tool_mode": tool_mode,
                            "image": image,
                        })
                        state["analog_macro_gds"] = final_gds
                        digital = state.setdefault("digital", {})
                        if isinstance(digital, dict):
                            digital["macro_gds"] = list(dict.fromkeys((digital.get("macro_gds") or []) + [final_gds]))
                            digital.pop("macro_blackbox_mode", None)
                        analog_lvs = _run_analog_lvs(
                            state,
                            stage_dir=stage_dir,
                            module_name=module_name,
                            pdk_variant=pdk_variant,
                            pdk_root_host=pdk_root_host,
                            source_spice=staged_spice,
                            docker_bin=docker_bin,
                        )
                        lvs_repair_applied = True
                        analog_lvs = {
                            **analog_lvs,
                            "repair_attempted": True,
                            "repair_applied": True,
                            "pass1": {
                                "status": pass1_analog_lvs.get("status"),
                                "counts": pass1_analog_lvs.get("counts"),
                                "failure_class": pass1_analog_lvs.get("failure_class"),
                            },
                        }
                    else:
                        lvs_repair_reason = invalid_reason or "lvs_repair_gds_not_produced"
                        summary.update({
                            "status": "failed",
                            "return_code": cp.returncode,
                            "reason": lvs_repair_reason,
                            "magic_feedback_problem_count": _magic_feedback_problem_count(log),
                            "log": log_path,
                            "log_tail": _tail(log),
                            "tool_mode": tool_mode,
                            "image": image,
                        })
                        analog_lvs = {
                            **analog_lvs,
                            "repair_attempted": True,
                            "repair_applied": False,
                            "repair_reason": lvs_repair_reason,
                        }
            else:
                lvs_repair_reason = "lvs_repair_pass_no_valid_mos_devices"
                analog_lvs = {
                    **analog_lvs,
                    "repair_attempted": True,
                    "repair_applied": False,
                    "repair_reason": lvs_repair_reason,
                }
        elif isinstance(analog_lvs, dict):
            analog_lvs = {
                **analog_lvs,
                "repair_attempted": False,
                "repair_applied": False,
            }
        summary["analog_lvs"] = analog_lvs
        summary["lvs_repair_attempted"] = lvs_repair_attempted
        summary["lvs_repair_applied"] = lvs_repair_applied
        summary["lvs_repair_reason"] = lvs_repair_reason or None
        summary["lvs_repair_layout_issues"] = lvs_repair_layout_issues or None

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
    if backend == "magic" and tcl_path and os.path.exists(tcl_path):
        with open(tcl_path, "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_import_spice.tcl", fh.read())
    if backend == "magic" and os.path.exists(staged_spice):
        with open(staged_spice, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input.sp", fh.read())
    pass1_spice_path = os.path.join(stage_dir, "magic_input_pass1.sp")
    if backend == "magic" and os.path.exists(pass1_spice_path):
        with open(pass1_spice_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input_pass1.sp", fh.read())
    repair_spice_path = os.path.join(stage_dir, "magic_input_repair.sp")
    if backend == "magic" and os.path.exists(repair_spice_path):
        with open(repair_spice_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input_repair.sp", fh.read())
    lvs_repair_spice_path = os.path.join(stage_dir, "magic_input_lvs_repair.sp")
    if backend == "magic" and os.path.exists(lvs_repair_spice_path):
        with open(lvs_repair_spice_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input_lvs_repair.sp", fh.read())
    feedback_path = os.path.join(stage_dir, "magic_feedback.txt")
    if backend == "magic" and os.path.exists(feedback_path):
        with open(feedback_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_feedback.txt", fh.read())
    pass1_feedback_path = os.path.join(stage_dir, "magic_feedback_pass1.txt")
    if backend == "magic" and os.path.exists(pass1_feedback_path):
        with open(pass1_feedback_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_feedback_pass1.txt", fh.read())
    pass1_log_path = os.path.join(stage_dir, "magic_gds_pass1.log")
    if backend == "magic" and os.path.exists(pass1_log_path):
        with open(pass1_log_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_gds_pass1.log", fh.read())
    pass1_lvs_gds_log_path = os.path.join(stage_dir, "magic_gds_lvs_pass1.log")
    if backend == "magic" and os.path.exists(pass1_lvs_gds_log_path):
        with open(pass1_lvs_gds_log_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_gds_lvs_pass1.log", fh.read())
    extract_lvs_tcl = os.path.join(stage_dir, "magic_extract_lvs.tcl")
    if backend == "magic" and os.path.exists(extract_lvs_tcl):
        with open(extract_lvs_tcl, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/signoff", "magic_extract_lvs.tcl", fh.read())
    for artifact_name in (
        "analog_lvs_magic_extract.log",
        "analog_lvs_magic_extract_pass1.log",
        "analog_lvs_netgen.log",
        "analog_lvs_netgen_pass1.log",
        f"{module_name}_extracted.spice",
        f"{module_name}_extracted_pass1.spice",
        f"{module_name}_lvs_source.spice",
        f"{module_name}_lvs_source_pass1.spice",
    ):
        artifact_path = os.path.join(stage_dir, artifact_name)
        if os.path.exists(artifact_path):
            with open(artifact_path, "r", encoding="utf-8", errors="ignore") as fh:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/signoff", artifact_name, fh.read())
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", log_name, log)
    _publish_analog_signoff(
        workflow_id,
        state,
        _analog_signoff_summary(
            summary,
            log_path=log_path,
            log=log,
            gds_path=summary.get("gds") if isinstance(summary.get("gds"), str) else gds_path,
            invalid_reason=invalid_reason,
            analog_lvs=analog_lvs,
        ),
    )
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
    state["analog_gds_generation"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if summary["status"] != "generated":
        detail = summary.get("log_tail") or ""
        label = "Magic" if backend == "magic" else "ALIGN"
        raise RuntimeError(
            f"Analog GDS generation failed: {summary.get('reason') or 'gds_not_produced'}"
            + (f"\n{label} log tail:\n{detail}" if detail else "")
        )
    return state
