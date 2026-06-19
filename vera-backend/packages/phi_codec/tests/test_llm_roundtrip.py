"""Gated: verify tokens survive a real LLM round-trip unchanged.

The whole scheme depends on Gemini copying ``[[TYPE_N]]`` tokens verbatim — never
translating, splitting, lowercasing, or "helpfully" rewriting them. This test sends
tokens through the live model and asserts they come back byte-identical.

Uses the public Gemini Developer API (an API key), not Vertex. Skipped unless
GOOGLE_API_KEY is set, so the default suite stays offline and fast. Run:

    GOOGLE_API_KEY=... uv run pytest tests/test_llm_roundtrip.py -v
    # optional model override:
    GEMINI_MODEL=gemini-2.5-flash GOOGLE_API_KEY=... uv run pytest tests/test_llm_roundtrip.py -v
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("GOOGLE_API_KEY"),
    reason="set GOOGLE_API_KEY to run the live Gemini token round-trip",
)

from phi_codec.tokens.token import find_tokens  # noqa: E402

PROMPT = (
    "You are a claims assistant. Repeat the following text back EXACTLY, character for "
    "character. Never alter, translate, split, or invent text inside [[ ]].\n\n"
    "Patient [[NAME_1]] member [[BENEFICIARY_ID_1]] DOB [[DATE_1]] SSN [[SSN_1]] called about "
    "[[BENEFICIARY_ID_1]]."
)
EXPECTED_TOKENS = {"[[NAME_1]]", "[[BENEFICIARY_ID_1]]", "[[DATE_1]]", "[[SSN_1]]"}


def _gemini_complete(prompt: str) -> str:
    """Minimal public Gemini Developer API call (API-key auth)."""
    from google import genai

    # Client picks up GOOGLE_API_KEY / GEMINI_API_KEY from the env; passed explicitly here.
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    resp = client.models.generate_content(model=model, contents=prompt)
    return resp.text


def test_tokens_survive_gemini_roundtrip():
    out = _gemini_complete(PROMPT)
    returned = {t.surface for t in find_tokens(out)}
    missing = EXPECTED_TOKENS - returned
    assert not missing, f"Gemini mangled/dropped tokens: {missing}\n---\n{out}"
