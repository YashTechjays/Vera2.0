"""Dependency floors that exist for security reasons, not just compatibility.

A version specifier cannot record WHY it sits where it does. These tests do, so a
later relaxation fails with the reason attached instead of silently re-admitting a
known-vulnerable transitive pin.
"""

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.version import Version

_VERA_BACKEND = Path(__file__).resolve().parents[2]

#: livekit-agents pins json-repair EXACTLY, so our floor decides which pin is reachable:
#:
#:     livekit-agents 1.6.4 -> json-repair==0.59.10   GHSA-xf7x-x43h-rpqh
#:     livekit-agents 1.6.5 -> json-repair==0.59.10   vulnerable
#:     livekit-agents 1.6.6 -> json-repair==0.60.1    patched
#:     livekit-agents 1.6.7 -> json-repair==0.60.1    patched
_MIN_LIVEKIT_AGENTS = Version("1.6.6")
_MIN_JSON_REPAIR = Version("0.60.1")

#: 50.0.0 is the first release clearing BOTH, which is why the floor is 50 and not 49:
#:
#:     CVE-2026-69247  PKCS#7 EnvelopedData decryption is a Bleichenbacher oracle  fixed 50.0.0
#:     CVE-2026-69249  duplicate self-signed intermediates -> exponential paths    fixed 49.0.0
_MIN_CRYPTOGRAPHY = Version("50")

#: CVE-2026-69244: out-of-bounds heap read in the C response parser's error path.
_MIN_AIOHTTP = Version("3.14.3")

#: (package dir, distribution, floor, what relaxing it re-admits) — one row per declared floor.
_DECLARED_FLOORS = (
    ("packages/vera_core", "cryptography", _MIN_CRYPTOGRAPHY, "CVE-2026-69247 / CVE-2026-69249"),
    ("packages/vera_core", "aiohttp", _MIN_AIOHTTP, "CVE-2026-69244"),
    ("apps/control_plane", "aiohttp", _MIN_AIOHTTP, "CVE-2026-69244"),
)

#: What must actually be INSTALLED, whatever the declared floors happen to say.
_RESOLVED_FLOORS = (
    ("cryptography", _MIN_CRYPTOGRAPHY),
    ("aiohttp", _MIN_AIOHTTP),
    ("json-repair", _MIN_JSON_REPAIR),
)


def _declared_floor(pyproject: Path, package: str) -> Version:
    """The `>=` bound declared for *package* in *pyproject*'s dependencies."""
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    for dep in deps:
        name, _, bound = dep.partition(">=")
        if name.split("[")[0].strip() == package:
            return Version(bound)
    raise AssertionError(f"{package} is not a declared dependency of {pyproject}")


def test_livekit_agents_floor_excludes_the_vulnerable_json_repair_pin() -> None:
    """The floor must carry the SECURITY bound, not only the correctness one.

    1.6.4 is where the handoff clock fix landed, so it is the correctness floor — but
    the root `override-dependencies = ["json-repair>=0.60.1"]` that used to force the
    patched pin was removed once livekit-agents pinned it itself. With the override
    gone, declaring `>=1.6.4` permits 1.6.4/1.6.5 and therefore json-repair 0.59.10.
    uv.lock resolving 1.6.7 today is not a defence: any re-resolve, downgrade, or added
    upper bound could land on a vulnerable pair with nothing left to catch it.
    """
    pyproject = _VERA_BACKEND / "apps" / "agent_worker" / "pyproject.toml"
    floor = _declared_floor(pyproject, "livekit-agents")
    assert floor >= _MIN_LIVEKIT_AGENTS, (
        f"livekit-agents floor {floor} admits a build pinning json-repair 0.59.10 "
        f"(GHSA-xf7x-x43h-rpqh); needs >= {_MIN_LIVEKIT_AGENTS}"
    )


@pytest.mark.parametrize(("package_dir", "distribution", "minimum", "admits"), _DECLARED_FLOORS)
def test_security_floor_is_declared_where_the_package_is_imported(
    package_dir: str, distribution: str, minimum: Version, admits: str
) -> None:
    """A floor only binds the package that declares it, so every direct importer needs one.

    aiohttp is the case that motivated this: `vera_core.stt` and
    `control_plane.livekit_gateway` imported it outright while it arrived transitively via
    livekit-agents/livekit-api, so nothing in our tree bounded the version we got.
    """
    floor = _declared_floor(_VERA_BACKEND / package_dir / "pyproject.toml", distribution)
    assert floor >= minimum, (
        f"{package_dir} {distribution} floor {floor} re-admits {admits}; needs >= {minimum}"
    )


@pytest.mark.parametrize(("distribution", "minimum"), _RESOLVED_FLOORS)
def test_resolved_pin_is_patched(distribution: str, minimum: Version) -> None:
    """Belt to the floors' braces: a lock regression is caught even when every declared
    floor still looks correct."""
    resolved = Version(version(distribution))
    assert resolved >= minimum, f"{distribution} {resolved} is below the security floor {minimum}"
