# ADR-0005: PHI tokenization retained even with BAAs in place

Date: 2026-06-10 · Status: Accepted

## Context

We will execute BAAs with every vendor that could touch call content
(LiveKit, Deepgram, Google, Cartesia, Twilio). A BAA makes PHI disclosure to
that vendor *legally permitted*. A reasonable question: if every hop is
BAA-covered, why keep the tokenization codec and its latency/complexity?

## Decision

Keep the PHI codec (vendored `phi_codec` + `vera_core.phi` boundary) as a
mandatory pipeline stage regardless of BAA coverage. BAAs and tokenization are
layers, not alternatives.

## Rationale

- **Minimum necessary standard**: HIPAA expects disclosure limited to what the
  purpose requires. The LLM doesn't need real SSNs to drive an eligibility
  script — tokens carry the dialogue just as well, so sending raw PHI is
  unnecessary disclosure even when permitted.
- **Blast-radius control**: a vendor-side incident (logging bug, prompt leak,
  subpoena, retention surprise) exposes `[[BENEFICIARY_ID_1]]`, not the
  identifier. The session vault — wiped at call end — is the only mapping.
- **LLM behavior**: models repeat, paraphrase, and hallucinate. A model that
  never saw the raw value cannot leak it into a transcript, trace, or tool
  call. The fail-safe/strict hydrate split (ADR-0001's seams) is only possible
  because tokens are distinguishable from real data.
- **Vendor mobility**: swapping an LLM/STT vendor doesn't reopen a PHI
  exposure review of model internals — the wall is ours, audited in our
  audit_log.

## Consequences

- Tokenize budget (~80ms p95) stays on the turn latency path.
- Detection recall is a safety parameter: the codec's leak-canary scan and
  recall suite are release gates for codec changes.
- TTS readback formatting must come from the vault (it does: spell-out rules
  in phi_codec.formatting), since the LLM can't know raw values.
