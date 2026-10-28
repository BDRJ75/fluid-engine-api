from __future__ import annotations

from typing import Any

import numpy as np

from engine.base import Backend, SimMetrics, SimState


class StubBackend:
    name = "stub"

    def create(
        self,
        *,
        domain: tuple[float, float, float | None],
        dt: float,
        hyper: dict[str, Any],
        rest_density: float,
        walls: list[dict[str, Any]],
        seed: int | None,
    ) -> SimState:
        rng = np.random.default_rng(seed)
        n = int(hyper.get("particle_count", 64))
        w, h, _ = domain
        pos = np.column_stack(
            [rng.uniform(0.1 * w, 0.4 * w, n), rng.uniform(0.1 * h, 0.6 * h, n)]
        )
        vel = np.zeros_like(pos)
        dens = np.full(n, rest_density)
        mass = np.full(n, rest_density * (w * h * 0.15) / max(n, 1))
        return SimState(
            method=self.name,
            positions=pos,
            velocities=vel,
            densities=dens,
            masses=mass,
            dt=dt,
            hyper=dict(hyper),
            walls=walls,
            rest_density=rest_density,
        )

    def step(self, state: SimState) -> SimMetrics:
        cfl = float(state.hyper.get("cfl", 0.4))
        state.velocities += np.array([0.0, -9.81]) * state.dt * 0.01
        state.positions += state.velocities * state.dt * cfl
        # soft walls
        w = float(state.hyper.get("_domain_w", 1.0))
        h = float(state.hyper.get("_domain_h", 1.0))
        for ax, lim in ((0, w), (1, h)):
            lo = state.positions[:, ax] < 0
            hi = state.positions[:, ax] > lim
            state.positions[lo, ax] = 0
            state.positions[hi, ax] = lim
            state.velocities[lo | hi, ax] *= -0.3
        speed = np.linalg.norm(state.velocities, axis=1)
        ke = float(0.5 * np.sum(state.masses * speed**2))
        state.step_index += 1
        state.sim_time += state.dt
        nan = int(np.isnan(state.positions).sum() + np.isnan(state.velocities).sum())
        blow = nan > 0 or float(np.max(speed) if len(speed) else 0) > 50
        return SimMetrics(
            kinetic_energy=ke,
            max_velocity=float(np.max(speed) if len(speed) else 0.0),
            mean_density=float(np.mean(state.densities)),
            nan_count=nan,
            blowup=blow,
        )


BACKEND: Backend = StubBackend()
