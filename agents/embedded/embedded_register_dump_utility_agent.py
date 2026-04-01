
import json
import logging
import os
import re
from typing import Optional

from ._embedded_common import ensure_workflow_dir, llm_chat, strip_markdown_fences_for_code, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Register Dump Utility Agent"
PHASE = "reg_dump"
OUTPUT_PATH = "firmware/diagnostics/register_dump.rs"
DEBUG_PATH = "firmware/diagnostics/register_dump_debug.json"
SUMMARY_PATH = "firmware/diagnostics/register_dump_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed loading %s: %s", AGENT_NAME, path, exc)
    return None


def _safe_read(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as exc:
        logger.warning("%s failed reading %s: %s", AGENT_NAME, path, exc)
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

def _flatten_registers(regmap: dict) -> list[dict]:
    if isinstance(regmap.get("registers"), list):
        return [r for r in regmap.get("registers", []) if isinstance(r, dict)]

    regs = []
    for blk in regmap.get("blocks") or []:
        if isinstance(blk, dict):
            regs.extend([r for r in (blk.get("registers") or []) if isinstance(r, dict)])
    return regs

def _deterministic_dump(regmap: dict) -> str:
    regs = _flatten_registers(regmap)    

    lines = [
        "use crate::hal::registers::*;",
        "",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
        "pub struct RegisterSnapshot {",
        "    pub name: &'static str,",
        "    pub value: u32,",
        "}",
        "",
        f"pub const REGISTER_SNAPSHOT_COUNT: usize = {len(regs)};",
        "",
        "pub fn dump_registers() -> [RegisterSnapshot; REGISTER_SNAPSHOT_COUNT] {",
        "    [",
    ]
    for reg in regs:
        reg_name = reg.get("name") or "UNNAMED"
        reg_ident = _safe_identifier(reg_name)
        lines.append(f'        RegisterSnapshot {{ name: "{reg_name}", value: read_{reg_ident}() }},')
    lines.extend([
        "    ]",
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
    regmap = _load_regmap(state, workflow_dir, manifest)
    hal_code = state.get("firmware_hal_code") or (state.get("firmware") or {}).get("hal_code")
    if not hal_code:
        hal_path = state.get("firmware_hal_path") or manifest.get("hal_path") or "firmware/hal/registers.rs"
        if hal_path and not os.path.isabs(hal_path):
            hal_path = os.path.join(workflow_dir, hal_path)
        hal_code = _safe_read(hal_path)

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

FIRMWARE MANIFEST:
{json.dumps(manifest, indent=2)}

REGISTER MAP:
{json.dumps(regmap, indent=2)}

HAL REGISTER LAYER:
{hal_code if hal_code else "(not available)"}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Create register dump utility and formatting.

OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/diagnostics/register_dump.rs
- Output MUST be raw, compile-ready Rust MODULE code only (no markdown fences, no prose).
- This file is a Rust MODULE (not crate root). DO NOT emit crate attributes.
- Do not define main().
- Do not use heap allocations.
- Reuse HAL read helpers when available.
- Provide module functions callable from firmware code.
- If information is missing, assumptions only as Rust comments at top:
  // ASSUMPTION: ...
"""
    out = llm_chat(
        prompt,
        system="You are a senior embedded firmware engineer. Output MUST be raw, compile-ready Rust only. NEVER include markdown fences.",
    )
    if not out:
        logger.warning("%s LLM returned empty output; using deterministic fallback", AGENT_NAME)
        out = _deterministic_dump(regmap)

    out = strip_markdown_fences_for_code(out).strip() + "\n"

    if "use crate::hal::registers::*;" not in out:
        logger.warning("%s output missing HAL import; using deterministic fallback", AGENT_NAME)
        out = _deterministic_dump(regmap)

    if any(
        marker in out
        for marker in (
            "```",
            "#![no_std]",
            "#![no_main]",
            "Vec<",
            "String",
            "print!",
            "println!",
            "format!",
            "std::",
            "alloc::",
        )
    ):
        logger.warning("%s output contained invalid module patterns; using deterministic fallback", AGENT_NAME)
        out = _deterministic_dump(regmap)
    if "dump_registers" not in out:
        logger.warning("%s output missing dump_registers; using deterministic fallback", AGENT_NAME)
        out = _deterministic_dump(regmap)

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))
    all_regs = _flatten_registers(regmap)

    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "regmap_present": bool(regmap),
                "hal_present": bool(hal_code),
                "register_count": len(all_regs),
                "register_names": [reg.get("name") for reg in all_regs if isinstance(reg, dict)],
            },
            indent=2,
        ),
        key=os.path.basename(DEBUG_PATH),
    )
    
    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps({"agent": AGENT_NAME, "phase": PHASE, "register_dump_path": OUTPUT_PATH}, indent=2),
        key=os.path.basename(SUMMARY_PATH),
    )

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    diag = state.setdefault("firmware_diagnostics", {})
    diag["register_dump_path"] = OUTPUT_PATH

    manifest = dict(manifest or {})
    manifest["register_dump_path"] = OUTPUT_PATH
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH

    state["status"] = f"✅ {AGENT_NAME} done"
    return state
