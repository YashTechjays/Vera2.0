"""One-off: normalize coverage sections into per-row CPT tables.

Handles three encodings found in ibv-form-v1-parity-new.json:
  1. nested code-bearing objects with cpt_codes="..." attrs   (infertility_treatment)
  2. a single object with codes listed in verbatim_prompt      (diagnostic_*)
  3. a single object with flat dotted service keys, e.g.
     office_visits.covered                                     (general_coverage,
                                                                  male_partner_coverage)

Output is uniform: each section becomes groups → CPT rows → coverage columns.
`copay_coinsurance` is split into Copay + Coinsurance, the ICD-10 is resolved
(attribute or spoken form) onto a clean `icd10` field, and the CPT code lives in
each row's `title` (keys stay stable: cpt_1, cpt_2…).

NOTE: CURATED_CPT holds codes that are NOT in the schema (taken from legacy
screenshots). These should move into vera-schema-builder as structured data.
Mirror the whole transform there when ready.
"""
import json
import re

PATH = "src/lib/ibv/ibv-schema.json"

# CPT codes absent from the schema, transcribed from legacy screenshots.
# Keyed by service key. CONFIRM these and migrate into the schema-builder.
CURATED_CPT = {
    "office_visits": ["99211"],
    "asc_professional": ["58555"],
    "asc_facility": ["58555"],
    "semen_analysis": ["89320"],
    "sperm_cryo": ["89259"],
}

# Display labels for dotted services where the schema's humanized title is poor.
SERVICE_LABELS = {
    "semen_analysis": "Semen Analysis (SA)",
    "sperm_cryo": "Sperm Cryopreservation",
}

# Fields to omit from the form (not needed in the UI).
OMIT_FIELDS = {"male_partner_covered"}

WORD = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "point": ".",
}


def spoken_to_code(text):
    """'Z-three-one... point four-one' -> 'Z31.41'."""
    out = []
    for tok in re.split(r"[^a-zA-Z]+", text):
        low = tok.lower()
        if low in WORD:
            out.append(WORD[low])
        elif len(tok) == 1 and tok.isalpha():
            out.append(tok.upper())
    return "".join(out)


def extract_icd(vp):
    m = re.search(r'icd10="([^"]*)"', vp)
    if m:
        return m.group(1).strip()
    m = re.search(r'code is[.\s]*([^"]*?)\."', vp)
    return spoken_to_code(m.group(1)) if m else ""


def extract_cpts(vp):
    codes = []
    for m in re.findall(r"\b\d{5}\b", vp):
        if m not in codes:
            codes.append(m)
    return codes


def clean_title(title, key):
    title = title or key
    return title.split(" — ")[-1].strip() if " — " in title else title


def text_field(title):
    return {"type": "string", "title": title, "ui": {"widget": "text"}}


def derive_columns(props):
    """Coverage columns from leaf fields; split copay_coinsurance; clean titles.
    The Covered column is a Yes/No/N/A dropdown."""
    cols = []
    for key, f in props.items():
        if f.get("type") != "string" or f.get("prompt_role") == "prose":
            continue
        if key == "copay_coinsurance":
            cols.append(("copay", text_field("Copay")))
            cols.append(("coinsurance", text_field("Coinsurance")))
        elif key == "covered":
            field = text_field(clean_title(f.get("title"), key))
            field["constraint_ref"] = "YES_NO_NA"
            cols.append((key, field))
        elif key == "prior_auth":
            field = text_field("Auth Req")
            field["constraint_ref"] = "YES_NO_NA"
            cols.append((key, field))
        else:
            cols.append((key, text_field(clean_title(f.get("title"), key))))
    return cols


def make_group(label, icd, codes, cols):
    if not codes:
        codes = ["—"]
    rows = {}
    for i, code in enumerate(codes):
        rows[f"cpt_{i + 1}"] = {
            "type": "object",
            "title": code,
            "properties": {ck: dict(cv) for ck, cv in cols},
        }
    return {"type": "object", "title": label, "icd10": icd, "properties": rows}


def transform_dotted(section):
    """Sections whose object holds flat dotted service keys -> service groups."""
    new_props = {}
    for okey, oval in section["properties"].items():
        if oval.get("type") != "object":
            new_props[okey] = oval
            continue
        props = oval.get("properties", {})
        if not any("." in k for k in props):
            new_props[okey] = oval
            continue

        icd = extract_icd(oval.get("verbatim_prompt", "") or "")
        services, order = {}, []
        for k, v in props.items():
            if "." in k:
                svc, suf = k.split(".", 1)
                if svc not in services:
                    services[svc], _ = {}, order.append(svc)
                services[svc][suf] = v
            elif k not in OMIT_FIELDS:
                new_props[k] = v  # non-service field -> section lead field

        for svc in order:
            sufs = services[svc]
            any_title = next(iter(sufs.values())).get("title", "")
            label = any_title.split(" — ")[0].strip() if " — " in any_title else svc
            label = SERVICE_LABELS.get(svc, label)
            new_props[svc] = make_group(
                label, icd, CURATED_CPT.get(svc, []), derive_columns(sufs)
            )
    section["properties"] = new_props


