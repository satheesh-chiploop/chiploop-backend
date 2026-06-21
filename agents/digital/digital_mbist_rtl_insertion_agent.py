import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.runner import tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital MBIST RTL Insertion Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    Path(path).write_text(content, encoding="utf-8")


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _safe_name(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "memory")).strip("_")
    return text or "memory"


def _openram_cell(memory: dict[str, Any]) -> str:
    return str(memory.get("openram_cell") or memory.get("cell") or "").strip()


def _rtl_instance_cell(memory: dict[str, Any]) -> str:
    return str(memory.get("rtl_cell") or memory.get("cell") or "").strip()


def _run(cmd: list[str], cwd: str, timeout: int = 600, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") + "\nTIMEOUT\n"
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}\n"


def _autombist_hardware_dirs(autombist: str) -> list[str]:
    dirs: list[str] = []
    real = os.path.realpath(autombist) if autombist else ""
    if real:
        prefix = os.path.dirname(os.path.dirname(real))
        dirs.extend(glob.glob(os.path.join(prefix, "lib", "python*", "site-packages", "autombist", "tests", "hardware")))
    dirs.extend(glob.glob(os.path.join(os.sep, "opt", "chiploop-dft", "lib", "python*", "site-packages", "autombist", "tests", "hardware")))
    return sorted(dict.fromkeys(os.path.abspath(path) for path in dirs if os.path.isdir(path)))


def _memory_source_needs_sim_model(memory: dict[str, Any]) -> bool:
    text = _read_text(str(memory.get("openram_behavioral_model") or memory.get("source_file") or ""))
    if not text:
        return True
    clean = _strip_comments(text).lower()
    if re.search(r"\b(reg|logic)\s*(?:\[[^\]]+\]\s*)?\w+\s*\[[^\]]+\]", clean):
        return False
    if re.search(r"\b(always_ff|always)\b", clean) and re.search(r"\b(addr|address)\b", clean):
        return False
    return True


def _behavioral_sram_model_text(memory: dict[str, Any]) -> str:
    ports = memory.get("ports") if isinstance(memory.get("ports"), dict) else {}
    cell = _openram_cell(memory) or "autombist_sram"
    clk = ports.get("clk", "clk")
    csb = ports.get("csb", "csb")
    we = ports.get("we", "web")
    addr = ports.get("addr", "addr")
    din = ports.get("din", "din")
    dout = ports.get("dout", "dout")
    addr_width = max(1, int(memory.get("addr_width") or 8))
    data_width = max(1, int(memory.get("data_width") or 32))
    lines = [
        f"module {cell}(",
        f"    input wire {clk},",
    ]
    if csb:
        lines.append(f"    input wire {csb},")
    lines.extend([
        f"    input wire {we},",
        f"    input wire [{addr_width - 1}:0] {addr},",
        f"    input wire [{data_width - 1}:0] {din},",
        f"    output reg [{data_width - 1}:0] {dout}",
        ");",
        f"reg [{data_width - 1}:0] mem [0:{(1 << addr_width) - 1}];",
        f"always @(posedge {clk}) begin",
    ])
    enable = f"!{csb}" if csb else "1'b1"
    lines.extend([
        f"  if ({enable}) begin",
        f"    if (!{we}) begin",
        f"      mem[{addr}] <= {din};",
        "    end else begin",
        f"      {dout} <= mem[{addr}];",
        "    end",
        "  end",
        "end",
        "endmodule",
        "",
    ])
    return "\n".join(lines)


def _stage_memory_model_for_autombist(
    memory: dict[str, Any],
    autombist: str,
    stage_dir: str,
    allow_generated_sim_model: bool = False,
) -> dict[str, Any]:
    src = str(memory.get("openram_behavioral_model") or memory.get("source_file") or "")
    cell = _openram_cell(memory)
    result: dict[str, Any] = {"status": "not_needed", "source": src, "targets": []}
    if not src or not cell or not os.path.isfile(src):
        result["status"] = "missing_source"
        return result

    use_generated_model = _memory_source_needs_sim_model(memory)
    model_src = src
    if use_generated_model:
        result["status"] = "openram_behavioral_model_missing"
        result["reason"] = "detected_memory_rtl_is_abstract_or_constant_shell"
        result["simulation_model_source"] = "missing_openram_behavioral_model"
        return result
    else:
        result["simulation_model_source"] = "detected_rtl_source"

    staged_src = os.path.join(stage_dir, f"{cell}.v")
    try:
        shutil.copy2(model_src, staged_src)
        result["workflow_copy"] = staged_src
    except Exception as exc:
        result["workflow_copy_error"] = f"{type(exc).__name__}: {exc}"

    copied: list[str] = []
    errors: list[str] = []
    for hardware_dir in _autombist_hardware_dirs(autombist):
        dst = os.path.join(hardware_dir, f"{cell}.v")
        try:
            if os.path.abspath(model_src) != os.path.abspath(dst):
                shutil.copy2(model_src, dst)
            copied.append(dst)
        except Exception as exc:
            errors.append(f"{dst}: {type(exc).__name__}: {exc}")
    result["targets"] = copied
    if copied:
        result["status"] = "staged"
    elif errors:
        result["status"] = "failed"
    else:
        result["status"] = "autombist_hardware_dir_not_found"
    if errors:
        result["errors"] = errors
    return result


