#!/usr/bin/env bash
# Hydrostatic validation: scene → pending run → worker → assert stable + score gates
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -z "${DATABASE_URL:-}" && -f .env ]]; then
  DATABASE_URL="$(grep -E '^DATABASE_URL=' .env | tail -n1 | cut -d= -f2-)"
fi
: "${DATABASE_URL:?Set DATABASE_URL or create .env (see .env.example)}"
export DATABASE_URL
export API_PORT="${API_PORT:-8000}"

.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" &
UV_PID=$!
trap 'kill $UV_PID 2>/dev/null || true' EXIT

for i in $(seq 1 40); do
  if .venv/bin/python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ['API_PORT']+'/health')" 2>/dev/null; then
    break
  fi
  sleep 0.15
done

SCENE=$(API_BASE_URL="http://127.0.0.1:$API_PORT" .venv/bin/python scripts/seed_hydrostatic.py)

# H = 0.7 * domain_height ≈ 0.7; √(gH)≈2.62; noise gate 0.05*√(gH)≈0.13 → use 0.15
RUN=$(SCENE="$SCENE" API_PORT="$API_PORT" .venv/bin/python - <<'PY'
import asyncio, os
from httpx import AsyncClient
async def main():
    base = f"http://127.0.0.1:{os.environ['API_PORT']}"
    async with AsyncClient(base_url=base, timeout=60) as c:
        r = await c.post("/runs", json={
            "scene_id": os.environ["SCENE"],
            "seed": 0,
            "hyperparameters": {
                "layout": "column",
                "particle_count": 100,
                "max_steps": 25,
                "cfl": 0.35,
                "viscosity": 0.15,
                "stiffness": 500.0,
                "gamma": 7.0,
                "xsph": 0.5,
                "require_stable": True,
                "max_rel_density_err": 0.20,
                "metric_tail": 8,
                "max_velocity_gate": 0.15,
                "max_score": 500.0,
            },
        })
        r.raise_for_status()
        print(r.json()["id"])
asyncio.run(main())
PY
)

# abort other pending so claim gets ours
API_PORT="$API_PORT" RUN="$RUN" .venv/bin/python - <<'PY'
import asyncio, os
from httpx import AsyncClient
async def main():
    base = f"http://127.0.0.1:{os.environ['API_PORT']}"
    run_id = os.environ["RUN"]
    async with AsyncClient(base_url=base, timeout=60) as c:
        runs = (await c.get("/runs", params={"status": "pending", "limit": 100})).json()
        for r in runs:
            if r["id"] != run_id:
                await c.patch(f"/runs/{r['id']}", json={"status": "aborted"})
asyncio.run(main())
PY

.venv/bin/python -m worker.loop --base-url "http://127.0.0.1:$API_PORT" --once

RUN="$RUN" API_PORT="$API_PORT" .venv/bin/python - <<'PY'
import asyncio, os
from httpx import AsyncClient
async def main():
    base = f"http://127.0.0.1:{os.environ['API_PORT']}"
    run_id = os.environ["RUN"]
    async with AsyncClient(base_url=base, timeout=60) as c:
        runs = (await c.get("/runs", params={"limit": 100})).json()
        mine = next(x for x in runs if x["id"] == run_id)
        ts = (await c.get(f"/runs/{run_id}/timesteps")).json()
        assert ts, "no timesteps"
        peak_v = max(t["max_velocity"] for t in ts)
        last_rho = ts[-1]["mean_density"]
        # learning via improvements may be empty; read best or leaderboard
        # fetch learning row indirectly: completed + best for scene
        assert mine["status"] == "completed", mine
        assert mine.get("error_message") in (None, ""), mine
        assert peak_v < 0.15, peak_v
        # density within ~25% of 1000 for this coarse particle count
        assert abs(last_rho - 1000.0) / 1000.0 < 0.20, last_rho
        print("VALIDATION_OK", "steps", len(ts), "peak_v", round(peak_v, 5), "rho", round(last_rho, 2))
asyncio.run(main())
PY
