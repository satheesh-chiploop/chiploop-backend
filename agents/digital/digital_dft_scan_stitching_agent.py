import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital DFT Scan Stitching Agent"
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _existing_path(value: Any, workflow_dir: str | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidates = [raw]
    if workflow_dir and not os.path.isabs(raw):
        candidates.insert(0, os.path.join(workflow_dir, raw))
    for cand in candidates:
        try:
            cand = os.path.abspath(cand)
            if os.path.exists(cand):
                return cand
        except (TypeError, ValueError, OSError):
            continue
    return None


def _read_json(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pdk_context(state: dict, workflow_dir: str, container_paths: bool) -> dict:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    foundry = digital.get("foundry") if isinstance(digital.get("foundry"), dict) else {}
    foundry_file = _read_json(os.path.join(workflow_dir, "digital", "foundry", "foundry_profile.json"))
    pdk_variant = str(
        state.get("pdk_variant")
        or state.get("pdk")
        or foundry.get("pdk")
        or foundry_file.get("pdk")
        or os.getenv("CHIPLOOP_PDK_VARIANT")
        or os.getenv("PDK")
        or "sky130A"
    ).strip()
    pdk_root_host = (
        state.get("pdk_root_host")
        or state.get("pdk_root")
        or foundry.get("pdk_root")
        or foundry_file.get("pdk_root")
        or os.getenv("CHIPLOOP_PDK_ROOT_HOST")
        or os.getenv("PDK_ROOT")
        or "/root/chiploop-backend/backend/pdk"
    )
    pdk_root_host = os.path.abspath(str(pdk_root_host))
    root = "/pdk" if container_paths else pdk_root_host
    pdk_root = os.path.join(root, pdk_variant)
    return {
        "pdk_variant": pdk_variant,
        "pdk_root_host": pdk_root_host,
        "pdk_root": pdk_root,
        "container_paths": container_paths,
    }


def _candidate_tech_files(pdk: dict) -> dict[str, list[str]]:
    root = pdk["pdk_root"]
    return {
        "tech_lefs": [
            os.path.join(root, "libs.ref", "sky130_fd_sc_hd", "techlef", "sky130_fd_sc_hd__nom.tlef"),
            os.path.join(root, "libs.ref", "sky130_fd_sc_hd", "techlef", "sky130_fd_sc_hd.tlef"),
            os.path.join(root, "libs.tech", "openroad", "sky130_fd_sc_hd.tlef"),
            os.path.join(root, "libs.tech", "openroad", "sky130_fd_sc_hd__nom.tlef"),
        ],
        "cell_lefs": [
            os.path.join(root, "libs.ref", "sky130_fd_sc_hd", "lef", "sky130_fd_sc_hd.lef"),
            os.path.join(root, "libs.tech", "openroad", "sky130_fd_sc_hd.lef"),
        ],
        "liberties": [
            os.path.join(root, "libs.ref", "sky130_fd_sc_hd", "lib", "sky130_fd_sc_hd__tt_025C_1v80.lib"),
            os.path.join(root, "libs.ref", "sky130_fd_sc_hd", "lib", "sky130_fd_sc_hd__tt_100C_1v80.lib"),
            os.path.join(root, "libs.ref", "sky130_fd_sc_hd", "lib", "sky130_fd_sc_hd__ss_100C_1v60.lib"),
        ],
    }


def _tcl_read_existing(command: str, paths: list[str]) -> str:
    lines = []
    for path in paths:
        q = json.dumps(path)
        lines.append(f"if {{[file exists {q}]}} {{ {command} {q} }}")
    return "\n".join(lines)


def _synth_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for cand in (synth.get("netlist"), digital.get("synth_netlist"), state.get("synth_netlist")):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    hits = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    return hits[0] if hits else None


def _constraints_sdc(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for cand in (
        synth.get("constraints_sdc"),
        digital.get("constraints_sdc"),
        state.get("constraints_sdc"),
    ):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    for pattern in (
        "digital/synth/constraints/*.sdc",
        "digital/impl_setup/constraints/*.sdc",
        "digital/constraints/*.sdc",
    ):
        hits = sorted(glob.glob(os.path.join(workflow_dir, pattern)))
        if hits:
            return hits[0]
    return None


def _top_module(state: dict, netlist: str | None) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for value in (synth.get("top_module"), digital.get("top_module"), state.get("top_module")):
        if isinstance(value, str) and value.strip() and value.strip() != "imported_from_arch2rtl":
            return value.strip()
    text = _read_text(netlist)
    match = re.search(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE)
    return match.group(1) if match else "top"


def _count_scan_candidates(netlist: str | None) -> tuple[int, int]:
    text = _read_text(netlist).lower()
    if not text:
        return 0, 0
    flop_count = 0
    latch_count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if any(token in line for token in ("sky130_fd_sc_hd__df", "sky130_fd_sc_hs__df", "__dff", "__sdff", " dff", " sdff")):
            flop_count += 1
        elif any(token in line for token in ("sky130_fd_sc_hd__dl", "__dlatch", "latch")):
            latch_count += 1
    return flop_count, latch_count


def _add_scan_ports(text: str, chain_count: int, scan_enable: str, scan_in_prefix: str, scan_out_prefix: str) -> str:
    match = re.search(r"module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\((.*?)\);", text, flags=re.DOTALL)
    if not match:
        return text
    port_text = match.group(2).strip()
    scan_in = scan_in_prefix
    scan_out = scan_out_prefix
    if scan_enable in port_text and scan_in in port_text and scan_out in port_text:
        return text

    new_ports = f"{port_text}, {scan_enable}, {scan_in}, {scan_out}"
    text = text[:match.start(2)] + new_ports + text[match.end(2):]
    decl = (
        f"  input {scan_enable};\n"
        f"  input [{chain_count - 1}:0] {scan_in};\n"
        f"  output [{chain_count - 1}:0] {scan_out};\n"
    )
    insert_at = text.find("\n", match.end())
    if insert_at >= 0:
        text = text[:insert_at + 1] + decl + text[insert_at + 1:]
    return text


def _scan_map_sky130_reset_flops(netlist_text: str, config: dict) -> tuple[str, dict]:
    """
    Deterministically map SKY130 reset DFFs to equivalent scan DFFs.

    OpenROAD's DFT commands expect scan-capable flops before chain stitching.
    For SKY130 HD, dfrtp has ports CLK/D/RESET_B/Q; sdfrtp adds SCD/SCE.
    """
    chain_count = max(1, int(config.get("scan_chains") or 1))
    scan_enable = str(config.get("scan_enable") or "scan_en")
    scan_in = str(config.get("scan_in_prefix") or "scan_in")
    scan_out = str(config.get("scan_out_prefix") or "scan_out")
    chain_tails: dict[int, str] = {}
    mapped = 0

    pattern = re.compile(
        r"(?P<cell>sky130_fd_sc_hd__dfrtp(?:_\d+)?)\s+"
        r"(?P<inst>(?:\\[^\s(]+|[A-Za-z_][A-Za-z0-9_$]*))\s*"
        r"\((?P<ports>.*?)\n\s*\);",
        flags=re.DOTALL,
    )

    def verilog_signal(raw: str) -> str:
        signal = raw.strip()
        if signal.startswith("\\"):
            return signal + " "
        return signal

    def repl(match: re.Match[str]) -> str:
        nonlocal mapped
        ports = match.group("ports")
        q_match = re.search(r"\.Q\s*\(\s*(.*?)\s*\)", ports, flags=re.DOTALL)
        q_signal = verilog_signal(q_match.group(1)) if q_match else ""
        chain = mapped % chain_count
        scd = chain_tails.get(chain) or f"{scan_in}[{chain}]"
        if q_signal:
            chain_tails[chain] = q_signal
        mapped += 1
        scan_cell = match.group("cell").replace("__dfrtp", "__sdfrtp")
        return (
            f"{scan_cell} {match.group('inst')} ("
            f"{ports},\n"
            f"    .SCD({scd}),\n"
            f"    .SCE({scan_enable})\n"
            f"  );"
        )

    mapped_text = pattern.sub(repl, netlist_text)
    if mapped <= 0:
        return netlist_text, {"status": "not_applied", "mapped_flops": 0, "scan_chains": chain_count}

    mapped_text = _add_scan_ports(mapped_text, chain_count, scan_enable, scan_in, scan_out)
    assignments = []
    for chain in range(chain_count):
        tail = chain_tails.get(chain) or f"{scan_in}[{chain}]"
        assignments.append(f"  assign {scan_out}[{chain}] = {tail};")
    assignment_block = "\n" + "\n".join(assignments) + "\nendmodule\n"
    mapped_text = re.sub(r"\nendmodule\s*$", lambda _: assignment_block, mapped_text, count=1)
    return mapped_text, {
        "status": "mapped",
        "mapping": "sky130_fd_sc_hd__dfrtp_to_sdfrtp",
        "mapped_flops": mapped,
        "scan_chains": chain_count,
        "scan_enable": scan_enable,
        "scan_in": scan_in,
        "scan_out": scan_out,
    }


def _scan_config(state: dict, top: str, flop_count: int) -> dict:
    dft = state.get("dft") if isinstance(state.get("dft"), dict) else {}
    requested_chains = dft.get("scan_chains") or state.get("scan_chains")
    try:
        chains = max(1, int(requested_chains))
    except Exception:
        chains = max(1, min(4, flop_count or 1))
    max_len = (flop_count + chains - 1) // chains if flop_count else 0
    return {
        "top_module": top,
        "scan_chains": chains,
        "max_chain_length_estimate": max_len,
        "scan_enable": str(dft.get("scan_enable") or state.get("scan_enable") or "scan_en"),
        "scan_in_prefix": str(dft.get("scan_in_prefix") or state.get("scan_in_prefix") or "scan_in"),
        "scan_out_prefix": str(dft.get("scan_out_prefix") or state.get("scan_out_prefix") or "scan_out"),
        "clock_mixing": str(dft.get("clock_mixing") or state.get("clock_mixing") or "no_mix"),
    }


def _openroad_tcl(config: dict, netlist: str | None, sdc: str | None, out_netlist: str, pdk: dict) -> str:
    # OpenROAD DFT command availability depends on build/package. Keep this
    # generated script explicit so private deployments can adapt it to local
    # foundry/scan-cell naming without changing the agent.
    tech = _candidate_tech_files(pdk)
    return f"""# Auto-generated by {AGENT_NAME}
# Uses DFT commands available in OpenLane2/OpenROAD builds:
# set_dft_config, preview_dft, scan_replace.
{_tcl_read_existing("read_lef", tech["tech_lefs"])}
{_tcl_read_existing("read_lef", tech["cell_lefs"])}
{_tcl_read_existing("read_liberty", tech["liberties"])}
read_verilog {json.dumps(netlist or "")}
link_design {config["top_module"]}
if {{[file exists {json.dumps(sdc or "")}]}} {{ read_sdc {json.dumps(sdc or "")} }}

set_dft_config \\
  -max_length {config["max_chain_length_estimate"] or 1} \\
  -max_chains {config["scan_chains"]} \\
  -clock_mixing {config["clock_mixing"]}

preview_dft -verbose
scan_replace
write_verilog {json.dumps(out_netlist)}
"""


def _preserve_missing_macro_instances(source_text: str, stitched_text: str) -> tuple[str, list[str]]:
    if not source_text.strip() or not stitched_text.strip():
        return stitched_text, []
    existing_instances = set(re.findall(r"\b(?:\\[^\s(]+|[A-Za-z_][A-Za-z0-9_$]*)\s*\(", stitched_text))
    preserved: list[str] = []
    blocks: list[str] = []
    pattern = re.compile(
        r"(?m)^\s*(?P<cell>[A-Za-z_][A-Za-z0-9_$]*)\s+"
        r"(?P<inst>(?:\\[^\s(]+|[A-Za-z_][A-Za-z0-9_$]*))\s*"
        r"\((?P<body>.*?)\);",
        flags=re.DOTALL,
    )
    for match in pattern.finditer(source_text):
        cell = match.group("cell")
        inst = match.group("inst").strip()
        if cell in {"module", "endmodule", "assign", "always", "if", "for", "generate"}:
            continue
        if cell.startswith(("sky130_fd_sc_", "sky130_ef_sc_")):
            continue
        if f"{inst}(" in existing_instances or inst in stitched_text:
            continue
        blocks.append(match.group(0).rstrip())
        preserved.append(f"{cell}:{inst}")
    if not blocks:
        return stitched_text, []
    insertion = "\n  // Preserved non-stdcell macro instances omitted by DFT write_verilog.\n" + "\n".join(blocks) + "\n"
    repaired, count = re.subn(r"\nendmodule\s*$", lambda _match: insertion + "endmodule\n", stitched_text, count=1)
    return (repaired if count else stitched_text + insertion), preserved


def _classify(
    rc: int | None,
    log: str,
    tool_available: bool,
    netlist: str | None,
    flop_count: int,
    stitched: bool,
) -> str:
    if not netlist:
        return "incomplete_inputs"
    if flop_count <= 0:
        return "no_scan_flops"
    if not tool_available:
        return "tool_unavailable"
    actual_chains = _actual_scan_chain_count(log)
    if rc == 0:
        if actual_chains == 0:
            if "not a scan cell" in log.lower():
                return "scan_cell_mapping_required"
            return "scan_not_inserted"
        return "scan_replace_pass" if stitched else "preview_pass"
    text = log.lower()
    if "unknown command" in text or "invalid command" in text or "dft" in text and "not" in text and "enabled" in text:
        return "tool_missing_dft_support"
    if "no technology has been read" in text or "ord-2010" in text:
        return "tool_needs_technology"
    return "fail"


def _actual_scan_chain_count(log: str) -> int | None:
    match = re.search(r"Number\s+of\s+chains:\s*(\d+)", log, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _docker_openroad_command(stage_dir: str, image: str, pdk_root_host: str, pdk_variant: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{stage_dir}:/work",
        "-v",
        f"{pdk_root_host}:/pdk",
        "-e",
        "PDK_ROOT=/pdk",
        "-e",
        f"PDK={pdk_variant}",
        image,
        "bash",
        "-lc",
        "cd /work && openroad -exit openroad_dft_scan.tcl",
    ]


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    if toggles.get("enable_scan_dft") is False:
        workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
        stage_dir = os.path.join(workflow_dir, "digital", "dft")
        _ensure_dir(stage_dir)
        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "not_applicable",
            "reason": "enable_scan_dft_disabled",
            "scan_chains": None,
            "actual_scan_chains": None,
            "scan_flops": None,
            "stitched_netlist_generated": False,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
        report = "# DFT Scan Stitching\n\n- Status: `not_applicable`\n- Reason: `enable_scan_dft_disabled`\n"
        _write_text(os.path.join(stage_dir, "scan_summary.json"), json.dumps(summary, indent=2))
        _write_text(os.path.join(stage_dir, "dft_report.md"), report)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/scan_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/dft_report.md", report)
        digital = state.setdefault("digital", {})
        digital["dft"] = {
            "status": "not_applicable",
            "reason": "enable_scan_dft_disabled",
            "scan_stitched_netlist": None,
        }
        state["status"] = f"{AGENT_NAME}: not_applicable"
        return state

    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "dft")
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(logs_dir)

    netlist = _synth_netlist(state, workflow_dir)
    sdc = _constraints_sdc(state, workflow_dir)
    top = _top_module(state, netlist)
    flop_count, latch_count = _count_scan_candidates(netlist)
    config = _scan_config(state, top, flop_count)
    openroad = tool_path("openroad", state)
    docker = tool_path("docker", state)
    openlane_image = str(state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE)
    use_container_paths = not bool(openroad) and bool(docker)
    pdk = _pdk_context(state, workflow_dir, container_paths=use_container_paths)

    scan_netlist_path = os.path.join(stage_dir, "scan_stitched_netlist.v")
    input_dir = os.path.join(stage_dir, "input")
    input_netlist_path = os.path.join(input_dir, "synth_netlist.v")
    input_sdc_path = os.path.join(input_dir, "constraints.sdc")
    scan_mapped_netlist_path = os.path.join(input_dir, "scan_mapped_netlist.v")
    tcl_path = os.path.join(stage_dir, "openroad_dft_scan.tcl")
    log_path = os.path.join(logs_dir, "openroad_dft_scan.log")
    summary_path = os.path.join(stage_dir, "scan_summary.json")
    report_path = os.path.join(stage_dir, "dft_report.md")

    scan_mapping = {"status": "not_applied", "mapped_flops": 0}
    if netlist:
        source_netlist_text = _read_text(netlist)
        if str(pdk["pdk_variant"]).lower().startswith("sky130"):
            mapped_text, scan_mapping = _scan_map_sky130_reset_flops(source_netlist_text, config)
            if scan_mapping.get("status") == "mapped":
                _write_text(scan_mapped_netlist_path, mapped_text)
                _write_text(input_netlist_path, mapped_text)
            else:
                _write_text(input_netlist_path, source_netlist_text)
        else:
            _write_text(input_netlist_path, source_netlist_text)
    if sdc:
        _write_text(input_sdc_path, _read_text(sdc))
    tcl_netlist = "input/synth_netlist.v" if netlist else None
    tcl_sdc = "input/constraints.sdc" if sdc else None
    tcl = _openroad_tcl(config, tcl_netlist, tcl_sdc, "scan_stitched_netlist.v", pdk)
    _write_text(tcl_path, tcl)

    rc = None
    log = ""
    tool_source = "none"
    if openroad and netlist and flop_count > 0:
        tool_source = "host"
        proc = run_command(state, "dft_scan_stitching", ["openroad", "-exit", "openroad_dft_scan.tcl"], cwd=stage_dir, timeout_sec=900)
        rc = proc.returncode
        log = (proc.stdout or "") + (proc.stderr or "")
    elif docker and netlist and flop_count > 0:
        tool_source = "openlane2_docker"
        proc = run_command(
            state,
            "dft_scan_stitching",
            _docker_openroad_command(stage_dir, openlane_image, pdk["pdk_root_host"], pdk["pdk_variant"]),
            cwd=stage_dir,
            timeout_sec=900,
        )
        rc = proc.returncode
        log = (proc.stdout or "") + (proc.stderr or "")
    elif not openroad:
        log = "Neither host OpenROAD nor Docker/OpenLane2 was found in the active ChipLoop tool profile.\n"
    elif not netlist:
        log = "Missing synthesized netlist for DFT scan stitching.\n"
    else:
        log = "No scan-capable flops were detected in the synthesized netlist.\n"
    _write_text(log_path, log)

    stitched = os.path.exists(scan_netlist_path)
    preserved_macros: list[str] = []
    if stitched:
        source_for_preserve = _read_text(scan_mapped_netlist_path) or _read_text(input_netlist_path)
        stitched_text, preserved_macros = _preserve_missing_macro_instances(source_for_preserve, _read_text(scan_netlist_path))
        if preserved_macros:
            _write_text(scan_netlist_path, stitched_text)
    tool_available = bool(openroad or docker)
    status = _classify(rc, log, tool_available, netlist, flop_count, stitched)
    actual_scan_chains = _actual_scan_chain_count(log)
    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": status,
        "tool": "openroad",
        "tool_source": tool_source,
        "tool_available": tool_available,
        "openlane_image": openlane_image if tool_source == "openlane2_docker" else None,
        "pdk_variant": pdk["pdk_variant"],
        "pdk_root_host": pdk["pdk_root_host"],
        "dft_mode": "scan_replace_preview",
        "scan_mapping": scan_mapping,
        "full_dft_plan_commands_available": False,
        "return_code": rc,
        "top_module": top,
        "synth_netlist": os.path.basename(netlist) if netlist else None,
        "constraints_sdc": os.path.basename(sdc) if sdc else None,
        "scan_flops": flop_count,
        "latches": latch_count,
        "scan_chains": config["scan_chains"],
        "actual_scan_chains": actual_scan_chains,
        "max_chain_length_estimate": config["max_chain_length_estimate"],
        "scan_enable": config["scan_enable"],
        "stitched_netlist_generated": stitched,
        "preserved_macro_instances": preserved_macros,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "config": "digital/dft/scan_config.json",
            "script": "digital/dft/openroad_dft_scan.tcl",
            "log": "digital/dft/logs/openroad_dft_scan.log",
            "summary": "digital/dft/scan_summary.json",
            "report": "digital/dft/dft_report.md",
            "stitched_netlist": "digital/dft/scan_stitched_netlist.v" if stitched else None,
            "scan_mapped_netlist": "digital/dft/input/scan_mapped_netlist.v" if scan_mapping.get("status") == "mapped" else None,
        },
    }
    report = "\n".join([
        "# DFT Scan Stitching",
        "",
        f"- Status: `{status}`",
        f"- Tool: `openroad` via `{tool_source}`",
        f"- PDK: `{pdk['pdk_variant']}`",
        f"- DFT mode: `scan_replace_preview`",
        f"- Scan mapping: `{scan_mapping.get('status')}`",
        f"- Scan-mapped flops: `{scan_mapping.get('mapped_flops', 0)}`",
        f"- Top module: `{top}`",
        f"- SDC: `{os.path.basename(sdc) if sdc else 'missing'}`",
        f"- Scan flops: `{flop_count}`",
        f"- Latches: `{latch_count}`",
        f"- Scan chains: `{config['scan_chains']}`",
        f"- Actual scan chains: `{actual_scan_chains if actual_scan_chains is not None else 'not reported'}`",
        f"- Max chain length estimate: `{config['max_chain_length_estimate']}`",
        f"- Scan enable: `{config['scan_enable']}`",
        f"- Stitched netlist generated: `{stitched}`",
        "",
        "This OpenROAD integration uses the DFT commands available in the OpenLane2 image: `set_dft_config`, `preview_dft`, and `scan_replace`.",
        "If status is `scan_cell_mapping_required`, synthesis produced ordinary flops rather than scan flops; use a scan-cell mapping step or a private DFT adapter before ATPG.",
        "If status is `tool_unavailable` or `tool_missing_dft_support`, install/configure OpenROAD/OpenLane2 with DFT support or map this agent to a private DFT tool adapter.",
        "If status is `tool_needs_technology`, configure the active PDK root so OpenROAD can read technology LEF, standard-cell LEF, and Liberty files.",
    ]) + "\n"

    _write_text(os.path.join(stage_dir, "scan_config.json"), json.dumps(config, indent=2))
    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(report_path, report)

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/scan_config.json", json.dumps(config, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/openroad_dft_scan.tcl", tcl)
    if netlist:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/input/synth_netlist.v", _read_text(input_netlist_path))
        if scan_mapping.get("status") == "mapped":
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/input/scan_mapped_netlist.v", _read_text(scan_mapped_netlist_path))
    if sdc:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/input/constraints.sdc", _read_text(input_sdc_path))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/logs/openroad_dft_scan.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/scan_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/dft_report.md", report)
    if stitched:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "dft/scan_stitched_netlist.v", _read_text(scan_netlist_path))

    digital = state.setdefault("digital", {})
    digital["dft"] = {
        "status": status,
        "summary_json": summary_path,
        "report_md": report_path,
        "log": log_path,
        "script": tcl_path,
        "scan_stitched_netlist": scan_netlist_path if stitched else None,
    }
    state["status"] = f"{AGENT_NAME}: {status}"
    return state
