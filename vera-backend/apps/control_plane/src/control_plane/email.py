"""Outbound email — workforce invite and password-reset links.

Recipients are operators (VAs / supervisors / admins), not patients, so these
messages carry **no PHI**. Both link types embed a single-use bearer token, so the
body must never be logged. Deployed environments send through the Twilio Email API
on the same Twilio account as outbound SIP; local dev delivers to the `sendria`
SMTP sandbox (docker-compose: SMTP 1025, web UI http://localhost:1080).
"""

from dataclasses import dataclass
from email.message import EmailMessage as _MimeMessage
from typing import Protocol

import aiosmtplib
import httpx

from vera_core.config import SecretProvider
from vera_core.config.settings import Settings

__all__ = [
    "EmailDeliveryError",
    "EmailMessage",
    "EmailSender",
    "InMemoryEmailSender",
    "SmtpEmailSender",
    "TwilioEmailSender",
    "build_email_sender",
]

TWILIO_EMAIL_ENDPOINT = "https://comms.twilio.com/v1/Emails"
_SEND_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class EmailDeliveryError(Exception):
    """Twilio rejected the send; carries the HTTP status and nothing else."""


class TwilioEmailSender:
    """Twilio Email API over Basic auth, taking either credential pair Twilio accepts:
    the account SID + auth token, or an API key SID + secret (preferred in production).
    One client per send — invite/reset volume is far too low to pool."""

    def __init__(self, *, account_sid: str, auth_token: str, sender: str) -> None:
        self._auth = (account_sid, auth_token)
        self._sender = sender

    async def send(self, message: EmailMessage) -> None:
        payload = {
            "from": {"address": self._sender},
            "to": [{"address": message.to}],
            "content": {"subject": message.subject, "text": message.body},
        }
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS) as client:
            response = await client.post(TWILIO_EMAIL_ENDPOINT, json=payload, auth=self._auth)
        # Status only — never Twilio's response body or the payload, which holds a live token.
        if not response.is_success:
            raise EmailDeliveryError(f"twilio email rejected the send: HTTP {response.status_code}")


class SmtpEmailSender:
    """Local-sandbox sender over plain SMTP; sendria needs no auth or TLS."""

    def __init__(self, *, host: str, port: int, sender: str) -> None:
        self._host = host
        self._port = port
        self._sender = sender

    async def send(self, message: EmailMessage) -> None:
        mime = _MimeMessage()
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        await aiosmtplib.send(mime, hostname=self._host, port=self._port)


class InMemoryEmailSender:
    """Dev/tests: records sent messages instead of delivering them."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


def build_email_sender(settings: Settings, secrets: SecretProvider) -> EmailSender:
    """Factory: TwilioEmailSender when `twilio_account_sid` is set, else SmtpEmailSender."""
    if settings.twilio_account_sid is not None:
        return TwilioEmailSender(
            account_sid=settings.twilio_account_sid,
            auth_token=secrets.get("TWILIO_AUTH_TOKEN"),
            sender=settings.email_from,
        )
    return SmtpEmailSender(
        host=settings.smtp_host, port=settings.smtp_port, sender=settings.email_from
    )
