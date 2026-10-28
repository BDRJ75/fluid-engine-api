# Fluid Engine API

FastAPI nad schematem PostgreSQL silnika płynów (`schema.sql` + `schema_v2_campaigns_score.sql`).

## Endpointy

- `GET /campaigns` — lista z `v_campaigns` (`?status=`)
- `GET /campaigns/{id}` — jedna kampania
- `GET /campaigns/{id}/leaderboard?score_formula_id=` — ranking kampanii
- `GET /leaderboard?score_formula_id=` — globalny ranking per formuła (`v_learning_leaderboard_by_formula`)

## Odtwarzanie środowiska

Komputer bywa czyszczony (znika Postgres z danymi i `.venv`, pliki projektu zostają). Całość odtwarza jedno polecenie:

```bash
./scripts/bootstrap_env.sh
```

Skrypt instaluje `postgresql-17` (przez `sudo apt`, w razie potrzeby dodaje repo PGDG), tworzy i uruchamia klaster `17/main`, zakłada rolę i bazę z `DATABASE_URL` w `.env`, wgrywa schematy z `db/` (lub `../fluid-physics-db/`, jeśli `db/` nie ma) (v1 + v2 z formułą `default` v1) tylko gdy ich brak, oraz tworzy `.venv` z `requirements.txt`, `httpx` i `pytest`. Jest idempotentny: ponowne uruchomienie niczego nie zmienia i kończy się `BOOTSTRAP_OK`. Dane runów nie są odtwarzane (baza startuje pusta).

Potem: `./scripts/run_golden.sh`, `./scripts/e2e_worker_smoke.sh`, `./scripts/e2e_validation.sh`, `./scripts/e2e_dambreak.sh`.

## Start

```bash
cp .env.example .env
# ustaw DATABASE_URL
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Migracje SQL leżą w `db/`.

## Worker (pętla uczenia)

Stub SPH/FLIP: bierze `pending` → `running` → batch timesteps → learning → `completed`/`failed`.

```bash
# terminal 1
uvicorn app.main:app --reload
# terminal 2
python -m worker.loop --base-url http://127.0.0.1:8000
# albo jednorazowo
./scripts/e2e_worker_smoke.sh
```

Hiperparametry: `cfl` (>0.85 → blowup), `viscosity`, `max_steps`.

## Engine

- `engine/sph.py` — minimal 2D WCSPH (Poly6/Spiky, XSPH, AABB walls)
- `engine/stub.py` — fallback
- `scenes.method`: `SPH` / `sph` / `stub` (unknown → stub)

## Golden regression tests (SPH engine)

Offline pytest tests (no DB/API) in `tests/test_golden_sph.py` re-run the dam-break
(`scripts/e2e_dambreak.sh`: 300 particles, 200 steps, dt 0.003, stiffness 4000) and
hydrostatic (`scripts/e2e_validation.sh`) cases directly on `engine` and compare metrics
every 10 steps (KE, vmax, mean/max density, front x, center of mass) with
`tests/golden/*.json` at rtol 1e-6. A second test checks that `worker.simulator.run_simulation`
gives the same per-step metrics.

```bash
./scripts/run_golden.sh                  # compare
./scripts/run_golden.sh --update-golden  # regenerate (or UPDATE_GOLDEN=1)
```

Regenerate only after an intentional physics change, and commit the JSON diff.
