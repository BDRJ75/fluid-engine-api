#!/usr/bin/env bash
# Odtwarza środowisko fluid-engine-api po wyczyszczeniu komputera:
#   PostgreSQL 17 (pakiet + klaster 17/main), rola i baza z .env, schematy v1+v2, .venv.
# Idempotentny: przy ponownym uruchomieniu niczego nie zmienia, jeśli wszystko już jest.
# Użycie: ./scripts/bootstrap_env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
if [[ -z "${SCHEMA_DIR:-}" ]]; then
  if [[ -f "$PROJECT_DIR/db/schema.sql" ]]; then SCHEMA_DIR="$PROJECT_DIR/db"
  else SCHEMA_DIR="$PROJECT_DIR/../fluid-physics-db"; fi
fi
PG_VER=17
CLUSTER=main
SUMMARY=()
log() { echo "[bootstrap] $*"; }
note() { SUMMARY+=("$*"); log "$*"; }

# --- 1. DATABASE_URL -> rola, hasło, host, port, baza ---
DB_URL="${DATABASE_URL:-}"
if [[ -z "$DB_URL" && -f .env ]]; then
  DB_URL="$(grep -E '^DATABASE_URL=' .env | tail -n1 | cut -d= -f2-)"
fi
if [[ -z "$DB_URL" && -f "$SCHEMA_DIR/DATABASE_URL.txt" ]]; then
  DB_URL="$(grep -E '^DATABASE_URL=' "$SCHEMA_DIR/DATABASE_URL.txt" | tail -n1 | cut -d= -f2-)"
fi
[[ -n "$DB_URL" ]] || { echo "Brak DATABASE_URL (.env)"; exit 1; }
re='^[a-z+]+://([^:]+):([^@]+)@([^:/]+):?([0-9]*)/([^?]+)'
[[ "$DB_URL" =~ $re ]] || { echo "Nie umiem sparsować DATABASE_URL"; exit 1; }
DB_USER="${BASH_REMATCH[1]}"; DB_PASS="${BASH_REMATCH[2]}"
DB_HOST="${BASH_REMATCH[3]}"; DB_PORT="${BASH_REMATCH[4]:-5432}"; DB_NAME="${BASH_REMATCH[5]}"
PSQL_URL="postgresql://$DB_USER:$DB_PASS@$DB_HOST:$DB_PORT/$DB_NAME"

# --- 2. Pakiet postgresql-17 ---
if [[ -x /usr/lib/postgresql/$PG_VER/bin/postgres ]]; then
  note "postgresql-$PG_VER: już zainstalowany"
else
  log "Instaluję postgresql-$PG_VER..."
  sudo apt-get update -qq
  if ! apt-cache policy "postgresql-$PG_VER" 2>/dev/null | grep -q 'Candidate: [0-9]'; then
    log "Brak pakietu w repo systemowym, dodaję repozytorium PGDG"
    sudo apt-get install -y -qq curl ca-certificates gnupg lsb-release >/dev/null
    sudo install -d /usr/share/postgresql-common/pgdg
    sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
      | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
    sudo apt-get update -qq
  fi
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "postgresql-$PG_VER" "postgresql-client-$PG_VER" >/dev/null 2>&1
  note "postgresql-$PG_VER: zainstalowany"
fi

# --- 3. Klaster 17/main ---
if pg_lsclusters -h 2>/dev/null | awk '{print $1"/"$2}' | grep -qx "$PG_VER/$CLUSTER"; then
  note "klaster $PG_VER/$CLUSTER: istnieje"
else
  sudo pg_createcluster "$PG_VER" "$CLUSTER" --port="$DB_PORT" >/dev/null
  note "klaster $PG_VER/$CLUSTER: utworzony"
fi
if pg_lsclusters -h | awk -v v="$PG_VER" -v c="$CLUSTER" '$1==v && $2==c {print $4}' | grep -q online; then
  note "klaster $PG_VER/$CLUSTER: już działa"
else
  sudo pg_ctlcluster "$PG_VER" "$CLUSTER" start
  note "klaster $PG_VER/$CLUSTER: uruchomiony"
fi
for i in $(seq 1 30); do
  sudo -u postgres psql -p "$DB_PORT" -qtAc 'select 1' >/dev/null 2>&1 && break; sleep 0.5
done

pg_admin() { sudo -u postgres psql -p "$DB_PORT" -v ON_ERROR_STOP=1 -qtA "$@"; }

# --- 4. Rola i baza ---
if [[ "$(pg_admin -c "select 1 from pg_roles where rolname='$DB_USER'")" == 1 ]]; then
  note "rola $DB_USER: istnieje (bez zmian)"
else
  pg_admin -c "create role \"$DB_USER\" login password '$DB_PASS'"
  note "rola $DB_USER: utworzona"
fi
if [[ "$(pg_admin -c "select 1 from pg_database where datname='$DB_NAME'")" == 1 ]]; then
  note "baza $DB_NAME: istnieje (bez zmian)"
else
  pg_admin -c "create database \"$DB_NAME\" owner \"$DB_USER\""
  note "baza $DB_NAME: utworzona"
fi

# --- 5. Schematy (tylko gdy ich brak) ---
has_table() { [[ "$(PGPASSWORD="$DB_PASS" psql "$PSQL_URL" -qtAc "select to_regclass('public.$1') is not null")" == t ]]; }
if has_table simulation_runs; then
  note "schemat v1: już jest"
else
  PGPASSWORD="$DB_PASS" psql "$PSQL_URL" -q -v ON_ERROR_STOP=1 -f "$SCHEMA_DIR/schema.sql" >/dev/null
  note "schemat v1: wgrany"
fi
if has_table score_formulas; then
  note "schemat v2 (kampanie + score): już jest"
else
  PGPASSWORD="$DB_PASS" psql "$PSQL_URL" -q -v ON_ERROR_STOP=1 -f "$SCHEMA_DIR/schema_v2_campaigns_score.sql" >/dev/null
  note "schemat v2 (kampanie + score): wgrany"
fi

# --- 6. .venv ---
PY_PKGS_CHECK='import fastapi, uvicorn, sqlalchemy, asyncpg, pydantic, pydantic_settings, numpy, httpx, pytest'
if [[ -x .venv/bin/python ]]; then
  note ".venv: istnieje"
else
  python3 -m venv .venv
  note ".venv: utworzony"
fi
if .venv/bin/python -c "$PY_PKGS_CHECK" 2>/dev/null; then
  note "pakiety Pythona: komplet (bez zmian)"
else
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt httpx pytest
  note "pakiety Pythona: zainstalowane"
fi

# --- 7. Sprawdzenie końcowe ---
FORMULAS="$(PGPASSWORD="$DB_PASS" psql "$PSQL_URL" -qtAc "select count(*) from score_formulas")"
RUNS="$(PGPASSWORD="$DB_PASS" psql "$PSQL_URL" -qtAc "select count(*) from simulation_runs")"
echo
echo "================ PODSUMOWANIE ================"
for s in "${SUMMARY[@]}"; do echo " - $s"; done
echo " - połączenie: $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME OK (score_formulas: $FORMULAS, simulation_runs: $RUNS)"
echo "=============================================="
echo "BOOTSTRAP_OK"
