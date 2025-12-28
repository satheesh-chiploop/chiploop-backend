import os
import json
from portkey_ai import Portkey
from openai import OpenAI

from utils.artifact_utils import save_text_artifact_and_record


PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _read_json_if_exists(path: str):
    if isinstance(path, str) and path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _safe_dump(obj) -> str:
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return str(obj)


def run_agent(state: dict) -> dict:
    print("\n🧩 Running Digital Microarchitecture Agent...")

    agent_name = "Digital Microarchitecture Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = (state.get("spec", "") or "").strip()
    spec_obj = _read_json_if_exists(state.get("spec_json"))
    arch_obj = _read_json_if_exists(state.get("digital_architecture_json"))

    prompt = f"""
You are a senior micro-architecture designer for synthesizable digital IP.

INPUTS:
- USER_REQUEST (may be empty): {user_prompt}
- SPEC_JSON (may be null):
{_safe_dump(spec_obj)}
- ARCHITECTURE_JSON (may be null):
{_safe_dump(arch_obj)}

OUTPUT RULES (CRITICAL):
- DO NOT use markdown.
- Output ONLY a single raw JSON object. No extra text.
- JSON must be valid (parseable by json.loads).
- No comments in JSON.

TASK:
Produce a concrete micro-architecture plan: pipelines/FSMs/queues/arbitration,
timing/latency/throughput expectations, and implementation-ready design intent.

Output schema:
{{
  "design_name":"string",
  "microarchitecture_summary":"string",
  "clocking_intent": {{
    "clock_domains":[{{"domain":"...", "clk":"...", "notes":"..."}}],
    "reset_intent":"..."
  }},
  "pipeline": {{
    "stages":[
      {{"name":"S0", "purpose":"...", "latency_cycles":1, "key_ops":["..."], "inputs":["..."], "outputs":["..."]}}
    ],
    "total_latency_cycles":"string",
    "throughput":"string"
  }},
  "control": {{
    "f sms":[
      {{"name":"fsm_name", "states":["IDLE","..."], "transitions":["..."], "notes":"..."}}
    ],
    "arbitration":[
      {{"name":"arb_name", "policy":"rr|fixed|priority", "requesters":["..."], "grants":["..."]}}
    ]
  }},
  "storage": {{
    "registers":["..."],
    "fifos":[{{"name":"fifo0","depth":4,"width":32,"notes":"..."}}],
    "memories":[{{"name":"mem0","type":"sram|ram|rom","depth":256,"width":32,"notes":"..."}}]
  }},
  "interfaces_intent": {{
    "internal_signals":["..."],
    "handshakes":[{{"name":"ready/valid", "signals":["..."], "notes":"..."}}]
  }},
  "timing_notes":[
    "CDC considerations (intent only, no implementation)",
    "critical paths likely to watch"
  ],
  "verification_targets": {{
    "corner_cases":["..."],
    "assertions":["..."],
    "coverage_points":["..."]
  }}
}}
""".strip()

    try:
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
    except Exception as e:
        state["status"] = f"❌ Microarchitecture LLM generation failed: {e}"
        return state

    raw_path = os.path.join(workflow_dir, "digital_microarchitecture_raw_output.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    micro = None
    try:
        micro = json.loads(llm_output.strip())
    except Exception as e:
        micro = {"error": "LLM JSON parse failed", "parse_error": str(e), "raw": llm_output.strip()}

    out_path = os.path.join(workflow_dir, "digital_microarchitecture.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(micro, f, indent=2)

    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_microarchitecture_raw_output.txt",
            content=open(raw_path, "r", encoding="utf-8").read(),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_microarchitecture.json",
            content=open(out_path, "r", encoding="utf-8").read(),
        )
    except Exception as e:
        print(f"⚠️ Failed to upload microarchitecture artifacts: {e}")

    state.update({
        "status": "✅ Digital microarchitecture generated.",
        "digital_microarchitecture_json": out_path,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })
    return state
