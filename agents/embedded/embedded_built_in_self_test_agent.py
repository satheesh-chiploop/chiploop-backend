
import json
import logging
import os
import re
from typing import Dict, List, Optional

from ._embedded_common import ensure_workflow_dir, llm_chat, strip_markdown_fences_for_code, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Built In Self Test Agent"
PHASE = "bist"
OUTPUT_PATH = "firmware/diagnostics/bist.rs"
DEBUG_PATH = "firmware/diagnostics/bist_debug.json"
SUMMARY_PATH = "firmware/diagnostics/bist_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed to load JSON from %s: %s", AGENT_NAME, path, exc)
    return None


def _safe_read(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as exc:
        logger.warning("%s failed to read %s: %s", AGENT_NAME, path, exc)
    return ""


def _load_manifest(state: dict, workflow_dir: str) -> dict:
    manifest = state.get("firmware_manifest") or (state.get("firmware") or {}).get("manifest")
    if isinstance(manifest, dict):
        return dict(manifest)
    manifest_path = state.get("firmware_manifest_path") or (state.get("firmware") or {}).get("manifest_path") or MANIFEST_PATH
    if manifest_path and not os.path.isabs(manifest_path):
        manifest_path = os.path.join(workflow_dir, manifest_path)
    loaded = _safe_load_json(manifest_path)
    return loaded if isinstance(loaded, dict) else {}


def _load_regmap(state: dict, workflow_dir: str, manifest: dict) -> dict:
    regmap = state.get("firmware_register_map") or (state.get("firmware") or {}).get("register_map")
    if isinstance(regmap, dict):
        return regmap
    regmap_path = state.get("firmware_register_map_path") or manifest.get("register_map_path") or "firmware/register_map.json"
    if regmap_path and not os.path.isabs(regmap_path):
        regmap_path = os.path.join(workflow_dir, regmap_path)
    loaded = _safe_load_json(regmap_path)
    return loaded if isinstance(loaded, dict) else {}


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


def _pick_candidate_fields(regmap: dict) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    for reg in regmap.get("registers") or []:
        if not isinstance(reg, dict):
            continue
        reg_name = str(reg.get("name") or "")
        reg_access = str(reg.get("access") or "RW").upper()
        for field in reg.get("fields") or []:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or "")
            field_access = str(field.get("access") or reg_access).upper()
            field_upper = field_name.upper()
            bit_width = int(field.get("bit_width") or 1)

            if field_access in {"RW", "WO", "RW1C", "W1C"} and any(
                token in field_upper for token in ("ENABLE", "EN", "START", "GO", "TRIGGER", "INIT")
            ):
                candidates.append({
                    "kind": "boolish_write",
                    "register": reg_name,
                    "field": field_name,
                    "rust_type": _field_rust_type(bit_width),
                })

            if field_access in {"RO", "RW", "RW1C"} and any(
                token in field_upper for token in ("READY", "DONE", "VALID", "FAULT", "ERROR", "ERR")
            ):
                candidates.append({
                    "kind": "observable",
                    "register": reg_name,
                    "field": field_name,
                    "rust_type": _field_rust_type(bit_width),
                })

    return candidates


