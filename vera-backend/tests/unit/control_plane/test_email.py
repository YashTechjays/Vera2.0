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
_MESSAGE_WITH_ACTION = EmailMessage(
    to="va@example.com",
    subject="Hello",
    body="Hi there.\n\nhttps://vera.example/action\n\nIgnore this if unexpected.",
    action_url="https://vera.example/action",
    action_label="Do the thing",
)
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
        "from": {"address": _FROM, "name": "Vera Techsolutions"},
        "to": [{"address": "va@example.com"}],
        "content": {"subject": "Hello", "text": "Body text.", "html": "Body text."},
    }


async def test_twilio_renders_a_button_when_an_action_is_given() -> None:
    post = _post_mock(202)
    with patch("control_plane.email.httpx.AsyncClient.post", post):
        await _twilio_sender().send(_MESSAGE_WITH_ACTION)
    html_body = post.call_args.kwargs["json"]["content"]["html"]
    assert 'href="https://vera.example/action"' in html_body
    assert "Do the thing</a>" in html_body
    assert "Hi there." in html_body
    assert "Ignore this if unexpected." in html_body
    # The bare URL paragraph is replaced by the button, not duplicated as text.
    assert html_body.count("https://vera.example/action") == 1


async def test_twilio_rejection_raises_with_status_only() -> None:
    post = _post_mock(401, "unverified sender no-reply@x")
    with patch("control_plane.email.httpx.AsyncClient.post", post):
        with pytest.raises(EmailDeliveryError) as excinfo:
            await _twilio_sender().send(_MESSAGE)
    # The token lives in the request body; the failure must not echo either side of it.
    assert "401" in str(excinfo.value)
    assert "unverified sender" not in str(excinfo.value)
    assert "Body text." not in str(excinfo.value)


async def test_twilio_auth_failure_logs_loudly(caplog: pytest.LogCaptureFixture) -> None:
    # 401/403 means every future send will fail identically until the SID/token/
    # sender is fixed — that must be loud, not indistinguishable from a one-off bounce.
    post = _post_mock(401)
    with caplog.at_level("ERROR", logger="control_plane.email"):
        with patch("control_plane.email.httpx.AsyncClient.post", post):
            with pytest.raises(EmailDeliveryError):
                await _twilio_sender().send(_MESSAGE)
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_twilio_transient_failure_does_not_log_the_auth_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    post = _post_mock(500)
    with caplog.at_level("ERROR", logger="control_plane.email"):
        with patch("control_plane.email.httpx.AsyncClient.post", post):
            with pytest.raises(EmailDeliveryError):
                await _twilio_sender().send(_MESSAGE)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


async def test_smtp_sends_plain_and_html_to_the_sandbox() -> None:
    sender = SmtpEmailSender(host="localhost", port=1025, sender=_FROM)
    with patch("control_plane.email.aiosmtplib.send", new_callable=AsyncMock) as send:
        await sender.send(_MESSAGE_WITH_ACTION)
    assert send.call_args.kwargs == {"hostname": "localhost", "port": 1025}
    mime = send.call_args.args[0]
    assert mime["From"] == _FROM
    assert mime["To"] == "va@example.com"
    assert mime["Subject"] == "Hello"
    assert mime.get_body(("plain",)).get_content().strip() == _MESSAGE_WITH_ACTION.body
    html_body = mime.get_body(("html",)).get_content()
    assert 'href="https://vera.example/action"' in html_body


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://fake:fake@localhost/fake",
        redis_url="redis://localhost:6379/0",
        **overrides,
    )


def test_build_selects_smtp_without_a_twilio_account(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real Twilio SID configured in the dev shell must not leak into this "unset" case.
    monkeypatch.delenv("VERA_TWILIO_ACCOUNT_SID", raising=False)
    sender = build_email_sender(_settings(), EnvSecretProvider())
    assert isinstance(sender, SmtpEmailSender)


def test_build_selects_twilio_and_reads_the_token_from_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-abc")
    sender = build_email_sender(_settings(twilio_account_sid="AC123"), EnvSecretProvider())
    assert isinstance(sender, TwilioEmailSender)


def test_build_selects_smtp_when_the_sid_is_an_empty_string() -> None:
    # pydantic-settings assigns "" for VERA_TWILIO_ACCOUNT_SID= with no
    # env_ignore_empty — a falsy SID must not select Twilio.
    sender = build_email_sender(_settings(twilio_account_sid=""), EnvSecretProvider())
    assert isinstance(sender, SmtpEmailSender)


def test_build_degrades_to_smtp_when_the_token_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SID present, token unresolvable (e.g. secret rotation lag) must not crash the
    # caller — that caller is app startup, so this can't raise. A real token set in
    # the dev shell must not leak into this "unresolvable" case.
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    sender = build_email_sender(_settings(twilio_account_sid="AC123"), EnvSecretProvider())
    assert isinstance(sender, SmtpEmailSender)
