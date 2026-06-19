"""Detection recall + latency harness over synthetic spoken-form PHI.

Two recall numbers per entity type:
  * redaction recall — was the value tokenized at all (regardless of type)? This is
    the compliance metric: a miss here is PHI leaking to the LLM. Target ~99%+.
  * type recall — was it tokenized as the correct semantic type? Secondary; affects
    LLM comprehension, not leakage.

Run: ``uv run python -m phi_codec.eval.recall --n 300``
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass

from ..codec import PHICodec
from ..config import CodecConfig
from .synth import generate


@dataclass
class TypeStats:
    total: int = 0
    redacted: int = 0
    typed: int = 0

    @property
    def redaction_recall(self) -> float:
        return self.redacted / self.total if self.total else 1.0

    @property
    def type_recall(self) -> float:
        return self.typed / self.total if self.total else 1.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100) * (len(s) - 1)))))
    return s[k]


async def run(n: int, *, seed: int = 0, use_gliner: bool = True) -> dict:
    codec = PHICodec(CodecConfig(use_gliner=use_gliner))
    sid = "eval"
    await codec.open_session(sid)

    stats: dict[str, TypeStats] = defaultdict(TypeStats)
    latencies: list[float] = []
    leaks = 0

    for i, sample in enumerate(generate(n, seed=seed)):
        res = await codec.tokenize(sid, sample.spoken_text, turn_id=f"t{i}")
        latencies.append(res.latency_ms)
        if not res.leak_ok:
            leaks += 1
        for gt in sample.truths:
            st = stats[gt.entity_type.value]
            st.total += 1
            # Redaction: the canonical value no longer appears in the LLM-facing text.
            if gt.value not in res.text_tokenized:
                st.redacted += 1
            # Type: some detection of the right type covers the value (containment-tolerant,
            # since URL/street/age spans may include surrounding characters).
            if any(
                e.entity_type == gt.entity_type.value
                and (gt.value in e.raw_text or e.raw_text in gt.value)
                for e in res.entities
            ):
                st.typed += 1

    return {
        "n": n,
        "stats": dict(stats),
        "latency_p50": _percentile(latencies, 50),
        "latency_p95": _percentile(latencies, 95),
        "leak_turns": leaks,
    }


def _print_report(report: dict) -> None:
    print(f"\nSynthetic recall over {report['n']} utterances\n")
    print(f"{'entity':12} {'n':>5} {'redaction':>10} {'type':>8}")
    print("-" * 40)
    overall_total = overall_redacted = 0
    for etype, st in sorted(report["stats"].items()):
        print(f"{etype:12} {st.total:>5} {st.redaction_recall:>9.1%} {st.type_recall:>7.1%}")
        overall_total += st.total
        overall_redacted += st.redacted
    overall = overall_redacted / overall_total if overall_total else 1.0
    print("-" * 40)
    print(f"{'OVERALL':12} {overall_total:>5} {overall:>9.1%}")
    print(f"\nlatency p50={report['latency_p50']:.1f}ms  p95={report['latency_p95']:.1f}ms")
    print(f"leak-canary trips: {report['leak_turns']} / {report['n']} turns\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-gliner", action="store_true")
    args = ap.parse_args()
    report = asyncio.run(run(args.n, seed=args.seed, use_gliner=not args.no_gliner))
    _print_report(report)


if __name__ == "__main__":
    main()
