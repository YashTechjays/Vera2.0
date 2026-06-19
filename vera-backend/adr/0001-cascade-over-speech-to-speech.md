# ADR-0001: Cascaded STT → LLM → TTS pipeline, not speech-to-speech

Date: 2026-06-10 · Status: Accepted

## Context

Realtime speech-to-speech models (e.g. Gemini Live, GPT realtime) collapse the
voice pipeline into one model call: lower latency, simpler plumbing. Vera's
calls carry PHI under HIPAA, and our PHI strategy is a tokenization wall: raw
identifiers are swapped for `[[TYPE_N]]` tokens before any LLM sees the
transcript, and re-identified only at the speech/tool boundaries
(`vera_core.phi`).

## Decision

Use a cascaded pipeline — Deepgram (STT) → Gemini (LLM) → Cartesia (TTS) —
with explicit text boundaries between every stage.

## Rationale

- **The tokenization wall requires text seams.** redact() must run between STT
  and LLM, hydrate between LLM and TTS. A speech-to-speech model has no seam:
  raw PHI audio goes in, we lose the ability to keep PHI out of the model and
  out of provider logs.
- **Auditability**: each crossing is a discrete, auditable event keyed to a
  session; impossible to attest when audio goes straight into a multimodal
  model.
- **Vendor swap-ability**: each stage is independently replaceable (payer IVRs
  are a hostile audio environment; STT choice will be re-litigated).
- Cost: cascade stages are individually cheaper and contractually simpler to
  cover with BAAs.

## Consequences

- We own turn-taking/latency budgets (~80ms tokenize budget; see phi_codec).
- IVR/DTMF handling stays in the STT/SIP layer, which the cascade keeps
  accessible.
- Revisit if a speech-to-speech provider offers in-model redaction with BAA
  coverage AND an audit story; the seams in `agent_worker` keep that swap
  contained.
