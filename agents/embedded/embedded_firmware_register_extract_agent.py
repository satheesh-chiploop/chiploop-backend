
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ._embedded_common import (
    ensure_workflow_dir,
    llm_chat,
    strip_outer_markdown_fences,
    write_artifact,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Firmware Register Extract Agent"
PHASE = "register_extract"
OUTPUT_PATH = "firmware/register_map.json"
DEBUG_PATH = "firmware/register_map_debug.json"
SUMMARY_PATH = "firmware/register_map_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed to load JSON from %s: %s", AGENT_NAME, path, exc)
    return None


def _find_first_existing(workflow_dir: str, candidates: List[str]) -> str:
    for rel in candidates:
        p = os.path.join(workflow_dir, rel)
        if os.path.isfile(p):
            return p
    return ""


def _parse_intish(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            s = value.strip().lower()
            if not s:
                return default
            if s.startswith("0x"):
                return int(s, 16)
            return int(s)
    except Exception:
        pass
    return default


def _fmt_hex32(value: Any) -> str:
    return f"0x{_parse_intish(value, 0):08X}"


def _normalize_access(value: Any) -> str:
    s = str(value or "RW").strip().upper()
    allowed = {"RW", "RO", "WO", "W1C", "RW1C"}
    return s if s in allowed else "RW"


def _first_nonempty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalize_fields(fields: Any) -> List[dict]:
    if isinstance(fields, dict):
        tmp = []
        for fname, fbody in fields.items():
            if isinstance(fbody, dict):
                item = dict(fbody)
                item.setdefault("name", fname)
                tmp.append(item)
        fields = tmp

    out: List[dict] = []
    if not isinstance(fields, list):
        return out

    for field in fields:
        if not isinstance(field, dict):
            continue

        bit_offset = _parse_intish(field.get("bit_offset", field.get("lsb", 0)), 0)

        if field.get("bit_width") is not None:
            bit_width = max(1, _parse_intish(field.get("bit_width"), 1))
        elif field.get("width") is not None:
            bit_width = max(1, _parse_intish(field.get("width"), 1))
        elif field.get("msb") is not None:
            msb = _parse_intish(field.get("msb"), bit_offset)
            bit_width = max(1, msb - bit_offset + 1)
        else:
            bit_width = 1

        out.append(
            {
                "name": field.get("name") or "UNNAMED_FIELD",
                "bit_offset": bit_offset,
                "bit_width": bit_width,
                "access": _normalize_access(field.get("access") or field.get("sw") or "RW"),
                "description": field.get("description") or field.get("desc") or "",
            }
        )
    return out


def _sort_registers(registers: List[dict]) -> List[dict]:
    return sorted(
        registers,
        key=lambda reg: (
            _parse_intish(
                (reg or {}).get("offset", (reg or {}).get("addr_offset", (reg or {}).get("address", 0)))
            ),
            (reg or {}).get("name", ""),
        ),
    )


def _extract_register_candidates(source_obj: dict) -> List[dict]:
    candidate_regs = None

    if isinstance(source_obj.get("registers"), list):
        candidate_regs = source_obj["registers"]
    elif isinstance(source_obj.get("regs"), list):
        candidate_regs = source_obj["regs"]
    elif isinstance(source_obj.get("csr_registers"), list):
        candidate_regs = source_obj["csr_registers"]
    elif isinstance(source_obj.get("register_map"), dict):
        tmp = []
        for reg_name, reg_body in source_obj["register_map"].items():
            if isinstance(reg_body, dict):
                item = dict(reg_body)
                item.setdefault("name", reg_name)
                tmp.append(item)
        candidate_regs = tmp
    else:
        tmp = []
        for reg_name, reg_body in source_obj.items():
            if isinstance(reg_body, dict) and (
                "offset" in reg_body
                or "addr_offset" in reg_body
                or "fields" in reg_body
                or "bitfields" in reg_body
                or "reset" in reg_body
                or "reset_value" in reg_body
            ):
                item = dict(reg_body)
                item.setdefault("name", reg_name)
                tmp.append(item)
        candidate_regs = tmp

    out: List[dict] = []
    if isinstance(candidate_regs, list):
        for reg in candidate_regs:
            if not isinstance(reg, dict):
                continue
            if not reg.get("name"):
                continue
            if "offset" not in reg and "addr_offset" not in reg and "address" not in reg:
                continue
            out.append(reg)
    return out


def _normalize_register(reg: dict) -> dict:
    fields = reg.get("fields") or reg.get("bitfields") or []
    return {
        "name": reg.get("name") or reg.get("reg_name") or "UNNAMED",
        "offset": _fmt_hex32(reg.get("offset", reg.get("addr_offset", 0))),
        "address": reg.get("address"),
        "access": _normalize_access(reg.get("access") or reg.get("sw") or reg.get("mode") or "RW"),
        "reset": reg.get("reset") or reg.get("reset_value") or "0x00000000",
        "description": reg.get("description") or reg.get("desc") or "",
        "fields": _normalize_fields(fields),
    }


def _normalize_registers_from_any_shape(obj: dict) -> dict:
    """
    Backward-compatible generic normalizer.
    Preserves concrete upstream information and avoids protocol invention.
    """
    if not isinstance(obj, dict):
        return {
            "block_name": "digital_subsystem",
            "module_name": "digital_subsystem",
            "base_address": "0x00000000",
            "registers": [],
            "__assumptions": ["Upstream register source was missing or invalid."],
        }

    if "regmap" in obj and isinstance(obj["regmap"], dict):
        obj = obj["regmap"]
    elif "register_map" in obj and isinstance(obj["register_map"], dict) and "registers" in obj["register_map"]:
        obj = obj["register_map"]

    base_address = (
        obj.get("base_address")
        or obj.get("base_addr")
        or obj.get("mmio_base")
        or obj.get("base")
        or "0x00000000"
    )
    block_name = obj.get("block_name") or obj.get("module_name") or obj.get("name") or "digital_subsystem"
    module_name = obj.get("module_name") or obj.get("block_name") or obj.get("name") or "digital_subsystem"

    regs_in = _extract_register_candidates(obj)
    regs_out = _sort_registers([_normalize_register(reg) for reg in regs_in])

    out = {
        "block_name": block_name,
        "module_name": module_name,
        "base_address": _fmt_hex32(base_address),
        "registers": regs_out,
    }
    if not regs_out:
        out["__assumptions"] = ["No concrete registers were found in the upstream source."]
    return out


def _build_summary(regmap: dict, mode: str, regmap_path: str) -> dict:
    registers = regmap.get("registers") or []
    return {
        "agent": AGENT_NAME,
        "phase": PHASE,
        "mode": mode,
        "source_path": regmap_path,
        "block_name": regmap.get("block_name"),
        "module_name": regmap.get("module_name"),
        "base_address": regmap.get("base_address"),
        "register_count": len(registers),
        "register_names": [reg.get("name") for reg in registers if isinstance(reg, dict)],
    }


def _infer_interrupt_sources(state: dict, regmap: dict) -> List[str]:
    integration = state.get("system_integration_intent") or (state.get("system") or {}).get("integration_intent") or {}
    digital = state.get("digital") or {}

    # 1) Prefer explicit upstream metadata when present
    explicit = (
        integration.get("interrupt_sources")
        or digital.get("interrupt_sources")
        or regmap.get("interrupt_sources")
        or regmap.get("interrupts")
        or state.get("interrupt_sources")
        or []
    )
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit if x]

    sources: List[str] = []

    # 2) Generic inference from register/field metadata
    interrupt_keywords = {
        "IRQ", "INT", "INTR", "INTERRUPT",
        "DONE", "READY", "FAULT", "ERROR", "ERR",
        "PENDING"
    }

    for reg in regmap.get("registers") or []:
        reg_name = str(reg.get("name") or "")
        reg_upper = reg_name.upper()

        if any(keyword in reg_upper for keyword in interrupt_keywords):
            sources.append(reg_name)

        for field in reg.get("fields") or []:
            field_name = str(field.get("name") or "")
            field_upper = field_name.upper()
            if any(keyword in field_upper for keyword in interrupt_keywords):
                sources.append(field_name)

    ordered: List[str] = []
    for src in sources:
        if src and src not in ordered:
            ordered.append(src)
    return ordered

