import os
import json
import re
from utils.artifact_utils import save_text_artifact_and_record
from model_gateway import complete_text
import logging
import time
from json import JSONDecodeError


PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")


logger = logging.getLogger("chiploop")


_VERILOG_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _default_rtl_output_file(module_name: str) -> str:
    name = str(module_name or "").strip()
    return f"{name}.v" if name else ""


def _memory_macro_module(macro: dict) -> dict:
    name = str(macro.get("name") or "").strip()
    data_width = int(macro.get("data_width") or macro.get("width") or 1)
    addr_width = int(macro.get("addr_width") or 1)
    port_map = macro.get("ports") if isinstance(macro.get("ports"), dict) else {}
    semantic_ports = port_map or {
        "clk": "clk",
        "csb": "csb",
        "web": "web",
        "addr": "addr",
        "din": "din",
        "dout": "dout",
    }

    def port_width(role: str) -> int:
        role_l = role.lower()
        if role_l in {"addr", "address"}:
            return max(addr_width, 1)
        if role_l in {"din", "dout", "data_in", "data_out", "wdata", "rdata"}:
            return max(data_width, 1)
        return 1

    def port_direction(role: str) -> str:
        role_l = role.lower()
        if role_l in {"dout", "data_out", "rdata", "q"}:
            return "output"
        return "input"

    ports = []
    seen = set()
    for role, port_name in semantic_ports.items():
        pname = str(port_name or "").strip()
        if not pname or pname in seen:
            continue
        seen.add(pname)
        ports.append({"name": pname, "direction": port_direction(str(role)), "width": port_width(str(role))})

    return {
        "name": name,
        "description": "Memory macro interface module derived from memory_macros.",
        "ports": ports,
        "functionality": "Single-port memory macro abstraction.",
        "responsibilities": ["Expose the memory macro interface for RTL generation and downstream collateral."],
        "must_drive": [p["name"] for p in ports if p["direction"] == "output"],
        "must_receive": [p["name"] for p in ports if p["direction"] == "input"],
        "must_not_drive": [p["name"] for p in ports if p["direction"] == "input"],
        "reset_behavior": "Memory macro contents are not reset by the controller reset.",
        "behavior_rules": ["Preserve the declared memory macro port interface."],
        "rtl_output_file": _default_rtl_output_file(name),
    }


def _coerce_prompt_value(value: str):
    text = str(value or "").strip().rstrip(".")
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    return text


def _extract_memory_macros_from_prompt(prompt_text: str) -> list[dict]:
    macros: dict[int, dict] = {}
    pattern = re.compile(r"memory_macros\[(\d+)\]\.([A-Za-z0-9_.]+)\s*=\s*([^\r\n]+)")
    for match in pattern.finditer(prompt_text or ""):
        idx = int(match.group(1))
        key_path = match.group(2).strip().split(".")
        value = _coerce_prompt_value(match.group(3))
        macro = macros.setdefault(idx, {})
        target = macro
        for key in key_path[:-1]:
            target = target.setdefault(key, {})
        target[key_path[-1]] = value
    return [macros[idx] for idx in sorted(macros) if macros[idx].get("name")]


def _parse_prompt_port_line(line: str, direction: str) -> dict | None:
    item = re.sub(r"^\s*[-*]\s*", "", line or "").strip()
    if not item:
        return None
    match = re.match(r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)(?:\s*\[\s*(?P<msb>\d+)\s*:\s*(?P<lsb>\d+)\s*\])?\s*$", item)
    if not match:
        return None
    width = 1
    if match.group("msb") is not None and match.group("lsb") is not None:
        width = abs(int(match.group("msb")) - int(match.group("lsb"))) + 1
    port = {"name": match.group("name"), "direction": direction, "width": width}
    if port["name"].lower() in {"reset_n", "rst_n"}:
        port["active_low"] = True
    return port


def _extract_top_ports_from_prompt(prompt_text: str) -> list[dict]:
    ports: list[dict] = []
    seen: set[str] = set()
    current_direction: str | None = None
    for raw in (prompt_text or "").splitlines():
        line = raw.strip()
        lower = line.lower()
        if re.fullmatch(r"inputs?\s*:", lower):
            current_direction = "input"
            continue
        if re.fullmatch(r"outputs?\s*:", lower):
            current_direction = "output"
            continue
        if current_direction and re.match(r"^[A-Za-z][A-Za-z0-9 _/-]*:\s*$", line):
            current_direction = None
            continue
        if not current_direction:
            continue
        port = _parse_prompt_port_line(line, current_direction)
        if port and port["name"] not in seen:
            ports.append(port)
            seen.add(port["name"])
    return ports


def _repair_top_ports_from_prompt(spec_json: dict, mode: str, source_prompt: str) -> dict:
    prompt_ports = _extract_top_ports_from_prompt(source_prompt)
    if not prompt_ports:
        return spec_json
    prompt_names = {p["name"] for p in prompt_ports}
    if mode == "flat":
        if not spec_json.get("ports"):
            spec_json["ports"] = prompt_ports
        else:
            spec_json["ports"] = [
                {**next((p for p in spec_json.get("ports", []) if isinstance(p, dict) and p.get("name") == prompt_port["name"]), {}), **prompt_port}
                for prompt_port in prompt_ports
            ]
        spec_json["must_receive"] = [p["name"] for p in prompt_ports if p.get("direction") == "input"]
        spec_json["must_drive"] = [p["name"] for p in prompt_ports if p.get("direction") == "output"]
        spec_json["must_not_drive"] = list(spec_json["must_receive"])
    else:
        hierarchy = spec_json.get("hierarchy") if isinstance(spec_json.get("hierarchy"), dict) else {}
        top = hierarchy.get("top_module") if isinstance(hierarchy, dict) else None
        if isinstance(top, dict):
            if not top.get("ports"):
                top["ports"] = prompt_ports
            else:
                top["ports"] = [
                    {**next((p for p in top.get("ports", []) if isinstance(p, dict) and p.get("name") == prompt_port["name"]), {}), **prompt_port}
                    for prompt_port in prompt_ports
                ]
            top["must_receive"] = [p["name"] for p in prompt_ports if p.get("direction") == "input"]
            top["must_drive"] = [p["name"] for p in prompt_ports if p.get("direction") == "output"]
            top["must_not_drive"] = list(top["must_receive"])
            top["ports_documentation"] = [
                p for p in top.get("ports_documentation", []) or []
                if isinstance(p, dict) and p.get("name") in prompt_names
            ]
    return spec_json


def _repair_empty_top_ports_from_prompt(spec_json: dict, mode: str, source_prompt: str) -> dict:
    return _repair_top_ports_from_prompt(spec_json, mode, source_prompt)


def _enforce_prompt_top_ports_after_hierarchy_repair(spec_json: dict, mode: str, source_prompt: str) -> dict:
    prompt_ports = _extract_top_ports_from_prompt(source_prompt)
    if mode != "hierarchical" or not prompt_ports:
        return spec_json
    hier = spec_json.get("hierarchy") if isinstance(spec_json.get("hierarchy"), dict) else {}
    top = hier.get("top_module") if isinstance(hier, dict) else None
    if not isinstance(top, dict):
        return spec_json
    prompt_names = {p["name"] for p in prompt_ports}
    existing_ports = {
        str(p.get("name") or ""): p
        for p in (top.get("ports") or [])
        if isinstance(p, dict) and p.get("name")
    }
    top["ports"] = [
        {**existing_ports.get(prompt_port["name"], {}), **prompt_port}
        for prompt_port in prompt_ports
    ]
    top["must_receive"] = [p["name"] for p in prompt_ports if p.get("direction") == "input"]
    top["must_drive"] = [p["name"] for p in prompt_ports if p.get("direction") == "output"]
    top["must_not_drive"] = list(top["must_receive"])
    spec_json["top_level_connections"] = [
        conn for conn in (spec_json.get("top_level_connections") or [])
        if isinstance(conn, dict) and conn.get("top_port") in prompt_names
    ]
    return spec_json


def _merge_prompt_memory_macros(spec_json: dict, prompt_text: str) -> dict:
    if not isinstance(spec_json, dict):
        return spec_json
    prompt_macros = _extract_memory_macros_from_prompt(prompt_text)
    if prompt_macros:
        # Explicit dotted memory_macros[] declarations in the source request
        # are authoritative. Do not let model-generated fallback/inferred
        # memory identities replace a qualified macro contract.
        spec_json["memory_macros"] = prompt_macros
    return spec_json


def _ensure_module_contract_defaults(mod: dict) -> None:
    if not isinstance(mod, dict):
        return
    mod.setdefault("functionality", mod.get("description", ""))
    mod.setdefault("responsibilities", [])
    mod.setdefault("must_drive", [])
    mod.setdefault("must_receive", [])
    mod.setdefault("must_not_drive", [])
    mod.setdefault("reset_behavior", "")
    mod.setdefault("behavior_rules", [])


def _requested_top_module(state: dict) -> str:
    top = str(state.get("top_module") or "").strip()
    if not top:
        return ""
    if not _VERILOG_IDENTIFIER_RE.match(top):
        raise ValueError(f"Requested top_module '{top}' is not a valid Verilog identifier.")
    return top


def _replace_endpoint_module(endpoint: str, old: str, new: str) -> str:
    if isinstance(endpoint, str) and endpoint.startswith(f"{old}."):
        return f"{new}.{endpoint.split('.', 1)[1]}"
    return endpoint


def _apply_requested_top_module(spec_json: dict, mode: str, requested_top: str) -> dict:
    if not requested_top:
        return spec_json

    if mode == "flat":
        old_top = str(spec_json.get("name") or "").strip()
        if old_top == requested_top:
            return spec_json
        ext = os.path.splitext(str(spec_json.get("rtl_output_file") or ""))[1] or ".v"
        spec_json["name"] = requested_top
        spec_json["rtl_output_file"] = f"{requested_top}{ext}"
        return spec_json

    hier = spec_json["hierarchy"]
    top = hier["top_module"]
    old_top = str(top.get("name") or "").strip()
    if old_top == requested_top:
        return spec_json

    for mod in hier.get("modules", []):
        if mod.get("name") == requested_top:
            raise ValueError(
                f"Requested top_module '{requested_top}' conflicts with an existing child module name."
            )

    ext = os.path.splitext(str(top.get("rtl_output_file") or ""))[1] or ".v"
    top["name"] = requested_top
    top["rtl_output_file"] = f"{requested_top}{ext}"
    spec_json["design_name"] = requested_top

    if old_top:
        for conn in spec_json.get("top_level_connections", []):
            conn["connected_to"] = [
                _replace_endpoint_module(dst, old_top, requested_top)
                for dst in conn.get("connected_to", [])
            ]
        for sig in spec_json.get("inter_module_signals", []):
            sig["source"] = _replace_endpoint_module(sig.get("source"), old_top, requested_top)
            sig["destinations"] = [
                _replace_endpoint_module(dst, old_top, requested_top)
                for dst in sig.get("destinations", [])
            ]
        for own in spec_json.get("signal_ownership", []):
            own["owner"] = _replace_endpoint_module(own.get("owner"), old_top, requested_top)

    return spec_json


def _flat_spec_is_memory_macro_interface(spec_json: dict) -> bool:
    macros = spec_json.get("memory_macros") if isinstance(spec_json.get("memory_macros"), list) else []
    ports = spec_json.get("ports") if isinstance(spec_json.get("ports"), list) else []
    if len(macros) != 1 or not ports:
        return False
    macro_ports = macros[0].get("ports") if isinstance(macros[0], dict) and isinstance(macros[0].get("ports"), dict) else {}
    if not macro_ports:
        return False
    spec_port_names = {str(port.get("name") or "").strip() for port in ports if isinstance(port, dict)}
    macro_port_names = {str(name or "").strip() for name in macro_ports.values()}
    if not spec_port_names or spec_port_names != macro_port_names:
        return False
    text = " ".join(str(spec_json.get(key) or "").lower() for key in ("description", "functionality"))
    return bool(re.search(r"\b(memory|sram|macro|fallback|wrapper)\b", text))


def _reject_requested_top_memory_interface(spec_json: dict, mode: str, requested_top: str) -> None:
    if not requested_top or mode != "flat":
        return
    if str(spec_json.get("name") or "").strip() != requested_top:
        return
    if _flat_spec_is_memory_macro_interface(spec_json):
        raise ValueError(
            f"Requested top_module '{requested_top}' resolved to a memory macro interface contract. "
            "Generate the requested controller/top-level module as the top spec and keep SRAM macros as child instances."
        )


