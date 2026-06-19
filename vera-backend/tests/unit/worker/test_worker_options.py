from agent_worker.main import AGENT_NAME, build_worker_options, entrypoint


def test_registers_with_agent_name_for_explicit_dispatch() -> None:
    options = build_worker_options()
    assert options.agent_name == AGENT_NAME == "vera-agent"
    assert options.entrypoint_fnc is entrypoint
