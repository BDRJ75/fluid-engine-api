from __future__ import annotations

from engine.base import Backend
from engine import sph, stub

_BACKENDS: dict[str, Backend] = {
    "stub": stub.BACKEND,
    "sph": sph.BACKEND,
    "SPH": sph.BACKEND,
    "wcsph": sph.BACKEND,
}


def get_backend(method: str) -> Backend:
    key = (method or "stub").strip()
    if key not in _BACKENDS:
        # unknown method → stub with clear fallback
        return _BACKENDS["stub"]
    return _BACKENDS[key]


def list_methods() -> list[str]:
    return sorted({b.name for b in _BACKENDS.values()})
