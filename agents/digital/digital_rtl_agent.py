import os
import re
import json
import datetime
import logging
import shutil
import time
import glob
logger = logging.getLogger("chiploop")
from typing import Dict, List, Tuple, Optional
from pathlib import Path

from agents.runtime import RUNTIME_ACTIVE_STATE_KEY, AgentContext, execute_agent
from model_gateway import complete_text
from tooling.profiles import profile_summary
from tooling.runner import run_tool
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital RTL Agent"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")

def _stage(msg: str):
    """
    Lightweight stage logger (same pattern as spec agent)
    """
    try:
        logger.info(f"[RTL DEBUG] {msg}")
    except Exception:
        print(f"[RTL DEBUG] {msg}")

def _strip_verilog_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _safe_json(obj):
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return json.dumps(str(obj), indent=2)


def _is_empty_model_response_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "response was empty" in message or "empty response" in message


def _complete_rtl_text(prompt: str, *, agent_name: str, state: dict, stage_label: str) -> str:
    """Retry one provider-level empty response; design/tool failures are not retried here."""
    for attempt in (1, 2):
        try:
            output = complete_text(
                prompt,
                capability="rtl_generation",
                agent_name=agent_name,
                state=state,
            )
            if not str(output or "").strip():
                raise RuntimeError("Model response was empty")
            return output
        except Exception as exc:
            if attempt == 1 and _is_empty_model_response_error(exc):
                _stage(f"{stage_label}_empty_response_retry: 1")
                continue
            raise
    raise RuntimeError("RTL model response was empty after retry")


