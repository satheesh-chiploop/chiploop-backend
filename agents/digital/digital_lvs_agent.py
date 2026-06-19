import os
import json
import glob
import shutil
import subprocess
import re
import logging
from datetime import datetime
logger = logging.getLogger("chiploop")

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital LVS Agent"
DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _select_single_top_netlist(paths: list[str]) -> list[str]:
    if len(paths) <= 1:
        return paths
    physical = [p for p in paths if os.path.basename(p).endswith((".pnl.v", ".nl.v"))]
    if physical:
        return [sorted(physical, key=lambda p: (0 if ".pnl." in os.path.basename(p).lower() else 1, 0 if ".nl." in os.path.basename(p).lower() else 1, len(p)))[0]]
    logical = [p for p in paths if not os.path.basename(p).endswith((".pnl.v", ".nl.v"))]
    if logical:
        return [sorted(logical, key=lambda p: (0 if "synth" in os.path.basename(p).lower() else 1, len(p)))[0]]
    return [sorted(paths, key=lambda p: (0 if "synth" in os.path.basename(p).lower() else 1, len(p)))[0]]


def _clear_stage_netlists(netlist_dir: str) -> None:
    for old in glob.glob(os.path.join(netlist_dir, "*.v")):
        try:
            os.remove(old)
        except OSError:
            logger.warning(f"{AGENT_NAME}: could not remove stale netlist {old}")


def _closure_overrides(state: dict, workflow_dir: str, stage: str) -> dict:
    plan = state.get("signoff_closure_plan") if isinstance(state.get("signoff_closure_plan"), dict) else {}
    if not plan:
        plan = _read_json(os.path.join(workflow_dir, "digital", "signoff_closure", "signoff_closure_plan.json"))
    eco = plan.get("eco_profile") if isinstance(plan.get("eco_profile"), dict) else {}
    overrides = eco.get("config_overrides") if isinstance(eco.get("config_overrides"), dict) else {}
    stage_overrides = overrides.get(stage) if isinstance(overrides.get(stage), dict) else {}
    return dict(stage_overrides)


def _run(cmd: list[str], cwd: str, state: dict | None = None) -> tuple[int, str]:
    p = run_command(state or {}, "digital_lvs", [str(x) for x in cmd], cwd=cwd, timeout_sec=1800)
    return p.returncode if p.returncode is not None else 1, (p.stdout or "") + (p.stderr or "")


def _latest_run_dir(run_work_dir: str) -> str | None:
    run_roots = [
        os.path.join(run_work_dir, "runs"),
        os.path.join(run_work_dir, "lvs", "runs"),
    ]
    dirs = []
    for runs_dir in run_roots:
        if not os.path.isdir(runs_dir):
            continue
        dirs.extend(os.path.join(runs_dir, d) for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d)))
    if not dirs:
        return None
    dirs.sort(key=lambda p: os.path.getmtime(p))
    return dirs[-1]


def _copy_metrics(latest: str | None, stage_dir: str) -> str | None:
    if not latest:
        return None
    src = os.path.join(latest, "final", "metrics.json")
    dst = os.path.join(stage_dir, "metrics.json")
    if os.path.exists(src):
        shutil.copy2(src, dst)
        return dst
    return None


def _copy_lvs_report(latest: str | None, stage_dir: str) -> tuple[str | None, str | None]:
    if not latest:
        return None, None
    reports = sorted(glob.glob(os.path.join(latest, "**", "*lvs*.rpt"), recursive=True))
    reports += sorted(glob.glob(os.path.join(latest, "**", "*lvs*.log"), recursive=True))
    if not reports:
        return None, None
    src = reports[-1]
    reports_dir = os.path.join(stage_dir, "reports")
    _ensure_dir(reports_dir)
    dst = os.path.join(reports_dir, os.path.basename(src))
    shutil.copy2(src, dst)
    return dst, _lvs_status_from_text(_read_text(dst))


