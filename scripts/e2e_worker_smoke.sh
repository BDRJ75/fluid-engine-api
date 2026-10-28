#!/usr/bin/env bash
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
for i in $(seq 1 30); do
  if .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ['API_PORT']+'/health')" 2>/dev/null; then
    break
  fi
  sleep 0.2
done

SCENE=$(.venv/bin/python - <<'PY'
import asyncio, os
from httpx import AsyncClient
async def main():
    base = f"http://127.0.0.1:{os.environ['API_PORT']}"
    async with AsyncClient(base_url=base, timeout=30) as c:
        scenes = (await c.get("/scenes")).json()
        if scenes:
            print(scenes[0]["id"]); return
        r = await c.post("/scenes", json={"name":"worker-e2e","domain_width":1,"domain_height":1,"dt":0.001,"target_density":1000})
        r.raise_for_status(); print(r.json()["id"])
asyncio.run(main())
PY
)

RUN=$(API_PORT="$API_PORT" SCENE="$SCENE" .venv/bin/python - <<'PY'
import asyncio, os
from httpx import AsyncClient
async def main():
    base = f"http://127.0.0.1:{os.environ['API_PORT']}"
    async with AsyncClient(base_url=base, timeout=30) as c:
        r = await c.post("/runs", json={
            "scene_id": os.environ["SCENE"],
            "seed": 123,
            "hyperparameters": {"cfl": 0.4, "max_steps": 20},
        })
        r.raise_for_status()
        print(r.json()["id"])
asyncio.run(main())
PY
)

.venv/bin/python -m worker.loop --base-url "http://127.0.0.1:$API_PORT" --once

RUN="$RUN" API_PORT="$API_PORT" .venv/bin/python - <<'PY'
import asyncio, os
from httpx import AsyncClient
async def main():
    base = f"http://127.0.0.1:{os.environ['API_PORT']}"
    run_id = os.environ["RUN"]
    async with AsyncClient(base_url=base, timeout=30) as c:
        runs = (await c.get("/runs", params={"limit": 50})).json()
        mine = next(x for x in runs if x["id"] == run_id)
        assert mine["status"] in ("completed", "failed"), mine
        ts = (await c.get(f"/runs/{run_id}/timesteps")).json()
        assert len(ts) > 0, ts
        print("E2E_OK", mine["status"], "timesteps", len(ts))
asyncio.run(main())
PY
