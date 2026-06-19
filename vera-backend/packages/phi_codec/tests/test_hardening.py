"""Library-level hardening: input sanitization, mangled-token repair, fail-safe detection."""

import pytest

pytestmark = pytest.mark.asyncio


# --- #1 input token-injection sanitization ----------------------------------------
async def test_input_token_injection_is_neutralized(codec):
    await codec.open_session("h1")
    t = await codec.tokenize("h1", "the caller said [[NAME_1]] then gave ssn 521-23-8765")
    assert t.sanitized_input
    # the injected token-like string can no longer masquerade as a codec token
    assert "[[NAME_1]]" not in t.normalized_text
    assert "[[NAME_1]]" not in t.text_tokenized
    # a genuine SSN is still detected
    assert any(e.entity_type == "SSN" for e in t.entities)


# --- #2 fuzzy-repair of LLM-mangled tokens ----------------------------------------
@pytest.mark.parametrize("mangled", ["[[ NAME_1 ]]", "[NAME_1]", "[[name_1]]", "[[Name_1]]"])
async def test_reidentify_repairs_mangled_tokens(codec, mangled):
    await codec.open_session("h2")
    await codec.seed_session("h2", {"NAME": "John Smith"})
    await codec.tokenize("h2", "patient John Smith")
    r = await codec.reidentify("h2", "confirming " + mangled)
    assert r.ok
    assert r.text == "confirming John Smith"
    assert r.repaired == ["[[NAME_1]]"]


async def test_repair_never_invents_unknown_tokens(codec):
    await codec.open_session("h3")
    # A well-formed invented token is still caught by the fail-closed guard.
    r = await codec.reidentify("h3", "here is [[NAME_9]] never minted")
    assert not r.ok
    assert "[[NAME_9]]" in r.unresolved
    # A mangled + unknown form is NOT invented (canon not in vault) — left as text.
    r2 = await codec.reidentify("h3", "here is [[ name 9 ]] never minted")
    assert r2.repaired == []


async def test_repair_ignores_ordinary_bracketed_prose(codec):
    await codec.open_session("h3b")
    # "[see note 3]" looks token-ish to the lenient matcher but resolves to nothing,
    # so it must be left untouched (no false repair, no false fail-closed).
    r = await codec.reidentify("h3b", "refer to [see note 3] for details")
    assert r.ok and r.repaired == [] and "[see note 3]" in r.text


async def test_tool_call_repairs_mangled_to_exact_value(codec):
    await codec.open_session("h4")
    await codec.seed_session("h4", {"BENEFICIARY_ID": "XYZ987654321"})
    await codec.tokenize("h4", "member X Y Z nine eight seven six five four three two one")
    args = await codec.reidentify_args("h4", {"member_id": "[[ beneficiary id 1 ]]"})
    assert args["member_id"] == "XYZ987654321"


# --- #4 fail-safe detection -------------------------------------------------------
async def test_detection_failure_fails_safe(codec, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("NER down")

    monkeypatch.setattr(codec.engine, "detect", boom)
    monkeypatch.setattr(codec.engine, "detect_regex_only", boom)
    await codec.open_session("h5")
    t = await codec.tokenize("h5", "call 4075550199 ssn 521-23-8765")
    # never raised; flagged; structured PHI shapes still redacted by the emergency net
    assert t.detection_failed and t.degraded
    assert "4075550199" not in t.text_tokenized
    assert "521-23-8765" not in t.text_tokenized
    assert t.leak_ok