def _read_text(path: str | None) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _xor_difference_count(log: str) -> int | None:
    counts = [int(match.group(1)) for match in re.finditer(r"Total XOR differences:\s*(\d+)", log or "", flags=re.IGNORECASE)]
    if counts:
        return counts[-1]
    match = re.search(r"(\d+)\s+XOR differences found", log or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _lvs_failure_details(text: str) -> dict[str, object]:
    bus_mismatches: list[str] = []
    implicit_pin_records: list[dict[str, str]] = []
    disconnected_counts: list[dict[str, int]] = []
    for line in (text or "").splitlines():
        if "bus width" in line and "does not match port" in line:
            bus_mismatches.append(line.strip())
        count_match = re.search(r"Circuit contains\s+(\d+)\s+nets(?:,\s+and\s+(\d+)\s+disconnected pins)?", line)
        if count_match:
            disconnected_counts.append({
                "nets": int(count_match.group(1)),
                "disconnected_pins": int(count_match.group(2) or 0),
            })
        match = re.search(r"Implicit pin\s+(.+?)\s+in instance\s+(.+?)\s+of\s+(.+?)\s+in cell\s+(.+)", line)
        if match:
            implicit_pin_records.append({
                "pin": match.group(1).strip(),
                "instance": match.group(2).strip(),
                "model": match.group(3).strip(),
                "cell": match.group(4).strip(),
            })
    implicit_pins = [item["pin"] for item in implicit_pin_records]
    stdcell_implicit_outputs = [
        item for item in implicit_pin_records
        if item["model"].startswith("sky130_") and item["pin"] in {"X", "Y", "Q", "Q_N"}
    ]
    macro_bus_implicit = [
        item for item in implicit_pin_records
        if "[" in item["pin"] and "]" in item["pin"] and not item["model"].startswith("sky130_")
    ]
    if bus_mismatches or macro_bus_implicit:
        return {
            "failure_reason": "macro_bus_width_mismatch",
            "bus_width_mismatches": bus_mismatches[:20],
            "implicit_pins": implicit_pins[:50],
            "implicit_pin_records": implicit_pin_records[:50],
        }
    if stdcell_implicit_outputs:
        return {
            "failure_reason": "physical_netlist_missing_stdcell_outputs",
            "implicit_pins": implicit_pins[:50],
            "implicit_pin_records": implicit_pin_records[:50],
        }
    if "Top level cell failed pin matching" in (text or ""):
        details: dict[str, object] = {"failure_reason": "top_level_pin_mismatch"}
        if len(disconnected_counts) >= 2:
            source_counts = disconnected_counts[-2]
            layout_counts = disconnected_counts[-1]
            details.update({
                "source_disconnected_pins": source_counts["disconnected_pins"],
                "layout_disconnected_pins": layout_counts["disconnected_pins"],
            })
            if source_counts["disconnected_pins"] != layout_counts["disconnected_pins"]:
                details["failure_reason"] = "top_level_disconnected_pin_mismatch"
        return details
    if "Final result: Netlists do not match" in (text or ""):
        return {"failure_reason": "netlists_do_not_match"}
    return {}


def _lvs_status_from_text(text: str) -> str | None:
    lower = (text or "").lower()
    if "circuits match uniquely" in lower:
        return "clean"
    if "netlists match uniquely" in lower:
        return "clean"
    if (
        "property errors were found" in lower
        or "netlists do not match" in lower
        or "circuits do not match" in lower
        or "failed pin matching" in lower
        or "pin matching failed" in lower
        or "top level cell failed" in lower
    ):
        return "mismatch"
    return None


def _lvs_status(rc: int, report_status: str | None, log: str) -> str:
    from_log = _lvs_status_from_text(log)
    if report_status:
        return report_status
    if from_log:
        return from_log
    if rc == 0:
        return "completed"
    if _xor_difference_count(log):
        return "completed_with_deferred_xor_error"
    return "failed"


def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None

def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    drc_state = digital.get("drc") or {}

    cand = drc_state.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital.drc -> {cand}")
        return cand

    cand = digital.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital -> {cand}")
        return cand

    for stage in ["drc", "fill", "route", "sta_postroute", "sta_postcts", "cts", "place", "floorplan", "impl_setup", "synth"]:
        cands = sorted(glob.glob(os.path.join(workflow_dir, "digital", stage, "constraints", "*.sdc")))
        for cand in cands:
            if os.path.exists(cand):
                logger.info(f"{AGENT_NAME}: selected SDC from {stage} -> {cand}")
                return cand

    legacy = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))
    for cand in legacy:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected legacy SDC -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found")
    return None


def _resolve_config_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}
    drc_state = digital.get("drc") or {}

    # LVS is a physical signoff stage; prefer latest physical configs before
    # prior signoff/global pointers so closure ECOs are not lost.
    for cand in [
        os.path.join(workflow_dir, "digital", "fill", "config.json"),
        os.path.join(workflow_dir, "digital", "sta_postfill", "config.json"),
        os.path.join(workflow_dir, "digital", "route", "config.json"),
        os.path.join(workflow_dir, "digital", "sta_postroute", "config.json"),
        os.path.join(workflow_dir, "digital", "cts", "config.json"),
        os.path.join(workflow_dir, "digital", "sta_postcts", "config.json"),
        os.path.join(workflow_dir, "digital", "place", "config.json"),
        os.path.join(workflow_dir, "digital", "sta_postplace", "config.json"),
        os.path.join(workflow_dir, "digital", "floorplan", "config.json"),
        os.path.join(workflow_dir, "digital", "drc", "config.json"),
        os.path.join(workflow_dir, "digital", "impl_setup", "openlane", "config.json"),
        os.path.join(workflow_dir, "digital", "synth", "config.json"),
        os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json"),
    ]:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config fallback -> {cand}")
            return cand

    cand = drc_state.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital.drc -> {cand}")
        return cand

    cand = digital.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital -> {cand}")
        return cand

    logger.warning(f"{AGENT_NAME}: no OpenLane config found")
    return None


def _resolve_physical_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    candidates: list[str] = []
    for stage in ("sta_postfill", "fill", "sta_postroute", "route"):
        stage_state = digital.get(stage) if isinstance(digital.get(stage), dict) else {}
        for key in ("final_netlist", "netlist", "fill_netlist", "route_netlist"):
            value = stage_state.get(key)
            if isinstance(value, str):
                candidates.append(value)
    for stage in ("sta_postfill", "fill", "sta_postroute", "route"):
        candidates.extend(sorted(glob.glob(os.path.join(workflow_dir, "digital", stage, "netlist", "*.pnl.v"))))
        candidates.extend(sorted(glob.glob(os.path.join(workflow_dir, "digital", stage, "netlist", "*.nl.v"))))
        candidates.extend(sorted(glob.glob(os.path.join(workflow_dir, "digital", stage, "netlist", "*.v"))))
    for cand in candidates:
        if isinstance(cand, str) and os.path.isfile(cand):
            logger.info(f"{AGENT_NAME}: selected physical netlist -> {cand}")
            return os.path.abspath(cand)
    return None


def _resolve_macro_files_from_workflow(workflow_dir: str, exts: tuple[str, ...]) -> list[str]:
    hits = []
    for ext in exts:
        hits.extend(glob.glob(os.path.join(workflow_dir, "**", f"*{ext}"), recursive=True))

    out = []
    seen = set()
    for p in sorted(hits):
        base = os.path.basename(p).lower()

        if base.endswith("_llm_lef_raw.lef"):
            continue
        if base.endswith("_raw.lef"):
            continue
        if "debug" in base:
            continue

        ap = os.path.abspath(p)
        if ap in seen or not os.path.isfile(ap):
            continue
        seen.add(ap)
        out.append(ap)
    return out


