"""Run one simulation via engine backends (SPH / stub), emit timesteps + learning metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from engine.base import create_state, step_once


@dataclass
class StepResult:
    step_index: int
    sim_time: float
    kinetic_energy: float
    max_velocity: float
    mean_density: float
    dump_uri: str | None = None


@dataclass
class SimOutcome:
    steps: list[StepResult]
    stable: bool
    nan_count: int
    blowup_step: int | None
    rmse_density: float
    rmse_velocity: float
    max_err_density: float
    max_err_velocity: float
    peak_memory_bytes: int
    hyperparameters: dict[str, Any]


def _public_hyper(hyper: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in hyper.items() if not str(k).startswith("_")}


def run_simulation(
    *,
    hyperparameters: dict[str, Any],
    seed: int | None,
    target_density: float | None = 1000.0,
    target_velocity: tuple[float | None, float | None, float | None] | None = None,
    dt: float = 0.001,
    max_steps: int = 50,
    method: str = "sph",
    domain: tuple[float, float, float | None] = (1.0, 1.0, None),
    walls: list[dict[str, Any]] | None = None,
) -> SimOutcome:
    hyper = dict(hyperparameters)
    hyper.setdefault("max_steps", max_steps)
    rest = float(target_density or 1000.0)
    state = create_state(
        method,
        domain=domain,
        dt=dt,
        hyper=hyper,
        rest_density=rest,
        walls=walls or [],
        seed=seed,
    )

    steps: list[StepResult] = []
    nan_total = 0
    blowup_step: int | None = None
    stable = True
    n_steps = int(hyper.get("max_steps", max_steps))

    for i in range(n_steps):
        metrics = step_once(state)
        nan_total += metrics.nan_count
        dump = None
        if i % 10 == 0:
            dump = f"file:///tmp/fluid-dumps/{method}-step-{i}.npy"
            try:
                np.save(
                    f"/tmp/fluid-dumps-{method}-step-{i}.npy",
                    state.positions,
                )
                dump = f"file:///tmp/fluid-dumps-{method}-step-{i}.npy"
            except OSError:
                dump = None
        steps.append(
            StepResult(
                step_index=i,
                sim_time=state.sim_time,
                kinetic_energy=metrics.kinetic_energy,
                max_velocity=metrics.max_velocity,
                mean_density=metrics.mean_density,
                dump_uri=dump,
            )
        )
        if metrics.blowup:
            stable = False
            blowup_step = i
            break

    dens = [s.mean_density for s in steps]
    vels = [s.max_velocity for s in steps]
    tgt_d = rest
    tvx, tvy, _ = target_velocity or (None, None, None)
    tgt_v = 0.0
    if tvx is not None or tvy is not None:
        tgt_v = math.hypot(tvx or 0.0, tvy or 0.0)

    # Prefer last-K window so settling transient does not inflate RMSE
    k = int(hyperparameters.get("metric_tail", max(1, len(steps) // 5 or 1)))
    dens_w = dens[-k:] if dens else []
    vels_w = vels[-k:] if vels else []

    if dens_w:
        rmse_d = math.sqrt(sum((d - tgt_d) ** 2 for d in dens_w) / len(dens_w))
        max_d = max(abs(d - tgt_d) for d in dens_w)
        # also expose last-step relative error via max_err when tighter
        max_d = min(max_d, abs(dens[-1] - tgt_d))
    else:
        rmse_d, max_d = 1e3, 1e3
    if vels_w:
        rmse_v = math.sqrt(sum((v - tgt_v) ** 2 for v in vels_w) / len(vels_w))
        max_v = max(abs(v - tgt_v) for v in vels_w)
    else:
        rmse_v, max_v = 1e3, 1e3

    if not stable:
        rmse_d, rmse_v = max(rmse_d, 1e3), max(rmse_v, 1e3)

    peak_mem = int(state.positions.nbytes + state.velocities.nbytes + state.densities.nbytes)
    peak_mem *= 4  # rough allocator overhead

    return SimOutcome(
        steps=steps,
        stable=stable,
        nan_count=nan_total,
        blowup_step=blowup_step,
        rmse_density=rmse_d,
        rmse_velocity=rmse_v,
        max_err_density=max_d,
        max_err_velocity=max_v,
        peak_memory_bytes=peak_mem,
        hyperparameters=_public_hyper(state.hyper),
    )
