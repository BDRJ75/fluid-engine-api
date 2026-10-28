from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np


@dataclass
class SimMetrics:
    kinetic_energy: float
    max_velocity: float
    mean_density: float
    nan_count: int = 0
    blowup: bool = False


@dataclass
class SimState:
    method: str
    positions: np.ndarray  # (N, 2) or (N, 3)
    velocities: np.ndarray
    densities: np.ndarray
    masses: np.ndarray
    dt: float
    hyper: dict[str, Any] = field(default_factory=dict)
    walls: list[dict[str, Any]] = field(default_factory=list)
    step_index: int = 0
    sim_time: float = 0.0
    rest_density: float = 1000.0
    particle_radius: float = 0.02


class Backend(Protocol):
    name: str

    def create(
        self,
        *,
        domain: tuple[float, float, float | None],
        dt: float,
        hyper: dict[str, Any],
        rest_density: float,
        walls: list[dict[str, Any]],
        seed: int | None,
    ) -> SimState: ...

    def step(self, state: SimState) -> SimMetrics: ...


def create_state(method: str, **kwargs: Any) -> SimState:
    from engine.registry import get_backend

    return get_backend(method).create(**kwargs)


def step_once(state: SimState) -> SimMetrics:
    from engine.registry import get_backend

    return get_backend(state.method).step(state)
