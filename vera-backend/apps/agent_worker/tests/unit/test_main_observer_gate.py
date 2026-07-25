from agent_worker.main import observer_enabled_from_meta


def test_observer_enabled_defaults_true_when_flag_absent() -> None:
    # A dispatch that predates the enable_observer flag keeps today's behaviour.
    assert observer_enabled_from_meta({}) is True


def test_observer_enabled_true_when_flag_set() -> None:
    assert observer_enabled_from_meta({"enable_observer": True}) is True


def test_observer_disabled_when_flag_false() -> None:
    assert observer_enabled_from_meta({"enable_observer": False}) is False
