"""A simulated payer representative for the behavioural evals.

The compiled IBV plan asks up to 184 questions, so a scripted rep cannot answer it. This is a
plain LLM chat loop — deliberately NOT an `Agent`, because it must not participate in the
AgentSession under test; it only turns VERA's last utterance into a rep-style reply.

The fact sheet states RULES, not 184 answers, because the plan's field space is repeating
structure: the coverage and `male_partner_coverage` sections are `Covered → Copay → Coinsurance →
Prior Auth` quads per CPT code, and `financial` is `Total → Met → Remaining` triples per bucket.
Enumerating them would be unmaintainable; the rules cover any code or bucket VERA happens to ask
about.

Answers are improvised within those rules, so evals built on this may assert on conversational
FLOW (handoffs, carried context, re-asking) but never on extracted values — value correctness
belongs to the Observer and needs a real call.
"""

from livekit.agents.llm import ChatContext
from livekit.plugins import google

# Modelled on a real reference call, where the rep answered nearly every question and volunteered
# copay/coinsurance/authorization together. Synthetic values only.
FACT_SHEET = """\
You are looking at the member's plan in your system.

## Plan and policy
- Plan: UnitedHealthcare Choice Plus, plan type PPO. Active, effective January 1 2026, no
  termination date. Coordination of benefits: this plan is PRIMARY.
- Member: Test T, date of birth April 12 1991. She IS on the plan and IS the subscriber.
- Spouse/partner on the policy: Alex T, date of birth March 3 1989.
- Policy / member ID POL-661522. Group name "Springfield Manufacturing", group number GRP-88421.
- Policy situs (contract state): Illinois. Benefit year runs on a CALENDAR year.
- This is an INDIVIDUAL policy, not family. Telehealth IS covered.
- The plan is FULLY FUNDED (not self-insured). The employer group is a SMALL group.
- There IS an infertility plan mandate on this policy.
- Out-of-network benefits ARE covered. No referral is required. No waiting period, and
  pre-existing conditions are not excluded. Not a COBRA plan, no other coverage on file.
- The requesting provider (Dr. Jane Smith, NPI 1982736450) is OUT of network, and the facility
  (Demo Health Partners) is OUT of network.

## Coverage — apply this RULE to any CPT code or named service you are asked about
- Infertility treatment IS covered under this plan.
- Diagnostic labs, X-ray and ultrasound with ICD-10 Z31.41 ARE covered: $40 copay, 40%
  coinsurance, no prior authorization.
- These specific codes ARE covered — $40 copay, 40% coinsurance, prior authorization NOT
  required: 89320, 89259, 99211.
- IVF-related codes require prior authorization when covered.
- EVERY OTHER CPT code or service you are asked about is NOT covered. Say so plainly and do not
  quote a copay for it.
- Embryo cryopreservation storage: covered for 12 months.

## Financial — apply this RULE to any dollar amount you are asked about
Always give the total, the amount met and the remaining amount together, in one reply.
- Individual deductible: total $3,000, $1,250 met, $1,750 remaining.
- Family deductible: total $6,000, $1,250 met, $4,750 remaining.
- Individual out-of-pocket maximum: total $7,500, $1,250 met, $6,250 remaining.
- Family out-of-pocket maximum: total $15,000, $1,250 met, $13,750 remaining.
- Infertility lifetime maximum: total $25,000, $0 used, $25,000 remaining. It applies to
  DIAGNOSTIC AND INFERTILITY TREATMENT.
- Infertility lifetime cycle maximum: 4 cycles, 0 used.
- Deductibles are separate for medical and mental health, and do not include the copay.

## Male partner
- Male partner fertility services ARE covered, under the coverage rule above.

## Administrative
- Enrollment is NOT required.
- Authorization department: "UHC Prior Auth", phone 800-555-0142.
- A center of excellence is NOT required.
- There is NO third party administrator and NO pharmacy benefit manager.
- There is NO infertility specialty pharmacy.

## Closing
- Your name is Martha Reed. Your call reference number is 841026.

## The only things you genuinely cannot see
If, and ONLY if, you are asked about one of these, say you cannot see it:
- the plan year renewal date
- whether the deductible cross-accumulates with another carrier
"""

