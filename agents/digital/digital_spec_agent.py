import os
import json
from utils.artifact_utils import save_text_artifact_and_record
from portkey_ai import Portkey
import logging


PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")


logger = logging.getLogger("chiploop")




def _normalize_spec_json(spec_json: dict):
    if not isinstance(spec_json, dict):
        raise ValueError("Spec JSON must be a dictionary.")

    # Hierarchical form
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

        norm = {
            "design_name": spec_json.get("design_name") or top["name"],
            "design_summary": spec_json.get("design_summary", ""),
            "operating_constraints": spec_json.get("operating_constraints", {}),
            "hierarchy": {
                "top_module": top,
                "modules": modules,
            },
            "top_level_connections": spec_json.get("top_level_connections", []),
            "inter_module_signals": spec_json.get("inter_module_signals", []),
            "signal_ownership": spec_json.get("signal_ownership", []),
            "register_contract": spec_json.get("register_contract", {}),
        }
        return norm, "hierarchical"

    # Flat form
    if spec_json.get("name") and spec_json.get("rtl_output_file"):
        norm = {
            "name": spec_json["name"],
            "description": spec_json.get("description", ""),
            "operating_constraints": spec_json.get("operating_constraints", {}),
            "ports": spec_json.get("ports", []),
            "functionality": spec_json.get("functionality", ""),
            "responsibilities": spec_json.get("responsibilities", []),
            "must_drive": spec_json.get("must_drive", []),
            "must_receive": spec_json.get("must_receive", []),
            "must_not_drive": spec_json.get("must_not_drive", []),
            "reset_behavior": spec_json.get("reset_behavior", ""),
            "behavior_rules": spec_json.get("behavior_rules", []),
            "rtl_output_file": spec_json["rtl_output_file"],
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


def _validate_hierarchical_endpoint_coverage(spec_json: dict) -> None:
    module_ports = _collect_module_port_names(spec_json)
    top_name = spec_json["hierarchy"]["top_module"]["name"]
    top_ports = module_ports[top_name]

    for i, c in enumerate(spec_json.get("top_level_connections", [])):
        tp = c["top_port"]
        if tp not in top_ports:
            raise ValueError(f"top_level_connections[{i}].top_port '{tp}' is not present in top module ports.")
        for dst in c.get("connected_to", []):
            if "." not in dst:
                raise ValueError(f"top_level_connections[{i}] target '{dst}' is invalid. Expected module.port")
            mod, port = dst.split(".", 1)
            if mod not in module_ports:
                raise ValueError(f"top_level_connections[{i}] target module '{mod}' does not exist.")
            if port not in module_ports[mod]:
                raise ValueError(f"top_level_connections[{i}] target port '{mod}.{port}' is not present in module ports.")

    for i, s in enumerate(spec_json.get("inter_module_signals", [])):
        src = s["source"]
        if "." not in src:
            raise ValueError(f"inter_module_signals[{i}].source '{src}' is invalid. Expected module.port")
        smod, sport = src.split(".", 1)
        if smod not in module_ports:
            raise ValueError(f"inter_module_signals[{i}] source module '{smod}' does not exist.")
        if sport not in module_ports[smod]:
            raise ValueError(f"inter_module_signals[{i}] source port '{smod}.{sport}' is not present in module ports.")

        for dst in s.get("destinations", []):
            if "." not in dst:
                raise ValueError(f"inter_module_signals[{i}] destination '{dst}' is invalid. Expected module.port")
            dmod, dport = dst.split(".", 1)
            if dmod not in module_ports:
                raise ValueError(f"inter_module_signals[{i}] destination module '{dmod}' does not exist.")
            if dport not in module_ports[dmod]:
                raise ValueError(f"inter_module_signals[{i}] destination port '{dmod}.{dport}' is not present in module ports.")

    for i, o in enumerate(spec_json.get("signal_ownership", [])):
        owner = o["owner"]
        if "." not in owner:
            raise ValueError(f"signal_ownership[{i}].owner '{owner}' is invalid. Expected module.port")
        omod, oport = owner.split(".", 1)
        if omod not in module_ports:
            raise ValueError(f"signal_ownership[{i}] owner module '{omod}' does not exist.")
        if oport not in module_ports[omod]:
            raise ValueError(f"signal_ownership[{i}] owner port '{omod}.{oport}' is not present in module ports.")


def _validate_spec_contract(spec_json: dict, mode: str) -> None:
    if mode == "flat":
        _validate_module(spec_json, "spec", require_non_empty_ports=False)
        return

    hier = spec_json["hierarchy"]
    top = hier["top_module"]
    modules = hier.get("modules", [])

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
    if not isinstance(ims, list) or not ims:
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
        "spec_agent_normalized.json",
        "spec_agent_normalized_pass2.json",
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

    for own in spec_json.get("signal_ownership", []):
        owner = own.get("owner", "")
        if "." in owner:
            referenced.append(owner)

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

def _build_repair_prompt(base_prompt: str, previous_json_text: str, failure_log_text: str) -> str:
    return f"""
{base_prompt}

==============================
REPAIR MODE (SECOND PASS)
==============================

Your previous JSON did not pass contract validation.

You MUST preserve the same architecture unless a structural change is strictly required to fix the validation errors.

PREVIOUS JSON:
{previous_json_text}

VALIDATION FAILURE LOG:
{failure_log_text}

REPAIR RULES:
- Do NOT redesign the architecture unless required to resolve the errors
- Preserve module names, hierarchy, ports, and intent as much as possible
- Fix only structural inconsistencies needed for contract closure
- Return ONE full corrected JSON object only
- Do NOT return partial edits
- Do NOT return explanations
""".strip()


def _compile_spec_contract(llm_output: str, spec_dir: str, suffix: str = ""):
    logger.info(f"🔍 Digital Spec Agent compile start suffix='{suffix or 'pass1'}'")
    raw_name = f"llm_raw_output{suffix}.txt"
    raw_output_path = os.path.join(spec_dir, raw_name)
    with open(raw_output_path, "w", encoding="utf-8") as rf:
        rf.write(llm_output)

    parsed_json = json.loads(llm_output.strip())
    logger.info(f"🔍 Digital Spec Agent JSON parsed suffix='{suffix or 'pass1'}'")
    spec_json, mode = _normalize_spec_json(parsed_json)
    logger.info(f"🔍 Digital Spec Agent normalized mode={mode} suffix='{suffix or 'pass1'}'")
    if mode == "hierarchical":
        spec_json = _ensure_hierarchical_port_closure(spec_json)
        logger.info(f"🔍 Digital Spec Agent hierarchical port closure done suffix='{suffix or 'pass1'}'")

   
    normalized_name = "spec_agent_normalized.json" if not suffix else f"spec_agent_normalized{suffix}.json"
    normalized_path = os.path.join(spec_dir, normalized_name)
    with open(normalized_path, "w", encoding="utf-8") as nf:
        json.dump(spec_json, nf, indent=2)

    _validate_spec_contract(spec_json, mode)
    logger.info(f"✅ Digital Spec Agent contract compile passed suffix='{suffix or 'pass1'}'")
    return spec_json, mode, raw_output_path, normalized_path


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

    client_portkey = Portkey(api_key=PORTKEY_API_KEY)


    entry_path = os.path.join(spec_dir, "spec_agent_entry.json")
    entry_payload = {
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
        "spec_dir": spec_dir,
        "state_keys": sorted(list(state.keys())),
        "input_candidates": {
            "spec": state.get("spec"),
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
        or state.get("digital_spec")
        or state.get("digital_spec_text")
        or state.get("soc_spec")
        or state.get("system_spec")
        or state.get("description")
        or ""
    ).strip()

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

If the user spec is incomplete, choose the simplest valid architecture ONCE and encode it here.
This JSON becomes the source of truth for downstream agents.

Return JSON only.
""".strip()

    try:
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
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

    try:
        spec_json, mode, raw_output_path, normalized_path = _compile_spec_contract(
            llm_output=llm_output,
            spec_dir=spec_dir,
            suffix="",
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
            completion_pass2 = client_portkey.chat.completions.create(
                model="@chiploop/gpt-4o-mini",
                messages=[{"role": "user", "content": repair_prompt}],
                stream=False,
            )
            llm_output_pass2 = completion_pass2.choices[0].message.content or ""
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
            )
            raw_output_path = raw_output_path_pass2
            
        except Exception as e2:
            pass2_error = e2
            pass2_log_path = os.path.join(spec_dir, "spec_agent_contract_pass2.log")
            pass2_exc_path = os.path.join(spec_dir, "spec_agent_exception_pass2.txt")

            _write_text(pass2_log_path, f"Digital Spec Agent parse/normalize failure:\n{e2}\n")
            _write_text(pass2_exc_path, repr(e2))

            state.update({
                "status": f"❌ JSON parse/normalize failed after pass2: {e2}",
                "artifact": None,
                "artifact_list": [],
                "artifact_log": log_path,
                "workflow_dir": workflow_dir,
                "workflow_id": workflow_id,
                "issues": [
                    f"Pass1 JSON parse/normalize failed: {pass1_error}",
                    f"Pass2 JSON parse/normalize failed: {pass2_error}",
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