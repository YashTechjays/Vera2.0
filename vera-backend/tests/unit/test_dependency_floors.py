"""Dependency floors that exist for security reasons, not just compatibility.

A version specifier cannot record WHY it sits where it does. These tests do, so a
later relaxation fails with the reason attached instead of silently re-admitting a
known-vulnerable transitive pin.
"""

import tomllib
from importlib.metadata import version
from pathlib import Path

_VERA_BACKEND = Path(__file__).resolve().parents[2]

#: livekit-agents pins json-repair EXACTLY, so our floor decides which pin is reachable:
#:
#:     livekit-agents 1.6.4 -> json-repair==0.59.10   GHSA-xf7x-x43h-rpqh
#:     livekit-agents 1.6.5 -> json-repair==0.59.10   vulnerable
#:     livekit-agents 1.6.6 -> json-repair==0.60.1    patched
#:     livekit-agents 1.6.7 -> json-repair==0.60.1    patched
_MIN_LIVEKIT_AGENTS = (1, 6, 6)
_MIN_JSON_REPAIR = (0, 60, 1)


def _parse(spec: str) -> tuple[int, ...]:
    return tuple(int(part) for part in spec.split(".")[:3])


def _declared_floor(pyproject: Path, package: str) -> tuple[int, ...]:
    """The `>=` bound declared for *package* in *pyproject*'s dependencies."""
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    for dep in deps:
        name, _, bound = dep.partition(">=")
        if name.split("[")[0].strip() == package:
            return _parse(bound)
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


def test_resolved_json_repair_is_patched() -> None:
    """Belt to the floor's braces: assert what actually got installed, so a lock
    regression is caught even if the declared floor still looks correct."""
    assert _parse(version("json-repair")) >= _MIN_JSON_REPAIR, (
        f"json-repair {version('json-repair')} is vulnerable to GHSA-xf7x-x43h-rpqh"
    )