def _is_truncated_model_response(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "truncated" in msg and "max_completion_tokens" in msg


def _state_with_spec_token_budget(state: dict, token_budget: int) -> dict:
    profile = json.loads(json.dumps(state.get("model_profile") or {})) if isinstance(state.get("model_profile"), dict) else {}
    routing = profile.setdefault("routing", {})
    spec_route = routing.setdefault("spec_generation", {})
    spec_route["max_completion_tokens"] = token_budget
    spec_route["timeout_sec"] = max(int(spec_route.get("timeout_sec") or 0), 180)

    retry_state = dict(state)
    retry_state["model_profile"] = profile
    return retry_state


def _complete_spec_generation(prompt: str, agent_name: str, state: dict, phase: str) -> str:
    try:
        return complete_text(
            prompt,
            capability="spec_generation",
            agent_name=agent_name,
            state=state,
        )
    except Exception as exc:
        if not _is_truncated_model_response(exc):
            raise
        retry_tokens = int(os.getenv("CHIPLOOP_SPEC_RETRY_MAX_COMPLETION_TOKENS", "32000"))
        logger.warning(
            "Digital Spec Agent %s response truncated; retrying with max_completion_tokens=%s",
            phase,
            retry_tokens,
        )
        return complete_text(
            prompt,
            capability="spec_generation",
            agent_name=agent_name,
            state=_state_with_spec_token_budget(state, retry_tokens),
        )




def _normalize_spec_json(spec_json: dict):
    if not isinstance(spec_json, dict):
        raise ValueError("Spec JSON must be a dictionary.")

    # Root-level ``design_name`` is also common for flat contracts. Ports plus
    # an RTL/function contract make that shape unambiguous, so normalize the
    # alias rather than spending repair passes asking for a one-key rename.
    if (
        not spec_json.get("name")
        and spec_json.get("design_name")
        and isinstance(spec_json.get("ports"), list)
        and (
            spec_json.get("rtl_output_file")
            or spec_json.get("functionality")
            or spec_json.get("responsibilities")
        )
    ):
        spec_json["name"] = spec_json["design_name"]

    # Hierarchical form
    if isinstance(spec_json.get("hierarchy"), dict):
        hier = spec_json["hierarchy"]
        top = hier.get("top_module")
        modules = hier.get("modules", [])
        if not modules and isinstance(hier.get("submodules"), list):
            modules = hier.get("submodules", [])
        if not modules and isinstance(spec_json.get("modules"), list):
            modules = spec_json.get("modules", [])
        if not modules and isinstance(spec_json.get("hierarchical_modules"), list):
            modules = spec_json.get("hierarchical_modules", [])

        if not isinstance(top, dict):
            raise ValueError("hierarchy.top_module must be an object.")
        if not top.get("name"):
            raise ValueError("hierarchy.top_module.name is required.")
        if not top.get("rtl_output_file"):
            top["rtl_output_file"] = spec_json.get("rtl_output_file") or _default_rtl_output_file(top["name"])
        _ensure_module_contract_defaults(top)
        if not isinstance(modules, list):
            raise ValueError("hierarchy.modules must be a list.")

        referenced_modules = set()
        for sig in spec_json.get("inter_module_signals") or hier.get("inter_module_signals") or []:
            if isinstance(sig, dict):
                endpoints = [sig.get("source")] + list(sig.get("destinations") or [])
                for endpoint in endpoints:
                    if isinstance(endpoint, str) and "." in endpoint:
                        referenced_modules.add(endpoint.split(".", 1)[0])
        existing_module_names = {
            str(mod.get("name") or "").strip()
            for mod in modules
            if isinstance(mod, dict)
        }
        for macro in spec_json.get("memory_macros", []):
            if not isinstance(macro, dict):
                continue
            macro_name = str(macro.get("name") or "").strip()
            if macro_name and macro_name in referenced_modules and macro_name not in existing_module_names:
                modules.append(_memory_macro_module(macro))
                existing_module_names.add(macro_name)

        top_name = str(top.get("name") or "").strip()
        filtered_modules = []
        for mod in modules:
            if isinstance(mod, dict) and str(mod.get("name") or "").strip() == top_name:
                for key, value in mod.items():
                    if top.get(key) in (None, "", [], {}):
                        top[key] = value
                continue
            filtered_modules.append(mod)
        modules = filtered_modules

        for mod in modules:
            if isinstance(mod, dict) and mod.get("name") and not mod.get("rtl_output_file"):
                mod["rtl_output_file"] = _default_rtl_output_file(mod["name"])
            _ensure_module_contract_defaults(mod)

        top_level_connections = spec_json.get("top_level_connections") or hier.get("top_level_connections") or []
        inter_module_signals = spec_json.get("inter_module_signals") or hier.get("inter_module_signals") or []
        signal_ownership = spec_json.get("signal_ownership") or hier.get("signal_ownership") or []
        inter_module_signals = [
            sig for sig in inter_module_signals
            if not (
                isinstance(sig, dict)
                and isinstance(sig.get("source"), str)
                and isinstance(sig.get("destinations"), list)
                and sig.get("destinations") == [sig.get("source")]
            )
        ]

        norm = {
            "design_name": spec_json.get("design_name") or top["name"],
            "design_summary": spec_json.get("design_summary", ""),
            "operating_constraints": spec_json.get("operating_constraints", {}),
            "implementation_requirements": spec_json.get("implementation_requirements", []),
            "verification_requirements": spec_json.get("verification_requirements", []),
            "memory_macros": spec_json.get("memory_macros", []),
            "hierarchy": {
                "top_module": top,
                "modules": modules,
            },
            "top_level_connections": top_level_connections,
            "inter_module_signals": inter_module_signals,
            "signal_ownership": signal_ownership,
            "register_contract": spec_json.get("register_contract") or hier.get("register_contract") or spec_json.get("register_map") or {},
        }
        return norm, "hierarchical"

    # Flat form
    if spec_json.get("name"):
        if not spec_json.get("rtl_output_file"):
            spec_json["rtl_output_file"] = _default_rtl_output_file(spec_json["name"])
        norm = {
            "name": spec_json["name"],
            "description": spec_json.get("description") or spec_json.get("design_summary", ""),
            "operating_constraints": spec_json.get("operating_constraints", {}),
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
            "rtl_output_file": spec_json["rtl_output_file"],
            "register_contract": spec_json.get("register_contract") or spec_json.get("register_map") or {},
        }
        return norm, "flat"

    raise ValueError("Spec JSON must be either flat single-module form or hierarchical form.")


def _validate_port(port: dict, where: str) -> None:
    if not isinstance(port, dict):
        raise ValueError(f"{where} must be an object.")
    if not port.get("name"):
        raise ValueError(f"{where}.name is required.")
    if port.get("direction") not in ("input", "output", "inout"):
        raise ValueError(f"{where}.direction must be input/output/inout.")
    width = port.get("width", 1)
    if not isinstance(width, int) or width < 1:
        raise ValueError(f"{where}.width must be integer >= 1.")


def _validate_module(mod: dict, where: str, require_non_empty_ports: bool = False) -> None:
    if not isinstance(mod, dict):
        raise ValueError(f"{where} must be an object.")
    if not mod.get("name"):
        raise ValueError(f"{where}.name is required.")
    if not mod.get("rtl_output_file"):
        raise ValueError(f"{where}.rtl_output_file is required.")

    ports = mod.get("ports")
    if not isinstance(ports, list):
        raise ValueError(f"{where}.ports must be a list.")
    if require_non_empty_ports and not ports:
        raise ValueError(f"{where}.ports must be non-empty for hierarchical mode.")
    for i, p in enumerate(ports):
        _validate_port(p, f"{where}.ports[{i}]")

    required_keys = [
        "functionality",
        "responsibilities",
        "must_drive",
        "must_receive",
        "must_not_drive",
        "reset_behavior",
        "behavior_rules",
    ]
    for key in required_keys:
        if key not in mod:
            raise ValueError(f"{where}.{key} is required.")

    if not isinstance(mod["responsibilities"], list):
        raise ValueError(f"{where}.responsibilities must be a list.")
    if not isinstance(mod["must_drive"], list):
        raise ValueError(f"{where}.must_drive must be a list.")
    if not isinstance(mod["must_receive"], list):
        raise ValueError(f"{where}.must_receive must be a list.")
    if not isinstance(mod["must_not_drive"], list):
        raise ValueError(f"{where}.must_not_drive must be a list.")
    if not isinstance(mod["behavior_rules"], list):
        raise ValueError(f"{where}.behavior_rules must be a list.")


def _validate_top_level_connection(conn: dict, where: str) -> None:
    if not isinstance(conn, dict):
        raise ValueError(f"{where} must be an object.")
    if not conn.get("top_port"):
        raise ValueError(f"{where}.top_port is required.")
    if not isinstance(conn.get("connected_to"), list) or not conn.get("connected_to"):
        raise ValueError(f"{where}.connected_to must be a non-empty list.")


def _validate_inter_signal(sig: dict, where: str) -> None:
    if not isinstance(sig, dict):
        raise ValueError(f"{where} must be an object.")
    if not sig.get("name"):
        raise ValueError(f"{where}.name is required.")
    width = sig.get("width")
    if not isinstance(width, int) or width < 1:
        raise ValueError(f"{where}.width must be integer >= 1.")
    if not sig.get("source"):
        raise ValueError(f"{where}.source is required.")
    if not isinstance(sig.get("destinations"), list) or not sig.get("destinations"):
        raise ValueError(f"{where}.destinations must be a non-empty list.")


def _validate_ownership(item: dict, where: str) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object.")
    if not item.get("signal"):
        raise ValueError(f"{where}.signal is required.")
    if not item.get("owner"):
        raise ValueError(f"{where}.owner is required.")


def _collect_module_port_names(spec_json: dict):
    hier = spec_json["hierarchy"]
    mods = [hier["top_module"]] + list(hier.get("modules", []))
    out = {}
    for m in mods:
        out[m["name"]] = {p["name"] for p in m.get("ports", [])}
    return out


def _collect_module_port_dirs(spec_json: dict):
    hier = spec_json["hierarchy"]
    mods = [hier["top_module"]] + list(hier.get("modules", []))
    out = {}
    for m in mods:
        port_dirs = {}
        for p in m.get("ports", []) or []:
            if isinstance(p, dict) and p.get("name"):
                port_dirs[str(p["name"])] = str(p.get("direction") or "").strip().lower()
        out[m["name"]] = port_dirs
    return out


def _validate_hierarchical_endpoint_coverage(spec_json: dict) -> None:
    module_ports = _collect_module_port_names(spec_json)
    module_dirs = _collect_module_port_dirs(spec_json)
    top_name = spec_json["hierarchy"]["top_module"]["name"]
    top_ports = module_ports[top_name]
    top_dirs = module_dirs.get(top_name, {})

    for i, c in enumerate(spec_json.get("top_level_connections", [])):
        tp = c["top_port"]
        if tp not in top_ports:
            raise ValueError(f"top_level_connections[{i}].top_port '{tp}' is not present in top module ports.")
        top_dir = top_dirs.get(tp)
        for dst in c.get("connected_to", []):
            if "." not in dst:
                raise ValueError(f"top_level_connections[{i}] target '{dst}' is invalid. Expected module.port")
            mod, port = dst.split(".", 1)
            if mod not in module_ports:
                raise ValueError(f"top_level_connections[{i}] target module '{mod}' does not exist.")
            if port not in module_ports[mod]:
                raise ValueError(f"top_level_connections[{i}] target port '{mod}.{port}' is not present in module ports.")
            child_dir = (module_dirs.get(mod) or {}).get(port)
            if top_dir == "input" and child_dir not in {"input", "inout"}:
                raise ValueError(
                    f"top_level_connections[{i}] top input '{tp}' can only drive child input/inout endpoints; "
                    f"target '{mod}.{port}' has direction '{child_dir}'."
                )
            if top_dir == "output" and child_dir not in {"output", "inout"}:
                raise ValueError(
                    f"top_level_connections[{i}] top output '{tp}' must be driven by child output/inout endpoint; "
                    f"target '{mod}.{port}' has direction '{child_dir}'."
                )
            if top_dir == "inout" and child_dir not in {"inout"}:
                raise ValueError(
                    f"top_level_connections[{i}] top inout '{tp}' must connect to child inout endpoint; "
                    f"target '{mod}.{port}' has direction '{child_dir}'."
                )

    for i, s in enumerate(spec_json.get("inter_module_signals", [])):
        src = s["source"]
        if "." not in src:
            raise ValueError(f"inter_module_signals[{i}].source '{src}' is invalid. Expected module.port")
        smod, sport = src.split(".", 1)
        if smod not in module_ports:
            raise ValueError(f"inter_module_signals[{i}] source module '{smod}' does not exist.")
        if smod != top_name and sport not in module_ports[smod]:
            raise ValueError(f"inter_module_signals[{i}] source port '{smod}.{sport}' is not present in module ports.")
        src_dir = (module_dirs.get(smod) or {}).get(sport)
        if smod != top_name and src_dir and src_dir not in {"output", "inout"}:
            raise ValueError(
                f"inter_module_signals[{i}] source port '{smod}.{sport}' must be output/inout, got '{src_dir}'."
            )

        for dst in s.get("destinations", []):
            if "." not in dst:
                raise ValueError(f"inter_module_signals[{i}] destination '{dst}' is invalid. Expected module.port")
            dmod, dport = dst.split(".", 1)
            if dmod not in module_ports:
                raise ValueError(f"inter_module_signals[{i}] destination module '{dmod}' does not exist.")
            if dmod != top_name and dport not in module_ports[dmod]:
                raise ValueError(f"inter_module_signals[{i}] destination port '{dmod}.{dport}' is not present in module ports.")
            dst_dir = (module_dirs.get(dmod) or {}).get(dport)
            if dmod != top_name and dst_dir and dst_dir not in {"input", "inout"}:
                raise ValueError(
                    f"inter_module_signals[{i}] destination port '{dmod}.{dport}' must be input/inout, got '{dst_dir}'."
                )

    for i, o in enumerate(spec_json.get("signal_ownership", [])):
        owner = o["owner"]
        if "." not in owner:
            raise ValueError(f"signal_ownership[{i}].owner '{owner}' is invalid. Expected module.port")
        omod, oport = owner.split(".", 1)
        if omod not in module_ports:
            raise ValueError(f"signal_ownership[{i}] owner module '{omod}' does not exist.")
        if omod != top_name and oport not in module_ports[omod]:
            raise ValueError(f"signal_ownership[{i}] owner port '{omod}.{oport}' is not present in module ports.")
        owner_dir = (module_dirs.get(omod) or {}).get(oport)
        if omod != top_name and owner_dir and owner_dir not in {"output", "inout"}:
            raise ValueError(
                f"signal_ownership[{i}] owner port '{omod}.{oport}' must be output/inout, got '{owner_dir}'."
            )

    # Every child input must have an explicit structural source. A port
    # declaration by itself otherwise becomes an undriven internal RTL wire.
    # Checking only must_receive allowed an incomplete LLM topology to pass
    # spec validation and fail much later during RTL interface repair.
    driven_child_inputs = set()
    for connection in spec_json.get("top_level_connections", []):
        driven_child_inputs.update(str(endpoint) for endpoint in connection.get("connected_to", []))
    for signal in spec_json.get("inter_module_signals", []):
        driven_child_inputs.update(str(endpoint) for endpoint in signal.get("destinations", []))
    memory_macro_names = {
        str(macro.get("name") or "") for macro in spec_json.get("memory_macros", [])
        if isinstance(macro, dict) and macro.get("name")
    }
    missing_child_inputs = []
    for module in spec_json["hierarchy"].get("modules", []):
        module_name = str(module.get("name") or "")
        if module_name in memory_macro_names:
            continue
        ports = {
            str(port.get("name")): str(port.get("direction") or "").lower()
            for port in module.get("ports", []) if isinstance(port, dict) and port.get("name")
        }
        for port_name, direction in ports.items():
            endpoint = f"{module_name}.{port_name}"
            if direction in {"input", "inout"} and endpoint not in driven_child_inputs:
                missing_child_inputs.append(endpoint)

    if missing_child_inputs:
        first_endpoint = missing_child_inputs[0]
        remaining = missing_child_inputs[1:]
        detail = ""
        if remaining:
            detail = " Other required child inputs without sources: " + ", ".join(
                f"'{endpoint}'" for endpoint in remaining
            ) + "."
        raise ValueError(
            f"Required child input '{first_endpoint}' has no source in top_level_connections or "
            "inter_module_signals. Add an explicit top-level input connection or a real child producer."
            f"{detail} Repair every listed input in the same response."
        )


def _validate_spec_contract(spec_json: dict, mode: str) -> None:
    if mode == "flat":
        _validate_module(spec_json, "spec", require_non_empty_ports=False)
        return

    hier = spec_json["hierarchy"]
    top = hier["top_module"]
    modules = hier.get("modules", [])

    nested_submodules = top.get("submodules") if isinstance(top, dict) else None
    if isinstance(nested_submodules, list) and nested_submodules:
        names = [
            str(module.get("name") or "").strip()
            for module in nested_submodules
            if isinstance(module, dict) and str(module.get("name") or "").strip()
        ]
        raise ValueError(
            "Child module definitions must be declared in hierarchy.modules, not "
            "hierarchy.top_module.submodules. Each child requires a unique name and "
            f"rtl_output_file. Nested children found: {', '.join(names[:8]) or 'unnamed'}."
        )

    seen_mods = set()
    seen_files = set()

    def check_unique(mod: dict, where: str):
        name = mod["name"]
        rtl_file = mod["rtl_output_file"]
        if name in seen_mods:
            raise ValueError(f"Duplicate module name detected: {name}")
        if rtl_file in seen_files:
            raise ValueError(f"Duplicate rtl_output_file detected: {rtl_file}")
        seen_mods.add(name)
        seen_files.add(rtl_file)
        _validate_module(mod, where, require_non_empty_ports=True)

    check_unique(top, "hierarchy.top_module")
    for idx, mod in enumerate(modules):
        check_unique(mod, f"hierarchy.modules[{idx}]")

    tlc = spec_json.get("top_level_connections")
    ims = spec_json.get("inter_module_signals")
    own = spec_json.get("signal_ownership")

    if not isinstance(tlc, list) or not tlc:
        raise ValueError("top_level_connections must be present and non-empty for hierarchical mode.")
    if not isinstance(ims, list) or (modules and not ims):
        raise ValueError("inter_module_signals must be present and non-empty for hierarchical mode.")
    if not isinstance(own, list) or not own:
        raise ValueError("signal_ownership must be present and non-empty for hierarchical mode.")

    for i, c in enumerate(tlc):
        _validate_top_level_connection(c, f"top_level_connections[{i}]")
    for i, s in enumerate(ims):
        _validate_inter_signal(s, f"inter_module_signals[{i}]")
    for i, o in enumerate(own):
        _validate_ownership(o, f"signal_ownership[{i}]")

    _validate_hierarchical_endpoint_coverage(spec_json)

def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


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


def _strip_json_wrappers(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json_object_text(text: str) -> str:
    cleaned = _strip_json_wrappers(text)
    if not cleaned:
        return cleaned
    decoder = json.JSONDecoder()
    try:
        _, end = decoder.raw_decode(cleaned)
        tail = cleaned[end:].strip()
        if not tail:
            return cleaned[:end]
    except JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start < 0:
        return cleaned

    in_string = False
    escaped = False
    depth = 0
    for idx in range(start, len(cleaned)):
        ch = cleaned[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:idx + 1].strip()
    return cleaned[start:].strip()


def _extract_json_object_texts(text: str) -> list[str]:
    cleaned = _strip_json_wrappers(text)
    if not cleaned:
        return []
    decoder = json.JSONDecoder()
    out: list[str] = []
    for idx, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(cleaned[idx:])
        except JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(cleaned[idx:idx + end].strip())
    return out


def _json_information_score(value) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, (list, dict)):
        if not value:
            return 0
        return len(json.dumps(value, sort_keys=True, default=str))
    return 1


def _prefer_informative_duplicate_pairs(pairs):
    """Recover invalid model JSON where a later empty duplicate erases a contract.

    JSON normally keeps the final duplicate key. Model output occasionally repeats
    module fields (ports, ownership, behavior) and leaves the second copy empty.
    Preserve the more informative value while retaining normal last-value behavior
    when both values carry the same amount of information.
    """
    result = {}
    for key, value in pairs:
        if key not in result or _json_information_score(value) >= _json_information_score(result[key]):
            result[key] = value
    return result


def _loads_model_json(text: str):
    return json.loads(text, object_pairs_hook=_prefer_informative_duplicate_pairs)


def _hierarchical_candidate_completeness(candidate: dict) -> tuple[int, int]:
    """Rank full contracts above nested hierarchy fragments.

    The object extractor intentionally returns nested JSON objects as well as
    the outer response. A nested ``hierarchy`` object has ``top_module`` and
    ``modules`` and can therefore look like a contract, but it omits root-level
    connectivity, ownership, and register metadata. Prefer structural coverage
    first and information volume second; caller position remains the tie-break.
    """
    hierarchy = candidate.get("hierarchy") if isinstance(candidate.get("hierarchy"), dict) else {}
    top = hierarchy.get("top_module") if isinstance(hierarchy.get("top_module"), dict) else {}
    modules = hierarchy.get("modules") if isinstance(hierarchy.get("modules"), list) else []
    structural_fields = (
        bool(top.get("name")),
        bool(top.get("ports")),
        bool(modules),
        bool(candidate.get("top_level_connections")),
        bool(candidate.get("inter_module_signals")),
        bool(candidate.get("signal_ownership")),
        bool(candidate.get("register_contract")),
    )
    return sum(structural_fields), _json_information_score(candidate)


def _parse_llm_json_object(llm_output: str) -> dict:
    candidates = _extract_json_object_texts(llm_output)
    hierarchical_candidates: list[dict] = []
    flat_candidates: list[dict] = []
    for item in candidates:
        try:
            parsed_item = _loads_model_json(item)
        except JSONDecodeError:
            continue
        if isinstance(parsed_item, dict) and parsed_item.get("hierarchy"):
            hierarchical_candidates.append(parsed_item)
            continue
        if isinstance(parsed_item, dict) and isinstance(parsed_item.get("top_module"), dict) and isinstance(parsed_item.get("modules"), list):
            hierarchical_candidates.append({
                "design_name": parsed_item.get("design_name") or parsed_item["top_module"].get("name"),
                "design_summary": parsed_item.get("design_summary", ""),
                "operating_constraints": parsed_item.get("operating_constraints", {}),
                "implementation_requirements": parsed_item.get("implementation_requirements", []),
                "verification_requirements": parsed_item.get("verification_requirements", []),
                "memory_macros": parsed_item.get("memory_macros", []),
                "hierarchy": {
                    "top_module": parsed_item["top_module"],
                    "modules": parsed_item.get("modules", []),
                },
                "top_level_connections": parsed_item.get("top_level_connections", []),
                "inter_module_signals": parsed_item.get("inter_module_signals", []),
                "signal_ownership": parsed_item.get("signal_ownership", []),
                "register_contract": parsed_item.get("register_contract", {}),
                "verification": parsed_item.get("verification", {}),
                "implementation_notes": parsed_item.get("implementation_notes", {}),
            })
            continue
        is_flat_spec = parsed_item.get("name") and (
            "functionality" in parsed_item
            or "responsibilities" in parsed_item
            or "rtl_output_file" in parsed_item
        )
        if isinstance(parsed_item, dict) and is_flat_spec:
            flat_candidates.append(parsed_item)
    if hierarchical_candidates:
        return max(
            enumerate(hierarchical_candidates),
            key=lambda item: (_hierarchical_candidate_completeness(item[1]), item[0]),
        )[1]
    if flat_candidates:
        return flat_candidates[-1]

    candidate = _extract_json_object_text(llm_output)
    try:
        parsed = _loads_model_json(candidate)
    except JSONDecodeError as exc:
        repaired = _repair_json_syntax_near_error(candidate, exc)
        if repaired == candidate:
            repaired = _repair_json_if_truncated_at_eof(candidate, exc)
        if repaired == candidate:
            raise
        parsed = _loads_model_json(repaired)
    if not isinstance(parsed, dict):
        raise ValueError("Spec JSON root must be an object.")
    return parsed


def _try_parse_after_eof_repair(candidate: str) -> str | None:
    try:
        json.loads(candidate)
        return candidate
    except JSONDecodeError as exc:
        repaired = _repair_json_if_truncated_at_eof(candidate, exc)
        if repaired == candidate:
            return None
        try:
            json.loads(repaired)
            return repaired
        except JSONDecodeError:
            return None


def _repair_json_syntax_near_error(candidate: str, exc: JSONDecodeError) -> str:
    text = (candidate or "").strip()
    if not text:
        return candidate

    positions = [exc.pos, exc.pos - 1, exc.pos + 1]
    replacements = {"]": "}", "}": "]"}
    for pos in positions:
        if pos < 0 or pos >= len(text):
            continue
        ch = text[pos]
        replacement = replacements.get(ch)
        if not replacement:
            continue
        probe = text[:pos] + replacement + text[pos + 1:]
        parsed = _try_parse_after_eof_repair(probe)
        if parsed is not None:
            return parsed
    for pos in positions:
        if pos < 0 or pos >= len(text) or text[pos] not in ("}", "]"):
            continue
        # Remove only a closing token adjacent to the decoder failure, and
        # accept the edit only when the complete JSON object validates.
        probe = text[:pos] + text[pos + 1:]
        parsed = _try_parse_after_eof_repair(probe)
        if parsed is not None:
            return parsed
    return candidate


def _repair_json_if_truncated_at_eof(candidate: str, exc: JSONDecodeError) -> str:
    text = (candidate or "").strip()
    if not text or exc.pos < len(text) - 2:
        return candidate

    stack = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
            else:
                return candidate

    if in_string or not stack:
        return candidate
    return text + "".join(reversed(stack))


def _json_error_context(text: str, err: Exception, window: int = 1600) -> str:
    if not isinstance(err, JSONDecodeError):
        return str(err)
    candidate = _extract_json_object_text(text)
    pos = max(0, min(err.pos, len(candidate)))
    lo = max(0, pos - window)
    hi = min(len(candidate), pos + window)
    return (
        f"{err.msg} at line {err.lineno} column {err.colno} char {err.pos}\n"
        f"Context around error:\n{candidate[lo:hi]}"
    )

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

def _upload_spec_debug_artifacts(workflow_id, agent_name, spec_dir):
    for fname in [
        "spec_agent_entry.json",
        "spec_agent_input.txt",
        "spec_agent_summary.txt",
        "spec_agent_contract.log",
        "llm_raw_output.txt",
        "spec_agent_exception.txt",
        "spec_agent_contract_pass2.log",
        "llm_raw_output_pass2.txt",
        "spec_agent_exception_pass2.txt",
        "spec_agent_contract_pass3.log",
        "llm_raw_output_pass3.txt",
        "spec_agent_exception_pass3.txt",
        "spec_agent_contract_pass4.log",
        "llm_raw_output_pass4.txt",
        "spec_agent_exception_pass4.txt",
        "spec_agent_contract_pass5.log",
        "llm_raw_output_pass5.txt",
        "spec_agent_exception_pass5.txt",
        "spec_agent_normalized.json",
        "spec_agent_normalized_pass2.json",
        "spec_agent_normalized_pass3.json",
        "spec_agent_normalized_pass4.json",
        "spec_agent_normalized_pass5.json",
    ]:
        _record_text_artifact_safe(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="spec",
            filename=fname,
            path=os.path.join(spec_dir, fname),
        )

    for fn in os.listdir(spec_dir):
        if fn.endswith("_spec.json"):
            _record_text_artifact_safe(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename=fn,
                path=os.path.join(spec_dir, fn),
            )


def _find_module_obj(spec_json: dict, module_name: str):
    hier = spec_json["hierarchy"]
    if hier["top_module"]["name"] == module_name:
        return hier["top_module"]
    for m in hier.get("modules", []):
        if m["name"] == module_name:
            return m
    return None


def _infer_direction_from_usage(module_name: str, port_name: str, spec_json: dict) -> str:
    # default conservative choice
    direction = "input"

    for sig in spec_json.get("inter_module_signals", []):
        src = sig.get("source", "")
        if "." in src:
            smod, sport = src.split(".", 1)
            if smod == module_name and sport == port_name:
                return "output"
        for dst in sig.get("destinations", []):
            if "." in dst:
                dmod, dport = dst.split(".", 1)
                if dmod == module_name and dport == port_name:
                    direction = "input"

    for conn in spec_json.get("top_level_connections", []):
        top_port = conn.get("top_port")
        for dst in conn.get("connected_to", []):
            if "." in dst:
                dmod, dport = dst.split(".", 1)
                if dmod == module_name and dport == port_name:
                    direction = "input"

    for own in spec_json.get("signal_ownership", []):
        owner = own.get("owner", "")
        if "." in owner:
            omod, oport = owner.split(".", 1)
            if omod == module_name and oport == port_name:
                return "output"

    return direction


def _infer_width_from_usage(module_name: str, port_name: str, spec_json: dict) -> int:
    for sig in spec_json.get("inter_module_signals", []):
        width = sig.get("width")
        src = sig.get("source", "")
        if "." in src:
            smod, sport = src.split(".", 1)
            if smod == module_name and sport == port_name and isinstance(width, int) and width >= 1:
                return width
        for dst in sig.get("destinations", []):
            if "." in dst:
                dmod, dport = dst.split(".", 1)
                if dmod == module_name and dport == port_name and isinstance(width, int) and width >= 1:
                    return width
    return 1


def _ensure_hierarchical_port_closure(spec_json: dict) -> dict:
    hier = spec_json["hierarchy"]
    mods = [hier["top_module"]] + list(hier.get("modules", []))

    port_maps = {}
    for m in mods:
        port_maps[m["name"]] = {p["name"]: p for p in m.get("ports", [])}

    referenced = []

    for conn in spec_json.get("top_level_connections", []):
        for dst in conn.get("connected_to", []):
            if "." in dst:
                referenced.append(dst)

    for sig in spec_json.get("inter_module_signals", []):
        src = sig.get("source", "")
        if "." in src:
            referenced.append(src)
        for dst in sig.get("destinations", []):
            if "." in dst:
                referenced.append(dst)

    # Ownership is a validation assertion, not a declaration mechanism. A
    # model cannot make a nonexistent port valid merely by naming it as an
    # owner; only actual connectivity endpoints may participate in closure.

    for ep in referenced:
        mod_name, port_name = ep.split(".", 1)
        mod = _find_module_obj(spec_json, mod_name)
        if mod is None:
            continue

        existing = port_maps[mod_name]
        if port_name not in existing:
            existing[port_name] = {
                "name": port_name,
                "direction": _infer_direction_from_usage(mod_name, port_name, spec_json),
                "width": _infer_width_from_usage(mod_name, port_name, spec_json),
            }

    for m in mods:
        pmap = port_maps[m["name"]]
        m["ports"] = list(pmap.values())

    return spec_json


def _normalize_endpoint_port(endpoint: str):
    if not isinstance(endpoint, str) or "." not in endpoint:
        return "", ""
    module_name, port_name = endpoint.split(".", 1)
    port_name = re.sub(r"\[[^\]]+\]", "", port_name).strip()
    return module_name.strip(), port_name


def _set_reconciled_port_direction(module: dict, port_name: str, direction: str) -> None:
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


def _normalize_memory_wrapper_port_directions(spec_json: dict, mode: str) -> dict:
    """Correct the standard producer/consumer direction of explicit wrappers.

    Model output sometimes reverses an entire BRAM wrapper interface, making
    address/write-data outputs and read-data an input. Apply this only to small
    modules explicitly named/described as memory wrappers, before connectivity
    sanitization has a chance to discard otherwise valid edges.
    """
    if mode != "hierarchical":
        return spec_json
    hierarchy = spec_json.get("hierarchy") or {}
    for module in hierarchy.get("modules") or []:
        if not isinstance(module, dict):
            continue
        identity = " ".join(
            str(module.get(key) or "").lower()
            for key in ("name", "description", "functionality")
        )
        ports = [port for port in (module.get("ports") or []) if isinstance(port, dict)]
        port_names = {str(port.get("name") or "").strip().lower() for port in ports}
        is_explicit_wrapper = (
            "wrapper" in identity
            and any(token in identity for token in ("bram", "sram", "memory"))
            and any(name in port_names for name in ("dout", "rdata", "data_out", "q"))
            and any(name in port_names for name in ("addr", "address"))
        )
        if not is_explicit_wrapper:
            continue
        for port in ports:
            name = str(port.get("name") or "").strip()
            role = name.lower()
            direction = "output" if role in {"dout", "rdata", "data_out", "q"} else "input"
            _set_reconciled_port_direction(module, name, direction)
    return spec_json


def _reconcile_hierarchical_signal_directions(spec_json: dict, mode: str) -> dict:
    if mode != "hierarchical":
        return spec_json
    hier = spec_json.get("hierarchy") or {}
    top_module = hier.get("top_module") or {}
    top_name = str(top_module.get("name") or "").strip()
    top_port_dirs = {
        str(port.get("name") or "").strip(): str(port.get("direction") or "").strip().lower()
        for port in (top_module.get("ports") or [])
        if isinstance(port, dict) and str(port.get("name") or "").strip()
    }
    modules = [top_module] + list(hier.get("modules") or [])
    module_map = {
        str(module.get("name") or "").strip(): module
        for module in modules
        if isinstance(module, dict) and str(module.get("name") or "").strip()
    }
    desired = {}

    def mark(endpoint: str, direction: str) -> None:
        module_name, port_name = _normalize_endpoint_port(endpoint)
        if module_name in module_map and port_name:
            if module_name == top_name and top_port_dirs.get(port_name) in {"input", "output", "inout"}:
                return
            key = (module_name, port_name)
            if direction == "output" or key not in desired:
                desired[key] = direction

    for sig in spec_json.get("inter_module_signals", []) or []:
        if not isinstance(sig, dict):
            continue
        mark(sig.get("source"), "output")
        for dst in sig.get("destinations", []) or []:
            mark(dst, "input")

    # Do not create ports from ownership assertions. Ownership is valid only
    # after an endpoint has been declared by the module contract or real
    # connectivity; the sanitizer below removes stale assertions.

    for (module_name, port_name), direction in desired.items():
        _set_reconciled_port_direction(module_map[module_name], port_name, direction)

    return spec_json


def _ensure_hierarchical_top_level_connections(spec_json: dict) -> dict:
    if not isinstance(spec_json.get("hierarchy"), dict):
        return spec_json
    if isinstance(spec_json.get("top_level_connections"), list) and spec_json["top_level_connections"]:
        return spec_json

    hier = spec_json["hierarchy"]
    top = hier.get("top_module") if isinstance(hier.get("top_module"), dict) else {}
    top_ports = top.get("ports") if isinstance(top.get("ports"), list) else []
    child_modules = [m for m in hier.get("modules", []) if isinstance(m, dict)]
    child_port_map = {
        str(m.get("name") or ""): {
            str(p.get("name") or "")
            for p in (m.get("ports") or [])
            if isinstance(p, dict)
        }
        for m in child_modules
    }

    connections = []
    top_name = str(top.get("name") or "").strip()
    for port in top_ports:
        if not isinstance(port, dict):
            continue
        port_name = str(port.get("name") or "").strip()
        if not port_name:
            continue
        if child_port_map:
            connected_to = [
                f"{module_name}.{port_name}"
                for module_name, ports in child_port_map.items()
                if module_name and port_name in ports
            ]
        else:
            connected_to = [f"{top_name}.{port_name}"] if top_name else []
        if connected_to:
            connections.append({
                "top_port": port_name,
                "connected_to": connected_to,
                "description": f"Top-level port {port_name} connected to matching child module port(s).",
            })

    if connections:
        spec_json["top_level_connections"] = connections
    return spec_json


def _ensure_hierarchical_inter_module_signals(spec_json: dict) -> dict:
    if not isinstance(spec_json.get("hierarchy"), dict):
        return spec_json

    hier = spec_json["hierarchy"]
    top_connected_endpoints = set()
    for conn in spec_json.get("top_level_connections", []) or []:
        if not isinstance(conn, dict):
            continue
        top_connected_endpoints.update(str(endpoint or "").strip() for endpoint in conn.get("connected_to", []) or [])

    outputs = []
    inputs = []
    for module in (hier.get("modules") or []):
        if not isinstance(module, dict):
            continue
        module_name = str(module.get("name") or "").strip()
        if not module_name:
            continue
        for port in module.get("ports") or []:
            if not isinstance(port, dict):
                continue
            port_name = str(port.get("name") or "").strip()
            direction = str(port.get("direction") or "").strip().lower()
            if not port_name:
                continue
            endpoint = f"{module_name}.{port_name}"
            if endpoint in top_connected_endpoints:
                continue
            width = int(port.get("width") or 1) if str(port.get("width") or "").isdigit() else 1
            record = {"module": module_name, "port": port_name, "endpoint": endpoint, "width": width}
            if direction in {"output", "inout"}:
                outputs.append(record)
            if direction in {"input", "inout"}:
                inputs.append(record)

    # Treat model-provided connectivity as a partial graph. A single valid
    # edge must not suppress deterministic completion of other uniquely
    # matchable child ports.
    signals = [
        dict(signal)
        for signal in (spec_json.get("inter_module_signals") or [])
        if isinstance(signal, dict)
    ]
    connected_destinations = {
        str(destination or "").strip()
        for signal in signals
        for destination in (signal.get("destinations") or [])
        if str(destination or "").strip()
    }
    existing_edges = {
        (str(signal.get("source") or "").strip(), str(destination or "").strip())
        for signal in signals
        for destination in (signal.get("destinations") or [])
    }
    output_groups = {}
    for output in outputs:
        output_groups.setdefault((output["port"], output["width"]), []).append(output)
    for (port_name, width), candidates in sorted(output_groups.items()):
        if len(candidates) != 1:
            continue
        source = candidates[0]
        destinations = [
            item["endpoint"]
            for item in inputs
            if item["module"] != source["module"]
            and item["port"] == port_name
            and item["width"] == width
            and item["endpoint"] not in connected_destinations
            and (source["endpoint"], item["endpoint"]) not in existing_edges
        ]
        if not destinations:
            continue
        signals.append({
            "name": f"{source['module']}_{port_name}",
            "width": width,
            "source": source["endpoint"],
            "destinations": list(dict.fromkeys(destinations)),
            "description": f"Derived child-to-child signal from the unique matching producer {source['endpoint']}.",
        })

    spec_json["inter_module_signals"] = signals
    return spec_json


def _sanitize_hierarchical_connectivity(spec_json: dict) -> dict:
    if not isinstance(spec_json.get("hierarchy"), dict):
        return spec_json
    hier = spec_json["hierarchy"]
    top = hier.get("top_module") if isinstance(hier.get("top_module"), dict) else {}
    modules = [top] + [m for m in (hier.get("modules") or []) if isinstance(m, dict)]
    module_dirs = {
        str(m.get("name") or "").strip(): {
            str(p.get("name") or "").strip(): str(p.get("direction") or "").strip().lower()
            for p in (m.get("ports") or [])
            if isinstance(p, dict) and str(p.get("name") or "").strip()
        }
        for m in modules
        if str(m.get("name") or "").strip()
    }
    module_widths = {
        str(m.get("name") or "").strip(): {
            str(p.get("name") or "").strip(): int(p.get("width") or 1)
            for p in (m.get("ports") or [])
            if isinstance(p, dict) and str(p.get("name") or "").strip()
        }
        for m in modules
        if str(m.get("name") or "").strip()
    }
    top_name = str(top.get("name") or "").strip()
    top_dirs = module_dirs.get(top_name, {})

    def endpoint_dir(endpoint: str) -> str:
        module_name, port_name = _normalize_endpoint_port(endpoint)
        return (module_dirs.get(module_name) or {}).get(port_name, "")

    def source_ok(endpoint: str) -> bool:
        module_name, _ = _normalize_endpoint_port(endpoint)
        if module_name == top_name:
            return True
        return endpoint_dir(endpoint) in {"output", "inout"}

    def destination_ok(endpoint: str) -> bool:
        module_name, _ = _normalize_endpoint_port(endpoint)
        if module_name == top_name:
            return True
        return endpoint_dir(endpoint) in {"input", "inout"}

    def endpoint_width(endpoint: str) -> int:
        module_name, port_name = _normalize_endpoint_port(endpoint)
        return int((module_widths.get(module_name) or {}).get(port_name, 1))

    def compatible_top_endpoint(top_dir: str, child_dir: str) -> bool:
        if top_dir == "input":
            return child_dir in {"input", "inout"}
        if top_dir == "output":
            return child_dir in {"output", "inout"}
        if top_dir == "inout":
            return child_dir == "inout"
        return False

    sanitized_connections = []
    for conn in spec_json.get("top_level_connections", []) or []:
        if not isinstance(conn, dict):
            continue
        top_port = str(conn.get("top_port") or "").strip()
        top_dir = top_dirs.get(top_port, "")
        endpoints = []
        for endpoint in conn.get("connected_to", []) or []:
            endpoint = str(endpoint or "").strip()
            if endpoint and compatible_top_endpoint(top_dir, endpoint_dir(endpoint)):
                endpoints.append(endpoint)
        if endpoints:
            new_conn = dict(conn)
            new_conn["connected_to"] = list(dict.fromkeys(endpoints))
            sanitized_connections.append(new_conn)
    spec_json["top_level_connections"] = sanitized_connections

    sanitized_signals = []
    for sig in spec_json.get("inter_module_signals", []) or []:
        if not isinstance(sig, dict):
            continue
        source = str(sig.get("source") or "").strip()
        src_mod, src_port = _normalize_endpoint_port(source)
        if not source_ok(source):
            continue
        # A top-level input is already a complete externally-driven signal; it
        # cannot be the producer of a differently named derived/configuration
        # signal. Direct top-input fanout belongs in top_level_connections.
        # Keeping such aliases here creates an ownership contract that the RTL
        # gate must reject (for example cfg_wdata owning cfg_enable).
        if (
            src_mod == top_name
            and top_dirs.get(src_port) == "input"
            and str(sig.get("name") or "").strip() != src_port
        ):
            continue
        signal_width = int(sig.get("width") or 1)
        if src_mod != top_name and endpoint_width(source) != signal_width:
            continue
        destinations = []
        for endpoint in sig.get("destinations", []) or []:
            endpoint = str(endpoint or "").strip()
            dst_mod, _ = _normalize_endpoint_port(endpoint)
            if dst_mod == src_mod:
                continue
            if destination_ok(endpoint) and (dst_mod == top_name or endpoint_width(endpoint) == signal_width):
                destinations.append(endpoint)
        if destinations:
            new_sig = dict(sig)
            new_sig["destinations"] = list(dict.fromkeys(destinations))
            sanitized_signals.append(new_sig)

    # A child input is a single structural sink and may have only one producer.
    # Prefer the semantically exact signal/port match when an LLM emits
    # contradictory edges; never allow the ambiguity to become RTL multi-drive.
    winners = {}
    for index, sig in enumerate(sanitized_signals):
        signal_name = str(sig.get("name") or "").strip().lower()
        _, source_port = _normalize_endpoint_port(str(sig.get("source") or ""))
        for destination in sig.get("destinations") or []:
            _, destination_port = _normalize_endpoint_port(destination)
            dest_lower = destination_port.lower()
            signal_tokens = set(re.split(r"_+", signal_name))
            dest_tokens = set(re.split(r"_+", dest_lower))
            score = len(signal_tokens.intersection(dest_tokens))
            if signal_name == dest_lower:
                score += 100
            if source_port.lower() == dest_lower:
                score += 80
            current = winners.get(destination)
            if current is None or score > current[0]:
                winners[destination] = (score, index)
    for index, sig in enumerate(sanitized_signals):
        sig["destinations"] = [
            destination
            for destination in sig.get("destinations") or []
            if winners.get(destination, (-1, -1))[1] == index
        ]
    sanitized_signals = [sig for sig in sanitized_signals if sig.get("destinations")]
    spec_json["inter_module_signals"] = sanitized_signals

    ownership = []
    seen = set()
    for item in spec_json.get("signal_ownership", []) or []:
        if not isinstance(item, dict):
            continue
        signal = str(item.get("signal") or "").strip()
        owner = str(item.get("owner") or "").strip()
        owner_mod, owner_port = _normalize_endpoint_port(owner)
        key = (signal, owner)
        owner_top_dir = top_dirs.get(owner_port)
        valid_top_owner = owner_mod == top_name and owner_top_dir in {"output", "inout"}
        if signal and owner and key not in seen and (valid_top_owner or endpoint_dir(owner) in {"output", "inout"}):
            ownership.append(dict(item))
            seen.add(key)
    for sig in sanitized_signals:
        signal = str(sig.get("name") or "").strip()
        owner = str(sig.get("source") or "").strip()
        key = (signal, owner)
        if signal and owner and key not in seen:
            ownership.append({"signal": signal, "owner": owner})
            seen.add(key)
    for conn in spec_json.get("top_level_connections", []) or []:
        top_port = str(conn.get("top_port") or "").strip()
        if top_dirs.get(top_port) != "output":
            continue
        owner = next((str(x).strip() for x in conn.get("connected_to", []) or [] if str(x).strip()), "")
        key = (top_port, owner)
        if top_port and owner and key not in seen:
            ownership.append({"signal": top_port, "owner": owner})
            seen.add(key)
    spec_json["signal_ownership"] = ownership
    return spec_json


def _build_connectivity_repair_diagnostics(previous_json_text: str) -> str:
    """Explain structurally invalid attempted edges without changing the contract."""
    def positive_width(value) -> int:
        try:
            width = int(value)
        except (TypeError, ValueError):
            return 1
        return width if width > 0 else 1

    try:
        parsed = _parse_llm_json_object(str(previous_json_text or ""))
        spec_json, mode = _normalize_spec_json(parsed)
    except (JSONDecodeError, ValueError, TypeError):
        return ""
    if mode != "hierarchical":
        return ""

    top = spec_json.get("hierarchy", {}).get("top_module", {})
    modules = spec_json.get("hierarchy", {}).get("modules", [])
    ports = {}
    for module in [top, *modules]:
        module_name = str(module.get("name") or "").strip()
        for port in module.get("ports") or []:
            endpoint = f"{module_name}.{str(port.get('name') or '').strip()}"
            ports[endpoint] = (
                str(port.get("direction") or "").lower(),
                positive_width(port.get("width")),
            )

    findings = []
    sink_sources = {}
    for index, signal in enumerate(spec_json.get("inter_module_signals") or []):
        if not isinstance(signal, dict):
            continue
        source = str(signal.get("source") or "").strip()
        source_contract = ports.get(source)
        signal_width = positive_width(signal.get("width"))
        source_problem = None
        if source_contract is None:
            source_problem = "source endpoint is undeclared"
        elif source_contract[0] not in {"output", "inout"}:
            source_problem = f"source is a {source_contract[0] or 'directionless'} consumer port"
        elif source_contract[1] != signal_width:
            source_problem = f"source width {source_contract[1]} does not match signal width {signal_width}"
        for destination in signal.get("destinations") or []:
            destination = str(destination or "").strip()
            destination_contract = ports.get(destination)
            problems = []
            if source_problem:
                problems.append(source_problem)
            if destination_contract is None:
                problems.append("destination endpoint is undeclared")
            else:
                if destination_contract[0] not in {"input", "inout"}:
                    problems.append(f"destination is a {destination_contract[0] or 'directionless'} producer port")
                if destination_contract[1] != signal_width:
                    problems.append(
                        f"destination width {destination_contract[1]} does not match signal width {signal_width}"
                    )
            if problems:
                findings.append(f"- REJECT edge {source} -> {destination}: {'; '.join(problems)}.")
            else:
                sink_sources.setdefault(destination, []).append((index, source))

    for destination, attempts in sink_sources.items():
        unique_sources = list(dict.fromkeys(source for _, source in attempts))
        if len(unique_sources) > 1:
            findings.append(
                f"- REJECT duplicate drivers for {destination}: {', '.join(unique_sources)}. "
                "Choose one real producer or create an explicit combining/aggregation output."
            )
    if not findings:
        return ""
    return "\n\nSTRUCTURAL GRAPH DIAGNOSTICS FROM THE PREVIOUS JSON:\n" + "\n".join(findings[:80])


def _remove_self_owned_alias_inputs(spec_json: dict) -> dict:
    """Remove undriven child inputs explicitly owned by the same child's output.

    This contract shape describes internally computed state twice: once as an
    input consumer and once as a same-module output owner.  Keeping both would
    require artificial self-feedback.  Ownership and port directions provide
    the complete deterministic proof; signal names are not interpreted.
    """
    hierarchy = spec_json.get("hierarchy") if isinstance(spec_json.get("hierarchy"), dict) else {}
    modules = hierarchy.get("modules") if isinstance(hierarchy.get("modules"), list) else []
    driven = {
        str(endpoint)
        for connection in spec_json.get("top_level_connections", []) or []
        if isinstance(connection, dict)
        for endpoint in connection.get("connected_to", []) or []
    }
    driven.update(
        str(endpoint)
        for signal in spec_json.get("inter_module_signals", []) or []
        if isinstance(signal, dict)
        for endpoint in signal.get("destinations", []) or []
    )
    module_by_name = {
        str(module.get("name") or "").strip(): module
        for module in modules if isinstance(module, dict) and module.get("name")
    }
    removable: dict[str, set[str]] = {}
    for ownership in spec_json.get("signal_ownership", []) or []:
        if not isinstance(ownership, dict):
            continue
        alias = str(ownership.get("signal") or "").strip()
        owner = str(ownership.get("owner") or "").strip()
        if not alias or "." not in owner:
            continue
        owner_module, owner_port = owner.split(".", 1)
        module = module_by_name.get(owner_module)
        if not module:
            continue
        directions = {
            str(port.get("name") or "").strip(): str(port.get("direction") or "").lower()
            for port in module.get("ports", []) or [] if isinstance(port, dict)
        }
        alias_endpoint = f"{owner_module}.{alias}"
        if (
            alias != owner_port
            and directions.get(alias) == "input"
            and directions.get(owner_port) in {"output", "inout"}
            and alias_endpoint not in driven
        ):
            removable.setdefault(owner_module, set()).add(alias)

    for module_name, aliases in removable.items():
        module = module_by_name[module_name]
        module["ports"] = [
            port for port in module.get("ports", []) or []
            if not (isinstance(port, dict) and str(port.get("name") or "").strip() in aliases)
        ]
        for field in ("must_receive", "must_not_drive"):
            if isinstance(module.get(field), list):
                module[field] = [name for name in module[field] if str(name).strip() not in aliases]
    return spec_json


def _build_repair_prompt(
    base_prompt: str,
    previous_json_text: str,
    failure_log_text: str,
    *,
    strict_connectivity: bool = False,
    final_graph_closure: bool = False,
) -> str:
    graph_diagnostics = _build_connectivity_repair_diagnostics(previous_json_text)
    firmware_examples = ""
    if "firmware control-plane" in str(failure_log_text or "").lower() or "register_contract" in str(failure_log_text or ""):
        firmware_examples = """

FIRMWARE CONTROL-PLANE REPAIR EXAMPLES:
- GOOD: keep application-specific configuration semantics, expose them through a declared CSR/MMIO address/write-data/read-data/write-enable/ready interface, and describe the exact implemented registers and fields in register_contract.
- GOOD SHAPE: "register_contract":{"bus_type":"csr","registers":[{"name":"CONTROL","offset":"0x00","access":"RW","fields":[{"name":"enable","lsb":0,"msb":0,"access":"RW"}]}]}; declare and implement the matching bus ports in the top module.
- GOOD: direct real-time streaming ports may coexist with the CSR interface.
- BAD: expose only dozens of direct cfg_* value pins while claiming firmware can configure the block.
- BAD: add register_contract JSON without adding the matching synthesizable top-level bus, or add a bus without concrete addressed registers.
- Repair the complete specification coherently; do not patch only the validation message.
"""
    hierarchy_examples = ""
    if "hierarchy.top_module.submodules" in str(failure_log_text or "") or "child module definitions" in str(failure_log_text or "").lower():
        hierarchy_examples = """

HIERARCHY DELIVERABLE REPAIR EXAMPLES:
- GOOD: hierarchy.top_module contains only the top contract; every instantiated child is a separate object in hierarchy.modules with its own unique rtl_output_file.
- GOOD: top-level connectivity references the exact ports declared by those hierarchy.modules children.
- BAD: place full child definitions under hierarchy.top_module.submodules, responsibilities, behavior_rules, or prose. Those locations do not create RTL deliverables.
- BAD: let the top instantiate a module that is absent from hierarchy.modules and the expected RTL file list.
- Return the complete corrected hierarchy and connectivity; do not merely remove the nested definitions.
"""
    connectivity_examples = ""
    failure_text_lower = str(failure_log_text or "").lower()
    if "required child input" in failure_text_lower and "has no source" in failure_text_lower:
        connectivity_examples = """

HIERARCHICAL CONNECTIVITY-CLOSURE REPAIR EXAMPLES:
- Repair EVERY unconnected child input listed in the validation failure log in this single response, not only the first one.
- Do not add any new child input ports while repairing missing sources. That only moves the orphan and is not a repair.
- GOOD: connect an externally driven child input through top_level_connections from a compatible top-level input port.
- GOOD: connect an internally driven child input through one inter_module_signals entry whose source is a real output/inout child port and whose destinations contain the real input/inout child port.
- GOOD: when producer and consumer ports express the same semantic signal with directional suffixes (for example producer status_valid_out and consumer status_valid_in or status_valid), connect those exact declared endpoints when their widths match.
- GOOD: if several consumers use the same produced signal, place every compatible consumer endpoint in that signal's destinations list.
- GOOD: if no real producer exists, add a coherent producer output port to the responsible existing module and update that module's must_drive, behavior contract, inter_module_signals, and signal_ownership together.
- GOOD: if the orphan is state computed by the consumer module itself (for example an age counter, occupancy, or sticky status that its behavior says it tracks), remove the redundant input port and keep or add the module's output/status port. Internal state is not an external consumer.
- GOOD: when firmware writes a CSR command/setpoint and downstream logic needs a command-valid event, make the CSR/MMIO register block produce an explicit write/accept pulse and connect it to the consumer, or have the consumer derive validity from already-connected control fields and remove the redundant input.
- GOOD: in firmware-mediated request/response designs, every response field consumed by a transport, validator, or safety block must be produced by explicit CSR/MMIO register-block outputs, and the response write/commit event must produce the corresponding push/valid pulse. Do not leave firmware-written payload fields as source-less child inputs.
- GOOD: if a wholly optional helper module has no externally required behavior and none of its outputs are consumed, remove that entire module and its stale connectivity/ownership entries coherently instead of inventing meaningless traffic for it.
- BAD: rename or delete a required consumer input merely to silence validation.
- BAD: add a replacement consumer input to the same or another module without connecting it in the same response.
- BAD: invent a producer for a value that the consumer's own behavior explicitly computes internally.
- BAD: treat a consumer module's filtered/qualified valid output as the source of that same module's raw valid input; that leaves the original input orphaned or creates feedback.
- BAD: use an input port as a source, use an output port as a destination, connect incompatible widths, invent an undeclared endpoint, or connect unrelated signals merely because widths match.
- BAD: repair one listed endpoint while leaving the other endpoints from the failure log structurally undriven.
- Before returning JSON, audit every child input against top_level_connections and inter_module_signals and ensure each has exactly one semantically valid structural source.
"""
        if strict_connectivity:
            connectivity_examples += """

STRICT PASS3/PASS4 CONNECTIVITY REPAIR:
- Treat the previous JSON as a graph: child output/inout ports are producers; child input/inout ports are consumers; top-level inputs are external producers.
- First make a private checklist of every endpoint named in VALIDATION FAILURE LOG. Return JSON only, but do not finish until every checklist item has a source.
- Prefer a semantically matching declared producer of the same width. Direction suffixes may differ: producer.payload_out may drive consumer.payload_in.
- If no producer exists for a required consumer, add an OUTPUT to the responsible producer module, never another INPUT. Update that producer's behavior_rules and must_drive, then add the inter_module_signals and signal_ownership entries.
- If an orphan is a memory read-data input and memory_macros declares its memory, materialize that declared technology-neutral wrapper as a hierarchy.modules RTL deliverable and connect the complete request/read-data interface in both directions. The memory wrapper's dout is the producer; do not delete the CPU/mailbox read-data input.
- A memory_macros metadata object alone is not an instantiated child producer. Its wrapper module and exact ports must exist in hierarchy.modules and connectivity.
- If an optional helper has no required externally visible behavior and none of its outputs are consumed, remove the whole helper and all references to it.
- After editing, rebuild the consumer checklist from the returned JSON, including any ports you added. Every child input/inout must occur exactly once in either top_level_connections[].connected_to or inter_module_signals[].destinations.

GOOD EXAMPLE — reuse a semantic producer:
producer ports: payload_out(output, width 16)
consumer ports: payload_in(input, width 16)
inter_module_signals: [{"name":"payload","width":16,"source":"producer.payload_out","destinations":["consumer.payload_in"],"description":"Payload transfer."}]

GOOD EXAMPLE — create the missing producer side:
Before: sink.trigger_in is unconnected and controller has no trigger output.
After: add controller.trigger_out as output width 1, require controller to drive it, and connect controller.trigger_out to sink.trigger_in.

BAD EXAMPLE — migrate the orphan:
Before: sink.trigger_in is unconnected.
Wrong repair: add helper.trigger_in as another input, or add controller.trigger_in and use it as a source. Inputs are consumers and cannot repair a missing producer.

BAD EXAMPLE — meaningless width match:
Do not drive fifo.write_data from an unrelated status_word merely because both are the same width. Either identify the real semantic producer or remove an optional unused FIFO coherently.
"""
        if final_graph_closure:
            connectivity_examples += """

FINAL GRAPH-CLOSURE PASS:
- Earlier repair output still failed validation. Do not return the previous JSON unchanged.
- The VALIDATION FAILURE LOG is the authoritative checklist for this pass. For each named endpoint, make exactly one explicit decision: connect it to a declared output/inout producer, connect it to a compatible top-level input, or remove it only when the owning module computes that state internally.
- Status, sticky-fault, pending, and aggregate-fault consumers are not exempt from connectivity closure. A readback/status input needs a real status producer; internally held state must not also be modeled as an input; a combined fault needs an explicit aggregator output rather than an implicit Boolean expression in prose.
- Do not preserve an orphan merely because it appears in must_receive, must_not_drive, responsibilities, or behavior text. Those sections must be updated together with the port and graph decision.
- Start from the complete previous JSON and make a concrete structural change for every endpoint in the newest validation failure log.
- Re-audit every child input after those edits so the repair does not migrate or recreate an orphan.
- Preserve already-valid connectivity and architecture; this pass is focused on the remaining graph gaps.
- Use the graph diagnostics below to replace rejected attempts; do not repeat an edge whose source direction or width is invalid, and do not leave multiple producers on one child input.
"""
    fpga_memory_examples = ""
    if "fpga memory contract" in str(failure_log_text or "").lower() or "fpga-only contract" in str(failure_log_text or "").lower():
        fpga_memory_examples = """

FPGA MEMORY REPAIR EXAMPLES:
- GOOD: declare a technology-neutral wrapper in hierarchy.modules with its own rtl_output_file and synthesizable inferred-memory behavior suitable for native FPGA block RAM mapping.
- BAD: declare openram_sram, prebuilt_sky130_sram, or another ASIC hard macro in an FPGA-only contract.
- BAD: instantiate a memory module that has neither an RTL deliverable nor explicit external simulation/synthesis collateral.
"""
    # Preserve the complete prior contract across semantic repair passes.
    # Pretty model output can exceed a short excerpt and previously lost late
    # sections (notably register_contract). Compact valid JSON first so the
    # prompt retains substantially more contract information.
    previous_contract = str(previous_json_text or "")
    try:
        previous_contract = json.dumps(
            _parse_llm_json_object(previous_contract),
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (JSONDecodeError, ValueError, TypeError):
        pass

    original_contract_excerpt = (
        "Omitted in final graph-closure mode; the complete previous JSON below is the authoritative design."
        if final_graph_closure
        else _truncate_text(base_prompt, 24000)
    )
    failure_section = _truncate_text(failure_log_text, 4000)
    return f"""
==============================
REPAIR MODE (SECOND PASS)
==============================

Your previous JSON did not pass contract validation.

VALIDATION FAILURE LOG (AUTHORITATIVE REPAIR CHECKLIST):
{failure_section}

You MUST preserve the same architecture unless a structural change is strictly required to fix the validation errors.

ORIGINAL GENERATION CONTRACT EXCERPT:
{original_contract_excerpt}

PREVIOUS JSON:
{_truncate_text(previous_contract, 70000)}

VALIDATION FAILURE LOG:
{failure_section}

REPAIR RULES:
- Do NOT redesign the architecture unless required to resolve the errors
- Preserve module names, hierarchy, ports, and intent as much as possible
- Fix only structural inconsistencies needed for contract closure
- Return ONE full corrected JSON object only
- Do NOT return partial edits
- Do NOT return explanations
{firmware_examples}
{hierarchy_examples}
{connectivity_examples}
{graph_diagnostics}
{fpga_memory_examples}
""".strip()


def _build_json_syntax_repair_prompt(previous_json_text: str, failure_text: str) -> str:
    return f"""
==============================
JSON SYNTAX REPAIR MODE
==============================

The previous response is intended to be one JSON object but it is not parseable.

Rules:
- Return ONE complete JSON object only.
- Do not add markdown fences, comments, explanations, or prose.
- Preserve all keys, module names, ports, requirements, behavior rules, and numeric values.
- Fix only JSON syntax issues such as missing commas, unterminated strings, trailing prose, bad escaping, or truncated wrapper text.
- Do not simplify the design and do not remove requirements to make parsing easier.

JSON parse failure:
{_truncate_text(failure_text, 5000)}

Previous JSON text:
{_truncate_text(previous_json_text, 24000)}
""".strip()


def _compile_spec_contract(
    llm_output: str,
    spec_dir: str,
    suffix: str = "",
    requested_top: str = "",
    source_prompt: str = "",
    require_firmware_control_plane: bool = False,
):
    logger.info(f"🔍 Digital Spec Agent compile start suffix='{suffix or 'pass1'}'")
    raw_name = f"llm_raw_output{suffix}.txt"
    raw_output_path = os.path.join(spec_dir, raw_name)
    with open(raw_output_path, "w", encoding="utf-8") as rf:
        rf.write(llm_output)

    parsed_json = _parse_llm_json_object(llm_output)
    parsed_json = _merge_prompt_memory_macros(parsed_json, source_prompt)
    logger.info(f"🔍 Digital Spec Agent JSON parsed suffix='{suffix or 'pass1'}'")
    spec_json, mode = _normalize_spec_json(parsed_json)
    logger.info(f"🔍 Digital Spec Agent normalized mode={mode} suffix='{suffix or 'pass1'}'")
    spec_json = _apply_requested_top_module(spec_json, mode, requested_top)
    spec_json = _repair_empty_top_ports_from_prompt(spec_json, mode, source_prompt)
    if requested_top:
        logger.info(
            "Digital Spec Agent enforced requested top_module=%s suffix='%s'",
            requested_top,
            suffix or "pass1",
        )
    _reject_requested_top_memory_interface(spec_json, mode, requested_top)

    if mode == "hierarchical":
        spec_json = _normalize_memory_wrapper_port_directions(spec_json, mode)
        spec_json = _ensure_hierarchical_top_level_connections(spec_json)
        spec_json = _ensure_hierarchical_inter_module_signals(spec_json)
        # Reject stale endpoints against the declared contract before port
        # closure. Otherwise an invalid connection/ownership claim can create
        # the very port that makes itself appear valid.
        spec_json = _sanitize_hierarchical_connectivity(spec_json)
        spec_json = _ensure_hierarchical_port_closure(spec_json)
        spec_json = _reconcile_hierarchical_signal_directions(spec_json, mode)
        spec_json = _sanitize_hierarchical_connectivity(spec_json)
        spec_json = _remove_self_owned_alias_inputs(spec_json)
        spec_json = _enforce_prompt_top_ports_after_hierarchy_repair(spec_json, mode, source_prompt)
        logger.info(f"🔍 Digital Spec Agent hierarchical port closure done suffix='{suffix or 'pass1'}'")

   
    normalized_name = "spec_agent_normalized.json" if not suffix else f"spec_agent_normalized{suffix}.json"
    normalized_path = os.path.join(spec_dir, normalized_name)
    with open(normalized_path, "w", encoding="utf-8") as nf:
        json.dump(spec_json, nf, indent=2)

    _validate_spec_contract(spec_json, mode)
    _validate_mandatory_firmware_control_plane(
        spec_json,
        mode,
        source_prompt,
        required=require_firmware_control_plane,
    )
    _validate_fpga_memory_contract(spec_json, source_prompt)
    logger.info(f"✅ Digital Spec Agent contract compile passed suffix='{suffix or 'pass1'}'")
    return spec_json, mode, raw_output_path, normalized_path


def _validate_mandatory_firmware_control_plane(
    spec_json: dict,
    mode: str,
    source_prompt: str,
    *,
    required: bool = False,
) -> None:
    """Reject a spec that drops an explicitly required firmware control plane."""
    if not required and "FIRMWARE CONTROL-PLANE CONTRACT (mandatory)" not in str(source_prompt or ""):
        return
    contract = spec_json.get("register_contract") if isinstance(spec_json, dict) else None
    registers = contract.get("registers") if isinstance(contract, dict) else None
    bus = str((contract or {}).get("bus_type") or (contract or {}).get("bus") or "").strip()
    if not bus or not isinstance(registers, list) or not registers:
        raise ValueError(
            "Mandatory firmware control-plane contract is missing a concrete register_contract bus and registers"
        )
    if mode == "hierarchical":
        top = ((spec_json.get("hierarchy") or {}).get("top_module") or {})
    else:
        top = spec_json
    port_directions = {
        str(port.get("name") or "").lower(): str(port.get("direction") or "").lower()
        for port in (top.get("ports") or [])
        if isinstance(port, dict) and str(port.get("name") or "").strip()
    }
    names = set(port_directions)
    is_wishbone = "wishbone" in bus.lower()
    # Interface signals commonly carry a final direction suffix (for example,
    # ``csr_we_i`` or ``apb_pwrite_i``).  Ignore that suffix when matching
    # semantic strobe endings so conventional HDL port names are accepted.
    strobe_names = names | {
        name[: -len(direction_suffix)]
        for name in names
        for direction_suffix in ("_in", "_out", "_i", "_o")
        if name.endswith(direction_suffix)
    }

    def has_any(tokens: tuple[str, ...]) -> bool:
        return any(any(token in name for token in tokens) for name in names)

    def has_strobe(suffixes: tuple[str, ...]) -> bool:
        return any(
            any(name.endswith(suffix) or name == suffix.lstrip("_") for suffix in suffixes)
            for name in strobe_names
        )

    missing = []
    if not has_any(("addr", "address", "adr")):
        missing.append("address")
    wishbone_write_data = is_wishbone and any(
        ("dat" in name or "data" in name) and direction in {"input", "inout"}
        for name, direction in port_directions.items()
    )
    wishbone_read_data = is_wishbone and any(
        ("dat" in name or "data" in name) and direction in {"output", "inout"}
        for name, direction in port_directions.items()
    )
    if not has_any(("wdata", "wr_data", "write_data", "pwdata")) and not wishbone_write_data:
        missing.append("write-data")
    if not has_any(("rdata", "rd_data", "read_data", "prdata")) and not wishbone_read_data:
        missing.append("read-data")
    if not (
        has_any(("valid", "select", "sel", "enable", "psel", "cyc", "stb"))
        or has_strobe(("_we", "_wen", "_re", "_ren", "_write", "_read"))
    ):
        missing.append("transaction-valid/select")
    if not (
        has_any(("write", "wr_en", "write_en", "pwrite"))
        or has_strobe(("_we", "_wen"))
    ):
        missing.append("write-enable")
    if not has_any(("ready", "response", "resp", "ack", "pready")):
        missing.append("response/ready")
    if missing:
        raise ValueError(
            "Mandatory firmware control-plane top interface is incomplete; missing " + ", ".join(missing)
        )


def _validate_fpga_memory_contract(spec_json: dict, source_prompt: str) -> None:
    """Keep FPGA-only specifications free of ASIC/generated hard macros."""
    prompt = str(source_prompt or "")
    if "FPGA MEMORY CONTRACT (mandatory)" not in prompt:
        return
    forbidden_kinds = {
        "openram_sram",
        "prebuilt_sky130_sram",
        "prebuilt_sram",
        "precompiled_sram_macro",
        "sky130_sram",
    }
    violations = []
    for macro in spec_json.get("memory_macros", []) or []:
        if not isinstance(macro, dict):
            continue
        kind = str(macro.get("kind") or "").strip().lower()
        if kind in forbidden_kinds:
            violations.append(f"{macro.get('name') or 'unnamed'} ({kind})")
    if violations:
        raise ValueError(
            "FPGA memory contract is FPGA-only and cannot use ASIC/OpenRAM hard macros: "
            + ", ".join(violations[:8])
            + ". Use a technology-neutral inferred-memory wrapper declared in hierarchy.modules "
              "with its own rtl_output_file."
        )


def _write_contract_failure_log(spec_dir: str, filename: str, err: Exception) -> str:
    log_path = os.path.join(spec_dir, filename)
    _write_text(log_path, f"Digital Spec Agent parse/normalize failure:\n{err}\n")
    return log_path

def run_agent(state: dict) -> dict:
    print("\n🚀 Running Digital Spec Agent (contract-only mode)...")
    agent_name = "Digital Spec Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Restore local directory structure
    spec_dir = os.path.join(workflow_dir, "spec")
    os.makedirs(spec_dir, exist_ok=True)

    entry_path = os.path.join(spec_dir, "spec_agent_entry.json")
    entry_payload = {
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
        "spec_dir": spec_dir,
        "state_keys": sorted(list(state.keys())),
        "input_candidates": {
            "spec": state.get("spec"),
            "spec_text": state.get("spec_text"),
            "digital_spec": state.get("digital_spec"),
            "digital_spec_text": state.get("digital_spec_text"),
            "soc_spec": state.get("soc_spec"),
            "system_spec": state.get("system_spec"),
            "description": state.get("description"),
        },
    }
    with open(entry_path, "w", encoding="utf-8") as ef:
        json.dump(entry_payload, ef, indent=2, default=str)

    user_prompt = (
        state.get("spec")
        or state.get("spec_text")
        or state.get("digital_spec")
        or state.get("digital_spec_text")
        or state.get("soc_spec")
        or state.get("system_spec")
        or state.get("description")
        or ""
    ).strip()
    try:
        requested_top = _requested_top_module(state)
    except ValueError as e:
        log_path = os.path.join(spec_dir, "spec_agent_contract.log")
        summary_path = os.path.join(spec_dir, "spec_agent_summary.txt")

        _write_text(log_path, f"Digital Spec Agent aborted: {e}\n")
        _write_text(summary_path, f"Digital Spec Agent failed.\n\nReason: {e}\n")

        state.update({
            "status": f"Spec input invalid: {e}",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
            "issues": [str(e)],
        })
        _upload_spec_debug_artifacts(workflow_id, agent_name, spec_dir)
        return state

    input_snapshot = os.path.join(spec_dir, "spec_agent_input.txt")
    _write_text(input_snapshot, user_prompt if user_prompt else "<EMPTY>")


    if not user_prompt:
        log_path = os.path.join(spec_dir, "spec_agent_contract.log")
        summary_path = os.path.join(spec_dir, "spec_agent_summary.txt")

        _write_text(log_path, "Digital Spec Agent aborted: no spec provided.\n")
        _write_text(summary_path, "❌ Digital Spec Agent failed.\n\nReason: no spec provided.\n")

        state.update({
            "status": "❌ No spec provided",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
            "issues": ["No spec provided"],
        })
        _upload_spec_debug_artifacts(workflow_id, agent_name, spec_dir)
        return state

    prompt = f"""
USER DIGITAL SPECIFICATION:
{user_prompt}

REQUESTED_TOP_MODULE:
{requested_top or "null"}

You are a professional ASIC digital architect.

Your task is to generate ONLY the authoritative digital design contract as JSON.
Do NOT generate RTL.
Do NOT generate Verilog.
Do NOT include markdown.
Do NOT include prose before or after JSON.

STRICT OUTPUT RULES
- Output ONLY one raw JSON object.
- No markdown fences.
- JSON must parse with json.loads().

IMPORTANT
You may output EITHER of these two valid forms.

VALID FORM A — Flat single-module form:
{{
  "name": "module_name",
  "description": "Explain purpose.",
  "operating_constraints": {{
    "clock_domains": [
      {{
        "name": "clk",
        "frequency_mhz": 50.0,
        "period_ns": 20.0
      }}
    ],
    "reset_signals": [
      {{
        "name": "reset_n",
        "active_low": true,
        "async": false
      }}
    ],
    "fixed_assumptions": []
  }},
  "memory_macros": [
    {{
      "name": "openram_sram_64x32",
      "kind": "openram_sram",
      "depth": 64,
      "data_width": 32,
      "addr_width": 6,
      "instance_name": "u_sram",
      "ports": {{
        "clk": "clk",
        "csb": "csb",
        "we": "web",
        "addr": "addr",
        "din": "din",
        "dout": "dout"
      }},
      "requires_mbist": false
    }}
  ],
  "ports": [
    {{"name": "clk", "direction": "input", "width": 1}},
    {{"name": "reset_n", "direction": "input", "width": 1, "active_low": true}},
    {{"name": "enable", "direction": "input", "width": 1}},
    {{"name": "count", "direction": "output", "width": 4}}
  ],
  "functionality": "Full descriptive functionality.",
  "responsibilities": ["..."],
  "must_drive": ["..."],
  "must_receive": ["..."],
  "must_not_drive": ["..."],
  "reset_behavior": "Describe reset behavior.",
  "behavior_rules": ["..."],
  "rtl_output_file": "module_name.v"
}}

VALID FORM B — Hierarchical multi-module form:
{{
  "design_name": "top_module_name",
  "design_summary": "High-level design summary.",
  "operating_constraints": {{
    "clock_domains": [
      {{
        "name": "clk",
        "frequency_mhz": 50.0,
        "period_ns": 20.0
      }}
    ],
    "reset_signals": [
      {{
        "name": "rst_n",
        "active_low": true,
        "async": false
      }}
    ],
    "fixed_assumptions": []
  }},
  "memory_macros": [
    {{
      "name": "openram_sram_64x32",
      "kind": "openram_sram",
      "depth": 64,
      "data_width": 32,
      "addr_width": 6,
      "instance_name": "u_sram",
      "ports": {{
        "clk": "clk",
        "csb": "csb",
        "we": "web",
        "addr": "addr",
        "din": "din",
        "dout": "dout"
      }},
      "requires_mbist": false
    }}
  ],
  "hierarchy": {{
    "top_module": {{
      "name": "top_module_name",
      "description": "Describe top-level integration.",
      "ports": [],
      "functionality": "Full top-level functional description.",
      "responsibilities": ["..."],
      "must_drive": ["..."],
      "must_receive": ["..."],
      "must_not_drive": ["..."],
      "reset_behavior": "Describe reset behavior.",
      "behavior_rules": ["..."],
      "rtl_output_file": "top_module_name.v"
    }},
    "modules": [
      {{
        "name": "sub_module_a",
        "description": "Purpose of submodule.",
        "ports": [],
        "functionality": "Full detailed submodule functionality from the datasheet/spec.",
        "responsibilities": ["..."],
        "must_drive": ["..."],
        "must_receive": ["..."],
        "must_not_drive": ["..."],
        "reset_behavior": "Describe reset behavior.",
        "behavior_rules": ["..."],
        "rtl_output_file": "sub_module_a.v"
      }}
    ]
  }},
  "top_level_connections": [
    {{
      "top_port": "clk",
      "connected_to": ["sub_module_a.clk", "sub_module_b.clk"],
      "description": "How a top-level port connects into submodules."
    }}
  ],
  "inter_module_signals": [
    {{
      "name": "internal_signal_name",
      "width": 1,
      "source": "producer_module.producer_port",
      "destinations": ["consumer_module.consumer_port"],
      "description": "Internal signal connection."
    }}
  ],
  "signal_ownership": [
    {{
      "signal": "internal_signal_name",
      "owner": "producer_module.producer_port"
    }},
    {{
      "signal": "top_output_signal",
      "owner": "owning_module.output_port"
    }}
  ],
  "register_contract": {{
    "bus_type": "custom|i2c|abstract|minimal",
    "registers": []
  }}
}}

RULES
- If the design is truly just one module, output the flat single-module form.
- If the design has internal hierarchy, output the hierarchical form.
- Define exact module names.
- If REQUESTED_TOP_MODULE is not null, the top-level module name MUST be exactly that value.
- Suffixes such as _mmio, _wrapper, or _rtl are allowed for child/internal modules when useful.
- If the design needs an MMIO/register/bus wrapper and REQUESTED_TOP_MODULE is not null, keep REQUESTED_TOP_MODULE as the top-level module and place any suffixed wrapper below it or fold the wrapper behavior into that top.
- If REQUESTED_TOP_MODULE is not null, the top-level rtl_output_file MUST be REQUESTED_TOP_MODULE plus a Verilog extension.
- Define exact ports.
- Define exact rtl_output_file names.
- Every port must include name, direction, width.
- direction must be input/output/inout.
- width must be integer >= 1.
- For EVERY module, preserve rich functionality from the user datasheet/spec.
- For EVERY module, include responsibilities, must_drive, must_receive, must_not_drive, reset_behavior, behavior_rules.
- Preserve exact signal ownership.
- Preserve exact internal interface contracts.
- Preserve exact fixed clock frequency if the user specifies it.
- If the user asks for SRAM/OpenRAM/prebuilt SRAM/MBIST/memory macro behavior, include memory_macros[] with exact SRAM macro requirements.
- memory_macros[].name must be the SRAM macro module/cell name the RTL should instantiate.
- memory_macros[] is the authoritative implementation contract. Do not introduce a differently named wrapper, fallback model, behavioral SRAM module, or inferred-memory alternative in module descriptions, responsibilities, functionality, or behavior_rules.
- When kind is prebuilt_sky130_sram, prebuilt_sram, or precompiled_sram_macro, the functional RTL hierarchy must instantiate memory_macros[].name using memory_macros[].instance_name, either directly in the controller or through an explicitly requested functional wrapper; descriptive prose must not offer a substitute implementation.
- A simulation model may be supplied as external collateral under the exact same memory_macros[].name. Do not create a second RTL module identity for simulation.
- memory_macros[].kind must distinguish intent, for example openram_sram for generated OpenRAM or prebuilt_sky130_sram/prebuilt_sram for explicit existing macro collateral.
- memory_macros[].depth, data_width, and addr_width must match the requested memory capacity.
- memory_macros[].ports must map canonical roles clk, csb, we, addr, din, dout to real RTL port names.
- If MBIST is requested or likely required, set memory_macros[].requires_mbist true; otherwise false.
- Do not replace an OpenRAM SRAM requirement with a register array in the spec.
- For hierarchical designs, top_level_connections, inter_module_signals, and signal_ownership are mandatory and must be non-empty.
- top_level_connections must describe how top-level ports connect to submodule ports.
- inter_module_signals must describe how submodules connect to each other.
- signal_ownership must identify the only legal driver of each internally-driven or externally-driven signal.

PORT COMPLETENESS AND ENDPOINT RULES FOR HIERARCHICAL DESIGNS:
1. Do NOT leave hierarchical submodule ports empty.
2. Every endpoint referenced in top_level_connections must exist as a real port in the referenced module.
3. Every source and destination referenced in inter_module_signals must exist as a real port in the referenced module.
4. Every owner referenced in signal_ownership must exist as a real port in the referenced module.
5. Use the connectivity endpoints to derive complete submodule port lists.

STRICT CONNECTIVITY FORMAT RULES:
1. In hierarchical mode, every inter_module_signals[].source MUST be exactly "module.port".
2. In hierarchical mode, every inter_module_signals[].destinations[] entry MUST be exactly "module.port".
3. In hierarchical mode, every signal_ownership[].owner MUST be exactly "module.port".
4. Never use a bare module name as an endpoint. Examples of INVALID endpoints: "i2c_slave", "register_map".
5. Never use grouped, abstract, bundled, or placeholder connectivity names such as:
   - reg_bus_signals
   - adc_status_signals
   - irq_signals
   - control_bus
   - data_bus
   - status_bus
   - internal_bus
   - grouped_signals
   If a bus really exists, it must be represented as a real module port with an exact port name and width.
6. Do NOT summarize an interface as one grouped connection. Instead, expand it into explicit signal-level entries, one per real signal.
7. If one module communicates multiple control/data/status signals to another module, list each signal separately in inter_module_signals with the exact producer port and consumer port.
8. Every inter-module signal name must represent a real explicit signal, not a conceptual bundle.
9. Every inter_module_signals[].source endpoint must be a producer port declared as output or inout.
10. Every inter_module_signals[].destinations[] endpoint must be a consumer port declared as input or inout.
11. Never list an output port as an inter_module_signals destination.
12. Never list an input port as an inter_module_signals source.

STRICT INTER-MODULE SIGNAL OBJECT RULES:
1. Every object in inter_module_signals MUST include ALL of these fields:
   - name
   - width
   - source
   - destinations
   - description
2. inter_module_signals[].width is mandatory for EVERY entry.
3. inter_module_signals[].width must be an integer >= 1.
4. Never omit width, even for single-bit signals. Use width: 1 explicitly.
5. width must match the real width of the connected producer and consumer ports.
6. The signal name should match the actual transferred signal, not an abstract interface name.

STRICT ENDPOINT TOKEN RULES:
1. Every inter_module_signals[].source must be exactly "module.port" with no bit-slice suffix.
2. Every inter_module_signals[].destinations[] entry must be exactly "module.port" with no bit-slice suffix.
3. Do NOT use endpoints like module.port[11:0] or module.port[0].
4. Width belongs in the width field, not in the endpoint token.
5. Example:
   VALID: "source": "analog_if_logic.adc_data_sync", "width": 12
   INVALID: "source": "analog_if_logic.adc_data_sync[11:0]"

STRICT TOP-LEVEL CONNECTION RULES:
1. Every object in top_level_connections MUST include:
   - top_port
   - connected_to
   - description
2. top_port must be the exact top-level port name.
3. Every connected_to entry must be exactly "module.port".
4. Do NOT connect a top-level port to a bare module name.
5. If top_port is a top-level input, connected_to endpoints must be child input/inout ports.
6. If top_port is a top-level output, connected_to endpoints must be child output/inout ports that drive the top output.
7. Do NOT also list a top-level output's child driver as an inter_module_signals destination.

STRICT SIGNAL OWNERSHIP RULES:
1. Every object in signal_ownership MUST include:
   - signal
   - owner
2. signal_ownership[].owner must be exactly "module.port".
3. signal_ownership[].signal must refer to a real explicit signal name.
4. For internal inter-module signals, signal_ownership[].signal should match an entry from inter_module_signals[].name.
5. For top-level externally-driven outputs, signal_ownership[].signal may be the top-level output signal name, but owner must still be the exact producing module.port.
6. Do NOT assign ownership to abstract interfaces, grouped buses, bundles, or bare modules.

STRICT PORT CLOSURE RULES:
1. Every signal referenced anywhere as module.port MUST exist as a real port in that module's ports[] list.
2. If a signal appears in inter_module_signals as source "module.port", then that exact port name MUST be declared in module.ports[].
3. If a signal appears in inter_module_signals as destination "module.port", then that exact port name MUST be declared in module.ports[].
4. If a signal appears in signal_ownership as owner "module.port", then that exact port name MUST be declared in module.ports[].
5. If a signal appears in top_level_connections as "module.port", then that exact port name MUST be declared in module.ports[].
6. If a module lists a signal in must_drive, that signal must either:
   - be declared as an output/inout port in that module, or
   - be explicitly described as purely internal and therefore MUST NOT appear in inter_module_signals, top_level_connections, or signal_ownership.
7. If a module lists a signal in must_receive, that signal must either:
   - be declared as an input/inout port in that module, or
   - be explicitly described as purely internal and therefore MUST NOT appear in inter_module_signals, top_level_connections, or signal_ownership.
8. For hierarchical designs, any signal exchanged between two modules MUST be represented as real ports on both modules.
9. Do NOT mention a signal in must_drive/must_receive and then omit it from ports[] if that signal is used for module-to-module connectivity.

SEMANTIC SIGNAL RESOLUTION RULES (CRITICAL)

1. Distinguish between:
   - Transport signals (e.g., reg_wdata, reg_addr, reg_wr_en)
   - Semantic signals (e.g., cfg_enable, start, mode, threshold, dac_code, irq)

2. If ANY module consumes semantic signals (cfg_*, enable, start, mode, data, etc.):
   THEN those signals MUST have an explicit producer in inter_module_signals.

3. Do NOT assume semantic signals can be derived later from transport buses.

   ❌ INVALID:
   control_fsm.cfg_enable exists
   but no inter_module_signals defines who produces cfg_enable

4. If a module (e.g., register_map, decoder, controller) is responsible for decoding transport data:
   THEN it MUST expose semantic outputs explicitly as ports.

   Example:
   register_map MUST include:
   - cfg_enable
   - cfg_adc_start
   - cfg_dac_enable
   - cfg_dac_code

5. Every semantic signal must follow FULL CONTRACT CLOSURE:

   For each signal S:
   - Declared in inter_module_signals
   - Exists as source module.port
   - Exists as destination module.port
   - Appears in signal_ownership
   - Appears in module.ports[]

6. Multi-register or encoded signals must be represented as FINAL semantic signals:

   Example:
   If DAC code is 12-bit split across registers:
   - Represent ONLY:
     register_map.cfg_dac_code (width=12)

   Do NOT expose:
   - raw reg_wdata bits
   - partial slices
   - implicit packing

7. NEVER connect semantic inputs directly from raw transport buses unless explicitly defined.

   ❌ INVALID:
   control_fsm.cfg_dac_code ← register_map.reg_wdata

8. If semantic signal exists in must_receive or must_drive:
   it MUST be implemented as a real port AND connected via inter_module_signals.

9. Avoid "hidden derivation":
   Every signal consumed by a module must be traceable through:

   producer → inter_module_signals → consumer

10. If unsure, prefer explicit semantic signals over implicit bus reuse.


STRICT CLOCK/RESET PROPAGATION RULES:
1. If a clock or reset signal appears in top_level_connections, then every referenced destination endpoint MUST be declared as a real port in that destination module.
2. If top_level_connections includes endpoints like "some_module.clk", "some_module.rst_n", "some_module.reset_n", or "some_module.reset", then that exact port name MUST appear in some_module.ports[].
3. Do NOT connect top-level clock/reset signals to a submodule unless that submodule explicitly declares the matching clock/reset port.
4. If a submodule participates in synchronous logic, sampled logic, register interfaces, control sequencing, state machines, or synchronized data paths, include an explicit clock port and reset port for that submodule unless the user spec clearly says otherwise.
5. For hierarchical designs, clock/reset connectivity and module port lists must be mutually consistent:
   - every referenced clock/reset connection must have a corresponding module port
   - every declared module clock/reset port that is intended to be driven from the top should appear in top_level_connections
6. Use consistent reset naming across ports and connections. Do not mix rst_n, reset_n, rst, and reset unless the user spec explicitly requires different names.
7. Before finalizing JSON, verify that every destination referenced by a top-level reset or clock connection exists verbatim in the destination module port list.

HIERARCHICAL CONNECTIVITY EXAMPLES:
VALID:
- {{"name":"reg_wr_en","width":1,"source":"i2c_slave.reg_wr_en","destinations":["register_map.reg_wr_en"],"description":"Register write enable."}}
- {{"name":"reg_rd_en","width":1,"source":"i2c_slave.reg_rd_en","destinations":["register_map.reg_rd_en"],"description":"Register read enable."}}
- {{"name":"reg_addr","width":8,"source":"i2c_slave.reg_addr","destinations":["register_map.reg_addr"],"description":"Register address bus."}}
- {{"name":"reg_wdata","width":8,"source":"i2c_slave.reg_wdata","destinations":["register_map.reg_wdata"],"description":"Register write data bus."}}
- {{"name":"reg_rdata","width":8,"source":"register_map.reg_rdata","destinations":["i2c_slave.reg_rdata"],"description":"Register read data bus."}}
- {{"signal":"reg_wr_en","owner":"i2c_slave.reg_wr_en"}}
- {{"signal":"reg_rdata","owner":"register_map.reg_rdata"}}

INVALID:
- {{"name":"reg_wr_en","source":"i2c_slave.reg_wr_en","destinations":["register_map.reg_wr_en"],"description":"Missing width"}}
- {{"name":"reg_bus_signals","width":8,"source":"i2c_slave","destinations":["register_map"],"description":"Grouped abstract interface"}}
- {{"signal":"register_bus","owner":"i2c_slave"}}
- {{"top_port":"irq","connected_to":["interrupt_controller"],"description":"Bare module endpoint is invalid"}}

VALID CLOCK/RESET EXAMPLE:
top_level_connections:
- {{"top_port":"clk","connected_to":["i2c_slave.clk","register_map.clk","analog_if_logic.clk"],"description":"Top clock fanout."}}
- {{"top_port":"rst_n","connected_to":["i2c_slave.rst_n","register_map.rst_n","analog_if_logic.rst_n"],"description":"Top active-low reset fanout."}}
Then the following ports MUST exist:
- i2c_slave.ports includes "clk" and "rst_n"
- register_map.ports includes "clk" and "rst_n"
- analog_if_logic.ports includes "clk" and "rst_n"

INVALID CLOCK/RESET EXAMPLE:
- {{"top_port":"rst_n","connected_to":["analog_if_logic.rst_n"],"description":"Invalid if analog_if_logic.ports does not include rst_n"}}


FINAL SELF-CHECK BEFORE OUTPUT:
Before emitting the JSON, verify ALL of the following:
1. Hierarchical submodule ports are non-empty.
2. Every referenced endpoint exists as a real declared port.
3. Every inter_module_signals entry has name, width, source, destinations, description.
4. Every inter_module_signals width is an integer >= 1.
5. Every source/destination/owner endpoint uses exact module.port format.
6. No grouped or placeholder signal names are used.
7. signal_ownership aligns with explicit real signals.
8. The JSON is complete and parseable with json.loads().
9. Every top-level clock/reset connection is mirrored by an exact matching port in the referenced submodule.
10. No top_level_connections entry may reference module.port unless that exact port string is present in that module's ports[] list.
11. Every signal named in must_drive or must_receive that participates in module-to-module connectivity is declared in that module's ports[] list.
12. No inter_module_signals endpoint contains bit slicing such as [11:0]; width is expressed only by the width field.
13. For every inter_module_signals entry, both producer and consumer modules declare the referenced port names exactly.
14. For every module input port that is NOT a top-level connection:
    there must exist exactly one inter_module_signals entry that drives it.
15. No module input port may remain "unexplained" (i.e., not connected via contract).
16. No semantic signal (cfg_*, enable, mode, data, etc.) may be sourced from a transport bus unless explicitly defined in inter_module_signals.
17. For every inter_module_signals entry, source direction is output/inout and every destination direction is input/inout.
18. For every top_level_connections entry, endpoint directions match the top port direction.

If the user spec is incomplete, choose the simplest valid architecture ONCE and encode it here.
This JSON becomes the source of truth for downstream agents.

Return JSON only.
""".strip()

    try:
        logger.info(f"Digital Spec Agent pass1 prompt size: {len(prompt)} chars")
        t0 = time.monotonic()
        llm_output = _complete_spec_generation(prompt, agent_name, state, "pass1")
        logger.info(f"Digital Spec Agent pass1 LLM elapsed: {time.monotonic() - t0:.2f}s")
    except Exception as e:
            log_path = os.path.join(spec_dir, "spec_agent_contract.log")
            summary_path = os.path.join(spec_dir, "spec_agent_summary.txt")

            _write_text(log_path, f"Digital Spec Agent LLM failure:\n{e}\n")
            _write_text(summary_path, f"❌ Digital Spec Agent failed.\n\nLLM generation failed: {e}\n")

            state.update({
                "status": f"❌ LLM generation failed: {e}",
                "artifact": None,
                "artifact_list": [],
                "artifact_log": log_path,
                "workflow_dir": workflow_dir,
                "workflow_id": workflow_id,
                "issues": [f"LLM generation failed: {e}"],
            })
            _upload_spec_debug_artifacts(workflow_id, agent_name, spec_dir)

            return state

    pass1_error = None
    pass2_error = None
    require_firmware_control_plane = bool(state.get("require_firmware_control_plane"))

    try:
        spec_json, mode, raw_output_path, normalized_path = _compile_spec_contract(
            llm_output=llm_output,
            spec_dir=spec_dir,
            suffix="",
            requested_top=requested_top,
            source_prompt=user_prompt,
            require_firmware_control_plane=require_firmware_control_plane,
        )
    except Exception as e:
        pass1_error = e

        # Keep pass1 logs/artifacts exactly as today
        log_path = os.path.join(spec_dir, "spec_agent_contract.log")
        summary_path = os.path.join(spec_dir, "spec_agent_summary.txt")
        exc_path = os.path.join(spec_dir, "spec_agent_exception.txt")

        _write_text(log_path, f"Digital Spec Agent parse/normalize failure:\n{e}\n")
        _write_text(summary_path, f"❌ Digital Spec Agent failed.\n\nPass1 JSON parse/normalize failed: {e}\n")
        _write_text(exc_path, repr(e))

        # Pass2 only if pass1 compile failed
        repair_prompt = _build_repair_prompt(
            base_prompt=prompt,
            previous_json_text=llm_output,
            failure_log_text=str(e),
        )

        try:
            logger.warning(f"❌ Digital Spec Agent pass1 contract compile failed: {e}")
            logger.info("🔁 Digital Spec Agent invoking pass2 repair flow")
            logger.info(f"Digital Spec Agent pass2 prompt size: {len(repair_prompt)} chars")
            t0 = time.monotonic()
            llm_output_pass2 = _complete_spec_generation(repair_prompt, agent_name, state, "pass2")
            logger.info(f"Digital Spec Agent pass2 LLM elapsed: {time.monotonic() - t0:.2f}s")
            logger.info(f"🧠 Digital Spec Agent pass2 LLM output size: {len(llm_output_pass2)} chars")
        except Exception as e2:
            logger.error(f"❌ Digital Spec Agent pass2 contract compile failed: {e2}")
            pass2_error = e2
            pass2_log_path = os.path.join(spec_dir, "spec_agent_contract_pass2.log")
            pass2_exc_path = os.path.join(spec_dir, "spec_agent_exception_pass2.txt")

            _write_text(pass2_log_path, f"Digital Spec Agent PASS2 LLM failure:\n{e2}\n")
            _write_text(pass2_exc_path, repr(e2))

            state.update({
                "status": f"❌ Pass1 failed and Pass2 LLM generation failed: {e2}",
                "artifact": None,
                "artifact_list": [],
                "artifact_log": log_path,
                "workflow_dir": workflow_dir,
                "workflow_id": workflow_id,
                "issues": [
                    f"Pass1 JSON parse/normalize failed: {pass1_error}",
                    f"Pass2 LLM generation failed: {pass2_error}",
                ],
            })

            _upload_spec_debug_artifacts(workflow_id, agent_name, spec_dir)
            return state

        try:
            spec_json, mode, raw_output_path_pass2, normalized_path_pass2 = _compile_spec_contract(
                llm_output=llm_output_pass2,
                spec_dir=spec_dir,
                suffix="_pass2",
                requested_top=requested_top,
                source_prompt=user_prompt,
                require_firmware_control_plane=require_firmware_control_plane,
            )
            raw_output_path = raw_output_path_pass2
            
        except Exception as e2:
            pass2_error = e2
            pass2_log_path = os.path.join(spec_dir, "spec_agent_contract_pass2.log")
            pass2_exc_path = os.path.join(spec_dir, "spec_agent_exception_pass2.txt")

            _write_text(pass2_log_path, f"Digital Spec Agent parse/normalize failure:\n{e2}\n")
            _write_text(pass2_exc_path, repr(e2))

            # A syntax-only pass intentionally preserves keys, so it cannot
            # repair semantic failures such as a missing register contract or
            # invalid connectivity. Route semantic failures back through the
            # full contract repair prompt.
            if isinstance(e2, JSONDecodeError):
                pass3_prompt = _build_json_syntax_repair_prompt(
                    previous_json_text=llm_output_pass2,
                    failure_text=_json_error_context(llm_output_pass2, e2),
                )
                pass3_mode = "syntax_repair"
            else:
                pass3_prompt = _build_repair_prompt(
                    base_prompt=prompt,
                    previous_json_text=llm_output_pass2,
                    failure_log_text=str(e2),
                    strict_connectivity=True,
                )
                pass3_mode = "contract_repair_pass3"
            llm_output_pass3 = llm_output_pass2
            try:
                logger.info(f"Digital Spec Agent invoking pass3 {pass3_mode} flow")
                logger.info(f"Digital Spec Agent pass3 prompt size: {len(pass3_prompt)} chars")
                t0 = time.monotonic()
                llm_output_pass3 = _complete_spec_generation(pass3_prompt, agent_name, state, pass3_mode)
                logger.info(f"Digital Spec Agent pass3 LLM elapsed: {time.monotonic() - t0:.2f}s")
                logger.info(f"Digital Spec Agent pass3 LLM output size: {len(llm_output_pass3)} chars")
                spec_json, mode, raw_output_path_pass3, normalized_path_pass3 = _compile_spec_contract(
                    llm_output=llm_output_pass3,
                    spec_dir=spec_dir,
                    suffix="_pass3",
                    requested_top=requested_top,
                    source_prompt=user_prompt,
                    require_firmware_control_plane=require_firmware_control_plane,
                )
                raw_output_path = raw_output_path_pass3
            except Exception as e3:
                pass3_log_path = os.path.join(spec_dir, "spec_agent_contract_pass3.log")
                pass3_exc_path = os.path.join(spec_dir, "spec_agent_exception_pass3.txt")
                _write_text(pass3_log_path, f"Digital Spec Agent pass3 repair failure ({pass3_mode}):\n{e3}\n")
                _write_text(pass3_exc_path, repr(e3))

                final_contract_repair_prompt = _build_repair_prompt(
                    base_prompt=prompt,
                    previous_json_text=llm_output_pass3,
                    failure_log_text=str(e3),
                    strict_connectivity=True,
                )
                try:
                    logger.info("Digital Spec Agent invoking final contract repair after syntax repair")
                    logger.info(f"Digital Spec Agent pass4 prompt size: {len(final_contract_repair_prompt)} chars")
                    t0 = time.monotonic()
                    llm_output_pass4 = _complete_spec_generation(final_contract_repair_prompt, agent_name, state, "contract_repair_after_syntax")
                    logger.info(f"Digital Spec Agent pass4 LLM elapsed: {time.monotonic() - t0:.2f}s")
                    logger.info(f"Digital Spec Agent pass4 LLM output size: {len(llm_output_pass4)} chars")
                    spec_json, mode, raw_output_path_pass4, normalized_path_pass4 = _compile_spec_contract(
                        llm_output=llm_output_pass4,
                        spec_dir=spec_dir,
                        suffix="_pass4",
                        requested_top=requested_top,
                        source_prompt=user_prompt,
                        require_firmware_control_plane=require_firmware_control_plane,
                    )
                    raw_output_path = raw_output_path_pass4
                except Exception as e4:
                    pass4_log_path = os.path.join(spec_dir, "spec_agent_contract_pass4.log")
                    pass4_exc_path = os.path.join(spec_dir, "spec_agent_exception_pass4.txt")
                    _write_text(pass4_log_path, f"Digital Spec Agent final contract repair failure:\n{e4}\n")
                    _write_text(pass4_exc_path, repr(e4))

                    pass5_prompt = _build_repair_prompt(
                        base_prompt=prompt,
                        previous_json_text=llm_output_pass4,
                        failure_log_text=str(e4),
                        strict_connectivity=True,
                        final_graph_closure=True,
                    )
                    try:
                        logger.info("Digital Spec Agent invoking pass5 focused contract repair")
                        logger.info(f"Digital Spec Agent pass5 prompt size: {len(pass5_prompt)} chars")
                        t0 = time.monotonic()
                        llm_output_pass5 = _complete_spec_generation(
                            pass5_prompt, agent_name, state, "contract_repair_pass5"
                        )
                        logger.info(f"Digital Spec Agent pass5 LLM elapsed: {time.monotonic() - t0:.2f}s")
                        spec_json, mode, raw_output_path_pass5, normalized_path_pass5 = _compile_spec_contract(
                            llm_output=llm_output_pass5,
                            spec_dir=spec_dir,
                            suffix="_pass5",
                            requested_top=requested_top,
                            source_prompt=user_prompt,
                            require_firmware_control_plane=require_firmware_control_plane,
                        )
                        raw_output_path = raw_output_path_pass5
                        e5 = None
                    except Exception as pass5_error:
                        e5 = pass5_error
                        pass5_log_path = os.path.join(spec_dir, "spec_agent_contract_pass5.log")
                        pass5_exc_path = os.path.join(spec_dir, "spec_agent_exception_pass5.txt")
                        _write_text(pass5_log_path, f"Digital Spec Agent pass5 contract repair failure:\n{e5}\n")
                        _write_text(pass5_exc_path, repr(e5))

                    if e5 is not None:
                        state.update({
                            "status": f"❌ JSON parse/normalize failed after pass5 contract repair: {e5}",
                            "artifact": None,
                            "artifact_list": [],
                            "artifact_log": log_path,
                            "workflow_dir": workflow_dir,
                            "workflow_id": workflow_id,
                            "issues": [
                                f"Pass1 JSON parse/normalize failed: {pass1_error}",
                                f"Pass2 JSON parse/normalize failed: {pass2_error}",
                                f"Pass3 JSON parse/normalize failed: {e3}",
                                f"Pass4 contract repair failed: {e4}",
                                f"Pass5 contract repair failed: {e5}",
                            ],
                        })

                        _upload_spec_debug_artifacts(workflow_id, agent_name, spec_dir)
                        return state

    module_name = spec_json["name"] if mode == "flat" else spec_json["hierarchy"]["top_module"]["name"]

    spec_json_path = os.path.join(spec_dir, f"{module_name}_spec.json")
    with open(spec_json_path, "w", encoding="utf-8") as sf:
        json.dump(spec_json, sf, indent=2)
    logger.info(f"🎉 Digital Spec Agent succeeded via {'pass2' if pass1_error else 'pass1'}")
    logger.info(f"📦 Digital Spec Agent spec JSON saved: {spec_json_path}")

    log_path = os.path.join(spec_dir, "spec_agent_contract.log")
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write("Digital Spec Agent completed successfully.\n")
        lf.write("Mode: contract-only\n")
        lf.write(f"Spec mode: {mode}\n")
        lf.write(f"Resolved via: {'pass2' if pass1_error else 'pass1'}\n")
        lf.write(f"Spec JSON: {spec_json_path}\n")

    summary_path = os.path.join(spec_dir, "spec_agent_summary.txt")
    _write_text(
        summary_path,
        f"✅ Digital Spec Agent completed successfully.\n\n"
        f"Spec mode: {mode}\n"
        f"Spec JSON: {spec_json_path}\n",
    )

    try:
        agent_name = "Digital Spec Agent"
        with open(raw_output_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename="llm_raw_output.txt",
                content=f.read(),
            )
        with open(spec_json_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename=os.path.basename(spec_json_path),
                content=f.read(),
            )
        with open(log_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename="spec_agent_contract.log",
                content=f.read(),
            )
    except Exception as e:
        print(f"⚠️ Spec Agent artifact upload failed: {e}")

    state.update({
        "status": "✅ Digital spec contract generated.",
        "artifact": spec_json_path,
        "artifact_list": [spec_json_path],
        "artifact_log": log_path,
        "spec_json": spec_json_path,
        "digital_spec_json": spec_json_path,
        "workflow_dir": workflow_dir,
        "workflow_id": workflow_id,
    })
    _upload_spec_debug_artifacts(workflow_id, agent_name, spec_dir)
    return state
