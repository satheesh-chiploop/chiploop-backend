# backend/planner/mycelium_memory.py
import hashlib
from supabase import create_client
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def context_hash(struct):
    return hashlib.sha256(str(struct).encode("utf-8")).hexdigest()[:12]

def memory_lookup(context_hash):
    """
    Returns count of times this context has appeared before.
    """
    try:
        r = supabase.table("agent_memory").select("context_hash").eq("context_hash", context_hash).execute()
        return len(r.data or [])
    except:
        return 0

def record_success(agent_name, struct):
    """
    Called after final workflow success.
    """
    try:
        supabase.table("agent_memory").insert({
            "context_hash": context_hash(struct),
            "agent": agent_name
        }).execute()
    except:
        pass