def _truncate_text(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return (
        text[:head]
        + f"\n\n...[truncated {len(text) - max_chars} chars for repair prompt]...\n\n"
        + text[-tail:]
    )


def _load_json_if_path(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
        with open(v, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _candidate_sram_roots(rtl_dir: str) -> List[str]:
    roots: List[str] = []
    for key in ("CHIPLOOP_PRECOMPILED_SRAM_ROOTS", "CHIPLOOP_SRAM_MACRO_ROOTS"):
        for item in re.split(r"[;:]", os.getenv(key, "")):
            item = item.strip()
            if item and os.path.isdir(item):
                roots.append(os.path.abspath(item))

    probe = os.path.abspath(rtl_dir)
    for _ in range(8):
        backend_pdk = os.path.join(probe, "backend", "pdk")
        direct_pdk = os.path.join(probe, "pdk")
        for pdk_root in (backend_pdk, direct_pdk):
            if os.path.isdir(pdk_root):
                for path in glob.glob(os.path.join(pdk_root, "*", "libs.ref", "*sram*")):
                    if os.path.isdir(path):
                        roots.append(os.path.abspath(path))
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return sorted(dict.fromkeys(roots))


def _memory_macro_cells(spec_json: dict) -> List[str]:
    cells: List[str] = []
    for macro in spec_json.get("memory_macros", []) or []:
        if not isinstance(macro, dict):
            continue
        kind = str(macro.get("kind") or macro.get("macro_kind") or "").lower()
        if not any(token in kind for token in ("sram", "openram", "prebuilt", "precompiled")):
            continue
        cell = str(macro.get("name") or macro.get("cell") or macro.get("openram_cell") or "").strip()
        if cell:
            cells.append(cell)
    return sorted(dict.fromkeys(cells))


def _validate_memory_macro_instances(spec_json: dict, verilog_map: Dict[str, str]) -> List[str]:
    """Enforce the authoritative macro cell/instance identity from the spec."""
    issues: List[str] = []
    text = _strip_verilog_comments("\n".join(verilog_map.values()))
    for macro in spec_json.get("memory_macros", []) or []:
        if not isinstance(macro, dict):
            continue
        cell = str(macro.get("name") or macro.get("cell") or macro.get("openram_cell") or "").strip()
        instance = str(macro.get("instance_name") or "").strip()
        if not cell:
            continue
        if instance:
            pattern = rf"\b{re.escape(cell)}\s+{re.escape(instance)}\s*\("
            count = len(re.findall(pattern, text))
            if count != 1:
                issues.append(
                    f"❌ Required memory macro instance mismatch: expected exactly one "
                    f"'{cell} {instance}(...)', found {count}. memory_macros[] is authoritative; "
                    "do not substitute a wrapper, behavioral model, or invented module name."
                )
        elif not re.search(rf"\b{re.escape(cell)}\s+[A-Za-z_][A-Za-z0-9_$]*\s*\(", text):
            issues.append(
                f"❌ Required memory macro cell '{cell}' is not instantiated. "
                "memory_macros[] is authoritative."
            )
    return issues


def _validate_memory_macro_reachability(spec_json: dict, verilog_map: Dict[str, str]) -> List[str]:
    """Reject required memories that synthesis can prove are functionally dead.

    This is intentionally a diagnostic gate, not an RTL rewrite. The model gets
    the concrete finding in the normal repair prompt and decides how the memory
    participates in the application behavior described by the specification.
    """
    issues: List[str] = []
    text = _strip_verilog_comments("\n".join(verilog_map.values()))

    def connected_ports(body: str) -> Dict[str, str]:
        return {
            match.group(1): match.group(2).strip()
            for match in re.finditer(
                r"\.([A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*([^()]*)\s*\)", body
            )
        }

    def constant_value(expression: str) -> Optional[int]:
        expression = str(expression or "").strip()
        match = re.fullmatch(r"(?:(\d+)'[bBdDhH])?([0-9a-fA-F_xXzZ]+)", expression)
        if not match or re.search(r"[xXzZ_]", match.group(2)):
            return None
        base = 10
        if "'" in expression:
            radix = expression.split("'", 1)[1][:1].lower()
            base = {"b": 2, "d": 10, "h": 16}.get(radix, 10)
        try:
            return int(match.group(2), base)
        except ValueError:
            return None

    def resolved_constant(expression: str) -> Optional[int]:
        direct = constant_value(expression)
        if direct is not None:
            return direct
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", expression):
            return None
        assignments = re.findall(
            rf"\bassign\s+{re.escape(expression)}\s*=\s*([^;]+);", text
        )
        if len(assignments) != 1:
            return None
        return constant_value(assignments[0])

    for macro in spec_json.get("memory_macros", []) or []:
        if not isinstance(macro, dict) or macro.get("unused") is True or macro.get("required") is False:
            continue
        cell = str(macro.get("name") or macro.get("cell") or macro.get("openram_cell") or "").strip()
        instance = str(macro.get("instance_name") or "").strip()
        if not cell:
            continue
        instance_name = re.escape(instance) if instance else r"[A-Za-z_][A-Za-z0-9_$]*"
        match = re.search(
            rf"\b{re.escape(cell)}\s+(?P<instance>{instance_name})\s*\((?P<body>.*?)\)\s*;",
            text,
            flags=re.DOTALL,
        )
        if not match:
            continue
        ports = macro.get("ports") if isinstance(macro.get("ports"), dict) else {}
        connections = connected_ports(match.group("body"))
        cs_port = str(ports.get("csb") or "csb")
        dout_port = str(ports.get("dout") or "dout")
        cs_expression = connections.get(cs_port, "")
        dout_expression = connections.get(dout_port, "")
        inactive_select = resolved_constant(cs_expression) == 1
        simple_dout = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", dout_expression or "")
        unconsumed_output = bool(
            simple_dout
            and (
                "unused" in dout_expression.lower()
                or len(re.findall(rf"\b{re.escape(dout_expression)}\b", text)) <= 2
            )
        )
        if inactive_select or unconsumed_output:
            reasons = []
            if inactive_select:
                reasons.append(f"active-low select '{cs_port}' is permanently inactive via '{cs_expression}'")
            if unconsumed_output:
                reasons.append(f"read output '{dout_port}' connects to unconsumed signal '{dout_expression}'")
            issues.append(
                f"❌ Required memory '{cell} {match.group('instance')}' is functionally unreachable: "
                + "; ".join(reasons)
                + ". Implement a legal input-driven read/write transaction and make read data observable; "
                "do not preserve the instance with constant tie-offs or an unused output."
            )
    return issues


def _align_memory_macro_instance_ports(verilog_map: Dict[str, str], spec_json: dict) -> Dict[str, str]:
    """Align macro instance bindings to the ports actually emitted by its module.

    ``memory_macros[].ports`` maps canonical roles to preferred concrete names,
    but generated technology-neutral wrappers may legally expose the canonical
    role names themselves.  The emitted module declaration is authoritative.
    """
    out = dict(verilog_map)
    declared_by_cell: Dict[str, set[str]] = {}
    for code in out.values():
        for module_name, module_code in _extract_verilog_modules(code).items():
            declared_by_cell[module_name] = set(_declared_ports(module_code))
    for macro in spec_json.get("memory_macros", []) or []:
        if not isinstance(macro, dict):
            continue
        cell = str(macro.get("name") or macro.get("cell") or "").strip()
        instance = str(macro.get("instance_name") or "").strip()
        ports = macro.get("ports") if isinstance(macro.get("ports"), dict) else {}
        if not cell or not instance or not ports:
            continue
        instance_pattern = re.compile(
            rf"(?P<head>\b{re.escape(cell)}\s+{re.escape(instance)}\s*\()(?P<body>.*?)(?P<tail>\)\s*;)",
            flags=re.DOTALL,
        )
        for filename, code in list(out.items()):
            def repair(match: re.Match) -> str:
                body = match.group("body")
                declared = declared_by_cell.get(cell)
                for role, concrete in ports.items():
                    role_name = str(role or "").strip()
                    concrete_name = str(concrete or "").strip()
                    if not role_name or not concrete_name:
                        continue
                    if declared:
                        if concrete_name in declared:
                            desired_name = concrete_name
                        elif role_name in declared:
                            desired_name = role_name
                        else:
                            continue
                    else:
                        desired_name = concrete_name
                    for alias in {role_name, concrete_name} - {desired_name}:
                        body = re.sub(
                            rf"\.{re.escape(alias)}\s*\(",
                            f".{desired_name}(",
                            body,
                        )
                return f"{match.group('head')}{body}{match.group('tail')}"
            out[filename] = instance_pattern.sub(repair, code)
    return out


def _stage_memory_macro_models_for_rtl_validation(spec_json: dict, rtl_dir: str, suffix: str = "") -> List[str]:
    staged: List[str] = []
    cells = _memory_macro_cells(spec_json)
    if not cells:
        return staged
    support_dir = os.path.join(rtl_dir, "_external_rtl_models" if not suffix else f"_external_rtl_models_{suffix}")
    os.makedirs(support_dir, exist_ok=True)

    for cell in cells:
        found = ""
        for root in _candidate_sram_roots(rtl_dir):
            for ext in (".v", ".sv"):
                candidate = os.path.join(root, "verilog", f"{cell}{ext}")
                if os.path.isfile(candidate):
                    found = candidate
                    break
                matches = glob.glob(os.path.join(root, "**", f"{cell}{ext}"), recursive=True)
                if matches:
                    found = matches[0]
                    break
            if found:
                break
        if not found:
            continue
        dst = os.path.join(support_dir, os.path.basename(found))
        shutil.copy2(found, dst)
        text = Path(dst).read_text(encoding="utf-8", errors="ignore")
        sanitized = text.replace("%m", "scope")
        if sanitized != text:
            Path(dst).write_text(sanitized, encoding="utf-8")
        staged.append(dst)
    return sorted(dict.fromkeys(staged))


def _is_fpga_bram_kind(value: object) -> bool:
    kind = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return kind in {"fpga_bram", "fpga_block_ram", "fpga_blockram"}


def _materialize_declared_fpga_bram_wrappers(spec_json: dict, materialize_dir: str) -> List[str]:
    """Materialize synthesizable inferred-RAM wrappers declared by the spec.

    ``fpga_bram`` is a technology-neutral RTL component, not an external PDK
    macro. Its implementation therefore has to travel with the generated RTL.
    The module identity, geometry, and port names all come from the contract.
    """
    generated: List[str] = []
    for macro in spec_json.get("memory_macros", []) or []:
        if not isinstance(macro, dict) or not _is_fpga_bram_kind(macro.get("kind") or macro.get("macro_kind")):
            continue
        name = str(macro.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            continue
        depth = int(macro.get("depth") or 0)
        data_width = int(macro.get("data_width") or 0)
        addr_width = int(macro.get("addr_width") or 0)
        if depth <= 0 or data_width <= 0 or addr_width <= 0 or (1 << addr_width) < depth:
            continue
        ports = macro.get("ports") if isinstance(macro.get("ports"), dict) else {}
        clk = str(ports.get("clk") or "clk")
        csb = str(ports.get("csb") or "csb")
        we = str(ports.get("we") or ports.get("web") or "we")
        addr = str(ports.get("addr") or "addr")
        din = str(ports.get("din") or "din")
        dout = str(ports.get("dout") or "dout")
        port_names = (clk, csb, we, addr, din, dout)
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", port) for port in port_names):
            continue
        code = f"""module {name} (
    input {clk},
    input {csb},
    input {we},
    input [{addr_width - 1}:0] {addr},
    input [{data_width - 1}:0] {din},
    output reg [{data_width - 1}:0] {dout}
);
    reg [{data_width - 1}:0] mem [0:{depth - 1}];
    always @(posedge {clk}) begin
        if (!{csb}) begin
            if ({we})
                mem[{addr}] <= {din};
            {dout} <= mem[{addr}];
        end
    end
endmodule
"""
        path = os.path.join(materialize_dir, f"{name}.v")
        Path(path).write_text(code, encoding="utf-8")
        generated.append(path)
    return sorted(dict.fromkeys(generated))


def _normalize_spec_json(spec_json: dict) -> Tuple[dict, str]:
    if not isinstance(spec_json, dict):
        raise ValueError("Spec JSON must be a dictionary.")

    if isinstance(spec_json.get("hierarchy"), dict):
        hier = spec_json["hierarchy"]
        top = hier.get("top_module")
        modules = hier.get("modules", [])

        if not isinstance(top, dict):
            raise ValueError("hierarchy.top_module must be an object.")
        if not top.get("name"):
            raise ValueError("hierarchy.top_module.name is required.")
        if not top.get("rtl_output_file"):
            raise ValueError("hierarchy.top_module.rtl_output_file is required.")
        if not isinstance(modules, list):
            raise ValueError("hierarchy.modules must be a list.")

        return {
            "design_name": spec_json.get("design_name") or top["name"],
            "design_summary": spec_json.get("design_summary", ""),
            "implementation_requirements": spec_json.get("implementation_requirements", []),
            "verification_requirements": spec_json.get("verification_requirements", []),
            "memory_macros": spec_json.get("memory_macros", []),
            "hierarchy": {
                "top_module": top,
                "modules": modules,
            },
            "operating_constraints": spec_json.get("operating_constraints", {}),
            "top_level_connections": spec_json.get("top_level_connections", []),
            "inter_module_signals": spec_json.get("inter_module_signals", []),
            "signal_ownership": spec_json.get("signal_ownership", []),
            "register_contract": spec_json.get("register_contract", {}),
        }, "hierarchical"

    if spec_json.get("name") and spec_json.get("rtl_output_file"):
        return {
            "name": spec_json["name"],
            "description": spec_json.get("description", ""),
            "design_summary": spec_json.get("design_summary", ""),
            "implementation_requirements": spec_json.get("implementation_requirements", []),
            "verification_requirements": spec_json.get("verification_requirements", []),
            "memory_macros": spec_json.get("memory_macros", []),
            "ports": spec_json.get("ports", []),
            "functionality": spec_json.get("functionality", ""),
            "responsibilities": spec_json.get("responsibilities", []),
            "must_drive": spec_json.get("must_drive", []),
            "must_receive": spec_json.get("must_receive", []),
            "must_not_drive": spec_json.get("must_not_drive", []),
            "reset_behavior": spec_json.get("reset_behavior", ""),
            "behavior_rules": spec_json.get("behavior_rules", []),
            "operating_constraints": spec_json.get("operating_constraints", {}),
            "rtl_output_file": spec_json["rtl_output_file"],
        }, "flat"

    raise ValueError("Spec JSON must be either flat or hierarchical.")


def _collect_expected_modules(spec_json: dict, mode: str) -> List[dict]:
    if mode == "flat":
        return [spec_json]
    return [spec_json["hierarchy"]["top_module"]] + list(spec_json["hierarchy"].get("modules", []))


def _collect_expected_rtl_files(spec_json: dict, mode: str) -> List[str]:
    return [m["rtl_output_file"] for m in _collect_expected_modules(spec_json, mode)]


def _top_module_name(spec_json: dict, mode: str) -> str:
    return spec_json["name"] if mode == "flat" else spec_json["hierarchy"]["top_module"]["name"]


def _top_rtl_file(spec_json: dict, mode: str) -> str:
    return spec_json["rtl_output_file"] if mode == "flat" else spec_json["hierarchy"]["top_module"]["rtl_output_file"]


def _module_by_name(spec_json: dict, mode: str) -> Dict[str, dict]:
    if mode != "hierarchical":
        return {}
    modules = [spec_json["hierarchy"]["top_module"]] + list(spec_json["hierarchy"].get("modules", []))
    return {
        str(module.get("name") or "").strip(): module
        for module in modules
        if isinstance(module, dict) and str(module.get("name") or "").strip()
    }


def _set_module_port_direction(module: dict, port_name: str, direction: str) -> None:
    if not isinstance(module, dict) or not port_name or direction not in {"input", "output", "inout"}:
        return
    for port in module.get("ports", []) or []:
        if isinstance(port, dict) and str(port.get("name") or "").strip() == port_name:
            port["direction"] = direction
            break

    if direction == "output":
        must_drive = list(module.get("must_drive") or [])
        if port_name not in must_drive:
            must_drive.append(port_name)
        module["must_drive"] = must_drive
        module["must_receive"] = [p for p in (module.get("must_receive") or []) if p != port_name]
        module["must_not_drive"] = [p for p in (module.get("must_not_drive") or []) if p != port_name]
    elif direction == "input":
        must_receive = list(module.get("must_receive") or [])
        if port_name not in must_receive:
            must_receive.append(port_name)
        module["must_receive"] = must_receive
        module["must_drive"] = [p for p in (module.get("must_drive") or []) if p != port_name]


def _reconcile_hierarchical_signal_directions(spec_json: dict, mode: str) -> dict:
    """
    Make the structured contract self-consistent before prompting RTL generation.
    Ownership/source endpoints are drivers; destination endpoints are consumers
    unless the same endpoint is explicitly a driver elsewhere.
    """
    if mode != "hierarchical":
        return spec_json

    modules = _module_by_name(spec_json, mode)
    top_name = _top_module_name(spec_json, mode)
    desired: Dict[Tuple[str, str], str] = {}

    def mark(endpoint: str, direction: str) -> None:
        try:
            module_name, port_name = _split_endpoint(str(endpoint or ""))
        except Exception:
            return
        if module_name in modules and module_name != top_name and port_name:
            key = (module_name, port_name)
            if direction == "output" or key not in desired:
                desired[key] = direction

    for sig in spec_json.get("inter_module_signals", []) or []:
        if not isinstance(sig, dict):
            continue
        mark(sig.get("source"), "output")
        for dst in sig.get("destinations", []) or []:
            mark(dst, "input")

    for owner in spec_json.get("signal_ownership", []) or []:
        if isinstance(owner, dict):
            mark(owner.get("owner"), "output")

    for (module_name, port_name), direction in desired.items():
        _set_module_port_direction(modules[module_name], port_name, direction)

    return spec_json


def _parse_named_verilog_blocks(llm_output: str) -> Dict[str, str]:
    blocks = re.findall(
        r"---BEGIN\s+([A-Za-z_][\w\-]*\.s?vh?)---(.*?)---END\s+\1---",
        llm_output,
        re.DOTALL,
    )
    return {fname.strip(): code.strip() for fname, code in blocks}


def _merge_rtl_repair_output(previous_output: str, repair_output: str, expected_files: List[str]) -> str:
    """Overlay repaired named files while retaining unchanged hierarchy files."""
    previous = _normalize_emitted_rtl_filenames(_parse_named_verilog_blocks(previous_output), expected_files)
    repaired = _normalize_emitted_rtl_filenames(_parse_named_verilog_blocks(repair_output), expected_files)
    merged = {name: previous[name] for name in expected_files if name in previous}
    for name, code in repaired.items():
        if name in expected_files:
            merged[name] = code
    if not merged:
        return repair_output
    return "\n".join(
        f"---BEGIN {name}---\n{merged[name].strip()}\n---END {name}---"
        for name in expected_files
        if name in merged
    )


def _normalize_emitted_rtl_filenames(verilog_map: Dict[str, str], expected_files: List[str]) -> Dict[str, str]:
    normalized = dict(verilog_map)
    by_stem = {os.path.splitext(name)[0]: name for name in verilog_map}
    for expected in expected_files:
        if expected in normalized:
            continue
        stem = os.path.splitext(expected)[0]
        candidate = by_stem.get(stem)
        if candidate and os.path.splitext(candidate)[1] in {".v", ".sv"} and os.path.splitext(expected)[1] in {".v", ".sv"}:
            normalized[expected] = normalized[candidate]
            if candidate != expected:
                normalized.pop(candidate, None)

    # Some models emit the complete hierarchy inside the top file's named
    # block. Canonicalize that valid multi-module payload before a targeted
    # repair is merged. Otherwise replacing only the top block also discards
    # every child module that happened to share its original container.
    # Matching an expected filename stem to an actual module declaration is
    # deterministic and does not synthesize or guess any RTL.
    modules_by_name: Dict[str, str] = {}
    for code in verilog_map.values():
        modules_by_name.update(_extract_verilog_modules(code))
    for expected in expected_files:
        if expected in normalized:
            expected_stem = os.path.splitext(expected)[0]
            expected_code = normalized[expected]
            expected_modules = _extract_verilog_modules(expected_code)
            # A canonical file already containing only its expected module
            # must remain byte-for-byte intact, including local helper text.
            if set(expected_modules) == {expected_stem}:
                continue
        else:
            expected_stem = os.path.splitext(expected)[0]
        if expected_stem in modules_by_name:
            normalized[expected] = modules_by_name[expected_stem]
    return normalized


def _extract_verilog_modules(code: str) -> Dict[str, str]:
    modules: Dict[str, str] = {}
    text = _strip_verilog_comments(code or "")
    for match in re.finditer(
        r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b.*?\bendmodule\b",
        text,
        flags=re.DOTALL,
    ):
        modules[match.group(1)] = match.group(0).strip()
    return modules


def _module_code_for_name(code: str, module_name: str) -> str:
    modules = _extract_verilog_modules(code)
    return modules.get(module_name, code or "")


def _module_code_with_local_dependencies(code: str, module_name: str, excluded_modules: Optional[set[str]] = None) -> str:
    modules = _extract_verilog_modules(code)
    if module_name not in modules:
        return code or ""
    selected: List[str] = []
    seen: set[str] = set()
    excluded = set(excluded_modules or set()) - {module_name}

    def visit(name: str) -> None:
        if name in seen or name not in modules or name in excluded:
            return
        seen.add(name)
        body = modules[name]
        for candidate in modules:
            if candidate == name or candidate in excluded:
                continue
            if re.search(rf"\b{re.escape(candidate)}\s*(?:#\s*\([^;]*?\)\s*)?[A-Za-z_][A-Za-z0-9_$]*\s*\(", body):
                visit(candidate)
        selected.append(body)

    visit(module_name)
    return "\n\n".join(selected)


def _canonical_memory_port_roles(port_names: List[str]) -> Dict[str, str]:
    available = {name.lower(): name for name in port_names}
    roles: Dict[str, str] = {}

    def pick(role: str, candidates: tuple[str, ...]) -> None:
        for candidate in candidates:
            if candidate.lower() in available:
                roles[role] = available[candidate.lower()]
                return

    pick("clk", ("clk", "clock"))
    pick("csb", ("csb", "ceb", "cs_n", "cen"))
    pick("we", ("web", "we", "wen", "write_enable"))
    pick("addr", ("addr", "address"))
    pick("din", ("din", "data_in", "wdata", "d"))
    pick("dout", ("dout", "data_out", "rdata", "q"))
    return roles


def _ports_from_module_spec(module: dict) -> Dict[str, dict]:
    ports: Dict[str, dict] = {}
    for port in module.get("ports", []) or []:
        if not isinstance(port, dict) or not port.get("name"):
            continue
        ports[str(port["name"])] = {
            "direction": str(port.get("direction") or "input").lower(),
            "width": int(port.get("width") or 1),
        }
    return ports


def _module_decl_from_spec(module_name: str, ports: Dict[str, dict]) -> str:
    names = list(ports)
    lines = [f"module {module_name} (", "    " + ",\n    ".join(names), ");"]
    for name, info in ports.items():
        direction = info.get("direction") or "input"
        width = int(info.get("width") or 1)
        rng = f" [{width - 1}:0]" if width > 1 else ""
        lines.append(f"{direction}{rng} {name};")
    return "\n".join(lines)


def _build_memory_adapter_module(module: dict, helper_name: str, helper_code: str) -> str | None:
    module_name = str(module.get("name") or "").strip()
    ports = _ports_from_module_spec(module)
    if not module_name or not ports:
        return None
    target_roles = _canonical_memory_port_roles(list(ports))
    helper_ports = _declared_ports(helper_code)
    helper_roles = _canonical_memory_port_roles(list(helper_ports))
    required = {"clk", "addr", "din", "dout"}
    if not required.issubset(target_roles) or not required.issubset(helper_roles):
        return None

    lines = [_module_decl_from_spec(module_name, ports), ""]
    conns: List[str] = []
    for helper_role, helper_port in helper_roles.items():
        target_port = target_roles.get(helper_role)
        if not target_port and helper_role == "we":
            target_port = target_roles.get("we")
        if not target_port and helper_role == "csb":
            target_port = target_roles.get("csb")
        if target_port:
            conns.append(f"    .{helper_port}({target_port})")
    if not conns:
        return None
    lines.append(f"{helper_name} u_backing_macro (")
    lines.append(",\n".join(conns))
    lines.append(");")
    lines.append("")
    lines.append("endmodule")
    return "\n".join(lines)


def _fill_missing_expected_memory_modules(
    verilog_map: Dict[str, str],
    spec_json: dict,
    mode: str,
    helper_source_map: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    expected_modules = _collect_expected_modules(spec_json, mode)
    expected_names = {str(module.get("name") or "").strip() for module in expected_modules}
    helper_modules: Dict[str, str] = {}
    for code in (helper_source_map or verilog_map).values():
        for name, body in _extract_verilog_modules(code).items():
            if name not in expected_names:
                helper_modules[name] = body
    if not helper_modules:
        return verilog_map

    out = dict(verilog_map)
    for module in expected_modules:
        rtl_file = str(module.get("rtl_output_file") or "").strip()
        module_name = str(module.get("name") or "").strip()
        if not rtl_file or rtl_file in out:
            continue
        name_hint = module_name.lower()
        if not any(token in name_hint for token in ("sram", "memory", "mem", "openram", "wrapper", "model")):
            continue
        for helper_name, helper_code in helper_modules.items():
            adapter = _build_memory_adapter_module(module, helper_name, helper_code)
            if adapter:
                out[rtl_file] = adapter
                break
    return out


def _range_decl(width: int) -> str:
    return f"[{width - 1}:0] " if int(width or 1) > 1 else ""


def _repair_decl_width(code: str, kind_pattern: str, name: str, width: int) -> str:
    if int(width or 1) <= 1:
        return code
    rng = _range_decl(width)
    name_re = re.escape(name)

    def repl(match: re.Match) -> str:
        prefix = match.group("prefix")
        sep = match.group("sep")
        return f"{prefix}{rng}{name}{sep}"

    pattern = re.compile(
        rf"(?P<prefix>\b(?:{kind_pattern})\b\s+(?:(?:wire|reg|logic|signed)\s+)*)(?:\[[^\]]+\]\s*)?{name_re}(?P<sep>\s*[,);])",
        flags=re.IGNORECASE,
    )
    code = pattern.sub(repl, code)

    line_pattern = re.compile(
        rf"^(?P<prefix>\s*(?:{kind_pattern})\b\s+(?:(?:wire|reg|logic|signed)\s+)*)(?:\[[^\]]+\]\s*)?{name_re}(?P<sep>\s*;)\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return line_pattern.sub(repl, code)


def _repair_decl_direction(code: str, name: str, direction: str) -> str:
    if direction not in {"input", "output", "inout"} or not name:
        return code
    name_re = re.escape(name)

    def repl(match: re.Match) -> str:
        rest = match.group("rest")
        if direction in {"input", "inout"}:
            rest = re.sub(r"\b(?:reg|logic)\b\s*", "", rest, count=1, flags=re.IGNORECASE)
        return f"{match.group('prefix')}{direction}{rest}"

    header_pattern = re.compile(
        rf"(?P<prefix>(?:^|[,(])\s*)(?:input|output|inout)(?P<rest>\b(?:(?![(),;]).)*?\b{name_re}\b)",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    code = header_pattern.sub(repl, code)

    line_pattern = re.compile(
        rf"^(?P<prefix>\s*)(?:input|output|inout)(?P<rest>\b(?:(?!;).)*?\b{name_re}\b(?:(?!;).)*?;\s*)$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return line_pattern.sub(repl, code)


def _repair_directional_port_aliases_from_spec(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """Map conventional ``name``/``name_in``/``name_out`` aliases to the contract."""
    out = dict(verilog_map)
    for module in _collect_expected_modules(spec_json, mode):
        rtl_file = str(module.get("rtl_output_file") or "").strip()
        module_name = str(module.get("name") or "").strip()
        if not rtl_file or not module_name or rtl_file not in out:
            continue
        expected = _ports_from_module_spec(module)
        module_code = _module_code_for_name(out[rtl_file], module_name)
        actual = _declared_ports(module_code)
        renames: Dict[str, str] = {}

        # Resolve input aliases first so a base-name output can subsequently
        # claim the emitted ``*_out`` port without ambiguity.
        for expected_name, expected_info in expected.items():
            if expected_name in actual or str(expected_info.get("direction")) != "input" or not expected_name.endswith("_in"):
                continue
            base = expected_name[:-3]
            if (actual.get(base) or {}).get("direction") == "input":
                renames[base] = expected_name
                actual[expected_name] = actual.pop(base)
        for expected_name, expected_info in expected.items():
            if expected_name in actual or str(expected_info.get("direction")) != "output":
                continue
            candidate = f"{expected_name}_out"
            if (actual.get(candidate) or {}).get("direction") == "output":
                renames[candidate] = expected_name
                actual[expected_name] = actual.pop(candidate)
        if not renames:
            continue

        repaired_module = module_code
        for old_name, new_name in renames.items():
            repaired_module = re.sub(rf"\b{re.escape(old_name)}\b", new_name, repaired_module)
        out[rtl_file] = out[rtl_file].replace(module_code, repaired_module, 1)

        for fname, code in list(out.items()):
            if fname == rtl_file:
                continue
            for old_name, new_name in renames.items():
                code = re.sub(
                    rf"(\b{re.escape(module_name)}\s+[A-Za-z_][A-Za-z0-9_$]*\s*\(.*?)\.{re.escape(old_name)}(\s*\()",
                    rf"\1.{new_name}\2",
                    code,
                    flags=re.DOTALL,
                )
            out[fname] = code
    return out


def _remove_writes_to_spec_input_ports(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """Remove emitted drivers for ports whose authoritative contract is input."""
    out = dict(verilog_map)
    for module in _collect_expected_modules(spec_json, mode):
        rtl_file = str(module.get("rtl_output_file") or "").strip()
        if not rtl_file or rtl_file not in out:
            continue
        code = out[rtl_file]
        for name, info in _ports_from_module_spec(module).items():
            if str(info.get("direction") or "") != "input":
                continue
            code = re.sub(rf"^\s*assign\s+{re.escape(name)}(?:\s*\[[^\]]+\])?\s*=.*?;\s*$", "", code, flags=re.MULTILINE)
            # Match only a procedural assignment statement whose LHS begins
            # the line (optionally after a one-line if).  A broad ``name <=``
            # search corrupts ordinary comparisons such as
            # ``if (input_name <= limit)``.
            statement = re.compile(
                rf"^(?P<indent>\s*)(?P<guard>if\s*\([^;\n]*\)\s*)?"
                rf"{re.escape(name)}(?:\s*\[[^\]]+\])?\s*(?:<=|=(?!=))\s*[^;]+;\s*$",
                flags=re.MULTILINE,
            )
            code = statement.sub(lambda match: f"{match.group('indent')}{match.group('guard') or ''};", code)
        out[rtl_file] = code
    return out


def _repair_module_port_widths_from_spec(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    out = dict(verilog_map)
    for module in _collect_expected_modules(spec_json, mode):
        rtl_file = str(module.get("rtl_output_file") or "").strip()
        if not rtl_file or rtl_file not in out:
            continue
        code = out[rtl_file]
        for pname, info in _ports_from_module_spec(module).items():
            width = int(info.get("width") or 1)
            if width <= 1:
                continue
            direction = str(info.get("direction") or "input").lower()
            code = _repair_decl_width(code, direction, pname, width)

            for alias in re.findall(rf"\bassign\s+{re.escape(pname)}\s*=\s*([A-Za-z_][A-Za-z0-9_$]*)\s*;", code):
                code = _repair_decl_width(code, r"wire|reg|logic", alias, width)
            for alias in re.findall(rf"\bassign\s+([A-Za-z_][A-Za-z0-9_$]*)\s*=\s*{re.escape(pname)}\s*;", code):
                code = _repair_decl_width(code, r"wire|reg|logic", alias, width)
        out[rtl_file] = code
    return out


def _repair_module_port_directions_from_spec(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    out = dict(verilog_map)
    for module in _collect_expected_modules(spec_json, mode):
        rtl_file = str(module.get("rtl_output_file") or "").strip()
        module_name = str(module.get("name") or "").strip()
        if not rtl_file or not module_name or rtl_file not in out:
            continue
        code = out[rtl_file]
        module_code = _module_code_for_name(code, module_name)
        if not module_code:
            continue
        repaired = module_code
        for pname, info in _ports_from_module_spec(module).items():
            repaired = _repair_decl_direction(repaired, pname, str(info.get("direction") or "input").lower())
        if repaired != module_code:
            code = code.replace(module_code, repaired, 1)
        out[rtl_file] = code
    return out


def _has_structural_width_warnings(tool_output: str) -> bool:
    text = tool_output or ""
    patterns = (
        r"warning:\s+Port\s+\d+\s+\([^)]+\)\s+of\s+\S+\s+expects\s+\d+\s+bits,\s+got\s+\d+",
        r"\bPadding\s+\d+\s+high bits\b",
        r"\bPruning\s+\d+\s+high bits\b",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _module_procedurally_assigns_signal(module_code: str, signal_name: str) -> bool:
    text = _strip_verilog_comments(module_code or "")
    assignment = rf"\b{re.escape(signal_name)}\s*(?:<=|(?<![=!<>])=(?!=))"
    # A one-statement always block must not absorb following continuous
    # assignments into its body during this structural check.
    single_statement = re.compile(
        rf"\balways(?:_ff|_comb)?\s*(?:@\s*\([^)]*\))?\s*(?!begin\b)[^;]*{assignment}[^;]*;",
        flags=re.IGNORECASE,
    )
    if single_statement.search(text):
        return True
    for block in re.findall(
        r"\balways(?:_ff|_comb)?\s*(?:@\s*\([^)]*\))?\s*begin\b(.*?)\bend\b",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        if re.search(assignment, block):
            return True
    return False


def _promote_procedurally_assigned_outputs(verilog_map: Dict[str, str]) -> Dict[str, str]:
    """Make Verilog output declarations legal when an always block drives them."""
    out = dict(verilog_map)
    for filename, code in list(out.items()):
        repaired = code
        for module_name, module_code in _extract_verilog_modules(code).items():
            updated_module = module_code
            declared = _declared_ports(module_code)
            output_names = {
                name for name, info in declared.items() if info.get("direction") == "output"
            }
            # _declared_ports primarily serves ANSI contracts; include classic
            # Verilog declarations used by generated hierarchical leaf files.
            output_names.update(re.findall(
                r"\boutput\s+(?:(?:wire|reg|logic|signed)\s+)*(?:\[[^\]]+\]\s*)?([A-Za-z_][A-Za-z0-9_$]*)",
                module_code,
                flags=re.IGNORECASE,
            ))
            for signal_name in output_names:
                if not _module_procedurally_assigns_signal(module_code, signal_name):
                    continue
                # Verilog-2005 requires a procedural output to be a variable.
                # Preserve its signedness/range and leave output logic/reg alone.
                declaration = re.compile(
                    rf"(?P<prefix>\boutput\s+)(?!(?:reg|logic)\b)(?:wire\s+)?"
                    rf"(?P<shape>(?:signed\s+)?(?:\[[^\]]+\]\s*)?)(?P<name>{re.escape(signal_name)}\b)",
                    flags=re.IGNORECASE,
                )
                updated_module = declaration.sub(
                    lambda match: f"{match.group('prefix')}reg {match.group('shape')}{match.group('name')}",
                    updated_module,
                    count=1,
                )
            if updated_module != module_code:
                repaired = repaired.replace(module_code, updated_module, 1)
        out[filename] = repaired
    return out


def _align_verilog_map_to_expected_modules(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    module_to_file = {
        str(module.get("name") or "").strip(): str(module.get("rtl_output_file") or "").strip()
        for module in _collect_expected_modules(spec_json, mode)
        if module.get("name") and module.get("rtl_output_file")
    }
    if not module_to_file:
        return verilog_map

    expected_files = set(module_to_file.values())
    aligned = dict(verilog_map)
    extracted_by_module: Dict[str, str] = {}
    expected_module_names = set(module_to_file)
    for code in verilog_map.values():
        extracted_by_module.update(_extract_verilog_modules(code))

    for module_name, rtl_file in module_to_file.items():
        module_code = None
        for code in verilog_map.values():
            if module_name in _extract_verilog_modules(code):
                module_code = _module_code_with_local_dependencies(
                    code,
                    module_name,
                    excluded_modules=expected_module_names,
                )
                break
        if module_code:
            aligned[rtl_file] = module_code

    aligned = {fname: code for fname, code in aligned.items() if fname in expected_files}
    aligned = _fill_missing_expected_memory_modules(aligned, spec_json, mode, helper_source_map=verilog_map)
    return _repair_module_port_widths_from_spec(aligned, spec_json, mode)


def _remove_comb_blocking_assigns_to_sequential_regs(code: str) -> str:
    # Nonblocking assignments are statements whose LHS starts the statement.
    # A global ``name <=`` search also matches ordinary relational comparisons
    # such as ``if (age <= timeout)``. That false positive previously caused
    # this sanitizer to delete the real combinational assignments to ``age``,
    # leaving an empty/invalid if statement in materialized RTL.
    nonblocking_statement = re.compile(
        r"^\s*(?:(?:else\s+)?if\s*\([^;]+\)\s*|else\s+)?"
        r"([A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)\s*<=\s*[^=]",
        flags=re.MULTILINE,
    )
    seq_targets = {
        re.sub(r"\[[^\]]+\]", "", name).strip()
        for name in nonblocking_statement.findall(code or "")
    }
    seq_targets.discard("")
    if not seq_targets:
        return code

    out: List[str] = []
    in_comb = False
    depth = 0
    assign_pat = re.compile(
        # A combinational assignment may be the statement owned by a case
        # item (for example ``8'h00: rd_data_reg = control_reg;``). Treat the
        # case label as syntax surrounding the assignment, not as part of
        # the signal name, so BLKANDNBLK cleanup covers readback muxes too.
        rf"^\s*(?:(?:default|[^:;]+)\s*:\s*)?(?:if\s*\([^;]+\)\s*)?"
        rf"({'|'.join(re.escape(name) for name in sorted(seq_targets))})"
        rf"(?:\s*\[[^\]]+\])?\s*=\s*[^=].*;\s*$"
    )

    for line in (code or "").splitlines():
        starts_comb = bool(re.search(r"\balways\s*@\s*\(\s*\*\s*\)", line))
        if starts_comb and not in_comb:
            in_comb = True
            depth = 0

        skip_line = in_comb and bool(assign_pat.match(line))
        if not skip_line:
            out.append(line)

        if in_comb:
            depth += len(re.findall(r"\bbegin\b", line))
            depth -= len(re.findall(r"\bend\b", line))
            if depth <= 0 and not starts_comb:
                in_comb = False

    return "\n".join(out).rstrip()


def _remove_reset_only_seq_assigns_for_comb_targets(code: str) -> str:
    """
    Verilator BLKANDNBLK is common when generated RTL models a combinational
    readback mux as a reg but also resets the same reg in the clocked block.
    If the only nonblocking assignments to that target are reset-zero style,
    keep the combinational mux and remove the redundant clocked reset writes.
    """
    text = code or ""
    comb_targets = set()
    in_comb = False
    depth = 0
    for line in text.splitlines():
        starts_comb = bool(re.search(r"\balways\s*@\s*\(\s*\*\s*\)", line))
        if starts_comb and not in_comb:
            in_comb = True
            depth = 0

        if in_comb:
            for lhs in re.findall(
                r"(?:^|:\s*)\s*([A-Za-z_][A-Za-z0-9_$]*)(?:\s*\[[^\]]+\])?\s*=\s*[^=]",
                line,
            ):
                comb_targets.add(lhs)

            depth += len(re.findall(r"\bbegin\b", line))
            depth -= len(re.findall(r"\bend\b", line))
            if depth <= 0 and not starts_comb:
                in_comb = False

    if not comb_targets:
        return code

    nb_by_target: Dict[str, List[str]] = {}
    nb_pat = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_$]*)(?:\s*\[[^\]]+\])?\s*<=\s*(.+?)\s*;\s*$"
    )
    for line in text.splitlines():
        m = nb_pat.match(line)
        if m and m.group(1) in comb_targets:
            nb_by_target.setdefault(m.group(1), []).append(m.group(2).strip())

    reset_only_targets = {
        target
        for target, rhs_values in nb_by_target.items()
        if rhs_values and all(re.fullmatch(r"(?:\d+'[bdh])?0+", rhs.replace("_", ""), re.I) for rhs in rhs_values)
    }
    if not reset_only_targets:
        return code

    out = []
    for line in text.splitlines():
        m = nb_pat.match(line)
        if m and m.group(1) in reset_only_targets:
            continue
        out.append(line)
    return "\n".join(out).rstrip()


def _flatten_constant_part_select_bit_selects(code: str) -> str:
    """Rewrite ``signal[msb:lsb][bit]`` into a Verilog-2005 bit select.

    Generated RTL occasionally uses a SystemVerilog-style chained select that
    is not accepted consistently by Icarus. Constant bounds make the exact
    equivalent deterministic: ``value[127:96][0]`` becomes ``value[96]``.
    """
    pattern = re.compile(
        r"\b(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*"
        r"\[\s*(?P<msb>\d+)\s*:\s*(?P<lsb>\d+)\s*\]\s*"
        r"\[\s*(?P<bit>\d+)\s*\]"
    )

    def replace(match: re.Match) -> str:
        msb = int(match.group("msb"))
        lsb = int(match.group("lsb"))
        bit = int(match.group("bit"))
        width = abs(msb - lsb) + 1
        if bit >= width:
            return match.group(0)
        absolute = lsb + bit if msb >= lsb else lsb - bit
        return f"{match.group('name')}[{absolute}]"

    return pattern.sub(replace, code or "")


def _repair_empty_case_statements(code: str) -> str:
    """Keep sanitizer output syntactically valid when all case items vanish."""
    pattern = re.compile(
        r"(?P<header>\bcase[xz]?\s*\([^\n]*\)\s*\n)"
        r"(?P<indent>[ \t]*)"
        r"(?P<end>endcase\b)",
        flags=re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: (
            f"{match.group('header')}{match.group('indent')}    default: ;\n"
            f"{match.group('indent')}{match.group('end')}"
        ),
        code or "",
    )


def _convert_procedural_wire_declarations(code: str) -> str:
    text = code or ""
    for _module_name, module_code in _extract_verilog_modules(text).items():
        replacements: Dict[str, str] = {}
        for decl in re.finditer(
            r"^(?P<indent>\s*)wire\b(?P<body>\s*(?:signed\s*)?(?:\[[^\]]+\]\s*)?)(?P<names>[^;]+);",
            module_code,
            flags=re.MULTILINE,
        ):
            raw_items = [re.sub(r"=.*", "", item).strip() for item in decl.group("names").split(",")]
            names = [
                re.sub(r"\[[^\]]+\]", "", item).strip()
                for item in raw_items
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?", item or "")
            ]
            if names and all(_module_procedurally_assigns_signal(module_code, name) for name in names):
                replacements[decl.group(0)] = f"{decl.group('indent')}reg{decl.group('body')}{decl.group('names')};"
        if not replacements:
            continue
        updated_module = module_code
        for old, new in replacements.items():
            updated_module = updated_module.replace(old, new, 1)
        text = text.replace(module_code, updated_module, 1)
    return text


def _sanitize_single_driver_rtl(verilog_map: Dict[str, str]) -> Dict[str, str]:
    return {
        fname: _repair_empty_case_statements(
            _flatten_constant_part_select_bit_selects(
                _convert_procedural_wire_declarations(
                    _remove_comb_blocking_assigns_to_sequential_regs(
                        # Reset-only sequential writes are redundant when the
                        # signal is owned by a complete combinational mux. Remove
                        # those first; otherwise the next pass deletes every case
                        # item and leaves a syntactically empty case statement.
                        _remove_reset_only_seq_assigns_for_comb_targets(code)
                    )
                )
            )
        )
        for fname, code in verilog_map.items()
    }


def _remove_module_header_port(code: str, port_name: str) -> str:
    pat = re.compile(
        r"(\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*\()(.*?)(\)\s*;)",
        flags=re.DOTALL,
    )

    def repl(match: re.Match) -> str:
        ports = [p.strip() for p in match.group(2).split(",") if p.strip()]
        kept = [p for p in ports if p != port_name]
        if len(kept) == len(ports):
            return match.group(0)
        return match.group(1) + "\n    " + ",\n    ".join(kept) + "\n" + match.group(3)

    return pat.sub(repl, code, count=1)


def _declared_input_ports(code: str) -> List[str]:
    ports: List[str] = []
    for decl in re.finditer(
        r"^\s*input\b\s*(?:wire\s*)?(?:signed\s*)?(?:\[[^\]]+\]\s*)?([^;]+);",
        code or "",
        flags=re.MULTILINE,
    ):
        for raw in decl.group(1).split(","):
            name = re.sub(r"\[[^\]]+\]", "", raw).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name or ""):
                ports.append(name)
    return list(dict.fromkeys(ports))


def _declared_ports(code: str) -> Dict[str, dict]:
    ports: Dict[str, dict] = {}
    header = re.search(
        r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*(?:#\s*\([^;]*?\)\s*)?\((?P<ports>.*?)\)\s*;",
        code or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    if header:
        current_direction = ""
        current_range = ""
        for raw in header.group("ports").split(","):
            item = raw.strip()
            direction = re.search(r"\b(input|output|inout)\b", item, flags=re.IGNORECASE)
            if direction:
                current_direction = direction.group(1).lower()
            range_match = re.search(r"\[[^\]]+\]", item)
            if range_match:
                current_range = range_match.group(0)
            elif direction:
                current_range = ""
            item = re.sub(r"\b(input|output|inout|wire|reg|logic|signed)\b", " ", item, flags=re.IGNORECASE)
            item = re.sub(r"\[[^\]]+\]", " ", item)
            names = re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", item)
            if names and current_direction:
                ports[names[-1]] = {"direction": current_direction, "width": _range_width(current_range)}
    for decl in re.finditer(
        r"^\s*(input|output|inout)\b\s*(?:wire\s*|reg\s*|logic\s*)?(?:signed\s*)?(?P<range>\[[^\]]+\]\s*)?(?P<names>[^;]+);",
        code or "",
        flags=re.MULTILINE,
    ):
        direction = decl.group(1)
        width = _range_width(decl.group("range"))
        for raw in decl.group("names").split(","):
            name = re.sub(r"=.*", "", raw)
            name = re.sub(r"\[[^\]]+\]", "", name).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name or ""):
                ports[name] = {"direction": direction, "width": width}
    return ports


def _declared_signal_widths(code: str) -> Dict[str, int]:
    widths: Dict[str, int] = {name: int(info.get("width") or 1) for name, info in _declared_ports(code).items()}
    for decl in re.finditer(
        r"^\s*(?:input|output|inout|wire|reg|logic)\b\s*(?:wire\s*|reg\s*|logic\s*)?(?:signed\s*)?(?P<range>\[[^\]]+\]\s*)?(?P<names>[^;]+);",
        code or "",
        flags=re.MULTILINE,
    ):
        width = _range_width(decl.group("range"))
        for raw in decl.group("names").split(","):
            name = re.sub(r"=.*", "", raw)
            name = re.sub(r"\[[^\]]+\]", "", name).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name or ""):
                widths[name] = width
    return widths


def _wire_decl(name: str, width: int) -> str:
    return f"wire [{width - 1}:0] {name};" if width > 1 else f"wire {name};"


def _replace_or_insert_wire_decl(code: str, name: str, width: int) -> str:
    decl = _wire_decl(name, width)
    wire_pat = re.compile(
        rf"^\s*wire\b\s*(?:signed\s*)?(?:\[[^\]]+\]\s*)?{re.escape(name)}\s*;\s*$",
        flags=re.MULTILINE,
    )
    if wire_pat.search(code):
        return wire_pat.sub(decl, code, count=1)
    declared_pat = re.compile(
        rf"^\s*(?:input|output|inout|reg|logic)\b[^;]*\b{re.escape(name)}\b[^;]*;\s*$",
        flags=re.MULTILINE,
    )
    if declared_pat.search(code):
        return code
    insert_at = 0
    header = re.search(r"\)\s*;", code)
    if header:
        insert_at = header.end()
        for match in re.finditer(
            r"^\s*(?:input|output|inout|wire|reg|logic)\b[^;]*;\s*$",
            code,
            flags=re.MULTILINE,
        ):
            if match.start() >= header.end():
                insert_at = match.end()
    return code[:insert_at] + "\n" + decl + code[insert_at:]


def _named_instance_connections(conn_text: str) -> Dict[str, str]:
    conns: Dict[str, str] = {}
    for match in re.finditer(r"\.(?P<port>[A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*(?P<sig>[^()]+?)\s*\)", conn_text):
        sig = match.group("sig").strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?", sig):
            conns[match.group("port")] = sig
    return conns


def _sanitize_child_output_instance_connections(verilog_map: Dict[str, str]) -> Dict[str, str]:
    module_defs: Dict[str, dict] = {}
    for code in verilog_map.values():
        for match in re.finditer(
            r"\bmodule\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\b(?P<body>.*?)(?=\bendmodule\b)",
            code or "",
            flags=re.DOTALL,
        ):
            module_defs[match.group("name")] = {
                "ports": _declared_ports(match.group(0)),
            }

    if not module_defs:
        return verilog_map

    out: Dict[str, str] = {}
    child_names = sorted((re.escape(name) for name in module_defs), key=len, reverse=True)
    inst_re = re.compile(
        rf"\b(?P<cell>{'|'.join(child_names)})\s*(?:#\s*\([^;]*?\)\s*)?"
        r"(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<conns>.*?)\)\s*;",
        flags=re.DOTALL,
    )

    def _driver_score(sig: str, cell: str, inst: str, port: str) -> tuple[int, int]:
        tokens = {
            tok
            for tok in re.split(r"[^a-zA-Z0-9]+", sig.lower())
            if len(tok) >= 3 and tok not in {"out", "output", "sig", "wire", "reg"}
        }
        haystack = f"{cell} {inst} {port}".lower()
        token_hits = sum(1 for tok in tokens if tok in haystack)
        non_generic = 0 if re.search(r"(mmio|reg|register)", f"{cell} {inst}", re.I) else 1
        return token_hits, non_generic

    for fname, code in verilog_map.items():
        text = code or ""
        parent_ports = _declared_ports(text)
        signal_widths = _declared_signal_widths(text)
        wire_updates: Dict[str, int] = {}
        duplicate_output_drivers: Dict[tuple[str, str, str], str] = {}
        drivers_by_sig: Dict[str, list[dict]] = {}

        def has_parent_driver(sig: str, instance_start: int, instance_end: int) -> bool:
            # A child output is already a structural driver. If the parent also
            # assigns the connected net, Verilator reports BLKANDNBLK or
            # MULTIDRIVEN. Keep the explicit parent data path and move the
            # redundant child output to an isolated observation wire.
            module_start = text.rfind("module", 0, instance_start)
            module_end = text.find("endmodule", instance_end)
            parent_text = text[module_start : module_end if module_end >= 0 else len(text)]
            escaped = re.escape(sig)
            if re.search(rf"^\s*assign\s+{escaped}(?:\s*\[[^\]]+\])?\s*=", parent_text, flags=re.MULTILINE):
                return True
            if re.search(rf"^\s*{escaped}(?:\s*\[[^\]]+\])?\s*(?:<=|=(?!=))", parent_text, flags=re.MULTILINE):
                return True
            return False

        for match in inst_re.finditer(text):
            cell = match.group("cell")
            inst = match.group("inst")
            child_ports = module_defs.get(cell, {}).get("ports", {})
            conns = _named_instance_connections(match.group("conns"))
            for port, sig_expr in conns.items():
                sig = re.sub(r"\[[^\]]+\]", "", sig_expr).strip()
                child = child_ports.get(port)
                if not child or child.get("direction") not in {"output", "inout"}:
                    continue
                if parent_ports.get(sig, {}).get("direction") == "input":
                    continue
                drivers_by_sig.setdefault(sig, []).append({
                    "cell": cell,
                    "inst": inst,
                    "port": port,
                    "width": int(child.get("width") or 1),
                    "order": len(drivers_by_sig.get(sig, [])),
                })

        for sig, drivers in drivers_by_sig.items():
            # A child output connected directly to ``sig`` is already its
            # structural driver. Models occasionally add an invalid redundant
            # ``assign sig = module_name.port`` reference; module names are not
            # instance paths and the assignment must be removed.
            text = re.sub(
                rf"^\s*assign\s+{re.escape(sig)}\s*=\s*[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*\s*;\s*$",
                "",
                text,
                flags=re.MULTILINE,
            )
            if len(drivers) <= 1:
                continue
            keep = max(
                drivers,
                key=lambda d: (*_driver_score(sig, d["cell"], d["inst"], d["port"]), -int(d["order"])),
            )
            for driver in drivers:
                if driver is keep:
                    continue
                new_sig = f"{sig}_unused_from_{driver['inst']}_{driver['port']}"
                duplicate_output_drivers[(driver["cell"], driver["inst"], driver["port"])] = new_sig
                wire_updates[new_sig] = int(driver["width"] or 1)

        def repl(match: re.Match) -> str:
            cell = match.group("cell")
            inst = match.group("inst")
            child_ports = module_defs.get(cell, {}).get("ports", {})
            conns = _named_instance_connections(match.group("conns"))
            if not conns:
                return match.group(0)
            conn_text = match.group("conns")
            changed = False
            for port, sig_expr in conns.items():
                sig = re.sub(r"\[[^\]]+\]", "", sig_expr).strip()
                child = child_ports.get(port)
                # If the contract says the child consumes the complete bus,
                # do not preserve an LLM-introduced scalar bit-select from a
                # parent net of that exact width.  This is a deterministic
                # interface repair based on the emitted module declarations;
                # it does not depend on application or signal names.
                child_width = int((child or {}).get("width") or 1)
                if (
                    child
                    and child_width > 1
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*\[\s*\d+\s*\]", sig_expr)
                    and int(signal_widths.get(sig, 1)) == child_width
                ):
                    conn_text = re.sub(
                        rf"(\.{re.escape(port)}\s*\(\s*){re.escape(sig_expr)}(\s*\))",
                        rf"\1{sig}\2",
                        conn_text,
                        count=1,
                    )
                    sig_expr = sig
                    changed = True
                if not child or child.get("direction") not in {"output", "inout"}:
                    continue
                parent_info = parent_ports.get(sig)
                sig_width = int(signal_widths.get(sig, 1))
                duplicate_unused = duplicate_output_drivers.get((cell, inst, port))
                if has_parent_driver(sig, match.start(), match.end()):
                    new_sig = f"{sig}_unused_from_{inst}_{port}"
                    wire_updates[new_sig] = child_width
                elif duplicate_unused:
                    new_sig = duplicate_unused
                    wire_updates[new_sig] = child_width
                elif parent_info and parent_info.get("direction") == "input":
                    new_sig = f"{sig}_from_{inst}"
                    wire_updates[new_sig] = child_width
                elif not parent_info and sig in signal_widths and sig_width != child_width:
                    # Internal structural nets inherit the width of their
                    # child output driver. This corrects scalar-by-default
                    # declarations without an LLM repair or prompt change.
                    new_sig = sig
                    wire_updates[new_sig] = child_width
                else:
                    continue
                conn_text = re.sub(
                    rf"(\.{re.escape(port)}\s*\(\s*){re.escape(sig_expr)}(\s*\))",
                    rf"\1{new_sig}\2",
                    conn_text,
                    count=1,
                )
                changed = True
            if not changed:
                return match.group(0)
            return f"{cell} {inst} ({conn_text});"

        text = inst_re.sub(repl, text)
        for wire, width in sorted(wire_updates.items()):
            text = _replace_or_insert_wire_decl(text, wire, width)

        # Structured specs may expose a child input as a top-level observation
        # port named <module>_<port>. Mirror the actual internal connection into
        # that output so the declared contract is driven without changing the
        # child data path.
        mirror_assigns: List[str] = []
        for instance_match in inst_re.finditer(text):
            cell = instance_match.group("cell")
            inst = instance_match.group("inst")
            child_ports = module_defs.get(cell, {}).get("ports", {})
            conns = _named_instance_connections(instance_match.group("conns"))
            inst_stem = re.sub(r"^u_", "", inst)
            for port, sig_expr in conns.items():
                child = child_ports.get(port)
                if not child or child.get("direction") != "input":
                    continue
                signal = re.sub(r"\[[^\]]+\]", "", sig_expr).strip()
                for mirror in (f"{cell}_{port}", f"{inst_stem}_{port}"):
                    if mirror == signal or parent_ports.get(mirror, {}).get("direction") != "output":
                        continue
                    if re.search(rf"^\s*assign\s+{re.escape(mirror)}\s*=", text, flags=re.MULTILINE):
                        continue
                    mirror_assigns.append(f"assign {mirror} = {signal};")
                    break
        if mirror_assigns:
            unique_assigns = list(dict.fromkeys(mirror_assigns))
            text = re.sub(
                r"\nendmodule\b",
                "\n" + "\n".join(unique_assigns) + "\n\nendmodule",
                text,
                count=1,
            )
        out[fname] = text
    return out


def _connect_spec_inter_module_signals(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """Restore structural top wiring directly from the spec connection graph.

    Repair passes sometimes preserve both legal child port connections but leave
    the producer and consumer on different internal nets.  In that case the
    consumer net is undriven even though ``inter_module_signals`` identifies the
    unique producer.  Add only the missing structural assignment; never invent a
    source or alter a module interface.
    """
    if mode != "hierarchical":
        return verilog_map
    signals = spec_json.get("inter_module_signals") or []
    if not isinstance(signals, list) or not signals:
        return verilog_map

    modules = {
        str(module.get("name") or "")
        for module in ((spec_json.get("hierarchy") or {}).get("modules") or [])
        if isinstance(module, dict) and module.get("name")
    }
    if not modules:
        return verilog_map
    instance_re = re.compile(
        rf"\b(?P<cell>{'|'.join(sorted((re.escape(name) for name in modules), key=len, reverse=True))})\s+"
        r"(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<conns>.*?)\)\s*;",
        flags=re.DOTALL,
    )
    top_file = _top_rtl_file(spec_json, mode)
    top_code = verilog_map.get(top_file)
    if not top_code:
        return verilog_map
    top_ports = _declared_ports(top_code)

    module_port_directions: Dict[tuple[str, str], str] = {}
    for module in ((spec_json.get("hierarchy") or {}).get("modules") or []):
        if not isinstance(module, dict):
            continue
        module_name = str(module.get("name") or "")
        for port in module.get("ports") or []:
            if isinstance(port, dict) and module_name and port.get("name"):
                module_port_directions[(module_name, str(port.get("name")))] = str(port.get("direction") or "").lower()

    endpoint_nets: Dict[tuple[str, str], str] = {}
    structurally_driven_nets: set[str] = set()
    for match in instance_re.finditer(top_code):
        cell = match.group("cell")
        for port, signal in _named_instance_connections(match.group("conns")).items():
            net = re.sub(r"\[[^\]]+\]", "", signal).strip()
            endpoint_nets[(cell, port)] = net
            if module_port_directions.get((cell, port)) in {"output", "inout"}:
                structurally_driven_nets.add(net)

    existing_drivers = set(re.findall(r"^\s*assign\s+([A-Za-z_][A-Za-z0-9_$]*)\s*=", top_code, flags=re.MULTILINE))
    additions: List[str] = []
    for item in signals:
        if not isinstance(item, dict):
            continue
        source_module, source_port = _split_endpoint(str(item.get("source") or ""))
        source_net = endpoint_nets.get((source_module, source_port))
        if not source_net:
            continue
        for destination in item.get("destinations") or []:
            dest_module, dest_port = _split_endpoint(str(destination or ""))
            dest_net = endpoint_nets.get((dest_module, dest_port))
            if not dest_net or dest_net == source_net or dest_net in existing_drivers:
                continue
            # A net connected to any child output already has a structural
            # owner. A malformed/ambiguous graph edge must never add a second
            # continuous driver to it.
            if dest_net in structurally_driven_nets:
                continue
            # A parent input is an externally owned source and can never be a
            # legal destination for a generated structural assignment, even
            # when a malformed spec connection graph names it as one.
            if (top_ports.get(dest_net) or {}).get("direction") in {"input", "inout"}:
                continue
            additions.append(f"assign {dest_net} = {source_net};")
            existing_drivers.add(dest_net)

    if not additions:
        return verilog_map
    patched = re.sub(
        r"\nendmodule\b",
        "\n" + "\n".join(dict.fromkeys(additions)) + "\n\nendmodule",
        top_code,
        count=1,
    )
    return {**verilog_map, top_file: patched}


def _connect_top_output_feedback_to_matching_child_input(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """Feed a top observation net into a same-named child status input.

    Register/status blocks commonly consume the exact status signal also
    exported by the top. If the LLM puts that child input on a fresh undriven
    alias, the top's explicit output assignment is authoritative evidence for
    the connection; no application-specific signal mapping is required.
    """
    if mode != "hierarchical":
        return verilog_map
    top_file = _top_rtl_file(spec_json, mode)
    top_code = verilog_map.get(top_file)
    if not top_code:
        return verilog_map
    top_ports = _declared_ports(top_code)
    output_sources = {
        match.group("port"): match.group("source")
        for match in re.finditer(
            r"^\s*assign\s+(?P<port>[A-Za-z_][A-Za-z0-9_$]*)\s*=\s*(?P<source>[A-Za-z_][A-Za-z0-9_$]*)\s*;",
            top_code,
            flags=re.MULTILINE,
        )
        if (top_ports.get(match.group("port")) or {}).get("direction") == "output"
    }
    if not output_sources:
        return verilog_map

    module_ports: Dict[str, Dict[str, dict]] = {}
    for code in verilog_map.values():
        for module_name, module_code in _extract_verilog_modules(code).items():
            module_ports[module_name] = _declared_ports(module_code)
    additions: List[str] = []
    already_driven = set(re.findall(r"^\s*assign\s+([A-Za-z_][A-Za-z0-9_$]*)\s*=", top_code, flags=re.MULTILINE))
    for match in re.finditer(
        r"\b(?P<cell>[A-Za-z_][A-Za-z0-9_$]*)\s+(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<conns>.*?)\)\s*;",
        top_code,
        flags=re.DOTALL,
    ):
        ports = module_ports.get(match.group("cell")) or {}
        for port, signal_expr in _named_instance_connections(match.group("conns")).items():
            signal = re.sub(r"\[[^\]]+\]", "", signal_expr).strip()
            if (ports.get(port) or {}).get("direction") != "input" or signal in already_driven:
                continue
            source = output_sources.get(port)
            if not source or source == signal:
                continue
            additions.append(f"assign {signal} = {source};")
            already_driven.add(signal)
    if not additions:
        return verilog_map
    patched = re.sub(
        r"\nendmodule\b",
        "\n" + "\n".join(dict.fromkeys(additions)) + "\n\nendmodule",
        top_code,
        count=1,
    )
    return {**verilog_map, top_file: patched}


def _align_spec_inter_module_wire_widths(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """Apply the contract width to internal nets named by the connection graph.

    LLM RTL occasionally declares a structured-spec connection name as a scalar
    even though the actual producer and contract are buses.  Correcting the
    declaration is deterministic and preserves the generated data path.
    """
    if mode != "hierarchical":
        return verilog_map
    top_file = _top_rtl_file(spec_json, mode)
    top_code = verilog_map.get(top_file)
    if not top_code:
        return verilog_map
    top_ports = _declared_ports(top_code)
    patched = top_code
    for item in spec_json.get("inter_module_signals") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        try:
            width = int(item.get("width") or 1)
        except (TypeError, ValueError):
            continue
        if not name or width < 1 or name in top_ports:
            continue
        patched = _replace_or_insert_wire_decl(patched, name, width)
    return {**verilog_map, top_file: patched}


def _repair_undriven_inflight_state(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """Materialize a missing outstanding-transaction bit from valid/ready I/O.

    An ``*_inflight`` child input is state, not a free structural wire.  When a
    hierarchical top consumes such a net but supplies no driver, synthesize the
    standard one-entry handshake tracker.  This is limited to tops that expose
    both request and response valid/ready handshakes, so unrelated undriven nets
    remain hard quality-gate failures.
    """
    if mode != "hierarchical":
        return verilog_map
    top_file = _top_rtl_file(spec_json, mode)
    code = verilog_map.get(top_file)
    if not code:
        return verilog_map
    ports = _declared_ports(code)
    required = {"clk", "rst_n", "req_valid", "req_ready", "rsp_valid", "rsp_ready"}
    if not required.issubset(ports):
        return verilog_map

    patched = code
    for match in list(re.finditer(r"^\s*wire\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*(?:_inflight))\s*;\s*$", code, re.MULTILINE)):
        name = match.group("name")
        escaped = re.escape(name)
        driven = bool(
            re.search(rf"^\s*assign\s+{escaped}\s*=", code, re.MULTILINE)
            or re.search(rf"^\s*{escaped}\s*(?:<=|=(?!=))", code, re.MULTILINE)
        )
        if driven:
            continue
        # A child output connected to this net is already a structural driver.
        child_output_driver = False
        for module_code in verilog_map.values():
            for module_match in re.finditer(r"\bmodule\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\b(?P<body>.*?)(?=\bendmodule\b)", module_code or "", re.DOTALL):
                module_name = module_match.group("name")
                module_ports = _declared_ports(module_match.group(0))
                for inst in re.finditer(rf"\b{re.escape(module_name)}\s+[A-Za-z_][A-Za-z0-9_$]*\s*\((?P<conns>.*?)\)\s*;", code, re.DOTALL):
                    for port, signal in _named_instance_connections(inst.group("conns")).items():
                        if signal == name and (module_ports.get(port) or {}).get("direction") in {"output", "inout"}:
                            child_output_driver = True
                            break
                    if child_output_driver:
                        break
                if child_output_driver:
                    break
            if child_output_driver:
                break
        if child_output_driver:
            continue
        patched = re.sub(rf"^\s*wire\s+{escaped}\s*;\s*$", f"reg {name};", patched, count=1, flags=re.MULTILINE)
        tracker = (
            f"always @(posedge clk or negedge rst_n) begin\n"
            f"  if (!rst_n) {name} <= 1'b0;\n"
            f"  else begin\n"
            f"    if (rsp_valid && rsp_ready) {name} <= 1'b0;\n"
            f"    if (req_valid && req_ready) {name} <= 1'b1;\n"
            f"  end\n"
            f"end\n"
        )
        patched = re.sub(r"\nendmodule\b", "\n" + tracker + "\nendmodule", patched, count=1)
    return {**verilog_map, top_file: patched}


def _repair_undriven_last_accepted_observations(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """Create missing clocked ``last_*_accepted`` status observations."""
    if mode != "hierarchical":
        return verilog_map
    top_file = _top_rtl_file(spec_json, mode)
    code = verilog_map.get(top_file)
    if not code:
        return verilog_map
    ports = _declared_ports(code)
    if not {"clk", "rst_n"}.issubset(ports):
        return verilog_map
    widths = _declared_signal_widths(code)
    hierarchy_modules = ((spec_json.get("hierarchy") or {}).get("modules") or [])
    directions: Dict[tuple[str, str], str] = {}
    for module in hierarchy_modules:
        if not isinstance(module, dict):
            continue
        module_name = str(module.get("name") or "")
        for port in module.get("ports") or []:
            if isinstance(port, dict) and port.get("name"):
                directions[(module_name, str(port.get("name")))] = str(port.get("direction") or "").lower()
    module_names = [str(module.get("name")) for module in hierarchy_modules if isinstance(module, dict) and module.get("name")]
    if not module_names:
        return verilog_map
    instance_re = re.compile(
        rf"\b(?P<cell>{'|'.join(sorted((re.escape(name) for name in module_names), key=len, reverse=True))})\s+"
        r"[A-Za-z_][A-Za-z0-9_$]*\s*\((?P<conns>.*?)\)\s*;",
        re.DOTALL,
    )
    structural_outputs: set[str] = set()
    for instance in instance_re.finditer(code):
        cell = instance.group("cell")
        for port, signal in _named_instance_connections(instance.group("conns")).items():
            net = re.sub(r"\[[^\]]+\]", "", signal).strip()
            if directions.get((cell, port)) in {"output", "inout"}:
                structural_outputs.add(net)

    accepted_triggers = sorted(
        net for net in structural_outputs
        if int(widths.get(net, 1)) == 1 and (net == "accepted" or net.endswith("_accepted"))
    )
    patched = code
    for target_match in re.finditer(r"^\s*wire\s+(?:\[[^\]]+\]\s*)?(?P<name>last_(?P<field>[A-Za-z0-9_$]+)_accepted)\s*;\s*$", code, re.MULTILINE):
        target = target_match.group("name")
        field_tokens = {token for token in target_match.group("field").lower().split("_") if len(token) >= 3}
        if not field_tokens or target in structural_outputs:
            continue
        if re.search(rf"^\s*assign\s+{re.escape(target)}\s*=|^\s*{re.escape(target)}\s*(?:<=|=(?!=))", code, re.MULTILINE):
            continue
        target_width = int(widths.get(target, 1))
        candidates = [
            net for net in structural_outputs
            if net != target and int(widths.get(net, 1)) == target_width
            and field_tokens.issubset(set(net.lower().split("_")))
        ]
        if not candidates or not accepted_triggers:
            continue
        trigger = next((net for net in accepted_triggers if net.startswith("response_")), accepted_triggers[0])
        source = next((net for net in candidates if trigger.split("_", 1)[0] in net.lower().split("_")), None)
        if source is None and len(candidates) == 1:
            source = candidates[0]
        if source is None:
            continue
        patched = re.sub(
            rf"^\s*wire\s+(?P<range>\[[^\]]+\]\s*)?{re.escape(target)}\s*;\s*$",
            lambda match: f"reg {(match.group('range') or '')}{target};",
            patched,
            count=1,
            flags=re.MULTILINE,
        )
        tracker = (
            f"always @(posedge clk or negedge rst_n) begin\n"
            f"  if (!rst_n) {target} <= {target_width}'b0;\n"
            f"  else if ({trigger}) {target} <= {source};\n"
            f"end\n"
        )
        patched = re.sub(r"\nendmodule\b", "\n" + tracker + "\nendmodule", patched, count=1)
    return {**verilog_map, top_file: patched}


def _trim_zero_padded_assign_concats(verilog_map: Dict[str, str]) -> Dict[str, str]:
    """Normalize concat padding to the declared LHS width without losing data."""
    out: Dict[str, str] = {}
    for fname, code in verilog_map.items():
        widths = _declared_signal_widths(code)

        def item_width(item: str) -> int | None:
            literal = re.fullmatch(r"(\d+)'[bdh][0-9a-f_xz]+", item, flags=re.IGNORECASE)
            if literal:
                return int(literal.group(1))
            indexed = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_$]*)\[(\d+):(\d+)\]", item)
            if indexed:
                return abs(int(indexed.group(2)) - int(indexed.group(3))) + 1
            scalar = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", item)
            return int(widths.get(item, 1)) if scalar else None

        def repl(match: re.Match) -> str:
            lhs = match.group("lhs")
            lhs_width = int(widths.get(lhs, 1))
            items = [item.strip() for item in match.group("items").split(",")]
            if not items:
                return match.group(0)
            zero = re.fullmatch(r"(?P<width>\d+)'(?P<base>[bdh])0+", items[0], flags=re.IGNORECASE)
            if not zero:
                return match.group(0)

            item_widths = [item_width(item) for item in items]
            if any(width is None for width in item_widths):
                return match.group(0)
            excess = sum(int(width) for width in item_widths) - lhs_width
            pad_width = int(zero.group("width"))
            if excess <= 0 or excess >= pad_width:
                return match.group(0)
            new_width = pad_width - excess
            items[0] = f"{new_width}'{zero.group('base')}" + ("0" * max(1, (new_width + (3 if zero.group('base').lower() == 'h' else 0)) // (4 if zero.group('base').lower() == 'h' else 1)))
            return f"assign {lhs} = {{{', '.join(items)}}};"

        # Flatten a zero-extension nested inside a larger concatenation. This
        # keeps the same bit order and lets the width accounting below see all
        # fields (for example ``{header, {16'b0, value}, reserved}``).
        flattened = re.sub(
            r",\s*\{\s*(\d+'[bdh]0+)\s*,\s*([^,{}]+?)\s*\}",
            r", \1, \2",
            code,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"assign\s+(?P<lhs>[A-Za-z_][A-Za-z0-9_$]*)\s*=\s*\{(?P<items>[^{};]+)\}\s*;",
            repl,
            flattened,
        )

        def pad_repl(match: re.Match) -> str:
            lhs = match.group("lhs")
            lhs_width = int(widths.get(lhs, 1))
            items = [item.strip() for item in match.group("items").split(",")]
            item_widths = [item_width(item) for item in items]
            if not items or any(width is None for width in item_widths):
                return match.group(0)
            delta = lhs_width - sum(int(width) for width in item_widths)
            if delta == 0:
                return match.group(0)
            prefix = match.group("prefix") or ""
            operator = match.group("operator")
            if delta > 0:
                return f"{prefix}{lhs} {operator} {{{delta}'b0, {', '.join(items)}}};"

            excess = -delta
            # Generated packet layouts commonly over-allocate the final
            # reserved-zero field. Shrink only edge zero padding; never remove
            # or truncate a payload field.
            for index in (len(items) - 1, 0):
                zero = re.fullmatch(r"(?P<width>\d+)'[bdh]0+", items[index], flags=re.IGNORECASE)
                if not zero or int(zero.group("width")) < excess:
                    continue
                remaining = int(zero.group("width")) - excess
                if remaining:
                    items[index] = f"{remaining}'b0"
                else:
                    items.pop(index)
                return f"{prefix}{lhs} {operator} {{{', '.join(items)}}};"
            return match.group(0)

        out[fname] = re.sub(
            r"(?P<prefix>\bassign\s+)?(?P<lhs>[A-Za-z_][A-Za-z0-9_$]*)\s*"
            r"(?P<operator><=|=(?!=))\s*\{(?P<items>[^{};]+)\}\s*;",
            pad_repl,
            normalized,
        )
    return out


def _expected_top_port_names(spec_json: dict, mode: str) -> set:
    try:
        top = spec_json if mode == "flat" else spec_json["hierarchy"]["top_module"]
        return {
            str(port.get("name") or "").strip()
            for port in top.get("ports", [])
            if isinstance(port, dict) and str(port.get("name") or "").strip()
        }
    except Exception:
        return set()


def _replace_extra_input_with_internal_driver(code: str, port_name: str) -> Tuple[str, bool]:
    text = code or ""
    internal_candidates = [
        f"{port_name}_out",
        f"{port_name}_reg",
        f"{port_name}_q",
    ]
    internal = next((cand for cand in internal_candidates if re.search(rf"\b{re.escape(cand)}\b", text)), "")
    if not internal:
        return code, False

    changed = False

    def assign_repl(match: re.Match) -> str:
        nonlocal changed
        lhs = match.group(1)
        rhs = match.group(2)
        new_rhs = re.sub(rf"\b{re.escape(port_name)}\b", internal, rhs)
        if new_rhs != rhs:
            changed = True
        return f"{lhs}{new_rhs};"

    text = re.sub(
        r"(\bassign\s+[A-Za-z_][A-Za-z0-9_$]*\s*=\s*)([^;]*\b"
        + re.escape(port_name)
        + r"\b[^;]*);",
        assign_repl,
        text,
    )
    if not changed:
        return code, False

    text = _remove_module_header_port(text, port_name)
    text = re.sub(
        rf"^\s*input\b\s*(?:wire\s*)?(?:signed\s*)?(?:\[[^\]]+\]\s*)?{re.escape(port_name)}\s*;\s*\n",
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    return text.rstrip(), True


def _remove_spec_invalid_extra_control_inputs(verilog_map: Dict[str, str], spec_json: dict, mode: str) -> Dict[str, str]:
    """
    Spec-driven cleanup for LLM-generated hierarchical tops. If the top RTL
    invents an extra external input that is not part of the structured top
    module contract, and the same control exists as an internal register output
    or state signal, use the internal signal instead. This rejects only
    provably extra ports; it does not mask missing required ports.
    """
    top_file = _top_rtl_file(spec_json, mode)
    expected_ports = _expected_top_port_names(spec_json, mode)
    if not top_file or not expected_ports or top_file not in verilog_map:
        return verilog_map

    code = verilog_map[top_file]
    for port in _declared_input_ports(code):
        if port in expected_ports:
            continue
        code, _ = _replace_extra_input_with_internal_driver(code, port)

    updated = dict(verilog_map)
    updated[top_file] = code
    return updated


def _range_width(width: str) -> int:
    m = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", width or "")
    if not m:
        return 1
    return abs(int(m.group(1)) - int(m.group(2))) + 1


def _array_count(suffix: str) -> int:
    count = 1
    for hi, lo in re.findall(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", suffix or ""):
        count *= abs(int(hi) - int(lo)) + 1
    return count


def _declared_storage_bits(text: str) -> Dict[str, int]:
    decls: Dict[str, int] = {}
    decl_pat = re.compile(r"\b(?:reg|logic)\b\s*(?:signed\s*)?(\[[^\]]+\])?\s*([^;]+);", re.IGNORECASE)
    for dm in decl_pat.finditer(_strip_verilog_comments(text)):
        width = _range_width(dm.group(1) or "")
        for raw in dm.group(2).split(","):
            item = re.sub(r"=.*$", "", raw).strip()
            nm = re.match(r"([A-Za-z_][A-Za-z0-9_$]*)\s*((?:\[[^\]]+\]\s*)*)$", item)
            if nm:
                decls[nm.group(1)] = width * _array_count(nm.group(2))
    return decls


def _assigned_storage_bits(verilog_text: str) -> Tuple[int, List[str]]:
    text = _strip_verilog_comments(verilog_text)
    decl_bits = _declared_storage_bits(text)
    targets: Dict[str, int] = {}
    for block in re.findall(r"\balways(?:_ff)?\s*@\s*\((.*?)\)(.*?)(?=\balways|\bendmodule\b)", text, re.DOTALL):
        sens, body = block
        if not re.search(r"\b(posedge|negedge)\b", sens):
            continue
        for lhs in re.findall(r"\b([A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)\s*<=", body):
            target = re.sub(r"\[[^\]]+\]", "", lhs).strip()
            if target:
                targets[target] = max(targets.get(target, 0), decl_bits.get(target, 1))
    return sum(targets.values()), sorted(targets)


def _minimum_expected_flops(spec_json: dict, mode: str) -> int:
    text = json.dumps(spec_json).lower()
    targets: List[int] = []
    for m in re.finditer(r"(\d[\d,]*)\s*(?:-|to|through|–|—)\s*(\d[\d,]*)\s*(?:flip[- ]?flops?|flops?|register bits?)", text):
        lo = int(m.group(1).replace(",", ""))
        targets.append(max(1, int(lo * 0.65)))
    for m in re.finditer(r"(?:roughly|about|around|approximately|approx\.?|target)\s+(\d[\d,]*)\s*(?:flip[- ]?flops?|flops?|register bits?)", text):
        val = int(m.group(1).replace(",", ""))
        targets.append(max(1, int(val * 0.50)))
    for m in re.finditer(r"(\d[\d,]*)\s*(?:flip[- ]?flops?|flops?|register bits?)", text):
        val = int(m.group(1).replace(",", ""))
        if val >= 64:
            targets.append(max(1, int(val * 0.50)))
    # Do not infer a storage quota from feature words alone. Requirements often
    # say "optional FIFO", "do not infer FIFO", or describe remote DMA/model
    # interfaces. Those phrases are not an executable register-count contract.
    # Explicit numeric flip-flop/register-bit targets above remain enforceable;
    # memories and hard macros have their own implementation collateral gates.
    return max(targets) if targets else 0


def _validate_generated_complexity(spec_json: dict, mode: str, verilog_map: Dict[str, str]) -> List[str]:
    issues: List[str] = []
    full_text = "\n".join(verilog_map.values())
    scan = _strip_verilog_comments(full_text).lower()
    placeholder_patterns = [
        "placeholder",
        "implementation of functionality",
        "this logic to be defined",
        "omitted in this stub",
        "assume additional logic",
        "todo",
        "implement here",
        "fill in later",
    ]
    hits = [p for p in placeholder_patterns if p in scan]
    if hits:
        issues.append("❌ RTL contains placeholder/stub text instead of implemented logic: " + ", ".join(hits[:4]))

    min_flops = _minimum_expected_flops(spec_json, mode)
    assigned_bits, targets = _assigned_storage_bits(full_text)
    continuous_assignments = re.findall(r"\bassign\s+[A-Za-z_][A-Za-z0-9_$]*(?:\s*\[[^\]]+\])?\s*=\s*([^;]+);", scan)
    direct_constant_assignments = [
        rhs for rhs in continuous_assignments
        if re.fullmatch(r"\s*(?:\d+\s*'\s*[s]?[bodh]\s*[0-9a-f_xz?]+|\d+)\s*", rhs, re.IGNORECASE)
    ]
    data_inputs = [
        name for _width, names in re.findall(r"\binput\s*(\[[^\]]+\])?\s*([^;]+);", scan)
        for name in re.findall(r"[a-z_][a-z0-9_$]*", names)
        if name not in {"clk", "clock", "reset", "reset_n", "rst", "rst_n"}
    ]
    if (
        assigned_bits == 0
        and data_inputs
        and continuous_assignments
        and len(direct_constant_assignments) == len(continuous_assignments)
    ):
        issues.append(
            "❌ RTL is a constant-output shell: it has functional inputs but no state and every continuous output assignment is a literal constant. "
            "Implement the specified datapath, registers, counters, interfaces, and control behavior."
        )
    # Approximate resource prose is an architecture-size estimate rather than
    # an exact functional register contract. The parsed threshold is already
    # conservative; allow a bounded 20% estimation tolerance before calling
    # the RTL materially undersized. Placeholder shells are still rejected by
    # the explicit placeholder and minimum-state-element checks.
    material_floor = max(1, int(min_flops * 0.80)) if min_flops else 0
    if material_floor and assigned_bits < material_floor:
        issues.append(
            "❌ RTL storage is materially below the spec scale: "
            f"assigned_flipflop_bits={assigned_bits}, expected_minimum={material_floor} "
            f"(estimate_basis={min_flops}). "
            "Implement real FIFOs, buffers, counters, shifters, state machines, and register storage described by the spec."
        )
    if min_flops >= 128 and len(targets) < 12:
        issues.append(
            "❌ RTL appears collapsed into too few state elements for this spec: "
            f"storage_targets={len(targets)}. Do not return an output-register shell."
        )
    return issues


def _extract_module_ports(verilog_text: str) -> Dict[str, List[str]]:
    out = {}
    mod_pat = re.compile(r"\bmodule\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*;", re.DOTALL)
    for m in mod_pat.finditer(verilog_text):
        mod_name = m.group(1)
        raw_ports = m.group(2)
        port_names = []
        for p in raw_ports.split(","):
            token = p.strip()
            token = re.sub(r"\binput\b|\boutput\b|\binout\b|\bwire\b|\breg\b|\blogic\b|\bsigned\b", "", token)
            token = re.sub(r"\[[^\]]+\]", "", token)
            token = token.strip()
            if token:
                parts = token.split()
                if parts:
                    port_names.append(parts[-1].strip())
        out[mod_name] = port_names
    return out


def _normalize_signal_token(name: str) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"\[[^\]]+\]", "", name).strip()


def _split_endpoint(endpoint: str):
    if "." not in endpoint:
        raise ValueError(f"Invalid endpoint format: {endpoint}")
    mod, port = endpoint.split(".", 1)
    return mod.strip(), _normalize_signal_token(port.strip())


def _port_dir_map(module_ports):
    return {p["name"]: p.get("direction") for p in module_ports}


def _build_connectivity_contract(spec_json: dict, mode: str) -> dict:
    if mode != "hierarchical":
        return {
            "mode": mode,
            "modules": {},
            "top_module": _top_module_name(spec_json, mode),
            "top_ports": [],
            "top_level_connections": [],
            "internal_signals": [],
            "ownership": [],
        }

    top = spec_json["hierarchy"]["top_module"]
    modules = [top] + list(spec_json["hierarchy"].get("modules", []))

    module_map = {}
    for m in modules:
        module_map[m["name"]] = {
            "name": m["name"],
            "ports": m.get("ports", [])
        }

    top_ports = [p["name"] for p in top.get("ports", [])]

    internal_signals = []
    for sig in spec_json.get("inter_module_signals", []):
        src_mod, src_port = _split_endpoint(sig["source"])
        dsts = []
        for d in sig.get("destinations", []):
            dm, dp = _split_endpoint(d)
            dsts.append({"module": dm, "port": dp})

        internal_signals.append({
            "name": _normalize_signal_token(sig["name"]),
            "width": sig["width"],
            "source": {"module": src_mod, "port": src_port},
            "destinations": dsts,
            "description": sig.get("description", "")
        })

    top_conns = []
    for c in spec_json.get("top_level_connections", []):
        dsts = []
        for d in c.get("connected_to", []):
            dm, dp = _split_endpoint(d)
            dsts.append({"module": dm, "port": dp})
        top_conns.append({
            "top_port": _normalize_signal_token(c["top_port"]),
            "connected_to": dsts,
            "description": c.get("description", "")
        })

    ownership = []
    for o in spec_json.get("signal_ownership", []):
        om, op = _split_endpoint(o["owner"])
        ownership.append({
            "signal": _normalize_signal_token(o["signal"]),
            "owner": {"module": om, "port": op}
        })

    return {
        "mode": "hierarchical",
        "modules": module_map,
        "top_module": top["name"],
        "top_ports": top_ports,
        "top_level_connections": top_conns,
        "internal_signals": internal_signals,
        "ownership": ownership,
    }


def _validate_connectivity_contract(spec_json: dict, mode: str) -> List[str]:
    issues = []
    if mode != "hierarchical":
        return issues

    contract = _build_connectivity_contract(spec_json, mode)
    modules = contract["modules"]
    top_module = spec_json["hierarchy"]["top_module"]
    top_module_name = str(top_module.get("name") or "")
    top_port_names = {p["name"] for p in top_module.get("ports", [])}

    for mname, m in modules.items():
        if not m["ports"]:
            issues.append(f"❌ Module '{mname}' has empty ports in hierarchical spec.")

    for c in contract["top_level_connections"]:
        if c["top_port"] not in top_port_names:
            issues.append(f"❌ top_level_connections references unknown top port '{c['top_port']}'.")
        for dst in c["connected_to"]:
            if dst["module"] not in modules:
                issues.append(f"❌ top_level_connections target module '{dst['module']}' does not exist.")
                continue
            dirs = _port_dir_map(modules[dst["module"]]["ports"])
            if dst["port"] not in dirs:
                issues.append(f"❌ top_level_connections target port '{dst['module']}.{dst['port']}' does not exist.")

    for sig in contract["internal_signals"]:
        sm = sig["source"]["module"]
        sp = sig["source"]["port"]
        if sm not in modules:
            issues.append(f"❌ inter_module_signals source module '{sm}' does not exist.")
        elif sm != top_module_name:
            dirs = _port_dir_map(modules[sm]["ports"])
            if sp not in dirs:
                issues.append(f"❌ inter_module_signals source port '{sm}.{sp}' does not exist.")
            elif dirs.get(sp) not in {"output", "inout"}:
                issues.append(f"❌ inter_module_signals source port '{sm}.{sp}' must be output/inout, got '{dirs.get(sp)}'.")

        for dst in sig["destinations"]:
            dm = dst["module"]
            dp = dst["port"]
            if dm not in modules:
                issues.append(f"❌ inter_module_signals destination module '{dm}' does not exist.")
            elif dm != top_module_name:
                dirs = _port_dir_map(modules[dm]["ports"])
                if dp not in dirs:
                    issues.append(f"❌ inter_module_signals destination port '{dm}.{dp}' does not exist.")
                elif dirs.get(dp) not in {"input", "inout"}:
                    issues.append(f"❌ inter_module_signals destination port '{dm}.{dp}' must be input/inout, got '{dirs.get(dp)}'.")

    for owner in contract["ownership"]:
        om = owner["owner"]["module"]
        op = owner["owner"]["port"]
        top_internal_sources = {
            signal["source"]["port"]
            for signal in contract["internal_signals"]
            if signal["source"]["module"] == top_module_name
        }
        if om not in modules:
            issues.append(f"❌ signal_ownership owner module '{om}' does not exist.")
            continue
        dirs = _port_dir_map(modules[om]["ports"])
        if om == top_module_name:
            if op not in top_port_names and op not in top_internal_sources:
                issues.append(f"âŒ signal_ownership owner port '{om}.{op}' does not exist.")
            elif op in top_port_names and dirs.get(op) == "input" and owner["signal"] != op:
                issues.append(
                    f"âŒ top-level input owner '{om}.{op}' may own only its external input signal '{op}', "
                    f"not '{owner['signal']}'."
                )
            # Top inputs are driven by the external environment; top outputs
            # are driven by the design. Both are valid ownership endpoints.
            continue
        if op not in dirs:
            issues.append(f"❌ signal_ownership owner port '{om}.{op}' does not exist.")
        elif dirs.get(op) not in {"output", "inout"}:
            issues.append(f"❌ signal_ownership owner port '{om}.{op}' must be output/inout, got '{dirs.get(op)}'.")

    return issues


def _validate_spec_vs_rtl(spec_json: dict, mode: str, verilog_map: Dict[str, str]) -> Tuple[List[str], List[str], List[str]]:
    issues = []
    clock_ports = []
    reset_ports = []

    expected_modules = _collect_expected_modules(spec_json, mode)
    expected_files = set(_collect_expected_rtl_files(spec_json, mode))
    actual_files = set(verilog_map.keys())

    missing_files = sorted(expected_files - actual_files)
    extra_files = sorted(actual_files - expected_files)

    if missing_files:
        issues.append(f"❌ Missing expected RTL files: {missing_files}")
    if extra_files:
        issues.append(f"⚠ Extra RTL files emitted: {extra_files}")

    for mod in expected_modules:
        mod_name = mod["name"]
        rtl_file = mod["rtl_output_file"]
        spec_ports = [p["name"] for p in mod.get("ports", [])]

        code = verilog_map.get(rtl_file)
        if not code:
            continue

        extracted = _extract_module_ports(code)
        if mod_name not in extracted:
            issues.append(f"❌ Module '{mod_name}' not found in file '{rtl_file}'.")
            continue

        rtl_ports = extracted[mod_name]
        missing_ports = [p for p in spec_ports if p not in rtl_ports]
        extra_ports2 = [p for p in rtl_ports if p not in spec_ports]

        if missing_ports:
            issues.append(f"❌ Module '{mod_name}' missing ports vs spec: {missing_ports}")
        if extra_ports2:
            issues.append(f"❌ Module '{mod_name}' has extra ports vs spec: {extra_ports2}")

        declared = _declared_ports(_extract_verilog_modules(code).get(mod_name, code))
        for p in mod.get("ports", []) or []:
            pname = str(p.get("name") or "")
            if not pname or pname not in declared:
                continue
            expected_dir = str(p.get("direction") or "input").lower()
            expected_width = int(p.get("width") or 1)
            actual_dir = str(declared[pname].get("direction") or "").lower()
            actual_width = int(declared[pname].get("width") or 1)
            if actual_dir and actual_dir != expected_dir:
                issues.append(
                    f"Module '{mod_name}' port '{pname}' direction mismatch: spec={expected_dir}, rtl={actual_dir}"
                )
            if actual_width != expected_width:
                issues.append(
                    f"Module '{mod_name}' port '{pname}' width mismatch: spec={expected_width}, rtl={actual_width}"
                )

        for p in mod.get("ports", []):
            pname = p["name"]
            if re.search(r"clk|clock", pname, re.IGNORECASE):
                clock_ports.append(pname)
            if re.search(r"rst|reset", pname, re.IGNORECASE):
                reset_ports.append(pname)

    full_text = "\n".join(verilog_map.values())

    if mode == "hierarchical":
        contract = _build_connectivity_contract(spec_json, mode)

        for s in contract["internal_signals"]:
            sig_name = s.get("name")
            endpoints = [str((s.get("source") or {}).get("port") or "")]
            endpoints.extend(str((dst or {}).get("port") or "") for dst in s.get("destinations", []) or [])
            endpoints = [name for name in endpoints if name]
            if sig_name and sig_name not in full_text and endpoints and all(
                re.search(rf"\b{re.escape(name)}\b", full_text) for name in endpoints
            ):
                s["name"] = endpoints[0]

        for o in contract["ownership"]:
            sig_name = o.get("signal")
            owner_port = str((o.get("owner") or {}).get("port") or "")
            if sig_name and sig_name not in full_text and owner_port and re.search(rf"\b{re.escape(owner_port)}\b", full_text):
                o["signal"] = owner_port

        for c in contract["top_level_connections"]:
            tp = c["top_port"]
            if tp and tp not in full_text:
                issues.append(f"⚠ Top-level connection signal '{tp}' not clearly visible in RTL text.")

        for s in contract["internal_signals"]:
            sig_name = s["name"]
            if sig_name and sig_name not in full_text:
                issues.append(f"❌ Inter-module signal '{sig_name}' not found in RTL.")

        for o in contract["ownership"]:
            sig = o["signal"]
            owner = f"{o['owner']['module']}.{o['owner']['port']}"
            if sig and sig not in full_text:
                issues.append(f"⚠ Owned signal '{sig}' from '{owner}' not found in RTL.")

    return issues, sorted(set(clock_ports)), sorted(set(reset_ports))




def _build_generation_prompt(spec_json: dict, mode: str, regmap_obj: Optional[dict], clock_reset_obj: Optional[dict], power_intent_obj: Optional[dict]) -> str:
    connectivity_contract = _build_connectivity_contract(spec_json, mode)

    return f"""
You are a senior ASIC RTL engineer.

The input DIGITAL_SPEC_JSON is the single source of truth.
You must implement it exactly.
Your output is production-intent, functional, synthesizable, and directly verifiable RTL—not a syntax demonstration.
Implement every required behavior, state element, interface transaction, register semantic, datapath operation, status response, and reset rule described by the supplied contracts.
Compile and lint success are necessary but not sufficient: the design must perform its specified function and expose observable behavior suitable for automated simulation, formal checks, firmware access, FPGA prototyping, and ASIC implementation.
The top-level module name is {_top_module_name(spec_json, mode)}.
The top-level RTL file is {_top_rtl_file(spec_json, mode)}.
Child/internal modules may use wrapper or interface suffixes when they are present in DIGITAL_SPEC_JSON.
The declared top-level module must still be exactly {_top_module_name(spec_json, mode)}.
Do NOT redesign architecture.
Do NOT rename modules.
Do NOT rename ports.
Do NOT change rtl_output_file names.
Do NOT add extra modules.
Do NOT drop required modules.
Do NOT add extra ports.
Do NOT omit required ports.

STRICT OUTPUT RULES
- Output ONLY named Verilog file blocks.
- No markdown fences.
- No explanations.
- Use this exact format:
---BEGIN file_name.v---
<verilog here>
---END file_name.v---

SPEC MODE:
{mode}

DIGITAL_SPEC_JSON:
{_safe_json(spec_json)}

DERIVED_INTERFACE_CONTRACT:
{_safe_json(connectivity_contract)}

DIGITAL_REGMAP_JSON:
{_safe_json(regmap_obj) if regmap_obj is not None else "null"}

CLOCK_RESET_ARCH_JSON:
{_safe_json(clock_reset_obj) if clock_reset_obj is not None else "null"}

POWER_INTENT_JSON:
{_safe_json(power_intent_obj) if power_intent_obj is not None else "null"}

IMPLEMENTATION RULES

FATAL RTL CORRECTNESS RULES (HIGHEST PRIORITY)

The generated RTL must pass both:
1. Icarus Verilog compile
2. fatal Verilator lint

These rules override stylistic preferences.

A. SINGLE LEGAL OWNER PER SIGNAL (MANDATORY)
Every signal must have exactly one legal owner and exactly one legal driving style.

Allowed ownership styles:
- sequential register/state/output:
  assigned only with nonblocking <= in exactly one clocked always @(posedge clk or negedge rst_n) block
- combinational signal/output:
  assigned only with blocking = in exactly one always @(*) block with full default assignments
- structural wire:
  driven only by assign statements or module port connectivity, and never by procedural blocks

Forbidden:
- assigning the same signal with both = and <=
- assigning the same signal in both a clocked block and a combinational block
- assigning the same signal in two different always blocks
- procedurally driving a signal that is already driven structurally
- driving a child-owned signal again in the parent/top

If a signal is a child output or an inter-module wire, keep it as structural wiring only.
If a signal is declared as a stored register/state element, drive it from one clocked block only.
If a signal is a combinational decode/output, drive it from one always @(*) block only.

B. TOP/HIERARCHY OWNERSHIP DISCIPLINE (MANDATORY)
In hierarchical designs:
- the top module must not procedurally assign, reset, or re-drive any signal owned by a child module
- if signal_ownership says a child owns a signal, the top may only expose it through structural wiring
- do not convert child outputs into top-level procedural regs unless the spec explicitly requires that

C. FSM / OUTPUT STYLE DISCIPLINE
For FSM-controlled or decoded outputs, choose exactly one style per signal:
- registered output in one clocked block only
OR
- combinational output in one always @(*) block only

Do not mix reset-time <= assignments with combinational = assignments for the same output.

D. MULTI-DRIVER BAN
Every signal must have exactly one legal driver.
No signal may be driven by:
- two always blocks
- assign plus always block
- child output plus top assign
- child output plus top procedural block
- two child outputs
- clocked block plus combinational block

E. COMBINATIONAL SAFETY
Every combinational always @(*) block must:
- assign defaults at block entry
- assign every driven signal on all paths
- include a default branch in every case

CONCRETE GOOD/BAD EXAMPLES

BAD:
reg irq;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) irq <= 1'b0;
  else irq <= done;
end
always @(*) begin
  irq = fault;
end

GOOD:
reg irq;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) irq <= 1'b0;
  else irq <= (done | fault);
end

BAD:
wire child_irq;
assign irq = child_irq;
always @(*) begin
  irq = 1'b0;
end

GOOD:
wire child_irq;
assign irq = child_irq;

BAD:
reg status_reg;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= 8'h00;
  else if (adc_done) status_reg <= 8'h01;
end
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= 8'h00;
  else if (ana_fault) status_reg <= 8'h02;
end

GOOD:
reg [7:0] status_reg;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= 8'h00;
  else begin
    status_reg[0] <= adc_done;
    status_reg[1] <= ana_fault;
  end
end

B. OUTPUT / WIRE / REG ROLE DISCIPLINE
- If a signal is a pure structural connection between modules, keep it as a wire and do not assign it in always blocks.
- If a module output is driven from sequential logic, declare and drive it as a reg-style procedural output and do not also assign it combinationally.
- Do not declare a top-level wiring signal and then also reset or assign it procedurally.

C. FSM OUTPUT DISCIPLINE
For FSM-controlled outputs:
- either register them in the clocked block using <= only
- or compute them combinationally in always @(*) with blocking = only and full default assignments
- do not mix the two styles for the same output

D. MULTI-DRIVER BAN
Every signal must have exactly one legal driver.
No signal may be driven by:
- two always blocks
- child output plus top assign
- child output plus top procedural block
- assign plus procedural block

E. CASE / COMBINATIONAL SAFETY
Every combinational always @(*) block must:
- assign defaults at block entry
- assign every driven signal on all paths
- include a default branch in every case

- Generate synthesizable Verilog-2005 only.
- Do NOT use SystemVerilog constructs.
- Forbidden constructs include:
  - typedef
  - enum
  - logic
  - always_comb
  - always_ff
  - struct
  - union
  - packed arrays
  - unpacked array ports
  - unique case
  - priority case
- Use only Verilog-2005 constructs such as:
  - module
  - input/output/inout
  - wire
  - reg
  - localparam
  - assign
  - always @(*)
  - always @(posedge clk or negedge rst_n)
- If SPEC MODE is flat, generate exactly one module file only.
- If SPEC MODE is hierarchical, generate every required module file from spec.
- Each file must contain the module declared in its rtl_output_file mapping.
- All module headers must exactly match the spec contract.
- Use only declared signals.
- No undeclared identifiers.
- No TODOs.
- No empty shells.
- Do not take a part-select or bit-select from an expression, function call, concatenation, or parenthesized arithmetic expression.
- Illegal example: ({{1'b0, a}} + {{1'b0, b}})[12:1].
- Legal pattern: assign the expression to a named wire/reg first, then select from that named signal.
- Every declared output must have exactly one legal driver.
- A register assigned with nonblocking <= in a clocked always block must not be assigned with blocking = anywhere else.
- Do not use always @(*) blocks to write stored configuration registers, status registers, interrupt-clear registers, threshold registers, or rd_data registers that are also written in clocked logic.
- For register-file readback, either make rd_data a combinational wire driven from a separate read_mux, or make rd_data_r purely sequential; do not implement both styles for rd_data_r.
- In structural top modules, outputs may be exposed through wiring from the owning child module.
- Do not force procedural driving at the top unless the top module owns the signal.
- Use DIGITAL_SPEC_JSON module functionality, responsibilities, must_drive, must_receive, must_not_drive, reset_behavior, and behavior_rules as hard requirements.
- Use DERIVED_INTERFACE_CONTRACT as the exact wiring contract.
- For each top-level connection, connect the declared top port to the listed module ports.
- For each internal signal, create exactly one internal wire of the declared width.
- Drive that wire only from the declared source endpoint.
- Consume that wire only at the declared destination endpoints.
- Respect ownership exactly; do not invent alternate drivers or alternate buses.
- If a top-level output is owned by a submodule according to signal_ownership, the top module must expose it only through structural wiring/port connections.
- The top module must NOT procedurally assign or reset a top-level output that is owned by a submodule.
- Do NOT add top-level always blocks that drive outputs already driven by child modules.
- Do not collapse multiple declared signals into a grouped convenience bus unless the spec explicitly defines that bus.
- If multiple scalar/vector signals are declared separately in module ports, connect them separately by name.
- Do NOT invent aggregate ports such as reg_bus, reg_bus_signals, ctrl_bus, status_bus, or similar unless explicitly present in DIGITAL_SPEC_JSON.
- If there is a register map, implement real stored writable registers where required.
- Implement STATUS and INT_STATUS from explicit field semantics if regmap provides them.
- If a wider value is split across multiple narrower registers, reconstruct it to the exact declared signal width only.
- Example rule: if a 12-bit signal uses one low byte and one high nibble, reconstruct as {{high_reg[3:0], low_reg[7:0]}}, not as a 16-bit concatenation.
- When reconstructing a wider signal from register bytes, the concatenation width must exactly match the declared destination width.
- If cfg_dac_code is [11:0], reconstruct only 12 bits, for example:
  {{dac_code_h[3:0], dac_code_l[7:0]}}
- Never concatenate two full 8-bit registers into a 12-bit destination.
- Never assign a concatenation wider than the declared destination signal width.
- Prefer the simplest deterministic smoke-test implementation consistent with the contract, but do not collapse the architecture below the state/storage scale requested by the spec.

SCALE AND COMPLETENESS RULES
- If the spec requests a rough flip-flop/register-bit target, FIFO depth, line-buffer storage, histogram counters, DMA buffers, packet buffers, shifters, or multiple pipeline stages, implement those as real Verilog storage and real sequential logic.
- Do not satisfy a complex design by registering only outputs or by emitting a shell with comments.
- If DIGITAL_SPEC_JSON contains memory_macros[] with kind openram_sram, prebuilt_sky130_sram, prebuilt_sram, or precompiled_sram_macro, instantiate the named SRAM macro cell exactly once per required instance. Do not implement that SRAM macro as a local reg array.
- The SRAM instance module name must match memory_macros[].name, and the instance name should match memory_macros[].instance_name when provided.
- Connect SRAM ports using memory_macros[].ports canonical roles: clk, csb, we/web, addr, din, dout.
- The address width, data width, and depth implied by the RTL connections must match memory_macros[].addr_width, memory_macros[].data_width, and memory_macros[].depth.
- It is acceptable to emit a simulation-only abstract SRAM module with the same macro cell name only when needed for compile, but the top/controller RTL must still instantiate that macro cell so OpenRAM/AutoMBIST can replace and validate real collateral later.
- A declared SRAM macro used by functional requirements must be functionally reachable. Do not tie chip-select inactive, write-enable inactive, address, data, or output paths to constants unless the spec explicitly says the macro is unused.
- If the controller has software/register/port-driven memory read or write semantics, connect those transactions to the SRAM macro csb/we/web/addr/din/dout roles with real sequential/control logic.
- Any emitted simulation-only SRAM abstraction must implement writable/readable memory behavior for clk/csb/we-or-web/addr/din/dout. A constant-zero dout shell is allowed only for an explicitly unused placeholder macro.
- A required memory must survive synthesis as functional storage. Its chip-select must be asserted by at least one legal input-driven transaction, its address/write/data controls must come from real control or datapath logic, and its read data must affect an observable output, status/readback path, request/response path, or other required state transition.
- BAD required-memory implementation: tie active-low csb to 1'b1, tie all controls to constants, and connect dout to an *_unused wire. This is a dead instance and synthesis will remove it.
- GOOD required-memory implementation: decode a declared command/register/stream event into bounded read/write controls, retain the required synchronous-read timing, and consume dout in a declared functional or observable path.
- If Insert MBIST is enabled downstream, the SRAM macro instance is the integration point for the AutoMBIST wrapper. Do not hide the memory inside unrelated procedural logic.
- If the spec says FIFO, implement explicit FIFO storage, pointers, levels, full/empty status, push/pop behavior, and reset.
- If the spec says line buffer, histogram, frame buffer, or pipeline metadata, implement explicit storage arrays/counters/registers and update them in clocked logic.
- If the spec says UART/SPI/I2C/packet engine, implement real shifter/counter/FSM state consistent with the described smoke-test behavior.
- Placeholder text such as "implementation omitted", "logic to be defined", "assume additional logic", TODO, or comments in place of behavior is a hard failure.
UNUSED SIGNAL HYGIENE
- Every declared input, status input, and internal register should be either:
  - functionally used in logic, or
  - intentionally consumed in a harmless deterministic way so that lint does not report it as unused.
- For minimal smoke-test implementations, avoid leaving declared ports completely unused when a functional legal path can be implemented.
- Example acceptable pattern:
  - use a signal in a benign conditional branch
  - fold status inputs into a readback/status register if consistent with the spec
- Do not add fake functionality just to silence lint, but avoid trivially unused declared signals when possible.
- If any module uses an FSM, implement states using Verilog-2005 localparam constants and reg state registers.
- Do NOT use typedef enum or any SystemVerilog FSM syntax.
- Entire design must compile together cleanly.
- A module must NEVER reference another module instance by hierarchical name.
- Forbidden examples inside child RTL:
  - interrupt_ctrl.irq
  - u_interrupt_ctrl.irq
  - digital_subsystem.some_wire
- If a register block needs interrupt status, that status must arrive through an explicit declared input port from the top-level wiring contract.
- Every child module must be self-contained and may only use:
  - its own ports
  - its own local regs/wires/params
- Distinguish RW storage registers from RO view registers.
- RW registers must be backed by explicit stored regs when written by software.
- RO registers must NOT invent undeclared storage elements just because the regmap gives them names.
- If a RO register represents fields from an input/status signal, implement readback directly from the corresponding declared input port or from an explicitly declared shadow/status reg.
- Example: if the regmap contains ADC_DATA_L and ADC_DATA_H and the module has input adc_data_sync[11:0], then:
  - ADC_DATA_L readback must come from adc_data_sync[7:0]
  - ADC_DATA_H readback must come from the upper nibble packed into 8 bits, e.g. {{4'b0000, adc_data_sync[11:8]}}
- Do NOT reference symbolic register names such as ADC_DATA_L or ADC_DATA_H in RTL unless you explicitly declared them as reg/wire objects in that module.
- Every identifier used in a read-data mux must be either:
  - a declared reg
  - a declared wire
  - a declared port
  - a literal/concatenation/slice of declared signals
- Output must be fully compile-ready Verilog-2005 with no placeholders.
- NEVER emit pseudo-code, TODOs, comments, or template text in place of expressions.
- Forbidden examples:
  - /* condition */
  - /* address */
  - /* data */
  - TODO
  - implement here
  - fill in later
- Every assignment RHS must be a legal Verilog expression.
- If some protocol detail is underspecified, implement the smallest deterministic FUNCTIONAL subset justified by the declared ports, register map, behavior rules, and verification requirements. Document the chosen behavior in ordinary RTL comments.
- Underspecification is never permission to return a stub, constant-output shell, inactive interface, empty module, or compile-only implementation.
- Safe constants are permitted only as reset/default values or for outputs explicitly specified as unused. At least one legal input transaction must exercise every required functional block and produce observable state or output changes.
- For register-mapped designs, implement writable storage, readable status/data, address decode, reset behavior, and side effects described by the register map.
- For sensor/control designs, capture valid input samples, update counters/status, implement thresholds/alerts where specified, and make results observable through outputs or readback.
- Generate RTL that is directly verifiable: deterministic reset state, finite bounded transactions, explicit valid/ready or request/response behavior when declared, stable readback semantics, and no hidden initialization dependency.
- Do NOT derive semantic configuration or control signals from raw bus signals unless explicitly defined in DIGITAL_SPEC_JSON.
- If a module input is named cfg_*, enable, start, mode, threshold, data, etc.,
  it MUST be driven from an explicitly declared inter_module signal source.
- Forbidden example:
  cfg_* ← reg_wdata[x:y]
- If a required mapping is missing, do NOT guess or hide the omission with a constant-safe tie-off. Report the missing contract so generation fails visibly.
  - Distinguish raw external signals from derived internal signals.
- If an inter-module signal is owned by a child module according to signal_ownership, the top module MUST NOT recreate, shortcut, alias, or directly assign that signal from a top-level input or any other source.
- The top module may only connect child-owned internal signals structurally through wires and port connections.

DECLARED PORT COMPLETENESS RULES (MANDATORY)

- Every declared output port must be explicitly driven in the final RTL.
- Every declared input port must be:
  - used in functional logic, or
  - reflected in a specified status/readback path, or
  - intentionally tied into a benign deterministic condition that is consistent with the spec.
- Do not leave any declared output undriven.
- Do not leave any declared input completely unused if the spec gives it behavioral meaning.

For flat single-module register-based peripherals:
- if a control/output signal is listed in the interface, define its exact register or logic source
- if a status/data input is listed in the interface, define where it is captured or exposed in readback
- if a readiness/fault/done input is listed, define whether it affects control gating, status bits, or interrupt generation

DECLARED PORT USAGE EXAMPLES

BAD:
input        ana_ready;
output reg   dac_enable;
// ana_ready never used
// dac_enable never assigned

GOOD:
input        ana_ready;
output reg   dac_enable;

always @(*) begin
  dac_enable = control_reg[2] & ana_ready;
end

BAD:
input  [11:0] adc_data;
input         adc_done;
reg    [11:0] adc_data_reg;

// adc_data declared but never captured

GOOD:
input  [11:0] adc_data;
input         adc_done;
reg    [11:0] adc_data_reg;

always @(posedge clk or negedge rst_n) begin
  if (!rst_n)
    adc_data_reg <= 12'h000;
  else if (adc_done)
    adc_data_reg <= adc_data;
end

BAD:
always @(*) begin
  case (paddr)
    8'h00: prdata = control_reg;
    8'h04: prdata = status_reg;
  endcase
end

GOOD:
always @(*) begin
  prdata = 32'h00000000;
  case (paddr)
    8'h00: prdata = control_reg;
    8'h04: prdata = status_reg;
    default: prdata = 32'h00000000;
  endcase
end

PROCEDURAL OUTPUT DECLARATION RULES (MANDATORY)

- If an output is assigned inside any always @(*) block or any clocked always block, that output must be declared as a reg-style procedural output in Verilog-2005.
- Do NOT declare an output as a plain wire-style output if it is assigned procedurally.
- If an output is driven only by a continuous assign statement, it may remain a plain output wire-style port.
- Never procedurally assign to a wire-style output.


GOOD:
output reg [31:0] prdata;
always @(*) begin
  prdata = 32'h00000000;
  case (paddr)
    ...
    default: prdata = 32'h00000000;
  endcase
end

GOOD:
output [31:0] prdata;
assign prdata = prdata_mux;

BAD:
output [31:0] prdata;
always @(*) begin
  prdata = 32'h00000000;
end

BAD:
output [31:0] prdata;
wire [31:0] prdata_temp;
always @(*) begin
  prdata_temp = 32'h0;
end
assign prdata = prdata_temp;

GOOD:
output [31:0] prdata;
reg [31:0] prdata_temp;
always @(*) begin
  prdata_temp = 32'h0;
end
assign prdata = prdata_temp;

INTERNAL SIGNAL ROLE SEPARATION RULES (MANDATORY)

- Distinguish:
  1. externally visible top-level ports
  2. internal inter-module signals
  3. decoded control/configuration signals
  4. status/derived/behavioral outputs

- Do NOT reuse one signal for multiple unrelated roles unless explicitly defined in DIGITAL_SPEC_JSON.
- A decoded control/config signal must remain a dedicated internal signal unless explicitly defined as a top-level port.
- A behavioral/status/output signal must not be reused as an unrelated internal control/config signal.
- If one module produces a signal and another consumes it, connect them through a dedicated internal wire.
- Never merge signals just because names or widths look similar.
- Never alias two signals unless the contract explicitly defines them as the same.

DECODED REGISTER SIGNAL WIDTH RULES (MANDATORY)

- If multiple semantic signals are decoded from a register, the register must be wide enough.
- Do not declare a scalar if indexed bits are used.
- Bit/part selections must match declared widths.
- Concatenations must match destination width exactly.
- Do not rely on implicit truncation/expansion.

- Example:
  If adc_done_sync is owned by analog_if_logic.adc_done_sync, then the top module must connect:
    analog_if_logic.adc_done_sync -> internal wire adc_done_sync
  and then consume that wire in downstream modules.
  The top module MUST NOT do:
    assign adc_done_sync = adc_done;
    assign adc_done_sync = ana_done;
    assign adc_done_sync = any_top_input;

- The same rule applies to synchronized, decoded, filtered, qualified, or derived signals such as:
  *_sync
  *_status
  *_irq
  *_valid
  *_ready
  *_fault
  *_done

- If a signal name appears in inter_module_signals and signal_ownership, then:
  1. create exactly one internal wire of that name
  2. connect the declared owner port to that wire
  3. connect that wire to the declared consumer ports
  4. do not add any extra assign or procedural driver for that signal
DERIVED SIGNAL OWNERSHIP RULE:
If a submodule produces synchronized, decoded, filtered, qualified, or derived versions of top-level inputs (for example *_sync, *_status, *_irq, *_valid, *_ready, *_fault, *_done), then those derived outputs must appear explicitly in:
- module ports
- inter_module_signals
- signal_ownership
and must be owned by the producing submodule, not by the top module.
- If multiple semantic outputs are decoded from one writable register (for example cfg_enable, cfg_adc_start, cfg_dac_enable), the backing register MUST be declared as a vector wide enough to hold all referenced bits.
- Never declare a backing register as a scalar if any code reads indexed bits from it.
- Examples:
  - If RTL uses control_reg[0], control_reg[1], or control_reg[2], then control_reg must be declared at least as reg [2:0] control_reg;
  - If the register is software-visible as an 8-bit register, prefer reg [7:0] control_reg;

- Any signal used with bit selection [N] or part selection [M:N] MUST be declared as a vector of sufficient width.
- Never emit code that indexes into a scalar reg or wire.

- For register-backed semantic config outputs:
  - cfg_enable may be driven from control_reg[0]
  - cfg_adc_start may be driven from control_reg[1]
  - cfg_dac_enable may be driven from control_reg[2]
  only if control_reg is declared with sufficient width.

- When reconstructing a wider configuration value from byte registers, the concatenation width MUST exactly equal the destination width.
- Example:
  - if cfg_dac_code is [11:0]
  - and dac_code_l is [7:0]
  - and dac_code_h is [7:0] but only lower nibble is valid
  - then cfg_dac_code must be {{dac_code_h[3:0], dac_code_l[7:0]}}
- Never assign {{dac_code_h, dac_code_l}} to a 12-bit destination.
 Example:
  If software writes an 8-bit CONTROL register and RTL decodes bits [0], [1], and [2], declare:
    reg [7:0] control_reg;
  not:
    reg control_reg;

- Protocol-facing modules (such as i2c_slave, spi_slave, uart_rx, uart_tx, bus_adapter, decoder, bridge, handshake controllers, or similar) must still be fully compile-ready Verilog-2005.
- If the protocol behavior is not fully specified, DO NOT emit protocol pseudo-code, comment placeholders, or template conditions.
- In particular, NEVER emit executable constructs such as:
  if (/* ... */)
  while (/* ... */)
  case (/* ... */)
  assign x = /* ... */;
- For underspecified protocol modules, implement a minimal functional transaction path using the declared interface contract; do not emit a compile-only stub.
- Reset-safe defaults are required, but after reset declared legal inputs must be able to cause observable protocol, state, status, or readback behavior.
- Never disable required behavior with constant-false conditions such as `if (1'b0)` and never tie required enables, requests, read/write strobes, or outputs permanently inactive.
- If the specification genuinely lacks enough information to implement any legal transaction, fail generation and report the missing behavioral contract instead of inventing semantics or emitting nonfunctional RTL.
- Every if/else/case condition must be a legal Verilog expression using only literals, declared signals, parameters, or valid operators.

1. FSM coding rules
- For every FSM, use a standard 2-process style:
  a) one sequential always block for state registers:
     always @(posedge clk or negedge rst_n)
  b) one combinational always @(*) block for next_state and combinational outputs
- In every combinational always @(*) block, assign safe default values at the top BEFORE the case statement:
  - next_state must get a default assignment
  - every combinational output driven in that block must get a default assignment
- Every signal assigned in a combinational block must be assigned on all paths.
- Do NOT generate combinational blocks that leave outputs unassigned in any case/default branch.
- Do NOT generate latch-prone RTL.

2. Case statement rules
- Every case statement must include a default branch.
- Do not leave read/write decode cases incomplete.
- For register read muxes, provide a deterministic default read value.
- For write decodes, include a default no-op branch.

3. Width and assignment rules
- All assignments must be width-correct.
- Do not assign narrower concatenations into wider registers without explicit zero-extension.
- Match declared signal widths exactly.
- Avoid implicit truncation/expansion unless explicitly intended and coded.

4. Register map rules
- Separate control and status behavior clearly.
- If status registers pack status bits into a wider register, explicitly zero-pad unused upper bits.
- Register read data must be assigned deterministically.
- Avoid incomplete register decode logic.

6. Output requirement
Before finalizing the RTL, self-check:
- no latch inference
- no incomplete combinational assignments
- default branch exists in every case
- widths are consistent
- top/module/port names exactly match the spec JSON

FSM SAFETY RULES

- In every combinational always @(*) block:
  - assign default values at block entry
  - assign all outputs on all paths
- No latch inference is allowed.

SELF-CHECK BEFORE OUTPUT
1. Every expected file is emitted exactly once.
2. Every module name matches spec.
3. Every port list matches spec exactly.
4. No missing or extra ports.
5. No undeclared signals.
6. No width mismatches.
7. top_level_connections are reflected in the top RTL.
8. inter_module_signals are reflected as actual internal wires and connections.
9. signal_ownership is respected, with one legal driver per owned signal.
10. Stored registers are not faked by directly echoing bus write data on reads.
11. No SystemVerilog syntax is used.
12. No top-level always block drives an output owned by a child module.
13. FSMs use localparam + reg state encoding only.
14. For every case item in a register read-data mux, the right-hand-side expression must use only declared identifiers.
15. RO registers described in DIGITAL_REGMAP_JSON must map to declared status/input signals or explicitly declared shadow regs; never use undeclared symbolic register names from the regmap.
14. No comments or placeholder text may appear inside executable expressions.
15. No assignment may contain /* ... */ on the RHS.
16. If protocol details are incomplete, emit the smallest deterministic functional implementation supported by the contract; never emit pseudo-code or a compile-only stub.
17. For every inter-module signal owned by a child module:
    - the top module contains exactly one wire of that signal name
    - the wire is driven only by the declared owner child port
    - the wire is consumed only by the declared destination ports
    - there is no assign statement or always block in the top module that re-drives or aliases that signal

18. Never connect a *_sync signal directly from a raw top-level input unless the spec explicitly declares that raw input as the owner.
19. Every signal used with bit indexing or slicing must be declared as a vector of sufficient width; never index into a scalar.
20. Any backing register used to decode multiple semantic outputs must be declared wide enough for all referenced bit positions.
21. For every assignment, LHS width and RHS width must match exactly after slicing/concatenation.
22. If cfg_* outputs are derived from a writable control register, the control register declaration and bit usage must be mutually consistent.
23. No executable statement may contain comment text as an expression or condition.
24. Every if, case, while, and ternary condition must be a legal Verilog expression.
25. For underspecified protocol modules, emit a minimal functional transaction path, not protocol pseudo-code, inactive logic, or a compile-safe stub.
26. Every required functional input can influence observable state, status, readback, or output behavior through at least one legal bounded transaction after reset.
27. The design is suitable for automated verification: reset is deterministic, state transitions are bounded, register semantics are stable, and expected behavior is observable at declared ports.
28. Search generated RTL for forbidden placeholder patterns before output:
    - if (/*
    - case (/*
    - = /* 
    - TODO
    - implement here
    - some condition
27. No signal is reused for multiple unrelated roles unless explicitly defined.
28. No top-level port is reused as an unrelated internal signal.
29. No decoded control signal is aliased onto an unrelated output.
30. Every signal has exactly one legal driver.
31. Structural top modules do not convert child outputs into procedural top-level regs unless required.
PROCEDURAL OUTPUT CONSISTENCY SELF-CHECK (MANDATORY)

Before returning RTL, verify this exact rule for every output:
- if the output appears on the left-hand side inside any always block, it must be declared as output reg in Verilog-2005
- if the output is declared as plain output, it must not appear on the left-hand side inside any always block
- never return RTL that would cause:
  - "is not a valid l-value"
  - PROCASSWIRE

""".strip()

def _find_fallback_spec_json(workflow_dir: str):
    spec_dir = os.path.join(workflow_dir, "spec")
    if not os.path.isdir(spec_dir):
        return None
    cands = []
    for fn in os.listdir(spec_dir):
        if fn.endswith("_spec.json"):
            cands.append(os.path.join(spec_dir, fn))
    cands.sort()
    return cands[0] if cands else None

def _record_text_artifact_safe(workflow_id, agent_name, subdir, filename, path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir=subdir,
                    filename=filename,
                    content=f.read(),
                )
    except Exception as e:
        print(f"⚠️ Failed to upload artifact {filename}: {e}")




def _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir):
    for fname in [
        "rtl_agent_entry.json",
        "rtl_agent_preflight.json",
        "rtl_agent_compile.log",
        "rtl_verilator_lint.log",
        "rtl_agent_summary.txt",
        "rtl_agent_exception.txt",
        "rtl_llm_raw_output.txt",
        "rtl_agent_compile_pass2.log",
        "rtl_verilator_lint_pass2.log",
        "rtl_agent_summary_pass2.txt",
        "rtl_agent_exception_pass2.txt",
        "rtl_llm_raw_output_pass2.txt",
        "rtl_agent_compile_pass3.log",
        "rtl_verilator_lint_pass3.log",
        "rtl_agent_summary_pass3.txt",
        "rtl_agent_exception_pass3.txt",
        "rtl_llm_raw_output_pass3.txt",
        "rtl_agent_compile_pass4.log",
        "rtl_verilator_lint_pass4.log",
        "rtl_agent_summary_pass4.txt",
        "rtl_agent_exception_pass4.txt",
        "rtl_llm_raw_output_pass4.txt",
        "rtl_quality_gate.json",
        "rtl_agent_final_status.log",
        "rtl_agent_final_summary.txt",
    ]:
        _record_text_artifact_safe(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="rtl",
            filename=fname,
            path=os.path.join(rtl_dir, fname),
        )

def _append_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)


def _targeted_rtl_repair_context(previous_llm_output: str, compile_log_text: str,
                                 verilator_log_text: str, expected_files: Optional[List[str]] = None) -> tuple[str, List[str]]:
    """Keep complete failing files in context instead of truncating a large hierarchy."""
    expected = {os.path.basename(str(name)): str(name) for name in (expected_files or [])}
    log_text = f"{compile_log_text or ''}\n{verilator_log_text or ''}"
    referenced: List[str] = []
    for match in re.finditer(r"(?i)(?:^|[\\/\s'(])(?:pass\d+[\\/])?([A-Za-z0-9_.-]+\.(?:sv|v))(?=:\d+|[\s')]|$)", log_text):
        basename = os.path.basename(match.group(1))
        if basename in expected and basename not in referenced:
            referenced.append(basename)
    if not referenced:
        return _truncate_text(previous_llm_output, 30000), list(expected.values())
    emitted = _parse_named_verilog_blocks(previous_llm_output)
    blocks: List[str] = []
    targets: List[str] = []
    for basename in referenced:
        content = emitted.get(basename)
        if content is None:
            continue
        blocks.append(f"---BEGIN {basename}---\n{content.rstrip()}\n---END {basename}---")
        targets.append(expected[basename])
    if not blocks:
        return _truncate_text(previous_llm_output, 30000), list(expected.values())
    return "\n\n".join(blocks), targets


def _build_rtl_repair_prompt(base_prompt: str, previous_llm_output: str, compile_log_text: str, verilator_log_text: str, expected_files: Optional[List[str]] = None) -> str:
    repair_context, repair_targets = _targeted_rtl_repair_context(
        previous_llm_output, compile_log_text, verilator_log_text, expected_files
    )
    expected_file_text = ", ".join(expected_files or []) or "the complete original file set"
    repair_target_text = ", ".join(repair_targets) or expected_file_text
    return f"""
ORIGINAL RTL GENERATION CONTRACT EXCERPT:
{_truncate_text(base_prompt, 12000)}

==============================
REPAIR MODE (BOUNDED RETRY)
==============================

Your previous RTL output failed one or more correctness gates.

You MUST preserve the same architecture unless a structural change is strictly required to fix the errors.

PREVIOUS RTL OUTPUT:
{repair_context}

ICARUS COMPILE LOG:
{_truncate_text(compile_log_text, 8000)}

FATAL VERILATOR LOG (if any):
{_truncate_text(verilator_log_text, 8000)}

REPAIR RULES:
- Do NOT redesign the architecture unless required to resolve correctness errors
- Preserve module names, filenames, ports, hierarchy, and wiring intent as much as possible
- Fix only:
  - Icarus compile failures
  - fatal Verilator errors
  - structural/spec mismatch issues
- Do NOT spend tokens cleaning non-fatal lint warnings only
- Expected hierarchy files: {expected_file_text}
- Repair target files: {repair_target_text}
- Return every repair target as a complete named-file block. Unlisted hierarchy files are preserved automatically.
- Do NOT return explanations
- Do NOT return snippets or diff fragments
- Preserve all implemented functional behavior and verification observability while repairing structural or tool errors.
- Never repair an error by tying a functional output, enable, request, interrupt, status, readback, or state transition to a constant.
- Never replace a functional block with an empty module, inactive branch, constant-output shell, or compile-only stub.
- After repair, every behavior required by DIGITAL_SPEC_JSON and DIGITAL_REGMAP_JSON must remain implemented and reachable through legal declared inputs.
- If DIGITAL_SPEC_JSON contains memory_macros[], that array overrides conflicting descriptive prose: instantiate each exact memory_macros[].name using its exact instance_name. Never substitute a wrapper, fallback model, inferred array, or invented SRAM module name.
- For a missing-module error at an SRAM instance, replace the invented module identity with the exact authoritative memory macro cell identity and preserve the declared macro port-role mapping.
- If the correctness log reports a functionally unreachable required memory, repair the application RTL so a legal declared input transaction can enable and address it and its read data reaches an observable functional path. Do not remove the memory, mark it unused, or merely rename the unused signal.

REQUIRED-MEMORY REPAIR EXAMPLES:
- BAD: assign mem_csb = 1'b1; assign mem_addr = '0; connect .dout(mem_dout_unused).
- BAD: keep the memory instantiated only to satisfy hierarchy while all behavior bypasses it.
- GOOD: derive select, write-enable, address, and write data from declared controller/register/stream state, and use read data in declared readback, buffering, response, or datapath behavior.
- GOOD: preserve active polarity and synchronous-read latency from the memory contract; make at least one bounded legal transaction exercise storage and expose its result.

PRIMARY OBJECTIVE:
Make the MINIMUM NECESSARY change to fix correctness errors without reducing functionality or verifiability.

- Do NOT redesign architecture
- Do NOT rename modules/ports/files
- Do not redesign or reproduce unaffected files
- Prefer local fixes over global rewrites

CORRECTNESS REPAIR PRIORITIES (MANDATORY)

TARGETED REPAIR PROCEDURE FOR FATAL LINT / COMPILE ERRORS

1. Read the exact failing signal names from the compile and Verilator logs.
2. For each failing signal, classify it into exactly one category:
   - structural wire / child-owned connection
   - combinational signal
   - sequential registered signal
3. Rewrite that signal so it uses exactly one legal driving style:
   - structural wire -> assign / module port wiring only
   - combinational signal -> blocking = only in exactly one always @(*)
   - sequential signal -> nonblocking <= only in exactly one clocked always block
4. Remove all conflicting assignments to that signal everywhere else.
5. If a signal is owned by a child module or by explicit signal_ownership, do not also reset, assign, or procedurally drive it in the parent/top.
6. If Verilator reports BLKANDNBLK, the repair is incomplete unless every reported signal has only one assignment style in the final RTL.
7. If Verilator reports MULTIDRIVEN or multiple procedural drivers, the repair is incomplete unless all duplicate drivers are removed from the final RTL.
8. If Icarus or Verilator reports unexpected '[' after an arithmetic/parenthesized expression, move that expression into a named wire/reg first and select bits from the named signal.
9. If BLKANDNBLK reports a register-file signal, remove the combinational blocking assignment to that stored register; keep the clocked nonblocking assignment, or introduce a separate *_next/read_mux signal.
10. If either tool reports an undeclared identifier, unable-to-bind signal, or missing variable, use the exact declared port/wire/register name already present in that module. Do not invent an alias or silently rename an interface signal.

MANDATORY REPAIR OVERRIDE
If the previous RTL uses an illegal ownership pattern, you MUST rewrite the affected block structure enough to eliminate the illegal drivers.
Preserving the previous block structure is NOT allowed if it leaves:
- one signal assigned in multiple always blocks
- one signal assigned in both clocked and combinational logic
- one signal driven both structurally and procedurally
- a child-owned signal driven again in the parent/top

SPECIAL REPAIR RULE FOR HIERARCHICAL TOPS
In hierarchical mode, if a top-level signal is sourced from a child output or child-owned internal signal:
- keep that signal as structural wiring only
- remove any top-level reset assignment, combinational assignment, or extra assign to that same signal

SPECIAL REPAIR RULE FOR REGISTER/STATUS LOGIC
If multiple clocked blocks update the same stored register or status/output register:
- merge those updates into one legal clocked always block
- do not keep separate clocked writers for the same signal

WARNING CLEANUP RULE FOR DECLARED PORTS
If Verilator reports:
- UNDRIVEN on a declared output, the repair is incomplete until that output has an explicit legal driver.
- UNUSEDSIGNAL on a declared input with behavioral meaning from the spec, the repair is incomplete until that input is functionally consumed or exposed through status/readback.
- CASEINCOMPLETE, the repair is incomplete until a default branch is present.

GOOD/BAD REPAIR EXAMPLES

BAD REPAIR:
always @(posedge clk or negedge rst_n) irq <= done;
always @(*) irq = fault;

GOOD REPAIR:
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) irq <= 1'b0;
  else irq <= (done | fault);
end

BAD REPAIR:
assign irq = child_irq;
always @(*) irq = masked_irq;

GOOD REPAIR:
assign irq = child_irq;

BAD REPAIR:
always @(posedge clk or negedge rst_n) status_reg <= next_status_a;
always @(posedge clk or negedge rst_n) status_reg <= next_status_b;

GOOD REPAIR:
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= RESET_VALUE;
  else status_reg <= merged_next_status;
end

PROCEDURAL OUTPUT REPAIR RULE
- If compile or lint shows PROCASSWIRE, rewrite the affected signal so that:
  - either it becomes a reg-style procedural output and remains procedurally assigned
  - or it is moved to a continuous assign path and is no longer assigned in always blocks
- The repair is incomplete if a procedurally assigned signal remains declared as a wire-style output.

REPAIR PRIORITY ORDER
1. BLKANDNBLK
2. MULTIDRIVEN / illegal multiple drivers
3. undeclared or illegal reg/wire usage
4. incomplete combinational assignments / latch-prone logic
5. case completeness / default branches
6. width mismatches

WARNING-ONLY LINT REPAIR RULE
- If compile passes and the Verilator log contains only warning classes such as UNUSEDSIGNAL, do not redesign the RTL.
- Prefer minimal local fixes only if they are straightforward and preserve behavior.
- Do not perform broad rewrites for warning-only lint.


When repairing RTL, fix these classes of issues first:


1. FSM / latch issues
- Eliminate latch-prone combinational FSM logic.
- In every combinational FSM block:
  - assign next_state a default value first
  - assign every combinational output a default value first
  - keep assignments complete across all branches
- Preserve the same FSM architecture and state names unless a change is strictly required.

2. Register map correctness
- Fix width mismatches by using explicit zero-padding or width-correct assignments.
- Add missing default branches in register decode case statements.
- Ensure reg_rdata and similar outputs are assigned deterministically.

3. Structural correctness only
- Preserve filenames, module names, ports, and hierarchy.
- Do NOT redesign the architecture to fix non-fatal lint style issues.
- Fix only correctness blockers:
  - compile failures
  - fatal lint/elaboration issues
  - spec/structure mismatches
  - latch-prone coding
  - width/signing mistakes that can break synthesis or behavior

  SIGNAL ROLE / MULTI-DRIVER REPAIR RULES

- If one signal is used for multiple unrelated roles, split it into separate signals.
- Introduce internal wires instead of reusing top-level ports.
- If a signal is produced by one module and consumed by another, connect via a dedicated internal wire.
- Never allow multiple drivers on the same signal.
- Do NOT fix wiring bugs by converting outputs to reg at top-level.

Fix only:
- Icarus compile failures
- fatal Verilator errors
- structural/spec mismatches
- latch-prone coding
- illegal multi-driver issues
- illegal reg/wire usage

Do NOT spend effort fixing non-fatal lint warnings.

SELF-CHECK BEFORE RETURNING RTL
- No latch inference from incomplete combinational assignments
- Every combinational case has a default branch
- Every combinationally driven output has a default assignment at block entry
- Packed status/control register assignments are width-correct
- Minimal changes applied
- No architecture changes
- No multi-driver signals
- No illegal reg/wire connections
- No latch inference
- Interfaces unchanged
""".strip()


def _build_structural_closure_prompt(base_prompt: str, previous_llm_output: str,
                                     compile_log_text: str, verilator_log_text: str,
                                     expected_files: Optional[List[str]] = None) -> str:
    """Final bounded retry focused only on remaining structural blockers."""
    blockers = [
        line.strip() for line in (verilator_log_text or "").splitlines()
        if re.search(r"%Warning-(?:UNDRIVEN|MULTIDRIVEN)\b|%Error:", line)
    ]
    blocker_text = "\n".join(blockers) or "See the supplied latest compile and Verilator logs."
    return _build_rtl_repair_prompt(
        base_prompt, previous_llm_output, compile_log_text, verilator_log_text, expected_files
    ) + f"""

==============================
FINAL STRUCTURAL CLOSURE PASS
==============================
This is the last bounded repair attempt. Do not redesign or clean warning-only style issues.
Repair every blocker listed below in the same response:
{blocker_text}

For every UNDRIVEN signal, trace all consumers and connect it to exactly one real producer that
implements the existing specification. A declaration or consumer connection is not a driver.
Do not tie functional status, data, fault, valid, ready, enable, or control signals to constants.
Before returning, audit every named blocker and confirm it has exactly one legal source.
""".strip()


def _run_verilator_lint(rtl_dir: str, verilog_files: List[str], top_module: str, suffix: str = "", state: Optional[dict] = None) -> Tuple[bool, str, str, dict]:
    log_name = "rtl_verilator_lint.log" if not suffix else f"rtl_verilator_lint_{suffix}.log"
    lint_log_path = os.path.join(rtl_dir, log_name)

    if not verilog_files:
        msg = "No Verilog files provided to Verilator lint.\n"
        with open(lint_log_path, "w", encoding="utf-8") as f:
            f.write(msg)
        return False, lint_log_path, msg, {}

    verilator_inputs = []
    for vf in verilog_files:
        if os.path.isabs(vf):
            verilator_inputs.append(vf)
        else:
            abs_vf = os.path.abspath(vf)
            abs_rtl_dir = os.path.abspath(rtl_dir)
            try:
                if os.path.commonpath([abs_rtl_dir, abs_vf]) == abs_rtl_dir:
                    verilator_inputs.append(os.path.relpath(abs_vf, abs_rtl_dir).replace("\\", "/"))
                else:
                    verilator_inputs.append(vf.replace("\\", "/"))
            except Exception:
                verilator_inputs.append(vf.replace("\\", "/"))

    args = [
        "--lint-only",
        "-Wall",
        "-Wno-fatal",
        "--top-module",
        top_module,
    ] + verilator_inputs

    logger.info(
        f"[RTL DEBUG] running_verilator_lint suffix={suffix or 'pass1'} "
        f"top={top_module} file_count={len(verilog_files)}"
    )
    logger.info(f"[RTL DEBUG] verilator_args={' '.join(args)}")
    result = run_tool(
        state or {},
        "rtl_lint",
        "verilator",
        args,
        cwd=rtl_dir,
        metadata={"agent": AGENT_NAME, "suffix": suffix or "pass1"},
    )

    combined = ""
    if result.stdout:
        combined += "=== STDOUT ===\n" + result.stdout + "\n"
    if result.stderr:
        combined += "=== STDERR ===\n" + result.stderr + "\n"
    if result.error:
        combined += "=== ERROR ===\n" + result.error + "\n"
    combined += f"=== RETURN CODE ===\n{result.returncode}\n"

    with open(lint_log_path, "w", encoding="utf-8") as f:
        f.write(combined)

    if result.returncode != 0 or result.status in {"tool_unavailable", "exception"}:
        logger.error(f"[RTL DEBUG] verilator_failed suffix={suffix or 'pass1'} rc={result.returncode}")
        return False, lint_log_path, combined, result.to_dict()

    logger.info(f"[RTL DEBUG] verilator_passed suffix={suffix or 'pass1'}")
    return True, lint_log_path, combined, result.to_dict()



def _classify_verilator_result(verilator_ok: bool, verilator_output: str) -> str:
    """
    Returns one of:
      - "pass"
      - "warning_only"
      - "fatal"
    """
    text = verilator_output or ""

    # Structural warnings are implementation blockers even when Verilator was
    # invoked with warning-fatal behavior disabled. OpenLane/Yosys promotes an
    # undriven bus to synthesis-check errors, so HEM must stop or repair here.
    if re.search(r"%Warning-(?:UNDRIVEN|MULTIDRIVEN)\b", text):
        return "fatal"

    if verilator_ok:
        return "pass"

    if "%Error:" in text:
        # Treat warnings-only termination as warning_only
        if "Exiting due to" in text and "warning(s)" in text and "%Error:" == "%Error:":
            non_warning_errors = [
                line for line in text.splitlines()
                if "%Error:" in line and "Exiting due to" not in line
            ]
            if not non_warning_errors:
                return "warning_only"
        return "fatal"

    fatal_patterns = [
        "Cannot find file containing module",
        "syntax error",
        "Internal Error",
    ]

    for pat in fatal_patterns:
        if pat in text:
            return "fatal"

    return "warning_only"


def _promote_rtl_files_to_root(rtl_dir: str, artifact_list: List[str]) -> List[str]:
    promoted = []
    os.makedirs(rtl_dir, exist_ok=True)

    for src in artifact_list:
        dst = os.path.join(rtl_dir, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
        promoted.append(dst)

    return promoted

def _validate_and_materialize_rtl(
    llm_output: str,
    rtl_dir: str,
    spec_json: dict,
    mode: str,
    suffix: str = "",
    materialize_subdir: str = "",
    state: Optional[dict] = None,
) -> dict:
    raw_name = "rtl_llm_raw_output.txt" if not suffix else f"rtl_llm_raw_output_{suffix}.txt"
    compile_log_name = "rtl_agent_compile.log" if not suffix else f"rtl_agent_compile_{suffix}.log"
    summary_name = "rtl_agent_summary.txt" if not suffix else f"rtl_agent_summary_{suffix}.txt"

    raw_output_path = os.path.join(rtl_dir, raw_name)
    with open(raw_output_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    verilog_map = _parse_named_verilog_blocks(llm_output)
    if not verilog_map:
        return {
            "ok": False,
            "message": "LLM output did not contain any named Verilog file blocks in the required format.",
            "issues": ["❌ Missing named Verilog file blocks in LLM output."],
            "compile_log_path": os.path.join(rtl_dir, compile_log_name),
            "summary_path": os.path.join(rtl_dir, summary_name),
            "raw_output_path": raw_output_path,
            "artifact_list": [],
        }

    expected_files = _collect_expected_rtl_files(spec_json, mode)
    verilog_map = _normalize_emitted_rtl_filenames(verilog_map, expected_files)
    verilog_map = _align_verilog_map_to_expected_modules(verilog_map, spec_json, mode)
    verilog_map = _align_memory_macro_instance_ports(verilog_map, spec_json)
    verilog_map = _repair_directional_port_aliases_from_spec(verilog_map, spec_json, mode)
    verilog_map = _repair_module_port_directions_from_spec(verilog_map, spec_json, mode)
    verilog_map = _remove_writes_to_spec_input_ports(verilog_map, spec_json, mode)
    verilog_map = _sanitize_single_driver_rtl(verilog_map)
    verilog_map = _remove_spec_invalid_extra_control_inputs(verilog_map, spec_json, mode)
    verilog_map = _sanitize_child_output_instance_connections(verilog_map)
    verilog_map = _align_spec_inter_module_wire_widths(verilog_map, spec_json, mode)
    verilog_map = _connect_spec_inter_module_signals(verilog_map, spec_json, mode)
    verilog_map = _connect_top_output_feedback_to_matching_child_input(verilog_map, spec_json, mode)
    verilog_map = _repair_undriven_inflight_state(verilog_map, spec_json, mode)
    verilog_map = _repair_undriven_last_accepted_observations(verilog_map, spec_json, mode)
    verilog_map = _trim_zero_padded_assign_concats(verilog_map)
    # Wiring/feedback sanitizers above may create a convenient alias after the
    # initial direction repair. Re-assert the authoritative port contract at
    # the end so no generated continuous or procedural assignment can drive a
    # spec-declared input (Verilator ASSIGNIN).
    verilog_map = _remove_writes_to_spec_input_ports(verilog_map, spec_json, mode)
    # Run last: earlier interface/width alignment may reconstruct a declaration
    # and accidentally drop the variable qualifier from a procedural output.
    verilog_map = _promote_procedurally_assigned_outputs(verilog_map)
    artifact_list = []

    materialize_dir = rtl_dir if not materialize_subdir else os.path.join(rtl_dir, materialize_subdir)
    os.makedirs(materialize_dir, exist_ok=True)
    for fname in expected_files:
        code = verilog_map.get(fname)
        if not code:
            continue
        fpath = os.path.join(materialize_dir, fname)
        with open(fpath, "w", encoding="utf-8") as vf:
            vf.write(code + "\n")
        artifact_list.append(fpath)

    # FPGA BRAM wrappers are declared implementation components. Keep them in
    # the deliverable RTL file set so synthesis receives the same closure that
    # compile/lint validated.
    artifact_list.extend(_materialize_declared_fpga_bram_wrappers(spec_json, materialize_dir))
    artifact_list = sorted(dict.fromkeys(artifact_list))

    top_rtl_file = _top_rtl_file(spec_json, mode)
    top_rtl_path = os.path.join(materialize_dir, top_rtl_file)

 

    issues, clock_ports, reset_ports = _validate_spec_vs_rtl(spec_json, mode, verilog_map)

    forbidden_sv_patterns = [
        r"\btypedef\b",
        r"\benum\b",
        r"\blogic\b",
        r"\balways_comb\b",
        r"\balways_ff\b",
        r"\bstruct\b",
        r"\bunion\b",
    ]

    full_text = "\n".join(verilog_map.values())
    scan_text = _strip_verilog_comments(full_text)

    issues.extend(_validate_generated_complexity(spec_json, mode, verilog_map))
    issues.extend(_validate_memory_macro_instances(spec_json, verilog_map))
    issues.extend(_validate_memory_macro_reachability(spec_json, verilog_map))

    for pat in forbidden_sv_patterns:
        if pat == r"\blogic\b":
            if re.search(r"\blogic\s+(\[[^\]]+\]\s+)?[A-Za-z_]\w*", scan_text):
                issues.append(f"❌ Forbidden SystemVerilog construct found in RTL: pattern '{pat}'")
        else:
            if re.search(pat, scan_text):
                issues.append(f"❌ Forbidden SystemVerilog construct found in RTL: pattern '{pat}'")

    suspicious_grouped_buses = [
        "reg_bus_signals",
        "reg_bus",
        "ctrl_bus",
        "status_bus",
    ]
    spec_text = json.dumps(spec_json)
    for name in suspicious_grouped_buses:
        if re.search(rf"\b{re.escape(name)}\b", full_text) and not re.search(rf"\b{re.escape(name)}\b", spec_text):
            issues.append(f"❌ Invented grouped bus '{name}' found in RTL but not declared in spec.")


    top_rtl_file = _top_rtl_file(spec_json, mode)
    top_rtl_path = os.path.join(materialize_dir, top_rtl_file)

    compile_log_path = os.path.join(rtl_dir, compile_log_name)
    compile_status = "Compile not run yet."

    if not os.path.exists(top_rtl_path):
        issues.append(f"❌ Top RTL file missing after generation: {top_rtl_file}")
    if not artifact_list:
        issues.append("❌ No RTL files materialized to disk.")

    external_model_files = _stage_memory_macro_models_for_rtl_validation(spec_json, rtl_dir, suffix=suffix)
    validation_files = sorted(dict.fromkeys([*artifact_list, *external_model_files]))

    if mode == "hierarchical":
        top_file = _top_rtl_file(spec_json, mode)
        top_name = _top_module_name(spec_json, mode)
        top_code = _module_code_for_name(verilog_map.get(top_file, ""), top_name)
        owned_top_signals = []

        for o in spec_json.get("signal_ownership", []):
            sig = _normalize_signal_token(o.get("signal", ""))
            owner = o.get("owner", "")
            if owner and "." in owner:
                omod, _ = owner.split(".", 1)
                if omod != top_name:
                    owned_top_signals.append(sig)

        for sig in set(owned_top_signals):
            if _module_procedurally_assigns_signal(top_code, sig):
                issues.append(f"❌ Top module appears to procedurally drive child-owned signal '{sig}'.")

        _stage(f"iverilog_compile_start_{suffix or 'pass1'}")
    compile_args = [
        "-g2005",
        "-o",
        os.path.join(rtl_dir, f"rtl_out{('_' + suffix) if suffix else ''}")
    ] + validation_files

    iverilog_failed = False
    tool_executions = []
    try:
        cp = run_tool(
            state or {},
            "rtl_compile",
            "iverilog",
            compile_args,
            metadata={"agent": AGENT_NAME, "suffix": suffix or "pass1"},
        )
        tool_executions.append(cp.to_dict())
        compile_status = (cp.stdout or "") + "\n" + (cp.stderr or "")
        if cp.error:
            compile_status += "\n" + cp.error
        if cp.returncode != 0 or cp.status in {"tool_unavailable", "exception"}:
            iverilog_failed = True
            issues.append("❌ Icarus Verilog compile failed.")
            _stage(f"iverilog_compile_failed_{suffix or 'pass1'}")
        elif _has_structural_width_warnings(compile_status):
            iverilog_failed = True
            issues.append("Structural port width mismatch warnings reported by Icarus Verilog.")
            compile_status += "\nStructural port width mismatch warnings are treated as RTL failures.\n"
            _stage(f"iverilog_compile_width_mismatch_{suffix or 'pass1'}")
        else:
            _stage(f"iverilog_compile_passed_{suffix or 'pass1'}")
    except Exception as e:
        iverilog_failed = True
        compile_status = f"Compile invocation failed: {e}"
        issues.append(f"❌ Compile invocation failed: {e}")
        _stage(f"iverilog_compile_exception_{suffix or 'pass1'}")

    with open(compile_log_path, "w", encoding="utf-8") as logf:
        logf.write(compile_status.strip() + "\n")
        if issues:
            logf.write("\nIssues:\n")
            for issue in issues:
                logf.write(f"{issue}\n")

    verilator_ok = False
    verilator_log_path = os.path.join(
        rtl_dir,
        "rtl_verilator_lint.log" if not suffix else f"rtl_verilator_lint_{suffix}.log"
    )
    verilator_output = ""
    verilator_severity = "not_run"

    _stage(f"running_verilator_lint_{suffix or 'pass1'}")
    verilator_ok, verilator_log_path, verilator_output, verilator_result = _run_verilator_lint(
        rtl_dir=rtl_dir,
        verilog_files=validation_files,
        top_module=_top_module_name(spec_json, mode),
        suffix=suffix,
        state=state,
    )
    if verilator_result:
        tool_executions.append(verilator_result)

    verilator_severity = _classify_verilator_result(verilator_ok, verilator_output)

    if verilator_severity == "fatal":
        issues.append("❌ Verilator lint failed.")
        _append_text(
            compile_log_path,
            "\n=== VERILATOR LINT FAILURE ===\n"
            "Fatal Verilator issues detected. See corresponding rtl_verilator_lint log for details.\n"
        )
        _stage(f"verilator_lint_fatal_{suffix or 'pass1'}")

    elif verilator_severity == "warning_only":
        _append_text(
            compile_log_path,
            "\n=== VERILATOR LINT WARNINGS ===\n"
            "Non-fatal Verilator warnings detected.\n"
        )
        _stage(f"verilator_lint_warning_only_{suffix or 'pass1'}")

    else:
        _append_text(
            compile_log_path,
            "\n=== VERILATOR LINT ===\n"
            "PASS: Verilator lint completed successfully.\n"
        )
        _stage(f"verilator_lint_passed_{suffix or 'pass1'}")



    summary_path = os.path.join(rtl_dir, summary_name)
    with open(summary_path, "w", encoding="utf-8") as sf:
        sf.write("RTL Agent Summary\n")
        sf.write("=================\n")
        sf.write(f"Mode: {mode}\n")
        sf.write(f"Top module: {_top_module_name(spec_json, mode)}\n")
        sf.write(f"Expected files: {expected_files}\n")
        sf.write(f"Materialized files: {[os.path.basename(p) for p in artifact_list]}\n")
        if external_model_files:
            sf.write(f"External validation models: {[os.path.basename(p) for p in external_model_files]}\n")
        sf.write(f"Clock ports: {sorted(set(clock_ports))}\n")
        sf.write(f"Reset ports: {sorted(set(reset_ports))}\n")
        sf.write(f"Icarus compile: {'fail' if iverilog_failed else 'pass'}\n")
        sf.write(f"Verilator lint: {verilator_severity}\n")
        sf.write(f"Issue count: {len(issues)}\n")
        if issues:
            sf.write("\nIssues:\n")
            for issue in issues:
                sf.write(f"- {issue}\n")


    return {
        "ok": len(issues) == 0,
        "message": "RTL checks passed." if len(issues) == 0 else "RTL checks failed.",
        "issues": issues,
        "artifact_list": artifact_list,
        "external_model_files": external_model_files,
        "clock_ports": sorted(set(clock_ports)),
        "reset_ports": sorted(set(reset_ports)),
        "compile_log_path": compile_log_path,
        "summary_path": summary_path,
        "raw_output_path": raw_output_path,
        "verilator_log_path": verilator_log_path,
        "verilator_output": verilator_output,
        "verilator_severity": verilator_severity,
        "tool_profile": profile_summary(state or {}),
        "tool_executions": tool_executions,
        "llm_output": llm_output,
        "verilog_map": verilog_map,
    }


def _run(context: AgentContext) -> dict:
    state = context.state
    agent_name = context.agent_name
    print("\n🧠 Running RTL Agent (implementation mode).")



    _stage("entered_run_agent")

    workflow_id = context.workflow_id
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Restore local directory structure
    rtl_dir = os.path.join(workflow_dir, "rtl")
    os.makedirs(rtl_dir, exist_ok=True)

    def _fail_and_upload(msg: str, exc: Exception = None) -> dict:
        # Do NOT overwrite pass1/pass2 logs. Preserve them.
        final_log_path = os.path.join(rtl_dir, "rtl_agent_final_status.log")
        final_summary_path = os.path.join(rtl_dir, "rtl_agent_final_summary.txt")
        error_file = os.path.join(rtl_dir, "rtl_agent_exception.txt")
        quality_gate_path = os.path.join(rtl_dir, "rtl_quality_gate.json")
        failed_quality_gate = {
            "passed": False,
            "compile_passed": False,
            "lint_passed": False,
            "final_pass": "failed",
            "reason": msg,
        }

        with open(final_log_path, "w", encoding="utf-8") as lf:
            lf.write(msg + "\n")
            if exc is not None:
                lf.write(f"Exception type: {type(exc).__name__}\n")
                lf.write(f"Exception: {exc}\n")

        with open(final_summary_path, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation failed.\n\n")
            sf.write(msg + "\n")
            if exc is not None:
                sf.write(f"Exception type: {type(exc).__name__}\n")
                sf.write(f"Exception: {exc}\n")

        if exc is not None:
            with open(error_file, "w", encoding="utf-8") as ef:
                ef.write(repr(exc) + "\n")
        with open(quality_gate_path, "w", encoding="utf-8") as qf:
            json.dump(failed_quality_gate, qf, indent=2)

        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        _record_text_artifact_safe(workflow_id, agent_name, "rtl", "rtl_agent_final_status.log", final_log_path)
        _record_text_artifact_safe(workflow_id, agent_name, "rtl", "rtl_agent_final_summary.txt", final_summary_path)

        state.update({
            "status": f"❌ RTL generation failed: {msg}",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": final_log_path,
            "issues": [msg] + ([str(exc)] if exc is not None else []),
            "rtl_quality_gate": failed_quality_gate,
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        return state

    entry_log = os.path.join(rtl_dir, "rtl_agent_entry.json")
    with open(entry_log, "w", encoding="utf-8") as ef:
        json.dump({
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
            "digital_spec_json": state.get("digital_spec_json"),
            "spec_json": state.get("spec_json"),
            "digital_spec_json_exists": isinstance(state.get("digital_spec_json"), str) and os.path.exists(state.get("digital_spec_json", "")),
            "spec_json_exists": isinstance(state.get("spec_json"), str) and os.path.exists(state.get("spec_json", "")),
        }, ef, indent=2)

    spec_path = None
    _stage("loading_spec")
    spec_obj = _load_json_if_path(state.get("digital_spec_json"))
    _stage(f"spec_loaded: {spec_obj is not None}")
    if spec_obj is None:
        spec_obj = _load_json_if_path(state.get("spec_json"))
    if spec_obj is None:
        spec_path = _find_fallback_spec_json(workflow_dir)
        _stage("checking_fallback_spec")
        spec_obj = _load_json_if_path(spec_path)

    if not spec_obj:
        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("RTL agent could not locate spec JSON.\n")
            lf.write(f"digital_spec_json={state.get('digital_spec_json')}\n")
            lf.write(f"spec_json={state.get('spec_json')}\n")
            lf.write(f"fallback_spec_json={spec_path}\n")
        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation aborted: missing spec JSON.\n")
        state.update({
            "status": "❌ Missing digital spec JSON for RTL generation.",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "issues": ["Missing digital spec JSON for RTL generation."],
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        return state

    try:
        _stage("normalizing_spec")
        spec_json, mode = _normalize_spec_json(spec_obj)
        spec_json = _reconcile_hierarchical_signal_directions(spec_json, mode)
        source_spec_text = state.get("spec_text") or state.get("spec") or state.get("digital_spec_text")
        if isinstance(source_spec_text, str) and source_spec_text.strip():
            spec_json["_source_spec_text"] = source_spec_text.strip()
        _stage(f"normalized_spec: mode={mode}")
    except Exception as e:
        return _fail_and_upload("Spec JSON normalization failed.", e)
    _stage("validating_connectivity")
    pre_issues = _validate_connectivity_contract(spec_json, mode)
    _stage(f"connectivity_valid: {not pre_issues}")
    if pre_issues:
        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("RTL agent aborted due to spec connectivity contract violations:\n")
            for issue in pre_issues:
                lf.write(f"{issue}\n")
        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation aborted due to invalid spec connectivity contract.\n\n")
            for issue in pre_issues:
                sf.write(f"{issue}\n")

        state.update({
            "status": "❌ Invalid spec connectivity contract for RTL generation.",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "port_list": [],
            "clock_ports": [],
            "reset_ports": [],
            "issues": pre_issues,
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        return state

    _stage("loading_regmap")

    regmap_obj = (
        _load_json_if_path(state.get("digital_regmap_json"))
        or _load_json_if_path(state.get("digital_regmap"))
    )
    _stage(f"regmap_loaded: {regmap_obj is not None}")

    _stage("loading_clock_reset")

    clock_reset_obj = _load_json_if_path(state.get("clock_reset_arch_path"))

    _stage(f"clock_reset_loaded: {clock_reset_obj is not None}")

    _stage("loading_power_intent")

    power_intent_obj = None
    signoff_obj = state.get("signoff")

    _stage(f"signoff_type: {type(signoff_obj)}")

    if isinstance(signoff_obj, dict):
        pi = signoff_obj.get("power_intent")
        _stage(f"power_intent_type: {type(pi)}")
        if isinstance(pi, dict):
            power_intent_obj = pi

    _stage(f"power_intent_loaded: {power_intent_obj is not None}")


    _stage("building_prompt")
    try:
        prompt = _build_generation_prompt(spec_json, mode, regmap_obj, clock_reset_obj, power_intent_obj)
    except Exception as e:
        logger.exception("[RTL DEBUG] prompt build failed")
        return _fail_and_upload("RTL prompt build failed.", e)
    _stage(f"prompt_length: {len(prompt)}")

    
    _stage("writing_preflight")

    preflight_path = os.path.join(rtl_dir, "rtl_agent_preflight.json")
    with open(preflight_path, "w", encoding="utf-8") as pf:
        json.dump({
            "mode": mode,
            "top_module": _top_module_name(spec_json, mode),
            "expected_files": _collect_expected_rtl_files(spec_json, mode),
            "has_regmap": regmap_obj is not None,
            "has_clock_reset": clock_reset_obj is not None,
            "has_power_intent": power_intent_obj is not None,
            "prompt_chars": len(prompt),
        }, pf, indent=2)
    _stage("preflight_written")

    _stage("starting_llm_call")


    try:
        t0 = time.monotonic()
        llm_output = _complete_rtl_text(
            prompt, agent_name=agent_name, state=state, stage_label="llm_pass1"
        )
        _stage(f"llm_pass1_elapsed_sec: {time.monotonic() - t0:.2f}")
        _stage(f"llm_output_pass1_chars: {len(llm_output)}")
    except Exception as e:
        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        error_file = os.path.join(rtl_dir, "rtl_agent_exception.txt")

        with open(error_file, "w", encoding="utf-8") as ef:
            ef.write(f"RTL generation exception:\n{repr(e)}\n")

        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("RTL agent failed before RTL materialization.\n")
            lf.write(f"Exception type: {type(e).__name__}\n")
            lf.write(f"Exception: {e}\n")

        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation failed before raw output was written.\n")
            sf.write(f"Exception type: {type(e).__name__}\n")
            sf.write(f"Exception: {e}\n")

        state.update({
            "status": f"❌ RTL generation failed: {e}",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "issues": [f"RTL generation failed: {e}"],
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        return state
    try:
        _stage("pass1_validate_and_materialize")

        pass1 = _validate_and_materialize_rtl(
            llm_output=llm_output,
            rtl_dir=rtl_dir,
            spec_json=spec_json,
            mode=mode,
            suffix="",
            materialize_subdir="",      # keep pass1 exactly as today
            state=state,
        )


        if not pass1["ok"]:
            _stage("pass1_failed_triggering_pass2")
            logger.warning(f"[RTL DEBUG] pass1_failed issues={len(pass1['issues'])}")

            compile_log_text = ""
            if os.path.exists(pass1["compile_log_path"]):
                with open(pass1["compile_log_path"], "r", encoding="utf-8") as f:
                    compile_log_text = f.read()

            verilator_log_text = ""
            if pass1.get("verilator_severity") == "fatal" and os.path.exists(pass1["verilator_log_path"]):
                with open(pass1["verilator_log_path"], "r", encoding="utf-8") as f:
                    verilator_log_text = f.read()

            repair_prompt = _build_rtl_repair_prompt(
                base_prompt=prompt,
                previous_llm_output=llm_output,
                compile_log_text=compile_log_text,
                verilator_log_text=verilator_log_text,
                expected_files=_collect_expected_rtl_files(spec_json, mode),
            )


            _stage("starting_llm_call_pass2")

            
            try:
                _stage(f"repair_prompt_length: {len(repair_prompt)}")
                t0 = time.monotonic()
                llm_output_pass2 = _complete_rtl_text(
                    repair_prompt, agent_name=agent_name, state=state, stage_label="llm_pass2"
                )
                _stage(f"llm_pass2_elapsed_sec: {time.monotonic() - t0:.2f}")
                _stage(f"llm_output_pass2_chars: {len(llm_output_pass2)}")
            except Exception as e2:
                pass2_exc = os.path.join(rtl_dir, "rtl_agent_exception_pass2.txt")
                pass2_log = os.path.join(rtl_dir, "rtl_agent_compile_pass2.log")
                pass2_summary = os.path.join(rtl_dir, "rtl_agent_summary_pass2.txt")

                with open(pass2_exc, "w", encoding="utf-8") as ef:
                    ef.write(f"RTL pass2 generation exception:\n{repr(e2)}\n")

                with open(pass2_log, "w", encoding="utf-8") as lf:
                    lf.write("RTL pass2 failed before RTL materialization.\n")
                    lf.write(f"Exception type: {type(e2).__name__}\n")
                    lf.write(f"Exception: {e2}\n")

                with open(pass2_summary, "w", encoding="utf-8") as sf:
                    sf.write("❌ RTL pass2 generation failed before raw output was written.\n")
                    sf.write(f"Exception type: {type(e2).__name__}\n")
                    sf.write(f"Exception: {e2}\n")

                return _fail_and_upload("Pass1 failed and Pass2 LLM generation failed.", e2)

            llm_output_pass2 = _merge_rtl_repair_output(
                llm_output,
                llm_output_pass2,
                _collect_expected_rtl_files(spec_json, mode),
            )
            _stage("pass2_validate_and_materialize")

            pass2 = _validate_and_materialize_rtl(
                llm_output=llm_output_pass2,
                rtl_dir=rtl_dir,
                spec_json=spec_json,
                mode=mode,
                suffix="pass2",
                materialize_subdir="pass2", # isolate pass2 RTL
                state=state,
            )

            final_result = pass2
            final_suffix = "pass2"

            if not pass2["ok"]:
                # A first repair can fix the original syntax problem while
                # exposing a smaller elaboration error (for example one
                # undeclared identifier). Give the model one bounded retry
                # using the *latest* tool logs instead of failing the complete
                # Physical AI journey on that correctable residual error.
                pass2_compile_log = ""
                if os.path.exists(pass2["compile_log_path"]):
                    with open(pass2["compile_log_path"], "r", encoding="utf-8") as f:
                        pass2_compile_log = f.read()
                pass2_verilator_log = ""
                if os.path.exists(pass2["verilator_log_path"]):
                    with open(pass2["verilator_log_path"], "r", encoding="utf-8") as f:
                        pass2_verilator_log = f.read()

                repair_prompt_pass3 = _build_rtl_repair_prompt(
                    base_prompt=prompt,
                    previous_llm_output=llm_output_pass2,
                    compile_log_text=pass2_compile_log,
                    verilator_log_text=pass2_verilator_log,
                    expected_files=_collect_expected_rtl_files(spec_json, mode),
                )
                _stage("starting_llm_call_pass3")
                try:
                    llm_output_pass3 = _complete_rtl_text(
                        repair_prompt_pass3, agent_name=agent_name, state=state, stage_label="llm_pass3"
                    )
                except Exception as e3:
                    return _fail_and_upload("Pass2 failed and Pass3 LLM generation failed.", e3)

                llm_output_pass3 = _merge_rtl_repair_output(
                    llm_output_pass2,
                    llm_output_pass3,
                    _collect_expected_rtl_files(spec_json, mode),
                )

                pass3 = _validate_and_materialize_rtl(
                    llm_output=llm_output_pass3,
                    rtl_dir=rtl_dir,
                    spec_json=spec_json,
                    mode=mode,
                    suffix="pass3",
                    materialize_subdir="pass3",
                    state=state,
                )
                if not pass3["ok"]:
                    pass3_compile_log = ""
                    if os.path.exists(pass3["compile_log_path"]):
                        with open(pass3["compile_log_path"], "r", encoding="utf-8") as f:
                            pass3_compile_log = f.read()
                    pass3_verilator_log = ""
                    if os.path.exists(pass3["verilator_log_path"]):
                        with open(pass3["verilator_log_path"], "r", encoding="utf-8") as f:
                            pass3_verilator_log = f.read()
                    repair_prompt_pass4 = _build_structural_closure_prompt(
                        prompt, llm_output_pass3, pass3_compile_log, pass3_verilator_log,
                        _collect_expected_rtl_files(spec_json, mode),
                    )
                    _stage("starting_llm_call_pass4_structural_closure")
                    try:
                        llm_output_pass4 = _complete_rtl_text(
                            repair_prompt_pass4, agent_name=agent_name, state=state, stage_label="llm_pass4"
                        )
                    except Exception as e4:
                        return _fail_and_upload("Pass3 failed and Pass4 structural closure generation failed.", e4)
                    llm_output_pass4 = _merge_rtl_repair_output(
                        llm_output_pass3, llm_output_pass4,
                        _collect_expected_rtl_files(spec_json, mode),
                    )
                    pass4 = _validate_and_materialize_rtl(
                        llm_output=llm_output_pass4,
                        rtl_dir=rtl_dir,
                        spec_json=spec_json,
                        mode=mode,
                        suffix="pass4",
                        materialize_subdir="pass4",
                        state=state,
                    )
                    if not pass4["ok"]:
                        return _fail_and_upload("RTL failed checks in pass1 through pass4.")
                    final_result = pass4
                    final_suffix = "pass4"
                else:
                    final_result = pass3
                    final_suffix = "pass3"

            promoted_files = _promote_rtl_files_to_root(rtl_dir, final_result["artifact_list"])
            final_result["artifact_list"] = promoted_files

            _stage("pass2_passed")
        else:
            final_result = pass1
            final_suffix = "pass1"

        artifact_list = final_result["artifact_list"]
        log_path = final_result["compile_log_path"]
        clock_ports = final_result["clock_ports"]
        reset_ports = final_result["reset_ports"]
        issues = final_result["issues"]

        for path in artifact_list:
            try:
                with open(path, "r", encoding="utf-8") as vf:
                    save_text_artifact_and_record(
                        workflow_id=workflow_id,
                        agent_name=agent_name,
                        subdir="rtl",
                        filename=os.path.basename(path),
                        content=vf.read(),
                    )
            except Exception as e:
                print(f"⚠️ Failed to upload RTL artifact {path}: {e}")

        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        tool_profile_text = json.dumps(final_result.get("tool_profile") or profile_summary(state), indent=2)
        tool_summary = {
            "agent": agent_name,
            "tool_profile": final_result.get("tool_profile") or profile_summary(state),
            "executions": final_result.get("tool_executions") or [],
        }
        rtl_quality_gate = {
            "passed": True,
            "compile_passed": True,
            "lint_passed": True,
            "final_pass": final_suffix,
            "issue_count": len(issues),
        }
        tool_summary_text = json.dumps(tool_summary, indent=2)
        with open(os.path.join(rtl_dir, "tool_profile_used.json"), "w", encoding="utf-8") as f:
            f.write(tool_profile_text)
        with open(os.path.join(rtl_dir, "tool_execution_summary.json"), "w", encoding="utf-8") as f:
            f.write(tool_summary_text)
        save_text_artifact_and_record(workflow_id, agent_name, "rtl", "tool_profile_used.json", tool_profile_text)
        save_text_artifact_and_record(workflow_id, agent_name, "rtl", "tool_execution_summary.json", tool_summary_text)
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "rtl",
            "rtl_quality_gate.json",
            json.dumps(rtl_quality_gate, indent=2),
        )

        state.update({
            "rtl_output_dir": rtl_dir,
            "rtl_files": artifact_list,
            "artifact": rtl_dir,
            "artifact_list": artifact_list,
            "artifact_log": log_path,
            "port_list": sorted(set(clock_ports + reset_ports)),
            "clock_ports": sorted(set(clock_ports)),
            "reset_ports": sorted(set(reset_ports)),
            "issues": issues,
            "status": f"✅ RTL generation complete ({final_suffix})" if not issues else f"⚠ RTL generation completed with issues ({final_suffix})",
            "digital_rtl_generated": True,
            "rtl_quality_gate": rtl_quality_gate,
            "digital_rtl_dir": rtl_dir,
            "tool_profile": final_result.get("tool_profile") or profile_summary(state),
            "tool_execution_summary": tool_summary,
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        return state

    except Exception as e:
        return _fail_and_upload("Unhandled RTL agent exception after LLM generation.", e)


def run_agent(state: dict) -> dict:
    context = AgentContext.from_state(state, AGENT_NAME)
    if state.get(RUNTIME_ACTIVE_STATE_KEY):
        return _run(context)
    result = execute_agent(context, _run)
    state.update(result.to_state_update())
    return state

  
