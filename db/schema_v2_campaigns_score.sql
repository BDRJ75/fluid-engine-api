-- Fluid engine — campaigns + score formula versioning
-- Apply after schema.sql

-- ---------------------------------------------------------------------------
-- Score formula versions (weights for composite score)
-- ---------------------------------------------------------------------------
CREATE TABLE score_formulas (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                text NOT NULL,
    version             integer NOT NULL CHECK (version >= 1),
    weight_stability    double precision NOT NULL DEFAULT 1.0 CHECK (weight_stability >= 0),
    weight_rmse_density double precision NOT NULL DEFAULT 1.0 CHECK (weight_rmse_density >= 0),
    weight_rmse_velocity double precision NOT NULL DEFAULT 1.0 CHECK (weight_rmse_velocity >= 0),
    weight_memory       double precision NOT NULL DEFAULT 0.1 CHECK (weight_memory >= 0),
    -- lower score is better; unstable runs get this penalty added
    unstable_penalty    double precision NOT NULL DEFAULT 1e6,
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

-- ---------------------------------------------------------------------------
-- Experiment campaigns (A/B hyperparams, one scene goal)
-- ---------------------------------------------------------------------------
CREATE TABLE experiment_campaigns (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                text NOT NULL,
    description         text,
    scene_id            uuid NOT NULL REFERENCES scenes (id),
    score_formula_id    uuid NOT NULL REFERENCES score_formulas (id),
    goal_notes          text,
    status              text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('draft', 'active', 'completed', 'archived')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX experiment_campaigns_scene_idx ON experiment_campaigns (scene_id);
CREATE INDEX experiment_campaigns_formula_idx ON experiment_campaigns (score_formula_id);
CREATE INDEX experiment_campaigns_status_idx ON experiment_campaigns (status);

CREATE TABLE campaign_runs (
    campaign_id         uuid NOT NULL REFERENCES experiment_campaigns (id) ON DELETE CASCADE,
    run_id              uuid NOT NULL REFERENCES simulation_runs (id) ON DELETE CASCADE,
    variant_label       text,
    joined_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, run_id)
);

CREATE INDEX campaign_runs_run_idx ON campaign_runs (run_id);

-- Link learning entries to a formula (leaderboards must not mix formulas)
ALTER TABLE learning_registry
    ADD COLUMN score_formula_id uuid REFERENCES score_formulas (id);

CREATE INDEX learning_registry_formula_idx
    ON learning_registry (score_formula_id, stable DESC, score ASC NULLS LAST);

-- Optional: default formula per campaign applied when scoring a run
-- (app can copy campaign.score_formula_id into learning_registry.score_formula_id)

-- ---------------------------------------------------------------------------
-- Views for FastAPI: campaigns list + leaderboard filtered by formula
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_campaigns AS
SELECT
    c.id,
    c.name,
    c.description,
    c.scene_id,
    s.name AS scene_name,
    c.score_formula_id,
    sf.name AS score_formula_name,
    sf.version AS score_formula_version,
    c.status,
    c.goal_notes,
    c.created_at,
    c.updated_at,
    (SELECT count(*)::integer FROM campaign_runs cr WHERE cr.campaign_id = c.id) AS run_count
FROM experiment_campaigns c
JOIN scenes s ON s.id = c.scene_id
JOIN score_formulas sf ON sf.id = c.score_formula_id;

CREATE OR REPLACE VIEW v_campaign_leaderboard AS
SELECT
    cr.campaign_id,
    c.name AS campaign_name,
    lr.score_formula_id,
    sf.name AS score_formula_name,
    sf.version AS score_formula_version,
    lr.run_id,
    cr.variant_label,
    lr.stable,
    lr.nan_count,
    lr.blowup_step,
    lr.rmse_density,
    lr.rmse_velocity,
    lr.peak_memory_bytes,
    lr.score,
    lr.parent_run_id,
    lr.hyperparameters,
    sr.seed,
    sr.status AS run_status,
    lr.created_at,
    RANK() OVER (
        PARTITION BY cr.campaign_id, lr.score_formula_id
        ORDER BY lr.stable DESC, lr.score ASC NULLS LAST, lr.created_at DESC
    ) AS rank_position
FROM campaign_runs cr
JOIN experiment_campaigns c ON c.id = cr.campaign_id
JOIN learning_registry lr ON lr.run_id = cr.run_id
JOIN score_formulas sf ON sf.id = lr.score_formula_id
JOIN simulation_runs sr ON sr.id = lr.run_id
WHERE sr.status = 'completed'
  AND lr.score_formula_id IS NOT NULL;

-- Global leaderboard still usable, but partitioned by formula
CREATE OR REPLACE VIEW v_learning_leaderboard_by_formula AS
SELECT
    lr.id AS registry_id,
    lr.run_id,
    lr.score_formula_id,
    sf.name AS score_formula_name,
    sf.version AS score_formula_version,
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
        PARTITION BY lr.score_formula_id
        ORDER BY lr.stable DESC, lr.score ASC NULLS LAST, lr.created_at DESC
    ) AS rank_position
FROM learning_registry lr
JOIN simulation_runs sr ON sr.id = lr.run_id
JOIN scenes s ON s.id = sr.scene_id
LEFT JOIN score_formulas sf ON sf.id = lr.score_formula_id
WHERE sr.status = 'completed';

-- Seed one default formula for bootstrapping
INSERT INTO score_formulas (name, version, weight_stability, weight_rmse_density, weight_rmse_velocity, weight_memory, unstable_penalty, notes)
VALUES ('default', 1, 1.0, 1.0, 1.0, 0.1, 1e6, 'v1: stable first, then RMSE density/velocity, light memory penalty');
