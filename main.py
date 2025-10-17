# main.py
# ChipLoop backend (FastAPI) — loop-aware workflows + per-run logging

import os
import json
import uuid
import traceback
import httpx
import time;time.sleep(0.2)
from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Query, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import logging
logger = logging.getLogger("chiploop")
logging.basicConfig(level=logging.INFO)

# ---------------- Supabase client ----------------
try:
    from supabase import create_client, Client  # supabase-py v2
except ImportError:
    raise RuntimeError("Please install supabase-py v2: pip install supabase")

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/NEXT_PUBLIC_SUPABASE_ANON_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------- Auth helper (JWT) ----------------
# If you already have a stronger verify function, keep that and delete this one.
import jwt  # PyJWT

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

def verify_token(request: Request) -> Dict[str, Any]:
    """Best-effort JWT decode from Authorization: Bearer <token>. Fallback to anonymous."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    if token and SUPABASE_JWT_SECRET:
        try:
            payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"])
            return payload  # should contain sub/user id
        except Exception as e:
            logger.warning(f"JWT decode failed, continuing as anonymous: {e}")
    return {"sub": "anonymous"}

# ---------------- FastAPI app ----------------
app = FastAPI(title="ChipLoop API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================================
# ✅ DIGITAL AGENTS (labels must match the frontend exactly)
# ==========================================================
from agents.digital.digital_testbench_agent_uvm import run_agent as digital_testbench_agent_uvm
from agents.digital.digital_arch_doc_agent import run_agent as digital_arch_doc_agent
from agents.digital.digital_integration_doc_agent import run_agent as digital_integration_doc_agent
from agents.digital.digital_testcase_agent import run_agent as digital_testcase_agent
from agents.digital.digital_assertion_agent import run_agent as digital_assertion_agent
from agents.digital.digital_covergroup_agent import run_agent as digital_covergroup_agent
from agents.digital.digital_spec_agent import run_agent as digital_spec_agent
from agents.digital.digital_rtl_agent import run_agent as digital_rtl_agent
from agents.digital.digital_simulation_agent import run_agent as digital_simulation_agent
from agents.digital.digital_coverage_agent import run_agent as digital_coverage_agent
from agents.digital.digital_optimizer_agent import run_agent as digital_optimizer_agent

DIGITAL_AGENT_FUNCTIONS: Dict[str, Any] = {
    "Digital Spec Agent": digital_spec_agent,
    "Digital RTL Agent": digital_rtl_agent,
    "Digital Sim Agent": digital_simulation_agent,          # ← exact label used by UI
    "Digital Coverage Agent": digital_coverage_agent,
    "Digital Opitmizer Agent": digital_optimizer_agent,     # ← keep typo to match existing label
    "Digital Testbench Agent": digital_testbench_agent_uvm,
    "Digital Arch Doc Agent": digital_arch_doc_agent,
    "Digital Integration Doc Agent": digital_integration_doc_agent,
    "Digital Testcase Agent": digital_testcase_agent,
    "Digital Assertion Agent": digital_assertion_agent,
    "Digital CoverGroup Agent": digital_covergroup_agent,
}

# ==========================================================
# ✅ ANALOG AGENTS
# ==========================================================
from agents.analog.analog_spec_agent import run_agent as analog_spec_agent
from agents.analog.analog_netlist_agent import run_agent as analog_netlist_agent
from agents.analog.analog_sim_agent import run_agent as analog_sim_agent
from agents.analog.analog_result_agent import run_agent as analog_result_agent

ANALOG_AGENT_FUNCTIONS: Dict[str, Any] = {
    "Analog Spec Agent": analog_spec_agent,
    "Analog Netlist Agent": analog_netlist_agent,
    "Analog Sim Agent": analog_sim_agent,
    "Analog Result Agent": analog_result_agent,
}

# ==========================================================
# ✅ EMBEDDED AGENTS
# ==========================================================
from agents.embedded.embedded_spec_agent import run_agent as embedded_spec_agent
from agents.embedded.embedded_code_agent import run_agent as embedded_code_agent
from agents.embedded.embedded_sim_agent import run_agent as embedded_sim_agent
from agents.embedded.embedded_result_agent import run_agent as embedded_result_agent

EMBEDDED_AGENT_FUNCTIONS: Dict[str, Any] = {
    "Embedded Spec Agent": embedded_spec_agent,
    "Embedded Code Agent": embedded_code_agent,
    "Embedded Sim Agent": embedded_sim_agent,
    "Embedded Result Agent": embedded_result_agent,
}

SYSTEM_AGENT_FUNCTIONS: Dict[str,Any] = {
    "Digital Spec Agent": digital_spec_agent,
    "Digital RTL Agent": digital_rtl_agent,
    "Digital Sim Agent": digital_simulation_agent,          # ← exact label used by UI
    "Digital Coverage Agent": digital_coverage_agent,
    "Digital Opitmizer Agent": digital_optimizer_agent,     # ← keep typo to match existing label
    "Digital Testbench Agent": digital_testbench_agent_uvm,
    "Digital Arch Doc Agent": digital_arch_doc_agent,
    "Digital Integration Doc Agent": digital_integration_doc_agent,
    "Digital Testcase Agent": digital_testcase_agent,
    "Digital Assertion Agent": digital_assertion_agent,
    "Digital CoverGroup Agent": digital_covergroup_agent,
    "Analog Spec Agent": analog_spec_agent,
    "Analog Netlist Agent": analog_netlist_agent,
    "Analog Sim Agent": analog_sim_agent,
    "Analog Result Agent": analog_result_agent,
    "Embedded Spec Agent": embedded_spec_agent,
    "Embedded Code Agent": embedded_code_agent,
    "Embedded Sim Agent": embedded_sim_agent,
    "Embedded Result Agent": embedded_result_agent,      
}

# ==========================================================
# 🧠 UNIFIED + CUSTOM REGISTRY
# ==========================================================

AGENT_FUNCTIONS: Dict[str, Dict[str, Any]] = {
    "digital": DIGITAL_AGENT_FUNCTIONS,
    "analog": ANALOG_AGENT_FUNCTIONS,
    "embedded": EMBEDDED_AGENT_FUNCTIONS,
    "system": SYSTEM_AGENT_FUNCTIONS
}

# Dynamically load user-created agents as modules under `agents/` (optional)
import importlib, pkgutil, agents
AGENT_REGISTRY: Dict[str, Any] = {}  # custom/marketplace registry

def load_custom_agents():
    global AGENT_REGISTRY
    for _, name, _ in pkgutil.iter_modules(agents.__path__):
        if name not in AGENT_REGISTRY:
            try:
                module = importlib.import_module(f"agents.{name}")
                if hasattr(module, "run_agent"):
                    AGENT_REGISTRY[name] = module.run_agent
                    logger.info(f"⚡ Custom agent loaded: {name}")
            except Exception as e:
                logger.warning(f"⚠️ Could not load custom agent {name}: {e}")

load_custom_agents()

# ==========================================================
# ---------- Logging Helpers (ID-based) ----------
# ==========================================================

def append_log_workflow(workflow_id: str, line: str, status: Optional[str] = None,
                        phase: Optional[str] = None, artifacts: Optional[dict] = None):
    """
    Append a line to workflows.logs by workflow ID, and optionally update status/phase/artifacts.
    """
    try:
        row = supabase.table("workflows").select("logs").eq("id", workflow_id).single().execute()
        current = (row.data or {}).get("logs") or ""
        new_logs = (current + ("\n" if current else "") + line).strip()
        update = {"logs": new_logs, "updated_at": datetime.utcnow().isoformat()}
        if status: update["status"] = status
        if phase:  update["phase"] = phase
        if artifacts is not None: update["artifacts"] = artifacts
        supabase.table("workflows").update(update).eq("id", workflow_id).execute()
    except Exception as e:
        logger.warning(f"⚠️ append_log_workflow failed: {e}")

def append_log_run(run_id: str, line: str, status: Optional[str] = None,
                   artifacts_path: Optional[str] = None):
    """
    Append a line to runs.logs by run ID, and optionally update status and artifacts_path.
    """
    try:
        row = supabase.table("runs").select("logs").eq("id", run_id).single().execute()
        current = (row.data or {}).get("logs") or ""
        new_logs = (current + ("\n" if current else "") + line).strip()
        update = {"logs": new_logs}
        if status: update["status"] = status
        if artifacts_path is not None: update["artifacts_path"] = artifacts_path
        supabase.table("runs").update(update).eq("id", run_id).execute()
    except Exception as e:
        logger.warning(f"⚠️ append_log_run failed: {e}")

# ==========================================================
# ---------- Routes ----------
# ==========================================================

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

@app.get("/runs/recent")
def recent_runs(request: Request, limit: int = Query(10, ge=1, le=50)):
    user = verify_token(request)
    res = (
        supabase.table("runs")
        .select("id, workflow_id, loop_type, status, created_at, completed_at, artifacts_path")
        .eq("user_id", user.get("sub"))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data

@app.get("/stats")
def stats():
    wf = supabase.table("workflows").select("id", count="exact").execute()
    run = supabase.table("runs").select("id", count="exact").execute()
    return {
        "workflows": wf.count if hasattr(wf, "count") else None,
        "runs": run.count if hasattr(run, "count") else None,
    }

@app.post("/run_workflow")
async def run_workflow(
    request: Request,
    background_tasks: BackgroundTasks,
    workflow: str = Form(...),
    file: UploadFile = File(None),
    spec_text: str = Form(None)
):
    """
    Starts a workflow run:
      - Reads loop_type from payload
      - Creates rows in workflows + runs
      - Queues background execution
    """
    try:
        user = verify_token(request)
        user_id = user.get("sub") if user and user.get("sub") and user.get("sub") != "anonymous" else None
        workflow_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        data = json.loads(workflow)
        # payload contains nodes with exact backend "label"
        loop_type = (data.get("loop_type") or "digital").lower().strip()

        supabase.table("workflows").insert({
           "id": workflow_id,
           "user_id": user_id,
           "name": f"{loop_type.capitalize()} Loop Run",
           "status": "running",
           "phase": "queued",
           "logs": "🚀 Workflow queued.",
           "created_at": now,
           "updated_at": now,
           "artifacts": {},
           "loop_type": loop_type,
           "definitions": data
        }).execute()

        # Prepare artifact dir
        user_folder = str(user_id or "anonymous") 
        artifact_dir = os.path.join("artifacts", user_folder, workflow_id)
        os.makedirs(artifact_dir, exist_ok=True)

        # Save uploaded file (optional)
        upload_path = None
        if file is not None:
            contents = await file.read()
            upload_path = os.path.join(artifact_dir, file.filename)
            with open(upload_path, "wb") as f:
                f.write(contents)

        # Save spec text (optional)
        if spec_text:
            with open(os.path.join(artifact_dir, "spec.txt"), "w") as f:
                f.write(spec_text)

        # Insert per-run row (clean execution history)
        supabase.table("runs").insert({
            "id": run_id,
            "user_id": user_id,
            "workflow_id": workflow_id,
            "loop_type": loop_type,
            "status": "running",
            "logs": "🚀 Run started.",
            "artifacts_path": artifact_dir,
            "created_at": now
        }).execute()

        append_log_workflow(workflow_id, f"📘 Loop: {loop_type}", phase="start")
        append_log_run(run_id, f"📘 Loop: {loop_type}")
        time.sleep(0.2)

        # Queue background execution
        background_tasks.add_task(
            execute_workflow_background,
            workflow_id,
            run_id,
            user_id,
            loop_type,
            json.dumps(data),
            spec_text,
            upload_path,
            artifact_dir
        )

        return JSONResponse({"workflow_id": workflow_id, "run_id": run_id, "loop_type": loop_type, "status": "queued"})

    except Exception as e:
        logger.error(f"❌ run_workflow failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================================
# ---------- Background executor ----------
# ==========================================================

def execute_workflow_background(
    workflow_id: str,
    run_id: str,
    user_id: str,
    loop_type: str,
    workflow_json: str,
    spec_text: Optional[str],
    upload_path: Optional[str],
    artifact_dir: str,
):
    """
    Executes the workflow with loop-aware agent resolution and dual logging (workflows + runs).
    - Exact label resolution against DIGITAL/ANALOG/EMBEDDED maps
    - Falls back to AGENT_REGISTRY (custom agents)
    - Queues at *Sim Agent* phase for external runner (any loop)
    """
    try:
        os.makedirs(artifact_dir, exist_ok=True)
        data = json.loads(workflow_json)

        if loop_type not in AGENT_FUNCTIONS:
           append_log_workflow(workflow_id, f"⚠️ Unknown loop_type={loop_type}, defaulting to digital.")
           loop_type = "digital"

        loop_map = AGENT_FUNCTIONS.get(loop_type, DIGITAL_AGENT_FUNCTIONS)

        # Merge with dynamic/custom agents
        agent_map = dict(loop_map)
        agent_map.update(AGENT_REGISTRY)
        missing = [n["label"] for n in nodes if n["label"] not in agent_map]
        if missing:
          append_log_workflow(workflow_id, f"⚠️ Missing agent implementations: {', '.join(missing)}")

        append_log_workflow(workflow_id, "⚡ Executing workflow agents ...")
        append_log_run(run_id, "⚡ Executing workflow agents ...")
        time.sleep(0.2)

        nodes = data.get("nodes", []) or []
        for node in nodes:
            label = (node or {}).get("label", "")
            step = label or "agent"
            msg = f"⚙️ Running {step} ..."
            logger.info(msg)
            append_log_workflow(workflow_id, msg)
            append_log_run(run_id, msg)
            time.sleep(0.2)

            # Queue to external runner at Simulation phase (for any loop)
            if " sim agent" in step.lower():
                # Mark workflow queued for simulation runner
                supabase.table("workflows").update({
                    "status": "queued",
                    "phase": "simulation",
                    "runner_assigned": None,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", workflow_id).execute()

                append_log_workflow(workflow_id, "🟡 Queued for ChipRunner (Simulation phase).", phase="simulation")
                append_log_run(run_id, "🟡 Queued for ChipRunner (Simulation phase).", status="queued", artifacts_path=artifact_dir)
                time.sleep(0.2)
                return  # external runner will pick up

            # Resolve function
            fn = agent_map.get(step)
            if not fn:
                msg = f"❌ No agent implementation found for: {step}"
                append_log_workflow(workflow_id, msg)
                append_log_run(run_id, msg)
                time.sleep(0.2)
                continue

            try:
                # Execute agent function; pass a unified state
                state = {"workflow_id": workflow_id, "run_id": run_id, "artifact_dir": artifact_dir}
                if upload_path: state["uploaded_file"] = upload_path
                if spec_text:   state["spec"] = spec_text

                result = fn(state)  # your agents accept a dict 'state'

                # Save artifacts if provided
                if isinstance(result, dict):
                    label_safe = step.replace(" ", "_")
                    out_path = None
                    if result.get("artifact") is not None:
                        os.makedirs(artifact_dir, exist_ok=True)
                        out_path = os.path.join(artifact_dir, f"{label_safe}.txt")
                        with open(out_path, "w") as f:
                            f.write(str(result.get("artifact") or ""))

                    # Persist artifacts metadata on workflow row
                    row = supabase.table("workflows").select("artifacts").eq("id", workflow_id).single().execute()
                    artifacts = (row.data or {}).get("artifacts") or {}
                    artifacts[step] = {
                        "artifact": (f"/{out_path}" if out_path else None),
                        "artifact_log": result.get("artifact_log"),
                        "log": result.get("log"),
                        "code": result.get("code"),
                    }
                    supabase.table("workflows").update({"artifacts": artifacts}).eq("id", workflow_id).execute()

                msg = f"✅ {step} done"
                logger.info(msg)
                append_log_workflow(workflow_id, msg)
                append_log_run(run_id, msg)
                time.sleep(0.2)

            except Exception as agent_err:
                err = f"❌ {step} failed: {agent_err}"
                append_log_workflow(workflow_id, err)
                append_log_run(run_id, err)
                time.sleep(0.2)

        append_log_workflow(workflow_id, "🎉 Workflow complete", status="completed", phase="done")
        append_log_run(run_id, "🎉 Run complete", status="completed")
        time.sleep(0.2)

    except Exception as e:
        err = f"❌ Workflow crashed: {e}\n{traceback.format_exc()}"
        append_log_workflow(workflow_id, err, status="failed", phase="error")
        append_log_run(run_id, err, status="failed")
        time.sleep(0.2)
from fastapi import Body
from fastapi.responses import FileResponse
from pathlib import Path

# ---------- Helpers ----------
def _artifacts_dir_for_workflow(workflow_id: str) -> Path:
    # Prefer the latest run row (artifacts_path), fallback to canonical path using workflows.user_id
    try:
        r = supabase.table("runs").select("artifacts_path").eq("workflow_id", workflow_id).order("created_at", desc=True).limit(1).execute()
        path = (r.data or [{}])[0].get("artifacts_path")
        if path and Path(path).exists():
            return Path(path)
    except Exception:
        pass

    wf = supabase.table("workflows").select("user_id").eq("id", workflow_id).single().execute()
    user_id = (wf.data or {}).get("user_id") or "anonymous"
    p = Path("artifacts") / user_id / workflow_id
    return p

def _touch_runner_seen(runner_name: str):
    try:
        supabase.table("runners").update({"last_seen": datetime.utcnow().isoformat()}).eq("runner_name", runner_name).execute()
    except Exception:
        pass

# ---------- 1) Register runner ----------
@app.post("/register_runner")
def register_runner(payload: dict = Body(...)):
    """
    Body: { runner_name, email, token }
    Upserts runner and marks online.
    """
    runner = {
        "runner_name": payload.get("runner_name"),
        "email": payload.get("email"),
        "token": payload.get("token"),
        "status": "idle",
        "last_seen": datetime.utcnow().isoformat(),
    }
    if not runner["runner_name"]:
        raise HTTPException(status_code=400, detail="runner_name required")
    # upsert by runner_name
    supabase.table("runners").upsert(runner, on_conflict="runner_name").execute()
    return {"ok": True, "runner": runner}

# ---------- 2) Get a queued job (simulation) ----------
@app.get("/get_job")
def get_job(runner: str):
    """
    Query: ?runner=<runner_name>
    Finds a workflow queued for 'simulation' phase and assigns it.
    Returns: { job: { workflow_id, loop_type, top_module?, ... } } or { job: null }
    """
    if not runner:
        raise HTTPException(status_code=400, detail="runner required")

    _touch_runner_seen(runner)

    # Find first queued workflow for simulation
    q = (
        supabase.table("workflows")
        .select("id, user_id, loop_type, params")
        .eq("status", "queued")
        .eq("phase", "simulation")
        .is_("runner_assigned", None)
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    rows = q.data or []
    if not rows:
        return {"job": None}

    wf = rows[0]
    workflow_id = wf["id"]
    loop_type = (wf.get("loop_type") or "digital")
    params = wf.get("params") or ""
    # Allow a 'top_module=<name>' hint in workflows.params
    top_module = None
    if isinstance(params, str) and "top_module" in params:
        # naive parse "top_module=<name>" from params text
        for token in params.replace(",", " ").split():
            if token.startswith("top_module="):
                top_module = token.split("=", 1)[1].strip()

    # Assign runner
    supabase.table("workflows").update({
        "runner_assigned": runner,
        "status": "running",
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", workflow_id).execute()

    supabase.table("runners").upsert({
        "runner_name": runner,
        "status": "busy",
        "current_job": workflow_id,
        "last_seen": datetime.utcnow().isoformat()
    }, on_conflict="runner_name").execute()

    job = {
        "workflow_id": workflow_id,
        "loop_type": loop_type,
        "top_module": top_module or "tb_counter_4b",
    }
    return {"job": job}

# ---------- 3) Upload results from runner ----------
@app.post("/upload_results")
def upload_results(payload: dict = Body(...)):
    """
    Body: { workflow_id, status, logs, artifacts?, runner_name }
    - Appends logs
    - Updates workflow status/phase
    - Marks run completed
    """
    workflow_id = payload.get("workflow_id")
    status = (payload.get("status") or "completed").lower()
    logs = payload.get("logs") or ""
    artifacts = payload.get("artifacts") or {}
    runner_name = payload.get("runner_name")

    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id required")

    # Append logs and finalize workflow
    append_log_workflow(workflow_id, logs, status=status, phase="done")

    # Clear runner assignment and mark status
    supabase.table("workflows").update({
        "status": status,
        "phase": "done",
        "runner_assigned": None,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", workflow_id).execute()

    # Mark latest run completed
    try:
        r = supabase.table("runs").select("id").eq("workflow_id", workflow_id).order("created_at", desc=True).limit(1).execute()
        if (r.data or []):
            run_id = r.data[0]["id"]
            append_log_run(run_id, f"📦 Runner '{runner_name}' uploaded results.", status=status)
            supabase.table("runs").update({
                "status": status,
                "completed_at": datetime.utcnow().isoformat()
            }).eq("id", run_id).execute()
    except Exception as e:
        logger.warning(f"runs finalize failed: {e}")

    # Optionally merge artifacts metadata into workflow row
    try:
        row = supabase.table("workflows").select("artifacts").eq("id", workflow_id).single().execute()
        cur = (row.data or {}).get("artifacts") or {}
        cur["runner_upload"] = artifacts
        supabase.table("workflows").update({"artifacts": cur}).eq("id", workflow_id).execute()
    except Exception as e:
        logger.warning(f"artifacts merge failed: {e}")

    if runner_name:
        supabase.table("runners").upsert({
            "runner_name": runner_name,
            "status": "idle",
            "current_job": None,
            "last_seen": datetime.utcnow().isoformat()
        }, on_conflict="runner_name").execute()

    return {"ok": True}

# ---------- 4) List artifacts for a workflow ----------
@app.get("/list_artifacts/{workflow_id}")
def list_artifacts(workflow_id: str):
    base = _artifacts_dir_for_workflow(workflow_id)
    files = []
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file():
                files.append(str(p.relative_to(base)))
    return {"files": files, "base": str(base)}

# ---------- 5) Download a specific artifact file ----------
@app.get("/download_artifacts/{workflow_id}/{rel_path:path}")
def download_artifact(workflow_id: str, rel_path: str):
    base = _artifacts_dir_for_workflow(workflow_id)
    # prevent path traversal
    requested = (base / rel_path).resolve()
    if not str(requested).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not requested.exists() or not requested.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(requested))
