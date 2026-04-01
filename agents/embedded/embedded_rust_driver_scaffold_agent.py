
import json
import logging
import os
import re
from typing import Optional

from ._embedded_common import ensure_workflow_dir, llm_chat, strip_markdown_fences_for_code, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Rust Driver Scaffold Agent"
PHASE = "driver_scaffold"
OUTPUT_PATH = "firmware/drivers/driver_scaffold.rs"
DEBUG_PATH = "firmware/drivers/driver_scaffold_debug.json"
SUMMARY_PATH = "firmware/drivers/driver_scaffold_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_read(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as exc:
        logger.warning("%s failed reading %s: %s", AGENT_NAME, path, exc)
    return ""


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed loading %s: %s", AGENT_NAME, path, exc)
    return None


def _derive_driver_name(regmap_obj: dict) -> str:
    raw = (
        (regmap_obj or {}).get("module_name")
        or (regmap_obj or {}).get("block_name")
        or "digital_subsystem"
    )
    parts = re.split(r"[^A-Za-z0-9]+", raw)
    camel = "".join(part[:1].upper() + part[1:] for part in parts if part)
    return f"{camel}Driver" if camel else "DigitalSubsystemDriver"


def _safe_identifier(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not ident:
        ident = "unnamed"
    if ident[0].isdigit():
        ident = f"r_{ident}"
    return ident.lower()


def _field_rust_type(bit_width: int) -> str:
    if bit_width <= 1:
        return "bool"
    if bit_width <= 8:
        return "u8"
    if bit_width <= 16:
        return "u16"
    return "u32"


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

def _flatten_registers(regmap_obj: dict) -> list[dict]:
    if isinstance(regmap_obj.get("registers"), list):
        return [reg for reg in regmap_obj.get("registers", []) if isinstance(reg, dict)]

    regs = []
    for blk in regmap_obj.get("blocks") or []:
        if isinstance(blk, dict):
            regs.extend([reg for reg in (blk.get("registers") or []) if isinstance(reg, dict)])
    return regs


def _build_deterministic_driver(regmap_obj: dict, driver_name: str) -> str:
    regs = _flatten_registers(regmap_obj)

    lines = [
        "use crate::hal::registers::*;",
        "",
        f"pub struct {driver_name};",
        "",
        f"impl {driver_name} {{",
        "    #[inline]",
        "    pub const fn new() -> Self {",
        "        Self",
        "    }",
        "",
    ]

    for reg in regs:
        reg_name = reg.get("name") or "UNNAMED"
        reg_ident = _safe_identifier(reg_name)
        access = str(reg.get("access") or "RW").upper()

        lines.extend(
            [
                "    #[inline]",
                f"    pub fn read_{reg_ident}(&self) -> u32 {{",
                f"        read_{reg_ident}()",
                "    }",
                "",
            ]
        )

        if access not in {"RO"}:
            lines.extend(
                [
                    "    #[inline]",
                    f"    pub fn write_{reg_ident}(&self, value: u32) {{",
                    f"        write_{reg_ident}(value)",
                    "    }",
                    "",
                ]
            )

        for field in reg.get("fields") or []:
            fname = field.get("name") or "UNNAMED_FIELD"
            fident = _safe_identifier(fname)
            bit_width = int(field.get("bit_width") or 1)
            rust_type = _field_rust_type(bit_width)
            field_access = str(field.get("access") or access).upper()
            hal_get = f"get_{reg_ident}_{fident}"
            hal_set = f"set_{reg_ident}_{fident}"

            if rust_type == "bool":
                lines.extend(
                    [
                        "    #[inline]",
                        f"    pub fn get_{reg_ident}_{fident}(&self) -> bool {{",
                        f"        {hal_get}() != 0",
                        "    }",
                        "",
                    ]
                )
                if field_access not in {"RO"}:
                    lines.extend(
                        [
                            "    #[inline]",
                            f"    pub fn set_{reg_ident}_{fident}(&self, value: bool) {{",
                            f"        {hal_set}(if value {{ 1 }} else {{ 0 }})",
                            "    }",
                            "",
                        ]
                    )
            else:
                lines.extend(
                    [
                        "    #[inline]",
                        f"    pub fn get_{reg_ident}_{fident}(&self) -> {rust_type} {{",
                        f"        {hal_get}() as {rust_type}",
                        "    }",
                        "",
                    ]
                )
                if field_access not in {"RO"}:
                    lines.extend(
                        [
                            "    #[inline]",
                            f"    pub fn set_{reg_ident}_{fident}(&self, value: {rust_type}) {{",
                            f"        {hal_set}(value as u32)",
                            "    }",
                            "",
                        ]
                    )

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    manifest = _load_manifest(state, workflow_dir)

    regmap_obj = state.get("firmware_register_map") or (state.get("firmware") or {}).get("register_map")
    if not regmap_obj:
        regmap_path = state.get("firmware_register_map_path") or manifest.get("register_map_path") or "firmware/register_map.json"
        if regmap_path and not os.path.isabs(regmap_path):
            regmap_path = os.path.join(workflow_dir, regmap_path)
        regmap_obj = _safe_load_json(regmap_path)
    if not isinstance(regmap_obj, dict):
        state["status"] = "❌ firmware register map missing in state for driver generation"
        return state

    hal_code = state.get("firmware_hal_code") or (state.get("firmware") or {}).get("hal_code")
    hal_path = state.get("firmware_hal_path") or manifest.get("hal_path") or "firmware/hal/registers.rs"
    if hal_path and not os.path.isabs(hal_path):
        hal_path = os.path.join(workflow_dir, hal_path)
    if not hal_code:
        hal_code = _safe_read(hal_path)
    if not hal_code:
        state["status"] = "❌ firmware HAL missing in state for driver generation"
        return state

    regmap = json.dumps(regmap_obj, indent=2)
    driver_name = _derive_driver_name(regmap_obj)
    all_regs = _flatten_registers(regmap_obj)
    register_names = [reg.get("name") for reg in all_regs if isinstance(reg, dict)]


    logger.info(
        "%s using regmap=%s hal=%s driver_name=%s register_count=%d",
        AGENT_NAME,
        True,
        True,
        driver_name,
        len(register_names),
    )
    _write_debug(
        state,
        {
            "agent": AGENT_NAME,
            "phase": PHASE,
            "regmap_path": state.get("firmware_register_map_path") or manifest.get("register_map_path") or "firmware/register_map.json",
            "hal_path": state.get("firmware_hal_path") or manifest.get("hal_path") or "firmware/hal/registers.rs",
            "driver_name": driver_name,
            "register_count": len(register_names),
            "register_names": register_names,
            "manifest_present": bool(manifest),
        },
    )

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

FIRMWARE MANIFEST:
{json.dumps(manifest, indent=2)}

REGISTER MAP (AUTHORITATIVE SOURCE):
{regmap}

HAL REGISTER LAYER (AUTHORITATIVE SOURCE):
{hal_code}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate a thin Rust driver scaffold over the existing HAL.

STRICT REQUIREMENTS:
- Use REGISTER MAP and HAL as the source of truth.
- Do NOT invent registers, fields, offsets, or helper blocks.
- Reference only registers that appear in REGISTER MAP.
- Reuse HAL items instead of redefining them.
- The driver must be thin and compile-oriented, not a fake full SDK.
- Prefer register-level and field-level methods aligned to real hardware intent.
- Do NOT wrap output in mod driver_scaffold.
- Use: use crate::hal::registers::*;
- Do NOT prefix imported HAL items with registers::
- Define exactly one public driver struct named {driver_name}.
- Emit only module contents.

RUST REQUIREMENTS:
- No crate attributes.
- No prose.
- Assumptions only as Rust comments.
- Keep methods simple and realistic for later validation and build stages.

OUTPUT:
RAW RUST ONLY.
Write to firmware/drivers/driver_scaffold.rs
"""

    out = llm_chat(
        prompt,
        system="You are a senior embedded Rust engineer. Produce compile-ready Rust only. Never use markdown code fences.",
    )
    if not out:
        logger.warning("%s LLM returned empty output; using deterministic fallback driver", AGENT_NAME)
        out = _build_deterministic_driver(regmap_obj, driver_name)

    out = strip_markdown_fences_for_code(out)
    out = out.replace("#![no_std]", "// no_std configured at crate root")

    bad_markers = ["Certainly!", "Below is an example", "Make sure to substitute", "Here is", "```"]
    if any(marker in out for marker in bad_markers):
        logger.warning("%s output contained prose; using deterministic fallback driver", AGENT_NAME)
        out = _build_deterministic_driver(regmap_obj, driver_name)

    lines = out.splitlines()
    if lines and re.match(r"pub\s+mod\s+driver_scaffold", lines[0].strip()):
        body = lines[1:]
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip() == "}":
            body.pop()
        out = "\n".join(body).strip() + "\n"
    else:
        out = out.strip() + "\n"

    if "use crate::hal::registers::*;" in out:
        out = out.replace("registers::register_block()", "register_block()")
        out = out.replace("registers::RegisterBlock", "RegisterBlock")

    out = re.sub(r"let\s+(\w+)\s*=\s*register_block\.(\w+)\s*;", r"let \1 = &register_block.\2;", out)

    if f"pub struct {driver_name}" not in out:
        logger.warning("%s output missing expected driver struct; using deterministic fallback driver", AGENT_NAME)
        out = _build_deterministic_driver(regmap_obj, driver_name)

    bogus_markers = ["RegisterType1", "RegisterType2", "OFFSET_REG1", "OFFSET_REG2"]
    found_bogus = [marker for marker in bogus_markers if marker in out]
    if found_bogus:
        logger.warning("%s output contained placeholder symbols %s; using deterministic fallback driver", AGENT_NAME, found_bogus)
        out = _build_deterministic_driver(regmap_obj, driver_name)

    required_hal_refs = ["use crate::hal::registers::*;"]
    if any(token not in out for token in required_hal_refs):
        logger.warning("%s output missing required HAL imports; using deterministic fallback driver", AGENT_NAME)
        out = _build_deterministic_driver(regmap_obj, driver_name)

    missing_driver_apis = []
    missing_hal_symbols = []



    for reg in _flatten_registers(regmap_obj):
        reg_name = reg.get("name") or "UNNAMED"
        reg_ident = _safe_identifier(reg_name)
        reg_access = str(reg.get("access") or "RW").upper()

        # Driver-level register helpers
        driver_read = f"read_{reg_ident}"
        if driver_read not in out:
            missing_driver_apis.append(driver_read)

        driver_write = f"write_{reg_ident}"
        if reg_access not in {"RO"} and driver_write not in out:
            missing_driver_apis.append(driver_write)

        # HAL-level register helpers
        hal_read = f"read_{reg_ident}"
        if hal_read not in hal_code:
            missing_hal_symbols.append(hal_read)

        hal_write = f"write_{reg_ident}"
        if reg_access not in {"RO"} and hal_write not in hal_code:
            missing_hal_symbols.append(hal_write)

        for field in reg.get("fields") or []:
            field_name = field.get("name") or "UNNAMED_FIELD"
            field_ident = _safe_identifier(field_name)
            field_access = str(field.get("access") or reg_access).upper()

            driver_get = f"get_{reg_ident}_{field_ident}"
            if driver_get not in out:
                missing_driver_apis.append(driver_get)

            hal_get = f"get_{reg_ident}_{field_ident}"
            if hal_get not in hal_code:
                missing_hal_symbols.append(hal_get)

            if field_access not in {"RO"}:
                driver_set = f"set_{reg_ident}_{field_ident}"
                if driver_set not in out:
                    missing_driver_apis.append(driver_set)

                hal_set = f"set_{reg_ident}_{field_ident}"
                if hal_set not in hal_code:
                    missing_hal_symbols.append(hal_set)

    if missing_driver_apis:
        logger.warning(
            "%s output missing required driver APIs %s; using deterministic fallback driver",
            AGENT_NAME,
            missing_driver_apis[:5],
        )
        out = _build_deterministic_driver(regmap_obj, driver_name)

    if missing_hal_symbols:
        logger.warning(
            "%s HAL is missing required symbols %s; using deterministic fallback driver",
            AGENT_NAME,
            missing_hal_symbols[:5],
        )
        out = _build_deterministic_driver(regmap_obj, driver_name)

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "phase": PHASE,
                "driver_path": OUTPUT_PATH,
                "driver_name": driver_name,
                "register_count": len(register_names),
                "register_names": register_names,
                "used_regmap": True,
                "used_hal": True,
                "used_spec": bool(spec_text),
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    manifest = dict(manifest or {})
    manifest["driver_path"] = OUTPUT_PATH
    manifest["driver_name"] = driver_name
    manifest["hal_path"] = manifest.get("hal_path") or "firmware/hal/registers.rs"
    manifest["digital_block_name"] = manifest.get("digital_block_name") or regmap_obj.get("module_name") or regmap_obj.get("block_name") or "digital_subsystem"
    build = dict(manifest.get("build") or {})
    build.setdefault("driver_root", "firmware/drivers")
    manifest["build"] = build
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    state["driver_scaffold_path"] = OUTPUT_PATH
    state["firmware_driver_path"] = OUTPUT_PATH
    state["firmware_driver_code"] = out
    state["firmware_driver_summary_path"] = SUMMARY_PATH
    state["firmware_driver_name"] = driver_name
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH

    firmware_block = state.setdefault("firmware", {})
    firmware_block["driver_scaffold_path"] = OUTPUT_PATH
    firmware_block["driver_code"] = out
    firmware_block["driver_summary_path"] = SUMMARY_PATH
    firmware_block["driver_name"] = driver_name
    firmware_block["manifest"] = manifest
    firmware_block["manifest_path"] = MANIFEST_PATH

    state["driver_scaffold_inputs"] = {
        "used_regmap": True,
        "used_hal": True,
        "used_spec": bool(spec_text),
    }

    logger.info("%s generated driver %s", AGENT_NAME, driver_name)
    state["status"] = f"✅ {AGENT_NAME} done"
    return state



