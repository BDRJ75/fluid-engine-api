-- Fluid physics engine — PostgreSQL schema
-- Fluids, scenes, obstacles, timestep results, learning registry

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- Catalog: fluid definitions
-- ---------------------------------------------------------------------------
CREATE TABLE fluids (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    density         double precision NOT NULL CHECK (density > 0),
    viscosity       double precision NOT NULL CHECK (viscosity >= 0),
    surface_tension double precision NOT NULL DEFAULT 0 CHECK (surface_tension >= 0),
    reference_temp_k double precision,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Catalog: wall / obstacle materials
-- ---------------------------------------------------------------------------
CREATE TABLE materials (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    friction        double precision NOT NULL DEFAULT 0 CHECK (friction >= 0),
    elasticity      double precision NOT NULL DEFAULT 0 CHECK (elasticity >= 0 AND elasticity <= 1),
    permeable       boolean NOT NULL DEFAULT false,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Simulation scenes / setups
-- ---------------------------------------------------------------------------
CREATE TABLE scenes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    description     text,
    domain_width    double precision NOT NULL CHECK (domain_width > 0),
    domain_height   double precision NOT NULL CHECK (domain_height > 0),
    domain_depth    double precision CHECK (domain_depth IS NULL OR domain_depth > 0),
    dt              double precision NOT NULL CHECK (dt > 0),
    method          text NOT NULL DEFAULT 'SPH',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE scene_fluids (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scene_id        uuid NOT NULL REFERENCES scenes (id) ON DELETE CASCADE,
    fluid_id        uuid NOT NULL REFERENCES fluids (id),
    initial_volume  double precision CHECK (initial_volume IS NULL OR initial_volume > 0),
    spawn_x         double precision,
    spawn_y         double precision,
    spawn_z         double precision,
    UNIQUE (scene_id, fluid_id)
);

CREATE TABLE scene_obstacles (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scene_id        uuid NOT NULL REFERENCES scenes (id) ON DELETE CASCADE,
    material_id     uuid NOT NULL REFERENCES materials (id),
    kind            text NOT NULL DEFAULT 'wall', -- wall | box | mesh | …
    pose_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
    mesh_uri        text,
    UNIQUE (scene_id, material_id, kind, mesh_uri)
);

-- ---------------------------------------------------------------------------
-- Runs and per-timestep summaries
-- ---------------------------------------------------------------------------
CREATE TYPE run_status AS ENUM ('pending', 'running', 'completed', 'failed', 'aborted');

CREATE TABLE simulation_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scene_id        uuid NOT NULL REFERENCES scenes (id),
    seed            bigint,
    status          run_status NOT NULL DEFAULT 'pending',
    hyperparameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at      timestamptz,
    finished_at     timestamptz,
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX simulation_runs_scene_idx ON simulation_runs (scene_id);
CREATE INDEX simulation_runs_status_idx ON simulation_runs (status);

CREATE TABLE timestep_results (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          uuid NOT NULL REFERENCES simulation_runs (id) ON DELETE CASCADE,
    step_index      integer NOT NULL CHECK (step_index >= 0),
    sim_time        double precision NOT NULL CHECK (sim_time >= 0),
    kinetic_energy  double precision,
    max_velocity    double precision,
    mean_density    double precision,
    dump_uri        text, -- large grids/particles live outside the DB
    UNIQUE (run_id, step_index)
);

CREATE INDEX timestep_results_run_idx ON timestep_results (run_id);

-- ---------------------------------------------------------------------------
-- Learning registry (self-improvement)
-- ---------------------------------------------------------------------------
CREATE TABLE learning_registry (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          uuid NOT NULL UNIQUE REFERENCES simulation_runs (id) ON DELETE CASCADE,
    parent_run_id   uuid REFERENCES simulation_runs (id),

    stable          boolean NOT NULL,
    nan_count       integer NOT NULL DEFAULT 0 CHECK (nan_count >= 0),
    blowup_step     integer CHECK (blowup_step IS NULL OR blowup_step >= 0),

    rmse_density    double precision,
    rmse_velocity   double precision,
    max_err_density double precision,
    max_err_velocity double precision,

    peak_memory_bytes bigint CHECK (peak_memory_bytes IS NULL OR peak_memory_bytes >= 0),

    hyperparameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    score           double precision, -- lower is better (or document convention in app)

    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX learning_registry_score_idx ON learning_registry (score ASC NULLS LAST);
CREATE INDEX learning_registry_stable_idx ON learning_registry (stable);
CREATE INDEX learning_registry_parent_idx ON learning_registry (parent_run_id);

-- ---------------------------------------------------------------------------
-- Optional: target fields on scene for physics agreement metrics
-- ---------------------------------------------------------------------------
ALTER TABLE scenes
    ADD COLUMN target_density double precision,
    ADD COLUMN target_velocity_x double precision,
    ADD COLUMN target_velocity_y double precision,
    ADD COLUMN target_velocity_z double precision;

-- ---------------------------------------------------------------------------
-- Ranking / learning helpers
-- ---------------------------------------------------------------------------
-- Composite index: best stable runs first (score ASC = better)
CREATE INDEX learning_registry_rank_idx
    ON learning_registry (stable DESC, score ASC NULLS LAST, created_at DESC);

CREATE INDEX learning_registry_run_parent_score_idx
    ON learning_registry (parent_run_id, score ASC NULLS LAST)
    WHERE parent_run_id IS NOT NULL;

CREATE INDEX simulation_runs_hyperparams_gin
    ON simulation_runs USING gin (hyperparameters);

CREATE INDEX learning_registry_hyperparams_gin
    ON learning_registry USING gin (hyperparameters);

-- Leaderboard: one row per run, ready for ORDER BY rank_position
CREATE OR REPLACE VIEW v_learning_leaderboard AS
SELECT
    lr.id AS registry_id,
    lr.run_id,
    sr.scene_id,
    s.name AS scene_name,
    lr.stable,
    lr.nan_count,
    lr.blowup_step,
    lr.rmse_density,
    lr.rmse_velocity,
    lr.max_err_density,
    lr.max_err_velocity,
    lr.peak_memory_bytes,
    lr.score,
    lr.parent_run_id,
    lr.hyperparameters,
    sr.seed,
    sr.status,
    lr.created_at,
    RANK() OVER (
        ORDER BY lr.stable DESC, lr.score ASC NULLS LAST, lr.created_at DESC
    ) AS rank_position
FROM learning_registry lr
JOIN simulation_runs sr ON sr.id = lr.run_id
JOIN scenes s ON s.id = sr.scene_id
WHERE sr.status = 'completed';

-- Best run per scene (stable preferred, then lowest score)
CREATE OR REPLACE VIEW v_best_run_per_scene AS
SELECT DISTINCT ON (sr.scene_id)
    sr.scene_id,
    s.name AS scene_name,
    lr.run_id,
    lr.score,
    lr.stable,
    lr.rmse_density,
    lr.rmse_velocity,
    lr.peak_memory_bytes,
    lr.hyperparameters,
    lr.parent_run_id
FROM learning_registry lr
JOIN simulation_runs sr ON sr.id = lr.run_id
JOIN scenes s ON s.id = sr.scene_id
WHERE sr.status = 'completed'
ORDER BY sr.scene_id, lr.stable DESC, lr.score ASC NULLS LAST, lr.created_at DESC;

-- Improvement chain: child vs parent score delta
CREATE OR REPLACE VIEW v_learning_improvements AS
SELECT
    child.run_id AS child_run_id,
    child.parent_run_id,
    child.score AS child_score,
    parent.score AS parent_score,
    (parent.score - child.score) AS score_delta,
    child.stable AS child_stable,
    parent.stable AS parent_stable,
    child.rmse_density AS child_rmse_density,
    parent.rmse_density AS parent_rmse_density,
    child.peak_memory_bytes AS child_peak_memory,
    parent.peak_memory_bytes AS parent_peak_memory,
    child.created_at
FROM learning_registry child
JOIN learning_registry parent ON parent.run_id = child.parent_run_id
WHERE child.parent_run_id IS NOT NULL;
