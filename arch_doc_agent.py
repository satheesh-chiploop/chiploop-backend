from datetime import datetime
from openai import OpenAI

client = OpenAI()

def arch_doc_agent(state: dict) -> dict:
    print("\n📘 Running Architecture Doc Agent...")
    spec = state.get("spec", "")
    rtl  = state.get("rtl", "")

    prompt = f"""
Create an Architecture document in Markdown for module 'design'.

Sections (exact order):
# Architecture Document - Module 'design'
## 1. Overview
## 2. Block Diagram (ASCII)
## 3. Pin / Port Descriptions (table: Name | Dir | Width | Description)
## 4. Functional Description
## 5. Timing Diagram (ASCII)
## 6. Notes for Verification & Synthesis

Constraints:
- Output valid Markdown only.
- Derive ports from the RTL; infer missing details from spec.

[Spec]
{spec}

[RTL]
{rtl}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",  # or "gpt-4o" if you prefer full
        messages=[{"role": "user", "content": prompt}],
    )
    doc_md = response.choices[0].message.content

    fn = f"architecture_doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(doc_md)

    state["architecture_doc_path"] = fn
    state["architecture_doc"] = doc_md
    state["status_arch"] = "✅ Architecture document generated"
    print(f"  → Saved: {fn}")
    return state
