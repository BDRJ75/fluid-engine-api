#!/usr/bin/env bash
# Dam-break validation: stable, bounded KE/velocity, wave front moves right
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
  .venv/bin/python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ['API_PORT']+'/health')" 2>/dev/null && break
  sleep 0.15
done

API_PORT="$API_PORT" .venv/bin/python - <<'PY'
import asyncio, os, math, subprocess, sys
from urllib.parse import urlparse
import numpy as np
from httpx import AsyncClient

async def main():
    base = f"http://127.0.0.1:{os.environ['API_PORT']}"
    async with AsyncClient(base_url=base, timeout=300) as c:
        scenes = (await c.get("/scenes")).json()
        scene = next((s for s in scenes if s["name"] == "dam-break-validation"), None)
        if scene is None:
            r = await c.post("/scenes", json={
                "name": "dam-break-validation",
                "description": "Validation: water column collapses, front must advance",
                "domain_width": 1.0, "domain_height": 1.0, "dt": 0.0008,
                "method": "SPH", "target_density": 1000.0,
            })
            r.raise_for_status(); scene = r.json()
        for p in (await c.get("/runs", params={"status": "pending", "limit": 100})).json():
            await c.patch(f"/runs/{p['id']}", json={"status": "aborted"})
        run = (await c.post("/runs", json={
            "scene_id": scene["id"], "seed": 0,
            "hyperparameters": {
                "layout": "dam_break", "particle_count": 300, "max_steps": 200, "dt": 0.003,
                "cfl": 0.35, "viscosity": 0.08, "stiffness": 4000.0, "gamma": 7.0,
                "xsph": 0.4, "require_stable": True, "max_velocity_gate": 6.0,
            },
        })).json()
        os.environ["RUN"] = run["id"]

    subprocess.run([sys.executable, "-m", "worker.loop", "--base-url", base, "--once"], check=True)

    async with AsyncClient(base_url=base, timeout=60) as c:
        runs = (await c.get("/runs", params={"limit": 100})).json()
        mine = next(x for x in runs if x["id"] == run["id"])
        ts = (await c.get(f"/runs/{run['id']}/timesteps")).json()
    assert mine["status"] == "completed", mine
    assert len(ts) == 200, len(ts)
    ke = [t["kinetic_energy"] for t in ts]
    vmax = max(t["max_velocity"] for t in ts)
    H = 0.6  # initial column height
    assert all(math.isfinite(k) for k in ke)
    assert vmax < 2 * math.sqrt(2 * 9.81 * H), vmax
    assert ke[-1] > ke[0], (ke[0], ke[-1])  # column actually collapses
    dumps = [t for t in ts if t.get("dump_uri")]
    x0 = np.load(urlparse(dumps[0]["dump_uri"]).path)[:, 0].max()
    x1 = np.load(urlparse(dumps[-1]["dump_uri"]).path)[:, 0].max()
    assert x1 > x0 + 0.15, (x0, x1)  # front advanced by >15% of tank
    print("DAMBREAK_OK", "vmax", round(vmax, 3), "front", round(float(x0), 3), "->", round(float(x1), 3), "ke_last", round(ke[-1], 3))

asyncio.run(main())
PY
