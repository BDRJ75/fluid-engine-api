"""Create (or reuse) a hydrostatic validation scene via the API."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"))
    args = p.parse_args()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60) as c:
        scenes = (await c.get("/scenes")).json()
        for s in scenes:
            if s.get("name") == "hydrostatic":
                print(s["id"])
                return
        r = await c.post(
            "/scenes",
            json={
                "name": "hydrostatic",
                "description": "Validation: fluid column at rest; target v=0, density=rho0",
                "domain_width": 0.5,
                "domain_height": 1.0,
                "dt": 0.0005,
                "method": "SPH",
                "target_density": 1000.0,
                "target_velocity_x": 0.0,
                "target_velocity_y": 0.0,
                "target_velocity_z": 0.0,
            },
        )
        r.raise_for_status()
        print(r.json()["id"])


if __name__ == "__main__":
    asyncio.run(main())
