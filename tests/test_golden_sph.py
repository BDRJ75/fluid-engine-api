"""Golden regression tests for the 2D WCSPH engine (offline, no DB/API).

Cases mirror scripts/e2e_dambreak.sh and scripts/e2e_validation.sh exactly
(same scene fields, hyperparameters, seed=0, no obstacles -> walls=[]).
Init is a deterministic grid (no RNG), so results are bit-reproducible on the
same machine/numpy; rtol=1e-6 leaves room only for BLAS/numpy summation-order
differences across machines.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from engine.base import create_state, step_once
from worker.simulator import run_simulation

GOLDEN_DIR = Path(__file__).parent / "golden"
RTOL = 1e-6
ATOL = 1e-9
EVERY = 10

CASES = {
    "dambreak": dict(
        scene=dict(domain=(1.0, 1.0, None), dt=0.0008, method="SPH", target_density=1000.0,
                   target_velocity=None),
        hyper={"layout": "dam_break", "particle_count": 300, "max_steps": 200, "dt": 0.003,
               "cfl": 0.35, "viscosity": 0.08, "stiffness": 4000.0, "gamma": 7.0,
               "xsph": 0.4, "require_stable": True, "max_velocity_gate": 6.0},
    ),
    "hydrostatic": dict(
        scene=dict(domain=(0.5, 1.0, None), dt=0.0005, method="SPH", target_density=1000.0,
                   target_velocity=(0.0, 0.0, 0.0)),
        hyper={"layout": "column", "particle_count": 100, "max_steps": 25, "cfl": 0.35,
               "viscosity": 0.15, "stiffness": 500.0, "gamma": 7.0, "xsph": 0.5,
               "require_stable": True, "max_rel_density_err": 0.20, "metric_tail": 8,
               "max_velocity_gate": 0.15, "max_score": 500.0},
    ),
}


def _snapshot(i, state, m):
    p, v, rho, mass = state.positions, state.velocities, state.densities, state.masses
    com = (p * mass[:, None]).sum(0) / mass.sum()
    return {
        "step": i, "sim_time": float(state.sim_time),
        "kinetic_energy": float(m.kinetic_energy),
        "vmax": float(m.max_velocity),
        "ke_recomputed": float(0.5 * (mass * (v ** 2).sum(1)).sum()),
        "vmax_recomputed": float(np.sqrt((v ** 2).sum(1)).max()),
        "mean_density": float(rho.mean()), "max_density": float(rho.max()),
        "front_x": float(p[:, 0].max()),
        "com_x": float(com[0]), "com_y": float(com[1]),
    }


def simulate(case: dict) -> dict:
    sc, hyper = case["scene"], dict(case["hyper"])
    state = create_state(sc["method"], domain=sc["domain"], dt=sc["dt"], hyper=hyper,
                         rest_density=sc["target_density"], walls=[], seed=0)
    n = int(hyper["max_steps"])
    snaps = [_snapshot(-1, state, type("M", (), {"kinetic_energy": 0.0, "max_velocity": 0.0})())]
    for i in range(n):
        m = step_once(state)
        assert not m.blowup and m.nan_count == 0, f"blowup at step {i}"
        if i % EVERY == 0 or i == n - 1:
            snaps.append(_snapshot(i, state, m))
    return {"particle_count": int(len(state.positions)), "steps": n, "every": EVERY,
            "hyperparameters": case["hyper"], "snapshots": snaps}


def _compare(actual, expected, path=""):
    if isinstance(expected, dict):
        assert set(actual) == set(expected), f"{path}: keys differ"
        for k in expected:
            _compare(actual[k], expected[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), f"{path}: length {len(actual)} != {len(expected)}"
        for i, (a, e) in enumerate(zip(actual, expected)):
            _compare(a, e, f"{path}[{i}]")
    elif isinstance(expected, float):
        assert math.isclose(actual, expected, rel_tol=RTOL, abs_tol=ATOL), \
            f"{path}: {actual!r} != golden {expected!r} (rtol={RTOL})"
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden(name, update_golden):
    data = json.loads(json.dumps(simulate(CASES[name])))
    path = GOLDEN_DIR / f"{name}.json"
    if update_golden or not path.exists():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1) + "\n")
        if not update_golden:
            pytest.fail(f"golden {path.name} was missing; generated it, rerun to compare")
        return
    _compare(data, json.loads(path.read_text()))


@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_run_simulation(name):
    """The worker path (run_simulation) must yield the same per-step metrics."""
    sc, hyper = CASES[name]["scene"], CASES[name]["hyper"]
    out = run_simulation(hyperparameters=hyper, seed=0, target_density=sc["target_density"],
                         target_velocity=sc["target_velocity"], dt=sc["dt"],
                         max_steps=hyper["max_steps"], method=sc["method"], domain=sc["domain"],
                         walls=[])
    assert out.stable and len(out.steps) == hyper["max_steps"]
    golden = json.loads((GOLDEN_DIR / f"{name}.json").read_text())
    for s in golden["snapshots"][1:]:
        st = out.steps[s["step"]]
        assert math.isclose(st.kinetic_energy, s["kinetic_energy"], rel_tol=RTOL, abs_tol=ATOL)
        assert math.isclose(st.max_velocity, s["vmax"], rel_tol=RTOL, abs_tol=ATOL)
        assert math.isclose(st.mean_density, s["mean_density"], rel_tol=RTOL, abs_tol=ATOL)
