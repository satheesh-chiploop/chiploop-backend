
import json
import logging
import os
import re
from typing import Optional

from ._embedded_common import ensure_workflow_dir, llm_chat, strip_markdown_fences_for_code, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Stress Test Generator Agent"
PHASE = "stress"
OUTPUT_PATH = "firmware/diagnostics/stress_tests.rs"
DEBUG_PATH = "firmware/diagnostics/stress_tests_debug.json"
SUMMARY_PATH = "firmware/diagnostics/stress_tests_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed loading %s: %s", AGENT_NAME, path, exc)
    return None


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


def _deterministic_register_exercise(manifest: dict, regmap: dict) -> str:
    driver_name = manifest.get("driver_name") or "DigitalSubsystemDriver"
    regs = [reg for reg in (regmap.get("registers") or []) if isinstance(reg, dict)]
    readable = [reg for reg in regs if str(reg.get("access") or "RW").upper() in {"RO", "RW", "RW1C"}]

    lines = [
        "use crate::drivers::driver_scaffold::*;",
        "",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
        "pub struct StressSummary {",
        "    pub iterations: u32,",
        "    pub reads: u32,",
        "    pub writes: u32,",
        "}",
        "",
        f"pub fn run_register_exercise(driver: &{driver_name}, iterations: u32) -> StressSummary {{",
        "    let mut summary = StressSummary { iterations, reads: 0, writes: 0 };",
        "    let mut i = 0u32;",
        "    while i < iterations {",
    ]

    for reg in readable[:3]:
        reg_ident = _safe_identifier(reg.get("name") or "UNNAMED")
        lines.extend([
            f"        let _ = driver.read_{reg_ident}();",
            "        summary.reads += 1;",
        ])

    lines.extend([
        "        i += 1;",
        "    }",
        "    summary",
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

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

FIRMWARE MANIFEST:
{json.dumps(manifest, indent=2)}

REGISTER MAP:
{json.dumps(regmap, indent=2)}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Create bounded, register-safe firmware exercise routines using only features explicitly present in the firmware manifest and register map.

OUTPUT REQUIREMENTS:
- Output MUST be RAW RUST ONLY.
- This file is a Rust MODULE (not crate root).
- DO NOT emit crate attributes or heap allocations.
- Reuse the existing driver layer and manifest hardware features.
- Do not invent DMA, IRQ, PLL, reset, memory-controller, or power-mode behavior if unsupported by manifest.
- Prefer bounded read-based exercise over raw full-register writes unless the manifest and register map clearly prove the write path is safe.
- If no safe writable path is provable, generate read-only exercise routines.
- Assumptions allowed only as:
  // ASSUMPTION: ...
- Write output to:
  firmware/diagnostics/stress_tests.rs
"""
    out = llm_chat(
        prompt,
        system="You are a senior embedded firmware engineer. Output MUST be raw, compile-ready Rust only. NEVER include markdown fences.",
    )
    if not out:
        logger.warning("%s LLM returned empty output; using deterministic fallback", AGENT_NAME)
        out = _deterministic_register_exercise(manifest, regmap)

    out = strip_markdown_fences_for_code(out).replace("#![no_std]", "// no_std configured at crate root").strip() + "\n"
    if any(marker in out for marker in ("```", "#![feature(", "Vec<", "String", "std::")):
        logger.warning("%s output contained invalid patterns; using deterministic fallback", AGENT_NAME)
        out = _deterministic_register_exercise(manifest, regmap)
    if "run_register_exercise" not in out and "run_stress" not in out:
        logger.warning("%s output missing exercise entrypoint; using deterministic fallback", AGENT_NAME)
        out = _deterministic_register_exercise(manifest, regmap)

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "regmap_present": bool(regmap),
                "has_dma": bool((manifest.get("hardware_features") or {}).get("has_dma")),
                "has_programmable_pll": bool((manifest.get("hardware_features") or {}).get("has_programmable_pll")),
                "has_programmable_power_modes": bool((manifest.get("hardware_features") or {}).get("has_programmable_power_modes")),
            },
            indent=2,
        ),
        key=os.path.basename(DEBUG_PATH),
    )
    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps({"agent": AGENT_NAME, "phase": PHASE, "stress_path": OUTPUT_PATH}, indent=2),
        key=os.path.basename(SUMMARY_PATH),
    )

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    diag = state.setdefault("firmware_diagnostics", {})
    diag["stress_tests_path"] = OUTPUT_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state
