from utils.llm_utils import run_llm_fallback
import re

async def auto_fill_missing_fields(original_text: str, structured_spec_draft: dict, missing_fields: list):
    """
    Auto-fill missing fields using LLM. The model wraps suggested new values in [brackets].
    We extract those values and return them for UI editing.
    """
    missing_summary = "\n".join([
        f"- {m['path']}: {m['ask']}"
        for m in missing_fields
    ])

    prompt = f"""
You are assisting in refining a hardware specification.

User specification:

{original_text}

Detected missing or incomplete details:
{missing_summary}

Your task:
- Add only the missing information.
- Preserve the user's writing style.
- DO NOT rewrite the entire spec.
- When inserting new values, wrap them in square brackets [like this].
- Do not add explanations. Return only the revised spec.
"""

    improved_text = (await run_llm_fallback(prompt)).strip()

    # ✅ Extract suggested inserted values inside [...]
    #extracted_pairs = re.findall(r"([A-Za-z0-9_\[\].]+)\s*[:=]\s*\[([^\]]+)\]", improved_text)

    #auto_filled_values = {
     #   path: value for path, value in extracted_pairs
    #}

    auto_filled_values = {}
    for m in missing_fields:
       path = m["path"]
    # Look for [value] that follows this path in the improved_text
       pattern = re.escape(path) + r"\s*[:=]\s*\[([^\]]+)\]"
       match = re.search(pattern, improved_text)
       if match:
          auto_filled_values[path] = match.group(1).strip()

    # ✅ Convert missing field objects → UI string list
    #remaining_missing_fields = [m["path"] for m in missing_fields]
    remaining_missing_fields = missing_fields

    # ✅ structured_spec stays same for now — finalize will convert it
    structured_spec_enhanced = structured_spec_draft

    return improved_text, structured_spec_enhanced, remaining_missing_fields, auto_filled_values





