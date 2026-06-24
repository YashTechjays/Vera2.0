from _pytest.monkeypatch import MonkeyPatch

from vera_core.config.settings import Settings


def test_transcript_ttl_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.transcript_stream_ttl_seconds == 3600
    assert s.transcript_end_grace_seconds == 60


def test_transcript_ttl_env_override(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("VERA_TRANSCRIPT_END_GRACE_SECONDS", "30")
    assert Settings(_env_file=None).transcript_end_grace_seconds == 30
