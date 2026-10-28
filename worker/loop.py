"""Poll pending simulation_runs, simulate, write timesteps + learning via HTTP API."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any
from uuid import UUID

import httpx

# allow `python -m worker.loop` from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.simulator import run_simulation  # noqa: E402


def _public_hyper(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


async def claim_pending(client: httpx.AsyncClient) -> dict[str, Any] | None:
    r = await client.post("/runs/claim")
    r.raise_for_status()
    if r.status_code == 204 or r.content in (b"", b"null"):
        return None
    data = r.json()
    return data


async def process_run(client: httpx.AsyncClient, run: dict[str, Any]) -> None:
    run_id = run["id"]
    scene = (await client.get(f"/scenes/{run['scene_id']}")).json()
    hyper = _public_hyper(run.get("hyperparameters"))
    # obstacles: API has POST but no list yet — empty walls until GET exists; domain from scene
    obs = await client.get(f"/scenes/{run['scene_id']}/obstacles")
    obs.raise_for_status()
    walls = [
        {
            "kind": o.get("kind"),
            "pose_json": o.get("pose_json") or {},
            "mesh_uri": o.get("mesh_uri"),
            "material_id": o.get("material_id"),
        }
        for o in obs.json()
    ]
    outcome = run_simulation(
        hyperparameters=hyper,
        seed=run.get("seed"),
        target_density=scene.get("target_density"),
        target_velocity=(
            scene.get("target_velocity_x"),
            scene.get("target_velocity_y"),
            scene.get("target_velocity_z"),
        ),
        dt=float(scene.get("dt") or 0.001),
        max_steps=int(hyper.get("max_steps", 50)),
        method=str(scene.get("method") or "sph"),
        domain=(
            float(scene["domain_width"]),
            float(scene["domain_height"]),
            scene.get("domain_depth"),
        ),
        walls=walls,
    )

    # batch timesteps (chunk if large)
    batch = [
        {
            "step_index": s.step_index,
            "sim_time": s.sim_time,
            "kinetic_energy": s.kinetic_energy,
            "max_velocity": s.max_velocity,
            "mean_density": s.mean_density,
            "dump_uri": s.dump_uri,
        }
        for s in outcome.steps
    ]
    if batch:
        tr = await client.post(f"/runs/{run_id}/timesteps", json={"timesteps": batch})
        tr.raise_for_status()

    parent = None
    raw_hyper = run.get("hyperparameters") or {}
    if isinstance(raw_hyper, dict) and "_parent_run_id" in raw_hyper:
        parent = raw_hyper["_parent_run_id"]

    # Validation gate BEFORE learning so failures do not pollute leaderboard
    gate_fail = None
    if hyper.get("require_stable", False) and not outcome.stable:
        gate_fail = "validation: unstable"
    max_rmse_density = hyper.get("max_rmse_density")
    if max_rmse_density is not None and outcome.rmse_density > float(max_rmse_density):
        gate_fail = (
            f"validation: rmse_density {outcome.rmse_density} > {max_rmse_density}"
        )
    max_rel_density = hyper.get("max_rel_density_err")
    if max_rel_density is not None and outcome.steps:
        rho0 = float(scene.get("target_density") or 1000.0)
        last_rho = outcome.steps[-1].mean_density
        rel = abs(last_rho - rho0) / max(abs(rho0), 1e-12)
        if rel > float(max_rel_density):
            gate_fail = f"validation: |rho_last-rho0|/rho0={rel} > {max_rel_density}"
    max_vel = hyper.get("max_velocity_gate")
    if max_vel is not None and outcome.steps:
        peak_v = max(s.max_velocity for s in outcome.steps)
        if peak_v > float(max_vel):
            gate_fail = f"validation: max_velocity {peak_v} > {max_vel}"
    # max_score needs a formula score — compute soft precheck via rmse only unless learning allowed
    # full score gate runs only when learning is posted

    if gate_fail and not hyper.get("allow_learning_on_fail", False):
        await client.patch(
            f"/runs/{run_id}",
            json={"status": "failed", "error_message": gate_fail},
        )
        return

    learning_body: dict[str, Any] = {
        "stable": outcome.stable,
        "nan_count": outcome.nan_count,
        "blowup_step": outcome.blowup_step,
        "rmse_density": outcome.rmse_density,
        "rmse_velocity": outcome.rmse_velocity,
        "max_err_density": outcome.max_err_density,
        "max_err_velocity": outcome.max_err_velocity,
        "peak_memory_bytes": outcome.peak_memory_bytes,
        "hyperparameters": outcome.hyperparameters,
    }
    if parent:
        learning_body["parent_run_id"] = parent

    lr = await client.post(f"/runs/{run_id}/learning", json=learning_body)
    if lr.status_code == 409:
        await client.patch(f"/runs/{run_id}", json={"status": "completed"})
        return
    lr.raise_for_status()
    learning = lr.json()

    max_score = hyper.get("max_score")
    if max_score is not None and learning.get("score") is not None:
        if float(learning["score"]) > float(max_score):
            # learning already stored — mark failed but keep registry (score gate after score exists)
            await client.patch(
                f"/runs/{run_id}",
                json={
                    "status": "failed",
                    "error_message": (
                        f"validation: score {learning['score']} > max_score {max_score}"
                    ),
                },
            )
            return

    if outcome.stable:
        final_status, err = "completed", None
    else:
        final_status, err = "failed", f"blowup at step {outcome.blowup_step}"
    await client.patch(
        f"/runs/{run_id}",
        json={"status": final_status, "error_message": err},
    )


async def loop(base_url: str, poll_s: float, once: bool) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        while True:
            run = await claim_pending(client)
            if run is None:
                if once:
                    print("idle")
                    return
                await asyncio.sleep(poll_s)
                continue
            print(f"processing {run['id']}")
            try:
                await process_run(client, run)
                print(f"done {run['id']}")
            except Exception as exc:  # noqa: BLE001 — worker must not die on one run
                print(f"fail {run['id']}: {exc}")
                try:
                    await client.patch(
                        f"/runs/{run['id']}",
                        json={"status": "failed", "error_message": str(exc)[:500]},
                    )
                except Exception as patch_exc:  # noqa: BLE001
                    print(f"could not mark failed: {patch_exc}")
            if once:
                return


def main() -> None:
    p = argparse.ArgumentParser(description="Fluid engine simulation worker")
    p.add_argument("--base-url", default=os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--once", action="store_true", help="Process at most one pending run then exit")
    args = p.parse_args()
    asyncio.run(loop(args.base_url, args.poll, args.once))


if __name__ == "__main__":
    main()
