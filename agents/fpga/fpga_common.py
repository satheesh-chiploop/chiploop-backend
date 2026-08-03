import glob
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


FPGA_DIR = "fpga"
ARTIFACT_BUCKET = "artifacts"
RTL_EXTENSIONS = (".sv", ".v", ".svh", ".vh")


BOARD_REGISTRY: Dict[str, Dict[str, Any]] = {
    "icebreaker": {
        "label": "Lattice iCEBreaker",
        "vendor": "lattice",
        "family": "ice40",
        "device": "up5k",
        "package": "sg48",
        "nextpnr_device_flag": "--up5k",
        "nextpnr_package": "sg48",
        "constraint_format": "pcf",
        "programmer_board": "icebreaker",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 5280},
    },
    "upduino_v3": {
        "label": "Lattice UPduino v3",
        "vendor": "lattice",
        "family": "ice40",
        "device": "up5k",
        "package": "sg48",
        "nextpnr_device_flag": "--up5k",
        "nextpnr_package": "sg48",
        "constraint_format": "pcf",
        "programmer_board": "upduino3",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 5280},
    },
    "icestick": {
        "label": "Lattice iCEstick",
        "vendor": "lattice",
        "family": "ice40",
        "device": "hx1k",
        "package": "tq144",
        "nextpnr_device_flag": "--hx1k",
        "nextpnr_package": "tq144",
        "constraint_format": "pcf",
        "programmer_board": "icestick",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 1280},
    },
    "custom_ice40": {
        "label": "Custom iCE40",
        "vendor": "lattice",
        "family": "ice40",
        "device": "hx8k",
        "package": "ct256",
        "nextpnr_device_flag": "--hx8k",
        "nextpnr_package": "ct256",
        "constraint_format": "pcf",
        "programmer_board": None,
        "programming_note": "Use the external JTAG/SPI programmer supported by the selected HX8K breakout board.",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 7680},
    },
    "ice40_hx8k_breakout": {
        "label": "Lattice iCE40 HX8K Breakout",
        "vendor": "lattice",
        "family": "ice40",
        "device": "hx8k",
        "package": "ct256",
        "nextpnr_tool": "nextpnr-ice40",
        "nextpnr_device_flag": "--hx8k",
        "nextpnr_package": "ct256",
        "constraint_format": "pcf",
        "programmer_board": None,
        "programming_note": "Use the external JTAG/SPI programmer supported by the selected HX8K breakout board.",
        "default_frequency_mhz": 12.0,
        "resources": {"logic_cells": 7680},
    },
    "ulx3s_ecp5_45f": {
        "label": "Lattice ULX3S ECP5-45F",
        "vendor": "lattice",
        "family": "ecp5",
        "device": "45k",
        "package": "CABGA381",
        "nextpnr_tool": "nextpnr-ecp5",
        "nextpnr_device_flag": "--45k",
        "nextpnr_package": "CABGA381",
        "constraint_format": "lpf",
        "programmer_board": "ulx3s",
        "default_frequency_mhz": 25.0,
        "resources": {"logic_cells": 44000},
    },
    "orangecrab_ecp5_85f": {
        "label": "Lattice OrangeCrab ECP5-85F",
        "vendor": "lattice",
        "family": "ecp5",
        "device": "85k",
        "package": "CSFBGA285",
        "nextpnr_tool": "nextpnr-ecp5",
        "nextpnr_device_flag": "--85k",
        "nextpnr_package": "CSFBGA285",
        "constraint_format": "lpf",
        "programmer_board": "orangeCrab",
        "default_frequency_mhz": 48.0,
        "resources": {"logic_cells": 84000},
    },
    "colorlight_5a_75b": {
        "label": "Lattice Colorlight 5A-75B ECP5-25F",
        "vendor": "lattice",
        "family": "ecp5",
        "device": "25k",
        "package": "CABGA256",
        "nextpnr_tool": "nextpnr-ecp5",
        "nextpnr_device_flag": "--25k",
        "nextpnr_package": "CABGA256",
        "constraint_format": "lpf",
        "programmer_board": None,
        "programming_note": "Colorlight 5A-75B usually requires an external JTAG adapter; use openFPGALoader or OpenOCD with the adapter-specific cable configuration.",
        "default_frequency_mhz": 25.0,
        "resources": {"logic_cells": 24000},
    },
}

