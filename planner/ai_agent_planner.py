
import os
import json
import uuid
from datetime import datetime
from loguru import logger

from utils.llm_utils import run_llm_fallback
from utils.spec_analyzer import analyze_spec_text 
from utils.semantic import summarize_capability_long
from utils.semantic import compute_embedding
from utils.semantic import build_capability_signature

# Reuse the same supabase client pattern as ai_work_planner
from supabase import create_client
SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def infer_capability_tags(agent_name: str, loop_type: str):
    name = agent_name.lower()
    tags = [loop_type]  # domain tag (digital / analog / embedded)

    if "control" in name or "sequenc" in name or "fsm" in name:
        tags.append("control")
    elif "compute" in name or "alu" in name or "exec" in name:
        tags.append("compute")
    elif "decode" in name or "decoder" in name:
        tags.append("decode")
    else:
        tags.append("general")

    return tags


async def plan_agent_fallback(goal, user_id="anonymous"):
    result = await analyze_spec_text(goal, user_id=user_id)
    coverage = result.get("coverage", {})


async def fetch_user_memory(user_id: str):
    """Fetch user memory context from Supabase."""
    try:
        res = supabase.table("user_memory").select("*").eq("user_id", user_id).execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch user_memory: {e}")
    return {"recent_goals": [], "frequent_agents": [], "preferred_style": ""}


async def fetch_agent_memory():
    """Fetch known agents and capabilities."""
    try:
        res = supabase.table("agent_memory").select("agent_name, capability_tags").execute()
        return res.data or []
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch agent_memory: {e}")
    return []

# ---------- Helpers ----------
def slugify_filename(name: str) -> str:
    base = name.lower()
    base = "".join(ch if ch.isalnum() else "_" for ch in base)
    base = base.strip("_")
    return f"{base}.py" if not base.endswith(".py") else base

def infer_loop_type(structured_spec_final: dict, preplan: dict, fallback: str = "digital") -> str:
    # Order: preplan.loop_type → structured_spec_final.loop_type → fallback
    if isinstance(preplan, dict) and preplan.get("loop_type"):
        return preplan["loop_type"]
    if isinstance(structured_spec_final, dict) and structured_spec_final.get("loop_type"):
        return structured_spec_final["loop_type"]
    return fallback

