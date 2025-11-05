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

    # ✅ Extract values solely based on bracket order from LLM output

    # ✅ 1) Try to extract labeled values: "path: [value]"
    labeled_pairs = re.findall(r"([A-Za-z0-9_\[\].]+)\s*:\s*\[([^\]]+)\]", improved_text)
    auto_filled_values = {path.strip(): val.strip() for path, val in labeled_pairs}

    # ✅ 2) Fallback: assign bracket values by position to paths NOT already filled
    missing_section_match = re.search(
       r"Detected missing or incomplete details:(.*?)(?:\n\n|$)",
       improved_text,
       flags=re.DOTALL
    )

    if missing_section_match:
       missing_section = missing_section_match.group(1)
       bracket_values = re.findall(r"\[([^\]]+)\]", missing_section)
    else:
    # fallback: no missing block found → do not extract from full text
       bracket_values = []


    b_index = 0
    for m in missing_fields:
        path = m["path"]
        if path not in auto_filled_values and b_index < len(bracket_values):
           auto_filled_values[path] = bracket_values[b_index].strip()
           b_index += 1

    # ✅ Convert missing field objects → UI string list
    #remaining_missing_fields = [m["path"] for m in missing_fields]
    remaining_missing_fields = missing_fields

    # ✅ structured_spec stays same for now — finalize will convert it
    structured_spec_enhanced = structured_spec_draft

    return improved_text, structured_spec_enhanced, remaining_missing_fields, auto_filled_values





