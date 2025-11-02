import os
import json
import requests
from datetime import datetime
from loguru import logger
from openai import OpenAI
from utils.llm_utils import run_llm_fallback 
from portkey_ai import Portkey
from typing import Optional, Union
# ---------------- Supabase client ----------------

from supabase import create_client
SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# Reuse your environment variable pattern
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()

from planner.capability_graph import get_candidate_agents
from planner.ranking import rank_candidates
from planner.mycelium_memory import memory_lookup

import re

def extract_json_block(text):
    """Extract valid JSON block from any LLM text output."""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return {}

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}

    

async def plan_workflow(prompt: str, structured_spec_final=None) -> dict:
    """
    Generates an AI workflow plan from user intent using LLM (Portkey-first).
    Now enhanced with structured_spec_final → AGX-context-informed LLM planning.
    """

    logger.info(f"🧠 Planning workflow for goal: {prompt[:100]}...")

    from agent_capabilities import AGENT_CAPABILITIES
    from planner.capability_graph import get_candidate_agents, extract_niches
    import json

    # 🔹 Capability context for the LLM
    context = "\n".join([
        f"- {name}: {meta.get('description', '')}"
        for name, meta in AGENT_CAPABILITIES.items()
    ])

    # 🔹 If structured spec is present → Infer design niches & candidate agents
    niches = extract_niches(structured_spec_final) if structured_spec_final else []
    candidate_agents = get_candidate_agents(structured_spec_final) if structured_spec_final else list(AGENT_CAPABILITIES.keys())

    structured_context = ""
    if structured_spec_final:
        structured_context = f"""
Structured Digital Spec:
{json.dumps(structured_spec_final, indent=2)}

Detected Design Niches:
{', '.join(niches) if niches else "none"}

Recommended Agent Candidates (Do not invent new agent names):
{candidate_agents}

You MUST select agents that satisfy these architectural constraints:
- Respect multi-clock & multi-reset domain boundaries.
- If multiple power domains → include 'Power Intent Agent'.
- If CDC crossings present → include 'CDC Guard Agent'.
- If PDC crossings present → include 'PDC Guard Agent'.
- Always generate the minimal correct chain (no unnecessary agents).
"""

    # ✅ Your original rule prompt remains intact
    system_prompt = f"""
You are an AI Workflow Planner for ChipLoop.
You have access to these agents:
{context}

{structured_context}

Given a user goal, you must return valid JSON in the format:
{{
    "loop_type": "<digital|analog|embedded|system>",
    "agents": ["Agent1", "Agent2", ...]
}}

Rules:
- Choose the minimal set of agents required.
- Always end with "System Workflow Agent" if the loop_type is "system".
- Only use existing agents (NO new agent names).
- Never output text outside JSON.
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"User goal: {prompt}"}
    ]

    # --- 3. Try Portkey (Primary Backend) ---
    if PORTKEY_API_KEY:
        try:
            logger.info("🪄 Using Portkey API for workflow planning...")

            completion = client_portkey.chat.completions.create(
                model="@chiploop/gpt-5-mini",
                messages=messages,
                temperature=0.5,     # ⬇️ Less randomness = more stable planning
                stream=False
            )

            llm_output = completion.choices[0].message.content or ""
            plan = safe_parse_plan(llm_output)

            # ✅ Detect missing agents
            existing = set(AGENT_CAPABILITIES.keys())
            suggested = set(plan.get("agents", []))
            missing = list(suggested - existing)

            # ✅ Merge deterministic required agents (AGX selector)
            if structured_spec_final:
                from agents.agent_selector import select_required_agents
                required = select_required_agents(structured_spec_final)
                missing = list({*missing, *required})

            plan["missing_agents"] = missing

            logger.info(f"🧩 Missing agents detected (to autogen in auto-compose): {missing}")
            return plan

        except Exception as e:
            logger.warning(f"Portkey fallback failed: {e}")

    # --- 5. If all fail ---
    logger.error("❌ All AI backends failed for workflow planning.")
    return {"loop_type": "unknown", "agents": []}





def safe_parse_plan(text: str):
    """
    Robustly extract JSON from LLM responses that may include extra text.
    """
    import json, re, logging
    logger = logging.getLogger("planner")

    if not text:
        return {"loop_type": "unknown", "agents": []}

    # 🧩 Try strict JSON first
    try:
        return json.loads(text)
    except Exception:
        pass

    # 🔍 Try to extract JSON substring using regex
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            snippet = match.group(0)
            return json.loads(snippet)
    except Exception as e:
        logger.warning(f"⚠️ safe_parse_plan fallback failed: {e}")

    # 🪫 Final fallback
    logger.warning("⚠️ Failed to parse plan: returning empty template.")
    return {"loop_type": "unknown", "agents": []}


def register_new_agent(agent_data: dict):
    """Persist newly generated agent so it can be reused."""
    name = agent_data["agent_name"].replace(" ", "_").lower()
    path = f"agents/custom/{name}.py"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(agent_data["full_code"])
    logger.info(f"💾 Custom agent saved to {path}")


import json, random
from .ai_agent_planner import plan_agent_fallback


async def auto_compose_workflow_graph(
    goal: str,
    structured_spec_final: dict,
    preplan: Optional[Union[dict, str]] = None
):


    """
    Builds a structured workflow graph (nodes + edges)
    using a preplan if provided, or generates one internally.
    """
    # --- Step 1: Handle preplan or re-plan internally ---
    if preplan:
        # Handle JSON string sent from frontend
        if isinstance(preplan, str):
            try:
                preplan = json.loads(preplan)
                logger.info("✅ Parsed preplan string into dict.")
            except Exception as e:
                logger.warning(f"⚠️ Failed to parse preplan string: {e}")
                preplan = None

    if preplan and isinstance(preplan, dict) and len(preplan.keys()) > 0:
        logger.info("📎 Using preplan from frontend to skip re-planning.")
        plan_data = preplan

        prompt = f"""
