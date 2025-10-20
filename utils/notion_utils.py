# utils/notion_utils.py
import os
from notion_client import Client
from supabase import create_client, Client as SupabaseClient

notion = Client(auth=os.getenv("NOTION_API_KEY"))
supabase: SupabaseClient = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))

def get_or_create_notion_page(user_id: str) -> str:
    """Gets or creates a Notion page mapped to the user."""
    res = supabase.table("notion_user_map").select("notion_page_id").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0]["notion_page_id"]

    page = notion.pages.create(
        parent={"database_id": os.getenv("NOTION_DATABASE_ID")},
        properties={"title": [{"text": {"content": f"ChipLoop Spec - {user_id}"}}]}
    )
    notion_page_id = page["id"]
    supabase.table("notion_user_map").upsert({"user_id": user_id, "notion_page_id": notion_page_id}).execute()
    return notion_page_id

def append_to_notion(page_id: str, content: str):
    """Appends transcript text to the Notion page."""
    notion.blocks.children.append(
        page_id,
        children=[{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"text": [{"type": "text", "text": {"content": content[:2000]}}]},
        }]
    )
