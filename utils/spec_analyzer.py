# utils/spec_analyzer.py
import json, re
from datetime import datetime
from loguru import logger
from utils.llm_utils import run_llm_fallback

try:
    from main import supabase  # reuse existing client if running inside server
except Exception:
    supabase = None


async def analyze_spec_text(goal: str, voice_summary: str = "", user_id: str = "anonymous") -> dict:
    """
    Shared analyzer logic for both FastAPI route and internal planners.
    Returns dict: {"status": "...", "coverage": {...}}
    """
    # --- Combine voice + text ---
    combined_goal = f"{voice_summary}\nAdditional user text: {goal}" if goal and voice_summary else (goal or voice_summary)
    if not combined_goal.strip():
        return {"status": "error", "message": "No spec text or voice summary provided."}

    # --- LLM prompt ---
    analyzer_prompt = f"""
    You are ChipLoop's Spec Analyzer.
    Analyze the following design specification (may include combined voice+text input):
    \"\"\"{combined_goal}\"\"\"

    Score each dimension (0–25):
    - Intent (what to achieve)
    - I/O (inputs, outputs, data flow)
    - Constraints (timing, power, area)
    - Verification (how to validate)

    Suggest up to 5 clarifying questions to improve coverage.
    Return clean JSON:
    {{
      "normalized_spec": "...",
      "intent": <int>,
      "io": <int>,
      "constraints": <int>,
      "verification": <int>,
      "questions": ["...", "..."]
    }}
    """

    try:
        response = await run_llm_fallback(analyzer_prompt)
        match = re.search(r"\{[\s\S]*\}", response)
        json_str = match.group(0) if match else "{}"
        result = json.loads(json_str)
    except Exception as e:
        logger.warning(f"⚠️ JSON parse failed: {e}")
        result = {
            "normalized_spec": response[:300] if 'response' in locals() else "",
            "intent": 10,
            "io": 10,
            "constraints": 10,
            "verification": 10,
            "questions": ["Could you clarify the inputs/outputs?"]
        }

    total = sum(result.get(k, 0) for k in ("intent", "io", "constraints", "verification"))
    result["total_score"] = total

    # --- Optional Supabase logging ---
    if supabase:
        try:
            supabase.table("spec_coverage").insert({
                "user_id": user_id,
                "goal": combined_goal,
                "normalized_spec": result.get("normalized_spec"),
                "intent_score": result.get("intent"),
                "io_score": result.get("io"),
                "constraint_score": result.get("constraints"),
                "verification_score": result.get("verification"),
                "total_score": total,
                "clarifying_questions": result.get("questions"),
                "created_at": datetime.utcnow().isoformat()
            }).execute()
        except Exception as db_err:
            logger.warning(f"⚠️ Supabase insert failed: {db_err}")

    return {"status": "ok", "coverage": result}
