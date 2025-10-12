from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import List, Dict
from fastapi.middleware.cors import CORSMiddleware
import json
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
import jwt
import os
import stripe
import importlib.util
import logging
from supabase import create_client, Client
import time
import requests
from portkey_ai import Portkey
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse
from datetime import datetime
import uuid
import traceback



# ---------- Environment & Setup ----------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
logger = logging.getLogger("uvicorn.error")
logger.info(f"🔑 Loaded SUPABASE_JWT_SECRET length: {len(SUPABASE_JWT_SECRET)}")

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

client = Portkey(api_key=PORTKEY_API_KEY)

# ---------- FastAPI App ----------
app = FastAPI(title="ChipLoop Backend")

@app.get("/")
async def root():
    return {"status": "ok", "message": "Backend is live"}

origins = [
    "http://localhost:3000",
    "https://chiploop-saas.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- JWT Verify ----------
def verify_token(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = auth.split(" ")[1]
    if not SUPABASE_JWT_SECRET:
        logger.error("❌ SUPABASE_JWT_SECRET is not set!")
        raise HTTPException(status_code=500, detail="Server misconfigured")
    try:
        payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
        logger.info(f"✅ Token decoded for user: {payload.get('sub')}")
        return payload
    except Exception as e:
        logger.error(f"❌ Token decode failed: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid token")

# ---------- Shared Helpers (Streaming + Logging) ----------
def update_workflow_log(workflow_name, new_log, status="running", phase="init", artifacts=None):
    try:
        record = {
            "logs": new_log,
            "status": status,
            "phase": phase,
            "artifacts": artifacts or {},
            "updated_at": "now()"
        }
        supabase.table("workflows").update(record).eq("name", workflow_name).execute()
        logger.info(f"✅ Workflows table updated for '{workflow_name}' (phase={phase})")
    except Exception as e:
        logger.warning(f"⚠️ Workflow log update failed for '{workflow_name}' (phase={phase}): {e}")

async def stream_agent_response(agent_name: str, prompt: str, phase: str):
    """
    Generic streaming helper usable by any agent (Spec, RTL, Verify, etc.)
    """
    backend = "Ollama" if USE_LOCAL_OLLAMA else "Portkey/OpenAI"
    start_time = time.time()
    collected_output = []

    def generate_stream():
        try:
            if not USE_LOCAL_OLLAMA:
                # Future-ready Portkey/OpenAI route
                completion = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                )
                for chunk in completion:
                    if chunk and hasattr(chunk, "choices"):
                        delta = chunk.choices[0].delta.get("content", "")
                        if delta:
                            collected_output.append(delta)
                            yield delta
            else:
                # Local Ollama streaming
                payload = {"model": "llama3", "prompt": prompt}
                with requests.post(OLLAMA_URL, json=payload, stream=True) as r:
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            j = json.loads(line.decode())
                            if "response" in j:
                                token = j["response"]
                                collected_output.append(token)
                                yield token
                        except Exception:
                            continue
        finally:
            duration = round(time.time() - start_time, 2)
            update_workflow_log(
                agent_name,
                "".join(collected_output),
                status="completed",
                phase=phase,
                artifacts={"duration": duration, "backend": backend}
            )

    return StreamingResponse(generate_stream(), media_type="text/plain")

# ---------- Core Routes ----------
from openai import OpenAI
client_openai = OpenAI()

from agents.spec_agent import spec_agent
from agents.rtl_agent import rtl_agent
from agents.optimizer_agent import optimizer_agent
from agents.testbench_agent_uvm import testbench_agent_uvm
from agents.arch_doc_agent import arch_doc_agent
from agents.integration_doc_agent import integration_doc_agent
from agents.testcase_agent import testcase_agent
from agents.assertion_agent import assertion_agent
from agents.simulation_agent import simulation_agent
from agents.coverage_agent import coverage_agent
from agents.covergroup_agent import covergroup_agent


import importlib
import pkgutil
import agents
# Prebuilt system agents (always available)

AGENT_REGISTRY = {
    "spec_agent": spec_agent,
    "rtl_agent": rtl_agent,
    "optimizer_agent": optimizer_agent,
}

def load_custom_agents():
    """Dynamically load user-created agents in /agents directory."""
    global AGENT_REGISTRY
    for _, name, _ in pkgutil.iter_modules(agents.__path__):
        if name not in AGENT_REGISTRY:  # avoid overwriting prebuilt ones
            try:
                module = importlib.import_module(f"agents.{name}")
                if hasattr(module, "run_agent"):
                    AGENT_REGISTRY[name] = module.run_agent
                    logger.info(f"⚡ Custom agent loaded: {name}")
            except Exception as e:
                logger.warning(f"⚠️ Could not load {name}: {e}")

logger.info(f"🧠 Loaded agents: {list(AGENT_REGISTRY.keys())}")


AGENT_FUNCTIONS = {
    "Spec Agent": spec_agent,
    "RTL Agent": rtl_agent,
    "Testbench Agent": testbench_agent_uvm,
    "Testcase Agent": testcase_agent,
    "Assertion Agent": assertion_agent,
    "Covergroup Agent": covergroup_agent,
    "Simulation Agent": simulation_agent,
    "Coverage Agent": coverage_agent,
    "Optimizer Agent" : optimizer_agent,
    "Arch Doc Agent" : arch_doc_agent,
   "Integration Doc Agent" : integration_doc_agent,
}

from fastapi import BackgroundTasks, Depends
from datetime import datetime
import uuid
import traceback
from supabase import create_client, Client
from typing import Dict


# --- Supabase client ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


@app.post("/run_workflow")
async def run_workflow(
    background_tasks: BackgroundTasks,
    workflow: str = Form(...),
    file: UploadFile = File(None),
    spec_text: str = Form(None),
    user=Depends(verify_token)  # ✅ JWT auth
):
    """
    Asynchronous Spec2RTL workflow runner (JWT + Supabase integrated)
    - Creates workflow entry tied to the authenticated user
    - Executes Spec → RTL → Optimizer agents in background
    - Returns immediately to prevent 502 timeouts
    """
    try:
        user_id = user.get("sub") if user else "anonymous"
        workflow_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        # ✅ Insert workflow record into Supabase
        supabase.table("workflows").insert({
            "id": workflow_id,
            "user_id": user_id,
            "name": "Digital Loop Run",
            "status": "running",
            "phase": "Spec2RTL",
            "logs": "🚀 Workflow started asynchronously.",
            "created_at": now,
            "updated_at": now,
            "artifacts": {}
        }).execute()

        artifact_dir = f"artifacts/{user_id}/{workflow_id}"
        os.makedirs(artifact_dir, exist_ok=True)

        logger.info(f"🚀 run_workflow called by user: {user_id}")
        logger.info(f"workflow raw: {workflow[:200] if workflow else '❌ missing'}")
        logger.info(f"spec_text: {spec_text}")
        logger.info(f"file: {file.filename if file else '❌ none'}")

        # --- Save file (if any) ---
        upload_path = None
        if file:
            contents = await file.read()
            upload_path = os.path.join(artifact_dir, file.filename)
            with open(upload_path, "wb") as f:
                f.write(contents)
            logger.info(f"📁 File uploaded: {upload_path}")

        # --- Save spec text (if any) ---
        if spec_text:
            spec_path = os.path.join(artifact_dir, "spec.txt")
            with open(spec_path, "w") as f:
                f.write(spec_text)
            logger.info("📝 Spec text saved successfully")

        # --- Start background task ---
# --- Queue job for chiploop-runner instead of running locally ---
        background_tasks.add_task(
           execute_workflow_background,
           workflow_id,
           user_id,
           workflow,
           spec_text,
           upload_path,
           artifact_dir
         )
        logger.info(f"🟡 Workflow {workflow_id} Started background  execution.")


        return {
            "job_id": workflow_id,
            "status": "started",
            "message": "Workflow started in background."
        }

    except Exception as e:
        logger.error(f"❌ Error in run_workflow: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/reload_agents")
async def reload_agents():
    try:
        load_custom_agents()
        return {"status": "success", "loaded_agents": list(AGENT_REGISTRY.keys())}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def execute_workflow_background(workflow_id, user_id, workflow, spec_text, upload_path, artifact_dir):
    """Executes workflow dynamically using hybrid agent registry (prebuilt + custom)."""
    try:
        logger.info(f"🧠 [BG] Executing workflow {workflow_id} for user {user_id}")
        workflow_dir = f"backend/workflows/{workflow_id}"
        os.makedirs(workflow_dir, exist_ok=True)

        data = json.loads(workflow)
        state = {}
        if upload_path:
            state["uploaded_file"] = upload_path
        if spec_text:
            state["spec"] = spec_text
        state["workflow_id"] = workflow_id
        state["workflow_dir"] = f"backend/workflows/{workflow_id}"
        results: Dict[str, str] = {}
        artifacts: Dict[str, Dict[str, str]] = {}

        # Merge static and dynamic agents into one lookup map
        agent_map = dict(AGENT_FUNCTIONS)
        agent_map.update(AGENT_REGISTRY)

        for node in data.get("nodes", []):
            label = node.get("label")
            normalized = (
                label.lower()
                .replace("📘", "")
                .replace("💻", "")
                .replace("🛠", "")
                .replace("🧪", "")
                .replace("✨", "")
                .replace("agent", "")
                .strip()
                .replace(" ", "_")
                + "_agent"
            )

            # Try to resolve agent: prefer AGENT_FUNCTIONS (prebuilt), else dynamic
            func = agent_map.get(label) or agent_map.get(normalized)

            if func:
                try:
                    logger.info(f"🚀 Executing agent: {label}")
                    update_workflow_log(workflow_id, f"🚀 Running {label}\n")
                    if label.strip().lower() == "simulation agent":
                        logger.info("▶️ Reached Simulation Agent → queuing for ChipRunner and stopping local agent chain.")
                        supabase.table("workflows").update({
                                "status": "queued",
                                "phase": "simulation",
                                "runner_assigned": None
                        }).eq("id", workflow_id).execute()
                        update_workflow_log(workflow_id, "🟡 Queued for ChipRunner (Simulation phase)\n")
                        return  # stop after queuing

                    state = func(state)
                    results[label] = state.get("status", "✅ Done")
                    art_path = None
                    if state.get("artifact"):
                        safe_label = label.replace(" ", "_").replace("📘", "").replace("💻", "").replace("🛠", "")
                        os.makedirs(artifact_dir, exist_ok=True)
                        art_path = os.path.join(artifact_dir, f"{safe_label}.txt")
                        with open(art_path, "w") as f:
                            f.write(state["artifact"] or "")

                    artifacts[label] = {
                        "artifact": f"/{art_path}" if art_path else None,
                        "artifact_log": state.get("artifact_log"),
                    }

                    logger.info(f"✅ Agent executed: {label}")
                    update_workflow_log(workflow_id, f"✅ Completed {label}\n")

                except Exception as agent_err:
                    results[label] = f"❌ Error: {str(agent_err)}"
                    logger.error(f"Agent {label} failed: {agent_err}")
                    update_workflow_log(workflow_id, f"❌ {label} failed: {agent_err}\n")
    except Exception as e:
        logger.error(f"❌ Workflow execution failed: {e}\n{traceback.format_exc()}")
        update_workflow_log(workflow_id, f"❌ Workflow failed: {e}\n")

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

# ---------- Workflow Persistence Routes ----------

@app.post("/workflows/start")
async def start_workflow(name: str = Form(...), user=Depends(verify_token)):
    row = {
        "user_id": user.get("sub"),
        "name": name,
        "status": "running",
        "logs": "",
        "artifacts": {}
    }
    res = supabase.table("workflows").insert(row).execute()
    return res.data[0]

@app.patch("/workflows/{workflow_id}/complete")
async def complete_workflow(workflow_id: str, status: str = Form(...), logs: str = Form(""), artifacts: str = Form("{}"), user=Depends(verify_token)):
    try:
        artifacts_json = json.loads(artifacts) if artifacts else {}
    except:
        artifacts_json = {}
    update = {
        "status": status,
        "logs": logs,
        "artifacts": artifacts_json
    }
    res = supabase.table("workflows").update(update).eq("id", workflow_id).execute()
    return res.data[0]

@app.get("/workflows/history")
async def get_history(user=Depends(verify_token)):
    res = (
        supabase.table("workflows")
        .select("*")
        .eq("user_id", user.get("sub"))
        .order("created_at", desc=True)
        .execute()
    )
    return res.data

from fastapi.staticfiles import StaticFiles
os.makedirs("artifacts", exist_ok=True)
app.mount("/artifacts", StaticFiles(directory="artifacts"), name="artifacts")

from fastapi import APIRouter, Request


@app.post("/create-checkout-session")
async def create_checkout_session(request: Request, user=Depends(verify_token)):
    try:
        data = await request.json()

        user_id = user.get("sub", "unknown")  # pulled from Supabase JWT

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[
                {
                    "price": os.getenv("STRIPE_PRICE_ID"),
                    "quantity": 1,
                }
            ],
            success_url="https://chiploop-saas.vercel.app/success",
            cancel_url="https://chiploop-saas.vercel.app/cancel",
            metadata={
                "user_id": user_id,
            },
        )

        print(f"✅ Stripe session created for user {user_id}: {session.url}")
        return {"url": session.url}

    except Exception as e:
        print("❌ Stripe checkout creation failed:", str(e))
        return {"error": str(e)}


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError:
        print("❌ Invalid payload")
        return {"error": "Invalid payload"}
    except stripe.error.SignatureVerificationError:
        print("❌ Invalid Stripe signature")
        return {"error": "Invalid signature"}

    # --- Handle Stripe events ---
    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id = data.get("metadata", {}).get("user_id")
        customer_id = data.get("customer")

        if user_id and customer_id:
            print(f"✅ Checkout completed for user {user_id}")
            supabase.table("workflows").update({
                "subscription_status": "basic",
                "stripe_customer_id": customer_id
            }).eq("user_id", user_id).execute()
        else:
            print("⚠️ Missing user_id or customer_id in checkout.session.completed")

    elif event_type == "customer.subscription.updated":
        user_id = data.get("metadata", {}).get("user_id")
        new_status = data.get("status")

        if user_id:
            print(f"🔄 Subscription updated for user {user_id}: {new_status}")
            supabase.table("workflows").update({
                "subscription_status": new_status
            }).eq("user_id", user_id).execute()
        else:
            print("⚠️ Missing user_id in customer.subscription.updated")

    elif event_type == "customer.subscription.deleted":
        user_id = data.get("metadata", {}).get("user_id")

        if user_id:
            print(f"❌ Subscription canceled for user {user_id}")
            supabase.table("workflows").update({
                "subscription_status": "free"
            }).eq("user_id", user_id).execute()
        else:
            print("⚠️ Missing user_id in customer.subscription.deleted")

    else:
        print(f"ℹ️ Unhandled event type: {event_type}")

    return {"status": "success"}


