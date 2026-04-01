
import json
import logging
import os
import re
from typing import Any, List, Optional

from ._embedded_common import ensure_workflow_dir, llm_chat, strip_markdown_fences_for_code, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Rust Register Layer Generator Agent"
PHASE = "hal_generate"
OUTPUT_PATH = "firmware/hal/registers.rs"
DEBUG_PATH = "firmware/hal/registers_debug.json"
SUMMARY_PATH = "firmware/hal/registers_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed to load JSON from %s: %s", AGENT_NAME, path, exc)
    return None


def _parse_intish(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            s = value.strip().lower()
            if s.startswith("0x"):
                return int(s, 16)
            return int(s)
    except Exception:
        pass
    return default


def _reg_names(registers: List[dict]) -> List[str]:
    return [reg.get("name") for reg in registers if isinstance(reg, dict) and reg.get("name")]


def _safe_identifier(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not ident:
        ident = "unnamed"
    if ident[0].isdigit():
        ident = f"r_{ident}"
    return ident.lower()


def _safe_const_name(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not ident:
        ident = "UNNAMED"
    if ident[0].isdigit():
        ident = f"R_{ident}"
    return ident.upper()


def _write_debug(state: dict, payload: dict) -> None:
    write_artifact(state, DEBUG_PATH, json.dumps(payload, indent=2), key=os.path.basename(DEBUG_PATH))


def _load_manifest(state: dict, workflow_dir: str) -> dict:
    manifest = state.get("firmware_manifest") or (state.get("firmware") or {}).get("manifest")
    if isinstance(manifest, dict):
        return dict(manifest)

    manifest_path = state.get("firmware_manifest_path") or (state.get("firmware") or {}).get("manifest_path") or MANIFEST_PATH
    if manifest_path and not os.path.isabs(manifest_path):
        manifest_path = os.path.join(workflow_dir, manifest_path)
    loaded = _safe_load_json(manifest_path)
    return loaded if isinstance(loaded, dict) else {}


def _normalize_regmap(regmap: dict) -> dict:
    if not isinstance(regmap, dict):
        return {}

    flat_registers: List[dict] = []
    if isinstance(regmap.get("registers"), list):
        flat_registers = regmap["registers"]
    elif isinstance(regmap.get("blocks"), list):
        for blk in regmap["blocks"]:
            if isinstance(blk, dict):
                flat_registers.extend(blk.get("registers", []))

    if not flat_registers:
        return {}

    for reg in flat_registers:
        if "name" not in reg or "offset" not in reg:
            raise ValueError(f"malformed register entry: {reg}")

    return {
        "block_name": regmap.get("block_name") or regmap.get("module_name") or "digital_subsystem",
        "module_name": regmap.get("module_name") or regmap.get("block_name") or "digital_subsystem",
        "base_address": regmap.get("base_address") or "0x00000000",
        "registers": sorted(flat_registers, key=lambda r: _parse_intish((r or {}).get("offset", 0))),
    }


def _merge_manifest(manifest: dict, regmap: dict) -> dict:
    merged = dict(manifest or {})
    build = dict(merged.get("build") or {})
    merged["register_map_path"] = merged.get("register_map_path") or "firmware/register_map.json"
    merged["hal_path"] = OUTPUT_PATH
    merged["base_address"] = regmap.get("base_address") or merged.get("base_address") or "0x00000000"
    merged["digital_block_name"] = (
        merged.get("digital_block_name")
        or regmap.get("module_name")
        or regmap.get("block_name")
        or "digital_subsystem"
    )
    build.setdefault("hal_root", "firmware/hal")
    merged["build"] = build
    return merged


def _mask_expr(bit_width: int, bit_offset: int) -> str:
    if bit_width >= 32:
        return "0xFFFF_FFFF"
    mask = ((1 << bit_width) - 1) << bit_offset
    return f"0x{mask:08X}"


def _field_const_prefix(reg_name: str, field_name: str) -> str:
    return f"{_safe_const_name(reg_name)}_{_safe_const_name(field_name)}"


def _default_hal_from_regmap(regmap: dict) -> str:
    regs = regmap.get("registers") or []
    base_address = regmap.get("base_address") or "0x00000000"

    const_lines: List[str] = []
    helper_lines: List[str] = []

    for reg in regs:
        reg_name = reg.get("name") or "UNNAMED"
        reg_ident = _safe_identifier(reg_name)
        reg_const = _safe_const_name(reg_name)
        offset = reg.get("offset") or "0x00000000"
        reg_access = str(reg.get("access") or "RW").upper()

        const_lines.append(f"pub const {reg_const}_OFFSET: usize = {offset};")

        for field in reg.get("fields") or []:
            fname = field.get("name") or "UNNAMED_FIELD"
            bit_offset = int(field.get("bit_offset") or 0)
            bit_width = int(field.get("bit_width") or 1)
            prefix = _field_const_prefix(reg_name, fname)
            mask_expr = _mask_expr(bit_width, bit_offset)

            const_lines.append(f"pub const {prefix}_SHIFT: u32 = {bit_offset};")
            const_lines.append(f"pub const {prefix}_WIDTH: u32 = {bit_width};")
            const_lines.append(f"pub const {prefix}_MASK: u32 = {mask_expr};")

        helper_lines.extend(
            [
                "#[inline]",
                f"pub fn read_{reg_ident}() -> u32 {{",
                f"    read_reg({reg_const}_OFFSET)",
                "}",
                "",
            ]
        )

        if reg_access not in {"RO"}:
            helper_lines.extend(
                [
                    "#[inline]",
                    f"pub fn write_{reg_ident}(value: u32) {{",
                    f"    write_reg({reg_const}_OFFSET, value)",
                    "}",
                    "",
                ]
            )

        for field in reg.get("fields") or []:
            fname = field.get("name") or "UNNAMED_FIELD"
            fident = _safe_identifier(fname)
            prefix = _field_const_prefix(reg_name, fname)
            access = str(field.get("access") or reg_access).upper()

            helper_lines.extend(
                [
                    "#[inline]",
                    f"pub fn get_{reg_ident}_{fident}() -> u32 {{",
                    f"    (read_{reg_ident}() & {prefix}_MASK) >> {prefix}_SHIFT",
                    "}",
                    "",
                ]
            )

            if access not in {"RO"}:
                helper_lines.extend(
                    [
                        "#[inline]",
                        f"pub fn set_{reg_ident}_{fident}(value: u32) {{",
                        f"    let current = read_{reg_ident}();",
                        f"    let next = (current & !{prefix}_MASK) | ((value << {prefix}_SHIFT) & {prefix}_MASK);",
                        f"    write_{reg_ident}(next);",
                        "}",
                        "",
                    ]
                )

    return (
        "use core::ptr::{read_volatile, write_volatile};\n\n"
        f"pub const BASE_ADDRESS: usize = {base_address};\n\n"
        + "\n".join(const_lines)
        + "\n\n"
        + "#[inline]\n"
        + "fn reg_ptr(offset: usize) -> *mut u32 {\n"
        + "    (BASE_ADDRESS + offset) as *mut u32\n"
        + "}\n\n"
        + "#[inline]\n"
        + "fn read_reg(offset: usize) -> u32 {\n"
        + "    unsafe { read_volatile(reg_ptr(offset) as *const u32) }\n"
        + "}\n\n"
        + "#[inline]\n"
        + "fn write_reg(offset: usize, value: u32) {\n"
        + "    unsafe { write_volatile(reg_ptr(offset), value) }\n"
        + "}\n\n"
        + "\n".join(helper_lines)
        + "\n"
    )




def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}
    workflow_dir = state.get("workflow_dir") or ""

    manifest = _load_manifest(state, workflow_dir)

    regmap = state.get("firmware_register_map") or (state.get("firmware") or {}).get("register_map")
    if not regmap:
        regmap_path = (
            state.get("firmware_register_map_path")
            or manifest.get("register_map_path")
            or "firmware/register_map.json"
        )
        if regmap_path and not os.path.isabs(regmap_path):
            regmap_path = os.path.join(workflow_dir, regmap_path)
        regmap = _safe_load_json(regmap_path)

    try:
        normalized_regmap = _normalize_regmap(regmap)
    except Exception as exc:
        state["status"] = f"❌ HAL generation malformed register map: {exc}"
        return state

    if not normalized_regmap:
        state["status"] = "❌ HAL generation received regmap with zero concrete registers"
        return state

    regmap_json = json.dumps(normalized_regmap, indent=2)[:12000]
    register_names = _reg_names(normalized_regmap.get("registers", []))

    logger.info(
        "%s using manifest=%s regmap=%s register_count=%d",
        AGENT_NAME,
        bool(manifest),
        True,
        len(register_names),
    )
    _write_debug(
        state,
        {
            "agent": AGENT_NAME,
            "phase": PHASE,
            "workflow_dir": workflow_dir,
            "manifest_present": bool(manifest),
            "register_count": len(register_names),
            "register_names": register_names,
            "base_address": normalized_regmap.get("base_address"),
            "block_name": normalized_regmap.get("block_name"),
            "module_name": normalized_regmap.get("module_name"),
        },
    )

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

FIRMWARE MANIFEST:
{json.dumps(manifest, indent=2)}

REGISTER MAP (AUTHORITATIVE SOURCE):
{regmap_json}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate the Rust HAL register layer for the firmware register map.

STRICT REQUIREMENTS:
- Use REGISTER MAP as the source of truth when present.
- Do NOT invent registers, fields, offsets, or base addresses.
- Preserve exact register names from REGISTER MAP as much as possible.
- Emit one BASE_ADDRESS constant from REGISTER MAP or FIRMWARE MANIFEST.
- Emit one *_OFFSET constant per register using the exact register offset.
- Preserve exact register and field semantics from REGISTER MAP.
- Access registers using BASE_ADDRESS + OFFSET so sparse or irregular MMIO maps remain correct.
- Do NOT assume registers are contiguous in memory.
- You may emit a RegisterBlock only if you also emit correct reserved padding for all address gaps.
- If padding is not explicitly generated and verified, use raw offset-based access helpers instead.
- This file is crate::hal::registers module content only.
- Do NOT emit crate attributes.
- Do NOT emit prose.
- Do NOT wrap in mod/pubs.

RUST STYLE REQUIREMENTS:
- Use core::ptr::read_volatile / write_volatile or a minimal compile-safe pattern.
- Avoid fictional types like VolatileCell unless you fully define them in this file.
- The output must be plausible for bare-metal/no_std firmware.
- The module must be compatible with later driver generation.

OUTPUT:
RAW RUST ONLY.
Write to firmware/hal/registers.rs
"""

    out = llm_chat(
        prompt,
        system="You are a senior embedded Rust engineer. Produce compile-ready Rust module code only. Never use markdown fences.",
    )

    if not out:
        logger.warning("%s LLM returned empty output; using deterministic fallback HAL", AGENT_NAME)
        out = _default_hal_from_regmap(normalized_regmap)

    out = strip_markdown_fences_for_code(out)
    out = out.replace("#![no_std]", "")
    out = out.replace("#![no_main]", "")
    out = out.strip() + "\n"

    lines = out.splitlines()
    if lines and lines[0].strip().startswith("pub mod registers"):
        body = lines[1:]
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip() == "}":
            body.pop()
        out = "\n".join(body).strip() + "\n"
        lines = out.splitlines()

    if lines and re.match(r"(pub\s+)?mod\s+[A-Za-z_]\w*\s*\{?", lines[0].strip()):
        body = lines[1:]
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip() == "}":
            body.pop()
        out = "\n".join(body).strip() + "\n"

    bad_markers = ["Certainly!", "Below is an example", "Make sure to substitute", "Here is", "```"]
    if any(marker in out for marker in bad_markers):
        logger.warning("%s returned prose; using deterministic fallback HAL", AGENT_NAME)
        out = _default_hal_from_regmap(normalized_regmap)

    required_tokens = ["BASE_ADDRESS"]
    if any(token not in out for token in required_tokens):
        logger.warning("%s output shape weak; replacing with deterministic fallback HAL", AGENT_NAME)
        out = _default_hal_from_regmap(normalized_regmap)

    missing_offset_consts = []
    missing_reg_helpers = []
    missing_field_helpers = []

    for reg in normalized_regmap.get("registers", []):
        reg_name = reg.get("name") or "UNNAMED"
        reg_ident = _safe_identifier(reg_name)
        reg_const = _safe_const_name(reg_name)
        reg_access = str(reg.get("access") or "RW").upper()

        offset_const = f"{reg_const}_OFFSET"
        if offset_const not in out:
            missing_offset_consts.append(offset_const)

        read_helper = f"read_{reg_ident}"
        if read_helper not in out:
            missing_reg_helpers.append(read_helper)

        write_helper = f"write_{reg_ident}"
        if reg_access not in {"RO"} and write_helper not in out:
            missing_reg_helpers.append(write_helper)

        for field in reg.get("fields") or []:
            field_name = field.get("name") or "UNNAMED_FIELD"
            field_ident = _safe_identifier(field_name)
            field_access = str(field.get("access") or reg_access).upper()

            getter = f"get_{reg_ident}_{field_ident}"
            if getter not in out:
                missing_field_helpers.append(getter)

            setter = f"set_{reg_ident}_{field_ident}"
            if field_access not in {"RO"} and setter not in out:
                missing_field_helpers.append(setter)

    if missing_offset_consts or missing_reg_helpers or missing_field_helpers:
        logger.warning(
            "%s output missing required HAL contract pieces; offsets=%s reg_helpers=%s field_helpers=%s. Using deterministic fallback HAL",
            AGENT_NAME,
            missing_offset_consts[:5],
            missing_reg_helpers[:5],
            missing_field_helpers[:5],
        )
        out = _default_hal_from_regmap(normalized_regmap)

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "phase": PHASE,
                "hal_path": OUTPUT_PATH,
                "register_count": len(register_names),
                "register_names": register_names,
                "base_address": normalized_regmap.get("base_address"),
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    manifest = _merge_manifest(manifest, normalized_regmap)
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    state["firmware_hal_code"] = out
    state["firmware_hal_path"] = OUTPUT_PATH
    state["firmware_hal_summary_path"] = SUMMARY_PATH
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH

    firmware_block = state.setdefault("firmware", {})
    firmware_block["hal_code"] = out
    firmware_block["hal_path"] = OUTPUT_PATH
    firmware_block["hal_summary_path"] = SUMMARY_PATH
    firmware_block["manifest"] = manifest
    firmware_block["manifest_path"] = MANIFEST_PATH

    logger.info("%s generated HAL for %d registers", AGENT_NAME, len(register_names))
    state["status"] = f"✅ {AGENT_NAME} done"
    return state
