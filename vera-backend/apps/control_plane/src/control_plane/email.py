"""Outbound email — currently only workforce invite links.

Invitees are operators (VAs / supervisors / admins), not patients, so these
messages carry **no PHI**. The invite link does embed a single-use bearer token,
so the body must never be logged. Local dev delivers to the `sendria` SMTP sandbox
(docker-compose: SMTP 1025, web UI http://localhost:1080); production points the
same interface at the real relay.
"""

from dataclasses import dataclass
from email.message import EmailMessage as _MimeMessage
from typing import Protocol

import aiosmtplib


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class SmtpEmailSender:
    """Production / local-sandbox sender over plain SMTP. Auth/TLS knobs are added
    when the real relay is wired; sendria needs neither."""

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