@app.post("/create-customer-portal-session")
async def create_customer_portal_session(user=Depends(verify_token)):
    """
    Create a Stripe Billing Portal session for the logged-in user.
    Allows users to manage (cancel/update) their subscriptions.
    """
    try:
        response = (
            supabase.table("workflows")
            .select("stripe_customer_id")
            .eq("user_id", user.get("sub"))
            .single()
            .execute()
        )

        if not response.data or "stripe_customer_id" not in response.data:
            return {"error": "Stripe customer not found for this user"}

        customer_id = response.data["stripe_customer_id"]

        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url="https://chiploop-saas.vercel.app/?portal=success"
        )

        return {"url": session.url}

    except Exception as e:
        return {"error": str(e)}


# ---------- Agent Streaming Endpoints ----------
@app.get("/spec_agent")
async def spec_agent_stream(prompt: str):
    return await stream_agent_response("Spec2RTL", prompt, phase="spec_agent")

@app.get("/rtl_agent")
async def rtl_agent_stream(prompt: str):
    return await stream_agent_response("Spec2RTL", prompt, phase="rtl_agent")

@app.get("/verify_agent")
async def verify_agent_stream(prompt: str):
    return await stream_agent_response("Spec2RTL", prompt, phase="verify_agent")

# ---------- Existing run_ai Endpoint ----------
@app.post("/run_ai")
async def run_ai(prompt: str):
    try:
        if not USE_LOCAL_OLLAMA:
            completion = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                timeout=30,
            )
            return {
                "backend": "Portkey/OpenAI",
                "output": completion.choices[0].message.content,
            }
    except Exception as e:
        logger.warning(f"[⚠️ Portkey failed, falling back to Ollama] {e}")

    try:
        payload = {"model": "llama3", "prompt": prompt}
        r = requests.post(OLLAMA_URL, json=payload, stream=True, timeout=60)
        r.raise_for_status()
        out = []
        for line in r.iter_lines():
            if not line:
                continue
            try:
                j = json.loads(line.decode())
                if "response" in j:
                    out.append(j["response"])
            except Exception:
                pass
        return {"backend": "Ollama", "output": "".join(out)}
    except Exception as e:
        logger.error(f"❌ Both Portkey and Ollama failed: {e}")
        return {"error": f"Both Portkey and Ollama failed: {str(e)}"}

