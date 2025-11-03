from utils.llm_utils import run_llm_fallback

async def auto_fill_missing_fields(original_text: str, structured_spec_draft: dict, missing_fields: list):
    """
    Insert missing values into the original text using minimal rewriting.
    Each auto-inserted value is wrapped in [brackets] for user visibility/editing.
    """
    missing_summary = "\n".join([
        f"- {m['path']}: {m['ask']}"
        for m in missing_fields
    ])

    prompt = f"""
You are assisting in refining a hardware specification.

User specification:


Detected missing or incomplete details:
{missing_summary}

Your task:
- Add only the missing information.
- Preserve the user's writing style.
- DO NOT rewrite the entire spec.
- When inserting new values, wrap them in square brackets [like this].
- Do not add explanations. Return only the revised spec.
"""

    improved = await run_llm_fallback(prompt)
    return improved.strip()