BOARD_REGISTRY.update({
    "certus_nx_versa_40": {
        "label": "Lattice Certus-NX Versa (LFD2NX-40)", "vendor": "lattice", "family": "nexus", "product_family": "Certus-NX",
        "device": "LFD2NX-40-8BG256C", "package": "BG256", "nextpnr_tool": "nextpnr-nexus",
        "nextpnr_device_args": ["--device", "LFD2NX-40-8BG256C"], "constraint_format": "pdc", "yosys_family": "lfd2nx", "bitstream_tool": "prjoxide",
        "bitstream_ext": ".bit", "pnr_output_ext": ".fasm", "support_tier": "experimental",
        "segments": ["industrial", "general-purpose embedded", "connectivity"], "programmer_board": "certusnx_versa_evn", "default_frequency_mhz": 12.0, "resources": {"logic_cells": 39000},
    },
    "crosslink_nx_eval_40": {
        "label": "Lattice CrossLink-NX Evaluation Board (LIFCL-40)", "vendor": "lattice", "family": "nexus", "product_family": "CrossLink-NX",
        "device": "LIFCL-40-9BG400C", "package": "BG400", "nextpnr_tool": "nextpnr-nexus",
        "nextpnr_device_args": ["--device", "LIFCL-40-9BG400C"], "constraint_format": "pdc", "yosys_family": "lifcl", "bitstream_tool": "prjoxide",
        "bitstream_ext": ".bit", "pnr_output_ext": ".fasm", "support_tier": "experimental",
        "segments": ["machine vision", "camera/display bridging", "sensor aggregation"], "programmer_board": "crosslinknx_evn", "default_frequency_mhz": 12.0, "resources": {"logic_cells": 39000},
    },
    "certuspro_nx_versa_100": {
        "label": "Lattice CertusPro-NX Versa (LFCPNX-100)", "vendor": "lattice", "family": "nexus", "product_family": "CertusPro-NX",
        "device": "LFCPNX-100-9LFG672C", "package": "LFG672", "nextpnr_tool": "nextpnr-nexus",
        "nextpnr_device_args": ["--device", "LFCPNX-100-9LFG672C"], "constraint_format": "lpf", "bitstream_tool": "prjoxide",
        "bitstream_ext": ".bit", "pnr_output_ext": ".fasm", "support_tier": "unavailable",
        "unsupported_reason": "Yosys synth_nexus and Project Oxide do not support LFCPNX in the qualified open-source flow.",
        "segments": ["communications", "networking", "compute acceleration", "infrastructure"], "default_frequency_mhz": 25.0, "resources": {"logic_cells": 100000},
    },
    "machxo5_nx_65t": {
        "label": "Lattice MachXO5-NX 65T Development Board", "vendor": "lattice", "family": "nexus", "product_family": "MachXO5-NX",
        "device": "LFMXO5-65", "package": "board-specific", "support_tier": "unavailable",
        "unsupported_reason": "Deferred until Project Oxide supports and ChipLoop qualifies this exact open-source target.",
        "segments": ["secure control", "server management", "industrial platform management"], "default_frequency_mhz": 25.0, "resources": {"logic_cells": 65000},
    },
    "gowin_tang_nano_9k": {
        "label": "Gowin Tang Nano 9K (LittleBee GW1NR-9)", "vendor": "gowin", "family": "gowin", "product_family": "LittleBee",
        "device": "GW1NR-LV9QN88PC6/I5", "apicula_family": "GW1N-9C", "package": "QN88", "nextpnr_tool": "nextpnr-himbaechel",
        "nextpnr_device_args": ["--device", "GW1NR-LV9QN88PC6/I5", "--vopt", "family=GW1N-9C"], "constraint_format": "cst",
        "bitstream_tool": "gowin_pack", "bitstream_ext": ".fs", "pnr_output_ext": ".json", "support_tier": "beta",
        "segments": ["education", "makers", "IoT", "low-cost embedded", "small industrial control"], "programmer_board": "tangnano9k",
        "default_frequency_mhz": 27.0, "resources": {"logic_cells": 8640},
    },
    "gowin_tang_nano_20k": {
        "label": "Gowin Tang Nano 20K (Arora II GW2AR-18C)", "vendor": "gowin", "family": "gowin", "product_family": "Arora II",
        "device": "GW2AR-LV18QN88C8/I7", "apicula_family": "GW2A-18C", "yosys_family": "gw2a", "package": "QN88", "nextpnr_tool": "nextpnr-himbaechel",
        "nextpnr_device_args": ["--device", "GW2AR-LV18QN88C8/I7", "--vopt", "family=GW2A-18C"], "constraint_format": "cst",
        "bitstream_tool": "gowin_pack", "bitstream_ext": ".fs", "pnr_output_ext": ".json", "support_tier": "beta",
        "segments": ["video", "soft CPUs", "DSP", "robotics", "industrial control"], "programmer_board": "tangnano20k",
        "default_frequency_mhz": 27.0, "resources": {"logic_cells": 20736},
    },
    "gowin_tang_primer_20k": {
        "label": "Gowin Tang Primer 20K (Arora II GW2A-18)", "vendor": "gowin", "family": "gowin", "product_family": "Arora II",
        "device": "GW2A-LV18PG256C8/I7", "apicula_family": "GW2A-18", "yosys_family": "gw2a", "package": "PG256", "nextpnr_tool": "nextpnr-himbaechel",
        "nextpnr_device_args": ["--device", "GW2A-LV18PG256C8/I7", "--vopt", "family=GW2A-18"], "constraint_format": "cst",
        "bitstream_tool": "gowin_pack", "bitstream_ext": ".fs", "pnr_output_ext": ".json", "support_tier": "beta",
        "segments": ["modular prototyping", "embedded compute", "motor control", "communications"], "programmer_board": "tangprimer20k",
        "default_frequency_mhz": 27.0, "resources": {"logic_cells": 20736},
    },
    "gowin_gw5a_25_starter": {
        "label": "Gowin Arora V GW5A-25 Starter Board", "vendor": "gowin", "family": "gowin", "product_family": "Arora V",
        "device": "GW5A-LV25LQ144C1/I0", "apicula_family": "GW5A-25", "package": "LQ144", "nextpnr_tool": "nextpnr-himbaechel",
        "nextpnr_device_args": ["--device", "GW5A-LV25LQ144C1/I0", "--vopt", "family=GW5A-25"], "constraint_format": "cst",
        "bitstream_tool": "gowin_pack", "bitstream_ext": ".fs", "pnr_output_ext": ".json", "support_tier": "unavailable",
        "unsupported_reason": "The exact GW5A-25 device and board pin map are not qualified in upstream nextpnr/Apicula.",
        "segments": ["machine vision", "displays", "high-performance DSP", "edge processing"], "default_frequency_mhz": 50.0, "resources": {"logic_cells": 23040},
    },
    "gowin_gw5at_60_pcie": {
        "label": "Gowin Arora V GW5AT-60 PCIe Board", "vendor": "gowin", "family": "gowin", "product_family": "Arora V",
        "device": "GW5AT-LV60UG324", "package": "UG324", "support_tier": "unavailable",
        "unsupported_reason": "PCIe/SerDes and the exact board bitstream are not qualified in the open-source flow.",
        "segments": ["PCIe", "SerDes", "networking", "high-speed video"], "default_frequency_mhz": 50.0, "resources": {"logic_cells": 59904},
    },
    "gowin_gw5ast_138": {
        "label": "Gowin Arora V GW5AST-138 RISC-V Board", "vendor": "gowin", "family": "gowin", "product_family": "Arora V",
        "device": "GW5AST-LV138FPG676", "package": "FPG676", "support_tier": "unavailable",
        "unsupported_reason": "The hardened RISC-V SoC and exact device are not qualified with Project Apicula.",
        "segments": ["embedded RISC-V", "edge AI", "industrial compute"], "default_frequency_mhz": 50.0, "resources": {"logic_cells": 138240},
    },
    "gowin_gw3a_20k": {
        "label": "Gowin Arora III GW3A-20K Starter Board", "vendor": "gowin", "family": "gowin", "product_family": "Arora III",
        "device": "GW3A-LV20LQ144", "package": "LQ144", "support_tier": "unavailable",
        "unsupported_reason": "Deferred until Yosys, nextpnr-himbaechel and Project Apicula provide an upstream implementation database.",
        "segments": ["industrial control", "machine vision", "displays", "mid-range DSP"], "default_frequency_mhz": 50.0, "resources": {"logic_cells": 23040},
    },
})


