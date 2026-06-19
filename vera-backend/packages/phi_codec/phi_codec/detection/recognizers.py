"""Custom Presidio PatternRecognizers for the structured identifiers that dominate
payer eligibility calls and aren't covered (well) by built-ins.

Entity labels here align to the HIPAA Safe Harbor §3.3 token names (BENEFICIARY_ID,
STREET_ADDRESS, ZIP_CODE, FAX, LICENSE, VEHICLE, DEVICE_SERIAL, UNIQUE_CODE,
AGE_OVER_89). These run in sub-millisecond regex time on the *normalized* text. Scores
are deliberately generous (recall over precision); the leak canary, proximity-weighted
context, and LLM-side semantics are the backstops.
"""

from __future__ import annotations

from presidio_analyzer import Pattern, PatternRecognizer

_BENEFICIARY_CONTEXT = [
    "member", "subscriber", "policy", "beneficiary", "id", "member id",
    "insured", "group", "group number", "group id", "plan",
]
_MRN_CONTEXT = ["mrn", "medical record", "record number", "chart"]
_ACCOUNT_CONTEXT = ["account", "acct", "claim", "reference", "ref", "guarantor"]
_ZIP_CONTEXT = ["zip", "zipcode", "postal", "address"]
_PHONE_CONTEXT = ["phone", "callback", "call back", "call", "reach", "contact", "cell", "telephone"]
_FAX_CONTEXT = ["fax", "facsimile"]
_LICENSE_CONTEXT = ["license", "licence", "npi", "provider", "dea", "certificate", "certification"]
_VEHICLE_CONTEXT = ["vehicle", "ambulance", "plate", "license plate", "unit", "transport", "vin"]
_DEVICE_CONTEXT = ["serial", "s/n", "sn", "device", "model", "pump", "implant"]
_UNIQUE_CONTEXT = [
    "authorization", "auth", "prior", "claim", "reference", "ref",
    "token", "transaction", "tracking", "case", "confirmation",
]
_AGE_CONTEXT = ["years old", "year old", "age", "aged", "y/o", "yo", "old"]


def _beneficiary_id_recognizer() -> PatternRecognizer:
    patterns = [
        # Alpha prefix + digits, e.g. XYZ987654321 (most common payer member ID shape).
        Pattern("benef alpha+digits", r"\b[A-Za-z]{1,4}\d{6,12}\b", 0.6),
        # Long all-digit IDs (9-13). Lower score; context/leak-canary disambiguates from SSN.
        Pattern("benef digits", r"\b\d{9,13}\b", 0.4),
        # Digits with internal letter(s), e.g. 12AB3456789 or AGXZ2434.
        Pattern("benef mixed", r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,15}\b", 0.45),
        # Short numeric IDs (244523) only become IDs via context ("member/group id").
        Pattern("benef short ctx", r"\b\d{4,8}\b", 0.3),
    ]
    return PatternRecognizer(
        supported_entity="BENEFICIARY_ID", patterns=patterns, context=_BENEFICIARY_CONTEXT
    )


def _mrn_recognizer() -> PatternRecognizer:
    patterns = [
        Pattern("mrn labeled", r"\bMRN\s*[:#-]?\s*([A-Z0-9-]{5,12})\b", 0.7),
        Pattern("mrn digits", r"\b\d{6,10}\b", 0.3),
    ]
    return PatternRecognizer(supported_entity="MRN", patterns=patterns, context=_MRN_CONTEXT)


def _account_recognizer() -> PatternRecognizer:
    patterns = [
        Pattern("account labeled", r"\b(?:acct|account|claim)\s*[:#]?\s*([A-Z0-9-]{5,15})\b", 0.6),
        Pattern("account hash", r"#\s*\d{5,12}\b", 0.5),
    ]
    return PatternRecognizer(
        supported_entity="ACCOUNT", patterns=patterns, context=_ACCOUNT_CONTEXT
    )


def _zip_code_recognizer() -> PatternRecognizer:
    patterns = [
        # Strong: a 5(-4) digit code right after a 2-letter state ("FL 32801").
        Pattern("zip after state", r"(?<=\b[A-Z]{2}\s)\d{5}(?:-\d{4})?\b", 0.6),
        # Weak: a bare 5(-4) digit code; needs zip/postal/address context to win.
        Pattern("zip", r"\b\d{5}(?:-\d{4})?\b", 0.3),
    ]
    return PatternRecognizer(supported_entity="ZIP_CODE", patterns=patterns, context=_ZIP_CONTEXT)


def _street_address_recognizer() -> PatternRecognizer:
    # House number + name + street-type suffix, e.g. "123 Magnolia Lane". High base score
    # so it wins over a spaCy CITY tag on an interior token (else the street leaks).
    suffix = (
        r"(?:Street|St|Avenue|Ave|Lane|Ln|Road|Rd|Boulevard|Blvd|Drive|Dr|Court|Ct|"
        r"Way|Place|Pl|Circle|Cir|Terrace|Ter|Highway|Hwy|Parkway|Pkwy|Trail|Trl|Square|Sq)"
    )
    pattern = rf"\b\d{{1,6}}\s+(?:[A-Z][A-Za-z]*\.?\s+){{1,4}}{suffix}\b\.?"
    return PatternRecognizer(
        supported_entity="STREET_ADDRESS",
        patterns=[Pattern("street address", pattern, 0.85)],
        context=["address", "street", "resides", "reside", "live", "located", "visit"],
    )