_PERSONA = """\
You are Martha, a provider-services representative at UnitedHealthcare, on a recorded phone call \
with an automated benefits-verification assistant. You are reading the member's plan on screen.

How you answer:
- ANSWER THE QUESTION. You are a competent rep with the plan in front of you — apply the rules in
  your plan notes rather than declining. Only the two listed "cannot see" items get a "I don't
  have that in front of me"; everything else gets a real answer.
- Asked whether a CPT code or service is covered: answer yes or no per the coverage rule, and when
  it IS covered give the copay, the coinsurance and whether prior authorization is required in the
  same reply, without being asked for each.
- Asked about a dollar amount: give the total, the amount met and the remaining amount in one
  reply.
- When the assistant reads a value back to confirm, confirm it plainly ("Yes, that's correct").
- Be terse, the way a busy rep is — usually one short sentence. No pleasantries, no bullet lists.
- Answer only what was asked. Do not volunteer unrelated benefits.
- Never invent a number, date or code that is not in your plan notes.
- Never mention that you are an AI or that this is a simulation.
"""


class SimulatedRep:
    """Keeps its own conversation so its answers stay consistent across a long call."""

    def __init__(self, llm: google.LLM, facts: str = FACT_SHEET) -> None:
        self._llm = llm
        self._ctx = ChatContext.empty()
        self._ctx.add_message(role="system", content=f"{_PERSONA}\n{facts}")

    async def reply(self, vera_said: str) -> str:
        """One rep turn in answer to `vera_said`."""
        self._ctx.add_message(role="user", content=vera_said)
        parts: list[str] = []
        stream = self._llm.chat(chat_ctx=self._ctx)
        try:
            async for chunk in stream:
                if chunk.delta is not None and chunk.delta.content:
                    parts.append(chunk.delta.content)
        finally:
            await stream.aclose()
        # An empty completion would stall the call; a neutral prompt keeps VERA moving.
        answer = "".join(parts).strip() or "Sorry, could you repeat that?"
        self._ctx.add_message(role="assistant", content=answer)
        return answer


# --- fact sheets that trip the compiled schema's rules -------------------------------------
# The default FACT_SHEET deliberately fires NO rule (out-of-network IS covered; the mandate and
# infertility coverage agree), so a rule scenario has to supply its own facts.

# `mandate_requires_infertility_coverage` (ibv_standard.py) — a plan mandate obliges coverage,
# so claiming it is not covered is self-contradictory. This is the push-back seen in the reference
# call: "With a mandate, infertility services should be covered. Could you double-check…"
MANDATE_CONTRADICTION_FACTS = """\
You are looking at the member's plan in your system.

- Plan: UnitedHealthcare Choice Plus, plan type PPO, active, effective January 1 2026.
- Member: Test T, date of birth April 12 1991, policy POL-661522, group number GRP-88421.
- There IS an infertility plan mandate on this policy.
- Infertility treatment is NOT covered under this plan.
- Every CPT code you are asked about is NOT covered.
- Your name is Martha Reed. Your call reference number is 841026.
"""

# `insurance_not_active` (ibv_standard.py) — an inactive policy ends the call, so the plan
# skips straight to wrap_up instead of collecting benefits nobody can use.
INACTIVE_POLICY_FACTS = """\
You are looking at the member's plan in your system.

- Member: Test T, date of birth April 12 1991, policy POL-661522.
- The insurance is NOT active. The policy terminated on December 31 2025.
- Because the policy is inactive you have no benefit details to give: if asked about deductibles,
  coverage or CPT codes, say the plan is not active so there are no active benefits.
- Your name is Martha Reed. Your call reference number is 841026.
"""