You are ChipLoop Workflow Architect.

User goal:
{goal}

A preplan has already been generated with identified agents and their order of execution.
Here is the preplan JSON:
{json.dumps(plan_data, indent=2)}

🧠 Instructions:
- Use ONLY the agents listed in the preplan.
- Do NOT create new or repeated agent instances.
- Build logical connections (edges) between agents to represent workflow data flow.
- Maintain order and hierarchy from the preplan.
- Add a concise "summary" explaining how this workflow achieves the goal.
- Output a valid JSON object with the following keys only: summary, nodes, edges.

Each node must include:
- id (n1, n2, ...)
- type (agent name from preplan)
- position (x, y) spaced horizontally
"""    
    else:
        logger.info("🧠 No valid preplan supplied — generating plan internally.")
        from agent_capabilities import AGENT_CAPABILITIES
        plan_data = plan_workflow(goal, AGENT_CAPABILITIES)
        prompt = f"""
You are ChipLoop Workflow Architect.

Goal:
{goal}

Available agents:
{json.dumps(plan_data, indent=2)}

🧠 Instructions:
- Choose the minimum number of relevant agents.
- Each agent can appear only once.
- Do NOT invent unknown or placeholder agents.
- Build a clean JSON workflow with keys: summary, nodes, edges.
"""
    response = await run_llm_fallback(prompt)
    plan = extract_json_block(response)

    if not plan:
        logger.warning("⚠️ No valid JSON detected, falling back to empty plan.")
        plan = {"nodes": [], "edges": [], "summary": str(response)}

    # --- Step 3: Detect missing agents ---
    missing = []
    if isinstance(preplan, dict) and preplan.get("missing_agents"):
        missing = preplan["missing_agents"]
        logger.info(f"📎 Using missing_agents from preplan: {missing}")
    else:
        existing_agents = []
        for a in plan.get("nodes", []):
        # Prefer the explicit 'agent' field from JSON; fall back to 'type'
            agent_name = (
                a.get("agent")
                or a.get("data", {}).get("backendLabel")
                or a.get("type")
                or a.get("label")
                or "unknown_agent"
            )
        existing_agents.append(agent_name)
        existing_agents = [
            a for a in existing_agents
            if a.lower() not in ["process", "flow", "pipeline", "unknown_agent"]
        ]
        from agent_capabilities import AGENT_CAPABILITIES
        missing = [a for a in existing_agents if a not in AGENT_CAPABILITIES]

         # ✅ Remove duplicates while preserving order
        seen = set()
        existing_agents = [a for a in existing_agents if not (a in seen or seen.add(a))]

        if len(existing_agents) < len(plan.get("nodes", [])):
            logger.warning(f"🧹 Deduplicated agents: {existing_agents}")
       
        logger.info(f"🔍 LLM suggested agents: {existing_agents}")

        logger.info(f"📚 Known agents: {list(AGENT_CAPABILITIES.keys())[:10]}")
        logger.info(f"🧩 Missing agents: {missing}")
    # --- Step 4: Create and persist any missing agents ---
    from agents.agent_generator import generate_agent,reload_custom_agents
    from agent_capabilities import AGENT_CAPABILITIES

    for agent in missing:
        if agent not in AGENT_CAPABILITIES:     # means it does not exist yet
            await generate_agent(agent, structured_spec_final)
        # IMPORTANT: dynamically load new agent into runtime registry
            reload_custom_agents()

    # --- Step 5: Auto-layout nodes ---
    for i, n in enumerate(plan.get("nodes", [])):

        agent_name = (
            n.get("agent")
            or n.get("data", {}).get("backendLabel")
            or n.get("type")
            or n.get("label")
            or "unknown_agent"
        )

    n["id"] = f"n{i+1}"
    n["position"] = {"x": 150 * i, "y": 100 + 60 * (i % 2)}

    # ✅ Standardize node data structure for frontend & execution
    n["data"] = {
        "uiLabel": agent_name.replace("_", " ").title(),   # What user sees
        "backendLabel": agent_name,                        # Execution lookup key
        "desc": f"Auto-composed: {agent_name}"
    }

    # --- Step 6: Auto-connect edges ---
    if not plan.get("edges"):
        plan["edges"] = [
            {"source": plan["nodes"][i]["id"], "target": plan["nodes"][i + 1]["id"]}
            for i in range(len(plan["nodes"]) - 1)
        ]
    logger.success("✅ Auto-compose complete.")

    # --- Step 7: Update agent memory ---
    
    # --- Step 7: Update agent memory ---
    try:
        for node in plan.get("nodes", []):
        # handle both {"data": {...}} and flat {"type": "..."} formats
            agent_name = (
                node.get("data", {}).get("backendLabel")
                or node.get("type")
                or node.get("label")
                or "unknown_agent"
            )

            supabase.table("agent_memory").upsert({
               "agent_name": agent_name,
               "last_used_in": [goal],
               "updated_at": datetime.utcnow().isoformat()
            }).execute()

        logger.info("🧠 Agent memory updated successfully.")
    except Exception as e:
        logger.warning(f"⚠️ Failed to update agent memory: {e}")

    return {
        "nodes": plan.get("nodes", []),
        "edges": plan.get("edges", []),
        "summary": plan.get("summary", "Auto-composed workflow complete."),
        "structured_spec_final": structured_spec_final,  
    }
    