def _existing_path(value: Any, workflow_dir: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidates = [raw]
    if not os.path.isabs(raw):
        candidates.insert(0, os.path.join(workflow_dir, raw))
    for cand in candidates:
        try:
            path = os.path.abspath(cand)
            if os.path.isfile(path):
                return path
        except OSError:
            continue
    return None


def _rtl_files(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    handoff = digital.get("handoff") if isinstance(digital.get("handoff"), dict) else {}
    candidates: list[str] = []
    for value in (
        state.get("rtl_files"),
        digital.get("rtl_files"),
        handoff.get("rtl_files"),
        handoff.get("imported_rtl_files"),
    ):
        if isinstance(value, list):
            candidates.extend(str(item) for item in value)
    found = [_existing_path(item, workflow_dir) for item in candidates]
    found = [item for item in found if item]
    if found:
        return sorted(dict.fromkeys(found))

    patterns = [
        os.path.join(workflow_dir, "rtl", "*.sv"),
        os.path.join(workflow_dir, "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "rtl", "*.sv"),
        os.path.join(workflow_dir, "digital", "rtl", "*.v"),
        os.path.join(workflow_dir, "handoff", "rtl", "*.sv"),
        os.path.join(workflow_dir, "handoff", "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "*.sv"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "*.v"),
    ]
    hits: list[str] = []
    for pattern in patterns:
        hits.extend(glob.glob(pattern))
    return sorted(dict.fromkeys(os.path.abspath(path) for path in hits))


def _load_json_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.endswith(".json") and os.path.exists(value):
        try:
            return json.loads(_read_text(value))
        except Exception:
            return None
    return None


def _spec_memory_macros(state: dict) -> list[dict[str, Any]]:
    spec = (
        _load_json_value(state.get("digital_spec_json"))
        or _load_json_value(state.get("spec_json"))
        or _load_json_value(state.get("digital_spec"))
    )
    if not isinstance(spec, dict):
        return []
    macros = spec.get("memory_macros")
    if not isinstance(macros, list):
        return []
    out: list[dict[str, Any]] = []
    for item in macros:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("module") or item.get("cell") or "").strip()
        kind = str(item.get("kind") or "").lower()
        if any(token in kind for token in ("model", "fallback", "synthesizable")) and "openram" not in kind:
            continue
        if not name or ("sram" not in name.lower() and "openram" not in name.lower() and "sram" not in kind and "openram" not in kind):
            continue
        ports = item.get("ports") if isinstance(item.get("ports"), dict) else {}
        addr_width = int(item.get("addr_width") or 0)
        depth = int(item.get("depth") or item.get("num_words") or 0)
        if addr_width <= 0 and depth > 0:
            addr_width = max(1, (depth - 1).bit_length())
        if depth <= 0 and addr_width > 0:
            depth = 1 << addr_width
        out.append({
            "kind": "spec_memory_macro",
            "macro_kind": kind,
            "cell": name,
            "openram_cell": name,
            "instance": item.get("instance_name") or item.get("instance"),
            "source_file": None,
            "connections": {},
            "ports": {
                "clk": ports.get("clk", "clk"),
                "csb": ports.get("csb", "csb"),
                "we": ports.get("we") or ports.get("web") or "web",
                "addr": ports.get("addr", "addr"),
                "din": ports.get("din", "din"),
                "dout": ports.get("dout", "dout"),
            },
            "addr_width": addr_width or 8,
            "data_width": int(item.get("data_width") or item.get("word_size") or 32),
            "depth": depth or (1 << (addr_width or 8)),
            "requires_mbist": bool(item.get("requires_mbist")),
        })
    return out


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _parse_declaration_widths(text: str) -> dict[str, int]:
    widths: dict[str, int] = {}
    widths.update(_parse_ansi_port_widths(text))
    clean = _strip_comments(text)
    decl_re = re.compile(
        r"\b(?:input|output|inout|wire|logic|reg)\s+(?:wire\s+|logic\s+|reg\s+)?"
        r"(?P<range>\[[^\]]+\]\s+)?(?P<names>[^;]+);",
        re.IGNORECASE,
    )
    for match in decl_re.finditer(clean):
        width = _range_width(match.group("range"))
        for raw in match.group("names").split(","):
            name = re.sub(r"=.*", "", raw).strip()
            name = re.sub(r"\[[^\]]+\]", "", name).strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                widths[name] = width
    return widths


def _range_width(range_text: str | None) -> int:
    if not range_text:
        return 1
    match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_text)
    if not match:
        return 1
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def _parse_ansi_port_widths(text: str) -> dict[str, int]:
    widths: dict[str, int] = {}
    clean = _strip_comments(text)
    header = re.search(
        r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*(?:#\s*\([^;]*?\)\s*)?\((?P<ports>.*?)\)\s*;",
        clean,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not header:
        return widths
    current_dir = ""
    current_range = ""
    for raw in header.group("ports").split(","):
        item = raw.strip()
        if not item:
            continue
        direction = re.search(r"\b(input|output|inout)\b", item, flags=re.IGNORECASE)
        if direction:
            current_dir = direction.group(1)
        range_match = re.search(r"\[[^\]]+\]", item)
        if range_match:
            current_range = range_match.group(0)
        item = re.sub(r"\b(input|output|inout|wire|logic|reg|signed)\b", " ", item, flags=re.IGNORECASE)
        item = re.sub(r"\[[^\]]+\]", " ", item)
        names = re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", item)
        if names and current_dir:
            widths[names[-1]] = _range_width(current_range)
    return widths


def _extract_named_connections(conn_text: str) -> dict[str, str]:
    conns: dict[str, str] = {}
    for match in re.finditer(r"\.(?P<port>[A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*(?P<sig>[^()]+?)\s*\)", conn_text):
        sig = match.group("sig").strip()
        sig = re.sub(r"\[[^\]]+\]", "", sig).strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", sig):
            conns[match.group("port")] = sig
    return conns


def _module_blocks(text: str) -> list[tuple[str, str]]:
    clean = _strip_comments(text)
    blocks: list[tuple[str, str]] = []
    pattern = re.compile(
        r"\bmodule\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\b(?P<body>.*?)(?=\bendmodule\b)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(clean):
        blocks.append((match.group("name"), match.group(0)))
    return blocks


def _canonical_memory_ports_from_names(names: list[str], text: str = "") -> dict[str, str]:
    available = {name.lower(): name for name in names}
    ports: dict[str, str] = {}

    def pick(candidates: tuple[str, ...]) -> str | None:
        for candidate in candidates:
            if candidate.lower() in available:
                return available[candidate.lower()]
        for name in names:
            n = name.lower()
            if any(len(candidate) > 1 and candidate.lower() in n for candidate in candidates):
                return name
        return None

    for key, candidates in {
        "clk": ("clk", "clock"),
        "csb": ("csb", "ceb", "cs_n", "cen"),
        "we": ("web", "wen", "we", "write_enable"),
        "addr": ("addr", "address"),
        "din": ("din", "data_in", "wdata", "d"),
        "dout": ("dout", "data_out", "rdata", "q"),
    }.items():
        value = pick(candidates)
        if value:
            ports[key] = value

    if "addr" not in ports:
        ports["addr"] = "addr"
    if "din" not in ports:
        ports["din"] = "din"
    if "dout" not in ports:
        ports["dout"] = "dout"
    if "clk" not in ports:
        ports["clk"] = "clk"
    if "we" not in ports:
        ports["we"] = "web" if re.search(r"\bweb\b", text) else "we"
    if "csb" not in ports and re.search(r"\bcsb\b", text):
        ports["csb"] = "csb"

    return ports


def _module_definition_port_map(text: str, module_name: str) -> dict[str, str]:
    module_text = text
    for name, body in _module_blocks(text):
        if name == module_name:
            module_text = body
            break
    widths = _parse_declaration_widths(module_text)
    return _canonical_memory_ports_from_names(list(widths), module_text)


def _memory_ports_from_verilog_model(model_path: str, cell: str) -> dict[str, str]:
    text = _read_text(model_path)
    if not text or not cell:
        return {}
    for module_name, body in _module_blocks(text):
        if module_name == cell:
            return _canonical_memory_ports_from_names(list(_parse_declaration_widths(body)), body)
    return {}


def _looks_like_memory_module_definition(module_name: str, body: str, ports: dict[str, str]) -> bool:
    name = module_name.lower()
    if "sram" not in name and "openram" not in name:
        return False
    required = {"clk", "we", "addr", "din", "dout"}
    if not required.issubset(set(ports)):
        return False
    clean = _strip_comments(body)
    has_memory_array = bool(re.search(r"\b(?:reg|logic)\s*(?:\[[^\]]+\]\s*)?\w+\s*\[[^\]]+\]", clean))
    canonical_name = bool("openram" in name or re.search(r"\d+x\d+", name))
    control_name = bool(re.search(r"(?:^|_)(controller|ctrl|control|fabric|subsystem|top)(?:_|$)", name))
    if control_name and not canonical_name:
        return False
    return has_memory_array or canonical_name


def _detect_memory_module_definition(files: list[str]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for path in files:
        text = _read_text(path)
        for module_name, body in _module_blocks(text):
            name = module_name.lower()
            if "sram" not in name and "openram" not in name:
                continue
            widths = _parse_declaration_widths(body)
            ports = _module_definition_port_map(text, module_name)
            if not _looks_like_memory_module_definition(module_name, body, ports):
                continue
            addr_width = widths.get(ports.get("addr", ""), 8)
            data_width = max(widths.get(ports.get("din", ""), 1), widths.get(ports.get("dout", ""), 1), 32)
            detected = {
                "kind": "memory_module_definition",
                "cell": module_name,
                "instance": None,
                "source_file": path,
                "connections": {},
                "ports": ports,
                "addr_width": int(addr_width or 8),
                "data_width": int(data_width or 32),
                "depth": 1 << int(addr_width or 8),
            }
            if best is None or not name.endswith("_model"):
                best = detected
            if not name.endswith("_model"):
                return detected
    return best


def _detect_openram_memories(files: list[str]) -> list[dict[str, Any]]:
    inst_re = re.compile(
        r"(?P<cell>[A-Za-z_][A-Za-z0-9_$]*(?:openram|sram)[A-Za-z0-9_$]*)\s*"
        r"(?:#\s*\([^;]*?\)\s*)?(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<conns>.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL,
    )
    memories: list[dict[str, Any]] = []
    for path in files:
        text = _read_text(path)
        widths = _parse_declaration_widths(text)
        for parent_module, body in _module_blocks(text):
            header_end = body.find(";")
            scan_body = body[header_end + 1 :] if header_end >= 0 else body
            for match in inst_re.finditer(scan_body):
                cell = match.group("cell")
                inst = match.group("inst")
                if inst.lower() in {"input", "output", "inout", "wire", "logic", "reg"}:
                    continue
                conns = _extract_named_connections(match.group("conns"))
                addr_sig = _first_signal(conns, ("addr", "address", "A", "ADDR"))
                data_sig = _first_signal(conns, ("din", "dout", "data", "wdata", "rdata", "D", "Q", "DATA"))
                addr_width = widths.get(addr_sig or "", 8)
                data_width = widths.get(data_sig or "", 32)
                memories.append({
                    "kind": "memory_instance",
                    "cell": cell,
                    "instance": inst,
                    "source_file": path,
                    "parent_module": parent_module,
                    "connections": conns,
                    "ports": _canonical_memory_ports_from_names(list(conns), match.group("conns")),
                    "addr_width": int(addr_width or 8),
                    "data_width": int(data_width or 32),
                    "depth": 1 << int(addr_width or 8),
                })
    if memories:
        return memories
    definition = _detect_memory_module_definition(files)
    return [definition] if definition else []


def _detect_openram_memory(files: list[str]) -> dict[str, Any] | None:
    memories = _detect_openram_memories(files)
    return memories[0] if memories else None


def _merge_spec_memory_with_rtl_detection(spec_macros: list[dict[str, Any]], detected: dict[str, Any] | None) -> dict[str, Any] | None:
    if not spec_macros:
        return detected
    spec = spec_macros[0]
    if not detected:
        return dict(spec)
    merged = dict(detected)
    if str(detected.get("cell") or "").lower() == str(spec.get("cell") or "").lower():
        merged.update({
            "cell": spec.get("cell") or detected.get("cell"),
            "addr_width": spec.get("addr_width") or detected.get("addr_width"),
            "data_width": spec.get("data_width") or detected.get("data_width"),
            "depth": spec.get("depth") or detected.get("depth"),
            "ports": spec.get("ports") or detected.get("ports"),
            "requires_mbist": spec.get("requires_mbist"),
            "spec_memory_macro": spec,
        })
        return merged
    merged["spec_memory_macro"] = spec
    return merged


def _merge_spec_memories_with_rtl_detection(spec_macros: list[dict[str, Any]], detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not spec_macros:
        return detected
    used: set[int] = set()
    merged: list[dict[str, Any]] = []
    for spec in spec_macros:
        match_idx: int | None = None
        for idx, det in enumerate(detected):
            if idx in used:
                continue
            det_cell = str(det.get("cell") or "").lower()
            spec_cell = str(spec.get("cell") or "").lower()
            same_cell = det_cell == spec_cell
            det_base = det_cell[:-6] if det_cell.endswith("_model") else det_cell
            fallback_cell = det_base == spec_cell
            same_inst = bool(spec.get("instance")) and str(det.get("instance") or "") == str(spec.get("instance"))
            same_shape_fallback = (
                any(token in det_cell for token in ("model", "fallback"))
                and int(det.get("addr_width") or 0) == int(spec.get("addr_width") or 0)
                and int(det.get("data_width") or 0) == int(spec.get("data_width") or 0)
                and int(det.get("depth") or 0) == int(spec.get("depth") or 0)
            )
            same_instance_shape = (
                same_inst
                and int(det.get("addr_width") or 0) == int(spec.get("addr_width") or 0)
                and int(det.get("data_width") or 0) == int(spec.get("data_width") or 0)
                and int(det.get("depth") or 0) == int(spec.get("depth") or 0)
            )
            if same_cell or fallback_cell or same_shape_fallback or same_instance_shape:
                match_idx = idx
                break
        if match_idx is None:
            merged.append(dict(spec))
            continue
        used.add(match_idx)
        item = _merge_spec_memory_with_rtl_detection([spec], detected[match_idx])
        if item:
            if str(detected[match_idx].get("cell") or "").lower() != str(spec.get("cell") or "").lower():
                item["cell"] = spec.get("cell")
                item["addr_width"] = spec.get("addr_width") or item.get("addr_width")
                item["data_width"] = spec.get("data_width") or item.get("data_width")
                item["depth"] = spec.get("depth") or item.get("depth")
                item["ports"] = spec.get("ports") or item.get("ports")
            item["macro_kind"] = spec.get("macro_kind")
            item["rtl_cell"] = detected[match_idx].get("cell")
            item["openram_cell"] = spec.get("openram_cell") or spec.get("cell")
            merged.append(item)
    return merged


def _validate_memory_config_names(memories: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    by_cell: dict[str, tuple[int, int, int]] = {}
    for memory in memories:
        cell = _openram_cell(memory).lower()
        if not cell:
            continue
        config = (
            int(memory.get("depth") or 0),
            int(memory.get("data_width") or 0),
            int(memory.get("addr_width") or 0),
        )
        if cell in by_cell and by_cell[cell] != config:
            issues.append(
                f"memory_macro_cell_name_reused_with_different_config:{memory.get('cell')} "
                f"first={by_cell[cell]} current={config}"
            )
        else:
            by_cell[cell] = config
    return issues


def _first_signal(conns: dict[str, str], names: tuple[str, ...]) -> str | None:
    lowered = {key.lower(): value for key, value in conns.items()}
    for name in names:
        if name in conns:
            return conns[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    for key, value in conns.items():
        if any(token.lower() in key.lower() for token in names):
            return value
    return None


def _patch_yaml_value(text: str, key_tokens: tuple[str, ...], value: str) -> tuple[str, bool]:
    lines = text.splitlines()
    changed = False
    pattern = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*:\s*(.*)$")
    for i, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        key = match.group(2).lower()
        if any(token in key for token in key_tokens):
            lines[i] = f"{match.group(1)}{match.group(2)}: {value}"
            changed = True
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed


def _patch_openram_config(config_text: str, memory: dict[str, Any]) -> str:
    text = config_text or ""
    cell = _openram_cell(memory) or "sram"
    updates = [
        (("memory_name", "module_name", "name"), cell),
        (("word_size", "data_width", "width"), str(int(memory.get("data_width") or 32))),
        (("num_words", "depth", "words"), str(int(memory.get("depth") or (1 << int(memory.get("addr_width") or 8))))),
        (("addr_width", "address_width"), str(int(memory.get("addr_width") or 8))),
    ]
    changed_keys: set[str] = set()
    for tokens, value in updates:
        text, changed = _patch_yaml_value(text, tokens, value)
        if changed:
            changed_keys.update(tokens)
    lines = [line for line in text.splitlines() if line.strip()] if text else []
    if not any(key in changed_keys for key in ("memory_name", "module_name", "name")):
        lines.append(f"memory_name: {cell}")
    if not any(key in changed_keys for key in ("word_size", "data_width", "width")):
        lines.append(f"word_size: {int(memory.get('data_width') or 32)}")
    if not any(key in changed_keys for key in ("num_words", "depth", "words")):
        lines.append(f"num_words: {int(memory.get('depth') or (1 << int(memory.get('addr_width') or 8)))}")
    if not any(key in changed_keys for key in ("addr_width", "address_width")):
        lines.append(f"addr_width: {int(memory.get('addr_width') or 8)}")
    return "\n".join(lines) + "\n"


def _openram_python_config(memory: dict[str, Any], output_dir: str) -> str:
    cell = _openram_cell(memory) or "sram"
    data_width = int(memory.get("data_width") or 32)
    depth = int(memory.get("depth") or (1 << int(memory.get("addr_width") or 8)))
    tech_name = str(memory.get("openram_tech") or memory.get("tech_name") or os.getenv("CHIPLOOP_OPENRAM_TECH_NAME") or "sky130")
    lines = [
        f"word_size = {data_width}",
        f"num_words = {depth}",
        f'tech_name = "{tech_name}"',
        f'output_path = r"{os.path.abspath(output_dir)}"',
        f'output_name = "{cell}"',
        "num_rw_ports = 1",
        "num_r_ports = 0",
        "num_w_ports = 0",
        'process_corners = ["TT"]',
        "supply_voltages = [1.8]",
        "temperatures = [25]",
        'drc_name = "magic"',
        'lvs_name = "netgen"',
        'pex_name = "magic"',
        "",
    ]
    return "\n".join(lines)


def _candidate_openram_config(project_dir: str, stage_dir: str, memory: dict[str, Any]) -> str:
    template_path = os.path.join(project_dir, "openram.yml")
    template = _read_text(template_path)
    config = _patch_openram_config(template, memory)
    if template_path:
        _write_text(template_path, config)
    stage_config = os.path.join(stage_dir, "openram.yml")
    _write_text(stage_config, config)
    return template_path if os.path.exists(template_path) else stage_config


def _candidate_openram_python_config(stage_dir: str, memory: dict[str, Any], output_dir: str) -> str:
    config_path = os.path.join(stage_dir, f"{_safe_name(_openram_cell(memory))}_openram_config.py")
    _write_text(config_path, _openram_python_config(memory, output_dir))
    return config_path


def _find_openram_compiler(state: dict[str, Any]) -> str | None:
    candidates = [
        os.getenv("CHIPLOOP_OPENRAM_COMPILER"),
        os.getenv("OPENRAM_COMPILER"),
    ]
    openram_home = os.getenv("OPENRAM_HOME")
    if openram_home:
        candidates.extend([
            os.path.join(openram_home, "..", "sram_compiler.py"),
            os.path.join(openram_home, "sram_compiler.py"),
        ])
    profiled = tool_path("openram", state)
    candidates.extend([
        profiled,
        shutil.which("openram"),
        shutil.which("sram_compiler.py"),
        "/usr/local/bin/openram",
        "/usr/bin/openram",
    ])
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.exists(candidate) or shutil.which(candidate):
            return os.path.abspath(candidate) if os.path.exists(candidate) else candidate
    return None


def _openram_command_variants(openram_compiler: str, config_path: str) -> list[list[str]]:
    config_no_suffix = os.path.splitext(config_path)[0]
    if openram_compiler.endswith(".py"):
        python_bin = os.getenv("CHIPLOOP_OPENRAM_PYTHON") or sys.executable
        return [
            [python_bin, openram_compiler, config_path],
            [python_bin, openram_compiler, config_no_suffix],
        ]
    return [
        [openram_compiler, config_path],
        [openram_compiler, config_no_suffix],
    ]


def _resolve_pdk_root(stage_dir: str) -> str | None:
    candidates = [
        os.getenv("PDK_ROOT"),
        os.getenv("CHIPLOOP_PDK_ROOT"),
        os.getenv("OPEN_PDKS_ROOT"),
    ]
    current = os.path.abspath(stage_dir)
    for _ in range(8):
        candidates.extend([
            os.path.join(current, "backend", "pdk"),
            os.path.join(current, "pdk"),
        ])
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    candidates.extend([
        "/root/chiploop-backend/backend/pdk",
        "/root/chiploop-backend/pdk",
        "/pdk",
    ])
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return None


def _openram_env(stage_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    pdk_root = _resolve_pdk_root(stage_dir)
    if pdk_root:
        env["PDK_ROOT"] = pdk_root
    nix_bin = "/root/.nix-profile/bin"
    if os.path.isdir(nix_bin):
        env["PATH"] = f"{nix_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def _classify_openram_failure(output: str) -> str:
    lowered = (output or "").lower()
    if "no space left on device" in lowered or "database or disk is full" in lowered:
        return "openram_no_space_left_on_device"
    if "set pdk_root" in lowered or "unable to find open_pdks tech file" in lowered:
        return "openram_pdk_root_not_set"
    if "custom cell pin names do not match spice file" in lowered and " vs []" in lowered:
        return "openram_custom_cell_spice_missing"
    if "nix is required" in lowered or "nix' was not found" in lowered:
        return "openram_nix_not_available"
    if "failed to initialize nix toolchain" in lowered:
        return "openram_nix_toolchain_failed"
    if "no module named" in lowered:
        return "openram_python_dependency_missing"
    return "openram_command_failed"


def _discover_openram_collateral(roots: list[str], memory: dict[str, Any]) -> dict[str, Any]:
    cell = _openram_cell(memory)
    result: dict[str, Any] = {
        "behavioral_model": None,
        "lib": [],
        "lef": [],
        "gds": [],
        "spice": [],
        "verilog": [],
    }
    seen: set[str] = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for walk_root, _, files in os.walk(root):
            for filename in files:
                path = os.path.abspath(os.path.join(walk_root, filename))
                if path in seen:
                    continue
                seen.add(path)
                lower = filename.lower()
                if lower.endswith((".v", ".sv")):
                    result["verilog"].append(path)
                    text = _read_text(path)
                    if cell and re.search(rf"\bmodule\s+{re.escape(cell)}\b", text) and not _memory_source_needs_sim_model({"source_file": path}):
                        result["behavioral_model"] = result["behavioral_model"] or path
                elif lower.endswith(".lib"):
                    result["lib"].append(path)
                elif lower.endswith(".lef"):
                    result["lef"].append(path)
                elif lower.endswith(".gds"):
                    result["gds"].append(path)
                elif lower.endswith((".sp", ".spice", ".cdl")):
                    result["spice"].append(path)
    for key in ("lib", "lef", "gds", "spice", "verilog"):
        result[key] = sorted(result[key])
    return result


def _validate_openram_collateral(collateral: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
    cell = _openram_cell(memory)
    checks: dict[str, Any] = {
        "status": "clean",
        "missing": [],
        "issues": [],
        "required": ["behavioral_model", "lib", "lef", "gds"],
    }
    behavioral = collateral.get("behavioral_model")
    if not behavioral or not os.path.isfile(str(behavioral)):
        checks["missing"].append("behavioral_model")
    else:
        text = _read_text(str(behavioral))
        if cell and not re.search(rf"\bmodule\s+{re.escape(cell)}\b", text):
            checks["issues"].append("behavioral_model_module_name_mismatch")
        if _memory_source_needs_sim_model({"source_file": behavioral}):
            checks["issues"].append("behavioral_model_has_no_memory_behavior")

    for key in ("lib", "lef", "gds"):
        paths = collateral.get(key) if isinstance(collateral.get(key), list) else []
        existing = [path for path in paths if isinstance(path, str) and os.path.isfile(path) and os.path.getsize(path) > 0]
        if not existing:
            checks["missing"].append(key)
            continue
        if key in {"lib", "lef"} and cell:
            has_cell = any(cell in _read_text(path) for path in existing)
            if not has_cell:
                checks["issues"].append(f"{key}_cell_name_not_found")

    if checks["missing"] or checks["issues"]:
        checks["status"] = "issues"
    return checks


def _parse_sram_macro_dimensions(name: str) -> tuple[int | None, int | None]:
    text = str(name or "")
    match = re.search(r"(?<!\d)(?P<width>\d+)x(?P<depth>\d+)(?!\d)", text, flags=re.I)
    if match:
        return int(match.group("width")), int(match.group("depth"))
    match = re.search(r"(?:^|_)(?P<width>\d+)_(?P<depth>\d+)(?:_|$)", text)
    if match:
        return int(match.group("width")), int(match.group("depth"))
    return None, None


def _precompiled_sram_roots(stage_dir: str) -> list[str]:
    roots: list[str] = []
    pdk_root = _resolve_pdk_root(stage_dir)
    if pdk_root:
        for path in glob.glob(os.path.join(pdk_root, "*", "libs.ref", "*sram*")):
            if os.path.isdir(path):
                roots.append(path)
        for path in glob.glob(os.path.join(pdk_root, "*sram*")):
            if os.path.isdir(path):
                roots.append(path)
    extra = os.getenv("CHIPLOOP_PRECOMPILED_SRAM_ROOTS") or os.getenv("CHIPLOOP_SRAM_MACRO_ROOTS") or ""
    for item in re.split(r"[;:]", extra):
        item = item.strip()
        if item and os.path.isdir(item):
            roots.append(item)
    return sorted(dict.fromkeys(os.path.abspath(path) for path in roots))


def _discover_precompiled_sram_macros(stage_dir: str) -> list[dict[str, Any]]:
    by_cell: dict[str, dict[str, Any]] = {}
    for root in _precompiled_sram_roots(stage_dir):
        files: list[str] = []
        for walk_root, _, names in os.walk(root):
            for filename in names:
                if filename.lower().endswith((".v", ".sv", ".lib", ".lef", ".gds", ".sp", ".spice", ".cdl")):
                    files.append(os.path.abspath(os.path.join(walk_root, filename)))

        base_cells: set[str] = set()
        for path in files:
            lower = path.lower()
            if lower.endswith((".v", ".sv", ".lef", ".gds", ".sp", ".spice", ".cdl")):
                base_cells.add(os.path.splitext(os.path.basename(path))[0])

        for cell in sorted(base_cells):
            width, depth = _parse_sram_macro_dimensions(cell)
            if not width or not depth:
                continue
            item = by_cell.setdefault(
                cell,
                {
                    "cell": cell,
                    "data_width": width,
                    "depth": depth,
                    "addr_width": max(1, (depth - 1).bit_length()),
                    "collateral": {"behavioral_model": None, "lib": [], "lef": [], "gds": [], "spice": [], "verilog": []},
                    "source": "precompiled_sram_macro",
                    "root": root,
                },
            )
            collateral = item["collateral"]
            for path in files:
                filename = os.path.basename(path)
                stem = os.path.splitext(filename)[0]
                lower = filename.lower()
                if stem == cell and lower.endswith((".v", ".sv")):
                    collateral["verilog"].append(path)
                    collateral["behavioral_model"] = collateral["behavioral_model"] or path
                elif stem == cell and lower.endswith(".lef"):
                    collateral["lef"].append(path)
                elif stem == cell and lower.endswith(".gds"):
                    collateral["gds"].append(path)
                elif stem == cell and lower.endswith((".sp", ".spice", ".cdl")):
                    collateral["spice"].append(path)
                elif filename.startswith(f"{cell}_") and lower.endswith(".lib"):
                    collateral["lib"].append(path)
            for key in ("lib", "lef", "gds", "spice", "verilog"):
                collateral[key] = sorted(dict.fromkeys(collateral[key]))
    return sorted(by_cell.values(), key=lambda item: (item["data_width"], item["depth"], item["cell"]))


def _select_precompiled_sram_macro(memory: dict[str, Any], stage_dir: str) -> dict[str, Any] | None:
    req_width = int(memory.get("data_width") or 0)
    req_depth = int(memory.get("depth") or (1 << int(memory.get("addr_width") or 0)))
    candidates = [
        item for item in _discover_precompiled_sram_macros(stage_dir)
        if item.get("data_width") == req_width
    ]
    exact = [item for item in candidates if item.get("depth") == req_depth]
    if exact:
        return exact[0]
    compatible = [item for item in candidates if int(item.get("depth") or 0) >= req_depth]
    if compatible:
        return sorted(compatible, key=lambda item: (int(item.get("depth") or 0), str(item.get("cell") or "")))[0]
    return None


def _stage_precompiled_sram_macro_collateral(
    memory: dict[str, Any],
    stage_dir: str,
) -> dict[str, Any]:
    selected = _select_precompiled_sram_macro(memory, stage_dir)
    result: dict[str, Any] = {
        "status": "failed",
        "generator": "precompiled_sram_macro",
        "requested": {
            "cell": _openram_cell(memory),
            "data_width": int(memory.get("data_width") or 0),
            "depth": int(memory.get("depth") or (1 << int(memory.get("addr_width") or 0))),
            "addr_width": int(memory.get("addr_width") or 0),
        },
        "selected": None,
        "collateral": {},
        "validation": {},
    }
    if not selected:
        result["reason"] = "precompiled_sram_macro_not_found"
        return result

    collateral = selected["collateral"]
    validation = _validate_openram_collateral(collateral, {"cell": selected["cell"]})
    result.update({
        "selected": {k: v for k, v in selected.items() if k != "collateral"},
        "collateral": collateral,
        "validation": validation,
    })
    if validation.get("status") != "clean":
        result["reason"] = "precompiled_sram_collateral_validation_failed"
        return result

    requested_depth = result["requested"]["depth"]
    selected_depth = int(selected["depth"])
    memory["logical_cell"] = _openram_cell(memory)
    memory["logical_depth"] = requested_depth
    memory["logical_addr_width"] = int(memory.get("addr_width") or 0)
    memory["cell"] = selected["cell"]
    memory["openram_cell"] = selected["cell"]
    memory["data_width"] = int(selected["data_width"])
    memory["depth"] = selected_depth
    memory["addr_width"] = int(selected["addr_width"])
    memory["openram_behavioral_model"] = collateral["behavioral_model"]
    actual_ports = _memory_ports_from_verilog_model(collateral["behavioral_model"], selected["cell"])
    if actual_ports:
        memory["ports"] = actual_ports
        result["actual_ports"] = actual_ports

    result["status"] = "validated"
    result["depth_match"] = selected_depth == requested_depth
    if selected_depth != requested_depth:
        result["note"] = "Selected a real precompiled SRAM macro with matching data width and larger depth."
    return result


def _generate_openram_collateral(
    memory: dict[str, Any],
    autombist: str,
    project_dir: str,
    stage_dir: str,
    workflow_id: str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec_macro = memory.get("spec_memory_macro") if isinstance(memory.get("spec_memory_macro"), dict) else {}
    kind = str(memory.get("macro_kind") or spec_macro.get("macro_kind") or memory.get("kind") or spec_macro.get("kind") or "").lower()
    if any(token in kind for token in ("prebuilt", "precompiled")):
        result = _stage_precompiled_sram_macro_collateral(memory, stage_dir)
        result["selection_policy"] = "explicit_precompiled_sram_macro"
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital/mbist_rtl_insertion",
            "openram_collateral_generation.json",
            json.dumps(result, indent=2),
        )
        return result

    output_dir = os.path.join(stage_dir, "openram_out")
    _ensure_dir(output_dir)
    config_path = _candidate_openram_python_config(stage_dir, memory, output_dir)
    openram_compiler = _find_openram_compiler(state or {})
    attempts = _openram_command_variants(openram_compiler, config_path) if openram_compiler else []
    openram_env = _openram_env(stage_dir)
    result: dict[str, Any] = {
        "status": "not_run",
        "generator": "openram",
        "openram_compiler": openram_compiler,
        "pdk_root": openram_env.get("PDK_ROOT"),
        "config": config_path,
        "output_dir": output_dir,
        "attempts": [],
        "collateral": {},
        "validation": {},
    }
    if not openram_compiler:
        result["status"] = "failed"
        result["reason"] = "openram_compiler_not_found"
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital/mbist_rtl_insertion",
            "openram_collateral_generation.json",
            json.dumps(result, indent=2),
        )
        return result
    command_failed_reason = "openram_command_failed"
    for index, cmd in enumerate(attempts, start=1):
        rc, out = _run(cmd, cwd=stage_dir, timeout=3600, env=openram_env)
        log_name = f"openram_sram_compiler_attempt{index}.log"
        log_path = os.path.join(stage_dir, log_name)
        _write_text(log_path, out)
        _publish_stage_file(workflow_id, log_name, log_path)
        attempt = {"cmd": cmd, "rc": rc, "log": log_path}
        result["attempts"].append(attempt)
        if rc == 0:
            result["status"] = "command_completed"
            break
        command_failed_reason = _classify_openram_failure(out)
    collateral = _discover_openram_collateral([output_dir], memory)
    result["collateral"] = collateral
    validation = _validate_openram_collateral(collateral, memory)
    result["validation"] = validation
    if validation.get("status") == "clean":
        result["status"] = "validated"
        memory["openram_behavioral_model"] = collateral["behavioral_model"]
    elif result["status"] == "command_completed":
        result["status"] = "collateral_validation_failed"
        result["reason"] = "openram_collateral_validation_failed"
    else:
        result["status"] = "failed"
        result["reason"] = command_failed_reason
    save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        "digital/mbist_rtl_insertion",
        "openram_collateral_generation.json",
        json.dumps(result, indent=2),
    )
    return result


def _patch_autombist_config(config_text: str, memory: dict[str, Any]) -> str:
    ports = memory.get("ports") if isinstance(memory.get("ports"), dict) else {}
    cell = _openram_cell(memory)
    wrapper_module = f"{cell}_mbist"
    lines = [
        f"memory_name: {cell}",
        f"wrapper_module_name: {wrapper_module}",
        f"addr_width: {int(memory['addr_width'])}",
        f"data_width: {int(memory['data_width'])}",
        "we_active_low: true",
        "ports:",
        f"  clk: {ports.get('clk', 'clk')}",
        f"  addr: {ports.get('addr', 'addr')}",
        f"  din: {ports.get('din', 'din')}",
        f"  dout: {ports.get('dout', 'dout')}",
        f"  we: {ports.get('we', 'web')}",
    ]
    if ports.get("csb"):
        lines.append(f"  csb: {ports['csb']}")
    return "\n".join(lines) + "\n"


def _copy_tree_files(src_dir: str, dst_dir: str, exts: tuple[str, ...]) -> list[str]:
    copied: list[str] = []
    if not os.path.isdir(src_dir):
        return copied
    for root, _, files in os.walk(src_dir):
        for filename in files:
            if not filename.lower().endswith(exts):
                continue
            src = os.path.join(root, filename)
            rel = os.path.relpath(src, src_dir)
            dst = os.path.join(dst_dir, rel)
            _ensure_dir(os.path.dirname(dst))
            shutil.copy2(src, dst)
            copied.append(os.path.abspath(dst))
    return sorted(copied)


def _module_ports(text: str) -> tuple[str | None, list[str]]:
    match = re.search(
        r"\bmodule\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([^;]*?\)\s*)?\((?P<ports>.*?)\)\s*;",
        _strip_comments(text),
        flags=re.DOTALL,
    )
    if not match:
        return None, []
    ports = []
    for raw in match.group("ports").split(","):
        token = raw.strip()
        token = re.sub(r"\b(input|output|inout|wire|logic|reg)\b", " ", token)
        token = re.sub(r"\[[^\]]+\]", " ", token)
        names = re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", token)
        if names:
            ports.append(names[-1])
    return match.group("name"), ports


def _pick_generated_wrapper(generated_rtl: list[str], memory_cell: str) -> tuple[str | None, list[str]]:
    preferred = os.path.basename(memory_cell).lower() + "_mbist"
    best_name: str | None = None
    best_ports: list[str] = []
    for path in generated_rtl:
        name, ports = _module_ports(_read_text(path))
        if not name:
            continue
        lowered = name.lower()
        if lowered == preferred or lowered.endswith("_mbist") or "mbist" in lowered:
            best_name, best_ports = name, ports
            if lowered == preferred:
                break
    return best_name, best_ports


def _memory_port_role(port: str) -> str | None:
    p = re.sub(r"[^a-z0-9_]", "", str(port).lower())
    for prefix in ("func_", "functional_", "user_", "mem_", "memory_"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    if re.fullmatch(r"(clk|clock|clk\d+|clock\d+)", p):
        return "clk"
    if re.fullmatch(r"(csb|ceb|cen|cs_n|ce_n|csb\d+|ceb\d+)", p):
        return "csb"
    if re.fullmatch(r"(web|wen|we|write_enable|wr_en|web\d+|wen\d+|we\d+)", p):
        return "we"
    if re.fullmatch(r"(addr|address|a|addr\d+|address\d+)", p):
        return "addr"
    if re.fullmatch(r"(din|data_in|wdata|wd|din\d+|data_in\d+|wdata\d+)", p):
        return "din"
    if re.fullmatch(r"(dout|data_out|rdata|q|dout\d+|data_out\d+|rdata\d+)", p):
        return "dout"
    if "addr" in p:
        return "addr"
    if "dout" in p or "rdata" in p or "data_out" in p:
        return "dout"
    if "din" in p or "wdata" in p or "data_in" in p:
        return "din"
    if "web" in p or "wen" in p or "write" in p:
        return "we"
    if "csb" in p or "ceb" in p:
        return "csb"
    return None


def _connection_for_role(role: str | None, conns: dict[str, str]) -> str | None:
    if not role:
        return None
    for key, sig in conns.items():
        if _memory_port_role(key) == role:
            return sig
    return None


def _mbist_status_wire(instance: str, port: str) -> str:
    return f"mbist_{_safe_name(instance)}_{_safe_name(port)}"


def _fallback_signal_for_port(port: str, conns: dict[str, str], known_signals: set[str], instance: str = "") -> str:
    p = port.lower()
    role_sig = _connection_for_role(_memory_port_role(port), conns)
    if role_sig:
        return role_sig
    for key, sig in conns.items():
        k = key.lower()
        if ("clk" in p or "clock" in p) and ("clk" in k or "clock" in k):
            return sig
        if ("rst" in p or "reset" in p) and ("rst" in k or "reset" in k):
            return sig
    if "done" in p or "fail" in p:
        return _mbist_status_wire(instance or "wrapper", port)
    for sig in sorted(known_signals):
        s = sig.lower()
        if ("rst" in p or "reset" in p) and ("rst" in s or "reset" in s):
            return sig
        if ("start" in p or "enable" in p or p.endswith("_en")) and ("bist_start" in s or "mbist_start" in s):
            return sig
    if "reset" in p or "rst" in p:
        return "1'b1" if p.endswith("_n") or "reset_n" in p else "1'b0"
    return "1'b0"


def _wrapper_instance_mapping(
    instance: str,
    wrapper_ports: list[str],
    conns: dict[str, str],
    known_signals: set[str],
) -> tuple[list[str], list[str]]:
    mapped: list[str] = []
    local_wires: list[str] = []
    for port in wrapper_ports:
        port = str(port)
        if port in conns:
            sig = conns[port]
        else:
            sig = _fallback_signal_for_port(port, conns, known_signals, instance)
        if re.match(r"^mbist_[A-Za-z0-9_]+$", sig):
            local_wires.append(sig)
        mapped.append(f".{port}({sig})")
    return mapped, sorted(dict.fromkeys(local_wires))


def _replace_memory_instance_with_wrapper(memory: dict[str, Any], wrapper_name: str, wrapper_ports: list[str], out_dir: str) -> str | None:
    src = str(memory.get("source_file") or "")
    text = _read_text(src)
    if not text or not wrapper_name or not wrapper_ports:
        return None
    conns = memory.get("connections") if isinstance(memory.get("connections"), dict) else {}
    if not conns:
        return None

    known_signals = set(_parse_declaration_widths(text).keys()) | {str(sig) for sig in conns.values()}
    mapped, local_wires = _wrapper_instance_mapping(str(memory["instance"]), wrapper_ports, conns, known_signals)
    wire_decls = "".join(f"wire {name};\n" for name in local_wires if not re.search(rf"\b{name}\b", text))
    replacement = wire_decls + f"{wrapper_name} {memory['instance']} (\n    " + ",\n    ".join(mapped) + "\n  );"

    cell = re.escape(_rtl_instance_cell(memory))
    inst = re.escape(str(memory["instance"]))
    pattern = re.compile(
        rf"\b{cell}\s*(?:#\s*\([^;]*?\)\s*)?{inst}\s*\(.*?\)\s*;",
        flags=re.DOTALL,
    )
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        return None
    dst = os.path.join(out_dir, os.path.basename(src))
    _write_text(dst, new_text)
    return os.path.abspath(dst)


def _replace_memory_instances_with_wrappers(items: list[dict[str, Any]], out_dir: str) -> list[str]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
        src = str(memory.get("source_file") or "")
        if src and os.path.isfile(src):
            by_source.setdefault(src, []).append(item)

    patched_files: list[str] = []
    for src, source_items in by_source.items():
        text = _read_text(src)
        if not text:
            continue
        changed = False
        for item in source_items:
            memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
            wrapper_name = str(item.get("wrapper_module") or "")
            wrapper_ports = item.get("wrapper_ports") if isinstance(item.get("wrapper_ports"), list) else []
            conns = memory.get("connections") if isinstance(memory.get("connections"), dict) else {}
            if not wrapper_name or not wrapper_ports or not conns:
                continue
            known_signals = set(_parse_declaration_widths(text).keys()) | {str(sig) for sig in conns.values()}
            mapped, local_wires = _wrapper_instance_mapping(str(memory["instance"]), [str(port) for port in wrapper_ports], conns, known_signals)
            wire_decls = "".join(f"wire {name};\n" for name in local_wires if not re.search(rf"\b{name}\b", text))
            replacement = wire_decls + f"{wrapper_name} {memory['instance']} (\n    " + ",\n    ".join(mapped) + "\n  );"
            cell = re.escape(_rtl_instance_cell(memory))
            inst = re.escape(str(memory["instance"]))
            pattern = re.compile(
                rf"\b{cell}\s*(?:#\s*\([^;]*?\)\s*)?{inst}\s*\(.*?\)\s*;",
                flags=re.DOTALL,
            )
            text, count = pattern.subn(replacement, text, count=1)
            changed = changed or count == 1
        if changed:
            dst = os.path.abspath(os.path.join(out_dir, os.path.basename(src)))
            _write_text(dst, text)
            patched_files.append(dst)
    return sorted(dict.fromkeys(patched_files))


def _select_functional_wrapper_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []
    instantiated_memory_cells = {
        str(_rtl_instance_cell(memory)).lower()
        for item in items
        for memory in [item.get("memory") if isinstance(item.get("memory"), dict) else {}]
        if _rtl_instance_cell(memory)
    }
    selected: list[dict[str, Any]] = []
    for item in items:
        memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
        parent = str(memory.get("parent_module") or "").lower()
        if parent and parent in instantiated_memory_cells:
            continue
        selected.append(item)
    return selected or items


def _explicit_json_verdict(obj: Any) -> bool | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in {"status", "result", "verdict", "outcome"}:
                v = str(value).strip().lower()
                if v in {"pass", "passed", "ok", "success", "clean"}:
                    return True
                if v in {"fail", "failed", "error", "timeout"}:
                    return False
            if lowered in {"passed", "success", "ok"} and isinstance(value, bool):
                return value
        for value in obj.values():
            verdict = _explicit_json_verdict(value)
            if verdict is not None:
                return verdict
    elif isinstance(obj, list):
        verdicts = [_explicit_json_verdict(item) for item in obj]
        verdicts = [item for item in verdicts if item is not None]
        if verdicts:
            return all(verdicts)
    return None


def _simulation_passed(report_dir: str, run_output: str, rc: int = 0) -> bool:
    latest = os.path.join(report_dir, "latest.json")
    if os.path.exists(latest):
        try:
            data = json.loads(_read_text(latest))
            verdict = _explicit_json_verdict(data)
            if verdict is not None:
                return verdict
        except Exception:
            pass
    report_txt = os.path.join(report_dir, "report.txt")
    report = _read_text(report_txt).lower()
    if re.search(r"\b(overall|summary|simulation)\s*[:=]\s*(pass|passed|ok|success)\b", report):
        return True
    if re.search(r"\b(overall|summary|simulation)\s*[:=]\s*(fail|failed|error|timeout)\b", report):
        return False
    lowered = run_output.lower()
    hard_fail_tokens = (
        "traceback",
        "assertionerror",
        "syntax error",
        "compile error",
        "simulation failed",
        "simulator failed",
        "no module named",
        "module not found",
        "command not found",
        "error:",
    )
    if any(token in lowered for token in hard_fail_tokens):
        return False
    return rc == 0


def _has_structural_width_warnings(tool_output: str) -> bool:
    text = tool_output or ""
    patterns = (
        r"warning:\s+Port\s+\d+\s+\([^)]+\)\s+of\s+\S+\s+expects\s+\d+\s+bits,\s+got\s+\d+",
        r"\bPadding\s+\d+\s+high bits\b",
        r"\bPruning\s+\d+\s+high bits\b",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _run_integrated_rtl_lint(state: dict, workflow_id: str, stage_dir: str, rtl_files: list[str]) -> dict[str, Any]:
    lint_dir = os.path.join(stage_dir, "integrated_rtl_lint")
    _ensure_dir(lint_dir)
    result: dict[str, Any] = {
        "status": "not_run",
        "rtl_files": rtl_files,
        "tools": {},
    }
    if not rtl_files:
        result["status"] = "failed"
        result["reason"] = "missing_integrated_rtl"
        return result

    iverilog = tool_path("iverilog", state) or shutil.which("iverilog")
    verilator = tool_path("verilator", state) or shutil.which("verilator")
    result["tools"] = {
        "compile": "iverilog",
        "lint": "verilator",
        "iverilog_path": iverilog,
        "verilator_path": verilator,
    }
    if not iverilog or not verilator:
        result["status"] = "failed"
        result["reason"] = "lint_tool_unavailable"
        result["iverilog"] = {"status": "tool_unavailable" if not iverilog else "not_run"}
        result["verilator"] = {"status": "tool_unavailable" if not verilator else "not_run"}
        return result

    iverilog_out = os.path.join(lint_dir, "mbist_integrated_rtl_iverilog.out")
    rc_i, out_i = _run([iverilog, "-g2012", "-o", iverilog_out, *rtl_files], cwd=lint_dir, timeout=180)
    iverilog_log = os.path.join(lint_dir, "mbist_integrated_rtl_iverilog.log")
    _write_text(iverilog_log, out_i)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", "mbist_integrated_rtl_iverilog.log", out_i)
    width_warnings = _has_structural_width_warnings(out_i)
    result["iverilog"] = {
        "status": "pass" if rc_i == 0 and not width_warnings else "fail",
        "returncode": rc_i,
        "log": iverilog_log,
        "structural_width_warnings": width_warnings,
        "stdout_tail": out_i.splitlines()[-80:],
    }

    rc_v, out_v = _run([verilator, "--lint-only", "-Wall", "-Wno-fatal", *rtl_files], cwd=lint_dir, timeout=180)
    verilator_log = os.path.join(lint_dir, "mbist_integrated_rtl_verilator.log")
    _write_text(verilator_log, out_v)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", "mbist_integrated_rtl_verilator.log", out_v)
    error_count = len(re.findall(r"%Error", out_v))
    warning_count = len(re.findall(r"%Warning", out_v))
    result["verilator"] = {
        "status": "pass" if rc_v == 0 and error_count == 0 else "fail",
        "returncode": rc_v,
        "log": verilator_log,
        "error_count": error_count,
        "warning_count": warning_count,
        "stdout_tail": out_v.splitlines()[-80:],
    }

    if result["iverilog"]["status"] == "pass" and result["verilator"]["status"] == "pass":
        result["status"] = "pass"
    else:
        result["status"] = "failed"
        result["reason"] = "integrated_rtl_lint_failed"
    save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        "digital/mbist_rtl_insertion",
        "mbist_integrated_rtl_lint.json",
        json.dumps(result, indent=2),
    )
    return result


def _publish_stage_file(workflow_id: str, filename: str, path: str) -> None:
    if os.path.exists(path):
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", filename, _read_text(path))


def _write_publish_summary(workflow_id: str, stage_dir: str, summary: dict[str, Any]) -> None:
    content = json.dumps(summary, indent=2)
    _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), content)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", "mbist_rtl_insertion_summary.json", content)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "mbist_rtl_insertion")
    _ensure_dir(stage_dir)

    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    enabled = bool(
        toggles.get("insert_mbist")
        or toggles.get("enable_mbist_rtl_insertion")
        or state.get("insert_mbist")
        or state.get("enable_mbist_rtl_insertion")
    )
    rtl_files = _rtl_files(state, workflow_dir)
    spec_memory_macros = _spec_memory_macros(state)
    detected_memories = _detect_openram_memories(rtl_files)
    memories = _merge_spec_memories_with_rtl_detection(spec_memory_macros, detected_memories)
    memory = memories[0] if memories else None
    autombist = tool_path("autombist", state)

    summary: dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "enabled": enabled,
        "status": "disabled" if not enabled else "not_started",
        "detected_memory": memory,
        "detected_memories": memories,
        "detected_rtl_memories": detected_memories,
        "spec_memory_macros": spec_memory_macros,
        "autombist_executable": autombist,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "rtl_files_scanned": rtl_files,
    }

    if not enabled:
        _write_publish_summary(workflow_id, stage_dir, summary)
        state.setdefault("digital", {})["mbist_rtl_insertion"] = summary
        state["status"] = f"{AGENT_NAME}: disabled"
        return state
    if not memories:
        summary["status"] = "skipped_no_openram_sram_detected"
        _write_publish_summary(workflow_id, stage_dir, summary)
        state.setdefault("digital", {})["mbist_rtl_insertion"] = summary
        state["status"] = f"{AGENT_NAME}: no OpenRAM/SRAM memory detected"
        return state
    memory_config_issues = _validate_memory_config_names(memories)
    if memory_config_issues:
        summary["status"] = "failed"
        summary["reason"] = "invalid_memory_macro_config"
        summary["issues"] = memory_config_issues
        _write_publish_summary(workflow_id, stage_dir, summary)
        raise RuntimeError("MBIST RTL insertion failed: memory macro cell names must be unique per SRAM configuration.")
    if not autombist:
        summary["status"] = "failed"
        summary["reason"] = "autombist_not_found"
        _write_publish_summary(workflow_id, stage_dir, summary)
        raise RuntimeError("MBIST RTL insertion requested but autombist was not found in tool profile/PATH.")

    allow_generated_sim_model = False
    summary["allow_generated_sim_model"] = allow_generated_sim_model
    summary["simulation_model_policy"] = "require_real_openram_behavioral_model"
    digital = state.setdefault("digital", {})
    openram_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    memory_results: list[dict[str, Any]] = []
    generated_rtl_all: list[str] = []
    wrapper_items: list[dict[str, Any]] = []

    for index, memory in enumerate(memories):
        mem_slug = _safe_name(f"{index}_{_openram_cell(memory)}_{memory.get('instance') or 'macro'}")
        memory_stage_dir = os.path.join(stage_dir, mem_slug)
        project_dir = os.path.join(memory_stage_dir, "autombist_project")
        _ensure_dir(project_dir)
        result: dict[str, Any] = {"memory": memory, "stage_dir": memory_stage_dir}

        rc_init, out_init = _run([autombist, "init", "--out", project_dir, "--force"], cwd=memory_stage_dir, timeout=120)
        init_log = os.path.join(memory_stage_dir, "autombist_init.log")
        _write_text(init_log, out_init)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", f"{mem_slug}/autombist_init.log", out_init)
        config_path = os.path.join(project_dir, "config.yml")
        if rc_init != 0 or not os.path.exists(config_path):
            result.update({"status": "failed", "reason": "autombist_init_failed", "init_rc": rc_init})
            memory_results.append(result)
            continue

        cache_key = (
            _openram_cell(memory),
            int(memory.get("depth") or 0),
            int(memory.get("data_width") or 0),
            int(memory.get("addr_width") or 0),
        )
        if cache_key not in openram_cache:
            openram_cache[cache_key] = _generate_openram_collateral(memory, autombist, project_dir, memory_stage_dir, workflow_id, state)
        else:
            cached = openram_cache[cache_key]
            collateral = cached.get("collateral") if isinstance(cached.get("collateral"), dict) else {}
            if collateral.get("behavioral_model"):
                memory["openram_behavioral_model"] = collateral["behavioral_model"]
            actual_ports = cached.get("actual_ports") if isinstance(cached.get("actual_ports"), dict) else {}
            if actual_ports:
                memory["ports"] = actual_ports
        openram_generation = openram_cache[cache_key]
        result["openram_collateral_generation"] = openram_generation
        collateral = openram_generation.get("collateral") if isinstance(openram_generation.get("collateral"), dict) else {}
        for state_key, collateral_key in (
            ("macro_libs", "lib"),
            ("macro_lefs", "lef"),
            ("macro_gds", "gds"),
            ("macro_spice", "spice"),
        ):
            existing = digital.get(state_key) if isinstance(digital.get(state_key), list) else []
            generated = collateral.get(collateral_key) if isinstance(collateral.get(collateral_key), list) else []
            if generated:
                digital[state_key] = sorted(dict.fromkeys([*existing, *generated]))
        if openram_generation.get("status") != "validated":
            result.update({"status": "failed", "reason": openram_generation.get("reason") or "openram_collateral_validation_failed"})
            memory_results.append(result)
            continue

        patched_config = _patch_autombist_config(_read_text(config_path), memory)
        _write_text(config_path, patched_config)
        _write_text(os.path.join(memory_stage_dir, "config.yml"), patched_config)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", f"{mem_slug}/config.yml", patched_config)

        memory_model_stage = _stage_memory_model_for_autombist(
            memory,
            autombist,
            memory_stage_dir,
            allow_generated_sim_model=allow_generated_sim_model,
        )
        result["memory_model_stage"] = memory_model_stage
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital/mbist_rtl_insertion",
            f"{mem_slug}/memory_model_stage.json",
            json.dumps(memory_model_stage, indent=2),
        )
        if memory_model_stage.get("status") not in {"staged", "not_needed"}:
            result.update({"status": "failed", "reason": "autombist_memory_model_stage_failed"})
            memory_results.append(result)
            continue

        out_dir = os.path.join(memory_stage_dir, "autombist_out")
        rc_run, out_run = _run(
            [autombist, "run", "--config", config_path, "--out", out_dir, "--test"],
            cwd=memory_stage_dir,
            timeout=900,
        )
        run_log = os.path.join(memory_stage_dir, "autombist_run.log")
        _write_text(run_log, out_run)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", f"{mem_slug}/autombist_run.log", out_run)

        generated_root = os.path.join(out_dir, _openram_cell(memory))
        if not os.path.isdir(generated_root):
            hits = [path for path in glob.glob(os.path.join(out_dir, "*")) if os.path.isdir(path)]
            generated_root = hits[0] if hits else generated_root
        reports_dir = os.path.join(generated_root, "reports")
        if not _simulation_passed(reports_dir, out_run, rc_run):
            result.update({
                "status": "failed",
                "reason": "autombist_standalone_simulation_failed",
                "run_rc": rc_run,
                "run_log": run_log,
            })
            memory_results.append(result)
            _publish_stage_file(workflow_id, f"{mem_slug}/autombist_report.txt", os.path.join(reports_dir, "report.txt"))
            _publish_stage_file(workflow_id, f"{mem_slug}/autombist_latest.json", os.path.join(reports_dir, "latest.json"))
            _publish_stage_file(workflow_id, f"{mem_slug}/simulate.log", os.path.join(generated_root, "simulate.log"))
            continue

        memory_rtl_dir = os.path.join(memory_stage_dir, "generated_rtl")
        generated_rtl = _copy_tree_files(generated_root, memory_rtl_dir, (".v", ".sv", ".vh", ".svh"))
        generated_rtl_all.extend(generated_rtl)
        wrapper_name, wrapper_ports = _pick_generated_wrapper(generated_rtl, _openram_cell(memory))
        result.update({
            "status": "simulated",
            "simulation": {"status": "pass", "reports_dir": reports_dir, "run_log": run_log},
            "wrapper_module": wrapper_name,
            "wrapper_ports": wrapper_ports,
            "generated_rtl": generated_rtl,
        })
        if memory.get("kind") == "memory_instance" and wrapper_name and wrapper_ports:
            wrapper_items.append({"memory": memory, "wrapper_module": wrapper_name, "wrapper_ports": wrapper_ports})
        memory_results.append(result)

    summary["memory_results"] = memory_results
    failed = [item for item in memory_results if item.get("status") == "failed"]
    if failed:
        summary["status"] = "failed"
        summary["reason"] = failed[0].get("reason") or "memory_processing_failed"
        _write_publish_summary(workflow_id, stage_dir, summary)
        raise RuntimeError(f"MBIST RTL insertion failed: {summary['reason']}.")

    final_rtl_dir = os.path.join(stage_dir, "integrated_rtl")
    original_rtl_dir = os.path.join(final_rtl_dir, "functional_rtl")
    _ensure_dir(original_rtl_dir)
    integration_wrapper_items = _select_functional_wrapper_items(wrapper_items)
    skipped_nested_wrapper_items = [item for item in wrapper_items if item not in integration_wrapper_items]
    if not integration_wrapper_items:
        summary.update({
            "status": "failed",
            "reason": "mbist_insertion_requires_memory_instance",
            "integration_note": (
                "AutoMBIST standalone wrapper simulation passed for generated SRAM collateral, but at least one requested memory "
                "is not present as a replaceable functional RTL instance."
            ),
        })
        _write_publish_summary(workflow_id, stage_dir, summary)
        raise RuntimeError("MBIST RTL insertion failed: AutoMBIST passed, but no functional SRAM instance was found to wrap.")
    patched_sources = _replace_memory_instances_with_wrappers(integration_wrapper_items, original_rtl_dir)
    if len(patched_sources) != len({str(item["memory"].get("source_file")) for item in integration_wrapper_items}):
        summary.update({
            "status": "failed",
            "reason": "mbist_wrapper_parent_integration_failed",
            "integration_targets": integration_wrapper_items,
            "skipped_nested_integration_targets": skipped_nested_wrapper_items,
        })
        _write_publish_summary(workflow_id, stage_dir, summary)
        raise RuntimeError("MBIST RTL insertion failed: generated AutoMBIST wrapper could not be safely integrated into the functional RTL top.")
    wrapper_source_files = {
        os.path.abspath(str(item["memory"].get("source_file")))
        for item in wrapper_items
        if isinstance(item.get("memory"), dict) and item["memory"].get("source_file")
    }
    for src in rtl_files:
        if os.path.abspath(src) in wrapper_source_files:
            continue
        shutil.copy2(src, os.path.join(original_rtl_dir, os.path.basename(src)))

    copied_generated_rtl = []
    for src in generated_rtl_all:
        dst = os.path.join(final_rtl_dir, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        copied_generated_rtl.append(os.path.abspath(dst))
    final_files = sorted(dict.fromkeys([*copied_generated_rtl, *glob.glob(os.path.join(original_rtl_dir, "*.v")), *glob.glob(os.path.join(original_rtl_dir, "*.sv"))]))
    integrated_lint = _run_integrated_rtl_lint(state, workflow_id, stage_dir, final_files)
    if integrated_lint.get("status") != "pass":
        summary.update({
            "status": "failed",
            "reason": integrated_lint.get("reason") or "integrated_rtl_lint_failed",
            "integrated_rtl_lint": integrated_lint,
            "final_rtl_files": final_files,
        })
        _write_publish_summary(workflow_id, stage_dir, summary)
        raise RuntimeError(f"MBIST RTL insertion failed: {summary['reason']}.")
    integration_status = "wrapper_replaced_memory_instance"
    summary.update({
        "status": "mbist_rtl_generated_and_simulated",
        "simulation": {"status": "pass", "memory_count": len(memories)},
        "integration_status": integration_status,
        "integrated_rtl_lint": integrated_lint,
        "wrapper_modules": [item.get("wrapper_module") for item in integration_wrapper_items],
        "integration_targets": integration_wrapper_items,
        "skipped_nested_integration_targets": skipped_nested_wrapper_items,
        "patched_sources": patched_sources,
        "integrated_rtl_dir": final_rtl_dir,
        "final_rtl_files": final_files,
        "integration_note": (
            "AutoMBIST generated and simulated wrapper RTL for each detected OpenRAM/SRAM memory. The agent replaces detected instances when wrapper ports can be mapped; "
            "otherwise it fails later at synthesis rather than claiming a fake insertion."
        ),
    })

    _write_publish_summary(workflow_id, stage_dir, summary)
    report = "\n".join([
        "# MBIST RTL Insertion",
        "",
        f"- Status: `{summary['status']}`",
        f"- Memories processed: `{len(memories)}`",
        f"- AutoMBIST simulation: `pass`",
        f"- Integration status: `{integration_status}`",
        f"- Integrated RTL Icarus compile: `{integrated_lint.get('iverilog', {}).get('status')}`",
        f"- Integrated RTL Verilator lint: `{integrated_lint.get('verilator', {}).get('status')}`",
        "",
    ])
    _write_text(os.path.join(stage_dir, "mbist_rtl_insertion.md"), report)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", "mbist_rtl_insertion_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", "mbist_rtl_insertion.md", report)

    digital = state.setdefault("digital", {})
    digital["mbist_rtl_insertion"] = summary
    digital["rtl_files"] = final_files
    state["rtl_files"] = final_files
    state["mbist_rtl_inserted"] = True
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state
