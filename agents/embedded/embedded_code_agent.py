import os
import json
from datetime import datetime
from openai import OpenAI
from portkey_ai import Portkey

from utils.artifact_utils import save_text_artifact_and_record

USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _pick_embedded_spec_path(state: dict) -> str | None:
    """
    Embedded Spec Agent has varied keys over iterations.
    Try a few common ones and return the first path that exists.
    """
    candidates = [
        state.get("embedded_spec_json"),
        state.get("embedded_spec_file"),
        state.get("spec_file"),
        state.get("spec_json"),
        state.get("code_spec_json"),
    ]
    for p in candidates:
        if isinstance(p, str) and p and os.path.exists(p):
            return p
    return None


def run_agent(state: dict) -> dict:
    print("\n💻 Running Embedded Code Agent (JSON-driven)…")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    embedded_dir = os.path.join(workflow_dir, "embedded")
    os.makedirs(embedded_dir, exist_ok=True)

    log_path = os.path.join(embedded_dir, "embedded_code_agent.log")
    code_path = os.path.join(embedded_dir, "main.c")

    spec_path = _pick_embedded_spec_path(state)

    if not spec_path:
        msg = "❌ Missing embedded spec file for code generation"
        print(msg)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] {msg}\n")
            log.write(f"State keys present: {list(state.keys())}\n")

        # Upload log even on failure (helps debug in UI)
        try:
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

    # Persist for compatibility so downstream agents can rely on it
    state["spec_file"] = spec_path

    # Load spec JSON (fallback to raw text if needed)
    with open(spec_path, "r", encoding="utf-8") as f:
        try:
            spec = json.load(f)
        except Exception as e:
            f.seek(0)
            spec = {"_raw_text": f.read()}
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"[{datetime.now()}] ⚠️ Spec not valid JSON, using raw text. Error: {e}\n")

    # Build a clean summary for the LLM
    if "_raw_text" in spec:
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
Target a generic bare-metal microcontroller (no OS). You may assume simple stubs
like read_signal_u32(name) / read_signal_bool(name) for reading from digital logic.

FIRMWARE SPECIFICATION (JSON or TEXT):
{json.dumps(spec_summary, indent=2) if isinstance(spec_summary, dict) else spec_summary}

REQUIREMENTS:
- Output ONLY C source code (no markdown, no ``` fences, no explanations).
- Include: #include lines, helper stubs, and a main() loop.
- Implement the described control_logic.
- If polling_interval_ms exists, use a delay_ms(polling_interval_ms) stub.
- Keep it generic C (no Arduino/STM32-specific APIs).
""".strip()

    code = None
    llm_error = None

    try:
        if USE_LOCAL_OLLAMA:
            import requests
            resp = requests.post(
                OLLAMA_URL,
                json={"model": "llama3", "prompt": prompt},
                timeout=600,
            )
            resp.raise_for_status()
            code_raw = (resp.json().get("response") or "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="@chiploop/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            code_raw = (completion.choices[0].message.content or "").strip()

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
        llm_error = str(e)

    if not code:
        polling = 100
        if isinstance(spec_summary, dict):
            polling = int(spec_summary.get("polling_interval_ms", 100) or 100)

        code = f"""#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>

static uint32_t read_signal_u32(const char *name) {{
    (void)name;
    return 0;
}}

static bool read_signal_bool(const char *name) {{
    (void)name;
    static int counter = 0;
    counter++;
    return (counter % 10) == 0;
}}

static void delay_ms(unsigned int ms) {{
    (void)ms;
}}

int main(void) {{
    while (1) {{
        bool overheat_flag = read_signal_bool("overheat_flag");
        if (overheat_flag) {{
            printf("WARNING: Overheat detected!\\n");
        }}
        delay_ms({polling});
    }}
    return 0;
}}
"""
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ⚠️ LLM failed. Fallback code used.\n")
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
                filename="main.c",
                content=f.read(),
            )

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
            "status": "✅ Embedded Code Generated",
            "code_file": code_path,
            "artifact_log": log_path,
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
        }
    )

    print(f"✅ Embedded firmware C code saved to {code_path}")
    return state