@app.get("/run_ai")
async def run_ai_get(prompt: str):
    return await run_ai(prompt)

@app.post("/register_runner")
async def register_runner(request: Request):
    try:
        data = await request.json()
        runner_name = data.get("runner_name")
        email = data.get("email")
        token = data.get("token")

        if not (runner_name and email and token):
            return JSONResponse(
                {"status": "error", "message": "Missing fields"}, status_code=400
            )

        # Insert or update runner info (add status + last_seen)
        supabase.table("runners").upsert({
            "runner_name": runner_name,
            "email": email,
            "token": token,
            "status": "idle",
            "last_seen": datetime.utcnow().isoformat()
        }).execute()

        logger.info(f"✅ Runner registered: {runner_name}")
        return JSONResponse({"status": "success", "runner": runner_name})

    except Exception as e:
        logger.error(f"❌ register_runner failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/upload_results")
async def upload_results(request: Request):
    """
    Called by ChipRunner after job completion (simulation + coverage).
    Updates workflow logs, artifacts, and marks the runner idle.
    """
    try:
        data = await request.json()
        workflow_id = data.get("workflow_id")
        logs = data.get("logs", "")
        artifacts = data.get("artifacts", {})
        status = data.get("status", "completed")
        runner_name = data.get("runner_name")

        # 🧠 Update workflow record with logs and artifacts
        supabase.table("workflows").update({
            "status": status,
            "logs": logs,
            "artifacts": artifacts
        }).eq("id", workflow_id).execute()

        # 🧩 Mark runner as idle (optional, good housekeeping)
        if runner_name:
            supabase.table("runners").update({
                "status": "idle",
                "current_job": None,
                "last_seen": datetime.utcnow().isoformat()
            }).eq("runner_name", runner_name).execute()

        # 🧾 Log to server output
        logger.info(f"✅ Results uploaded for workflow {workflow_id}")
        logger.info(f"📦 Artifacts: {list(artifacts.keys()) if artifacts else 'None'}")

        # 🪶 Append a note to the workflow log
        update_workflow_log(workflow_id, "✅ Simulation + Coverage results uploaded successfully.\n")

        return JSONResponse({
            "status": "success",
            "workflow_id": workflow_id,
            "message": "Results uploaded and workflow updated successfully."
        })

    except Exception as e:
        logger.error(f"❌ Error in /upload_results: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500
        )



