"""Minimal 2D WCSPH (Poly6 + Spiky) with XSPH and simple axis-aligned walls."""

from __future__ import annotations

from typing import Any

import numpy as np

from engine.base import Backend, SimMetrics, SimState

PI = np.pi


def _poly6(r: np.ndarray, h: float) -> np.ndarray:
    c = 4.0 / (PI * h**8)
    q = h * h - r * r
    out = np.zeros_like(r)
    m = (r >= 0) & (r < h)
    out[m] = c * q[m] ** 3
    return out


def _spiky_grad(rij: np.ndarray, r: np.ndarray, h: float) -> np.ndarray:
    """rij: (M,2), r: (M,) distances → gradients (M,2)."""
    c = -30.0 / (PI * h**5)
    out = np.zeros_like(rij)
    m = (r > 1e-12) & (r < h)
    factor = c * (h - r[m]) ** 2 / r[m]
    out[m] = (rij[m].T * factor).T
    return out


def _visc_lap(r: np.ndarray, h: float) -> np.ndarray:
    c = 40.0 / (PI * h**5)
    out = np.zeros_like(r)
    m = (r >= 0) & (r < h)
    out[m] = c * (h - r[m])
    return out


class SPHBackend:
    name = "sph"

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
        w, h, _ = domain
        n = int(hyper.get("particle_count", 400))
        layout = str(hyper.get("layout", "dam_break"))
        if layout == "column":
            # hydrostatic: fill most of the tank quietly
            fill_w, fill_h = 0.9 * w, 0.7 * h
            nx = max(2, int(np.sqrt(n * fill_w / max(fill_h, 1e-6))))
            ny = max(1, n // nx)
            xs = np.linspace(0.05 * w, 0.05 * w + fill_w, nx)
            ys = np.linspace(0.05 * h, 0.05 * h + fill_h, ny)
        else:
            # dam-break block on the left
            nx = int(np.sqrt(n * (0.3 * w) / max(0.6 * h, 1e-6)))
            ny = max(1, n // max(nx, 1))
            xs = np.linspace(0.05 * w, 0.35 * w, nx)
            ys = np.linspace(0.05 * h, 0.65 * h, ny)
        xx, yy = np.meshgrid(xs, ys)
        pos = np.column_stack([xx.ravel(), yy.ravel()])[:n]
        n = len(pos)
        spacing = min(xs[1] - xs[0] if nx > 1 else 0.02 * w, ys[1] - ys[0] if ny > 1 else 0.02 * h)
        h_kern = float(hyper.get("h", 2.2 * spacing))
        mass = rest_density * spacing * spacing
        hyper = {
            **hyper,
            "h": h_kern,
            "_domain_w": w,
            "_domain_h": h,
            "particle_count": n,
        }
        return SimState(
            method=self.name,
            positions=pos.astype(np.float64),
            velocities=np.zeros_like(pos, dtype=np.float64),
            densities=np.full(n, rest_density, dtype=np.float64),
            masses=np.full(n, mass, dtype=np.float64),
            dt=float(hyper.get("dt", dt)),
            hyper=hyper,
            walls=walls,
            rest_density=rest_density,
            particle_radius=spacing * 0.5,
        )

    def step(self, state: SimState) -> SimMetrics:
        h = float(state.hyper.get("h", 0.05))
        gamma = float(state.hyper.get("gamma", 7.0))
        stiffness = float(state.hyper.get("stiffness", 1000.0))
        viscosity = float(state.hyper.get("viscosity", 0.01))
        xsph = float(state.hyper.get("xsph", 0.3))
        gravity = float(state.hyper.get("gravity", -9.81))
        cfl = float(state.hyper.get("cfl", 0.4))
        rho0 = state.rest_density
        w_dom = float(state.hyper.get("_domain_w", 1.0))
        h_dom = float(state.hyper.get("_domain_h", 1.0))

        x = state.positions
        v = state.velocities
        m = state.masses
        n = len(x)

        # neighbor pairs (O(N^2) — intentional for minimal solver)
        diff = x[:, None, :] - x[None, :, :]
        r = np.linalg.norm(diff, axis=2)
        np.fill_diagonal(r, 0.0)

        # density
        w_ij = _poly6(r, h)
        dens = np.sum(m[None, :] * w_ij, axis=1)
        dens = np.maximum(dens, 1e-6)
        state.densities = dens

        # pressure (Tait-like)
        pressure = stiffness * ((dens / rho0) ** gamma - 1.0)
        pressure = np.maximum(pressure, 0.0)

        # forces
        forces = np.zeros_like(x)
        forces[:, 1] += gravity * m

        for i in range(n):
            mask = (r[i] > 0) & (r[i] < h)
            if not np.any(mask):
                continue
            rij = diff[i, mask]
            rij_len = r[i, mask]
            # pressure
            grad = _spiky_grad(rij, rij_len, h)
            pi = pressure[i]
            pj = pressure[mask]
            rhoi = dens[i]
            rhoj = dens[mask]
            f_p = -(m[mask] * (pi / (rhoi * rhoi) + pj / (rhoj * rhoj)))[:, None] * grad
            forces[i] += np.sum(f_p, axis=0) * m[i]
            # viscosity
            lap = _visc_lap(rij_len, h)
            vij = v[mask] - v[i]
            f_v = viscosity * m[mask][:, None] * vij * lap[:, None] / rhoj[:, None]
            forces[i] += np.sum(f_v, axis=0) * m[i]

        # integrate with CFL-limited dt (particle speed + acoustic speed from Tait EOS)
        acc = forces / m[:, None]
        speed = np.linalg.norm(v, axis=1)
        vmax = float(np.max(speed)) if n else 0.0
        # c_s ≈ sqrt(dp/drho) at rho0 for p = k*((rho/rho0)^gamma - 1) → k*gamma/rho0
        c_s = float(np.sqrt(max(stiffness * gamma / max(rho0, 1e-12), 0.0)))
        signal = max(vmax, c_s)
        dt = state.dt
        if signal > 1e-8:
            dt = min(dt, cfl * h / signal)
        dt = max(dt, 1e-6)

        v_new = v + acc * dt
        # XSPH
        for i in range(n):
            mask = (r[i] > 0) & (r[i] < h)
            if not np.any(mask):
                continue
            w_i = _poly6(r[i, mask], h)
            rhoj = dens[mask]
            corr = np.sum(
                (m[mask] / rhoj)[:, None] * (v[mask] - v[i]) * w_i[:, None],
                axis=0,
            )
            v_new[i] += xsph * corr

        x_new = x + v_new * dt

        # AABB domain walls + optional obstacle AABBs from pose_json
        restitution = float(state.hyper.get("wall_restitution", 0.0))
        friction = float(state.hyper.get("wall_friction", 0.2))
        for ax, lim in ((0, w_dom), (1, h_dom)):
            lo = x_new[:, ax] < 0
            hi = x_new[:, ax] > lim
            x_new[lo, ax] = 0
            x_new[hi, ax] = lim
            v_new[lo, ax] *= -restitution
            v_new[hi, ax] *= -restitution
            other = 1 - ax
            v_new[lo | hi, other] *= 1.0 - friction

        for wall in state.walls:
            pose = wall.get("pose_json") or {}
            if wall.get("kind") not in ("wall", "box", None):
                continue
            x0 = float(pose.get("x", 0))
            y0 = float(pose.get("y", 0))
            ww = float(pose.get("w", pose.get("width", 0)))
            hh = float(pose.get("h", pose.get("height", 0)))
            if ww <= 0 or hh <= 0:
                continue
            inside = (
                (x_new[:, 0] >= x0)
                & (x_new[:, 0] <= x0 + ww)
                & (x_new[:, 1] >= y0)
                & (x_new[:, 1] <= y0 + hh)
            )
            # push out to nearest face (simple)
            if np.any(inside):
                cx = x0 + ww / 2
                cy = y0 + hh / 2
                px = x_new[inside, 0]
                py = x_new[inside, 1]
                dx = np.minimum(np.abs(px - x0), np.abs(px - (x0 + ww)))
                dy = np.minimum(np.abs(py - y0), np.abs(py - (y0 + hh)))
                prefer_x = dx < dy
                left = px < cx
                below = py < cy
                # x faces
                sel = inside.copy()
                idx = np.where(inside)[0]
                for k, i in enumerate(idx):
                    if prefer_x[k]:
                        x_new[i, 0] = x0 if left[k] else x0 + ww
                        v_new[i, 0] *= -restitution
                        v_new[i, 1] *= 1.0 - friction
                    else:
                        x_new[i, 1] = y0 if below[k] else y0 + hh
                        v_new[i, 1] *= -restitution
                        v_new[i, 0] *= 1.0 - friction

        state.positions = x_new
        state.velocities = v_new
        state.step_index += 1
        state.sim_time += dt

        speed = np.linalg.norm(state.velocities, axis=1)
        ke = float(0.5 * np.sum(state.masses * speed**2))
        nan = int(
            np.isnan(state.positions).sum()
            + np.isnan(state.velocities).sum()
            + np.isnan(state.densities).sum()
            + np.isinf(state.densities).sum()
        )
        max_v = float(np.nanmax(speed)) if n else 0.0
        mean_d = float(np.nanmean(state.densities)) if n else 0.0
        blow = nan > 0 or max_v > 80.0 or mean_d > 5 * rho0 or mean_d < 0.05 * rho0
        return SimMetrics(
            kinetic_energy=ke if not np.isnan(ke) else 0.0,
            max_velocity=max_v if not np.isnan(max_v) else 0.0,
            mean_density=mean_d if not np.isnan(mean_d) else 0.0,
            nan_count=nan,
            blowup=blow,
        )


BACKEND: Backend = SPHBackend()
