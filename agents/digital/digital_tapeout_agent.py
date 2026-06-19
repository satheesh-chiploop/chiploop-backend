import os
import json
import glob
import shutil
import subprocess
import re
import logging
from datetime import datetime
from xml.etree import ElementTree

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger("chiploop")

AGENT_NAME = "Digital Tapeout Agent"
DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))
DEFAULT_NONBLOCKING_XOR_LAYERS = {
    item.strip()
    for item in os.getenv("CHIPLOOP_XOR_NONBLOCKING_LAYERS", "235/4").split(",")
    if item.strip()
}
DEFAULT_XOR_SIGNOFF_MODE = os.getenv("CHIPLOOP_XOR_SIGNOFF_MODE", "not_applicable").strip().lower()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


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
    p = run_command(state or {}, "digital_tapeout", [str(x) for x in cmd], cwd=cwd, timeout_sec=1800)
    return p.returncode if p.returncode is not None else 1, (p.stdout or "") + (p.stderr or "")


def _latest_run_dir(run_work_dir: str) -> str | None:
    run_roots = [
        os.path.join(run_work_dir, "runs"),
        os.path.join(run_work_dir, "tapeout", "runs"),
    ]
    dirs = []
    for runs_dir in run_roots:
        if not os.path.isdir(runs_dir):
            continue
        dirs.extend(
            os.path.join(runs_dir, d)
            for d in os.listdir(runs_dir)
            if os.path.isdir(os.path.join(runs_dir, d))
        )
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


def _copy_xor_report(latest: str | None, stage_dir: str) -> str | None:
    if not latest:
        return None
    candidates = sorted(glob.glob(os.path.join(latest, "**", "xor.xml"), recursive=True))
    if not candidates:
        candidates = sorted(glob.glob(os.path.join(latest, "**", "*xor*.xml"), recursive=True))
    if not candidates:
        return None
    src = candidates[-1]
    reports_dir = os.path.join(stage_dir, "reports")
    _ensure_dir(reports_dir)
    dst = os.path.join(reports_dir, "xor.xml")
    shutil.copy2(src, dst)
    return dst


def _xor_layer_counts(report_path: str | None) -> dict[str, int]:
    if not report_path or not os.path.exists(report_path):
        return {}
    try:
        root = ElementTree.parse(report_path).getroot()
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for item in root.findall(".//item"):
        category = (item.findtext("category") or "").strip().strip("'\"")
        if category:
            counts[category] = counts.get(category, 0) + 1
    return counts


def _blocking_xor_difference_count(total_count: int | None, layer_counts: dict[str, int], nonblocking_layers: set[str] | None = None) -> int | None:
    if not layer_counts:
        return total_count
    ignored = nonblocking_layers or DEFAULT_NONBLOCKING_XOR_LAYERS
    return sum(count for layer, count in layer_counts.items() if layer not in ignored)


def _xor_signoff_status(mode: str | None = None) -> str:
    value = (mode or DEFAULT_XOR_SIGNOFF_MODE or "not_applicable").strip().lower()
    if value in {"blocking", "gate", "gated"}:
        return "enabled"
    return "not_applicable"


def _pick_gds(latest: str | None) -> tuple[str | None, str | None]:
    if not latest:
        return (None, None)

    gds_files = glob.glob(os.path.join(latest, "**", "*.gds"), recursive=True)
    if not gds_files:
        return (None, None)

    klayout_gds = None
    magic_gds = None

    for path in gds_files:
        lp = path.lower()
        if "klayout" in lp and klayout_gds is None:
            klayout_gds = path
        if "magic" in lp and magic_gds is None:
            magic_gds = path

    if klayout_gds is None:
        klayout_gds = gds_files[0]
    if magic_gds is None and len(gds_files) > 1:
        for cand in gds_files:
            if cand != klayout_gds:
                magic_gds = cand
                break

    return (klayout_gds, magic_gds)


