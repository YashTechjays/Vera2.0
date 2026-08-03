"""Outbound email — workforce invite and password-reset links.

Recipients are operators (VAs / supervisors / admins), not patients, so these
messages carry **no PHI**. Both link types embed a single-use bearer token, so the
body must never be logged. Deployed environments send through the Twilio Email API
on the same Twilio account as outbound SIP; local dev delivers to the `sendria`
SMTP sandbox (docker-compose: SMTP 1025, web UI http://localhost:1080).
"""

import html
import logging
from dataclasses import dataclass
from email.message import EmailMessage as _MimeMessage
from typing import Protocol

import aiosmtplib
import httpx

from vera_core.config import SecretNotFoundError, SecretProvider
from vera_core.config.settings import Settings

logger = logging.getLogger(__name__)

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
_SENDER_NAME = "Vera Techsolutions"


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str
    # Both set: the HTML part becomes a card with a button (the plain part stays `body`).
    action_url: str | None = None
    action_label: str | None = None


_PAGE_STYLE = (
    "background:#f4f4f5;padding:32px 16px;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
)
_CARD_STYLE = "max-width:480px;margin:0 auto;background:#ffffff;border-radius:12px;padding:32px;"
_HEADING_STYLE = "margin:0 0 24px;font-size:20px;font-weight:600;color:#18181b;"
_TEXT_STYLE = "margin:0 0 16px;font-size:15px;line-height:1.6;color:#18181b;"
_BUTTON_STYLE = (
    "display:inline-block;background:#18181b;color:#ffffff;text-decoration:none;"
    "padding:12px 24px;border-radius:8px;font-size:14px;font-weight:600;"
)
_NOTE_STYLE = "margin:24px 0 0;font-size:13px;line-height:1.5;color:#71717a;"


def _render_html(message: EmailMessage) -> str:
    """Render `body` as a single-column card: intro paragraphs, an action button, a footer note."""
    if not message.action_url or not message.action_label:
        return html.escape(message.body).replace("\n", "<br>")

    stripped = (p.strip() for p in message.body.split("\n\n"))
    # The paragraph that is just the bare URL is dropped — the button replaces it.
    *intro, footer = [p for p in stripped if p and p != message.action_url]
    intro_html = "".join(f'<p style="{_TEXT_STYLE}">{html.escape(p)}</p>' for p in intro)
    url = html.escape(message.action_url)
    label = html.escape(message.action_label)
    return (
        f'<div style="{_PAGE_STYLE}">'
        f'<div style="{_CARD_STYLE}">'
        f'<p style="{_HEADING_STYLE}">Vera Techsolutions</p>'
        f"{intro_html}"
        '<p style="margin:24px 0;text-align:center;">'
        f'<a href="{url}" style="{_BUTTON_STYLE}">{label}</a>'
        "</p>"
        f'<p style="{_NOTE_STYLE}">{html.escape(footer)}</p>'
        "</div></div>"
    )


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
        # Twilio 400s the send unless both from.name and content.html are present.
        payload = {
            "from": {"address": self._sender, "name": _SENDER_NAME},
            "to": [{"address": message.to}],
            "content": {
                "subject": message.subject,
                "text": message.body,
                "html": _render_html(message),
            },
        }
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS) as client:
            response = await client.post(TWILIO_EMAIL_ENDPOINT, json=payload, auth=self._auth)
        # Status only — never Twilio's response body or the payload, which holds a live token.
        if response.is_success:
            return
        if response.status_code in (401, 403):
            # Auth/config failure, not a per-message bounce — every send will fail
            # identically until the SID/token/sender is fixed. Every caller catches
            # EmailDeliveryError and logs a per-message warning (by design, so a
            # requester is never told a send failed) — that alone would leave a full
            # outage invisible, so this case also gets its own loud, alertable line.
            logger.error(
                "Twilio email auth/config failure (HTTP %s) — every send will fail "
                "until the account SID, token, or sender identity is fixed",
                response.status_code,
            )
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
        mime.add_alternative(_render_html(message), subtype="html")
        await aiosmtplib.send(mime, hostname=self._host, port=self._port)


class InMemoryEmailSender:
    """Dev/tests: records sent messages instead of delivering them."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


def build_email_sender(settings: Settings, secrets: SecretProvider) -> EmailSender:
    """Factory: TwilioEmailSender when `twilio_account_sid` is set, else SmtpEmailSender.

    Falsy (not just missing) SID falls through to SMTP too — pydantic-settings assigns
    "" for `VERA_TWILIO_ACCOUNT_SID=` with no `env_ignore_empty`, and an empty SID is
    not a usable one. And the token lookup is never allowed to take the whole app down:
    a resolvable SID with an unresolvable token (secret rotation lag) degrades to SMTP
    with a loud warning instead of crashing the lifespan.
    """
    if settings.twilio_account_sid:
        try:
            auth_token = secrets.get("TWILIO_AUTH_TOKEN")
        except SecretNotFoundError:
            # ERROR, not WARNING: in prod this silently swaps every send onto the
            # SMTP sandbox sender — an outage, not a one-off bounce.
            logger.error(
                "VERA_TWILIO_ACCOUNT_SID is set but TWILIO_AUTH_TOKEN did not resolve; "
                "falling back to the SMTP sender instead of failing app startup"
            )
        else:
            return TwilioEmailSender(
                account_sid=settings.twilio_account_sid,
                auth_token=auth_token,
                sender=settings.email_from,
            )
    return SmtpEmailSender(
        host=settings.smtp_host, port=settings.smtp_port, sender=settings.email_from
    )
