from datetime import datetime
from langchain_ollama import OllamaLLM

llm = OllamaLLM(model="mistral")

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
    doc_md = llm.invoke(prompt)
    fn = f"architecture_doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(doc_md)

    state["architecture_doc_path"] = fn
    state["architecture_doc"] = doc_md
    state["status_arch"] = "✅ Architecture document generated"
    print(f"  → Saved: {fn}")
    return state