def _resolve_stdcell_spice_models(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    explicit: list[str] = []
    for value in (
        state.get("stdcell_spice"),
        state.get("stdcell_spice_models"),
        digital.get("stdcell_spice"),
        digital.get("stdcell_spice_models"),
    ):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                cand = item if os.path.isabs(item) else os.path.join(workflow_dir, item)
                if os.path.isfile(cand):
                    explicit.append(os.path.abspath(cand))

    pdk_variant = state.get("pdk_variant") or state.get("pdk") or DEFAULT_PDK_VARIANT
    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    lib_names = ["sky130_fd_sc_hd", "sky130_ef_sc_hd"]
    patterns = []
    for lib_name in lib_names:
        patterns.extend([
            os.path.join(pdk_root_host, pdk_variant, "libs.ref", lib_name, "spice", "*.spice"),
            os.path.join(pdk_root_host, pdk_variant, "libs.ref", lib_name, "spice", "*.sp"),
            os.path.join(pdk_root_host, pdk_variant, "libs.ref", lib_name, "cdl", "*.cdl"),
            os.path.join(pdk_root_host, pdk_variant, "libs.ref", lib_name, "**", "*.spice"),
            os.path.join(pdk_root_host, pdk_variant, "libs.ref", lib_name, "**", "*.sp"),
            os.path.join(pdk_root_host, pdk_variant, "libs.ref", lib_name, "**", "*.cdl"),
        ])
    discovered: list[str] = []
    for pattern in patterns:
        discovered.extend(glob.glob(pattern, recursive=True))
    discovered = sorted(discovered, key=_stdcell_spice_sort_key)
    out: list[str] = []
    seen: set[str] = set()
    for path in sorted(explicit + discovered, key=_stdcell_spice_sort_key):
        ap = os.path.abspath(path)
        if ap in seen or not os.path.isfile(ap):
            continue
        seen.add(ap)
        out.append(ap)
    return out


def _stdcell_spice_sort_key(path: str) -> tuple[int, int, str]:
    base = os.path.basename(path).lower()
    aggregate = base in {"sky130_fd_sc_hd.spice", "sky130_fd_sc_hd.sp", "sky130_fd_sc_hd.cdl", "sky130_ef_sc_hd.spice", "sky130_ef_sc_hd.sp", "sky130_ef_sc_hd.cdl"}
    ext_rank = 0 if base.endswith(".spice") else 1 if base.endswith(".sp") else 2
    return (0 if aggregate else 1, ext_rank, base)


def _stdcell_missing_output_pins(model: str, connected_pins: set[str]) -> list[str]:
    cell = model.lower()
    expected: list[str] = []
    if "__clkbuf_" in cell or "__buf_" in cell or "__dlymetal" in cell:
        expected.append("X")
    if "__clkinv_" in cell or "__clkinvlp_" in cell or "__bufinv_" in cell or "__inv_" in cell:
        expected.append("Y")
    return [pin for pin in expected if pin not in connected_pins]


def _sanitize_lvs_netlist_unconnected_stdcell_outputs(src: str, dst: str | None = None, macro_spice_models: list[str] | None = None) -> tuple[str, int]:
    text = _read_text(src)
    if not text:
        if dst and os.path.abspath(dst) != os.path.abspath(src):
            _ensure_dir(os.path.dirname(dst))
            shutil.copy2(src, dst)
            return dst, 0
        return src, 0

    repairs = 0
    macro_ports = _spice_subckt_ports(macro_spice_models or [])
    def repl(match: re.Match) -> str:
        nonlocal repairs
        model = match.group("model")
        inst = match.group("inst")
        body = match.group("body")
        port_exprs = {
            port: expr.strip()
            for port, expr in re.findall(r"\.\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*([^)]*?)\s*\)", body, flags=re.DOTALL)
        }
        connected = set(port_exprs)
        missing = _stdcell_missing_output_pins(model, connected)
        fake_connected = [
            pin for pin in connected
            if pin in {"X", "Y", "Q", "Q_N"} and port_exprs.get(pin, "").startswith("_chiploop_lvs_nc_")
        ]
        if not missing and not fake_connected:
            return match.group(0)
        new_body = body
        for pin in fake_connected:
            new_body = re.sub(
                rf"\.\s*{re.escape(pin)}\s*\(\s*_chiploop_lvs_nc_[^)]*\)",
                f".{pin}()",
                new_body,
            )
            repairs += 1
        if not missing:
            return f"{model} {inst} ({new_body});"
        new_body = body.rstrip()
        for pin in missing:
            if new_body and not new_body.rstrip().endswith(","):
                new_body += ","
            repairs += 1
            new_body += f"\n    .{pin}()"
        return f"{model} {inst} ({new_body});"

    stdcell_pattern = re.compile(
        r"(?P<model>sky130_(?:fd|ef)_sc_hd__\S+)\s+(?P<inst>\S+)\s*\((?P<body>.*?)\);",
        flags=re.DOTALL,
    )
    repaired = stdcell_pattern.sub(repl, text)

    def macro_repl(match: re.Match) -> str:
        nonlocal repairs
        model = match.group("model")
        if model.lower().startswith("sky130_"):
            return match.group(0)
        ports = macro_ports.get(model)
        if not ports:
            return match.group(0)
        body = match.group("body")
        connected = {
            port
            for port, _expr in re.findall(r"\.\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*([^)]*?)\s*\)", body, flags=re.DOTALL)
        }
        additions: list[str] = []
        for port in ports:
            if port in connected or not _is_supply_port(port):
                continue
            additions.append(port)
        if not additions:
            return match.group(0)
        new_body = body.rstrip()
        for port in additions:
            if new_body and not new_body.rstrip().endswith(","):
                new_body += ","
            repairs += 1
            new_body += f"\n    .{port}()"
        return f"{model} {match.group('inst')} ({new_body});"

    if macro_ports:
        macro_pattern = re.compile(
            r"(?P<model>[A-Za-z_][A-Za-z0-9_$]*)\s+(?P<inst>\S+)\s*\((?P<body>.*?)\);",
            flags=re.DOTALL,
        )
        repaired = macro_pattern.sub(macro_repl, repaired)

    out = dst or src
    if repairs or (dst and os.path.abspath(dst) != os.path.abspath(src)):
        _write_text(out, repaired)
    return out, repairs


def _sanitize_openlane_lvs_run_netlists(latest: str | None, macro_spice_models: list[str] | None = None) -> dict[str, object]:
    if not latest or not os.path.isdir(latest):
        return {"repairs": 0, "files": []}
    touched: list[str] = []
    repairs = 0
    for path in sorted(glob.glob(os.path.join(latest, "**", "*.nl.v"), recursive=True)):
        _out, count = _sanitize_lvs_netlist_unconnected_stdcell_outputs(path, None, macro_spice_models)
        if count:
            repairs += count
            touched.append(path)
    for path in sorted(glob.glob(os.path.join(latest, "**", "*.pnl.v"), recursive=True)):
        _out, count = _sanitize_lvs_netlist_unconnected_stdcell_outputs(path, None, macro_spice_models)
        if count:
            repairs += count
            touched.append(path)
    return {"repairs": repairs, "files": touched[:100]}


def _write_physical_stdcell_blackbox_stubs(netlists: list[str], inputs_netlist_dir: str) -> list[str]:
    cells: dict[str, set[str]] = {}
    for path in netlists:
        text = _read_text(path)
        for match in re.finditer(
            r"(?P<model>sky130_(?:fd|ef)_sc_hd__(?:fill|decap|fakediode|tap|tapvpwrvgnd|endcap)\S*)\s+(?P<inst>\S+)\s*\((?P<body>.*?)\);",
            text,
            flags=re.DOTALL,
        ):
            model = match.group("model")
            body = match.group("body")
            ports = cells.setdefault(model, set())
            for port, _expr in re.findall(r"\.\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*([^)]*?)\s*\)", body, flags=re.DOTALL):
                ports.add(port)
    if not cells:
        return []

    lines = ["// Auto-generated physical-only stdcell blackboxes for signoff netlists."]
    for model in sorted(cells):
        ports = sorted(cells[model])
        port_list = ", ".join(ports)
        lines.append(f"(* blackbox *) module {model}({port_list});")
        for port in ports:
            lines.append(f"  inout {port};")
        lines.append("endmodule")
        lines.append("")
    path = os.path.join(inputs_netlist_dir, "chiploop_physical_stdcell_blackboxes.v")
    _write_text(path, "\n".join(lines).rstrip() + "\n")
    return [path]


def _resolve_macro_spice_models(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    candidates: list[str] = []
    gds_summary = state.get("analog_gds_generation") if isinstance(state.get("analog_gds_generation"), dict) else {}
    gds_lvs = gds_summary.get("analog_lvs") if isinstance(gds_summary.get("analog_lvs"), dict) else {}
    signoff = state.get("analog_signoff") if isinstance(state.get("analog_signoff"), dict) else {}
    signoff_lvs = signoff.get("lvs") if isinstance(signoff.get("lvs"), dict) else {}
    for value in (
        state.get("analog_lvs_source_spice"),
        signoff_lvs.get("source_spice"),
        gds_lvs.get("source_spice"),
        digital.get("macro_lvs_spice"),
    ):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                cand = item if os.path.isabs(item) else os.path.join(workflow_dir, item)
                if os.path.isfile(cand):
                    candidates.append(os.path.abspath(cand))
    candidates.extend(sorted(glob.glob(os.path.join(workflow_dir, "analog", "signoff", "*_lvs_source.spice"))))
    for summary_path in (
        os.path.join(workflow_dir, "analog", "physical_package", "analog_physical_collateral_package.json"),
        os.path.join(workflow_dir, "analog", "sky130", "sky130_spice_summary.json"),
    ):
        summary = _read_json(summary_path)
        for value in (
            summary.get("lvs_spice"),
            summary.get("spice"),
            (summary.get("spice_generation") or {}).get("spice") if isinstance(summary.get("spice_generation"), dict) else None,
        ):
            if isinstance(value, str):
                cand = value if os.path.isabs(value) else os.path.join(workflow_dir, value)
                if os.path.isfile(cand):
                    candidates.append(os.path.abspath(cand))
    for value in (
        state.get("analog_spice_path"),
        state.get("analog_netlist_path"),
        state.get("macro_spice"),
        digital.get("macro_spice"),
        digital.get("macro_spice_models"),
    ):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                cand = item if os.path.isabs(item) else os.path.join(workflow_dir, item)
                if os.path.isfile(cand):
                    candidates.append(os.path.abspath(cand))
    candidates.extend(_resolve_macro_files_from_workflow(workflow_dir, (".spice", ".sp", ".cdl")))
    out: list[str] = []
    seen: set[str] = set()
    seen_subckts: set[str] = set()
    for path in candidates:
        norm = path.replace("\\", "/").lower()
        base = os.path.basename(path).lower()
        if (
            "ngspice_characterization" in base
            or "characterize_" in base
            or "_extracted" in base
            or "/analog/lib_char/" in norm
            or ("/analog/gds/" in norm and not base.endswith("_lvs_source.spice"))
        ):
            continue
        ap = os.path.abspath(path)
        if ap in seen or not os.path.isfile(ap):
            continue
        subckts = _spice_subckt_names(ap)
        if subckts and seen_subckts.intersection(subckts):
            continue
        seen.add(ap)
        seen_subckts.update(subckts)
        out.append(ap)
    return out


def _spice_subckt_names(path: str) -> set[str]:
    text = _read_text(path)
    return set(re.findall(r"^\s*\.subckt\s+(\S+)", text, flags=re.IGNORECASE | re.MULTILINE))


def _spice_subckt_ports(paths: list[str]) -> dict[str, list[str]]:
    ports: dict[str, list[str]] = {}
    for path in paths:
        text = _read_text(path)
        if not text:
            continue
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line.lower().startswith(".subckt "):
                i += 1
                continue
            merged = line
            i += 1
            while i < len(lines) and lines[i].lstrip().startswith("+"):
                merged += " " + lines[i].lstrip()[1:].strip()
                i += 1
            parts = merged.split()
            if len(parts) >= 2 and not parts[1].lower().startswith("sky130_"):
                ports[parts[1]] = parts[2:]
    return ports


def _verilog_declared_ports(text: str) -> set[str]:
    ports: set[str] = set()
    for match in re.finditer(r"^\s*(?:input|output|inout)\s+(?:wire\s+|reg\s+)?(?:\[[^\]]+\]\s*)?([^;]+);", text or "", flags=re.MULTILINE):
        for name in match.group(1).split(","):
            clean = name.strip().split()[-1] if name.strip() else ""
            clean = clean.strip("\\ ")
            if clean:
                ports.add(clean)
    header = re.search(r"\bmodule\s+\S+\s*\((.*?)\)\s*;", text or "", flags=re.DOTALL)
    if header:
        for item in header.group(1).split(","):
            clean = re.sub(r"\b(?:input|output|inout|wire|reg|logic|signed)\b", " ", item)
            clean = re.sub(r"\[[^\]]+\]", " ", clean).strip()
            if clean:
                ports.add(clean.split()[-1].strip("\\ "))
    return ports


def _is_supply_port(name: str) -> bool:
    low = name.lower()
    return low in {"vpwr", "vgnd", "vdd", "vss", "vcc", "gnd", "avdd", "avss", "dvdd", "dvss"} or "vdd" in low or "vss" in low or "pwr" in low or "gnd" in low


def _supply_net_for_port(port: str, top_ports: set[str]) -> str | None:
    low = port.lower()
    power_order = [port, "VPWR", "VPB", "avdd", "dvdd", "vdd", "VDD", "vccd1", "vdda1"]
    ground_order = [port, "VGND", "VNB", "avss", "dvss", "vss", "VSS", "gnd", "GND", "vssd1", "vssa1"]
    order = ground_order if ("gnd" in low or "vss" in low or low in {"vgnd", "vnb"}) else power_order
    lower_ports = {p.lower(): p for p in top_ports}
    for cand in order:
        if cand in top_ports:
            return cand
        if cand.lower() in lower_ports:
            return lower_ports[cand.lower()]
    return None


def _stage_spice_models(work_stage_dir: str, stdcell_spice: list[str], macro_spice: list[str]) -> tuple[list[str], list[str]]:
    spice_dir = os.path.join(work_stage_dir, "inputs", "spice")
    _ensure_dir(spice_dir)
    staged_stdcell: list[str] = []
    staged_extra: list[str] = []
    seen: set[str] = set()
    for bucket, srcs in ((staged_stdcell, stdcell_spice), (staged_extra, macro_spice)):
        for src in srcs:
            if not isinstance(src, str) or not os.path.isfile(src):
                continue
            basename = os.path.basename(src)
            dst = os.path.join(spice_dir, basename)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
            rel = f"dir::inputs/spice/{basename}"
            if rel not in seen:
                seen.add(rel)
                bucket.append(rel)
    return staged_stdcell, staged_extra


def _stage_macro_inputs(state: dict, workflow_dir: str, work_stage_dir: str) -> tuple[list[str], list[str], list[str]]:
    digital = state.get("digital") or {}

    macro_lefs = list(dict.fromkeys(p for p in (digital.get("macro_lefs") or []) if isinstance(p, str) and os.path.isfile(p)))
    macro_libs = list(dict.fromkeys(p for p in (digital.get("macro_libs") or []) if isinstance(p, str) and os.path.isfile(p)))
    macro_gds  = list(dict.fromkeys(p for p in (digital.get("macro_gds") or []) if isinstance(p, str) and os.path.isfile(p)))

    inputs_dir = os.path.join(work_stage_dir, "inputs", "macros")
    lef_dir = os.path.join(inputs_dir, "lef")
    lib_dir = os.path.join(inputs_dir, "lib")
    gds_dir = os.path.join(inputs_dir, "gds")
    _ensure_dir(lef_dir)
    _ensure_dir(lib_dir)
    _ensure_dir(gds_dir)

    staged_lefs, staged_libs, staged_gds = [], [], []
    seen_staged_lefs, seen_staged_libs, seen_staged_gds = set(), set(), set()

    for src in macro_lefs:
        basename = os.path.basename(src)
        dst = os.path.join(lef_dir, basename)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        rel = f"dir::inputs/macros/lef/{basename}"
        if rel not in seen_staged_lefs:
            staged_lefs.append(rel)
            seen_staged_lefs.add(rel)

    for src in macro_libs:
        basename = os.path.basename(src)
        dst = os.path.join(lib_dir, basename)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        rel = f"dir::inputs/macros/lib/{basename}"
        if rel not in seen_staged_libs:
            staged_libs.append(rel)
            seen_staged_libs.add(rel)

    for src in macro_gds:
        basename = os.path.basename(src)
        dst = os.path.join(gds_dir, basename)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        rel = f"dir::inputs/macros/gds/{basename}"
        if rel not in seen_staged_gds:
            staged_gds.append(rel)
            seen_staged_gds.add(rel)

    return staged_lefs, staged_libs, staged_gds


def _stage_macro_placement_cfg_if_needed(cfg: dict, state: dict, workflow_dir: str, work_stage_dir: str) -> str | None:
    if not cfg.get("MACRO_PLACEMENT_CFG"):
        return None
    digital = state.get("digital") or {}
    place_state = digital.get("place") or {}
    candidates = [
        place_state.get("macro_placement_cfg"),
        os.path.join(workflow_dir, "digital", "place", "macro_placement.cfg"),
    ]
    src = next((p for p in candidates if isinstance(p, str) and os.path.isfile(p)), None)
    if not src:
        cfg.pop("MACRO_PLACEMENT_CFG", None)
        return None
    dst = os.path.join(work_stage_dir, "inputs", "macros", "macro_placement.cfg")
    _ensure_dir(os.path.dirname(dst))
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    cfg["MACRO_PLACEMENT_CFG"] = "dir::inputs/macros/macro_placement.cfg"
    return dst


def _macro_blackbox_deferred(staged_lefs: list[str], staged_libs: list[str], staged_gds: list[str]) -> bool:
    return bool((staged_lefs or staged_libs) and not staged_gds)

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "lvs")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")
    netlist_dir = os.path.join(stage_dir, "netlist")

    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)
    _ensure_dir(netlist_dir)

    logger.info(f"🏁 Running {AGENT_NAME}")

    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        raise RuntimeError("Missing upstream SDC: no constraints_sdc found in state or prior stages.")

    sdc_basename = os.path.basename(upstream_sdc)
    stage_sdc = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, stage_sdc)
    with open(stage_sdc, "r", encoding="utf-8") as f:
        sdc_text = f.read()

    base_cfg_path = _resolve_config_from_state(state, workflow_dir)
    if not base_cfg_path:
        raise RuntimeError("Missing OpenLane config: no config found in state or prior stages.")
    logger.info(f"{AGENT_NAME}: base_cfg_path={base_cfg_path}")

    cfg = _read_json(base_cfg_path)
    cfg.pop("SYNTH_SDC_FILE", None)
    cfg["RUN_LINTER"] = False
    cfg["RUN_XOR"] = False

    run_work_dir = state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work")
    run_work_dir = os.path.abspath(run_work_dir)
    _ensure_dir(run_work_dir)
    state["digital_run_work_dir"] = run_work_dir

    inputs_dir = os.path.join(run_work_dir, "inputs")
    inputs_constraints_dir = os.path.join(inputs_dir, "constraints")
    inputs_netlist_dir = os.path.join(inputs_dir, "netlist")
    _ensure_dir(inputs_constraints_dir)
    _ensure_dir(inputs_netlist_dir)

    shutil.copy2(stage_sdc, os.path.join(inputs_constraints_dir, sdc_basename))
    cfg["PNR_SDC_FILE"] = f"inputs/constraints/{sdc_basename}"

    physical_netlist = _resolve_physical_netlist(state, workflow_dir)
    if physical_netlist:
        dst = os.path.join(inputs_netlist_dir, os.path.basename(physical_netlist))
        if os.path.abspath(physical_netlist) != os.path.abspath(dst):
            shutil.copy2(physical_netlist, dst)

    macro_spice_models = _resolve_macro_spice_models(state, workflow_dir)

    stage_netlists = _select_single_top_netlist(sorted(glob.glob(os.path.join(inputs_netlist_dir, "*.v"))))
    if not stage_netlists:
        raise RuntimeError("LVS: missing run_work/inputs/netlist/*.v (synth/floorplan should populate it).")

    _clear_stage_netlists(netlist_dir)
    sanitized_stage_netlists: list[str] = []
    lvs_netlist_repairs = 0
    for nl in stage_netlists:
        base, ext = os.path.splitext(os.path.basename(nl))
        sanitized_base = f"{base}_lvs{ext}" if not base.endswith("_lvs") else f"{base}{ext}"
        inputs_sanitized = os.path.join(inputs_netlist_dir, sanitized_base)
        _sanitized, repairs = _sanitize_lvs_netlist_unconnected_stdcell_outputs(nl, inputs_sanitized, macro_spice_models)
        lvs_netlist_repairs += repairs
        stage_copy = os.path.join(netlist_dir, sanitized_base)
        shutil.copy2(inputs_sanitized, stage_copy)
        sanitized_stage_netlists.append(stage_copy)

    stage_netlists_local = _select_single_top_netlist(sanitized_stage_netlists)
    physical_stdcell_stubs = _write_physical_stdcell_blackbox_stubs(stage_netlists_local, inputs_netlist_dir)
    cfg["VERILOG_FILES"] = [
        *[f"inputs/netlist/{os.path.basename(p)}" for p in physical_stdcell_stubs],
        *[f"inputs/netlist/{os.path.basename(p)}" for p in stage_netlists_local],
    ]

    inferred = None
    if str(cfg.get("DESIGN_NAME", "")).strip() in ["", "top"]:
        inferred = _infer_top_from_netlist(stage_netlists_local[0])
    if inferred:
        cfg["DESIGN_NAME"] = inferred
        state["design_name"] = inferred

    top_module = str(cfg.get("DESIGN_NAME", "")).strip() or "top"

    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or state.get("workflow_type") or state.get("flow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag

    pdk_variant = state.get("pdk_variant") or DEFAULT_PDK_VARIANT
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE
    pdk_root_host = state.get("pdk_root_host") or os.getenv("CHIPLOOP_PDK_ROOT_HOST") or "backend/pdk"
    pdk_root_host = os.path.abspath(pdk_root_host)
    state["pdk_root_host"] = pdk_root_host

    work_stage_dir = os.path.join(run_work_dir, "lvs")
    _ensure_dir(work_stage_dir)


    staged_lefs, staged_libs, staged_gds = _stage_macro_inputs(state, workflow_dir, work_stage_dir)
    macro_placement_cfg = _stage_macro_placement_cfg_if_needed(cfg, state, workflow_dir, work_stage_dir)
    staged_cell_spice, staged_extra_spice = _stage_spice_models(
        work_stage_dir,
        _resolve_stdcell_spice_models(state, workflow_dir),
        macro_spice_models,
    )

    if staged_lefs:
        cfg["EXTRA_LEFS"] = staged_lefs
    if staged_libs:
        cfg["EXTRA_LIBS"] = staged_libs
    if staged_gds:
        cfg["EXTRA_GDS_FILES"] = staged_gds
    if staged_cell_spice:
        cfg["CELL_SPICE_MODELS"] = staged_cell_spice
    if staged_extra_spice:
        cfg["EXTRA_SPICE_MODELS"] = staged_extra_spice
    if not (staged_lefs or staged_libs or staged_gds):
        cfg.pop("EXTRA_LEFS", None)
        cfg.pop("EXTRA_LIBS", None)
        cfg.pop("EXTRA_GDS_FILES", None)
        cfg.pop("MACRO_PLACEMENT_CFG", None)
        cfg.pop("MACROS", None)
        cfg.pop("FP_DEF_TEMPLATE", None)

    closure_overrides = _closure_overrides(state, workflow_dir, "lvs")
    cfg.update(closure_overrides)

    logger.info(f"{AGENT_NAME}: staged macro LEFs -> {staged_lefs}")
    logger.info(f"{AGENT_NAME}: staged macro LIBs -> {staged_libs}")
    logger.info(f"{AGENT_NAME}: staged macro GDS -> {staged_gds}")
    logger.info(f"{AGENT_NAME}: staged stdcell SPICE -> {staged_cell_spice}")
    logger.info(f"{AGENT_NAME}: staged extra SPICE -> {staged_extra_spice}")

    config_path = os.path.join(stage_dir, "config.json")
    _write_text(config_path, json.dumps(cfg, indent=2))

    if _macro_blackbox_deferred(staged_lefs, staged_libs, staged_gds):
        out = "Analog macro LEF/LIB collateral is present but macro GDS is unavailable; LVS is deferred in black-box mode.\n"
        _write_text(os.path.join(logs_dir, "openlane_lvs.log"), out)
        input_resolution_log = "\n".join([
            f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
            f"workflow_id={workflow_id}",
            f"workflow_dir={workflow_dir}",
            f"top_module={top_module}",
            f"macro_lef_count={len(staged_lefs)}",
            f"macro_lib_count={len(staged_libs)}",
            f"macro_gds_count={len(staged_gds)}",
            f"macro_placement_cfg={cfg.get('MACRO_PLACEMENT_CFG')}",
            f"macro_placement_cfg_path={macro_placement_cfg}",
            "status=blackbox_deferred",
            "reason=analog_macro_gds_missing",
        ]) + "\n"
        input_resolution_log_path = os.path.join(logs_dir, "lvs_input_resolution.log")
        _write_text(input_resolution_log_path, input_resolution_log)
        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "blackbox_deferred",
            "lvs_status": "blackbox_deferred",
            "xor_differences": None,
            "return_code": 0,
            "blackbox_macros": (state.get("digital") or {}).get("macro_blackboxes") or [],
            "reason": "analog_macro_gds_missing",
            "outputs": {
                "sdc": f"digital/lvs/constraints/{sdc_basename}",
                "metrics_json": None,
                "lvs_report": None,
                "log": "digital/lvs/logs/openlane_lvs.log",
                "openlane_run_dir": None,
            },
        }
        _write_text(os.path.join(stage_dir, "lvs_summary.json"), json.dumps(summary, indent=2))
        _write_text(os.path.join(stage_dir, "lvs_summary.md"), "# LVS (Netgen)\n\n- status: `blackbox_deferred`\n- reason: `analog_macro_gds_missing`\n")
        try:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/config.json", json.dumps(cfg, indent=2))
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"lvs/constraints/{sdc_basename}", sdc_text)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/logs/openlane_lvs.log", out)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/logs/lvs_input_resolution.log", input_resolution_log)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/lvs_summary.json", json.dumps(summary, indent=2))
        except Exception as e:
            print(f"⚠️ {AGENT_NAME} upload failed: {e}")
        state.setdefault("digital", {})["lvs"] = {
            "status": "blackbox_deferred",
            "lvs_status": "blackbox_deferred",
            "stage_dir": stage_dir,
            "constraints_sdc": stage_sdc,
            "openlane_config": config_path,
            "input_resolution_log": input_resolution_log_path,
            "blackbox_macros": summary["blackbox_macros"],
        }
        return state

    exec_config_path = os.path.join(work_stage_dir, "config.json")
    _write_text(exec_config_path, json.dumps(cfg, indent=2))

    input_log = "\n".join([
        f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={workflow_dir}",
        f"upstream_sdc={upstream_sdc}",
        f"sdc_basename={sdc_basename}",
        f"stage_sdc={stage_sdc}",
        f"base_cfg_path={base_cfg_path}",
        f"run_work_dir={run_work_dir}",
        f"run_tag={run_tag}",
        f"top_module={top_module}",
        f"resolved_physical_netlist={physical_netlist}",
        f"selected_verilog_files={cfg.get('VERILOG_FILES')}",
        f"netlist_count={len(stage_netlists_local)}",
        f"lvs_netlist_repairs={lvs_netlist_repairs}",
        f"physical_stdcell_stub_count={len(physical_stdcell_stubs)}",
        f"macro_placement_cfg={cfg.get('MACRO_PLACEMENT_CFG')}",
        f"macro_placement_cfg_path={macro_placement_cfg}",
        f"cell_spice_count={len(staged_cell_spice)}",
        f"extra_spice_count={len(staged_extra_spice)}",
        f"closure_overrides={json.dumps(closure_overrides, sort_keys=True)}",
    ]) + "\n"
    _write_text(os.path.join(logs_dir, "lvs_input_resolution.log"), input_log)

    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "PDK_VARIANT={pdk_variant}"
