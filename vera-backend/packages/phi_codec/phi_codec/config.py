"""Central configuration: entity taxonomy, token syntax, thresholds, TTLs.

Everything tunable lives here so the latency/recall trade-offs called out in the
design are in one place rather than scattered across detectors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class EntityType(str, Enum):
    """Semantic PHI types, aligned to the HIPAA Safe Harbor §3.3 table. The string
    value is what appears inside a token, e.g. EntityType.BENEFICIARY_ID ->
    ``[[BENEFICIARY_ID_1]]``."""

    NAME = "NAME"  # 1
    STREET_ADDRESS = "STREET_ADDRESS"  # 2 (street)
    CITY = "CITY"  # 2 (city; State abbrev retained, never tokenized)
    ZIP_CODE = "ZIP_CODE"  # 2 (zip)
    DATE = "DATE"  # 3 (year retained)
    AGE_OVER_89 = "AGE_OVER_89"  # 3 (ages >89 generalized)
    PHONE = "PHONE"  # 4
    FAX = "FAX"  # 5
    EMAIL = "EMAIL"  # 6
    SSN = "SSN"  # 7
    MRN = "MRN"  # 8
    BENEFICIARY_ID = "BENEFICIARY_ID"  # 9 (health-plan member/beneficiary ID)
    MBI = "MBI"  # 9 (Medicare Beneficiary Identifier — specific subtype)
    ACCOUNT = "ACCOUNT"  # 10
    LICENSE = "LICENSE"  # 11 (certificate/license incl. NPI, DEA, driver license)
    VEHICLE = "VEHICLE"  # 12
    DEVICE_SERIAL = "DEVICE_SERIAL"  # 13
    URL = "URL"  # 14
    IP_ADDRESS = "IP_ADDRESS"  # 15
    UNIQUE_CODE = "UNIQUE_CODE"  # 18 (catch-all: auth tokens, claim/transaction IDs)


# Presidio's built-in + custom entity labels -> our semantic types. Anything detected
# that maps here is tokenized; everything else is ignored.
PRESIDIO_LABEL_MAP: dict[str, EntityType] = {
    # built-in recognizers
    "PERSON": EntityType.NAME,
    "DATE_TIME": EntityType.DATE,
    "PHONE_NUMBER": EntityType.PHONE,
    "US_SSN": EntityType.SSN,
    "EMAIL_ADDRESS": EntityType.EMAIL,
    "EMAIL": EntityType.EMAIL,
    "URL": EntityType.URL,
    "IP_ADDRESS": EntityType.IP_ADDRESS,
    "MEDICAL_LICENSE": EntityType.LICENSE,
    "US_DRIVER_LICENSE": EntityType.LICENSE,
    "US_PASSPORT": EntityType.UNIQUE_CODE,
    "LOCATION": EntityType.CITY,
    "MEDICARE_BENEFICIARY_IDENTIFIER": EntityType.MBI,
    "US_MBI": EntityType.MBI,
    # custom recognizer labels (see detection/recognizers.py)
    "BENEFICIARY_ID": EntityType.BENEFICIARY_ID,
    "MRN": EntityType.MRN,
    "ACCOUNT": EntityType.ACCOUNT,
    "ZIP_CODE": EntityType.ZIP_CODE,
    "STREET_ADDRESS": EntityType.STREET_ADDRESS,
    "MBI": EntityType.MBI,
    "FAX": EntityType.FAX,
    "LICENSE": EntityType.LICENSE,
    "VEHICLE": EntityType.VEHICLE,
    "DEVICE_SERIAL": EntityType.DEVICE_SERIAL,
    "UNIQUE_CODE": EntityType.UNIQUE_CODE,
    "AGE_OVER_89": EntityType.AGE_OVER_89,
}


@dataclass(frozen=True)
class CodecConfig:
    """Tunable knobs for one codec instance."""

    # Which identifiers we actually run detection for on a payer eligibility call.
    # Scoping this list is the primary latency lever (skip the Safe-Harbor long tail).
    active_entities: tuple[EntityType, ...] = (
        EntityType.NAME,
        EntityType.STREET_ADDRESS,
        EntityType.CITY,
        EntityType.ZIP_CODE,
        EntityType.DATE,
        EntityType.AGE_OVER_89,
        EntityType.PHONE,
        EntityType.FAX,
        EntityType.EMAIL,
        EntityType.SSN,
        EntityType.MRN,
        EntityType.BENEFICIARY_ID,
        EntityType.MBI,
        EntityType.ACCOUNT,
        EntityType.LICENSE,
        EntityType.VEHICLE,
        EntityType.DEVICE_SERIAL,
        EntityType.URL,
        EntityType.IP_ADDRESS,
        EntityType.UNIQUE_CODE,
    )

    # Recall-favoring: a low floor lets weak-but-real matches through. The leak
    # canary is the backstop against the false positives this admits.
    min_detection_score: float = 0.3

    # Whether to run the GLiNER name/location backend (tier 2). When False or the
    # dep is missing, detection runs regex-only (degraded recall on free-text names).
    use_gliner: bool = True
    gliner_model: str = "urchade/gliner_multi_pii-v1"

    # Hard cap on the GLiNER pass; on timeout we fall back to regex-only for the turn.
    ner_timeout_s: float = 0.150

    # Hot-vault session lifetime. Extended on activity; call end triggers close_session.
    session_ttl_s: int = 60 * 60  # 1h: call length + grace

    # spaCy model used purely for tokenization when GLiNER provides the NER.
    spacy_model: str = "en_core_web_sm"

    # Phonetic matching for seeded free-text values (names/cities): catches STT garbles
    # like "Kathryn" for a seeded "Catherine". Cheap — only runs against this patient's
    # seeded record, not a global list.
    use_phonetic_seed: bool = True
    phonetic_threshold: float = 0.9  # Jaro-Winkler floor (Metaphone equality also matches)

    def is_active(self, entity: EntityType) -> bool:
        return entity in self.active_entities

    @property
    def active_presidio_labels(self) -> list[str]:
        """Presidio entity labels to request in analyze(entities=[...])."""
        wanted = set(self.active_entities)
        return sorted({label for label, et in PRESIDIO_LABEL_MAP.items() if et in wanted})


DEFAULT_CONFIG = CodecConfig()


# We own the mapping from common external/EHR field names to our canonical Safe Harbor
# types, so callers can seed with their own vocabulary while the taxonomy stays closed,
# consistent, and auditable. Note this intentionally collapses some distinctions
# (GROUP_NUMBER and MEMBER_ID both -> BENEFICIARY_ID); keep them separate only if the
# payer connector needs the distinction (then add dedicated types, don't open the vocab).
SEED_ALIASES: dict[str, EntityType] = {
    # health-plan IDs
    "MEMBER_ID": EntityType.BENEFICIARY_ID,
    "MEMBERID": EntityType.BENEFICIARY_ID,
    "SUBSCRIBER_ID": EntityType.BENEFICIARY_ID,
    "SUBSCRIBER": EntityType.BENEFICIARY_ID,
    "POLICY_ID": EntityType.BENEFICIARY_ID,
    "POLICY_NUMBER": EntityType.BENEFICIARY_ID,
    "GROUP_NUMBER": EntityType.BENEFICIARY_ID,
    "GROUP_ID": EntityType.BENEFICIARY_ID,
    "PLAN_ID": EntityType.BENEFICIARY_ID,
    "INSURED_ID": EntityType.BENEFICIARY_ID,
    "MEDICARE_ID": EntityType.MBI,
    "MEDICARE_BENEFICIARY_IDENTIFIER": EntityType.MBI,
    # contact
    "PHONE_NUMBER": EntityType.PHONE,
    "TELEPHONE": EntityType.PHONE,
    "MOBILE": EntityType.PHONE,
    "CELL": EntityType.PHONE,
    "CELL_PHONE": EntityType.PHONE,
    "CALLBACK": EntityType.PHONE,
    "CALLBACK_NUMBER": EntityType.PHONE,
    "FAX_NUMBER": EntityType.FAX,
    "EMAIL_ADDRESS": EntityType.EMAIL,
    "E_MAIL": EntityType.EMAIL,
    "WEBSITE": EntityType.URL,
    # person / dates
    "PATIENT_NAME": EntityType.NAME,
    "FULL_NAME": EntityType.NAME,
    "FIRST_NAME": EntityType.NAME,
    "LAST_NAME": EntityType.NAME,
    "DOB": EntityType.DATE,
    "DATE_OF_BIRTH": EntityType.DATE,
    "BIRTHDATE": EntityType.DATE,
    "BIRTH_DATE": EntityType.DATE,
    # geography
    "ADDRESS": EntityType.STREET_ADDRESS,
    "STREET": EntityType.STREET_ADDRESS,
    "TOWN": EntityType.CITY,
    "ZIP": EntityType.ZIP_CODE,
    "ZIPCODE": EntityType.ZIP_CODE,
    "POSTAL_CODE": EntityType.ZIP_CODE,
    "POSTCODE": EntityType.ZIP_CODE,
    # other identifiers
    "SOCIAL_SECURITY": EntityType.SSN,
    "SOCIAL_SECURITY_NUMBER": EntityType.SSN,
    "NPI": EntityType.LICENSE,
    "DEA": EntityType.LICENSE,
    "LICENSE_NUMBER": EntityType.LICENSE,
    "MEDICAL_LICENSE": EntityType.LICENSE,
    "RECORD_NUMBER": EntityType.MRN,
    "MEDICAL_RECORD_NUMBER": EntityType.MRN,
    "CHART_NUMBER": EntityType.MRN,
    "ACCOUNT_NUMBER": EntityType.ACCOUNT,
    "ACCT": EntityType.ACCOUNT,
    "ACCT_NUMBER": EntityType.ACCOUNT,
    "SERIAL_NUMBER": EntityType.DEVICE_SERIAL,
    "DEVICE_SERIAL_NUMBER": EntityType.DEVICE_SERIAL,
    "VIN": EntityType.VEHICLE,
    "LICENSE_PLATE": EntityType.VEHICLE,
    "AUTH_CODE": EntityType.UNIQUE_CODE,
    "AUTHORIZATION_NUMBER": EntityType.UNIQUE_CODE,
    "REFERENCE_NUMBER": EntityType.UNIQUE_CODE,
}


def resolve_entity_type(name: str) -> EntityType:
    """Map a seed key to a canonical EntityType.

    Accepts: a canonical type ("BENEFICIARY_ID"), a known alias ("MEMBER_ID",
    "GROUP_NUMBER", "DOB"), and a token-style key with a trailing index
    ("BENEFICIARY_ID_2" — useful because JSON can't repeat a key). Spaces/hyphens
    are normalized to underscores and case is ignored. Raises ValueError otherwise.
    """
    key = re.sub(r"[\s\-]+", "_", name.strip()).upper()
    for candidate in (key, re.sub(r"_\d+$", "", key)):
        try:
            return EntityType(candidate)
        except ValueError:
            pass
        if candidate in SEED_ALIASES:
            return SEED_ALIASES[candidate]
    raise ValueError(
        f"unknown seed type {name!r} — use a canonical type, a known alias "
        f"(e.g. MEMBER_ID, GROUP_NUMBER, DOB, PHONE_NUMBER), and a list for multiple "
        f"values of one type"
    )
