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
            return safe_parse_plan(response_text)
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
            return safe_parse_plan(content)
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
            return safe_parse_plan(content)
        except Exception as e:
            logger.error(f"OpenAI fallback failed: {e}")

    # --- 5. If all fail ---
    logger.error("❌ All AI backends failed for workflow planning.")
    return {"loop_type": "unknown", "agents": []}

def safe_parse_plan(text: str) -> dict:
    """
    Extracts and parses JSON safely from LLM output.
    """
    try:
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        json_str = text[json_start:json_end]
        plan = json.loads(json_str)
        if not isinstance(plan, dict) or "agents" not in plan:
            raise ValueError("Invalid JSON plan format")
        logger.info(f"✅ Parsed workflow plan: {plan}")
        return plan
    except Exception as e:
        logger.warning(f"⚠️ Failed to parse plan: {e}")
        return {"loop_type": "unknown", "agents": []}

import json, random
from .ai_agent_planner import plan_agent_fallback
from utils.llm_utils import run_llm_fallback  # your existing chain

async def auto_compose_workflow_graph(goal: str):
    """
    Builds a structured workflow graph (nodes + edges)
    and creates missing agents if needed.
    """
    # --- Step 1: Ask LLM for required agents ---
    prompt = f"""You are ChipLoop workflow architect.
    Build a workflow for this goal: {goal}.
    Use known agents from AGENT_CAPABILITIES.
    Output valid JSON with keys: nodes, edges, summary.
    """
    response = await run_llm_fallback(prompt)

    try:
        plan = json.loads(response)
    except Exception:
        plan = {"nodes": [], "edges": [], "summary": response}

    # --- Step 2: Detect missing agents ---
    existing_agents = [a["data"]["backendLabel"] for a in plan.get("nodes", [])]
    from agent_capabilities import AGENT_CAPABILITIES
    missing = [a for a in existing_agents if a not in AGENT_CAPABILITIES]

    for agent in missing:
        await plan_agent_fallback(agent)  # creates agent & stores in Supabase

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