echo "OPENLANE_IMAGE={openlane_image}"
echo "WORKDIR=/work"

export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {openlane_image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to Netgen.LVS lvs/config.json'
"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir, state=state)
    _write_text(os.path.join(logs_dir, "openlane_lvs.log"), out)

    latest = _latest_run_dir(work_stage_dir)
    metrics_path = _copy_metrics(latest, stage_dir)
    lvs_report_path, report_lvs_status = _copy_lvs_report(latest, stage_dir)
    lvs_status = _lvs_status(rc, report_lvs_status, out)
    xor_differences = _xor_difference_count(out)
    report_text = _read_text(lvs_report_path) if lvs_report_path else ""
    failure_details = _lvs_failure_details("\n".join([out or "", report_text or ""]))
    lvs_repair: dict[str, object] = {"attempted": False}

    if lvs_status not in {"clean", "completed"} and failure_details.get("failure_reason") == "physical_netlist_missing_stdcell_outputs":
        sanitation = _sanitize_openlane_lvs_run_netlists(latest, macro_spice_models)
        repair_count = int(sanitation.get("repairs") or 0)
        lvs_repair = {
            "attempted": repair_count > 0,
            "reason": "physical_netlist_missing_stdcell_outputs",
            "netlist_repairs": repair_count,
            "files": sanitation.get("files") or [],
        }
        if repair_count > 0:
            repair_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} repaired Netgen LVS retry =="
