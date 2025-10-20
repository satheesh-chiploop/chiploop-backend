# utils/websocket_utils.py
active_connections = {}

async def push_summary_to_frontend(user_id, summary):
    ws = active_connections.get(user_id)
    if ws:
        await ws.send_json({"type": "spec_summary", "data": summary})
