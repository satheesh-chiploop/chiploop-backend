from fastapi import FastAPI, UploadFile, File, Form
from pydantic import BaseModel
from typing import List, Dict
from fastapi.middleware.cors import CORSMiddleware
import json
from fastapi.responses import FileResponse
import os
import importlib.util
import logging
from langchain_ollama import OllamaLLM
from fastapi.responses import JSONResponse

llm = OllamaLLM(model="mistral")

from spec_agent import spec_agent
from rtl_agent import rtl_agent
from optimizer_agent import optimizer_agent

app = FastAPI(title="ChipLoop Backend")

@app.get("/")
async def root():
    return {"status": "ok", "message": "Backend is live"}

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


logger = logging.getLogger("uvicorn.error")

@app.post("/run_workflow")

async def run_workflow(
    workflow: str = Form(...),
    file: UploadFile = File(None),
    spec_text: str = Form(None)
):

    logger.info("🚀 run_workflow called")
    logger.info(f"workflow raw: {workflow[:200] if workflow else '❌ missing'}")
    logger.info(f"spec_text: {spec_text}")
    logger.info(f"file: {file.filename if file else '❌ none'}")

    try:
        logger.info("🚀 /run_workflow called")
        data = json.loads(workflow)
        state = {}

        if file:
            contents = await file.read()
            filename = f"uploads/{file.filename}"
            os.makedirs("uploads", exist_ok=True)
            with open(filename, "wb") as f:
                f.write(contents)
            state["uploaded_file"] = filename
            logger.info(f"📁 File uploaded: {filename}")

        if spec_text:
            state["spec"] = spec_text
            logger.info("📝 Spec text provided")

        results: Dict[str, str] = {}
        artifacts: Dict[str, Dict[str, str]] = {}

        for node in data.get("nodes", []):
            label = node.get("label")
            func = AGENT_FUNCTIONS.get(label)
            if func:
                try:
                    state = func(state)
                    results[label] = state.get("status", "✅ Done")
                    artifacts[label] = {
                        "artifact": state.get("artifact"),
                        "artifact_log": state.get("artifact_log"),
                    }
                    logger.info(f"✅ Agent executed: {label}")
                except Exception as agent_err:
                    results[label] = f"❌ Error: {str(agent_err)}"
                    logger.error(f"Agent {label} failed: {agent_err}")
            else:
                results[label] = "⚠ No implementation yet."
                logger.warning(f"No function found for agent: {label}")

        return JSONResponse(
            content={
                "workflow_results": results,
                "artifacts": artifacts,
                "state": state,
                "status": "success",
            },
            status_code=200,
        )

    except Exception as e:
        logger.error(f"❌ Error in /run_workflow: {e}")
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500,
        )


@app.post("/create_agent")
async def create_agent(agent_name: str = Form(...), description: str = Form(...)):
    try:
        logger.info(f"✨ Creating agent: {agent_name}")
        os.makedirs("agents", exist_ok=True)
        filename = f"agents/{agent_name}.py"

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

        with open(filename, "w", encoding="utf-8") as f:
            f.write(generated_code)

        spec = importlib.util.spec_from_file_location(f"{agent_name}_agent", filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        func = getattr(module, f"{agent_name}_agent")

        AGENT_FUNCTIONS[f"✨ {agent_name} Agent"] = func
        logger.info(f"✅ Agent registered: {agent_name}")

        return JSONResponse(
            content={
                "status": "success",
                "message": f"Agent '{agent_name}' created successfully using AI",
                "file": filename,
            },
            status_code=200,
        )

    except Exception as e:
        logger.error(f"❌ Error in /create_agent: {e}")
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500,
        )


@app.get("/artifact/{filename}")
def get_artifact(filename: str):
    path = os.path.join(os.getcwd(), filename)
    if os.path.exists(path):
        return FileResponse(path, filename=filename)
    return {"error": "File not found"}
