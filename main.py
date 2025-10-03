from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict
from fastapi.middleware.cors import CORSMiddleware
import json
from fastapi.responses import FileResponse
import jwt
import os
import importlib.util
import logging
# ---------------- JWT Verification ----------------
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")

logger = logging.getLogger("uvicorn.error")
logger.info(f"🔑 Loaded SUPABASE_JWT_SECRET length: {len(SUPABASE_JWT_SECRET)}")

from openai import OpenAI
client = OpenAI()

from fastapi.responses import JSONResponse


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


def verify_token(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = auth.split(" ")[1]

    if not SUPABASE_JWT_SECRET:
        logger.error("❌ SUPABASE_JWT_SECRET is not set!")
        raise HTTPException(status_code=500, detail="Server misconfigured")

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"   # required by Supabase
        )
        logger.info(f"✅ Token decoded for user: {payload.get('sub')}")
        return payload
    except Exception as e:
        logger.error(f"❌ Token decode failed: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid token")

@app.post("/run_workflow")

async def run_workflow(
    request: Request,
    workflow: str = Form("{}"),
    file: UploadFile = File(None),
    spec_text: str = Form(None),
    user=Depends(verify_token)   # <-- enforce JWT here
):


    logger.info("🚀 run_workflow called")
    logger.info(f"Authenticated user: {user.get('sub') if user else '❌ none'}")
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

        resp = client.chat.completions.create(
               model="gpt-4o-mini",
               messages=[{"role": "user", "content": prompt}],
          )
        generated_code = resp.choices[0].message.content

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