def transform_code_bearing(section):
    """Sections with code-bearing nested objects -> per-CPT rows."""
    new_props = {}
    for key, val in section["properties"].items():
        if val.get("type") != "object":
            new_props[key] = val
            continue
        cols = derive_columns(val.get("properties", {}))
        if not cols:
            new_props[key] = val
            continue
        vp = val.get("verbatim_prompt", "") or ""
        group = make_group(
            val.get("title", key), extract_icd(vp), extract_cpts(vp), cols
        )
        new_val = {k: v for k, v in val.items() if k not in ("properties", "required")}
        new_val.update(group)
        new_props[key] = new_val
    section["properties"] = new_props


def patch_insurance_information(section):
    """Match the legacy Insurance Information layout: add COB / Group Name /
    Home Plan (absent from the schema) and order fields per the reference form.
    Migrate these field definitions into vera-schema-builder when ready."""
    p = section["properties"]

    def make(title, required=False, cref=None):
        f = {"type": "string", "title": title, "ui": {"widget": "text"}}
        if required:
            f["required_state"] = "required"
        if cref:
            f["constraint_ref"] = cref
        return f

    def retitle(key, title, fallback):
        f = p.get(key) or fallback
        f["title"] = title
        return f

    section["properties"] = {
        "doctor_inside_network": p.get("doctor_inside_network"),
        "facility_inside_network": p.get("facility_inside_network"),
        "out_of_network_coverage": p.get("out_of_network_coverage"),
        "health_plan": p.get("health_plan"),
        "coordination_of_benefits": make(
            "Coordination of Ben (COB)", cref="COORDINATION_OF_BENEFITS"
        ),
        "policy_number": retitle("policy_number", "Policy # (Mandatory)", make("Policy # (Mandatory)", required=True)),
        "group_information": retitle("group_information", "Group #", make("Group #")),
        "group_name": make("Group Name"),
        "home_plan": make("Home Plan"),
    }


def patch_benefit_coverage(section):
    """Add Plan Year Information + a separate Telehealth dropdown (absent from the
    schema) and order fields per the reference form."""
    p = section["properties"]
    plan_year = {
        "type": "string", "title": "Plan Year Information", "ui": {"widget": "text"},
    }
    telehealth = {
        "type": "string", "title": "Telehealth",
        "ui": {"widget": "text"}, "constraint_ref": "YES_NO",
    }
    section["properties"] = {
        "benefit_year_type": p.get("benefit_year_type"),
        "plan_effective_date": p.get("plan_effective_date"),
        "plan_year_information": plan_year,
        "coverage_type": p.get("coverage_type"),
        "referrals_telehealth": p.get("referrals_telehealth"),
        "telehealth": telehealth,
        "plan_fund_type": p.get("plan_fund_type"),
        "employer_support_size": p.get("employer_support_size"),
        "infertility_plan_mandate": p.get("infertility_plan_mandate"),
    }


def patch_deductibles_oop(section):
    """Build the Accumulation grid (Deductible / Out of Pocket x Individual /
    Family) — the schema only has a single free-text field for it."""
    def cell(title):
        return {"type": "string", "title": title, "ui": {"widget": "text"}}

    def row(title):
        return {
            "type": "object", "title": title,
            "properties": {"individual": cell("Individual"), "family": cell("Family")},
        }

    section["title"] = "Accumulation"
    section["properties"] = {
        "deductible": {
            "type": "object", "title": "Deductible",
            "properties": {
                "r1": row("Deductible:"),
                "r2": row("Met Amount:"),
                "r3": row("Remaining:"),
            },
        },
        "out_of_pocket": {
            "type": "object", "title": "Out of Pocket",
            "properties": {
                "r1": row("Out-of-Pocket:"),
                "r2": row("Met Amount:"),
                "r3": row("Remaining:"),
            },
        },
    }


def patch_infertility_limits(section):
    """Build the Lifetime Maximum grid (LTM / Met Amount / Remaining /
    Applicable Area x Value + Additional Notes)."""
    def cell(title):
        return {"type": "string", "title": title, "ui": {"widget": "text"}}

    def row(title):
        return {
            "type": "object", "title": title,
            "properties": {"value": cell("")},
        }

    section["properties"] = {
        "lifetime_maximum": {
            "type": "object", "title": "Lifetime Maximum",
            "properties": {
                "r1": row("LTM"),
                "r2": row("Met Amount"),
                "r3": row("Remaining"),
                "r4": row("Applicable Area"),
                # group-level: one Additional Notes spanning all rows
                "additional_notes": cell("Additional Notes"),
            },
        }
    }


