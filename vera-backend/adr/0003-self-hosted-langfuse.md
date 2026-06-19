# ADR-0003: Self-hosted Langfuse for LLM observability

Date: 2026-06-10 · Status: Accepted

## Context

Voice-pipeline debugging needs span-level traces of STT/LLM/TTS stages, token
usage, and per-call session views. Traces inevitably contain transcript
*shape* (tokenized text, timings, model IO sizes) — even with the PHI wall,
trace payloads are sensitive operational data about patient calls.

## Decision

Run Langfuse self-hosted (in our GCP project, CMEK-encrypted storage), fed via
OTLP from both processes (`vera_core.observability`). Langfuse Cloud is not
used.

## Rationale

- **Data residency**: tokenized transcripts stay inside our VPC; no third
  trace processor to add to the BAA chain.
- **OTel-native**: both the FastAPI plane and the LiveKit worker emit standard
  OTLP; the room name is the correlation key (`langfuse.session.id`), so one
  call lines up across processes with zero shared state.
- Langfuse's session/trace UI fits the "replay one call" debugging motion
  better than raw Tempo/Jaeger.

## Boundaries

Langfuse is observability ONLY. The compliance record (authz decisions, PHI
access) is the immutable `audit_log` table. Nothing about retention or
completeness of Langfuse data may be load-bearing for HIPAA evidence.

## Consequences

- We operate Langfuse (Postgres + ClickHouse + Redis) — real ops cost.
- Trace payloads must remain tokenized: span attributes never carry raw PHI
  (enforced by instrumenting AFTER redact()/before hydrate boundaries).
