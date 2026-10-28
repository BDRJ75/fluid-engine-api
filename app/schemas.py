from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    scene_id: UUID
    scene_name: str
    score_formula_id: UUID
    score_formula_name: str
    score_formula_version: int
    status: str
    goal_notes: str | None = None
    created_at: datetime
    updated_at: datetime
    run_count: int


class CampaignLeaderboardRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    campaign_id: UUID
    campaign_name: str
    score_formula_id: UUID
    score_formula_name: str
    score_formula_version: int
    run_id: UUID
    variant_label: str | None = None
    stable: bool
    nan_count: int
    blowup_step: int | None = None
    rmse_density: float | None = None
    rmse_velocity: float | None = None
    peak_memory_bytes: int | None = None
    score: float | None = None
    parent_run_id: UUID | None = None
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    run_status: str
    created_at: datetime
    rank_position: int


class FormulaLeaderboardRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    registry_id: UUID
    run_id: UUID
    score_formula_id: UUID | None = None
    score_formula_name: str | None = None
    score_formula_version: int | None = None
    scene_id: UUID
    scene_name: str
    stable: bool
    nan_count: int
    blowup_step: int | None = None
    rmse_density: float | None = None
    rmse_velocity: float | None = None
    max_err_density: float | None = None
    max_err_velocity: float | None = None
    peak_memory_bytes: int | None = None
    score: float | None = None
    parent_run_id: UUID | None = None
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    status: str
    created_at: datetime
    rank_position: int


class RunCreate(BaseModel):
    scene_id: UUID
    campaign_id: UUID | None = None
    seed: int | None = None
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    score_formula_id: UUID | None = None
    parent_run_id: UUID | None = None
    variant_label: str | None = None


class RunCreated(BaseModel):
    id: UUID
    status: str


class LearningCreate(BaseModel):
    stable: bool
    nan_count: int = 0
    blowup_step: int | None = None
    rmse_density: float | None = None
    rmse_velocity: float | None = None
    max_err_density: float | None = None
    max_err_velocity: float | None = None
    peak_memory_bytes: int | None = None
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    score_formula_id: UUID | None = None
    parent_run_id: UUID | None = None
    notes: str | None = None


class LearningCreated(BaseModel):
    registry_id: UUID
    run_id: UUID
    score: float
    score_formula_id: UUID
    parent_run_id: UUID | None = None


class ImprovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    child_run_id: UUID
    parent_run_id: UUID
    child_score: float | None = None
    parent_score: float | None = None
    score_delta: float | None = None
    child_stable: bool
    parent_stable: bool
    child_rmse_density: float | None = None
    parent_rmse_density: float | None = None
    child_peak_memory: int | None = None
    parent_peak_memory: int | None = None
    created_at: datetime


class SceneCreate(BaseModel):
    name: str
    description: str | None = None
    domain_width: float
    domain_height: float
    domain_depth: float | None = None
    dt: float
    method: str = "SPH"
    target_density: float | None = None
    target_velocity_x: float | None = None
    target_velocity_y: float | None = None
    target_velocity_z: float | None = None


class SceneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    domain_width: float
    domain_height: float
    domain_depth: float | None = None
    dt: float
    method: str
    target_density: float | None = None
    target_velocity_x: float | None = None
    target_velocity_y: float | None = None
    target_velocity_z: float | None = None
    created_at: datetime
    updated_at: datetime


class SceneFluidCreate(BaseModel):
    fluid_id: UUID
    initial_volume: float | None = None
    spawn_x: float | None = None
    spawn_y: float | None = None
    spawn_z: float | None = None


class SceneFluidOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scene_id: UUID
    fluid_id: UUID
    initial_volume: float | None = None
    spawn_x: float | None = None
    spawn_y: float | None = None
    spawn_z: float | None = None


class SceneObstacleCreate(BaseModel):
    material_id: UUID
    kind: str = "wall"
    pose_json: dict[str, Any] = Field(default_factory=dict)
    mesh_uri: str | None = None


class SceneObstacleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scene_id: UUID
    material_id: UUID
    kind: str
    pose_json: dict[str, Any]
    mesh_uri: str | None = None


class FluidOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    density: float
    viscosity: float
    surface_tension: float
    reference_temp_k: float | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    friction: float
    elasticity: float
    permeable: bool
    notes: str | None = None
    created_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scene_id: UUID
    seed: int | None = None
    status: str
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime


class BestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scene_id: UUID
    scene_name: str
    run_id: UUID
    score: float | None = None
    stable: bool
    rmse_density: float | None = None
    rmse_velocity: float | None = None
    peak_memory_bytes: int | None = None
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    parent_run_id: UUID | None = None


class RunStatusPatch(BaseModel):
    status: str
    error_message: str | None = None


class TimestepIn(BaseModel):
    step_index: int
    sim_time: float
    kinetic_energy: float | None = None
    max_velocity: float | None = None
    mean_density: float | None = None
    dump_uri: str | None = None


class TimestepsBatchCreate(BaseModel):
    timesteps: list[TimestepIn]


class TimestepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    step_index: int
    sim_time: float
    kinetic_energy: float | None = None
    max_velocity: float | None = None
    mean_density: float | None = None
    dump_uri: str | None = None


class RunForkCreate(BaseModel):
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    campaign_id: UUID | None = None
    score_formula_id: UUID | None = None
    variant_label: str | None = None


class RunClaimOut(BaseModel):
    """Claimed run; hyperparameters may still contain internal _parent stash for the worker."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scene_id: UUID
    seed: int | None = None
    status: str
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime
