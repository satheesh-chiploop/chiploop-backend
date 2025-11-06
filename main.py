# main.py
# ChipLoop backend (FastAPI) — loop-aware workflows + per-run logging

import os
import json
import uuid
import traceback
import httpx
import re
import time;time.sleep(0.2)
from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Query, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from agent_capabilities import AGENT_CAPABILITIES
from utils.graph_utils import build_capability_graph, serialize_graph
from fastapi.responses import JSONResponse
from utils.llm_utils import run_llm_fallback
from utils.audio_utils import transcribe_audio
from utils.notion_utils import append_to_notion, get_or_create_notion_page
from fastapi import WebSocket
import asyncio
from planner.auto_fill_missing import auto_fill_missing_fields


import logging
logger = logging.getLogger("chiploop")
logging.basicConfig(level=logging.INFO)

# ---------------- Supabase client ----------------
try:
    from supabase import create_client, Client  # supabase-py v2
except ImportError:
    raise RuntimeError("Please install supabase-py v2: pip install supabase")

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # <<< REMOVE fallback

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ---------------- Auth helper (JWT) ----------------
# If you already have a stronger verify function, keep that and delete this one.
import jwt  # PyJWT

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

from notion_client import Client as NotionClient
import asyncio

# === Notion + Supabase setup ===
notion = NotionClient(auth=os.getenv("NOTION_API_KEY"))


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
from agents.system.system_workflow_agent import run_agent as system_workflow_agent

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
    "System Workflow Agent": system_workflow_agent,     
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

        if loop_type == "system":
            has_validation = any(
                n.get("label") == "System Workflow Agent"
                for n in (data.get("nodes") or [])
            )
            if not has_validation:
                logger.info("🧩 Auto-appending System Workflow Agent as final step for System Loop.")
                # Append as a node for execution
                data["nodes"].append({"label": "System Workflow Agent"})
                append_log_workflow(workflow_id, "🧩 Added System Workflow Agent as final validation step.")

        # Merge with dynamic/custom agents
        agent_map = dict(loop_map)
        agent_map.update(AGENT_REGISTRY)

        append_log_workflow(workflow_id, "⚡ Executing workflow agents ...")
        append_log_run(run_id, "⚡ Executing workflow agents ...")
        time.sleep(0.2)

        nodes = data.get("nodes", []) or []
        missing = [n["label"] for n in nodes if n["label"] not in agent_map]
        if missing:
          append_log_workflow(workflow_id, f"⚠️ Missing agent implementations: {', '.join(missing)}")
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

@app.get("/list_agents")
async def list_agents():
    G = build_capability_graph(AGENT_CAPABILITIES)
    return {
        "status": "ok",
        "agents": AGENT_CAPABILITIES,
        "graph": serialize_graph(G)
    }

from planner.ai_work_planner import plan_workflow


@app.post("/plan_workflow")
async def plan_workflow_api(request: Request):
    data = await request.json()
    user_prompt = data.get("prompt", "")
    workflow_id = data.get("workflow_id", "manual_plan")
    structured_spec_final = data.get("structured_spec_final")
    plan = await plan_workflow(
       user_prompt,
       structured_spec_final=structured_spec_final
    )
    return {"status": "ok", "plan": plan}


# ==========================================================
#  🔥 Memory-Aware Planner + Spec Analyzer Integration
# ==========================================================

from utils.spec_analyzer import analyze_spec_text
from utils.domain_classifier import detect_domain
from analyze.digital.analyze_spec_digital import analyze_spec_digital, finalize_spec_digital


# backend/main.py  (inside /analyze_spec)
from utils.domain_classifier import detect_domain
from utils.spec_analyzer import analyze_spec_text  # your existing analyzer
from analyze.digital.analyze_spec_digital import analyze_spec_digital, finalize_spec_digital

@app.post("/analyze_spec")
async def analyze_spec(request: Request):
    data = await request.json()
    goal = data.get("goal", "")
    voice_summary = data.get("voice_summary", "")
    user_id = data.get("user_id", "anonymous")

    combined = (voice_summary or "") + "\n" + (goal or "")
    domain_info = await detect_domain(combined)
    domain = (domain_info.get("domain") or "mixed").lower()
    conf = domain_info.get("confidence") or {}
    max_conf = max(conf.values()) if conf else 0.0

    logger = logging.getLogger(__name__)

    # after domain_info = await detect_domain(...)

    logger.info("AnalyzeSpec: domain=%s conf=%s reason=%s",
            domain_info.get("domain"),
            domain_info.get("confidence"),
            domain_info.get("reason"))


    if domain == "digital" and max_conf >= 0.70:
        result = await analyze_spec_digital(goal, voice_summary, user_id)
    else:
        # keep current behavior for analog/embedded/mixed for now
        result = await analyze_spec_text(goal, voice_summary, user_id)

    return {
        "domain_info": domain_info,
        "result": result
    }


# ---------- /plan_workflow_memory ----------
@app.post("/plan_workflow_memory")
async def plan_workflow_memory(request: Request):
    """
    Plans workflow using memory from user_memory and agent_memory.
    """
    data = await request.json()
    goal = data.get("goal", "")
    user_id = data.get("user_id", "anonymous")

    # Retrieve memory context
    user_mem = supabase.table("user_memory").select("*").eq("user_id", user_id).execute()
    user_data = user_mem.data[0] if user_mem.data else {}
    agent_mem = supabase.table("agent_memory").select("*").execute()
    agent_data = agent_mem.data or []

    memory_context = f"""
    Recent goals: {user_data.get('recent_goals', [])}
    Frequent agents: {user_data.get('frequent_agents', [])}
    Known agent types: {[a['agent_name'] for a in agent_data[:8]]}
    """

    planner_prompt = f"""
    You are ChipLoop's Memory-Aware Planner.
    Use the memory below to propose a workflow for goal: "{goal}"

    Memory Context:
    {memory_context}

    Return only JSON:
    {{
      "loop_type": "<digital|analog|embedded|system>",
      "agents": ["Agent A", "Agent B", ...]
    }}
    """

    try:
        response = await run_llm_fallback(planner_prompt)
        plan = json.loads(response)

        # Update user memory (append goal + agents)
        supabase.table("user_memory").upsert({
            "user_id": user_id,
            "recent_goals": (user_data.get("recent_goals", []) + [goal])[-5:],
            "frequent_agents": list(set(user_data.get("frequent_agents", []) + plan.get("agents", []))),
            "updated_at": datetime.utcnow().isoformat(),
        }).execute()

        return {"status": "ok", "plan": plan}

    except Exception as e:
        logger.error(f"❌ Memory planner failed: {e}")
        return {"status": "error", "message": str(e)}


# ---------- Memory Update Helper for Auto-Compose ----------
def update_agent_memory(agent_name: str, workflow_name: str):
    """Update agent usage and last_used_in list."""
    try:
        record = {
            "agent_name": agent_name,
            "last_used_in": [workflow_name],
            "updated_at": datetime.utcnow().isoformat()
        }
        supabase.table("agent_memory").upsert(record).execute()
    except Exception as e:
        logger.warning(f"⚠️ Failed to update agent memory for {agent_name}: {e}")

def fetch_memory_context(user_id: str):
    try:
        user_mem = supabase.table("user_memory").select("*").eq("user_id", user_id).execute()
        user_data = user_mem.data[0] if user_mem.data else {}
        agent_mem = supabase.table("agent_memory").select("agent_name, capability_tags").execute()
        agent_data = agent_mem.data or []

        return {
            "recent_goals": user_data.get("recent_goals", []),
            "frequent_agents": user_data.get("frequent_agents", []),
            "known_agents": [a["agent_name"] for a in agent_data],
        }
    except Exception as e:
        logger.warning(f"⚠️ fetch_memory_context failed: {e}")
        return {"recent_goals": [], "frequent_agents": [], "known_agents": []}


from planner.ai_agent_planner import plan_agent_with_memory

@app.post("/plan_agent")
async def plan_agent(request: Request):
    """
    Agentic Agent Planner:
    - Analyzes spec
    - Fetches user + agent memory
    - Returns proposed agent JSON
    """
    data = await request.json()
    goal = data.get("goal", "")
    user_id = data.get("user_id", "anonymous")

    from planner.ai_agent_planner import plan_agent_with_memory
    result = await plan_agent_with_memory(goal, user_id)
    return {"status": "ok", "agent_plan": result}

@app.post("/save_custom_agent")
async def save_custom_agent(request: Request):
    """
    Save AI-generated agent from planner into Supabase `agents` table.
    """
    try:
        data = await request.json()
        agent = data.get("agent", {})
        if not agent.get("agent_name"):
            return {"status": "error", "message": "agent_name missing"}

        record = {
            "agent_name": agent.get("agent_name"),
            "loop_type": agent.get("domain", "system"),
            "tool": "AI Agent Planner",
            "script_path": None,
            "description": agent.get("description", ""),
            "is_custom": True,
            "is_prebuilt": False,
            "is_marketplace": False,
        }

        res = supabase.table("agents").insert(record).execute()
        if hasattr(res, "data") and res.data:
            logger.info(f"💾 Saved new custom agent: {agent.get('agent_name')}")
            return {"status": "ok", "data": res.data}
        else:
            logger.warning(f"⚠️ Supabase returned empty response for {agent.get('agent_name')}")
            return {"status": "warning", "message": "Insert may not have completed"}

    except Exception as e:
        logger.error(f"❌ Failed to save custom agent: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/save_custom_workflow")
