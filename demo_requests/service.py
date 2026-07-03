import os
import re
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Optional

from .models import DemoRequest, utcnow_iso
from .repository import DemoRequestRepository


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ORGANIZATION_TYPES = {"Individual", "Company", "University"}
DEFAULT_NOTIFICATION_TO = "chiploop.agx@gmail.com"


class DemoRequestError(ValueError):
    pass


class DemoRequestNotifier:
    def __init__(self):
        self.notification_to = os.getenv("CHIPLOOP_DEMO_NOTIFY_TO", DEFAULT_NOTIFICATION_TO).strip() or DEFAULT_NOTIFICATION_TO
        self.smtp_host = os.getenv("SMTP_HOST", "").strip()
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_username = os.getenv("SMTP_USERNAME", "").strip()
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_from = os.getenv("SMTP_FROM", self.smtp_username or self.notification_to).strip()
        self.smtp_tls = os.getenv("SMTP_TLS", "true").strip().lower() not in {"0", "false", "no"}

    def configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def send(self, demo_request: DemoRequest) -> None:
        if not self.configured():
            raise RuntimeError("smtp_not_configured")

        message = EmailMessage()
        message["Subject"] = f"New ChipLoop demo request: {demo_request.name}"
        message["From"] = self.smtp_from
        message["To"] = self.notification_to
        message["Reply-To"] = demo_request.email
        message.set_content(self._body(demo_request))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as smtp:
            if self.smtp_tls:
                smtp.starttls()
            if self.smtp_username:
                smtp.login(self.smtp_username, self.smtp_password)
            smtp.send_message(message)

    def _body(self, demo_request: DemoRequest) -> str:
        return "\n".join([
            "New ChipLoop demo request",
            "",
            f"Name: {demo_request.name}",
            f"Email: {demo_request.email}",
            f"Phone: {demo_request.phone or '-'}",
            f"Organization type: {demo_request.organization_type}",
            f"Organization name: {demo_request.organization_name or '-'}",
            f"Role: {demo_request.role or '-'}",
            f"Interest area: {demo_request.interest_area or '-'}",
            f"Preferred time: {demo_request.preferred_time or '-'}",
            f"Source: {demo_request.source}",
            f"Created at: {demo_request.created_at}",
            "",
            "Message:",
            demo_request.message or "-",
        ])


class DemoRequestService:
    def __init__(self, repository: DemoRequestRepository, notifier: Optional[DemoRequestNotifier] = None):
        self.repository = repository
        self.notifier = notifier or DemoRequestNotifier()

    def create(self, payload: Dict[str, Any]) -> DemoRequest:
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("email") or "").strip().lower()
        organization_type = str(payload.get("organization_type") or "").strip()

        if len(name) < 2:
            raise DemoRequestError("name_required")
        if not EMAIL_RE.match(email):
            raise DemoRequestError("valid_email_required")
        if organization_type not in ORGANIZATION_TYPES:
            raise DemoRequestError("organization_type_required")

        demo_request = DemoRequest(
            name=name[:160],
            email=email[:254],
            phone=str(payload.get("phone") or "").strip()[:60],
            organization_type=organization_type,
            organization_name=str(payload.get("organization_name") or "").strip()[:180],
            role=str(payload.get("role") or "").strip()[:120],
            interest_area=str(payload.get("interest_area") or "").strip()[:160],
            preferred_time=str(payload.get("preferred_time") or "").strip()[:160],
            message=str(payload.get("message") or "").strip()[:2000],
            source=str(payload.get("source") or "landing_page").strip()[:80],
            notification_to=self.notifier.notification_to,
        )
        saved = self.repository.save(demo_request)
        self._notify(saved)
        return saved

    def _notify(self, demo_request: DemoRequest) -> None:
        try:
            self.notifier.send(demo_request)
        except RuntimeError as exc:
            if str(exc) == "smtp_not_configured":
                self.repository.update_notification(demo_request.id, status="not_configured", error=str(exc))
                demo_request.notification_status = "not_configured"
                demo_request.notification_error = str(exc)
                return
            self.repository.update_notification(demo_request.id, status="failed", error=str(exc)[:500])
            demo_request.notification_status = "failed"
            demo_request.notification_error = str(exc)[:500]
            return
        except Exception as exc:
            self.repository.update_notification(demo_request.id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
            demo_request.notification_status = "failed"
            demo_request.notification_error = f"{type(exc).__name__}: {exc}"[:500]
            return

        sent_at = utcnow_iso()
        self.repository.update_notification(demo_request.id, status="sent", sent_at=sent_at)
        demo_request.notification_status = "sent"
        demo_request.notification_sent_at = sent_at
