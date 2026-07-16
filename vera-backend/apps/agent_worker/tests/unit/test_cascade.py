from agent_worker.cascade import cascade_session_kwargs


def test_cascade_uses_turn_handling_only() -> None:
    # turn_handling is mutually exclusive with the deprecated flat kwargs: if it's set,
    # livekit-agents ignores every flat kwarg (incl. turn_detection). Exactly one key
    # here guarantees none of those silently leaked back in.
    kw = cascade_session_kwargs(turn_detector=object())
    assert set(kw) == {"turn_handling"}


def test_interruption_pinned_to_local_vad() -> None:
    # Self-hosted LiveKit OSS has no Cloud inference gateway; the adaptive (ML)
    # detector would 401 against agent-gateway.livekit.cloud and stream audio off-box.
    # Pin local VAD barge-in instead.
    interruption = cascade_session_kwargs(turn_detector=object())["turn_handling"]["interruption"]
    assert interruption["mode"] == "vad"
    assert interruption["min_duration"] == 0.5
    assert interruption["false_interruption_timeout"] == 2.0
    assert interruption["resume_false_interruption"] is True


def test_turn_detection_lives_inside_turn_handling() -> None:
    detector = object()
    turn_handling = cascade_session_kwargs(turn_detector=detector)["turn_handling"]
    # Must be nested here — a top-level turn_detection kwarg is dropped when turn_handling is set.
    assert turn_handling["turn_detection"] is detector


def test_cascade_latency_knobs() -> None:
    turn_handling = cascade_session_kwargs(turn_detector=object())["turn_handling"]
    assert turn_handling["endpointing"]["min_delay"] == 0.3
    assert turn_handling["endpointing"]["max_delay"] == 0.6
    assert turn_handling["preemptive_generation"]["enabled"] is True


def test_stt_kwargs_carry_plan_key_terms() -> None:
    # The CallPlan's stt_key_terms feed Deepgram keyterm prompting verbatim.
    from agent_worker.cascade import stt_kwargs

    assert stt_kwargs(["Cigna", "copay"]) == {"keyterm": ["Cigna", "copay"]}


def test_stt_kwargs_empty_without_key_terms() -> None:
    from agent_worker.cascade import stt_kwargs

    assert stt_kwargs(None) == {}
    assert stt_kwargs([]) == {}
