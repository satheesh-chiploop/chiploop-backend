import os
import json
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
            if lsb < 0 or msb < lsb or msb >= data_width:
                violations.append(f"{reg_name}.{field_name} [{msb}:{lsb}] is outside the {data_width}-bit register word")
                continue
            for other_lsb, other_msb, other_name in occupied:
                if not (msb < other_lsb or lsb > other_msb):
                    violations.append(f"{reg_name}.{field_name} [{msb}:{lsb}] overlaps {other_name} [{other_msb}:{other_lsb}]")
            occupied.append((lsb, msb, field_name))
    return violations


def _repair_register_layout(regmap: dict, violations: list[str], spec_obj: dict, state: dict) -> dict:
    repair_prompt = f"""
You are repairing a generated SoC register-map JSON contract.
Return ONLY one raw JSON object with the same top-level schema.
Preserve every required field and its access semantics, but split fields across additional addressed registers when they do not fit.
Every field must satisfy 0 <= lsb <= msb < regmap.data_width and fields in one register must not overlap.
Do not widen the declared data bus beyond the DIGITAL_SPEC_JSON interface.
Do not remove status, control, fault, ready, or valid semantics.

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
    return repaired


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
        try:
            regmap = _repair_register_layout(regmap, violations, spec_obj, state)
        except Exception as exc:
            state["status"] = f"❌ Register map layout repair failed: {exc}"
            state["digital_regmap_layout_violations"] = violations
            raise RuntimeError(state["status"]) from exc
        repaired_violations = _register_layout_violations(regmap)
        if repaired_violations:
            state["status"] = "❌ Register map layout remains invalid after repair."
            state["digital_regmap_layout_violations"] = repaired_violations
            raise RuntimeError(
                f"{state['status']} Violations: {'; '.join(repaired_violations[:8])}"
            )
        state["digital_regmap_layout_repaired"] = True

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