def _infer_block_name(state: dict, source_obj: dict, regmap: dict) -> str:
    integration = state.get("system_integration_intent") or (state.get("system") or {}).get("integration_intent") or {}
    digital = state.get("digital") or {}

    return (
        _first_nonempty(
            regmap.get("module_name"),
            regmap.get("block_name"),
            source_obj.get("module_name"),
            source_obj.get("block_name"),
            digital.get("module_name"),
            digital.get("block_name"),
            integration.get("digital_block_name"),
            integration.get("digital_block"),
            state.get("digital_block_name"),
        )
        or "digital_subsystem"
    )


def _infer_top_module(state: dict) -> str:
    integration = state.get("system_integration_intent") or (state.get("system") or {}).get("integration_intent") or {}
    return (
        _first_nonempty(
            state.get("system_top_module"),
            state.get("top_module"),
            integration.get("top_module"),
            integration.get("soc_top_module"),
        )
        or "soc_top_sim"
    )


def _infer_bus_type_from_sources(state: dict, source_obj: dict, regmap: dict, spec_text: str, goal: str) -> str:
    integration = state.get("system_integration_intent") or (state.get("system") or {}).get("integration_intent") or {}
    digital = state.get("digital") or {}

    explicit = _first_nonempty(
        regmap.get("bus"),
        source_obj.get("bus"),
        digital.get("bus"),
        integration.get("bus_type"),
        integration.get("bus"),
        state.get("bus_type"),
    )
    if explicit:
        return str(explicit).upper()

    joined = "\n".join([spec_text or "", goal or "", json.dumps(regmap)[:4000]]).upper()
    if "APB" in joined:
        return "APB"
    if "AHB" in joined:
        return "AHB"
    if "AXI" in joined:
        return "AXI"
    return "MMIO"


