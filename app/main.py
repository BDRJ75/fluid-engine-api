import json
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import (
    BestRunOut,
    CampaignLeaderboardRow,
    CampaignOut,
    FluidOut,
    FormulaLeaderboardRow,
    ImprovementOut,
    LearningCreate,
    LearningCreated,
    MaterialOut,
    RunCreate,
    RunCreated,
    RunForkCreate,
    RunClaimOut,
    RunOut,
    RunStatusPatch,
    SceneCreate,
    SceneFluidCreate,
    SceneFluidOut,
    SceneObstacleCreate,
    SceneObstacleOut,
    SceneOut,
    TimestepOut,
    TimestepsBatchCreate,
)


_INTERNAL_HYPER_KEYS = frozenset({"_score_formula_id", "_parent_run_id"})


def _public_hyperparameters(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k not in _INTERNAL_HYPER_KEYS}


def _uuid_from_hyper(raw: dict[str, Any], key: str) -> UUID | None:
    if key not in raw:
        return None
    try:
        return UUID(str(raw[key]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid UUID in run hyperparameters.{key}",
        ) from exc


app = FastAPI(
    title="Fluid Engine API",
    description="Campaigns and learning leaderboards for the fluid physics engine",
    version="0.4.0",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    status: str | None = Query(None, description="Filter by campaign status"),
    session: AsyncSession = Depends(get_session),
) -> list[CampaignOut]:
    sql = "SELECT * FROM v_campaigns"
    params: dict[str, object] = {}
    if status is not None:
        sql += " WHERE status = :status"
        params["status"] = status
    sql += " ORDER BY updated_at DESC"
    result = await session.execute(text(sql), params)
    return [CampaignOut.model_validate(row) for row in result.mappings().all()]


@app.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> CampaignOut:
    result = await session.execute(
        text("SELECT * FROM v_campaigns WHERE id = :id"),
        {"id": campaign_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignOut.model_validate(row)


@app.get(
    "/campaigns/{campaign_id}/leaderboard",
    response_model=list[CampaignLeaderboardRow],
)
async def campaign_leaderboard(
    campaign_id: UUID,
    score_formula_id: UUID | None = Query(
        None,
        description="Filter ranks to one score formula (recommended)",
    ),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[CampaignLeaderboardRow]:
    sql = """
        SELECT * FROM v_campaign_leaderboard
        WHERE campaign_id = :campaign_id
    """
    params: dict[str, object] = {"campaign_id": campaign_id, "limit": limit}
    if score_formula_id is not None:
        sql += " AND score_formula_id = :score_formula_id"
        params["score_formula_id"] = score_formula_id
    sql += " ORDER BY rank_position ASC LIMIT :limit"
    result = await session.execute(text(sql), params)
    return [CampaignLeaderboardRow.model_validate(row) for row in result.mappings().all()]


@app.get("/leaderboard", response_model=list[FormulaLeaderboardRow])
async def learning_leaderboard(
    score_formula_id: UUID | None = Query(
        None,
        description="Partition filter — do not mix formulas",
    ),
    scene_id: UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[FormulaLeaderboardRow]:
    sql = "SELECT * FROM v_learning_leaderboard_by_formula WHERE 1=1"
    params: dict[str, object] = {"limit": limit}
    if score_formula_id is not None:
        sql += " AND score_formula_id = :score_formula_id"
        params["score_formula_id"] = score_formula_id
    if scene_id is not None:
        sql += " AND scene_id = :scene_id"
        params["scene_id"] = scene_id
    sql += " ORDER BY rank_position ASC LIMIT :limit"
    result = await session.execute(text(sql), params)
    return [FormulaLeaderboardRow.model_validate(row) for row in result.mappings().all()]


async def _default_formula_id(session: AsyncSession) -> UUID:
    result = await session.execute(
        text(
            """
            SELECT id FROM score_formulas
            WHERE name = 'default' AND version = 1
            LIMIT 1
            """
        )
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=500,
            detail="Default score formula (default v1) is missing — apply schema_v2 seed",
        )
    return row[0]


async def _resolve_score_formula_id(
    session: AsyncSession,
    *,
    explicit: UUID | None,
    campaign_id: UUID | None,
    run_id: UUID | None = None,
) -> UUID:
    if explicit is not None:
        return explicit
    if campaign_id is not None:
        result = await session.execute(
            text("SELECT score_formula_id FROM experiment_campaigns WHERE id = :id"),
            {"id": campaign_id},
        )
        row = result.first()
        if row is not None:
            return row[0]
    if run_id is not None:
        result = await session.execute(
            text(
                """
                SELECT c.score_formula_id
                FROM campaign_runs cr
                JOIN experiment_campaigns c ON c.id = cr.campaign_id
                WHERE cr.run_id = :run_id
                ORDER BY cr.joined_at DESC
                LIMIT 1
                """
            ),
            {"run_id": run_id},
        )
        row = result.first()
        if row is not None:
            return row[0]
    return await _default_formula_id(session)


def _compute_score(
    *,
    stable: bool,
    rmse_density: float | None,
    rmse_velocity: float | None,
    peak_memory_bytes: int | None,
    weight_stability: float,
    weight_rmse_density: float,
    weight_rmse_velocity: float,
    weight_memory: float,
    unstable_penalty: float,
) -> float:
    # Lower is better. Stability is primarily ranked in views; unstable runs
    # still get unstable_penalty scaled by weight_stability.
    score = 0.0
    if not stable:
        score += unstable_penalty * weight_stability
    score += weight_rmse_density * float(rmse_density or 0.0)
    score += weight_rmse_velocity * float(rmse_velocity or 0.0)
    score += weight_memory * (float(peak_memory_bytes or 0) / 1_000_000.0)
    return score


@app.post("/runs", response_model=RunCreated, status_code=201)
async def create_run(
    body: RunCreate,
    session: AsyncSession = Depends(get_session),
) -> RunCreated:
    scene = await session.execute(
        text("SELECT id FROM scenes WHERE id = :id"),
        {"id": body.scene_id},
    )
    if scene.first() is None:
        raise HTTPException(status_code=404, detail="Scene not found")

    campaign_id = body.campaign_id
    if campaign_id is not None:
        camp = await session.execute(
            text("SELECT id, scene_id FROM experiment_campaigns WHERE id = :id"),
            {"id": campaign_id},
        )
        crow = camp.mappings().first()
        if crow is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if crow["scene_id"] != body.scene_id:
            raise HTTPException(
                status_code=400,
                detail="campaign_id scene does not match scene_id",
            )

    # Stash preferred formula + parent for learning/fork without schema change on runs
    hyper: dict[str, Any] = dict(body.hyperparameters)
    if body.score_formula_id is not None:
        hyper["_score_formula_id"] = str(body.score_formula_id)
    if body.parent_run_id is not None:
        hyper["_parent_run_id"] = str(body.parent_run_id)

    try:
        result = await session.execute(
            text(
                """
                INSERT INTO simulation_runs (scene_id, seed, status, hyperparameters)
                VALUES (
                    :scene_id,
                    :seed,
                    'pending',
                    CAST(:hyperparameters AS jsonb)
                )
                RETURNING id, status::text AS status
                """
            ),
            {
                "scene_id": body.scene_id,
                "seed": body.seed,
                "hyperparameters": json.dumps(hyper),
            },
        )
        row = result.mappings().one()
        run_id = row["id"]

        if campaign_id is not None:
            await session.execute(
                text(
                    """
                    INSERT INTO campaign_runs (campaign_id, run_id, variant_label)
                    VALUES (:campaign_id, :run_id, :variant_label)
                    """
                ),
                {
                    "campaign_id": campaign_id,
                    "run_id": run_id,
                    "variant_label": body.variant_label,
                },
            )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Could not create run") from exc

    return RunCreated(id=run_id, status=row["status"])


@app.post("/runs/{run_id}/learning", response_model=LearningCreated, status_code=201)
async def create_learning(
    run_id: UUID,
    body: LearningCreate,
    session: AsyncSession = Depends(get_session),
) -> LearningCreated:
    run_result = await session.execute(
        text(
            """
            SELECT id, status::text AS status, hyperparameters
            FROM simulation_runs WHERE id = :id
            """
        ),
        {"id": run_id},
    )
    run = run_result.mappings().first()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    existing = await session.execute(
        text("SELECT id FROM learning_registry WHERE run_id = :run_id"),
        {"run_id": run_id},
    )
    if existing.first() is not None:
        raise HTTPException(
            status_code=409,
            detail="Learning entry already exists for this run",
        )

    hyper_run_raw = run["hyperparameters"] or {}
    if isinstance(hyper_run_raw, str):
        try:
            hyper_run_raw = json.loads(hyper_run_raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="Run hyperparameters are not valid JSON",
            ) from exc
    if not isinstance(hyper_run_raw, dict):
        hyper_run_raw = {}

    formula_hint = body.score_formula_id
    if formula_hint is None:
        formula_hint = _uuid_from_hyper(hyper_run_raw, "_score_formula_id")

    score_formula_id = await _resolve_score_formula_id(
        session,
        explicit=formula_hint,
        campaign_id=None,
        run_id=run_id,
    )

    formula_result = await session.execute(
        text(
            """
            SELECT weight_stability, weight_rmse_density, weight_rmse_velocity,
                   weight_memory, unstable_penalty
            FROM score_formulas WHERE id = :id
            """
        ),
        {"id": score_formula_id},
    )
    formula = formula_result.mappings().first()
    if formula is None:
        raise HTTPException(status_code=404, detail="Score formula not found")

    parent_run_id = body.parent_run_id
    if parent_run_id is None:
        parent_run_id = _uuid_from_hyper(hyper_run_raw, "_parent_run_id")

    if parent_run_id is not None:
        parent_check = await session.execute(
            text("SELECT id FROM simulation_runs WHERE id = :id"),
            {"id": parent_run_id},
        )
        if parent_check.first() is None:
            raise HTTPException(status_code=400, detail="parent_run_id not found")

    score = _compute_score(
        stable=body.stable,
        rmse_density=body.rmse_density,
        rmse_velocity=body.rmse_velocity,
        peak_memory_bytes=body.peak_memory_bytes,
        weight_stability=float(formula["weight_stability"]),
        weight_rmse_density=float(formula["weight_rmse_density"]),
        weight_rmse_velocity=float(formula["weight_rmse_velocity"]),
        weight_memory=float(formula["weight_memory"]),
        unstable_penalty=float(formula["unstable_penalty"]),
    )

    if body.hyperparameters:
        learning_hyper = _public_hyperparameters(body.hyperparameters)
    else:
        learning_hyper = _public_hyperparameters(hyper_run_raw)

    try:
        insert = await session.execute(
            text(
                """
                INSERT INTO learning_registry (
                    run_id, parent_run_id, stable, nan_count, blowup_step,
                    rmse_density, rmse_velocity, max_err_density, max_err_velocity,
                    peak_memory_bytes, hyperparameters, score, notes, score_formula_id
                ) VALUES (
                    :run_id, :parent_run_id, :stable, :nan_count, :blowup_step,
                    :rmse_density, :rmse_velocity, :max_err_density, :max_err_velocity,
                    :peak_memory_bytes, CAST(:hyperparameters AS jsonb), :score, :notes,
                    :score_formula_id
                )
                RETURNING id
                """
            ),
            {
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "stable": body.stable,
                "nan_count": body.nan_count,
                "blowup_step": body.blowup_step,
                "rmse_density": body.rmse_density,
                "rmse_velocity": body.rmse_velocity,
                "max_err_density": body.max_err_density,
                "max_err_velocity": body.max_err_velocity,
                "peak_memory_bytes": body.peak_memory_bytes,
                "hyperparameters": json.dumps(learning_hyper),
                "score": score,
                "notes": body.notes,
                "score_formula_id": score_formula_id,
            },
        )
        registry_id = insert.scalar_one()

        if run["status"] in ("pending", "running"):
            await session.execute(
                text(
                    """
                    UPDATE simulation_runs
                    SET status = 'completed', finished_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": run_id},
            )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Learning entry already exists for this run",
        ) from exc

    return LearningCreated(
        registry_id=registry_id,
        run_id=run_id,
        score=score,
        score_formula_id=score_formula_id,
        parent_run_id=parent_run_id,
    )


@app.get("/runs/{run_id}/improvements", response_model=ImprovementOut)
async def get_improvements(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> ImprovementOut:
    result = await session.execute(
        text(
            """
            SELECT * FROM v_learning_improvements
            WHERE child_run_id = :run_id
            """
        ),
        {"run_id": run_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No improvement row — learning entry missing or parent_run_id is null",
        )
    return ImprovementOut.model_validate(row)


@app.get("/fluids", response_model=list[FluidOut])
async def list_fluids(session: AsyncSession = Depends(get_session)) -> list[FluidOut]:
    result = await session.execute(text("SELECT * FROM fluids ORDER BY name"))
    return [FluidOut.model_validate(row) for row in result.mappings().all()]


@app.get("/materials", response_model=list[MaterialOut])
async def list_materials(
    session: AsyncSession = Depends(get_session),
) -> list[MaterialOut]:
    result = await session.execute(text("SELECT * FROM materials ORDER BY name"))
    return [MaterialOut.model_validate(row) for row in result.mappings().all()]


@app.get("/scenes", response_model=list[SceneOut])
async def list_scenes(session: AsyncSession = Depends(get_session)) -> list[SceneOut]:
    result = await session.execute(text("SELECT * FROM scenes ORDER BY updated_at DESC"))
    return [SceneOut.model_validate(row) for row in result.mappings().all()]


@app.post("/scenes", response_model=SceneOut, status_code=201)
async def create_scene(
    body: SceneCreate,
    session: AsyncSession = Depends(get_session),
) -> SceneOut:
    result = await session.execute(
        text(
            """
            INSERT INTO scenes (
                name, description, domain_width, domain_height, domain_depth,
                dt, method, target_density, target_velocity_x, target_velocity_y,
                target_velocity_z
            ) VALUES (
                :name, :description, :domain_width, :domain_height, :domain_depth,
                :dt, :method, :target_density, :target_velocity_x, :target_velocity_y,
                :target_velocity_z
            )
            RETURNING *
            """
        ),
        body.model_dump(),
    )
    row = result.mappings().one()
    await session.commit()
    return SceneOut.model_validate(row)


@app.get("/scenes/{scene_id}", response_model=SceneOut)
async def get_scene(
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> SceneOut:
    result = await session.execute(
        text("SELECT * FROM scenes WHERE id = :id"),
        {"id": scene_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    return SceneOut.model_validate(row)


@app.post("/scenes/{scene_id}/fluids", response_model=SceneFluidOut, status_code=201)
async def add_scene_fluid(
    scene_id: UUID,
    body: SceneFluidCreate,
    session: AsyncSession = Depends(get_session),
) -> SceneFluidOut:
    scene = await session.execute(
        text("SELECT id FROM scenes WHERE id = :id"), {"id": scene_id}
    )
    if scene.first() is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    fluid = await session.execute(
        text("SELECT id FROM fluids WHERE id = :id"), {"id": body.fluid_id}
    )
    if fluid.first() is None:
        raise HTTPException(status_code=404, detail="Fluid not found")
    try:
        result = await session.execute(
            text(
                """
                INSERT INTO scene_fluids (
                    scene_id, fluid_id, initial_volume, spawn_x, spawn_y, spawn_z
                ) VALUES (
                    :scene_id, :fluid_id, :initial_volume, :spawn_x, :spawn_y, :spawn_z
                )
                RETURNING *
                """
            ),
            {"scene_id": scene_id, **body.model_dump()},
        )
        row = result.mappings().one()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Fluid already attached to this scene",
        ) from exc
    return SceneFluidOut.model_validate(row)


@app.post(
    "/scenes/{scene_id}/obstacles",
    response_model=SceneObstacleOut,
    status_code=201,
)
async def add_scene_obstacle(
    scene_id: UUID,
    body: SceneObstacleCreate,
    session: AsyncSession = Depends(get_session),
) -> SceneObstacleOut:
    scene = await session.execute(
        text("SELECT id FROM scenes WHERE id = :id"), {"id": scene_id}
    )
    if scene.first() is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    material = await session.execute(
        text("SELECT id FROM materials WHERE id = :id"), {"id": body.material_id}
    )
    if material.first() is None:
        raise HTTPException(status_code=404, detail="Material not found")
    try:
        result = await session.execute(
            text(
                """
                INSERT INTO scene_obstacles (
                    scene_id, material_id, kind, pose_json, mesh_uri
                ) VALUES (
                    :scene_id, :material_id, :kind, CAST(:pose_json AS jsonb), :mesh_uri
                )
                RETURNING *
                """
            ),
            {
                "scene_id": scene_id,
                "material_id": body.material_id,
                "kind": body.kind,
                "pose_json": json.dumps(body.pose_json),
                "mesh_uri": body.mesh_uri,
            },
        )
        row = result.mappings().one()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Obstacle already exists for this scene/material/kind/mesh",
        ) from exc
    return SceneObstacleOut.model_validate(row)




@app.get("/scenes/{scene_id}/fluids", response_model=list[SceneFluidOut])
async def list_scene_fluids(
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> list[SceneFluidOut]:
    scene = await session.execute(
        text("SELECT id FROM scenes WHERE id = :id"), {"id": scene_id}
    )
    if scene.first() is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    result = await session.execute(
        text("SELECT * FROM scene_fluids WHERE scene_id = :id ORDER BY id"),
        {"id": scene_id},
    )
    return [SceneFluidOut.model_validate(row) for row in result.mappings().all()]


@app.get("/scenes/{scene_id}/obstacles", response_model=list[SceneObstacleOut])
async def list_scene_obstacles(
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> list[SceneObstacleOut]:
    scene = await session.execute(
        text("SELECT id FROM scenes WHERE id = :id"), {"id": scene_id}
    )
    if scene.first() is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    result = await session.execute(
        text("SELECT * FROM scene_obstacles WHERE scene_id = :id ORDER BY id"),
        {"id": scene_id},
    )
    return [SceneObstacleOut.model_validate(row) for row in result.mappings().all()]


@app.get("/scenes/{scene_id}/best", response_model=BestRunOut)
async def best_run_for_scene(
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> BestRunOut:
    result = await session.execute(
        text("SELECT * FROM v_best_run_per_scene WHERE scene_id = :id"),
        {"id": scene_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="No completed learning run for scene")
    return BestRunOut.model_validate(row)


@app.get("/runs", response_model=list[RunOut])
async def list_runs(
    scene_id: UUID | None = Query(None),
    status: str | None = Query(None),
    campaign_id: UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[RunOut]:
    sql = """
        SELECT sr.id, sr.scene_id, sr.seed, sr.status::text AS status,
               sr.hyperparameters, sr.started_at, sr.finished_at,
               sr.error_message, sr.created_at
        FROM simulation_runs sr
    """
    params: dict[str, object] = {"limit": limit}
    where: list[str] = []
    if campaign_id is not None:
        sql += " JOIN campaign_runs cr ON cr.run_id = sr.id"
        where.append("cr.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if scene_id is not None:
        where.append("sr.scene_id = :scene_id")
        params["scene_id"] = scene_id
    if status is not None:
        where.append("sr.status = CAST(:status AS run_status)")
        params["status"] = status
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sr.created_at DESC LIMIT :limit"
    result = await session.execute(text(sql), params)
    rows = []
    for row in result.mappings().all():
        data = dict(row)
        data["hyperparameters"] = _public_hyperparameters(data.get("hyperparameters"))
        rows.append(RunOut.model_validate(data))
    return rows


_ALLOWED_RUN_STATUS = frozenset({"pending", "running", "completed", "failed", "aborted"})


@app.patch("/runs/{run_id}", response_model=RunOut)
async def patch_run_status(
    run_id: UUID,
    body: RunStatusPatch,
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    if body.status not in _ALLOWED_RUN_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status; allowed: {sorted(_ALLOWED_RUN_STATUS)}",
        )
    existing = await session.execute(
        text("SELECT id, status::text AS status FROM simulation_runs WHERE id = :id"),
        {"id": run_id},
    )
    row = existing.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")

    started_sql = "started_at"
    finished_sql = "finished_at"
    params: dict[str, object] = {
        "id": run_id,
        "status": body.status,
        "error_message": body.error_message,
    }
    # set timestamps on transitions
    set_started = body.status == "running" and row["status"] != "running"
    set_finished = body.status in ("completed", "failed", "aborted")

    result = await session.execute(
        text(
            f"""
            UPDATE simulation_runs
            SET status = CAST(:status AS run_status),
                error_message = COALESCE(:error_message, error_message),
                started_at = CASE WHEN :set_started THEN now() ELSE started_at END,
                finished_at = CASE
                    WHEN :set_started THEN NULL
                    WHEN :set_finished THEN now()
                    ELSE finished_at
                END
            WHERE id = :id
            RETURNING id, scene_id, seed, status::text AS status, hyperparameters,
                      started_at, finished_at, error_message, created_at
            """
        ),
        {
            **params,
            "set_started": set_started,
            "set_finished": set_finished,
        },
    )
    updated = result.mappings().one()
    await session.commit()
    data = dict(updated)
    data["hyperparameters"] = _public_hyperparameters(data.get("hyperparameters"))
    return RunOut.model_validate(data)


@app.post("/runs/{run_id}/timesteps", response_model=list[TimestepOut], status_code=201)
async def create_timesteps_batch(
    run_id: UUID,
    body: TimestepsBatchCreate,
    session: AsyncSession = Depends(get_session),
) -> list[TimestepOut]:
    if not body.timesteps:
        raise HTTPException(status_code=400, detail="timesteps must not be empty")

    run = await session.execute(
        text("SELECT id FROM simulation_runs WHERE id = :id"),
        {"id": run_id},
    )
    if run.first() is None:
        raise HTTPException(status_code=404, detail="Run not found")

    indexes = [t.step_index for t in body.timesteps]
    if len(indexes) != len(set(indexes)):
        raise HTTPException(status_code=400, detail="Duplicate step_index in batch")

    rows_out: list[TimestepOut] = []
    try:
        for step in body.timesteps:
            result = await session.execute(
                text(
                    """
                    INSERT INTO timestep_results (
                        run_id, step_index, sim_time, kinetic_energy,
                        max_velocity, mean_density, dump_uri
                    ) VALUES (
                        :run_id, :step_index, :sim_time, :kinetic_energy,
                        :max_velocity, :mean_density, :dump_uri
                    )
                    RETURNING *
                    """
                ),
                {"run_id": run_id, **step.model_dump()},
            )
            rows_out.append(TimestepOut.model_validate(result.mappings().one()))
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Duplicate step_index for this run",
        ) from exc
    return rows_out


@app.get("/runs/{run_id}/timesteps", response_model=list[TimestepOut])
async def list_timesteps(
    run_id: UUID,
    from_step: int | None = Query(None, alias="from", ge=0),
    to_step: int | None = Query(None, alias="to", ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[TimestepOut]:
    run = await session.execute(
        text("SELECT id FROM simulation_runs WHERE id = :id"),
        {"id": run_id},
    )
    if run.first() is None:
        raise HTTPException(status_code=404, detail="Run not found")

    sql = "SELECT * FROM timestep_results WHERE run_id = :run_id"
    params: dict[str, object] = {"run_id": run_id}
    if from_step is not None:
        sql += " AND step_index >= :from_step"
        params["from_step"] = from_step
    if to_step is not None:
        sql += " AND step_index <= :to_step"
        params["to_step"] = to_step
    sql += " ORDER BY step_index ASC"
    result = await session.execute(text(sql), params)
    return [TimestepOut.model_validate(row) for row in result.mappings().all()]


@app.post("/runs/{run_id}/fork", response_model=RunCreated, status_code=201)
async def fork_run(
    run_id: UUID,
    body: RunForkCreate,
    session: AsyncSession = Depends(get_session),
) -> RunCreated:
    parent = await session.execute(
        text(
            """
            SELECT id, scene_id, seed, hyperparameters
            FROM simulation_runs WHERE id = :id
            """
        ),
        {"id": run_id},
    )
    prow = parent.mappings().first()
    if prow is None:
        raise HTTPException(status_code=404, detail="Parent run not found")

    parent_hyper = _public_hyperparameters(prow["hyperparameters"])
    merged = {**parent_hyper, **body.hyperparameters}
    merged["_parent_run_id"] = str(run_id)
    if body.score_formula_id is not None:
        merged["_score_formula_id"] = str(body.score_formula_id)

    campaign_id = body.campaign_id
    if campaign_id is None:
        # inherit latest campaign membership if any
        camp = await session.execute(
            text(
                """
                SELECT campaign_id FROM campaign_runs
                WHERE run_id = :run_id
                ORDER BY joined_at DESC LIMIT 1
                """
            ),
            {"run_id": run_id},
        )
        crow = camp.first()
        if crow is not None:
            campaign_id = crow[0]

    if campaign_id is not None:
        camp_check = await session.execute(
            text("SELECT id, scene_id FROM experiment_campaigns WHERE id = :id"),
            {"id": campaign_id},
        )
        c = camp_check.mappings().first()
        if c is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if c["scene_id"] != prow["scene_id"]:
            raise HTTPException(
                status_code=400,
                detail="campaign_id scene does not match parent scene",
            )

    seed = body.seed if body.seed is not None else prow["seed"]
    try:
        result = await session.execute(
            text(
                """
                INSERT INTO simulation_runs (scene_id, seed, status, hyperparameters)
                VALUES (
                    :scene_id, :seed, 'pending', CAST(:hyperparameters AS jsonb)
                )
                RETURNING id, status::text AS status
                """
            ),
            {
                "scene_id": prow["scene_id"],
                "seed": seed,
                "hyperparameters": json.dumps(merged),
            },
        )
        row = result.mappings().one()
        if campaign_id is not None:
            await session.execute(
                text(
                    """
                    INSERT INTO campaign_runs (campaign_id, run_id, variant_label)
                    VALUES (:campaign_id, :run_id, :variant_label)
                    """
                ),
                {
                    "campaign_id": campaign_id,
                    "run_id": row["id"],
                    "variant_label": body.variant_label,
                },
            )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Could not fork run") from exc

    return RunCreated(id=row["id"], status=row["status"])


@app.post("/runs/claim", response_model=RunClaimOut | None)
async def claim_next_run(
    session: AsyncSession = Depends(get_session),
) -> RunClaimOut | None:
    """Atomically claim one pending run (FOR UPDATE SKIP LOCKED) and mark it running."""
    result = await session.execute(
        text(
            """
            WITH next_run AS (
                SELECT id
                FROM simulation_runs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE simulation_runs sr
            SET status = 'running',
                started_at = now(),
                finished_at = NULL
            FROM next_run
            WHERE sr.id = next_run.id
            RETURNING sr.id, sr.scene_id, sr.seed, sr.status::text AS status,
                      sr.hyperparameters, sr.started_at, sr.finished_at,
                      sr.error_message, sr.created_at
            """
        )
    )
    row = result.mappings().first()
    await session.commit()
    if row is None:
        return None
    data = dict(row)
    # keep internal stash for worker parent/formula; strip only for public list endpoints
    if isinstance(data.get("hyperparameters"), str):
        data["hyperparameters"] = json.loads(data["hyperparameters"])
    return RunClaimOut.model_validate(data)
