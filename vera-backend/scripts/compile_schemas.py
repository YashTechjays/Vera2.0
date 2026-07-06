"""Compile every catalog form schema into its canonical JSON artifact.

Usage:
    uv run python scripts/compile_schemas.py           # write data/form_schemas/*.json
    uv run python scripts/compile_schemas.py --check   # fail if any artifact is stale

The committed artifacts are lockfile-style outputs: authored in
``vera_core.forms.catalog``, regenerated here, and asserted fresh by
``tests/unit/forms/test_schema_dsl.py`` (so ``just check`` catches drift and
hand-edits of the compiled JSON).
"""

from __future__ import annotations

import sys
from pathlib import Path

from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import compile_document

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "form_schemas"


def main(check_only: bool) -> int:
    stale: list[str] = []
    for insurance_type, (filename, build) in SCHEMAS.items():
        target = OUT_DIR / filename
        compiled = compile_document(build())
        current = target.read_text() if target.exists() else None
        if compiled == current:
            print(f"{insurance_type}: {filename} up to date")
            continue
        if check_only:
            stale.append(filename)
            print(f"{insurance_type}: {filename} STALE (rerun `just compile-schemas`)")
        else:
            target.write_text(compiled)
            print(f"{insurance_type}: wrote {filename}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main(check_only="--check" in sys.argv[1:]))