def workflow_dir(state: Dict[str, Any]) -> str:
    wid = str(state.get("workflow_id") or "default")
    root = str(state.get("workflow_dir") or f"backend/workflows/{wid}")
    os.makedirs(root, exist_ok=True)
    return root


def fpga_dir(state: Dict[str, Any], *parts: str) -> str:
    path = os.path.join(workflow_dir(state), FPGA_DIR, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def write_text(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    return path


def write_json(path: str, data: Dict[str, Any]) -> str:
    return write_text(path, json.dumps(data, indent=2, sort_keys=True))


def publish_json(state: Dict[str, Any], agent: str, subdir: str, filename: str, data: Dict[str, Any]) -> str:
    path = write_json(f"{fpga_dir(state, subdir)}/{filename}", data)
    workflow_id = str(state.get("workflow_id") or "")
    if workflow_id:
        try:
            from utils.artifact_utils import save_text_artifact_and_record

            save_text_artifact_and_record(
                workflow_id,
                agent,
                f"fpga/{subdir}".rstrip("/"),
                filename,
                json.dumps(data, indent=2, sort_keys=True),
            )
        except Exception:
            pass
    return path


def read_text(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def detect_top_module(paths: List[str]) -> Optional[str]:
    for path in paths:
        text = read_text(path)
        match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)
        if match:
            return match.group(1)
    return None


def _module_names(path: str) -> List[str]:
    text = read_text(path)
    return re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)


def _safe_rel(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return "rtl/source.sv"
    parts = Path(normalized).parts
    if any(part in {"..", ""} for part in parts) or Path(normalized).is_absolute():
        return os.path.basename(normalized) or "source.sv"
    return normalized


def _copy_tree_rtl(source_dir: str, dest_dir: str) -> List[str]:
    copied: List[str] = []
    for pattern in ("**/*.v", "**/*.sv"):
        for src in glob.glob(os.path.join(source_dir, pattern), recursive=True):
            if any(skip in src.replace("\\", "/").lower() for skip in ("/sim_build/", "/node_modules/", "/.git/", "/fpga/src/")):
                continue
            rel = os.path.relpath(src, source_dir)
            dst = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(dst)
    return copied


def _storage_paths(value: Any) -> List[str]:
    paths: List[str] = []
    if isinstance(value, dict):
        for child in value.values():
            paths.extend(_storage_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_storage_paths(child))
    elif isinstance(value, str):
        paths.append(value.replace("\\", "/"))
    return paths


def _list_storage_tree(client: Any, folder: str, depth: int = 0, max_depth: int = 6) -> List[str]:
    if depth > max_depth:
        return []
    try:
        entries = client.storage.from_(ARTIFACT_BUCKET).list(folder) or []
    except Exception:
        return []
    paths: List[str] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            continue
        path = f"{folder.rstrip('/')}/{name}"
        paths.append(path)
        paths.extend(_list_storage_tree(client, path, depth + 1, max_depth))
    return paths


def _copy_storage_rtl(state: Dict[str, Any], source_workflow_id: str, dest_dir: str) -> List[str]:
    client = state.get("supabase_client")
    if not client:
        return []
    paths: List[str] = []
    try:
        row = (
            client.table("workflows")
            .select("artifacts")
            .eq("id", source_workflow_id)
            .single()
            .execute()
        ).data or {}
        paths.extend(_storage_paths(row.get("artifacts") or {}))
    except Exception:
        pass
    paths.extend(_list_storage_tree(client, f"backend/workflows/{source_workflow_id}"))
    rtl_paths = [
        path for path in list(dict.fromkeys(paths))
        if path.lower().endswith(RTL_EXTENSIONS)
    ][:512]
    copied: List[str] = []
    for index, path in enumerate(rtl_paths):
        try:
            raw = client.storage.from_(ARTIFACT_BUCKET).download(path)
        except Exception:
            raw = None
        if not raw:
            continue
        rel = _safe_rel(f"upstream/{os.path.basename(path) or f'source_{index}.sv'}")
        target = os.path.join(dest_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_bytes(raw)
        copied.append(target)
    try:
        run_rows = (
            client.table("runs")
            .select("artifacts_path")
            .eq("workflow_id", source_workflow_id)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        run_rows = []
    for row in run_rows:
        root = Path(str((row or {}).get("artifacts_path") or ""))
        if not root.exists():
            continue
        for source in sorted(root.rglob("*")):
            lower_name = source.name.lower()
            if not source.is_file() or not lower_name.endswith(RTL_EXTENSIONS):
                continue
            if lower_name.startswith("tb_") or "testbench" in lower_name or "_tb." in lower_name:
                continue
            target = os.path.join(dest_dir, _safe_rel(f"upstream/{source.name}"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            Path(target).write_bytes(source.read_bytes())
            copied.append(target)
    return copied


def resolve_rtl_sources(state: Dict[str, Any]) -> List[str]:
    out_dir = fpga_dir(state, "src")
    sources: List[str] = []
    mode = str(state.get("rtl_source_mode") or state.get("source") or "").strip().lower()
    pasted = state.get("pasted_rtl_files")
    if isinstance(pasted, list):
        for index, item in enumerate(pasted):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "")
            if not content.strip():
                continue
            rel = str(item.get("path") or f"rtl/source_{index}.sv").replace("\\", "/").lstrip("/")
            if not rel.endswith((".v", ".sv")):
                rel += ".sv"
            sources.append(write_text(os.path.join(out_dir, rel), content))
    rtl_text = str(state.get("rtl_text") or "")
    if rtl_text.strip():
        sources.append(write_text(os.path.join(out_dir, "rtl", "top.sv"), rtl_text))
    repo_path = state.get("repo_path")
    if mode == "repo_path" and isinstance(repo_path, str) and repo_path.strip() and os.path.exists(repo_path):
        base = repo_path
        subdir = state.get("repo_subdir")
        if isinstance(subdir, str) and subdir.strip():
            base = os.path.join(base, subdir.strip())
        sources.extend(_copy_tree_rtl(base, out_dir))
    if mode in {"generate_arch2rtl", "spec", "arch2rtl_from_spec"}:
        for base in (state.get("artifact_dir"), workflow_dir(state)):
            if isinstance(base, str) and base and os.path.exists(base):
                sources.extend(_copy_tree_rtl(base, out_dir))
    source_wf = state.get("from_workflow_id") or state.get("source_arch2rtl_workflow_id") or state.get("source_workflow_id")
    if source_wf:
        sources.extend(_copy_storage_rtl(state, str(source_wf), out_dir))
        source_root = os.path.join("backend", "workflows", str(source_wf))
        if os.path.exists(source_root):
            sources.extend(_copy_tree_rtl(source_root, out_dir))
    unique_paths = list(dict.fromkeys(os.path.abspath(path) for path in sources if os.path.exists(path)))
    unique_paths = [
        path for path in unique_paths
        if not os.path.basename(path).lower().startswith("tb_")
        and "testbench" not in os.path.basename(path).lower()
        and "_tb." not in os.path.basename(path).lower()
    ]
    deduped: List[str] = []
    ignored: List[Dict[str, Any]] = []
    seen_hashes: Dict[str, str] = {}
    seen_modules: Dict[str, str] = {}
    for path in unique_paths:
        text = read_text(path)
        digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        if digest in seen_hashes:
            ignored.append({"path": path, "reason": "duplicate_content", "matches": seen_hashes[digest]})
            continue
        modules = _module_names(path)
        duplicate_modules = [name for name in modules if name in seen_modules]
        if duplicate_modules:
            ignored.append({
                "path": path,
                "reason": "duplicate_module_definition",
                "modules": duplicate_modules,
                "matches": {name: seen_modules[name] for name in duplicate_modules},
            })
            continue
        deduped.append(path)
        seen_hashes[digest] = path
        for name in modules:
            seen_modules[name] = path
    state["fpga_rtl_ignored_sources"] = ignored
    return sorted(deduped)


def board_config(state: Dict[str, Any]) -> Dict[str, Any]:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board_key = str(state.get("board") or fpga.get("board") or "icebreaker").strip().lower()
    base = dict(BOARD_REGISTRY.get(board_key) or BOARD_REGISTRY["custom_ice40"])
    base["board"] = board_key
    for key in ("family", "device", "package", "programmer_board"):
        if state.get(key):
            base[key] = state.get(key)
        if fpga.get(key):
            base[key] = fpga.get(key)
    family = str(base.get("family") or "").lower()
    configured_tier = str(base.get("support_tier") or "production").lower()
    if configured_tier == "unavailable":
        base["supported"] = False
        base.setdefault("unsupported_reason", "This target is unavailable in ChipLoop's open-source implementation flow.")
    elif family not in {"ice40", "ecp5", "nexus", "gowin"}:
        base["supported"] = False
        base["unsupported_reason"] = "This architecture is not supported by ChipLoop's open-source implementation flow."
    else:
        base["supported"] = True
    base["support_tier"] = configured_tier
    device = str(base.get("device", "")).lower()
    if family == "ice40":
        base.setdefault("nextpnr_tool", "nextpnr-ice40")
        base.setdefault("bitstream_tool", "icepack")
        base.setdefault("bitstream_ext", ".bin")
        base.setdefault("pnr_output_ext", ".asc")
        base.setdefault("constraint_format", "pcf")
        if device in {"up5k", "u4k"}:
            base["nextpnr_device_flag"] = "--up5k"
        elif device in {"hx1k", "lp1k"}:
            base["nextpnr_device_flag"] = "--hx1k"
        elif device in {"hx8k", "lp8k"}:
            base["nextpnr_device_flag"] = "--hx8k"
    elif family == "ecp5":
        base.setdefault("nextpnr_tool", "nextpnr-ecp5")
        base.setdefault("bitstream_tool", "ecppack")
        base.setdefault("bitstream_ext", ".bit")
        base.setdefault("pnr_output_ext", ".config")
        base.setdefault("constraint_format", "lpf")
        if device in {"25k", "lfe5u-25f", "lfe5um-25f"}:
            base["nextpnr_device_flag"] = "--25k"
        elif device in {"45k", "lfe5u-45f", "lfe5um-45f"}:
            base["nextpnr_device_flag"] = "--45k"
        elif device in {"85k", "lfe5u-85f", "lfe5um-85f"}:
            base["nextpnr_device_flag"] = "--85k"
    elif family == "nexus":
        base.setdefault("nextpnr_tool", "nextpnr-nexus")
        base.setdefault("bitstream_tool", "prjoxide")
        base.setdefault("bitstream_ext", ".bit")
        base.setdefault("pnr_output_ext", ".fasm")
        base.setdefault("constraint_format", "pdc")
        base.setdefault("nextpnr_device_args", ["--device", base.get("device")])
    elif family == "gowin":
        generic_himbaechel = shutil.which("nextpnr-himbaechel")
        split_himbaechel = shutil.which("nextpnr-himbaechel-gowin")
        if base.get("nextpnr_tool") == "nextpnr-himbaechel" and not generic_himbaechel and split_himbaechel:
            base["nextpnr_tool"] = split_himbaechel
        base.setdefault("bitstream_tool", "gowin_pack")
        base.setdefault("bitstream_ext", ".fs")
        base.setdefault("pnr_output_ext", ".json")
        base.setdefault("constraint_format", "cst")
        base.setdefault("nextpnr_device_args", ["--device", base.get("device"), "--vopt", f"family={base.get('apicula_family')}"])


    return base
def tool_status(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tooling.profiles import resolve_tool

    aliases = {
        "nextpnr-himbaechel": "nextpnr_himbaechel_gowin",
        "nextpnr-nexus": "nextpnr_nexus",
        "openFPGALoader": "openfpgaloader",
    }
    names = [
        "yosys", "nextpnr-ice40", "nextpnr-ecp5", "nextpnr-nexus",
        "nextpnr-himbaechel", "icepack", "icetime", "ecppack",
        "prjoxide", "gowin_pack", "openFPGALoader",
    ]
    tools = {
        name: resolve_tool(aliases.get(name, name), state or {})
        for name in names
    }
    return {name: {"available": bool(path), "path": path} for name, path in tools.items()}

def run_cmd(cmd: List[str], cwd: str, log_path: str, timeout: int = 600, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tooling.runner import run_command

    result = run_command(
        state or {},
        "fpga_implementation",
        cmd,
        cwd=cwd,
        timeout_sec=timeout,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr, result.error) if part).strip()
    write_text(log_path, output)
    return {
        "cmd": result.command or cmd,
        "executable": result.executable,
        "profile_id": result.profile_id,
        "status": result.status,
        "returncode": result.returncode,
        "ok": result.status == "success" and result.returncode == 0,
        "log": log_path,
        "stdout_tail": (result.stdout or "")[-4000:],
        "stderr_tail": (result.stderr or "")[-4000:],
        "error": result.error,
    }

def manifest_update(state: Dict[str, Any], key: str, value: Any) -> None:
    fpga = state.setdefault("fpga", {})
    if isinstance(fpga, dict):
        fpga[key] = value
