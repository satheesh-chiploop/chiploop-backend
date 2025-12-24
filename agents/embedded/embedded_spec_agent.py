import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

# Optional LLM enhancement (kept non-blocking)
try:
    from openai import OpenAI
    from portkey_ai import Portkey
except Exception:
    OpenAI = None
    Portkey = None

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY) if Portkey and PORTKEY_API_KEY else None
client_openai = OpenAI() if OpenAI else None


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _find_existing_json_path(state: dict, candidates: List[str]) -> Optional[str]:
    for k in candidates:
        v = state.get(k)
        if isinstance(v, str) and v and os.path.exists(v) and v.lower().endswith(".json"):
            return v
    return None


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_signal_name(name: str) -> str:
    # Keep it firmware-friendly and deterministic
    n = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name.strip())
    if not n:
        return "sig"
    if n[0].isdigit():
        n = f"sig_{n}"
    return n.lower()


def _extract_signals_from_digital_spec(digital_spec: dict) -> Tuple[List[dict], List[dict]]:
    """
    Returns (fw_reads, fw_writes) from a digital spec.
    We treat:
      - digital outputs => firmware reads (STATUS)
      - digital inputs  => firmware writes (CONTROL)
    Supports a few common shapes.
    """
    ports = []
    for key in ["ports", "io", "signals", "interface_signals"]:
        if isinstance(digital_spec.get(key), list):
            ports = digital_spec[key]
            break

    fw_reads: List[dict] = []
    fw_writes: List[dict] = []

    for p in ports:
        if not isinstance(p, dict):
            continue
        name = p.get("name") or p.get("signal") or p.get("id")
        direction = (p.get("direction") or p.get("dir") or "").lower()
        width = p.get("width") or p.get("bits") or 1
        ptype = (p.get("type") or "logic").lower()
        desc = p.get("description") or p.get("desc") or ""

        if not name or direction not in ("input", "output"):
            continue

        entry = {
            "name": _normalize_signal_name(str(name)),
            "original_name": str(name),
            "direction": direction,
            "width": int(width) if str(width).isdigit() else 1,
            "type": ptype,
            "description": desc,
        }

        if direction == "output":
            fw_reads.append(entry)   # firmware reads status exported by RTL
        else:
            fw_writes.append(entry)  # firmware writes control inputs into RTL

    return fw_reads, fw_writes


def _build_default_mmio_map(fw_reads: List[dict], fw_writes: List[dict]) -> dict:
    """
    A simple industry-style baseline:
      - CONTROL0: bitfields for fw_writes (RTL inputs)
      - STATUS0 : bitfields for fw_reads  (RTL outputs)
    """
    def pack_bits(signals: List[dict], max_bits: int = 32) -> List[dict]:
        fields = []
        bit = 0
        for s in signals:
            w = int(s.get("width", 1))
            if w <= 0:
                w = 1
            if bit + w > max_bits:
                break
            fields.append({
                "name": s["name"],
                "lsb": bit,
                "msb": bit + w - 1,
                "width": w,
                "description": s.get("description", "") or "",
                "original_name": s.get("original_name", s["name"]),
            })
            bit += w
        return fields

    base = "0x40000000"
    regmap = {
        "mmio": {
            "base_address": base,
            "bus": "AXI4-Lite",
            "addr_stride_bytes": 4,
            "registers": [
                {
                    "name": "CONTROL0",
                    "offset": "0x00",
                    "access": "RW",
                    "description": "Firmware-controlled inputs into RTL (packed bitfields).",
                    "fields": pack_bits(fw_writes),
                },
                {
                    "name": "STATUS0",
                    "offset": "0x04",
                    "access": "RO",
                    "description": "RTL-exported status signals readable by firmware (packed bitfields).",
                    "fields": pack_bits(fw_reads),
                },
                {
                    "name": "IRQ_STATUS",
                    "offset": "0x08",
                    "access": "RO",
                    "description": "Optional: interrupt status (implementation-defined).",
                    "fields": [],
                },
                {
                    "name": "IRQ_MASK",
                    "offset": "0x0C",
                    "access": "RW",
                    "description": "Optional: interrupt mask/enable bits (implementation-defined).",
                    "fields": [],
                },
            ],
        }
    }
    return regmap


def _maybe_llm_polish_spec(embedded_spec: dict, log_path: str) -> dict:
    """
    Optional LLM step: improve descriptions + add usage notes.
    Non-blocking; if it fails we return the input.
    """
    if not client_openai:
        return embedded_spec

    try:
        prompt = (
            "You are generating an embedded firmware integration spec for a hardware IP.\n"
            "Refine descriptions and add concise usage notes. Do NOT rename signals or registers.\n"
            "Return valid JSON only.\n\n"
            f"INPUT_JSON:\n{json.dumps(embedded_spec, indent=2)}\n"
        )
        # Keep model flexible; this is a “polish” step, not required for correctness
        resp = client_openai.chat.completions.create(
            model=os.getenv("EMBEDDED_SPEC_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "Return JSON only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        txt = resp.choices[0].message.content.strip()
        return json.loads(txt)
    except Exception as e:
        _log(log_path, f"LLM polish step skipped/failed: {e}")
        return embedded_spec


def run_agent(state: dict) -> dict:
    print("\n🧩 Running Embedded Spec Agent (generic MMIO-driven)…")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"

    # local temp log (uploaded as artifact)
    os.makedirs("artifact", exist_ok=True)
    log_path = os.path.join("artifact", "embedded_spec_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Embedded Spec Agent Log\n")

    # Find digital spec JSON (produced by Digital Spec Agent)
    digital_spec_path = _find_existing_json_path(
        state,
        candidates=[
            "digital_spec_json_path",
            "digital_spec_path",
            "spec_json_path",
            "spec_file",
            "spec_json",
        ],
    )

    if not digital_spec_path:
        _log(log_path, "ERROR: Could not locate Digital Spec JSON on disk via state.")
        # Still upload log
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Embedded Spec Agent",
            subdir="embedded",
            filename="embedded_spec_agent.log",
            content=open(log_path, "r", encoding="utf-8").read(),
        )
        state["embedded_spec_error"] = "missing_digital_spec"
        return state

    digital_spec = _load_json(digital_spec_path)
    fw_reads, fw_writes = _extract_signals_from_digital_spec(digital_spec)

    embedded_spec: Dict[str, Any] = {
        "embedded_spec_version": "1.0",
        "source": {
            "digital_spec_path": digital_spec_path,
        },
        "assumptions": [
            "Firmware integrates with RTL through AXI4-Lite MMIO registers (packed bitfields).",
            "This spec is generic scaffolding; real products may split signals across multiple registers, add FIFOs, DMA, and IRQ controllers.",
        ],
        "firmware_reads_from_rtl": fw_reads,
        "firmware_writes_to_rtl": fw_writes,
    }

    embedded_spec.update(_build_default_mmio_map(fw_reads, fw_writes))
    embedded_spec = _maybe_llm_polish_spec(embedded_spec, log_path)

    embedded_spec_json = json.dumps(embedded_spec, indent=2)

    # Record in state (and upload)
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Embedded Spec Agent",
        subdir="embedded",
        filename="embedded_spec.json",
        content=embedded_spec_json,
    )
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Embedded Spec Agent",
        subdir="embedded",
        filename="embedded_spec_agent.log",
        content=open(log_path, "r", encoding="utf-8").read(),
    )

    state["embedded_spec_json"] = embedded_spec
    state["embedded_spec_path"] = os.path.join(workflow_dir, "embedded", "embedded_spec.json")
    return state
