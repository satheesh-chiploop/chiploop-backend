
import json
import logging
import os
import re
from typing import Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Interrupt Mapping Agent"
PHASE = "irq_map"
OUTPUT_PATH = "firmware/isr/interrupts.rs"
JSON_OUTPUT_PATH = "firmware/isr/interrupt_map.json"
DEBUG_PATH = "firmware/isr/interrupts_debug.json"
SUMMARY_PATH = "firmware/isr/interrupts_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed loading %s: %s", AGENT_NAME, path, exc)
    return None


def _safe_identifier(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not ident:
        ident = "unnamed"
    if ident[0].isdigit():
        ident = f"irq_{ident}"
    return ident.lower()


def _safe_const_name(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not ident:
        ident = "UNNAMED"
    if ident[0].isdigit():
        ident = f"IRQ_{ident}"
    return ident.upper()


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


def _infer_sources(manifest: dict, regmap: dict) -> list:
    sources = manifest.get("interrupt_model", {}).get("sources") or []
    if isinstance(sources, list) and sources:
        return [str(s) for s in sources if s]

    out = []
    keywords = {"IRQ", "INT", "INTR", "INTERRUPT", "DONE", "FAULT", "ERROR", "READY", "PENDING"}
    for reg in regmap.get("registers") or []:
        for field in reg.get("fields") or []:
            name = str(field.get("name") or "")
            if any(k in name.upper() for k in keywords):
                out.append(name)

    ordered = []
    for src in out:
        if src and src not in ordered:
            ordered.append(src)
    return ordered


def _build_map_json(manifest: dict, sources: list) -> dict:
    return {
        "agent": AGENT_NAME,
        "phase": PHASE,
        "top_irq_signal": manifest.get("interrupt_model", {}).get("top_irq_signal") or "irq",
        "sources": [
            {
                "name": src,
                "const_name": _safe_const_name(src),
                "handler_name": f"handle_{_safe_identifier(src)}",
                "bit_index": idx,
            }
            for idx, src in enumerate(sources)
        ],
    }


def _render_rust(interrupt_map: dict) -> str:
    sources = interrupt_map.get("sources") or []
    lines = [
        "// ASSUMPTION: Interrupt decode is software-mediated using firmware-visible status/interrupt bits.",
        "// ASSUMPTION: No MCU-style external vector table is generated unless the hardware contract explicitly requires one.",
        "",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
        "pub enum InterruptSource {",
    ]
    if sources:
        for src in sources:
            lines.append(f"    {_safe_const_name(src['name']).title().replace('_', '')},")
    else:
        lines.append("    NoneDeclared,")
    lines += [
        "}",
        "",
        "#[inline]",
        "pub fn interrupt_count() -> usize {",
        f"    {len(sources)}",
        "}",
        "",
    ]

    for src in sources:
        lines += [
            f"pub const {src['const_name']}_BIT: u32 = {src['bit_index']};",
        ]
    if sources:
        lines.append("")

    for src in sources:
        handler = src["handler_name"]
        lines += [
            "#[inline]",
            f"pub fn {handler}() {{",
            "    // Default ISR scaffold: replace with concrete firmware behavior as needed.",
            "}",
            "",
        ]

    lines += [
        "#[inline]",
        "pub fn dispatch_interrupt(bit_index: u32) -> bool {",
        "    match bit_index {",
    ]
    for src in sources:
        lines += [
            f"        {src['const_name']}_BIT => {{ {src['handler_name']}(); true }}",
        ]
    lines += [
        "        _ => false,",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    regmap_obj = _load_regmap(state, workflow_dir, manifest)

    if not regmap_obj:
        state["status"] = "❌ firmware register map missing in state for interrupt generation"
        return state

    sources = _infer_sources(manifest, regmap_obj)
    interrupt_map = _build_map_json(manifest, sources)

    write_artifact(state, JSON_OUTPUT_PATH, json.dumps(interrupt_map, indent=2), key=os.path.basename(JSON_OUTPUT_PATH))
    write_artifact(state, OUTPUT_PATH, _render_rust(interrupt_map), key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "register_count": len(regmap_obj.get("registers") or []),
                "source_count": len(sources),
                "sources": sources,
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
                "interrupts_path": OUTPUT_PATH,
                "interrupt_map_path": JSON_OUTPUT_PATH,
                "source_count": len(sources),
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    manifest = dict(manifest or {})
    interrupt_model = dict(manifest.get("interrupt_model") or {})
    interrupt_model["sources"] = sources
    manifest["interrupt_model"] = interrupt_model
    manifest["interrupt_map_path"] = JSON_OUTPUT_PATH
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH
    firmware = state.setdefault("firmware", {})
    firmware["manifest"] = manifest
    firmware["manifest_path"] = MANIFEST_PATH
    firmware["interrupt_map_path"] = JSON_OUTPUT_PATH

    state["status"] = f"✅ {AGENT_NAME} done"
    return state