def ensure_agents_folder(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

# ---------- New, deterministic generator ----------
async def plan_agent_with_memory(payload: dict) -> dict:
    """
    Generate a single, user-named agent with deterministic filename and persist it.
    Expected payload keys:
      - goal: str
      - user_id: str (uuid)
      - missing_agent: str (final, user-approved display name)
      - file_name: str (deterministic python filename; optional if name given)
      - preplan: dict | None
      - structured_spec_final: dict | None
    Returns:
      {
        "agent_name": str,
        "file_name": str,
        "script_path": str,
        "domain": str,
        "description": str,
        "capability_tags": list[str],
        "input_schema": str,
        "output_schema": str,
        "example_prompt": str,
        "full_code": str
      }
    """
    # ---- 1) Parse inputs ----
    goal = payload.get("goal", "").strip()
    user_id = payload.get("user_id") or None
    final_name = payload.get("missing_agent", "").strip()  # enforced display name
    file_name = payload.get("file_name", "").strip()
    preplan = payload.get("preplan") or {}
    structured_spec_final = payload.get("structured_spec_final") or {}

    if not final_name:
        raise ValueError("missing_agent is required")
    if not file_name:
        file_name = slugify_filename(final_name)

    loop_type = infer_loop_type(structured_spec_final, preplan, fallback="digital")
    logger.info(f"🧱 Generating agent: name='{final_name}', file='{file_name}', loop='{loop_type}'")

    # ---- 2) Build prompt (spec-aware, preplan-aware, deterministic name) ----
    system = (
        "You are ChipLoop’s Agent Code Generator. "
        "Use EXACTLY the provided agent_name. Do not rename it."
    )
    user_prompt = f"""
agent_name: {final_name}
goal: {goal}

structured_spec_final (JSON):
{json.dumps(structured_spec_final or {}, indent=2)}

preplan (JSON) - use only as context, do NOT invent new agents:
{json.dumps(preplan or {}, indent=2)}

Return VALID JSON ONLY in this exact shape (no extra text):
{{
  "agent_name": "{final_name}",
  "description": "",
  "domain": "digital | analog | embedded | system",
  "capability_tags": [],
  "input_schema": "",
  "output_schema": "",
  "example_prompt": "",
  "full_code": ""
}}
- "full_code" must be a complete Python agent module (import-safe).
- The code must implement a callable `run(inputs: dict) -> dict`.
- Respect multi-clock/multi-reset/power domains if present in structured_spec_final.
- If CDC/PDC is implied by spec, include clear TODOs or simple checks accordingly.
"""

    # ---- 3) LLM call ----
    raw = await run_llm_fallback(
        prompt=f"{system}\n\n{user_prompt}"
    )

    # ---- 4) Safe JSON parse ----
    try:
        start, end = raw.find("{"), raw.rfind("}")
        plan = json.loads(raw[start:end+1]) if start != -1 and end != -1 else {}
    except Exception as e:
        logger.error(f"❌ JSON parse failed: {e} | Raw head: {raw[:200]}")
        plan = {
            "agent_name": final_name,
            "description": raw[:200],
            "domain": loop_type if loop_type in ["digital", "analog", "embedded", "system","validation"] else "digital",
            "capability_tags": [],
            "input_schema": "",
            "output_schema": "",
            "example_prompt": "",
            "full_code": f"# Fallback stub for {final_name}\n\ndef run(inputs: dict) -> dict:\n    return {{'status': 'stub', 'inputs': inputs}}",
        }

    # ---- 5) Enforce the final name and filename ----
    plan["agent_name"] = final_name
    plan["file_name"] = file_name

    # Domain normalization (map to loop_type constraint in DB)
    domain = str(plan.get("domain") or loop_type).lower()
    if domain not in ["digital", "analog", "embedded", "system","validation"]:
        domain = loop_type
    plan["domain"] = domain

    # ---- 6) Persist code to filesystem (same convention as workflow side) ----
    # We keep parity with ai_work_planner.register_new_agent: agents/custom/{name}.py
    slug = file_name[:-3] if file_name.endswith(".py") else file_name
    script_path = f"agents/custom/{slug}.py"
    ensure_agents_folder(script_path)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(plan.get("full_code") or "")

    plan["script_path"] = script_path

    # ---- 7) Upsert into public.agents ----
    # Unique key = (owner_id, agent_name) via your index; set is_custom = true
    try:
        supabase.table("agents").upsert({
            "agent_name": final_name,
            "loop_type": domain if domain in ["digital", "analog", "embedded","system","validation"] else None,
            "tool": None,
            "script_path": script_path,
            "description": plan.get("description"),
            "is_custom": True,
            "is_marketplace": False,
            "owner_id": user_id,
            "is_prebuilt": False,
        }, on_conflict="id").execute()
    except Exception as e:
        logger.warning(f"⚠️ Upsert to agents failed (non-fatal): {e}")

    # ---- 8) Update agent_memory (optional but nice) ----
    try:
        supabase.table("agent_memory").upsert({
            "agent_name": final_name,
            "capability_tags": plan.get("capability_tags") or [],
            "last_used_in": [goal] if goal else [],
            "version": "v1",
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.warning(f"⚠️ Upsert to agent_memory failed (non-fatal): {e}")

    logger.success(f"✅ Generated & saved agent: {final_name} → {script_path}")
    return {
        "agent_name": plan["agent_name"],
        "file_name": plan["file_name"],
        "script_path": plan["script_path"],
        "domain": plan["domain"],
        "description": plan.get("description", ""),
        "capability_tags": plan.get("capability_tags", []),
        "input_schema": plan.get("input_schema", ""),
        "output_schema": plan.get("output_schema", ""),
        "example_prompt": plan.get("example_prompt", ""),
        "full_code": plan.get("full_code", ""),
    }

from agents.agent_generator import generate_behavioral_agent
# --- NEW: LLM-based detection + batch behavioral generator ---

async def llm_detect_missing_behavioral_agents(structured_spec_final: dict) -> dict:
    """
    Ask the LLM whether a separate behavioral/control agent is needed.
    No hard-coded keywords. Pure reasoning on spec.
    Returns JSON:
      { "controller_required": bool, "agent_name": "..." }
    """
    prompt = f"""
You are a micro-architecture expert. Decide if a separate CONTROL/COORDINATION/SEQUENCING agent
is required for this design, distinct from datapath/RTL generation.

A behavioral control agent is needed if any of these hold:
- Multiple operations require decode/selection
- Multi-step sequencing or state progression exists
- Handshake/enable/writeback control is implied
- A controller FSM is needed to drive the datapath

Return STRICT JSON only:
{{
  "controller_required": true | false,
  "agent_name": "Human-readable agent name if required, else empty string"
}}

STRUCTURED_SPEC:
{json.dumps(structured_spec_final, indent=2)}
""".strip()


    print("Structured spec final from llm-detect",structured_spec_final)

    raw = await run_llm_fallback(prompt)
    try:
        start, end = raw.find("{"), raw.rfind("}")
        decision = json.loads(raw[start:end+1]) if start != -1 and end != -1 else {}
    except Exception as e:
        logger.warning(f"LLM decision parse failed: {e} | raw head: {raw[:200]}")
        decision = {"controller_required": False, "agent_name": ""}

    # normalize
    decision["controller_required"] = bool(decision.get("controller_required"))
    decision["agent_name"] = (decision.get("agent_name") or "").strip()
    return decision


async def generate_missing_agents_batch(payload: dict) -> dict:
    """
    Batch-create behavioral agents with functionality output (FSM + signal roles).
    payload = {
      "goal": str,
      "user_id": str,
      "agent_names": [str,...],
      "structured_spec_final": dict
    }
    """
    goal = payload.get("goal", "")
    user_id = payload.get("user_id")
    agent_names = payload.get("agent_names", [])
    structured_spec = payload.get("structured_spec_final") or {}

    loop_type = (structured_spec.get("loop_type") or "digital").lower()
    if loop_type not in ["digital", "analog", "embedded","system","validation"]:
        loop_type = "digital"

    created = []
    for name in agent_names:
        info = generate_behavioral_agent(name, loop_type)

        # Persist in agents table
        try:
            supabase.table("agents").upsert({
                "agent_name": info["agent_name"],
                "loop_type": loop_type,
                "script_path": info["path"],
                "description": info["description"],
                "is_custom": True,
                "owner_id": user_id,
                "is_prebuilt": False
            }).execute()
        except Exception as e:
            logger.warning(f"agents upsert failed for {name}: {e}")

        try:
            # 1) Build/refine description (2–4 sentences)
          #  clean_desc = await summarize_capability_long(info["description"])
            capability_text = build_capability_signature({
                "agent_name": info["agent_name"],
                "description": info["description"],
                "capability_tags": infer_capability_tags(info["agent_name"], loop_type),
                "purpose": info["description"],   # description ~ purpose
                "interfaces": structured_spec.get("signals", [])  # optional & available here
            })


            # 2) Compute embedding
            emb = await compute_embedding(capability_text)

            tags = infer_capability_tags(info["agent_name"], loop_type)

            # 4) Upsert memory row (per-user scope)
            supabase.table("agent_memory").upsert({
                "user_id": user_id,                           # <-- per-user
                "agent_name": info["agent_name"],
                "capability_tags": tags,
                "success_rate": 0.0,
                "last_used_in": [goal] if goal else [],
                "version": "v1",
                "updated_at": datetime.utcnow().isoformat(),
                "capability_embedding": emb                   # <-- vector(1536)
            }).execute()

            logger.info(f"🧠 Capability embedding stored for {info['agent_name']}")
        except Exception as e:
            logger.warning(f"⚠️ Capability memory update failed for {info['agent_name']}: {e}")
        # --- end dynamic capability memory ---
        created.append({
            "agent_name": info["agent_name"],
            "loop_type": info["loop_type"],
            "path": info["path"],
            "description": info["description"],
        })

    logger.info(f" Final created agent {created}")

    return {"created_agents": created, "loop_type": loop_type}










