import os
import json
from datetime import datetime
from typing import Any, Dict, Optional

from portkey_ai import Portkey
from openai import OpenAI

from utils.artifact_utils import save_text_artifact_and_record

USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _pick_embedded_spec_path(state: dict) -> Optional[str]:
    """
    Embedded Spec Agent has varied keys over iterations.
    Try a few common ones and return the first path that exists.
    """
    candidates = [
        state.get("embedded_spec_json"),
        state.get("embedded_spec_path"),
        state.get("spec_file"),
        state.get("embedded_spec_file"),
        state.get("spec_json"),  # sometimes misused; keep as last resort
    ]
    for p in candidates:
        if isinstance(p, str) and p and os.path.exists(p):
            return p
    return None


def _safe_json_load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        with open(path, "r", encoding="utf-8") as f:
            return {"_raw_text": f.read()}


def run_agent(state: dict) -> dict:
    print("\n💻 Running Embedded Code Agent (C++17)…")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    embedded_dir = os.path.join(workflow_dir, "embedded")
    os.makedirs(embedded_dir, exist_ok=True)

    log_path = os.path.join(embedded_dir, "embedded_code_agent.log")
    code_path = os.path.join(embedded_dir, "main.cpp")

    spec_path = _pick_embedded_spec_path(state)
    if not spec_path:
        msg = "❌ Missing embedded spec file for code generation"
        print(msg)
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"[{datetime.now()}] {msg}\n")
                log.write(f"State keys present: {list(state.keys())}\n")
        except Exception:
            pass

        # Upload log even on failure
        try:
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8") as f:
                    save_text_artifact_and_record(
                        workflow_id=workflow_id,
                        agent_name="Embedded Code Agent",
                        subdir="embedded",
                        filename="embedded_code_agent.log",
                        content=f.read(),
                    )
        except Exception as e:
            print(f"⚠️ Failed to upload embedded code log artifact: {e}")

        state.update({"status": msg, "artifact_log": log_path})
        return state

    spec = _safe_json_load(spec_path)

    # Build a compact summary for the LLM
    if "_raw_text" in spec:
        spec_summary = spec["_raw_text"]
        control_period = 100
    else:
        timing = spec.get("timing", {}) if isinstance(spec.get("timing"), dict) else {}
        control_period = int(timing.get("control_period_ms", 100) or 100)
        spec_summary = {
            "firmware_name": spec.get("firmware_name", "embedded_control_loop"),
            "description": spec.get("description", ""),
            "target": spec.get("target", {}),
            "timing": spec.get("timing", {"control_period_ms": control_period}),
            "logging": spec.get("logging", {}),
            "signals": spec.get("signals", {}),
            "behavior": spec.get("behavior", {}),
        }

    prompt = f"""
You are a senior embedded firmware engineer.

Generate clean, compilable C++17 code for a GENERIC bare-metal firmware control loop.
The code must implement the behavior described in the firmware specification.

FIRMWARE SPEC (JSON or TEXT):
{json.dumps(spec_summary, indent=2) if isinstance(spec_summary, dict) else spec_summary}

MANDATORY REQUIREMENTS:
- Output ONLY C++ source code (no markdown, no ``` fences, no explanations).
- Produce a single file main.cpp.
- Provide a minimal HAL layer as stubs:
  - bool hal_read_bool(const char* name);
  - uint32_t hal_read_u32(const char* name);
  - void hal_write_bool(const char* name, bool v);
  - void hal_write_u32(const char* name, uint32_t v);
  - void hal_delay_ms(uint32_t ms);
  - void log_printf(const char* fmt, ...);
- Use the spec timing.control_period_ms for loop delay.
- Use spec.signals.inputs_from_digital and outputs_to_digital names as-is (do NOT rename signals).
- On startup, drive outputs_to_digital to their default_value if provided, else 0.
- Implement spec.behavior.rules in a straightforward, deterministic way:
  - 'when' conditions based on available boolean/uint signals
  - 'then' actions for logging and setting outputs
- Keep it generic: no vendor SDKs, no Arduino, no STM32 headers.

QUALITY EXPECTATIONS:
- Clear structure (init, loop, helper functions)
- Type-safe wrappers for signals where possible
- No hardcoded domain names like "overheat_flag" unless present in spec
""".strip()

    code: Optional[str] = None
    llm_error: Optional[str] = None

    try:
        if USE_LOCAL_OLLAMA:
            import requests
            resp = requests.post(OLLAMA_URL, json={"model": "llama3", "prompt": prompt}, timeout=600)
            resp.raise_for_status()
            code_raw = (resp.json().get("response") or "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="@chiploop/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=False,
            )
            code_raw = (completion.choices[0].message.content or "").strip()

        text = code_raw.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].strip() if len(parts) >= 2 else text
        code = text

        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ✅ Embedded Code Agent executed successfully.\n")
            log.write(f"Spec used: {spec_path}\n")

    except Exception as e:
        llm_error = str(e)

    # Robust generic fallback (NO domain-specific signal names)
    if not code:
        period = 100
        inputs = []
        outputs = []
        try:
            if isinstance(spec_summary, dict):
                period = int(spec_summary.get("timing", {}).get("control_period_ms", 100) or 100)
                signals = spec_summary.get("signals", {}) if isinstance(spec_summary.get("signals"), dict) else {}
                inputs = signals.get("inputs_from_digital", []) if isinstance(signals.get("inputs_from_digital"), list) else []
                outputs = signals.get("outputs_to_digital", []) if isinstance(signals.get("outputs_to_digital"), list) else []
        except Exception:
            pass

        # Pick a boolean input if any, else generic placeholder
        bool_inputs = [s for s in inputs if isinstance(s, dict) and s.get("type") == "bool" and s.get("name")]
        sample_bool = bool_inputs[0]["name"] if bool_inputs else "status_flag"

        code = f"""#include <cstdint>
#include <cstdarg>
#include <cstdio>
#include <cstring>

// --------------------
// Minimal HAL stubs
// --------------------
static bool hal_read_bool(const char* name) {{
    (void)name;
    return false;
}}

static uint32_t hal_read_u32(const char* name) {{
    (void)name;
    return 0u;
}}

static void hal_write_bool(const char* name, bool v) {{
    (void)name; (void)v;
}}

static void hal_write_u32(const char* name, uint32_t v) {{
    (void)name; (void)v;
}}

static void hal_delay_ms(uint32_t ms) {{
    (void)ms;
}}

static void log_printf(const char* fmt, ...) {{
    va_list args;
    va_start(args, fmt);
    (void)vprintf(fmt, args);
    va_end(args);
}}

// --------------------
// Firmware logic
// --------------------
static void firmware_init() {{
    // Drive safe defaults (generic)
    // (If your spec provides outputs_to_digital, the LLM path sets them explicitly)
}}

static void firmware_step() {{
    // Generic example: read one status flag and log when asserted
    if (hal_read_bool("{sample_bool}")) {{
        log_printf("[INFO] {sample_bool} asserted\\n");
    }}
}}

int main() {{
    firmware_init();
    while (true) {{
        firmware_step();
        hal_delay_ms({period}u);
    }}
    return 0;
}}
"""
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ⚠️ LLM failed. Fallback C++ used.\n")
            if llm_error:
                log.write(f"Error: {llm_error}\n")

    # Write code file
    with open(code_path, "w", encoding="utf-8") as f:
        f.write(code)

    # Upload artifacts to Supabase storage + record
    try:
        agent_name = "Embedded Code Agent"

        with open(code_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="embedded",
                filename="main.cpp",
                content=f.read(),
            )

        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir="embedded",
                    filename="embedded_code_agent.log",
                    content=f.read(),
                )

        print("🧩 Embedded Code Agent artifacts uploaded successfully.")
    except Exception as e:
        print(f"⚠️ Embedded Code Agent artifact upload failed: {e}")

    state.update(
        {
            "status": "✅ Embedded C++ Code Generated",
            "code_file": code_path,
            "artifact_log": log_path,
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
        }
    )

    print(f"✅ Embedded firmware C++ code saved to {code_path}")
    return state
