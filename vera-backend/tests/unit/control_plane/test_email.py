"""Email senders and the build_email_sender selector: Twilio Email once an account
SID is configured, the local SMTP sandbox otherwise."""

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from control_plane.email import (
    TWILIO_EMAIL_ENDPOINT,
    EmailDeliveryError,
    EmailMessage,
    SmtpEmailSender,
    TwilioEmailSender,
    build_email_sender,
)
from vera_core.config import EnvSecretProvider, Settings

_MESSAGE = EmailMessage(to="va@example.com", subject="Hello", body="Body text.")
_FROM = "no-reply@vera.local"


def _twilio_sender() -> TwilioEmailSender:
    return TwilioEmailSender(account_sid="AC123", auth_token="token-abc", sender=_FROM)


def _post_mock(status_code: int, text: str = "") -> AsyncMock:
    """Stand-in for `httpx.AsyncClient.post` that replies with the given status."""
    request = httpx.Request("POST", TWILIO_EMAIL_ENDPOINT)
    return AsyncMock(return_value=httpx.Response(status_code, request=request, text=text))


async def test_twilio_posts_the_documented_payload() -> None:
    post = _post_mock(202)
    with patch("control_plane.email.httpx.AsyncClient.post", post):
        await _twilio_sender().send(_MESSAGE)
    assert post.call_args.args[0] == TWILIO_EMAIL_ENDPOINT
    assert post.call_args.kwargs["auth"] == ("AC123", "token-abc")
    assert post.call_args.kwargs["json"] == {
        "from": {"address": _FROM},
        "to": [{"address": "va@example.com"}],
        "content": {"subject": "Hello", "text": "Body text."},
    }


async def test_twilio_rejection_raises_with_status_only() -> None:
    post = _post_mock(401, "unverified sender no-reply@x")
    with patch("control_plane.email.httpx.AsyncClient.post", post):
        with pytest.raises(EmailDeliveryError) as excinfo:
            await _twilio_sender().send(_MESSAGE)
    # The token lives in the request body; the failure must not echo either side of it.
    assert "401" in str(excinfo.value)
    assert "unverified sender" not in str(excinfo.value)
    assert "Body text." not in str(excinfo.value)


async def test_smtp_sends_plain_to_the_sandbox() -> None:
    sender = SmtpEmailSender(host="localhost", port=1025, sender=_FROM)
    with patch("control_plane.email.aiosmtplib.send", new_callable=AsyncMock) as send:
        await sender.send(_MESSAGE)
    assert send.call_args.kwargs == {"hostname": "localhost", "port": 1025}
    mime = send.call_args.args[0]
    assert mime["From"] == _FROM
    assert mime["To"] == "va@example.com"
    assert mime["Subject"] == "Hello"
    assert mime.get_content().strip() == "Body text."


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://fake:fake@localhost/fake",
        redis_url="redis://localhost:6379/0",
        **overrides,
    )


def test_build_selects_smtp_without_a_twilio_account() -> None:
    sender = build_email_sender(_settings(), EnvSecretProvider())
    assert isinstance(sender, SmtpEmailSender)


def test_build_selects_twilio_and_reads_the_token_from_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-abc")
    sender = build_email_sender(_settings(twilio_account_sid="AC123"), EnvSecretProvider())
    assert isinstance(sender, TwilioEmailSender)