def _build_manifest(state: dict, regmap: dict, source_obj: Optional[dict] = None, regmap_path: str = "") -> dict:
    source_obj = source_obj or {}
    firmware = state.get("firmware") or {}
    build_root = firmware.get("build_root") or "firmware/build"
    src_root = firmware.get("src_root") or "firmware/src"

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()

    manifest = {
        "schema_version": "1.0",
        "flow": "system_firmware",
        "top_module": _infer_top_module(state),
        "digital_block_name": _infer_block_name(state, source_obj, regmap),
        "analog_block_name": state.get("analog_block_name") or "analog_subsystem",
        "bus_type": _infer_bus_type_from_sources(state, source_obj, regmap, spec_text, goal),
        "register_map_path": OUTPUT_PATH,
        "register_map_source_path": regmap_path,
        "hal_path": firmware.get("hal_path") or "",
        "driver_path": firmware.get("driver_scaffold_path") or firmware.get("driver_path") or "",
        "base_address": regmap.get("base_address") or "0x00000000",
        "interrupt_model": {
            "top_irq_signal": state.get("top_irq_signal") or "irq",
            "sources": _infer_interrupt_sources(state, regmap),
        },
        "hardware_features": {
            "has_dma": False,
            "has_programmable_pll": False,
            "has_reset_cause_registers": False,
            "has_programmable_power_modes": False,
        },
        "bringup_model": {
            "type": "register_driven_mixed_signal_bringup",
            "requires_os_handoff": False,
        },
        "build": {
            "target_triple": state.get("firmware_target_triple") or "x86_64-unknown-linux-gnu",
            "build_root": build_root,
            "src_root": src_root,
            "hal_root": "firmware/hal",
            "driver_root": "firmware/drivers",
        },
        "provenance": {
            "agent": AGENT_NAME,
            "phase": PHASE,
            "regmap_mode": regmap.get("__source", {}).get("mode", ""),
        },
    }
    return manifest


def _write_debug(state: dict, base_debug: dict, extra: dict) -> None:
    payload = {**base_debug, **extra}
    logger.info(
        "%s debug: mode=%s selected_regmap_path=%s candidate_regs_count=%s",
        AGENT_NAME,
        payload.get("mode"),
        payload.get("selected_regmap_path"),
        payload.get("candidate_regs_count"),
    )
    write_artifact(state, DEBUG_PATH, json.dumps(payload, indent=2), key=os.path.basename(DEBUG_PATH))