async def save_custom_workflow(request: Request):
    """
    Save a workflow (manual or AI-created) into Supabase.
    Automatically adds 'System Workflow Agent' if workflow spans multiple domains.
    Stores nodes, edges, goal, and summary for Canvas restore and dashboard preview.
    """
    try:
        data = await request.json()
        print("\n\n========== DEBUG SAVE_CUSTOM_WORKFLOW ==========")
        print("RAW data keys:", list(data.keys()))
        print("RAW data:", data)
        print("RAW workflow type:", type(data.get("workflow")))
        print("RAW workflow keys:", list(data.get("workflow", {}).keys()) if isinstance(data.get("workflow"), dict) else data.get("workflow"))
        print("===============================================\n\n")

    

        # Support both flat and nested payloads
        wf = data.get("workflow", {})
        body_user_id = data.get("user_id") or wf.get("user_id")

        # ✅ Step 2: extract from headers if body didn’t include
        headers = request.headers
        header_user_id = (
          headers.get("x-user-id")
          or headers.get("x-supabase-user-id")
          or headers.get("x-client-user-id")
        )
        # ✅ Step 3: Fallback to JWT (for logged-in Supabase users)
        jwt_user_id = None
        try:
          token_data = verify_token(request)
          jwt_user_id = token_data.get("sub")
        except Exception:
           pass

    # ✅ Step 4: Final priority (header > body > JWT > anonymous)
        auth_user_id = header_user_id or body_user_id or jwt_user_id or "anonymous"

        # ✅ Apply consistently to both data & workflow objects
        data["user_id"] = auth_user_id
        wf["user_id"] = auth_user_id
        user_id = auth_user_id
        logger.info(f"💾 Final resolved user_id={user_id}")
        
        name = (
          data.get("workflow", {}).get("workflow_name")
          or data.get("workflow", {}).get("name")
          or "Untitled Workflow"
        )

        name= str(name).strip()
        logger.info(f"💾 Normalized workflow name: '{name}' (type={type(name)})")
        
        goal = wf.get("goal", "")
        summary = wf.get("summary") or wf.get("data", {}).get("summary", "")
        loop_type = wf.get("loop_type", "system")
        nodes = wf.get("nodes") or wf.get("data", {}).get("nodes", [])
        edges = wf.get("edges") or wf.get("data", {}).get("edges", [])

        logger.info(f"📦 Saving workflow: name={name}, loop_type={loop_type}, nodes={len(nodes)}, edges={len(edges)}, user={user_id}")

        for node in nodes:
            if "data" not in node and "type" in node:
                node["data"] = {"backendLabel": node["type"]}
            elif "data" in node and "backendLabel" not in node["data"]:
                node["data"]["backendLabel"] = node.get("type", "Unknown")

        # --- Domain detection for auto system agent ---
        domains = set()
        for node in nodes:
            label = node.get("data", {}).get("backendLabel", "")
            if "digital" in label.lower():
                domains.add("digital")
            elif "analog" in label.lower():
                domains.add("analog")
            elif "embedded" in label.lower():
                domains.add("embedded")
            elif "system" in label.lower():
                domains.add("system")

        # ✅ Auto-append System Workflow Agent if multiple domains exist
        if len(domains) > 1 and not any("System Workflow Agent" in n.get("data", {}).get("backendLabel", "") for n in nodes):
            system_agent = {
                "id": f"system_validation_{len(nodes) + 1}",
                "type": "default",
                "data": {
                    "uiLabel": "System Workflow Agent",
                    "backendLabel": "System Workflow Agent",
                    "description": "Validates cross-domain integration.",
                },
                "position": {"x": 400, "y": 400},
            }
            nodes.append(system_agent)
            logger.info(f"🧩 Auto-added System Workflow Agent to {name}")

        # --- Prepare payload for Supabase ---
        payload = {
            "user_id": user_id if user_id not in ("anonymous", None) and len(user_id) == 36 else None,
            "name": name,
            "goal": goal,
            "summary": summary or f"Workflow for {goal[:80]}",
            "loop_type": loop_type,
            "definitions": {
              "nodes": nodes,
              "edges": edges,
            },
            "status": "saved",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        # --- Insert into Supabase ---
        result = supabase.table("workflows").insert(payload).execute()
        logger.info(f"💾 Workflow save payload user_id={user_id} (len={len(user_id) if user_id else 0})")

        logger.info(f"💾 Workflow saved: {name} | domains={list(domains)} | user={user_id or 'anonymous'}")
        return {"status": "ok", "data": result.data}

    except Exception as e:
        logger.error(f"❌ Failed to save workflow: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/auto_compose_workflow")
async def auto_compose_workflow(request: Request):
    """
    Auto-compose a workflow graph from a user goal.
    - Uses LLM fallback chain (Ollama → Portkey → OpenAI)
    - Calls Agent Planner for missing agents
    - Returns nodes + edges for rendering
    """
    try:
        data = await request.json()
        goal = data.get("goal", "")
        preplan = data.get("preplan", None)
        logger.info(f"🧠 Auto-composing workflow for goal: {goal}")
        logger.info(f"📥 Received preplan: {type(preplan)} | Keys: {list(preplan.keys()) if isinstance(preplan, dict) else 'None'}")
        from planner.ai_work_planner import auto_compose_workflow_graph
        structured_spec_final = data.get("structured_spec_final")
        graph = await auto_compose_workflow_graph(goal,structured_spec_final=structured_spec_final,preplan=preplan)

        return {"status": "ok", **graph}
    except Exception as e:
        logger.error(f"❌ Auto-compose failed: {e}")
        return {"status": "error", "message": str(e)}

from utils.audio_utils import transcribe_audio
from utils.notion_utils import append_to_notion, get_or_create_notion_page
from utils.llm_utils import run_llm_fallback
from fastapi import File, UploadFile, Form

@app.post("/voice_stream")
async def voice_stream(file: UploadFile = File(...), user_id: str = Form("anonymous")):
    """Takes audio input, transcribes it, and appends to Notion."""
    audio_bytes = await file.read()
    transcript = transcribe_audio(audio_bytes)
    page_id = get_or_create_notion_page(user_id)
    append_to_notion(page_id, transcript)
    return {"status": "ok", "transcript": transcript}

@app.post("/summarize_notion")
async def summarize_notion(user_id: str = Form("anonymous")):
    """Fetch recent Notion text → summarize → send to analyzer."""
    from utils.notion_utils import notion, get_or_create_notion_page
    page_id = get_or_create_notion_page(user_id)
    blocks = notion.blocks.children.list(page_id).get("results", [])
    text = " ".join(b["paragraph"]["text"][0]["text"]["content"] for b in blocks if b["type"] == "paragraph")

    summary = run_llm_fallback(f"Summarize this chip design spec in JSON (intent, inputs, outputs, constraints, verification): {text}")
    return {"status": "ok", "summary": summary}

# ==============================
# 🔄 WebSocket for Live Spec Feedback
# ==============================


@app.websocket("/spec_live_feedback")
async def spec_live_feedback(websocket: WebSocket):
    await websocket.accept()
    print("✅ WebSocket connection accepted")

    try:
        while True:
            try:
                # === 1. Fetch latest entry from Notion database ===
                notion_data = notion.databases.query(
                    **{
                        "database_id": os.getenv("NOTION_DATABASE_ID"),
                        "sorts": [{"property": "Last edited time", "direction": "descending"}],
                        "page_size": 1,
                    }
                )
                latest_page = notion_data["results"][0]
                notion_summary = latest_page["properties"].get("Name", {}).get("title", [{}])[0].get("plain_text", "")
                last_edit_time = latest_page["last_edited_time"]

                # === 2. Get latest coverage from Supabase ===
                coverage_row = (
                    supabase.table("spec_coverage")
                    .select("intent_score, io_score, constraint_score, verification_score, total_score")
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                coverage = coverage_row.data[0] if coverage_row.data else None

                # === 3. Combine & send ===
                message = {
                    "summary": {
                        "Notion_Summary": notion_summary or "Waiting for new Notion content...",
                        "Last_Updated": last_edit_time,
                    },
                    "coverage": coverage.get("total_score", 0) if coverage else 0,
                }
                await websocket.send_json(message)

            except Exception as inner_e:
                print(f"⚠️ Inner loop error in WebSocket: {inner_e}")
                await websocket.send_json({"error": str(inner_e)})

            await asyncio.sleep(5)

    except Exception as e:
        print(f"⚠️ WebSocket disconnected: {e}")

@app.post("/voice_to_spec")
async def voice_to_spec(file: UploadFile = File(...)):
    """Convert voice to summarized spec via Whisper + Notion"""
    try:
        import openai, tempfile
        from notion_client import Client

        # --- Step 1: Save temporary audio file ---
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        # --- Step 2: Transcribe using Whisper ---
        openai.api_key = os.getenv("OPENAI_API_KEY")
        transcript = openai.Audio.transcriptions.create(
            model="whisper-1", file=open(tmp_path, "rb")
        )
        text = transcript.text.strip()

        # --- Step 3: Append to Notion ---
        notion = Client(auth=os.getenv("NOTION_API_KEY"))
        db_id = os.getenv("NOTION_DATABASE_ID")
        notion.pages.create(
            parent={"database_id": db_id},
            properties={"Name": {"title": [{"text": {"content": text[:100]}}]}},
        )

        # --- Step 4: Summarize spec (optional) ---
        from utils.llm_utils import run_llm_fallback
        summary_prompt = f"Summarize and structure this design spec:\n{text}"
        summary = run_llm_fallback(summary_prompt)

        return {"summary": summary, "coverage": 65, "mode": "voice"}

    except Exception as e:
        return {"error": str(e)}

# ---------- DELETE a saved custom workflow ----------
@app.delete("/delete_custom_workflow")
def delete_custom_workflow(request: Request, name: str = Query(...)):
    """
    Deletes a workflow by name for the current user.
    Removes associated runs too.
    """
    user = verify_token(request)
    user_id = user.get("sub") if user and user.get("sub") != "anonymous" else None

    # Find workflow by name and user
    q = supabase.table("workflows").select("id").eq("name", name)
    q = q.eq("user_id", user_id) if user_id else q.is_("user_id", None)
    res = q.limit(1).execute()
    if not res.data:
        return {"status": "ok", "deleted": 0, "message": "No such workflow"}

    wf_id = res.data[0]["id"]

    # Delete dependent runs
    try:
        supabase.table("runs").delete().eq("workflow_id", wf_id).execute()
    except Exception:
        pass

    # Delete workflow
    supabase.table("workflows").delete().eq("id", wf_id).execute()
    return {"status": "ok", "deleted": 1, "workflow_id": wf_id}


# ---------- RENAME a saved custom workflow ----------
@app.post("/rename_custom_workflow")
async def rename_custom_workflow(request: Request):
    """
    Renames a saved workflow for the current user.
    Body: { "old_name": "Foo", "new_name": "Bar" }
    """
    try:
        data = await request.json()
        old_name = data.get("old_name")
        new_name = data.get("new_name")

        if not old_name or not new_name:
            raise HTTPException(status_code=400, detail="Both old_name and new_name required")

        user = verify_token(request)
        user_id = user.get("sub") if user and user.get("sub") != "anonymous" else None

        q = supabase.table("workflows").select("id").eq("name", old_name)
        q = q.eq("user_id", user_id) if user_id else q.is_("user_id", None)
        res = q.limit(1).execute()
        if not res.data:
            return {"status": "error", "message": "Workflow not found"}

        wf_id = res.data[0]["id"]
        supabase.table("workflows").update({
            "name": new_name,
            "updated_at": datetime.utcnow().isoformat()
        }).eq("id", wf_id).execute()

        return {"status": "ok", "workflow_id": wf_id, "new_name": new_name}

    except Exception as e:
        logger.error(f"❌ Rename failed: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/agents/get_code")
async def get_agent_code(agent: str):
    from agent_capabilities import AGENT_CAPABILITIES
    import pathlib

    possible_paths = [
        f"agents/custom/{agent.lower()}.py",
        f"agents/{agent.lower()}.py"
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return open(path).read()

    return ""

@app.post("/agents/save_code")
async def save_agent_code(data: dict):
    agent = data["agent"]
    code = data["code"]

    file_path = f"agents/custom/{agent.lower()}.py"
    with open(file_path, "w") as f:
        f.write(code)

    # Reload custom agents
    from agents.loader import load_custom_agents
    load_custom_agents()

    return {"status": "ok"}





@app.post("/finalize_spec_natural_sentences")
async def finalize_spec_natural_sentences(data: dict):
    """
    Convert edited missing values into natural language sentences and append to original prompt.
    Then recompute structured spec + real coverage and return all in one shot.
    Optional: if structured_spec_draft is provided, we finalize from the draft for higher accuracy.


    """

    
    print("\n---- FINALIZE (request) ----")
    print("keys:", list(data.keys()))
    d = data.get("structured_spec_draft")
    print("structured_spec_draft is None?", d )
    original_raw = (data.get("original_text") or "").strip()
    improved_raw = (data.get("improved_text") or "").strip()

    # ✅ Prefer improved (LLM-enhanced) text if available
    base_text = improved_raw if improved_raw else original_raw
    # --- REMOVE incomplete / missing details block ---
    base_text = re.sub(r"Detected missing or incomplete details:.*?(?=Additional Inferred Design Details:|$)", "", base_text, flags=re.DOTALL).strip()


    missing = data.get("missing", [])
    edited_values = data.get("edited_values", {})
    structured_spec_draft = data.get("structured_spec_draft")  # optional

    additions = []
    for item in missing:
        path = item.get("path", "")
        ask = item.get("ask", "") or path.replace("_", " ").replace(".", " → ")
        value = (edited_values.get(path) or "").strip()
        if not value:
           continue
        sentence_prompt = f"""
        Write one clear natural language design clarification sentence.
        Clarification: "{ask}"
        Value: "{value}"
        Style example: "The clock frequency is 200 MHz."
        Do NOT repeat or rewrite the original spec.
        Keep concise, factual, and correct.
        """.strip()
        sentence = (await run_llm_fallback(sentence_prompt)).strip()
        additions.append(f"- {sentence}")


    additions_text = "\n".join(additions)
    final_text = f"""{base_text}

Additional Inferred Design Details:
{additions_text}
""".strip()

    # --- Recompute structured spec + coverage ---
    try:
        # Prefer finalizing from a known draft if provided
        from analyze.digital.analyze_spec_digital import analyze_spec_digital, finalize_spec_digital
  
        if structured_spec_draft:
            # Merge edited values into draft (path strings like "clock.freq")

            # Merge edited values into draft (path strings like "clock_domains[0].frequency_mhz")
            print("\n---- FINALIZE START ----")
            print("original_text:", original_text[:120], "...")
            print("edited_values:", edited_values)
            print("missing:", missing)
            print("structured_spec_draft BEFORE merge:", structured_spec_draft)
            def _apply_value(spec: dict, path: str, value: Any):
                import re
                # split on '.' and brackets, keep only tokens
                tokens = [t for t in re.split(r"\.|\[|\]", path) if t != ""]
                target = spec
                for tok in tokens[:-1]:
                    if tok.isdigit():                 # array index
                        target = target[int(tok)]
                    else:
                  # ensure dict exists for next hop
                        if tok not in target or not isinstance(target[tok], (dict, list)):
                # guess next token; if next token is a digit we need a list
                # else a dict
                            next_idx = tokens.index(tok) + 1
                            is_list = next_idx < len(tokens) and tokens[next_idx].isdigit()
                            target[tok] = [] if is_list else {}
                        target = target[tok]
                last = tokens[-1]
                if last.isdigit():
        # ensure list is big enough
                    idx = int(last)
                    if not isinstance(target, list):
                        raise ValueError(f"Path expects list at '{path}'")
                    while len(target) <= idx:
                        target.append({})
                    target[idx] = value
                else:
                    target[last] = value

            for path, value in edited_values.items():
              _apply_value(structured_spec_draft, path, value)
    
            if additions_text and additions_text.strip():
               structured_spec_draft["natural_language_notes"] = additions_text.strip()

            
            print("structured_spec_draft AFTER merge:", structured_spec_draft)

            final = await finalize_spec_digital(structured_spec_draft)

            structured_final = (
               final.get("structured_spec_final")
               or final.get("structured_spec_draft")
               or final
            )
            print("final result raw:", final)

            coverage = final.get("coverage") or final.get("coverage_score") or {}

            print("DEBUG coverage_obj:", coverage)

            # Normalize coverage to a single numeric score
            if isinstance(coverage, dict):
              coverage_final = (
                coverage.get("total_score")
                or coverage.get("overall_score")
                or coverage.get("score", {}).get("total")
                or 0
              )
            else:
              coverage_final = coverage

            print("coverage_final computed:", coverage_final)
            print("---- FINALIZE END ----\n")

           
        else:
            # Fallback: analyze the natural-language final text to produce structure + coverage
            analyzed = await analyze_spec_digital(final_text, voice_summary="", user_id="anonymous")
            # support either shape: {structured_spec_final, coverage} OR nested result
            structured_final = analyzed.get("structured_spec_final") or analyzed.get("structured_spec_draft") or analyzed
            coverage_final = (
                analyzed.get("coverage")
                or (analyzed.get("coverage", {}) if isinstance(analyzed.get("coverage"), dict) else {})
                or analyzed.get("coverage_score")
                or 0
            )
            # Handle nested object coverage.total_score
            if isinstance(coverage_final, dict) and "total_score" in coverage_final:
                coverage_final = coverage_final["total_score"]

    except Exception as e:
        # Never fail the finalize call—return at least the final_text
        structured_final, coverage_final = {}, 0

    return {
        "status": "ok",
        "final_text": final_text,
        "structured_spec_final": structured_final,
        "coverage": int(coverage_final) if isinstance(coverage_final, (int, float)) else 0,
        "coverage_final": int(coverage_final) if isinstance(coverage_final, (int, float)) else 0,
        "additions": additions
    }



@app.post("/auto_fill_missing_fields")
async def auto_fill_missing_fields_endpoint(payload: dict):
    original_text = payload.get("user_prompt") or payload.get("original_text")
    structured_spec_draft = payload.get("structured_spec_draft")
    missing_fields = payload.get("missing") or payload.get("remaining_missing_fields") or []

    improved_text, structured_spec_enhanced, remaining_missing_fields, auto_filled_values = \
        await auto_fill_missing_fields(original_text, structured_spec_draft, missing_fields)

    return {
        "improved_text": improved_text,
        "structured_spec_enhanced": structured_spec_enhanced,
        "remaining_missing_fields": remaining_missing_fields,
        "auto_filled_values": auto_filled_values
    }