def _mbi_recognizer() -> PatternRecognizer:
    # Medicare Beneficiary Identifier: 11 chars, position-typed per CMS spec.
    c = r"[ACDEFGHJKMNPQRTUVWXY]"
    an = r"[0-9ACDEFGHJKMNPQRTUVWXY]"
    pattern = rf"\b\d{c}{an}\d{c}{an}\d{c}{c}\d\d\b"
    return PatternRecognizer(
        supported_entity="MBI",
        patterns=[Pattern("mbi", pattern, 0.85)],
        context=["medicare", "mbi", "beneficiary"],
    )


def _spoken_ssn_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="US_SSN",
        patterns=[Pattern("ssn 9 digits", r"\b\d{9}\b", 0.5)],
        context=["social", "security", "ssn", "social security"],
    )


def _spoken_phone_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="PHONE_NUMBER",
        patterns=[
            Pattern("phone 11 digits", r"\b1\d{10}\b", 0.4),
            Pattern("phone 10 digits", r"\b\d{10}\b", 0.4),
            Pattern("phone 7-11 ctx", r"\b\d{7,11}\b", 0.3),
        ],
        context=_PHONE_CONTEXT,
    )


def _fax_recognizer() -> PatternRecognizer:
    # Fax numbers look like phones; the "fax" cue is what distinguishes them. Ranked
    # above PHONE on a score/specificity tie so "fax ... 800-555-0122" -> FAX.
    return PatternRecognizer(
        supported_entity="FAX",
        patterns=[
            Pattern("fax formatted", r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", 0.35),
            Pattern("fax 10-11 digits", r"\b1?\d{10}\b", 0.35),
        ],
        context=_FAX_CONTEXT,
    )


def _license_recognizer() -> PatternRecognizer:
    # Provider/practitioner identifiers: NPI (10 digits), DEA (2 letters + 7 digits),
    # and state medical license formats like "MD-88391".
    return PatternRecognizer(
        supported_entity="LICENSE",
        patterns=[
            Pattern("npi", r"\b\d{10}\b", 0.3),
            Pattern("dea", r"\b[A-Za-z]{2}\d{7}\b", 0.5),
            Pattern("state license", r"\b[A-Z]{1,3}-\d{4,8}\b", 0.4),
        ],
        context=_LICENSE_CONTEXT,
    )


def _vehicle_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="VEHICLE",
        patterns=[
            # VIN: 17 alphanumerics excluding I, O, Q.
            Pattern("vin", r"\b[A-HJ-NPR-Z0-9]{17}\b", 0.6),
            # Unit/plate code right after "unit", e.g. "unit T-44".
            Pattern("unit code", r"(?<=\bunit\s)[A-Za-z]{1,3}-?\d{1,4}\b", 0.6),
        ],
        context=_VEHICLE_CONTEXT,
    )


def _device_serial_recognizer() -> PatternRecognizer:
    # Keep the device MODEL (needed for medical-necessity); tokenize only the serial.
    # Anchored on the "serial"/"s/n" label so we don't match ordinary alphanumerics
    # (Presidio runs patterns case-insensitively, so a bare [A-Z0-9]{n} would match words).
    return PatternRecognizer(
        supported_entity="DEVICE_SERIAL",
        patterns=[
            Pattern("serial labeled", r"(?<=serial:\s)(?=[A-Z0-9-]*\d)[A-Z0-9-]{3,15}\b", 0.75),
            Pattern("sn labeled", r"(?<=s/n\s)(?=[A-Z0-9-]*\d)[A-Z0-9-]{3,15}\b", 0.7),
        ],
        context=_DEVICE_CONTEXT,
    )


def _unique_code_recognizer() -> PatternRecognizer:
    # Catch-all for prefixed dashed codes: prior-auth tokens, claim/transaction IDs.
    return PatternRecognizer(
        supported_entity="UNIQUE_CODE",
        patterns=[
            Pattern("prefixed dashed code", r"\b[A-Z]{1,4}-\d{3,7}(?:-[A-Z0-9]{1,4})?\b", 0.5),
        ],
        context=_UNIQUE_CONTEXT,
    )


def _age_over_89_recognizer() -> PatternRecognizer:
    # Safe Harbor: ages <=89 may be retained; ages >89 must be generalized. Only match
    # 90-129, and capture the whole "N years old" phrase (high score) so it wins over
    # spaCy's DATE tag on the same span. A bare 90+ near "age" is the weaker fallback.
    return PatternRecognizer(
        supported_entity="AGE_OVER_89",
        patterns=[
            Pattern("age N years old", r"\b(?:9\d|1[0-2]\d)\s+years?\s+old\b", 0.85),
            Pattern("age N yo", r"\b(?:9\d|1[0-2]\d)\s*(?:y/o|yo)\b", 0.8),
            Pattern("aged N", r"(?<=age\s)(?:9\d|1[0-2]\d)\b", 0.7),
        ],
        context=_AGE_CONTEXT,
    )


def build_custom_recognizers() -> list[PatternRecognizer]:
    """All custom recognizers, ready to add to the registry."""
    return [
        _beneficiary_id_recognizer(),
        _mrn_recognizer(),
        _account_recognizer(),
        _zip_code_recognizer(),
        _street_address_recognizer(),
        _mbi_recognizer(),
        _spoken_ssn_recognizer(),
        _spoken_phone_recognizer(),
        _fax_recognizer(),
        _license_recognizer(),
        _vehicle_recognizer(),
        _device_serial_recognizer(),
        _unique_code_recognizer(),
        _age_over_89_recognizer(),
    ]