@app.get("/get_job")
async def get_job(runner: str):
    """
    Runner polls this endpoint to get the next available job.
    If no job is available, it just updates its last_seen timestamp.
    """
    try:
        # Look for any queued workflow
        res = supabase.table("workflows").select("*").eq("status", "queued").limit(1).execute()
        if not res.data:
            # No jobs — update runner heartbeat
            supabase.table("runners").update({
                "status": "idle",
                "last_seen": datetime.utcnow().isoformat()
            }).eq("runner_name", runner).execute()
            return {"job": None}

        job = res.data[0]
        workflow_id = job["id"]

        # Assign the job to this runner
        supabase.table("workflows").update({
            "status": "queued",
            "runner_assigned": None,
            "phase": "simulation",
            "updated_at": datetime.utcnow().isoformat()
        }).eq("id", workflow_id).execute()

        # Mark runner busy
        supabase.table("runners").update({
            "status": "busy",
            "last_seen": datetime.utcnow().isoformat()
        }).eq("runner_name", runner).execute()

        logger.info(f"🎯 Job {workflow_id} assigned to runner {runner}")

        return {
            "job": {
                "workflow_id": workflow_id,
                "design_dir": f"backend/workflows/{workflow_id}",
                "top_module": job.get("top_module", "tb_counter_4b")
            }
        }

    except Exception as e:
        logger.error(f"❌ Error in /get_job: {e}")
        return {"job": None, "error": str(e)}


@app.post("/heartbeat")
async def heartbeat(request: Request):
    """
    Simple heartbeat to keep runner alive and update its last_seen timestamp.
    """
    try:
        data = await request.json()
        runner_name = data.get("runner_name")
        status = data.get("status", "idle")

        if not runner_name:
            return JSONResponse({"status": "error", "message": "Missing runner_name"}, status_code=400)

        supabase.table("runners").update({
            "status": status,
            "last_seen": datetime.utcnow().isoformat()
        }).eq("runner_name", runner_name).execute()

        logger.info(f"❤️ Heartbeat from {runner_name} ({status})")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Heartbeat error: {e}")
        return {"status": "error", "message": str(e)}, 500

@app.get("/download_artifacts/{workflow_id}")
async def download_artifacts(workflow_id: str):
    """
    Returns the Verilog, testbench, and spec files for a given workflow.
    """
    base_path = Path(f"backend/workflows/{workflow_id}")
    files = {}
    for file in base_path.glob("*"):
        if file.is_file():
            files[file.name] = file.read_text()
    return JSONResponse(files)