def _publish_outputs(state: dict, regmap: dict, regmap_path: str, mode: str, source_obj: Optional[dict] = None) -> dict:
    write_artifact(state, OUTPUT_PATH, json.dumps(regmap, indent=2), key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps(_build_summary(regmap, mode, regmap_path), indent=2),
        key=os.path.basename(SUMMARY_PATH),
    )

    manifest = _build_manifest(state, regmap, source_obj=source_obj, regmap_path=regmap_path)
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    state["firmware_register_map"] = regmap
    state["firmware_register_map_path"] = OUTPUT_PATH
    state["firmware_register_summary_path"] = SUMMARY_PATH
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH

    firmware_block = state.setdefault("firmware", {})
    firmware_block["register_map"] = regmap
    firmware_block["register_map_path"] = OUTPUT_PATH
    firmware_block["register_map_summary_path"] = SUMMARY_PATH
    firmware_block["manifest"] = manifest
    firmware_block["manifest_path"] = MANIFEST_PATH

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    embedded["firmware_manifest"] = MANIFEST_PATH
    return state


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    candidate_paths = [
        "digital/digital_regmap.json",
        "digital/regmap.json",
        "digital/register_map.json",
        "regmap.json",
    ]

    digital_state = state.get("digital") or {}
    upstream_regmap = None
    regmap_path = ""

    if isinstance(state.get("digital_regmap"), dict):
        upstream_regmap = state["digital_regmap"]
        regmap_path = "state.digital_regmap"
    elif isinstance(digital_state.get("regmap"), dict):
        upstream_regmap = digital_state["regmap"]
        regmap_path = "state.digital.regmap"
    elif isinstance(digital_state.get("digital_regmap"), dict):
        upstream_regmap = digital_state["digital_regmap"]
        regmap_path = "state.digital.digital_regmap"

    candidate_probe = []
    for rel in candidate_paths:
        abs_path = os.path.join(workflow_dir, rel) if workflow_dir else rel
        candidate_probe.append({"rel": rel, "abs": abs_path, "exists": os.path.isfile(abs_path)})

    if upstream_regmap is None:
        regmap_path = _find_first_existing(workflow_dir, candidate_paths) if workflow_dir else ""

    if upstream_regmap is None and not regmap_path:
        state_candidates = [
            state.get("digital_regmap_path"),
            state.get("digital_register_map_path"),
            digital_state.get("regmap_path"),
            digital_state.get("digital_regmap_path"),
            digital_state.get("register_map_path"),
        ]
        for cand in state_candidates:
            if not cand:
                continue
            if os.path.isabs(cand) and os.path.isfile(cand):
                regmap_path = cand
                break
            if workflow_dir:
                joined = os.path.join(workflow_dir, cand)
                if os.path.isfile(joined):
                    regmap_path = joined
                    break
            if os.path.isfile(cand):
                regmap_path = cand
                break

    if upstream_regmap is None:
        upstream_regmap = _safe_load_json(regmap_path)

    debug_info = {
        "workflow_dir": workflow_dir,
        "candidate_paths": candidate_paths,
        "candidate_probe": candidate_probe,
        "state_digital_regmap_present": isinstance(state.get("digital_regmap"), dict),
        "state_digital_regmap_path": state.get("digital_regmap_path"),
        "state_digital_register_map_path": state.get("digital_register_map_path"),
        "state_digital_block_keys": list(digital_state.keys()) if isinstance(digital_state, dict) else [],
        "selected_regmap_path": regmap_path,
        "upstream_regmap_type": type(upstream_regmap).__name__,
        "passthrough_taken": False,
        "normalization_taken": False,
        "candidate_regs_count": 0,
        "goal_present": bool(goal),
        "toolchain_keys": sorted(list(toolchain.keys())) if isinstance(toolchain, dict) else [],
        "toggle_keys": sorted(list(toggles.keys())) if isinstance(toggles, dict) else [],
    }

    if isinstance(upstream_regmap, dict) and upstream_regmap.get("error"):
        _write_debug(
            state,
            debug_info,
            {
                "mode": "reject_invalid_upstream_regmap",
                "reason": upstream_regmap.get("error"),
                "upstream_regmap_preview": upstream_regmap,
            },
        )
        state["status"] = f"❌ upstream digital regmap invalid: {upstream_regmap.get('error')}"
        return state

    if isinstance(upstream_regmap, dict):
        source_obj = dict(upstream_regmap)
        if isinstance(source_obj.get("regmap"), dict):
            source_obj = source_obj["regmap"]
        elif isinstance(source_obj.get("register_map"), dict) and "registers" in source_obj["register_map"]:
            source_obj = source_obj["register_map"]

        candidate_regs = _extract_register_candidates(source_obj)
        debug_info["candidate_regs_count"] = len(candidate_regs)

        if candidate_regs:
            debug_info["passthrough_taken"] = True
            passthrough_out = {
                "block_name": _infer_block_name(state, source_obj, source_obj),
                "module_name": _infer_block_name(state, source_obj, source_obj),
                "base_address": _fmt_hex32(
                    source_obj.get("base_address")
                    or source_obj.get("base_addr")
                    or source_obj.get("mmio_base")
                    or source_obj.get("base")
                    or "0x00000000"
                ),
                "bus": _infer_bus_type_from_sources(state, source_obj, source_obj, spec_text, goal),
                "registers": _sort_registers([_normalize_register(reg) for reg in candidate_regs]),
                "__source": {
                    "mode": "artifact_passthrough",
                    "path": regmap_path,
                },
            }
            _write_debug(
                state,
                debug_info,
                {
                    "mode": "artifact_passthrough",
                    "register_names_preview": [
                        reg.get("name") for reg in passthrough_out["registers"][:10] if isinstance(reg, dict)
                    ],
                },
            )
            logger.info("%s preserved %d registers", AGENT_NAME, len(passthrough_out["registers"]))
            _publish_outputs(state, passthrough_out, regmap_path, "artifact_passthrough", source_obj=source_obj)
            state["status"] = f"✅ {AGENT_NAME} preserved upstream register map and emitted firmware manifest"
            return state

        debug_info["normalization_taken"] = True
        normalized = _normalize_registers_from_any_shape(upstream_regmap)
        normalized["bus"] = _infer_bus_type_from_sources(state, upstream_regmap, normalized, spec_text, goal)
        normalized["block_name"] = _infer_block_name(state, upstream_regmap, normalized)
        normalized["module_name"] = normalized["block_name"]
        normalized["__source"] = {
            "mode": "artifact_first_normalization",
            "path": regmap_path,
        }
        regs = normalized.get("registers") or []
        debug_info["candidate_regs_count"] = len(regs)

        if not regs:
            _write_debug(
                state,
                debug_info,
                {
                    "mode": "artifact_first_normalization",
                    "error": "Upstream digital regmap was found, but no concrete registers were extracted.",
                    "upstream_regmap_preview": upstream_regmap,
                    "normalized_result": normalized,
                },
            )
            state["status"] = f"❌ {AGENT_NAME} found upstream regmap but extracted zero registers"
            state["firmware_register_map_path"] = DEBUG_PATH
            embedded = state.setdefault("embedded", {})
            embedded[PHASE] = DEBUG_PATH
            return state

        _write_debug(
            state,
            debug_info,
            {
                "mode": "artifact_first_normalization",
                "register_names_preview": [reg.get("name") for reg in regs[:10] if isinstance(reg, dict)],
            },
        )
        logger.info("%s normalized %d registers", AGENT_NAME, len(regs))
        _publish_outputs(state, normalized, regmap_path, "artifact_first_normalization", source_obj=upstream_regmap)
        state["status"] = f"✅ {AGENT_NAME} normalized upstream register map and emitted firmware manifest"
        return state

    _write_debug(
        state,
        debug_info,
        {
            "mode": "llm_fallback_no_upstream_regmap",
            "reason": "upstream_regmap was not a dict; artifact-first path was skipped",
            "selected_regmap_path_exists": bool(regmap_path),
        },
    )

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
        system="You are a senior firmware engineer. Output valid JSON only. No markdown fences.",
    )
    if not out:
        state["status"] = "❌ Register extract LLM returned empty output"
        return state

    out = strip_outer_markdown_fences(out)
    try:
        parsed = json.loads(out)
    except Exception:
        state["status"] = "❌ Register extract LLM returned invalid JSON"
        return state

    if not parsed.get("registers"):
        state["status"] = "❌ Register extract produced zero registers"
        return state

    parsed = _normalize_registers_from_any_shape(parsed)
    parsed["bus"] = _infer_bus_type_from_sources(state, parsed, parsed, spec_text, goal)
    parsed["block_name"] = _infer_block_name(state, parsed, parsed)
    parsed["module_name"] = parsed["block_name"]
    parsed["__source"] = {"mode": "llm_fallback", "path": "llm"}
    logger.info("%s fallback generated %d registers", AGENT_NAME, len(parsed.get("registers") or []))
    _publish_outputs(state, parsed, "llm", "llm_fallback", source_obj=parsed)
    _write_debug(
        state,
        debug_info,
        {
            "mode": "llm_fallback_completed",
            "reason": "artifact-first path did not run or did not return",
            "llm_output_parsed": True,
            "llm_output_preview": parsed,
        },
    )
    state["status"] = f"✅ {AGENT_NAME} done"
    return state
