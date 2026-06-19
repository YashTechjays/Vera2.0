"""Synthetic spoken-form / written PHI generator with ground truth.

Recall on messy forms is the metric that matters for a voice pipeline, and clean
formatted strings overstate it. Each sample carries the text plus the canonical
(post-normalization) value(s) we expect tokenized, so the harness can measure
redaction recall (did it get tokenized at all — the leak metric) and type recall.

Covers the Safe Harbor §3.3 identifiers the codec targets. SSNs avoid Presidio's
hard-coded invalid/sample blocklist (000/666/9xx, all-same, group-zeros, canned
123456789 / 078051120), so the generator can't manufacture false negatives.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..config import EntityType

_DIGIT_WORD = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}
_FIRST = ["John", "Mary", "Robert", "Linda", "James", "Patricia", "Michael", "Susan"]
_LAST = ["Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis", "Nguyen"]
_STREETS = ["Magnolia Lane", "Oak Street", "Main Avenue", "Cedar Road", "Elm Court"]
_CITIES = ["Orlando", "Austin", "Denver", "Tampa", "Raleigh", "Phoenix"]
_STATES = ["FL", "TX", "CO", "NC", "AZ"]
_MBI_ALPHA = "ACDEFGHJKMNPQRTUVWXY"


@dataclass(frozen=True)
class GroundTruth:
    entity_type: EntityType
    value: str  # canonical (post-normalization) value expected in the vault


@dataclass(frozen=True)
class Sample:
    spoken_text: str
    truths: list[GroundTruth] = field(default_factory=list)


def _spell_digits(s: str) -> str:
    return " ".join(_DIGIT_WORD[c] for c in s)


def _spell_letters(s: str) -> str:
    return " ".join(list(s))


def _valid_ssn(rng: random.Random) -> str:
    while True:
        area = rng.randint(1, 899)
        if area == 666:
            continue
        digits = f"{area:03d}{rng.randint(1, 99):02d}{rng.randint(1, 9999):04d}"
        if len(set(digits)) == 1 or digits[3:5] == "00" or digits[5:] == "0000":
            continue
        if digits.startswith(("123456789", "078051120")):
            continue
        return digits


def _beneficiary_id(rng: random.Random) -> str:
    letters = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(rng.randint(2, 3)))
    return letters + "".join(str(rng.randint(0, 9)) for _ in range(rng.randint(8, 10)))


def _mbi(rng: random.Random) -> str:
    d = lambda: str(rng.randint(0, 9))
    a = lambda: rng.choice(_MBI_ALPHA)
    an = lambda: rng.choice(_MBI_ALPHA + "0123456789")
    return d() + a() + an() + d() + a() + an() + d() + a() + a() + d() + d()


def _phone(rng: random.Random) -> str:
    return f"{rng.randint(200,999)}{rng.randint(200,999)}{rng.randint(1000,9999)}"


def _name(rng: random.Random) -> str:
    return f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"


def _n_letters(s: str) -> int:
    return sum(1 for c in s if c.isalpha())


def make_sample(rng: random.Random) -> Sample:
    """One realistic eligibility-call utterance with 1-3 identifiers."""
    kind = rng.choice(
        ["benef", "ssn", "name", "phone", "mbi", "fax", "url", "ip",
         "license", "address", "age", "account", "unique", "combo"]
    )

    if kind == "benef":
        bid = _beneficiary_id(rng)
        nl = _n_letters(bid)
        spoken = f"the member id is {_spell_letters(bid[:nl])} {_spell_digits(bid[nl:])}"
        return Sample(spoken, [GroundTruth(EntityType.BENEFICIARY_ID, bid)])

    if kind == "ssn":
        ssn = _valid_ssn(rng)
        return Sample(f"social security number {_spell_digits(ssn)}", [GroundTruth(EntityType.SSN, ssn)])

    if kind == "name":
        name = _name(rng)
        return Sample(f"I'm calling on behalf of the patient {name}", [GroundTruth(EntityType.NAME, name)])

    if kind == "phone":
        ph = _phone(rng)
        return Sample(f"the callback number is {_spell_digits(ph)}", [GroundTruth(EntityType.PHONE, ph)])

    if kind == "mbi":
        mbi = _mbi(rng)
        spoken = f"the medicare beneficiary identifier is {' '.join(_DIGIT_WORD[c] if c.isdigit() else c for c in mbi)}"
        return Sample(spoken, [GroundTruth(EntityType.MBI, mbi)])

    if kind == "fax":
        fx = _phone(rng)
        return Sample(f"please fax the notes to {_spell_digits(fx)}", [GroundTruth(EntityType.FAX, fx)])

    if kind == "url":
        url = f"https://payer{rng.randint(1,9)}.example.com/auth/{rng.randint(10000,99999)}"
        return Sample(f"the guidelines are posted at {url}", [GroundTruth(EntityType.URL, url)])

    if kind == "ip":
        ip = f"{rng.randint(1,254)}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}"
        return Sample(f"system log routing {ip}", [GroundTruth(EntityType.IP_ADDRESS, ip)])

    if kind == "license":
        npi = "".join(str(rng.randint(0, 9)) for _ in range(10))
        return Sample(f"the requesting physician npi is {npi}", [GroundTruth(EntityType.LICENSE, npi)])

    if kind == "address":
        num = rng.randint(100, 9999)
        street = f"{num} {rng.choice(_STREETS)}"
        i = rng.randrange(len(_STATES))
        city, state, zc = _CITIES[i], _STATES[i], f"{rng.randint(10000,99999)}"
        spoken = f"the patient resides at {street}, {city}, {state} {zc}"
        return Sample(spoken, [
            GroundTruth(EntityType.STREET_ADDRESS, street),
            GroundTruth(EntityType.CITY, city),
            GroundTruth(EntityType.ZIP_CODE, zc),
        ])

    if kind == "age":
        age = rng.randint(90, 105)
        return Sample(f"the patient is {age} years old", [GroundTruth(EntityType.AGE_OVER_89, f"{age} years old")])

    if kind == "account":
        acct = f"{rng.randint(100000,9999999)}"
        return Sample(f"apply this to guarantor account #{acct}", [GroundTruth(EntityType.ACCOUNT, acct)])

    if kind == "unique":
        code = f"PA-{rng.randint(10000,99999)}-{rng.choice('XYZ')}"
        return Sample(f"the prior authorization token is {code}", [GroundTruth(EntityType.UNIQUE_CODE, code)])

    # combo: name + beneficiary id in one turn
    name = _name(rng)
    bid = _beneficiary_id(rng)
    nl = _n_letters(bid)
    spoken = f"this is for {name}, member {_spell_letters(bid[:nl])} {_spell_digits(bid[nl:])}"
    return Sample(spoken, [GroundTruth(EntityType.NAME, name), GroundTruth(EntityType.BENEFICIARY_ID, bid)])


def generate(n: int, *, seed: int = 0) -> list[Sample]:
    rng = random.Random(seed)
    return [make_sample(rng) for _ in range(n)]
