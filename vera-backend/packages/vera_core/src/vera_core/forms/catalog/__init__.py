"""Author sources for compiled form schemas, one module per insurance type.

``SCHEMAS`` maps insurance_type -> (compiled artifact filename, builder). The compiler
(`scripts/compile_schemas.py`) and the freshness test iterate this registry, so adding
a new insurance type is: write a builder module, register it here, run
``just compile-schemas``.
"""

from collections.abc import Callable

from vera_core.forms.catalog.disease_only import build_disease_only
from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.forms.dsl import FormSchemaDoc

SCHEMAS: dict[str, tuple[str, Callable[[], FormSchemaDoc]]] = {
    "infertility_treatment": ("ibv_form_standard_v2.json", build_ibv_standard),
    "disease_only": ("disease_only_verification.json", build_disease_only),
}
