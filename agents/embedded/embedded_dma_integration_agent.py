
import json
import logging
import os
from typing import Optional

from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded DMA Integration Agent"
PHASE = "dma_integrate"
OUTPUT_PATH = "firmware/dma/dma.rs"
DEBUG_PATH = "firmware/dma/dma_debug.json"
SUMMARY_PATH = "firmware/dma/dma_summary.json"
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
    regmap_obj = state.get("firmware_register_map") or (state.get("firmware") or {}).get("register_map")
    if isinstance(regmap_obj, dict):
        return regmap_obj
    regmap_path = state.get("firmware_register_map_path") or manifest.get("register_map_path") or "firmware/register_map.json"
    if regmap_path and not os.path.isabs(regmap_path):
        regmap_path = os.path.join(workflow_dir, regmap_path)
    loaded = _safe_load_json(regmap_path)
    return loaded if isinstance(loaded, dict) else {}


def _infer_has_dma(manifest: dict, regmap: dict) -> bool:
    feature_flag = manifest.get("hardware_features", {}).get("has_dma")
    if isinstance(feature_flag, bool):
        return feature_flag
    for reg in regmap.get("registers") or []:
        reg_name = str(reg.get("name") or "").upper()
        if "DMA" in reg_name:
            return True
        for field in reg.get("fields") or []:
            if "DMA" in str(field.get("name") or "").upper():
                return True
    return False


def _stub_no_dma() -> str:
    return """// ASSUMPTION: No firmware-visible DMA engine is present in the current hardware contract.\n\n#[derive(Debug, Clone, Copy, PartialEq, Eq)]\npub enum DmaStatus {\n    Unsupported,\n}\n\n#[inline]\npub fn dma_supported() -> bool {\n    false\n}\n\n#[inline]\npub fn dma_status() -> DmaStatus {\n    DmaStatus::Unsupported\n}\n"""


def _generic_dma_module() -> str:
    return """// ASSUMPTION: Firmware-visible DMA capability is declared by the hardware contract.\n// ASSUMPTION: This scaffold intentionally stays generic until concrete DMA channel/register metadata exists.\n\n#[derive(Debug, Clone, Copy, PartialEq, Eq)]\npub struct DmaChannel(pub u32);\n\n#[derive(Debug, Clone, Copy, PartialEq, Eq)]\npub enum DmaStatus {\n    Ready,\n    Busy,\n}\n\n#[inline]\npub fn dma_supported() -> bool {\n    true\n}\n\n#[inline]\npub fn channel_supported(_channel: DmaChannel) -> bool {\n    true\n}\n\n#[inline]\npub fn dma_status() -> DmaStatus {\n    DmaStatus::Ready\n}\n"""


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    regmap_obj = _load_regmap(state, workflow_dir, manifest)

    has_dma = _infer_has_dma(manifest, regmap_obj)
    out = _generic_dma_module() if has_dma else _stub_no_dma()
    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))

    summary = {
        "agent": AGENT_NAME,
        "phase": PHASE,
        "dma_path": OUTPUT_PATH,
        "has_dma": has_dma,
        "mode": "generic_dma_stub" if has_dma else "no_dma_stub",
    }
    write_artifact(state, SUMMARY_PATH, json.dumps(summary, indent=2), key=os.path.basename(SUMMARY_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "register_count": len(regmap_obj.get("registers") or []),
                "has_dma": has_dma,
                "interrupt_sources": manifest.get("interrupt_model", {}).get("sources", []),
            },
            indent=2,
        ),
        key=os.path.basename(DEBUG_PATH),
    )

    manifest = dict(manifest or {})
    hw = dict(manifest.get("hardware_features") or {})
    hw["has_dma"] = has_dma
    manifest["hardware_features"] = hw
    manifest["dma_path"] = OUTPUT_PATH
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH
    firmware = state.setdefault("firmware", {})
    firmware["manifest"] = manifest
    firmware["manifest_path"] = MANIFEST_PATH
    firmware["dma_path"] = OUTPUT_PATH

    state["status"] = f"✅ {AGENT_NAME} done"
    return state
