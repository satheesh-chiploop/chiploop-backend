from typing import Dict

from .models import DemoRequest


class DemoRequestRepository:
    def save(self, demo_request: DemoRequest) -> DemoRequest:
        raise NotImplementedError

    def update_notification(self, request_id: str, *, status: str, error: str = "", sent_at: str = "") -> None:
        raise NotImplementedError


class InMemoryDemoRequestRepository(DemoRequestRepository):
    def __init__(self):
        self.records: Dict[str, DemoRequest] = {}

    def save(self, demo_request: DemoRequest) -> DemoRequest:
        self.records[demo_request.id] = demo_request
        return demo_request

    def update_notification(self, request_id: str, *, status: str, error: str = "", sent_at: str = "") -> None:
        demo_request = self.records.get(request_id)
        if not demo_request:
            return
        demo_request.notification_status = status
        demo_request.notification_error = error
        demo_request.notification_sent_at = sent_at


class SupabaseDemoRequestRepository(DemoRequestRepository):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def save(self, demo_request: DemoRequest) -> DemoRequest:
        response = self.supabase.table("demo_requests").insert(demo_request.to_row()).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list) and data:
            return DemoRequest.from_dict(data[0])
        if isinstance(data, dict):
            return DemoRequest.from_dict(data)
        return demo_request

    def update_notification(self, request_id: str, *, status: str, error: str = "", sent_at: str = "") -> None:
        self.supabase.table("demo_requests").update({
            "notification_status": status,
            "notification_error": error or None,
            "notification_sent_at": sent_at or None,
        }).eq("id", request_id).execute()
