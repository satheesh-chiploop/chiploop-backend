from fastapi import FastAPI, UploadFile, File, Form
from pydantic import BaseModel
from typing import List, Dict
from fastapi.middleware.cors import CORSMiddleware
import json
from fastapi.responses import FileResponse
import os
import importlib.util
from langchain_ollama import OllamaLLM
llm = OllamaLLM(model="mistral")

from spec_agent import spec_agent
from rtl_agent import rtl_agent
from optimizer_agent import optimizer_agent

app = FastAPI(title="ChipLoop Backend")

origins = [
    "http://localhost:3000",
    "https://chiploop-saas.vercel.app",  # your deployed frontend
    # you can add custom domain later, e.g. "https://chiploop.ai"
]

# Allow frontend → backend calls (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # for dev; restrict later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Models ----------
class Node(BaseModel):
    id: str
    label: str

class Edge(BaseModel):
    source: str
    target: str

class Workflow(BaseModel):
    nodes: List[Node]
    edges: List[Edge]


AGENT_FUNCTIONS = {
    "📘 Spec Agent": spec_agent,
    "💻 RTL Agent": rtl_agent,
    "🛠 Optimizer Agent": optimizer_agent,
}

@app.post("/run_workflow")
async def run_workflow(
    workflow: str = Form(...),
    file: UploadFile = File(None),
    spec_text: str = Form(None)
):
    data = json.loads(workflow)
    state = {}

    if file:
        contents = await file.read()
        filename = f"uploads/{file.filename}"
        os.makedirs("uploads", exist_ok=True)
        with open(filename, "wb") as f:
            f.write(contents)
        state["uploaded_file"] = filename

    if spec_text:
        state["spec"] = spec_text

    results: Dict[str, str] = {}
    artifacts: Dict[str, Dict[str, str]] = {}

    for node in data["nodes"]:
        label = node["label"]
        func = AGENT_FUNCTIONS.get(label)
        if func:
            state = func(state)
            results[label] = state.get("status", "✅ Done")
            artifacts[label] = {
                "artifact": state.get("artifact"),
                "artifact_log": state.get("artifact_log"),
            }
        else:
            results[label] = "⚠ No implementation yet."

    return {
        "workflow_results": results,
        "artifacts": artifacts,
        "state": state,
    }

@app.post("/create_agent")
async def create_agent(agent_name: str = Form(...), description: str = Form(...)):
    os.makedirs("agents", exist_ok=True)
    filename = f"agents/{agent_name}.py"

    # Ask Ollama to generate the Python code for the agent
    prompt = f"""
    Write a Python function for an agent named {agent_name}_agent.

    Requirements:
    - Function signature: def {agent_name}_agent(state: dict) -> dict
    - The agent is described as: {description}
    - It must always:
        * Update state["status"]
        * Save output to backend/{agent_name}.txt
        * Save logs to backend/{agent_name}_log.txt
        * Update state["artifact"] and state["artifact_log"]
    - Keep the implementation self-contained and runnable.
    """

    generated_code = llm.invoke(prompt)

    # Save to file
    with open(filename, "w", encoding="utf-8") as f:
        f.write(generated_code)

    # Dynamically import the new agent
    spec = importlib.util.spec_from_file_location(f"{agent_name}_agent", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, f"{agent_name}_agent")

    # Register in runtime
    AGENT_FUNCTIONS[f"✨ {agent_name} Agent"] = func

    return {
        "message": f"Agent '{agent_name}' created successfully using AI",
        "file": filename,
    }

@app.get("/artifact/{filename}")
def get_artifact(filename: str):
    path = os.path.join(os.getcwd(), filename)
    if os.path.exists(path):
        return FileResponse(path, filename=filename)
    return {"error": "File not found"}
