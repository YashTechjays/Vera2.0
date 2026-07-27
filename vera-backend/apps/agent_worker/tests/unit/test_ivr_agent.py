"""IVR navigator: id sentinel and the transfer-to-verification handoff."""

from livekit.agents import Agent

from agent_worker.ivr_agent import IVR_NAVIGATOR_ID, IvrNavigatorAgent


def _navigator(verifier: Agent) -> IvrNavigatorAgent:
    return IvrNavigatorAgent(verification_agent_factory=lambda: verifier)


class TestConstruction:
    def test_navigator_id_is_the_sentinel(self) -> None:
        navigator = _navigator(Agent(instructions="verify"))
        assert navigator.id == IVR_NAVIGATOR_ID
