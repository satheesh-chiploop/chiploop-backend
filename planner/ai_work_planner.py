import os
import json
import requests
from loguru import logger
from openai import OpenAI

# Reuse your environment variable pattern
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
PORTKEY_BASE_URL = os.getenv("PORTKEY_BASE_URL", "https://api.portkey.ai")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

def plan_workflow(prompt: str, agent_capabilities: dict, workflow_id: str = None) -> dict:
    """
    Generates an AI workflow plan from user intent using LLM (Ollama → Portkey → OpenAI fallback).
    """

    logger.info(f"🧠 Planning workflow for goal: {prompt[:100]}...")

    # --- 1. Build planning context ---
    context = "\n".join([
        f"- {name}: domain={meta['domain']}, inputs={meta['inputs']}, outputs={meta['outputs']}, desc={meta['description']}"
        for name, meta in agent_capabilities.items()
    ])

    system_prompt = f"""
    You are an AI Workflow Planner for ChipLoop.
    You have access to these agents:
    {context}

    Given a user goal, you must return valid JSON in the format:
    {{
        "loop_type": "<digital|analog|embedded|system>",
        "agents": ["Agent1", "Agent2", ...]
    }}

    Rules:
    - Choose the minimal set of agents required.
    - Always end with "System Workflow Agent" if the loop_type is "system".
    - Only use existing agents.
    - Never output text outside JSON.
    """

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"User goal: {prompt}"}
    ]

    # --- 2. Try local Ollama first ---
    if USE_LOCAL_OLLAMA:
        try:
            logger.info("🦙 Using local Ollama model for workflow planning...")
            resp = requests.post(
                "http://localhost:11434/api/generate",
                json={"model": "llama3", "prompt": system_prompt + "\n" + prompt}
            )
            response_text = resp.text
                        # --- After receiving content from Portkey/OpenAI/Ollama ---
            plan = safe_parse_plan(content if 'content' in locals() else response_text)

            # 🔍 Detect missing agents
            from agent_capabilities import AGENT_CAPABILITIES
            existing = set(AGENT_CAPABILITIES.keys())
            suggested = set(plan.get("agents", []))
            missing = list(suggested - existing)
            plan["missing_agents"] = missing
            logger.info(f"🧩 Missing agents detected: {missing}")
            return plan

        except Exception as e:
            logger.warning(f"Ollama failed: {e}")

    # --- 3. Try Portkey (if configured) ---
    if PORTKEY_API_KEY:
        try:
            logger.info("🪄 Using Portkey API for workflow planning...")
            headers = {
                "x-portkey-api-key": PORTKEY_API_KEY,
                "Content-Type": "application/json"
            }
            payload = {
                "model": "gpt-4o-mini",
                "messages": messages,
                "temperature": 0.3
            }
            r = requests.post(f"{PORTKEY_BASE_URL}/v1/chat/completions", json=payload, headers=headers, timeout=60)
            content = r.json()["choices"][0]["message"]["content"]
                        # --- After receiving content from Portkey/OpenAI/Ollama ---
            plan = safe_parse_plan(content if 'content' in locals() else response_text)

            # 🔍 Detect missing agents
            from agent_capabilities import AGENT_CAPABILITIES
            existing = set(AGENT_CAPABILITIES.keys())
            suggested = set(plan.get("agents", []))
            missing = list(suggested - existing)
            plan["missing_agents"] = missing
            logger.info(f"🧩 Missing agents detected: {missing}")
            return plan


        except Exception as e:
            logger.warning(f"Portkey fallback failed: {e}")

    # --- 4. Fallback to direct OpenAI ---
    if OPENAI_API_KEY:
        try:
            logger.info("🌐 Using OpenAI fallback for workflow planning...")
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.3
            )
            content = resp.choices[0].message.content
            # --- After receiving content from Portkey/OpenAI/Ollama ---
            plan = safe_parse_plan(content if 'content' in locals() else response_text)

            # 🔍 Detect missing agents
            from agent_capabilities import AGENT_CAPABILITIES
            existing = set(AGENT_CAPABILITIES.keys())
            suggested = set(plan.get("agents", []))
            missing = list(suggested - existing)
            plan["missing_agents"] = missing
            logger.info(f"🧩 Missing agents detected: {missing}")
            return plan

        except Exception as e:
            logger.error(f"OpenAI fallback failed: {e}")

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
from utils.llm_utils import run_llm_fallback  # your existing chain


async def auto_compose_workflow_graph(goal: str, preplan: dict | None = None):
    """
    Builds a structured workflow graph (nodes + edges)
    using a preplan if provided, or generates one internally.
    """
    if preplan:
        logger.info("📎 Using preplan from frontend to skip re-planning.")
        plan_data = preplan
    else:
        logger.info("🧠 No preplan supplied — generating plan internally.")
        from agent_capabilities import AGENT_CAPABILITIES
        plan_data = plan_workflow(goal, AGENT_CAPABILITIES)

    prompt = f"""You are ChipLoop workflow architect.
    Build a workflow for this goal: {goal}.
    Here is a pre-generated plan you must follow:
    {json.dumps(plan_data, indent=2)}
    Use known agents from AGENT_CAPABILITIES.
    Output valid JSON with keys: nodes, edges, summary.
    """
    response = await run_llm_fallback(prompt)

    try:
        plan = json.loads(response)
    except Exception:
        plan = {"nodes": [], "edges": [], "summary": response}

    # --- Step 2: Detect missing agents ---
    # Prefer preplan's missing_agents if available
    missing = []
    if preplan and preplan.get("missing_agents"):
        missing = preplan["missing_agents"]
        logger.info(f"📎 Using missing_agents from preplan: {missing}")
    else:
    # Fallback: detect from current graph
        existing_agents = [a["data"]["backendLabel"] for a in plan.get("nodes", [])]
        from agent_capabilities import AGENT_CAPABILITIES
        missing = [a for a in existing_agents if a not in AGENT_CAPABILITIES]
        logger.info(f"🧩 Detected missing agents: {missing}")

# Create and persist any missing agents
    if missing:
        from .ai_agent_planner import plan_agent_fallback
        for m in missing:
            try:
                new_agent = await plan_agent_fallback(m)
                # Persist the new agent so it can be reused later
                register_new_agent(new_agent)
                logger.info(f"✅ Auto-created & saved missing agent: {m}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to generate missing agent {m}: {e}")

    # --- Step 3: Auto-position nodes ---
    for i, n in enumerate(plan.get("nodes", [])):
        n.setdefault("id", f"n{i+1}")
        n.setdefault("position", {"x": 150 * i, "y": 100 + 60 * (i % 2)})

    # --- Step 4: Auto-connect if edges missing ---
    if not plan.get("edges"):
        plan["edges"] = [
            {"source": plan["nodes"][i]["id"], "target": plan["nodes"][i+1]["id"]}
            for i in range(len(plan["nodes"]) - 1)
        ]
    logger.success("✅ Auto-compose complete.")

    return {
        "nodes": plan.get("nodes", []),
        "edges": plan.get("edges", []),
        "summary": plan.get("summary", "Auto-composed workflow complete."),
    }