export OPENLANE_NUM_CORES={DEFAULT_NUM_CORES}

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {openlane_image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --from Netgen.LVS --to Netgen.LVS lvs/config.json'
"""
            repair_sh_path = os.path.join(stage_dir, "run_lvs_repair.sh")
            _write_text(repair_sh_path, repair_sh)
            os.chmod(repair_sh_path, 0o755)
            repair_rc, repair_out = _run(["bash", "-lc", "./run_lvs_repair.sh"], cwd=stage_dir, state=state)
            _write_text(os.path.join(logs_dir, "openlane_lvs_repair.log"), repair_out)
            out = "\n".join([out, "\n== ChipLoop LVS repair retry ==\n", repair_out])
            _write_text(os.path.join(logs_dir, "openlane_lvs.log"), out)
            latest = _latest_run_dir(work_stage_dir)
            metrics_path = _copy_metrics(latest, stage_dir) or metrics_path
            lvs_report_path, report_lvs_status = _copy_lvs_report(latest, stage_dir)
            report_text = _read_text(lvs_report_path) if lvs_report_path else ""
            lvs_status = _lvs_status(repair_rc, report_lvs_status, repair_out)
            rc = repair_rc
            xor_differences = _xor_difference_count(repair_out) or xor_differences
            failure_details = _lvs_failure_details("\n".join([repair_out or "", report_text or ""]))
            lvs_repair.update({
                "return_code": repair_rc,
                "status_after_repair": lvs_status,
                "log": "digital/lvs/logs/openlane_lvs_repair.log",
            })

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if lvs_status in {"clean", "completed"} else lvs_status,
        "lvs_status": lvs_status,
        "xor_differences": xor_differences,
        "return_code": rc,
        "outputs": {
            "sdc": f"digital/lvs/constraints/{sdc_basename}",
            "metrics_json": "digital/lvs/metrics.json" if metrics_path else None,
            "lvs_report": f"digital/lvs/reports/{os.path.basename(lvs_report_path)}" if lvs_report_path else None,
            "log": "digital/lvs/logs/openlane_lvs.log",
            "openlane_run_dir": latest,
        },
    }
    if lvs_repair.get("attempted"):
        summary["lvs_repair"] = lvs_repair
    if lvs_status not in {"clean", "completed"}:
        summary.update(failure_details)

    _write_text(os.path.join(stage_dir, "lvs_summary.json"), json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir, "lvs_summary.md"), f"# LVS (Netgen)\n\n- status: `{summary['status']}` (rc={rc})\n")

    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/config.json", json.dumps(cfg, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"lvs/constraints/{sdc_basename}", sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/run.sh", run_sh)
        repair_sh_path = os.path.join(stage_dir, "run_lvs_repair.sh")
        if os.path.exists(repair_sh_path):
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/run_lvs_repair.sh", _read_text(repair_sh_path))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/logs/openlane_lvs.log", out)
        repair_log_path = os.path.join(logs_dir, "openlane_lvs_repair.log")
        if os.path.exists(repair_log_path):
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/logs/openlane_lvs_repair.log", _read_text(repair_log_path))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/lvs_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/logs/lvs_input_resolution.log", input_log)
        if lvs_report_path and os.path.exists(lvs_report_path):
            save_text_artifact_and_record(
                workflow_id,
                AGENT_NAME,
                "digital",
                f"lvs/reports/{os.path.basename(lvs_report_path)}",
                _read_text(lvs_report_path),
            )
        if metrics_path and os.path.exists(metrics_path):
            with open(metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lvs/metrics.json", f.read())
    except Exception as e:
        print(f"⚠️ {AGENT_NAME} upload failed: {e}")

    state.setdefault("digital", {})["lvs"] = {
        "status": summary["status"],
        "lvs_status": lvs_status,
        "xor_differences": xor_differences,
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": config_path,
        "input_resolution_log": os.path.join(logs_dir, "lvs_input_resolution.log"),
        "openlane_run_dir": latest,
    }
    return state
