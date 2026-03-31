
import json
import logging
import os
from typing import Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Power Mode Configuration Agent"
PHASE = "power_modes"
OUTPUT_PATH = "firmware/power/power_modes.md"
DEBUG_PATH = "firmware/power/power_modes_debug.json"
SUMMARY_PATH = "firmware/power/power_modes_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed to load %s: %s", AGENT_NAME, path, exc)
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


def _build_markdown(has_power_modes: bool, has_pll: bool) -> str:
    if not has_power_modes:
        mode_rows = [
            "| Active | default clocks only | " + ("programmable PLL not exposed" if not has_pll else "PLL use not required") + " | all firmware-visible peripherals | implementation-defined | reset / external events | none | none | No firmware-programmable power modes exposed in current hardware contract |"
        ]
        assumptions = [
            "- ASSUMPTION: Power management is handled externally or fixed by hardware integration.",
            "- ASSUMPTION: Firmware should remain in Active mode for bring-up and co-sim.",
        ]
    else:
        mode_rows = [
            "| Active | on | optional | all enabled | implementation-defined | reset / configured wake sources | confirm wake sources and save context if needed | restore clocks and peripheral context | Default operating mode |",
            "| Sleep | gated where allowed | optional | selected peripherals only | implementation-defined | configured wake sources | quiesce DMA/peripherals, mask unsafe interrupts, request sleep | restore clocks, unmask interrupts, revalidate status | Only enter after boot-ready checkpoint |",
        ]
        assumptions = [
            "- ASSUMPTION: Exact retention and wake-source registers are target-specific and must be integrated later.",
        ]

    lines = [
        "| Mode | Clocks | PLL | Peripherals | SRAM retention | Wake sources | Entry steps | Exit steps | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        *mode_rows,
        "",
        "## Do / Don't",
        "- Do keep boot-time validation in Active mode until bring-up checkpoints pass.",
        "- Do confirm wake sources before any low-power transition.",
        "- Do quiesce in-flight peripheral activity before entering reduced-power states.",
        "- Don't assume PLL control exists unless the firmware manifest says so.",
        "- Don't enter low-power states during early co-sim bring-up unless explicitly validated.",
        "",
        "## Assumptions",
        *assumptions,
        "",
    ]
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    features = manifest.get("hardware_features") or {}
    has_power_modes = bool(features.get("has_programmable_power_modes"))
    has_pll = bool(features.get("has_programmable_pll"))

    out = _build_markdown(has_power_modes, has_pll)

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "has_programmable_power_modes": has_power_modes,
                "has_programmable_pll": has_pll,
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
                "power_modes_path": OUTPUT_PATH,
                "has_programmable_power_modes": has_power_modes,
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    manifest = dict(manifest or {})
    manifest["power_modes_path"] = OUTPUT_PATH
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    firmware_power = state.setdefault("firmware_power", {})
    firmware_power["power_modes_path"] = OUTPUT_PATH

    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state
