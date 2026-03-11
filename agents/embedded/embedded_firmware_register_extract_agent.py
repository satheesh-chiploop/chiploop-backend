import json
import os
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_outer_markdown_fences

AGENT_NAME = "Embedded Firmware Register Extract Agent"
PHASE = "register_extract"
OUTPUT_PATH = "firmware/register_map.json"


def _safe_load_json(path: str):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _find_first_existing(workflow_dir: str, candidates):
    for rel in candidates:
        p = os.path.join(workflow_dir, rel)
        if os.path.isfile(p):
            return p
    return ""


def _normalize_registers_from_any_shape(obj: dict) -> dict:
    """
    Backward-compatible generic normalizer.
    Preserves concrete upstream information.
    Does not assume protocol-specific semantics.
    """
    if not isinstance(obj, dict):
        return {
            "block_name": "firmware_block",
            "module_name": "firmware_block",
            "base_address": "0x00000000",
            "registers": [],
            "__assumptions": ["Upstream register source was missing or invalid."]
        }

    base_address = (
        obj.get("base_address")
        or obj.get("base_addr")
        or obj.get("mmio_base")
        or obj.get("base")
        or "0x00000000"
    )

    block_name = obj.get("block_name") or obj.get("module_name") or obj.get("name") or "firmware_block"
    module_name = obj.get("module_name") or obj.get("block_name") or obj.get("name") or "firmware_block"

    regs_in = obj.get("registers") or obj.get("regs") or []
    if isinstance(regs_in, dict):
        tmp = []
        for k, v in regs_in.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("name", k)
                tmp.append(item)
        regs_in = tmp

    regs_out = []
    if isinstance(regs_in, list):
        for r in regs_in:
            if not isinstance(r, dict):
                continue
            regs_out.append({
                "name": r.get("name") or r.get("reg_name") or "UNNAMED",
                "offset": r.get("offset") or r.get("addr_offset") or "0x00",
                "address": r.get("address"),
                "access": r.get("access") or r.get("sw") or r.get("mode") or "RW",
                "reset": r.get("reset") or r.get("reset_value") or "0x00000000",
                "description": r.get("description") or "",
                "fields": r.get("fields") or [],
            })

    out = {
        "block_name": block_name,
        "module_name": module_name,
        "base_address": base_address,
        "registers": regs_out,
    }

    if not regs_out:
        out["__assumptions"] = ["No concrete registers were found in the upstream source."]
    return out


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    # Artifact-first for System_Firmware / System_End2End,
    # but preserve standalone Embedded_Run by falling back to spec-only LLM flow.
    candidate_paths = [
        "digital/digital_regmap.json",
        "digital/regmap.json",
        "digital/register_map.json",
        "regmap.json",
    ]
    regmap_path = _find_first_existing(workflow_dir, candidate_paths) if workflow_dir else ""
    upstream_regmap = _safe_load_json(regmap_path)

    if isinstance(upstream_regmap, dict):
        normalized = _normalize_registers_from_any_shape(upstream_regmap)
        normalized["__source"] = {
            "mode": "artifact_first_normalization",
            "path": regmap_path,
        }
        out = json.dumps(normalized, indent=2)
        write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

        state["firmware_register_map"] = normalized
        state["firmware_register_map_path"] = OUTPUT_PATH
        embedded = state.setdefault("embedded", {})
        embedded[PHASE] = OUTPUT_PATH
        state["status"] = f"✅ {AGENT_NAME} normalized upstream register map"
        return state

    # Backward-compatible fallback for Embedded_Run and standalone flows.
    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Extract registers/CSRs from the available spec/regmap sources and produce a normalized firmware register map.

OUTPUT REQUIREMENTS:
- Output MUST be VALID JSON ONLY (no markdown fences, no prose).
- Preserve concrete register names, offsets, access types, reset values, and fields if they are present.
- Do not invent protocol-specific semantics unless clearly implied by the input.
- If information is missing, add "__assumptions": ["..."].
- Write the primary output to: firmware/register_map.json
"""

    out = llm_chat(
        prompt,
        system="You are a senior firmware engineer. Output valid JSON only. No markdown fences."
    )

    if not out:
        out = json.dumps({
            "block_name": "firmware_block",
            "module_name": "firmware_block",
            "base_address": "0x00000000",
            "registers": [],
            "__assumptions": ["LLM returned empty output; produced a safe empty normalized register map."]
        }, indent=2)

    out = strip_outer_markdown_fences(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    try:
        state["firmware_register_map"] = json.loads(out)
    except Exception:
        pass

    state["firmware_register_map_path"] = OUTPUT_PATH
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state



