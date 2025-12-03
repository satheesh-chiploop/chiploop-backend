import os
import json
from datetime import datetime
from openai import OpenAI
from portkey_ai import Portkey

USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def run_agent(state: dict) -> dict:
    print("\n💻 Running Embedded Code Agent (JSON-driven)…")

    spec_path = state.get("spec_file")
    if not spec_path or not os.path.exists(spec_path):
        state["status"] = "❌ Missing spec file for code generation"
        print("❌ Missing spec file for code generation")
        return state

    with open(spec_path, "r", encoding="utf-8") as f:
        try:
            spec = json.load(f)
        except Exception as e:
            # If it's somehow not JSON, fall back to old behavior: raw text passthrough.
            f.seek(0)
            spec_text = f.read()
            spec = {"_raw_text": spec_text}
            print(f"⚠️ Spec file is not valid JSON, using raw text. Error: {e}")

    workflow_dir = os.path.dirname(spec_path)
    os.makedirs(workflow_dir, exist_ok=True)
    log_path = os.path.join(workflow_dir, "embedded_code_agent.log")

    # Build a clean summary for the LLM
    if "_raw_text" in spec:
        # Legacy mode
        spec_summary = spec["_raw_text"]
    else:
        spec_summary = {
            "firmware_name": spec.get("firmware_name", "firmware"),
            "description": spec.get("description", ""),
            "inputs_from_digital": spec.get("inputs_from_digital", []),
            "outputs_to_digital": spec.get("outputs_to_digital", []),
            "control_logic": spec.get("control_logic", ""),
            "polling_interval_ms": spec.get("polling_interval_ms", 100),
            "logging": spec.get("logging", {}),
        }

    prompt = f"""
You are an embedded C firmware engineer.

Generate clean, compilable C code based on this firmware specification.
Target a generic bare-metal microcontroller (no OS). You may assume a simple
`read_signal()` and `write_signal()` abstraction for interaction with digital logic.

FIRMWARE SPECIFICATION (JSON):
{json.dumps(spec_summary, indent=2)}

REQUIREMENTS:
- Output ONLY C source code (no markdown, no ``` fences, no explanations).
- Include:
  - any necessary #include lines
  - a clear representation of inputs_from_digital as functions or stub reads
  - a main() function with an infinite loop
  - logic that implements the described control_logic
  - logging via printf or a similar function when logging.enabled is true
- If polling_interval_ms is provided, approximate it using a placeholder delay function.
- Do NOT reference Arduino or STM32-specific APIs; keep it generic C.
- Do NOT add comments about the spec or instructions; only code comments are allowed.
"""

    code = None

    try:
        if USE_LOCAL_OLLAMA:
            import requests

            payload = {"model": "llama3", "prompt": prompt}
            response = requests.post(OLLAMA_URL, json=payload, timeout=600)
            # Some local models may wrap in metadata; we assume raw text here.
            code_raw = response.json().get("response", "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="@chiploop/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            code_raw = completion.choices[0].message.content.strip()

        # Strip accidental fences
        text = code_raw.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].strip() if len(parts) >= 2 else text

        code = text

        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ✅ Embedded Code Agent executed successfully.\n")
            log.write(f"Spec used: {spec_path}\n")

    except Exception as e:
        # Deterministic minimal fallback C
        polling = spec_summary.get("polling_interval_ms", 100) if isinstance(spec_summary, dict) else 100
        code = f"""#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>

// Fallback firmware generated due to LLM error.
// Replace read_overheat_flag() with real hardware access.

bool read_overheat_flag(void) {{
    // TODO: connect to real hardware input
    static int counter = 0;
    counter++;
    return (counter % 10) == 0; // fake event every 10 iterations
}}

void delay_ms(unsigned int ms) {{
    // TODO: platform-specific delay
    (void)ms;
}}

int main(void) {{
    while (1) {{
        bool overheat = read_overheat_flag();
        if (overheat) {{
            printf("WARNING: Overheat detected!\\n");
        }}
        delay_ms({polling});
    }}
    return 0;
}}
"""
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ⚠️ LLM failed. Fallback code used: {e}\n")

    code_path = os.path.join(workflow_dir, "main.c")
    with open(code_path, "w", encoding="utf-8") as f:
        f.write(code)

    state.update(
        {
            "status": "✅ Embedded Code Generated",
            "code_file": code_path,
        }
    )
    print(f"✅ Embedded firmware C code saved to {code_path}")
    return state