# Simple sections: an ordered list of (key, title, constraint_ref|None) fields,
# replacing the schema's single placeholder field. Per the reference form.
SIMPLE_SECTIONS = {
    "enrollment": [
        ("enrollment_required", "Enrollment Required?", "YES_NO"),
        ("provider_name", "Provider Name", None),
        ("provider_phone_number", "Provider Phone Number", None),
        ("center_of_excellence_required", "Center of Excellence Required?", "YES_NO"),
    ],
    "authorization_department": [
        ("authorization_dept_name", "Authorization Dept Name", None),
        ("auth_dept_phone_number", "Auth Dept Phone Number", None),
    ],
    "pharmacy": [
        ("pharmacy_benefit_manager", "Pharmacy Benefit Manager", None),
        ("pbm_phone_number", "PBM Phone Number", None),
    ],
    "infertility_specialty_pharmacy": [
        ("infertility_specialty_pharmacy", "Infertility Specialty Pharmacy (ISP)", None),
        ("isp_phone_number", "ISP Phone Number", None),
    ],
    "insurance_representative": [
        ("insurance_rep_name", "Insurance Rep Name", None),
        ("call_reference_number", "Call Reference #", None),
        ("web_portal_ref_number", "Web Portal Ref #", None),
    ],
}


def patch_simple_section(section, fields):
    def field(title, cref):
        f = {"type": "string", "title": title, "ui": {"widget": "text"}}
        if cref:
            f["constraint_ref"] = cref
        return f

    section["properties"] = {k: field(t, c) for k, t, c in fields}


# First-column header for each matrix table (overrides derived header).
ROW_HEADERS = {
    "infertility_treatment": "Infertility Treatment (TX)",
    "diagnostic_labs_xray_ultrasound": "Diagnostic Testing (DX)",
    "general_coverage": "Service",
    "male_partner_coverage": "Male Partner",
    "deductibles_oop": "INN",
    "infertility_limits": "Lifetime Maximum",
}

d = json.load(open(PATH, encoding="utf-8"))
done = []

for section in d["sections"]:
    if section["section_key"] == "insurance_information":
        patch_insurance_information(section)
        done.append(section["section_key"])
        continue
    if section["section_key"] == "benefit_coverage":
        patch_benefit_coverage(section)
        done.append(section["section_key"])
        continue
    if section["section_key"] == "deductibles_oop":
        patch_deductibles_oop(section)
        done.append(section["section_key"])
        continue
    if section["section_key"] == "infertility_limits":
        patch_infertility_limits(section)
        done.append(section["section_key"])
        continue
    if section["section_key"] in SIMPLE_SECTIONS:
        patch_simple_section(section, SIMPLE_SECTIONS[section["section_key"]])
        done.append(section["section_key"])
        continue

    objs = [v for v in section["properties"].values() if v.get("type") == "object"]
    has_dotted = any(
        "." in k for v in objs for k in (v.get("properties") or {})
    )
    has_codes = any(
        extract_cpts(v.get("verbatim_prompt", "") or "") for v in objs
    )
    if has_dotted:
        transform_dotted(section)
        done.append(section["section_key"])
    elif has_codes:
        transform_code_bearing(section)
        done.append(section["section_key"])

# Reorder sections: place `key` immediately after `after`.
secs = d["sections"]


def move_after(key, after):
    keys = [s["section_key"] for s in secs]
    if key not in keys or after not in keys:
        return
    node = next(s for s in secs if s["section_key"] == key)
    secs.remove(node)
    idx = next(i for i, s in enumerate(secs) if s["section_key"] == after)
    secs.insert(idx + 1, node)


move_after("general_coverage", "benefit_coverage")
move_after("diagnostic_labs_xray_ultrasound", "general_coverage")
move_after("male_partner_coverage", "diagnostic_labs_xray_ultrasound")
move_after("deductibles_oop", "infertility_treatment")
move_after("embryo_cryo_storage", "deductibles_oop")
move_after("infertility_limits", "embryo_cryo_storage")
move_after("pharmacy", "authorization_department")
move_after("infertility_specialty_pharmacy", "pharmacy")
move_after("third_party", "infertility_specialty_pharmacy")
move_after("insurance_representative", "third_party")

# Apply explicit first-column headers for the matrix tables.
for s in d["sections"]:
    if s["section_key"] in ROW_HEADERS:
        s["row_header"] = ROW_HEADERS[s["section_key"]]

json.dump(d, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("transformed sections:", done)
