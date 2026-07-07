import pytest

from agent_worker.main import build_worker_options, entrypoint
from vera_core.config.settings import Settings


def _patch_settings(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(
        "agent_worker.main.get_settings",
        lambda: Settings(_env_file=None, livekit_agent_name=name),
    )


def test_registers_with_default_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, "vera-agent")
    options = build_worker_options()
    assert options.agent_name == "vera-agent"
    assert options.entrypoint_fnc is entrypoint


def test_agent_name_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    # VERA_LIVEKIT_AGENT_NAME lets a laptop sharing a LiveKit project isolate its
    # dispatch pool from a deployed worker registered under the default name.
    _patch_settings(monkeypatch, "vera-agent-local")
    assert build_worker_options().agent_name == "vera-agent-local"
