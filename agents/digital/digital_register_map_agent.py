import os
import json
from copy import deepcopy
from model_gateway import complete_text
from utils.artifact_utils import save_text_artifact_and_record

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")


def _read_json_if_exists(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
        with open(v, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _safe_dump(obj):
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return "null"


def _detect_spec_mode(spec_obj: dict) -> str:
    if not isinstance(spec_obj, dict):
        return "unknown"
    if isinstance(spec_obj.get("hierarchy"), dict):
        return "hierarchical"
    if spec_obj.get("name") and spec_obj.get("rtl_output_file"):
        return "flat"
    return "unknown"


def _spec_requires_register_map(spec_obj: dict) -> bool:
    """Return true only when the authoritative interface exposes software-visible control."""
    contract = spec_obj.get("register_contract") if isinstance(spec_obj, dict) else None
    if isinstance(contract, list) and contract:
        return True
    if isinstance(contract, dict) and (contract.get("registers") or contract.get("bus") or contract.get("bus_type")):
        return True
    if not isinstance(spec_obj, dict):
        return False
    ports = []
    if isinstance(spec_obj.get("hierarchy"), dict):
        ports = ((spec_obj["hierarchy"].get("top_module") or {}).get("ports") or [])
    else:
        ports = spec_obj.get("ports") or []
    names = {str(port.get("name") or "").lower() for port in ports if isinstance(port, dict)}

    def prefixed_signal(prefixes: tuple[str, ...], suffixes: tuple[str, ...]) -> bool:
        return any(
            name.startswith(prefix) and any(name == f"{prefix}{suffix}" or name.endswith(suffix) for suffix in suffixes)
            for name in names
            for prefix in prefixes
        )

    # Classify the interface contract; do not infer register contents here.
    # Common CSR buses use *_wen/*_ren while others use *_we/*_valid. Both are
    # real transaction controls and must route through model-generated regmap
    # creation instead of the no-register-map bypass.
    address = (
        any(name in names for name in {"apb_paddr", "axi_awaddr", "i2c_addr"})
        or prefixed_signal(("cfg_", "reg_", "csr_"), ("addr", "address"))
    )
    transaction = (
        any(name in names for name in {"apb_psel", "axi_awvalid", "i2c_scl"})
        or prefixed_signal(
            ("cfg_", "reg_", "csr_"),
            ("we", "wen", "write", "write_en", "write_enable", "valid", "ren", "read_en", "read_enable"),
        )
    )
    data = (
        any(name in names for name in {"apb_pwdata", "axi_wdata", "i2c_sda"})
        or prefixed_signal(("cfg_", "reg_", "csr_"), ("wdata", "write_data"))
    )
    return address and transaction and data


def _no_register_map_document(spec_mode: str) -> dict:
    return {
        "derived_from_spec_only": True,
        "spec_mode": spec_mode,
        "register_map_required": False,
        "regmap": {
            "status": "not_applicable",
            "bus": "none",
            "base_address": None,
            "addr_width": 0,
            "data_width": 0,
            "registers": [],
        },
        "interrupts": {"sources": []},
        "software_driver_intent": {"init_sequence": [], "polling_sequence": [], "irq_sequence": []},
        "consistency_notes": ["The authoritative design contract exposes no software-visible register interface."],
    }


def _parse_int(value, default=0):
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return default


def _register_layout_violations(document: dict) -> list[str]:
    """Validate the software-visible layout without changing its semantics."""
    regmap = document.get("regmap") if isinstance(document, dict) else None
    if not isinstance(regmap, dict):
        return ["regmap must be a JSON object"]
    if document.get("register_map_required") is False and regmap.get("status") == "not_applicable":
        return []
    data_width = _parse_int(regmap.get("data_width"), 0)
    if data_width not in {8, 16, 32, 64}:
        return [f"regmap.data_width={data_width!r} must be one of 8, 16, 32, or 64"]
    violations: list[str] = []
    registers = regmap.get("registers")
    if not isinstance(registers, list) or not registers:
        return ["regmap.registers must contain at least one register"]
    for reg_index, register in enumerate(registers):
        if not isinstance(register, dict):
            violations.append(f"registers[{reg_index}] must be an object")
            continue
        reg_name = str(register.get("name") or f"registers[{reg_index}]")
        occupied: list[tuple[int, int, str]] = []
        for field_index, field in enumerate(register.get("fields") or []):
            if not isinstance(field, dict):
                violations.append(f"{reg_name}.fields[{field_index}] must be an object")
                continue
            field_name = str(field.get("name") or f"fields[{field_index}]")
            lsb = _parse_int(field.get("lsb", field.get("bit_offset")), -1)
            if field.get("msb") is not None:
                msb = _parse_int(field.get("msb"), -1)
            else:
                width = _parse_int(field.get("bit_width", field.get("width")), 1)
                msb = lsb + width - 1
            if msb < lsb:
                violations.append(
                    f"{reg_name}.{field_name} has reversed bit ordering: lsb={lsb}, msb={msb}; "
                    "require 0 <= lsb <= msb"
                )
                continue
            if lsb < 0 or msb >= data_width:
                violations.append(f"{reg_name}.{field_name} [{msb}:{lsb}] is outside the {data_width}-bit register word")
                continue
            for other_lsb, other_msb, other_name in occupied:
                if not (msb < other_lsb or lsb > other_msb):
                    violations.append(f"{reg_name}.{field_name} [{msb}:{lsb}] overlaps {other_name} [{other_msb}:{other_lsb}]")
            occupied.append((lsb, msb, field_name))
    return violations


def _repair_overlapping_fields_deterministically(document: dict) -> tuple[dict, bool]:
    """Relocate only overlapping fields into free bits without changing semantics.

    Byte-aligned locations are preferred for byte-sized fields. Out-of-range
    fields are deliberately left to the stricter LLM split-register repair,
    because silently moving those can change an explicitly addressed contract.
    """
    repaired = deepcopy(document)
    regmap = repaired.get("regmap") if isinstance(repaired, dict) else None
    if not isinstance(regmap, dict):
        return repaired, False
    data_width = _parse_int(regmap.get("data_width"), 0)
    if data_width not in {8, 16, 32, 64}:
        return repaired, False
    changed = False
    for register in regmap.get("registers") or []:
        if not isinstance(register, dict):
            continue
        occupied: set[int] = set()
        for field in register.get("fields") or []:
            if not isinstance(field, dict):
                continue
            lsb = _parse_int(field.get("lsb", field.get("bit_offset")), -1)
            if field.get("msb") is not None:
                msb = _parse_int(field.get("msb"), -1)
            else:
                width_value = _parse_int(field.get("bit_width", field.get("width")), 1)
                msb = lsb + width_value - 1
            width = msb - lsb + 1
            if lsb < 0 or width <= 0 or msb >= data_width:
                continue
            requested = set(range(lsb, msb + 1))
            if requested.isdisjoint(occupied):
                occupied.update(requested)
                continue
            aligned = list(range(0, data_width - width + 1, 8)) if width >= 8 else []
            candidates = [*aligned, *range(0, data_width - width + 1)]
            new_lsb = next(
                (
                    candidate
                    for candidate in dict.fromkeys(candidates)
                    if set(range(candidate, candidate + width)).isdisjoint(occupied)
                ),
                None,
            )
            if new_lsb is None:
                continue
            field["lsb"] = new_lsb
            field["msb"] = new_lsb + width - 1
            occupied.update(range(new_lsb, new_lsb + width))
            changed = True
    return repaired, changed


def _repair_register_layout(
    regmap: dict,
    violations: list[str],
    spec_obj: dict,
    state: dict,
    *,
    repair_pass: int,
) -> tuple[dict, str]:
    repair_prompt = f"""
You are repairing a generated SoC register-map JSON contract.
Return ONLY one raw JSON object with the same top-level schema.
Preserve every required field and its access semantics, but split fields across additional addressed registers when they do not fit.
Every field must satisfy 0 <= lsb <= msb < regmap.data_width and fields in one register must not overlap.
Do not widen the declared data bus beyond the DIGITAL_SPEC_JSON interface.
Do not remove status, control, fault, ready, or valid semantics.

BIT-RANGE EXAMPLES FOR A 64-BIT REGISTER
- GOOD 16-bit low field: {{"lsb": 0, "msb": 15}}. It is conventionally displayed as [15:0].
- GOOD 16-bit next field: {{"lsb": 16, "msb": 31}}. It is displayed as [31:16].
- GOOD 32-bit upper field: {{"lsb": 32, "msb": 63}}. It is displayed as [63:32].
- BAD: {{"lsb": 15, "msb": 0}}, {{"lsb": 31, "msb": 16}}, or {{"lsb": 63, "msb": 32}}. These reverse lsb/msb.
- BAD: infer ordering from the textual [msb:lsb] diagnostic. JSON always stores the smaller bit index in lsb and the larger bit index in msb.
- Repair the complete map coherently and return every register, field, address, access type, and reset value.

REPAIR PASS: {repair_pass} of 4

VALIDATION ERRORS:
{_safe_dump(violations)}

DIGITAL_SPEC_JSON:
{_safe_dump(spec_obj)}

INVALID_REGISTER_MAP_JSON:
{_safe_dump(regmap)}
""".strip()
    repaired_text = complete_text(repair_prompt, capability="spec_generation", agent_name="Digital Register Map Agent", state=state)
    repaired = json.loads(repaired_text.strip())
    if not isinstance(repaired, dict):
        raise ValueError("repair did not return a JSON object")
    return repaired, repaired_text


def run_agent(state: dict) -> dict:
    print("\n🗺️ Running Digital Register Map Agent...")

    agent_name = "Digital Register Map Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = (state.get("spec", "") or "").strip()

    spec_obj = (
        _read_json_if_exists(state.get("digital_spec_json"))
        or _read_json_if_exists(state.get("spec_json"))
    )
    arch_obj = _read_json_if_exists(state.get("digital_architecture_json"))
    micro_obj = _read_json_if_exists(state.get("digital_microarchitecture_json"))

    if not spec_obj:
        state["status"] = "❌ Missing digital spec JSON for register map generation."
        return state

    spec_mode = _detect_spec_mode(spec_obj)

    if not _spec_requires_register_map(spec_obj):
        regmap = _no_register_map_document(spec_mode)
        raw_path = os.path.join(workflow_dir, "digital_regmap_raw_output.txt")
        out_path = os.path.join(workflow_dir, "digital_regmap.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(regmap, indent=2))
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(regmap, f, indent=2)
        try:
            for filename, path in (("digital_regmap_raw_output.txt", raw_path), ("digital_regmap.json", out_path)):
                with open(path, "r", encoding="utf-8") as f:
                    save_text_artifact_and_record(
                        workflow_id=workflow_id, agent_name=agent_name, subdir="digital",
                        filename=filename, content=f.read(),
                    )
        except Exception as e:
            print(f"Failed to upload no-register-map artifacts: {e}")
        rel_regmap_path = "digital/digital_regmap.json"
        digital = state.setdefault("digital", {})
        digital.update({
            "regmap": regmap, "digital_regmap": regmap,
            "digital_regmap_path": rel_regmap_path, "register_map_path": rel_regmap_path,
        })
        state.update({
            "status": "Digital register map not required by the design contract.",
            "digital_regmap": regmap, "digital_regmap_path": rel_regmap_path,
            "digital_register_map_path": rel_regmap_path, "digital_regmap_json": out_path,
            "register_map_required": False, "workflow_id": workflow_id, "workflow_dir": workflow_dir,
        })
        return state

    prompt = f"""
You are a senior SoC register architect.

DIGITAL_SPEC_JSON is the primary source of truth.
ARCHITECTURE_JSON and MICROARCH_JSON are descriptive only.
If they conflict with DIGITAL_SPEC_JSON, DIGITAL_SPEC_JSON wins.

SPEC MODE:
{spec_mode}

INPUTS
USER_REQUEST:
{user_prompt}

DIGITAL_SPEC_JSON:
{_safe_dump(spec_obj)}

ARCHITECTURE_JSON:
{_safe_dump(arch_obj)}

MICROARCH_JSON:
{_safe_dump(micro_obj)}

OUTPUT RULES
- Output ONLY one raw JSON object.
- No markdown.
- No prose.
- No comments.

TASK
Generate a firmware-visible register map only if it is compatible with DIGITAL_SPEC_JSON.
Do NOT invent new hierarchy.
Do NOT invent incompatible top/module ports.
Do NOT force AXI/APB if not implied by the spec.
If the spec clearly implies an I2C/custom byte-register interface, prefer a custom 8-bit register bus description.
If a value wider than the data bus must be exposed, split it across multiple byte registers.
Define field-level semantics explicitly.

FIELD BIT ORDERING
- JSON field ranges always use lsb <= msb.
- For a 64-bit word, bits [15:0] are {{"lsb": 0, "msb": 15}}, bits [31:16] are {{"lsb": 16, "msb": 31}}, and bits [63:32] are {{"lsb": 32, "msb": 63}}.
- Never reverse these values. {{"lsb": 15, "msb": 0}} is invalid.

CONTROL-PLANE EXAMPLES
- GOOD: ports csr_addr + csr_wdata + csr_wen/csr_ren + csr_rdata describe a real custom CSR bus; generate concrete addressed registers whose access and fields follow DIGITAL_SPEC_JSON.
- GOOD: ports cfg_addr + cfg_wdata + cfg_we/cfg_valid describe a real configuration bus; preserve its exact width and handshake instead of renaming it to APB or AXI.
- BAD: return bus "none", status "not_applicable", or an empty registers list when address, write-data, and transaction-control ports are present.
- BAD: invent register meanings that are absent from DIGITAL_SPEC_JSON, USER_REQUEST, ARCHITECTURE_JSON, and MICROARCH_JSON.
- The generated RTL consumes this register map. Every generated register and field must therefore be implementable through the declared top-level control interface.

OUTPUT SCHEMA
{{
  "derived_from_spec_only": true,
  "spec_mode": "{spec_mode}",
  "regmap": {{
    "bus": "custom|i2c|abstract|minimal|axi_lite|apb",
    "base_address": "0x00",
    "addr_width": 8,
    "data_width": 8,
    "registers": [
      {{
        "name": "CONTROL",
        "offset": "0x01",
        "access": "RW",
        "description": "Control register",
        "fields": [
          {{"name": "ENABLE", "lsb": 0, "msb": 0, "access": "RW", "reset": 0, "description": "..."}}
        ]
      }}
    ]
  }},
  "interrupts": {{
    "sources": [
      {{"name": "ADC_DONE_IRQ", "description": "..."}},
      {{"name": "FAULT_IRQ", "description": "..."}}
    ]
  }},
  "software_driver_intent": {{
    "init_sequence": [],
    "polling_sequence": [],
    "irq_sequence": []
  }},
  "consistency_notes": [
    "All assumptions remain compatible with DIGITAL_SPEC_JSON."
  ]
}}
""".strip()

    try:
        llm_output = complete_text(prompt, capability="spec_generation", agent_name="Digital Register Map Agent", state=state)
    except Exception as e:
        state["status"] = f"❌ Register map LLM generation failed: {e}"
        return state

    raw_path = os.path.join(workflow_dir, "digital_regmap_raw_output.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    try:
        regmap = json.loads(llm_output.strip())
    except Exception as e:
        regmap = {
            "error": "LLM JSON parse failed",
            "parse_error": str(e),
            "raw": llm_output.strip()
        }

    violations = _register_layout_violations(regmap)
    if violations:
        deterministic_regmap, deterministic_changed = _repair_overlapping_fields_deterministically(regmap)
        deterministic_violations = _register_layout_violations(deterministic_regmap)
        if deterministic_changed and not deterministic_violations:
            regmap = deterministic_regmap
            violations = []
            state["digital_regmap_layout_repaired"] = True
            state["digital_regmap_layout_repair_method"] = "deterministic_free_bit_placement"
    repair_paths = []
    if violations:
        # Pass 1 is initial generation. Passes 2-4 are complete model repairs
        # followed by strict validation; never silently swap lsb/msb.
        for repair_pass in range(2, 5):
            try:
                regmap, repair_text = _repair_register_layout(
                    regmap,
                    violations,
                    spec_obj,
                    state,
                    repair_pass=repair_pass,
                )
            except Exception as exc:
                state["status"] = f"❌ Register map layout repair pass {repair_pass} failed: {exc}"
                state["digital_regmap_layout_violations"] = violations
                raise RuntimeError(state["status"]) from exc
            repair_path = os.path.join(workflow_dir, f"digital_regmap_repair_pass{repair_pass}.txt")
            with open(repair_path, "w", encoding="utf-8") as f:
                f.write(repair_text)
            repair_paths.append((repair_pass, repair_path))
            violations = _register_layout_violations(regmap)
            if not violations:
                state["digital_regmap_layout_repaired"] = True
                state["digital_regmap_layout_repair_method"] = f"llm_contract_repair_pass{repair_pass}"
                break
        if violations:
            state["status"] = "❌ Register map layout remains invalid after Pass 4."
            state["digital_regmap_layout_violations"] = violations
            raise RuntimeError(
                f"{state['status']} Violations: {'; '.join(violations[:8])}"
            )

    out_path = os.path.join(workflow_dir, "digital_regmap.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(regmap, f, indent=2)

    try:
        with open(raw_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_regmap_raw_output.txt",
                content=f.read(),
            )
        with open(out_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_regmap.json",
                content=f.read(),
            )
        for repair_pass, repair_path in repair_paths:
            with open(repair_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir="digital",
                    filename=f"digital_regmap_repair_pass{repair_pass}.txt",
                    content=f.read(),
                )
    except Exception as e:
        print(f"⚠️ Failed to upload regmap artifacts: {e}")

    rel_regmap_path = "digital/digital_regmap.json"

    digital = state.setdefault("digital", {})
    digital["regmap"] = regmap
    digital["digital_regmap"] = regmap
    digital["digital_regmap_path"] = rel_regmap_path
    digital["register_map_path"] = rel_regmap_path

    state.update({
        "status": "✅ Digital register map generated.",
        "digital_regmap": regmap,
        "digital_regmap_path": rel_regmap_path,
        "digital_register_map_path": rel_regmap_path,
        "digital_regmap_json": out_path,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })
    return state 