def _stage_status(state: dict, stage: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    stage_state = digital.get(stage) if isinstance(digital.get(stage), dict) else {}
    if stage == "drc":
        value = stage_state.get("drc_status") or stage_state.get("status")
    elif stage == "lvs":
        value = stage_state.get("lvs_status") or stage_state.get("status")
    else:
        value = stage_state.get("status")
    return str(value).strip().lower() if value is not None else None


def _stage_is_clean(stage: str, status: str | None) -> bool:
    if stage == "drc":
        return status in {"ok", "clean", "completed"}
    if stage == "lvs":
        return status in {"ok", "clean", "completed"}
    return status == "ok"


def _signoff_blocking_reasons(drc_status: str | None, lvs_status: str | None) -> list[str]:
    reasons: list[str] = []
    if drc_status == "blackbox_deferred":
        reasons.append("analog_macro_gds_missing")
    if lvs_status == "blackbox_deferred" and "analog_macro_gds_missing" not in reasons:
        reasons.append("analog_macro_gds_missing")
    return reasons


def _xor_difference_count(log: str) -> int | None:
    counts = [int(match.group(1)) for match in re.finditer(r"Total XOR differences:\s*(\d+)", log or "", flags=re.IGNORECASE)]
    if counts:
        return counts[-1]
    match = re.search(r"(\d+)\s+XOR differences found", log or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _tapeout_failure_reasons(
    *,
    rc: int,
    log: str,
    drc_status: str | None,
    lvs_status: str | None,
    klayout_gds: str | None,
    magic_gds: str | None,
    blocking_xor_count: int | None = None,
) -> list[str]:
    reasons: list[str] = []
    if rc != 0:
        reasons.append("streamout_command_failed")
    if not _stage_is_clean("drc", drc_status):
        reasons.append("drc_not_clean")
    if not _stage_is_clean("lvs", lvs_status):
        reasons.append("lvs_not_clean")
    reasons.extend(reason for reason in _signoff_blocking_reasons(drc_status, lvs_status) if reason not in reasons)
    xor_count = blocking_xor_count if blocking_xor_count is not None else _xor_difference_count(log)
    if _xor_signoff_status() == "enabled" and xor_count is not None and xor_count > 0:
        reasons.append("xor_differences_found")
    if not (klayout_gds or magic_gds):
        reasons.append("gds_not_produced")
    return reasons


def _infer_top_from_netlist(netlist_path: str) -> str | None:
    try:
        txt = open(netlist_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(', txt, flags=re.MULTILINE)
    return m.group(1) if m else None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:


    # Prefer latest explicit digital-level propagated SDC


    digital = state.get("digital") or {}
    lvs_state = digital.get("lvs") or {}

    cand = lvs_state.get("constraints_sdc")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected SDC from state.digital.lvs -> {cand}")
        return cand

    cand = digital.get("constraints_sdc")

    # Prefer later physical stages first
    for stage in [
        "lvs",
        "drc",
        "fill",
        "route",
        "sta_postroute",
        "sta_postcts",
        "cts",
        "place",
        "floorplan",
        "impl_setup",
        "synth",
    ]:
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
    lvs_state = digital.get("lvs") or {}

    # Tapeout must stream the latest physical result.  Prefer physical-stage
    # configs before signoff/global configs so closure ECOs and macro placement
    # survive into streamout/XOR.
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
        os.path.join(workflow_dir, "digital", "lvs", "config.json"),
        os.path.join(workflow_dir, "digital", "drc", "config.json"),
        os.path.join(workflow_dir, "digital", "impl_setup", "openlane", "config.json"),
        os.path.join(workflow_dir, "digital", "synth", "config.json"),
        os.path.join(workflow_dir, "digital", "foundry", "openlane", "config.json"),
    ]:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected config fallback -> {cand}")
            return cand

    cand = lvs_state.get("openlane_config")
    if cand and os.path.exists(cand):
        logger.info(f"{AGENT_NAME}: selected config from state.digital.lvs -> {cand}")
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

    out, seen = [], set()
    for p in sorted(hits):
        base = os.path.basename(p).lower()
        if base.endswith("_llm_lef_raw.lef") or base.endswith("_raw.lef") or "debug" in base:
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
    try:
        with open(src, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except Exception:
        text = ""
    if not text:
        if dst and os.path.abspath(dst) != os.path.abspath(src):
            _ensure_dir(os.path.dirname(dst))
            shutil.copy2(src, dst)
            return dst, 0
        return src, 0

    repairs = 0
    macro_ports = _spice_subckt_ports(macro_spice_models or [])
    top_ports = _verilog_declared_ports(text)

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

    pattern = re.compile(
        r"(?P<model>sky130_(?:fd|ef)_sc_hd__\S+)\s+(?P<inst>\S+)\s*\((?P<body>.*?)\);",
        flags=re.DOTALL,
    )
    repaired = pattern.sub(repl, text)

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
        additions: list[tuple[str, str]] = []
        for port in ports:
            if port in connected or not _is_supply_port(port):
                continue
            net = _supply_net_for_port(port, top_ports)
            if net:
                additions.append((port, net))
        if not additions:
            return match.group(0)
        new_body = body.rstrip()
        for port, net in additions:
            if new_body and not new_body.rstrip().endswith(","):
                new_body += ","
            repairs += 1
            new_body += f"\n    .{port}({net})"
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


def _write_physical_stdcell_blackbox_stubs(netlists: list[str], inputs_netlist_dir: str) -> list[str]:
    cells: dict[str, set[str]] = {}
    for path in netlists:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            text = ""
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
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except Exception:
        text = ""
    return set(re.findall(r"^\s*\.subckt\s+(\S+)", text, flags=re.IGNORECASE | re.MULTILINE))


def _spice_subckt_ports(paths: list[str]) -> dict[str, list[str]]:
    ports: dict[str, list[str]] = {}
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            text = ""
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


def _stage_macro_inputs(state: dict, workflow_dir: str, work_stage_dir: str):
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

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")
    logger.info(f"🏁 Running {AGENT_NAME}")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    workflow_dir = os.path.abspath(workflow_dir)

    stage_dir = os.path.join(workflow_dir, "digital", "tapeout")
    logs_dir = os.path.join(stage_dir, "logs")
    constraints_dir = os.path.join(stage_dir, "constraints")
    netlist_dir = os.path.join(stage_dir, "netlist")
    gds_dir = os.path.join(stage_dir, "gds")

    _ensure_dir(stage_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(constraints_dir)
    _ensure_dir(netlist_dir)
    _ensure_dir(gds_dir)

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

    upstream_netlists = _select_single_top_netlist(sorted(glob.glob(os.path.join(inputs_netlist_dir, "*.v"))))
    if not upstream_netlists:
        raise RuntimeError("Tapeout: missing run_work/inputs/netlist/*.v (synth/floorplan should populate it).")

    _clear_stage_netlists(netlist_dir)
    sanitized_stage_netlists: list[str] = []
    tapeout_netlist_repairs = 0
    for nl in upstream_netlists:
        base, ext = os.path.splitext(os.path.basename(nl))
        sanitized_base = f"{base}_lvs{ext}" if not base.endswith("_lvs") else f"{base}{ext}"
        inputs_sanitized = os.path.join(inputs_netlist_dir, sanitized_base)
        _sanitized, repairs = _sanitize_lvs_netlist_unconnected_stdcell_outputs(nl, inputs_sanitized, macro_spice_models)
        tapeout_netlist_repairs += repairs
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

    work_stage_dir = os.path.join(run_work_dir, "tapeout")
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

    closure_overrides = _closure_overrides(state, workflow_dir, "tapeout")
    cfg.update(closure_overrides)

    logger.info(f"{AGENT_NAME}: staged macro LEFs -> {staged_lefs}")
    logger.info(f"{AGENT_NAME}: staged macro LIBs -> {staged_libs}")
    logger.info(f"{AGENT_NAME}: staged macro GDS -> {staged_gds}")
    logger.info(f"{AGENT_NAME}: staged stdcell SPICE -> {staged_cell_spice}")
    logger.info(f"{AGENT_NAME}: staged extra SPICE -> {staged_extra_spice}")

    config_path = os.path.join(stage_dir, "config.json")
    _write_text(config_path, json.dumps(cfg, indent=2))
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
        f"tapeout_netlist_repairs={tapeout_netlist_repairs}",
        f"physical_stdcell_stub_count={len(physical_stdcell_stubs)}",
        f"macro_placement_cfg={cfg.get('MACRO_PLACEMENT_CFG')}",
        f"macro_placement_cfg_path={macro_placement_cfg}",
        f"cell_spice_count={len(staged_cell_spice)}",
        f"extra_spice_count={len(staged_extra_spice)}",
        f"closure_overrides={json.dumps(closure_overrides, sort_keys=True)}",
    ]) + "\n"
    input_log_path = os.path.join(logs_dir, "tapeout_input_resolution.log")
    _write_text(input_log_path, input_log)

    drc_status = _stage_status(state, "drc")
    lvs_status = _stage_status(state, "lvs")
    signoff_blockers = _signoff_blocking_reasons(drc_status, lvs_status)
    if signoff_blockers:
        out = "Tapeout blocked before streamout because required physical signoff is deferred: " + ", ".join(signoff_blockers) + "\n"
        log_path = os.path.join(logs_dir, "openlane_tapeout.log")
        _write_text(log_path, out)
        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "blocked",
            "return_code": 0,
            "failure_reasons": signoff_blockers + ["drc_not_clean", "lvs_not_clean"],
            "signoff_inputs": {
                "drc_status": drc_status,
                "lvs_status": lvs_status,
                "xor_status": _xor_signoff_status(),
                "xor_differences": None,
                "xor_differences_observed": None,
                "xor_differences_total": None,
                "xor_layer_counts": {},
                "xor_nonblocking_layers": sorted(DEFAULT_NONBLOCKING_XOR_LAYERS),
                "gds_produced": False,
            },
            "outputs": {
                "sdc": f"digital/tapeout/constraints/{sdc_basename}",
                "metrics_json": None,
                "xor_report_xml": None,
                "klayout_gds": None,
                "magic_gds": None,
                "log": "digital/tapeout/logs/openlane_tapeout.log",
                "input_resolution_log": "digital/tapeout/logs/tapeout_input_resolution.log",
                "openlane_run_dir": None,
            },
        }
        _write_text(os.path.join(stage_dir, "tapeout_summary.json"), json.dumps(summary, indent=2))
        _write_text(os.path.join(stage_dir, "tapeout_summary.md"), "# Tapeout\n\n- status: `blocked`\n- reason: `analog_macro_gds_missing`\n")
        try:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout/config.json", json.dumps(cfg, indent=2))
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"tapeout/constraints/{sdc_basename}", sdc_text)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout/logs/openlane_tapeout.log", out)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout/logs/tapeout_input_resolution.log", input_log)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout/tapeout_summary.json", json.dumps(summary, indent=2))
        except Exception as e:
            print(f"âš ï¸ {AGENT_NAME} upload failed: {e}")
        state.setdefault("digital", {})["tapeout"] = {
            "status": summary["status"],
            "stage_dir": stage_dir,
            "metrics_json": None,
            "constraints_sdc": stage_sdc,
            "openlane_config": config_path,
            "input_resolution_log": input_log_path,
            "gds_klayout": None,
            "gds_magic": None,
            "openlane_run_dir": None,
            "failure_reasons": summary["failure_reasons"],
        }
        return state

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
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to KLayout.StreamOut tapeout/config.json'

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {openlane_image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to Magic.StreamOut tapeout/config.json || true'

docker run --rm \\
  -v "{pdk_root_host}":/pdk \\
  -v "{run_work_dir}":/work \\
  -e PDK={pdk_variant} \\
  -e PDK_ROOT=/pdk \\
  {openlane_image} \\
  bash -lc 'set -e; cd /work && openlane --flow Classic --run-tag {run_tag} --override-config RUN_LINTER=False --to KLayout.XOR tapeout/config.json || true'
"""
    run_sh_path = os.path.join(stage_dir, "run.sh")
    _write_text(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    rc, out = _run(["bash", "-lc", "./run.sh"], cwd=stage_dir, state=state)

    log_path = os.path.join(logs_dir, "openlane_tapeout.log")
    _write_text(log_path, out)

    latest = _latest_run_dir(work_stage_dir)
    metrics_path = _copy_metrics(latest, stage_dir)
    xor_report_path = _copy_xor_report(latest, stage_dir)
    xor_layer_counts = _xor_layer_counts(xor_report_path)
    xor_total = _xor_difference_count(out)
    raw_blocking_xor_count = _blocking_xor_difference_count(xor_total, xor_layer_counts)
    xor_status = _xor_signoff_status()
    blocking_xor_count = raw_blocking_xor_count if xor_status == "enabled" else None

    kl_src, mg_src = _pick_gds(latest)
    kl_dst = os.path.join(gds_dir, "klayout.gds") if kl_src else None
    mg_dst = os.path.join(gds_dir, "magic.gds") if mg_src else None

    if kl_src and kl_dst:
        shutil.copy2(kl_src, kl_dst)
    if mg_src and mg_dst:
        shutil.copy2(mg_src, mg_dst)

    failure_reasons = _tapeout_failure_reasons(
        rc=rc,
        log=out,
        drc_status=drc_status,
        lvs_status=lvs_status,
        klayout_gds=kl_dst,
        magic_gds=mg_dst,
        blocking_xor_count=blocking_xor_count,
    )

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if not failure_reasons else "failed",
        "return_code": rc,
        "failure_reasons": failure_reasons,
        "signoff_inputs": {
            "drc_status": drc_status,
            "lvs_status": lvs_status,
            "xor_status": xor_status,
            "xor_differences": blocking_xor_count,
            "xor_differences_observed": raw_blocking_xor_count,
            "xor_differences_total": xor_total,
            "xor_layer_counts": xor_layer_counts,
            "xor_nonblocking_layers": sorted(DEFAULT_NONBLOCKING_XOR_LAYERS),
            "gds_produced": bool(kl_dst or mg_dst),
        },
        "outputs": {
            "sdc": f"digital/tapeout/constraints/{sdc_basename}",
            "metrics_json": "digital/tapeout/metrics.json" if metrics_path else None,
            "xor_report_xml": "digital/tapeout/reports/xor.xml" if xor_report_path else None,
            "klayout_gds": "digital/tapeout/gds/klayout.gds" if kl_dst else None,
            "magic_gds": "digital/tapeout/gds/magic.gds" if mg_dst else None,
            "log": "digital/tapeout/logs/openlane_tapeout.log",
            "input_resolution_log": "digital/tapeout/logs/tapeout_input_resolution.log",
            "openlane_run_dir": latest,
        },
    }

    _write_text(os.path.join(stage_dir, "tapeout_summary.json"), json.dumps(summary, indent=2))
    _write_text(
        os.path.join(stage_dir, "tapeout_summary.md"),
        f"# Tapeout\n\n- status: `{summary['status']}` (rc={rc})\n"
    )

    try:
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/config.json", json.dumps(cfg, indent=2)
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", f"tapeout/constraints/{sdc_basename}", sdc_text
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/run.sh", run_sh
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/logs/openlane_tapeout.log", out
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/logs/tapeout_input_resolution.log", input_log
        )
        save_text_artifact_and_record(
            workflow_id, AGENT_NAME, "digital", "tapeout/tapeout_summary.json", json.dumps(summary, indent=2)
        )

        if metrics_path and os.path.exists(metrics_path):
            with open(metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id, AGENT_NAME, "digital", "tapeout/metrics.json", f.read()
                )
        if xor_report_path and os.path.exists(xor_report_path):
            with open(xor_report_path, "r", encoding="utf-8", errors="ignore") as f:
                save_text_artifact_and_record(
                    workflow_id, AGENT_NAME, "digital", "tapeout/reports/xor.xml", f.read()
                )
        # GDS is binary; keep local and record path in summary/state
    except Exception as e:
        print(f"⚠️ {AGENT_NAME} upload failed: {e}")

    state.setdefault("digital", {})["tapeout"] = {
        "status": summary["status"],
        "stage_dir": stage_dir,
        "metrics_json": metrics_path,
        "constraints_sdc": stage_sdc,
        "openlane_config": config_path,
        "input_resolution_log": input_log_path,
        "gds_klayout": kl_dst,
        "gds_magic": mg_dst,
        "openlane_run_dir": latest,
    }

    return state
