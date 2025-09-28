from datetime import datetime
from langchain_ollama import OllamaLLM

llm = OllamaLLM(model="mistral")

def integration_doc_agent(state: dict) -> dict:
    print("\n📗 Running Integration Doc Agent...")
    spec = state.get("spec", "")
    arch = state.get("architecture_doc", "")
    rtl  = state.get("rtl", "")

    prompt = f"""
Create an Integration Guide in Markdown for module 'design'.

Sections (exact order):
# Integration Guide - Module 'design'
## 1. System Context Diagram (ASCII)
## 2. External Interfaces (clock/reset domain, bus/GPIO mapping)
## 3. Integration Guidelines (reset sequencing, constraints, params)
## 4. Example Instantiation (Verilog snippet)
## 5. Verification Hooks (top-level checks, assertions)

Constraints:
- Output valid Markdown only.
- Keep it concise and practical.

[Spec]
{spec}

[Architecture Doc]
{arch}

[RTL]
{rtl}
"""
    doc_md = llm.invoke(prompt)
    fn = f"integration_doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(doc_md)

    state["integration_doc_path"] = fn
    state["integration_doc"] = doc_md
    state["status_integ"] = "✅ Integration document generated"
    print(f"  → Saved: {fn}")
    return state