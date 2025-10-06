from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, Depends,Query
from pydantic import BaseModel
from typing import List, Dict
from fastapi.middleware.cors import CORSMiddleware
import json
from fastapi.responses import FileResponse
import jwt
import os
import stripe
import importlib.util
import logging
from supabase import create_client, Client
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
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
    workflow_row = supabase.table("workflows").insert({
        "user_id": user.get("sub"),
        "name": "Digital Loop run",
        "status": "running",
        "logs": "",
        "artifacts": {}
    }).execute()
    workflow_id = workflow_row.data[0]["id"]

    logger.info("🚀 run_workflow called")
    logger.info(f"Authenticated user: {user.get('sub') if user else '❌ none'}")
    logger.info(f"workflow raw: {workflow[:200] if workflow else '❌ missing'}")
    logger.info(f"spec_text: {spec_text}")
    logger.info(f"file: {file.filename if file else '❌ none'}")

    artifact_dir = f"artifacts/{user.get('sub')}/{workflow_id}"
    os.makedirs(artifact_dir, exist_ok=True)

    try:
        logger.info("🚀 /run_workflow called")
        data = json.loads(workflow)
        state = {}

        # --- uploaded file goes into artifact_dir ---
        if file:
            contents = await file.read()
            upload_path = os.path.join(artifact_dir, file.filename)
            with open(upload_path, "wb") as f:
                f.write(contents)
            state["uploaded_file"] = upload_path
            logger.info(f"📁 File uploaded: {upload_path}")

        # --- spec text saved into artifact_dir ---
        if spec_text:
            state["spec"] = spec_text
            spec_file = os.path.join(artifact_dir, "spec.txt")
            with open(spec_file, "w") as f:
                f.write(spec_text)
            logger.info("📝 Spec text provided and saved")

        results: Dict[str, str] = {}
        artifacts: Dict[str, Dict[str, str]] = {}

        for node in data.get("nodes", []):
            label = node.get("label")
            func = AGENT_FUNCTIONS.get(label)
            if func:
                try:
                    state = func(state)
                    results[label] = state.get("status", "✅ Done")

                    # save agent artifact if present
                    art_path = None
                    if state.get("artifact"):
                        safe_label = label.replace(" ", "_").replace("📘", "").replace("💻", "").replace("🛠", "")
                        art_path = os.path.join(artifact_dir, f"{safe_label}.txt")
                        with open(art_path, "w") as f:
                            f.write(state["artifact"] or "")

                    artifacts[label] = {
                        "artifact": f"/{art_path}" if art_path else None,
                        "artifact_log": state.get("artifact_log"),
                    }
                    logger.info(f"✅ Agent executed: {label}")
                except Exception as agent_err:
                    results[label] = f"❌ Error: {str(agent_err)}"
                    logger.error(f"Agent {label} failed: {agent_err}")
            else:
                results[label] = "⚠ No implementation yet."
                logger.warning(f"No function found for agent: {label}")

        supabase.table("workflows").update({
            "status": "success",
            "logs": json.dumps(results),
            "artifacts": artifacts
        }).eq("id", workflow_id).execute()

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
async def create_checkout_session(request: Request):
    try:
        data = await request.json()

        #  Try to extract user_id from request body if provided
        user_id = data.get("user_id", "unknown")

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
                "user_id": user_id,  # Safe even if not provided
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
        return {"error": "Invalid payload"}
    except stripe.error.SignatureVerificationError:
        return {"error": "Invalid signature"}

    # --- Handle Stripe events ---
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"].get("user_id")
        customer_id = session.get("customer")

        if user_id and customer_id:
            supabase.table("workflows").update({
                "subscription_status": "basic",
                "stripe_customer_id": customer_id
            }).eq("user_id", user_id).execute()

    elif event["type"] == "customer.subscription.updated":
        subscription = event["data"]["object"]
        user_id = subscription["metadata"].get("user_id")

        if user_id:
            supabase.table("workflows").update({
                "subscription_status": subscription["status"]
            }).eq("user_id", user_id).execute()

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        user_id = subscription["metadata"].get("user_id")

        if user_id:
            supabase.table("workflows").update({
                "subscription_status": "free"
            }).eq("user_id", user_id).execute()

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




