"""The gate on logging a tool call's model-authored reason.

The OFF case is the one that matters: that text is written by the model about live call state,
so it can carry a member name or a clinical detail, and production must never log it (see
vera_core/CLAUDE.md). A future refactor that "simplifies away" the branch has to fail here.
"""

import logging
from types import SimpleNamespace

import pytest
from _pytest.logging import LogCaptureFixture
from _pytest.monkeypatch import MonkeyPatch

from agent_worker import tool_log
from vera_core.config.settings import Settings

REASON = "the representative confirmed no coverage questions remain"


@pytest.fixture
def logged(caplog: LogCaptureFixture) -> LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="agent_worker")
    return caplog


def _flag(monkeypatch: MonkeyPatch, *, enabled: bool) -> None:
    # Patch the name tool_log resolved, rather than fighting get_settings' lru_cache.
    monkeypatch.setattr(tool_log, "get_settings", lambda: SimpleNamespace(log_tool_reasons=enabled))


def test_the_reason_text_never_reaches_the_log_by_default(
    monkeypatch: MonkeyPatch, logged: LogCaptureFixture
) -> None:
    _flag(monkeypatch, enabled=False)
    tool_log.log_tool_reason("task_complete", REASON)
    assert REASON not in logged.text
    assert "task_complete" in logged.text  # which tool fired is not PHI, and is worth having
    assert f"len={len(REASON)}" in logged.text


def test_the_flag_opens_the_reason_up_verbatim(
    monkeypatch: MonkeyPatch, logged: LogCaptureFixture
) -> None:
    _flag(monkeypatch, enabled=True)
    tool_log.log_tool_reason("task_complete", REASON)
    assert REASON in logged.text


def test_a_multiline_reason_is_logged_unaltered(
    monkeypatch: MonkeyPatch, logged: LogCaptureFixture
) -> None:
    _flag(monkeypatch, enabled=True)
    tool_log.log_tool_reason("end_call", "line one\nline two")
    assert "line one\nline two" in logged.text


def test_an_empty_reason_still_records_the_tool(
    monkeypatch: MonkeyPatch, logged: LogCaptureFixture
) -> None:
    _flag(monkeypatch, enabled=False)
    tool_log.log_tool_reason("give_up", "")
    assert "give_up" in logged.text
    assert "len=0" in logged.text


class TestSetting:
    def test_the_default_is_off(self) -> None:
        # Not a preference: the default is what keeps model-authored text out of prod logs.
        assert Settings(_env_file=None).log_tool_reasons is False

    def test_the_env_var_turns_it_on(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("VERA_LOG_TOOL_REASONS", "1")
        assert Settings(_env_file=None).log_tool_reasons is True