def _deterministic_bist(manifest: dict, regmap: dict) -> str:
    driver_name = (
        manifest.get("driver_name")
        or "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", manifest.get("digital_block_name") or "digital_subsystem") if part)
        + "Driver"
    )
    if driver_name == "Driver":
        driver_name = "DigitalSubsystemDriver"

    lines: List[str] = [
        "use crate::drivers::driver_scaffold::*;",
        "",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
        "pub enum BistError {",
        "    ObservationFailed,",
        "}",
        "",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
        "pub struct BistReport {",
        "    pub checks_run: u32,",
        "    pub checks_passed: u32,",
        "}",
        "",
        "#[inline]",
        "fn pass(report: &mut BistReport) {",
        "    report.checks_run += 1;",
        "    report.checks_passed += 1;",
        "}",
        "",
        "#[inline]",
        "fn fail(report: &mut BistReport) {",
        "    report.checks_run += 1;",
        "}",
        "",
        "pub fn run_bist(driver: &" + driver_name + ") -> Result<BistReport, BistError> {",
        "    let mut report = BistReport { checks_run: 0, checks_passed: 0 };",
    ]

    candidates = _pick_candidate_fields(regmap)
    wrote_any = False
    observed_any = False

    for c in candidates:
        reg_ident = _safe_identifier(c["register"])
        field_ident = _safe_identifier(c["field"])
        get_fn = f"get_{reg_ident}_{field_ident}"
        set_fn = f"set_{reg_ident}_{field_ident}"
        if c["kind"] == "boolish_write" and c["rust_type"] == "bool":
            lines.extend([
                f"    driver.{set_fn}(true);",
                f"    if driver.{get_fn}() {{",
                "        pass(&mut report);",
                "    } else {",
                "        fail(&mut report);",
                "    }",
                "",
            ])
            wrote_any = True
        elif c["kind"] == "boolish_write":
            lines.extend([
                f"    driver.{set_fn}(1 as {c['rust_type']});",
                f"    let observed_{reg_ident}_{field_ident} = driver.{get_fn}();",
                f"    if observed_{reg_ident}_{field_ident} == 1 as {c['rust_type']} {{",
                "        pass(&mut report);",
                "    } else {",
                "        fail(&mut report);",
                "    }",
                "",
            ])
            wrote_any = True
        elif c["kind"] == "observable" and not observed_any:
            lines.extend([
                f"    let _sample_{reg_ident}_{field_ident} = driver.{get_fn}();",
                "    pass(&mut report);",
                "",
            ])
            observed_any = True

    if not wrote_any and not observed_any:
        lines.extend([
            "    // ASSUMPTION: No firmware-visible self-test toggles were present; perform a baseline register read check only.",
            "    let _ = driver.read_ctrl();",
            "    pass(&mut report);",
            "",
        ])

    lines.extend([
        "    if report.checks_passed == 0 {",
        "        return Err(BistError::ObservationFailed);",
        "    }",
        "    Ok(report)",
        "}",
        "",
    ])
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    manifest = _load_manifest(state, workflow_dir)
    regmap_obj = _load_regmap(state, workflow_dir, manifest)
    driver_code = state.get("firmware_driver_code") or (state.get("firmware") or {}).get("driver_code")
    if not driver_code:
        driver_path = state.get("firmware_driver_path") or manifest.get("driver_path") or "firmware/drivers/driver_scaffold.rs"
        if driver_path and not os.path.isabs(driver_path):
            driver_path = os.path.join(workflow_dir, driver_path)
        driver_code = _safe_read(driver_path)

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

FIRMWARE MANIFEST:
{json.dumps(manifest, indent=2)}

REGISTER MAP:
{json.dumps(regmap_obj, indent=2)}

DRIVER SCAFFOLD:
{driver_code if driver_code else "(not available)"}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Create BIST hooks and test routines.

OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/diagnostics/bist.rs
- Output MUST be raw, compile-ready Rust MODULE code only (no markdown fences, no prose).
- This file is a Rust MODULE (not crate root). DO NOT emit crate attributes.
- Do not define a main() or panic_handler in this module.
- Do not use Vec or heap allocations.
- Use fixed arrays or stack buffers only.
- Reuse the existing driver layer when available.
- Do not invent hardware capabilities not present in the register map or manifest.
- If information is missing, assumptions only as Rust comments at top:
  // ASSUMPTION: ...
"""
    out = llm_chat(
        prompt,
        system="You are a senior embedded firmware engineer. Output MUST be raw, compile-ready Rust only. NEVER include markdown fences.",
    )
    if not out:
        logger.warning("%s LLM returned empty output; using deterministic fallback", AGENT_NAME)
        out = _deterministic_bist(manifest, regmap_obj)

    out = strip_markdown_fences_for_code(out).strip() + "\n"

    if any(marker in out for marker in ("```", "#![no_std]", "#![no_main]", "fn main(")):
        logger.warning("%s output contained invalid module patterns; using deterministic fallback", AGENT_NAME)
        out = _deterministic_bist(manifest, regmap_obj)

    if "run_bist" not in out:
        logger.warning("%s output missing run_bist; using deterministic fallback", AGENT_NAME)
        out = _deterministic_bist(manifest, regmap_obj)

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "regmap_present": bool(regmap_obj),
                "driver_present": bool(driver_code),
                "register_count": len(regmap_obj.get("registers") or []),
            },
            indent=2,
        ),
        key=os.path.basename(DEBUG_PATH),
    )
    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "phase": PHASE,
                "bist_path": OUTPUT_PATH,
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    state["firmware_bist_path"] = OUTPUT_PATH
    diag = state.setdefault("firmware_diagnostics", {})
    diag["bist_path"] = OUTPUT_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state
