#!/usr/bin/env bash
# Run with bash; no production config, images rebuilt, or host services touched.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
docker info --format '{{.ServerVersion}}' >/dev/null
work=$(mktemp -d "${TMPDIR:-/tmp}/sw-p4-gate.XXXXXXXX")
project="sw-p4-gate-${work##*.}"
project=${project,,}
# Ignore ambient Compose, Docker image, and application configuration overrides.
dc() {
    timeout --kill-after=5s "${compose_timeout:-90}s" env -i PATH="$PATH" HOME="$HOME" \
        DOCKER_HOST="${DOCKER_HOST:-}" DOCKER_CONTEXT="${DOCKER_CONTEXT:-}" \
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
        P4_GATE_DB_PASSWORD="$db_password" \
        SEASONALWEATHER_SECRET_DIR="$work" \
        SEASONALWEATHER_CONFIG_FILE="$work/config.yaml" \
        SEASONALWEATHER_ICECAST_CONFIG_FILE="$work/icecast.xml" \
        SEASONALWEATHER_ICECAST_PUBLISHED_PORT=127.0.0.1:0 \
        docker compose --env-file /dev/null -p "$project" --profile icecast \
        -f compose.yaml -f tools/p4_gate.compose.yaml "$@"
}
started=0
cleanup() {
    local result=$?
    trap - EXIT
    if (( started )); then
        if (( result )); then
            dc ps -a >&2 || true
            dc logs --no-color controller | grep -E 'postgresql-preflight|migration_state|SWDB3001' >&2 || true
            dc logs --no-color --tail=40 controller routine-worker icecast liquidsoap postgres >&2 || true
        fi
        if ! dc down --volumes --remove-orphans; then
            printf 'FAIL: cleanup failed for %s; retaining disposable config at %s\n' "$project" "$work" >&2
            exit 1
        fi
    fi
    rm -rf -- "$work"
    if (( result )); then
        printf 'FAIL: %s (exit %s)\n' "$project" "$result" >&2
    else
        printf 'PASS: %s (stack and volumes removed)\n' "$project"
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

db_password=$(openssl rand -hex 24)
umask 022  # dummy bind-mount files must be readable by mapped container users
for secret in ICECAST_SOURCE_PASSWORD SEASONAL_API_TOKEN SEASONAL_WORKER_TOKEN; do
    printf 'p4gate-disposable-%s\n' "$secret" >"$work/$secret"
done
cp tools/p4_gate.icecast.xml "$work/icecast.xml"
# Generate JSON (valid YAML) inside an existing controller image using its PyYAML.
# No repository venv or fixture server is needed.
missing_targets=()
if ! docker image inspect seasonalweather:standard >/dev/null 2>&1; then
    missing_targets+=(controller)
fi
if ! docker image inspect seasonalweather-worker:standard >/dev/null 2>&1; then
    missing_targets+=(routine-worker)
fi
if (( ${#missing_targets[@]} )); then
    printf 'Building missing gate images from this checkout: %s\n' "${missing_targets[*]}"
    python_bin=${PYTHON:-.venv/bin/python}
    "$python_bin" -m seasonalweather.build_metadata --output "$work/build-info.json" --repo-root "$PWD" --profile controller
    for target in "${missing_targets[@]}"; do
        "$python_bin" -m tools.build_interface image --build-info "$work/build-info.json" --target "$target"
    done
fi
timeout 90s docker run --rm -i --network none --entrypoint python seasonalweather:standard \
    -c 'import json, sys, yaml
cfg = yaml.safe_load(sys.stdin)
for section in ("nwws", "cap", "ern", "tests", "zonecounty", "mareas"):
    cfg[section]["enabled"] = False
cfg["now"]["api_backfill"]["enabled"] = False
cfg["api"]["allow_remote"] = True
cfg["station"]["name"] = "Disposable P4 Developer Gate"
cfg["stream"]["icecast_host"] = "icecast"
cfg["tts"]["local"].update(engine="espeak-ng", voice="en-us")
# Keep the legacy top-level compatibility fields aligned with the selected
# local profile for controller-to-worker descriptor coverage.
cfg["tts"].update(voice="en-us", rate_wpm=165)
cfg["network"]["liquidsoap"]["host"] = "liquidsoap"
cfg["network"]["postgresql"].update(enabled=True, address="postgres", database="p4gate", tls=False)
cfg["database"]["postgres"]["schema"] = "p4archive"
json.dump(cfg, sys.stdout)' <config/config.yaml >"$work/config.yaml"

dc config --quiet
started=1
wait_for() {
    local label=$1
    shift
    local deadline=$((SECONDS + 120)) compose_timeout
    printf 'Waiting up to 120 seconds: %s\n' "$label"
    while (( SECONDS < deadline )); do
        compose_timeout=$((deadline - SECONDS))
        if (( compose_timeout > 25 )); then
            compose_timeout=25
        fi
        if "$@"; then
            printf 'OK: %s\n' "$label"
            return 0
        fi
        if (( SECONDS < deadline )); then
            sleep 2
        fi
    done
    printf 'Timed out: %s; collecting diagnostics and tearing down\n' "$label" >&2
    return 1
}
gate_exec() {
    local service=$1 container
    shift
    container=$(dc ps -q "$service") || return
    [[ -n $container ]] || return 1
    timeout --kill-after=5s "${compose_timeout:-25}s" docker exec "$container" "$@" </dev/null
}
pg() {
    gate_exec postgres psql -X -Atq -v ON_ERROR_STOP=1 -U p4gate -d "${2:-p4gate}" -c "$1"
}
pg_online() {
    local container
    container=$(dc ps -q postgres) || return
    [[ -n $container ]] || return 1
    [[ $(timeout --kill-after=2s 5s docker inspect --format '{{.State.Health.Status}}' "$container") == healthy ]]
}
ready() {
    gate_exec controller python -m seasonalweather health controller --mode readiness
}
worker_ready() {
    gate_exec routine-worker python -m seasonalweather health worker --mode readiness
}
stream() {
    local label=$1 frequency=$2 compose_timeout=120
    printf 'Listening up to 120 seconds: %s\n' "$label"
    if ! gate_exec controller python -c "$(<tools/p4_gate_probe.py)" "$frequency"; then
        printf 'Failed: %s\n' "$label" >&2
        return 1
    fi
    printf 'OK: %s\n' "$label"
}
archive_ready() {
    [[ $(pg "SELECT to_regclass('p4archive.alert_lifecycle_versions') IS NOT NULL") == t ]]
}

printf 'Starting PostgreSQL container\n'
dc up -d postgres
wait_for 'PostgreSQL accepting TCP connections' pg_online
printf 'Provisioning PostgreSQL archive schema\n'
pg 'CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public; CREATE SCHEMA p4archive AUTHORIZATION p4gate; ALTER ROLE p4gate IN DATABASE p4gate SET search_path TO p4archive, public;'
[[ $(pg 'SELECT EXISTS (SELECT 1 FROM public.spatial_ref_sys WHERE srid = 4326)') == t ]]
printf 'OK: public PostGIS SRID 4326 and dedicated archive schema provisioned\n'
printf 'Installing disposable secrets with container ownership and mode 0400\n'
dc run --rm --no-deps -T --interactive=false gate-secrets </dev/null
dc up -d controller routine-worker liquidsoap icecast
wait_for 'controller readiness' ready
wait_for 'worker readiness' worker_ready
printf 'Checking eSpeak NG through the worker image synthesis path\n'
gate_exec routine-worker python -c 'from pathlib import Path
from tempfile import TemporaryDirectory
from seasonalweather.tts.tts import TTS
from seasonalweather.worker.handlers import _assigned_local_capability
with TemporaryDirectory() as root:
    engine = TTS(backend="local", local_engine="espeak-ng", voice="en-us",
                 rate_wpm=165, volume=1.0, sample_rate=48000,
                 capability_check=_assigned_local_capability)
    engine.synth_to_wav("Seasonal Weather developer gate.", Path(root) / "probe.wav", purpose="routine")
print("OK: worker image rendered speech")'
station_id_rendered() {
    gate_exec controller python -c 'from pathlib import Path
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.database.segments import SegmentRepository
repo = SegmentRepository(SeasonalDatabase(path="/var/lib/seasonalweather/state/seasonalweather.sqlite3"))
raise SystemExit(not any(
    row["key"] == "id" and not row["is_placeholder"] and row["duration_s"] > 0
    and Path(row["audio_path"]).is_file() for row in repo.load_entries()
))'
}
wait_for 'worker-synthesized station ID committed by controller' station_id_rendered
wait_for 'archive migration' archive_ready
stream 'Icecast stream before outage' 440
controller_epoch() {
    local container
    container=$(dc ps -q controller)
    timeout 10s docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$container"
}
epoch=$(controller_epoch)

postgres_container=$(dc ps -q postgres)
[[ -n $postgres_container ]]
postgres_stopped() {
    local status
    status=$(timeout --kill-after=2s 5s docker inspect --format '{{.State.Status}}' "$postgres_container") || return
    if [[ $status != exited ]]; then
        printf 'Expected PostgreSQL to be stopped; Docker reports %s\n' "$status" >&2
        return 1
    fi
}
dc stop postgres
postgres_stopped
printf 'OK: PostgreSQL is stopped\n'
wait_for 'controller readiness with PostgreSQL stopped' ready
stream 'Icecast stream with PostgreSQL stopped' 550
# Insert the P4-04 empty-snapshot fixture through the real outbox write path.
# Retrieve its UUID inside the same transaction, before concurrent writes can occur.
id=$(gate_exec controller python -c 'from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.database.outbox import append_alert_snapshot
path = "/var/lib/seasonalweather/state/seasonalweather.sqlite3"
with SeasonalDatabase(path=path).transaction() as conn:
    append_alert_snapshot(conn, [])
    snapshot_id = conn.execute("SELECT id FROM archive_outbox ORDER BY sequence DESC LIMIT 1").fetchone()[0]
print(snapshot_id)')
[[ $id =~ ^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$ ]]
printf 'OK: SQLite retained outage snapshot %s\n' "$id"
wait_for 'controller readiness with outage snapshot pending' ready
[[ $(controller_epoch) == "$epoch" ]]
stream 'Icecast stream with outage snapshot pending' 660

dc restart controller
wait_for 'cold controller readiness with PostgreSQL stopped' ready
postgres_stopped
outage_epoch=$(controller_epoch)
[[ $outage_epoch != "$epoch" ]]
wait_for 'worker readiness after cold controller restart' worker_ready
stream 'Icecast stream after cold controller restart with PostgreSQL stopped' 770
gate_exec controller python -c 'import sqlite3, sys
with sqlite3.connect("/var/lib/seasonalweather/state/seasonalweather.sqlite3") as conn:
    sys.exit(conn.execute("SELECT 1 FROM archive_outbox WHERE id = ?", sys.argv[1:]).fetchone() is None)' "$id"
printf 'OK: same SQLite snapshot survived cold controller restart\n'

dc start postgres
wait_for 'PostgreSQL accepting TCP connections after restart' pg_online
snapshot_archived() {
    [[ $(pg "SELECT count(*) FROM p4archive.alert_lifecycle_versions WHERE id = '$id' AND origin = 'local_outbox_replay' AND NOT broadcastable AND payload = '{\"active_alerts\": []}'::jsonb" "${1:-p4gate}") == 1 ]]
}
caught_up() {
    snapshot_archived &&
        gate_exec controller python -c 'import sqlite3, sys
with sqlite3.connect("/var/lib/seasonalweather/state/seasonalweather.sqlite3") as conn:
    sys.exit(conn.execute("SELECT 1 FROM archive_outbox WHERE id = ?", sys.argv[1:]).fetchone() is not None)' "$id"
}
wait_for 'same UUID and payload archived, then acknowledged in SQLite' caught_up
wait_for 'controller readiness after PostgreSQL recovery' ready
wait_for 'worker readiness after PostgreSQL recovery' worker_ready
stream 'Icecast stream after PostgreSQL recovery' 880
[[ $(controller_epoch) == "$outage_epoch" ]]
printf 'OK: controller did not restart during PostgreSQL recovery\n'

migration_history() {
    pg 'SELECT row_to_json(m) FROM p4archive.seasonalweather_schema_metadata m ORDER BY version' "${1:-p4gate}"
}
history=$(migration_history)
[[ -n $history ]]
gate_exec controller python -c 'from seasonalweather.config import load_config
from seasonalweather.database.postgresql_migrations import PostgresMigrationRunner
cfg = load_config("/etc/seasonalweather/config.yaml")
result = PostgresMigrationRunner(configuration=cfg.database.postgres, network=cfg.network.postgresql).apply_pending()
if result.applied_versions or result.current_version < 1:
    raise SystemExit(f"Expected an already migrated archive, got {result}")'
[[ $(migration_history) == "$history" ]]
printf 'OK: repeat safe migration was a no-op on the retained schema\n'

# Exercise recovery's production write path with a deterministic upstream response.
gate_exec controller python -c 'import asyncio
from seasonalweather.config import load_config
from seasonalweather.database.postgresql_archive import ArchiveTable, PostgresArchiveRepository
from seasonalweather.database.recent_backfill import RecoveryResource, recover_recent
async def fetch():
    return {"fixture": "p4gate-forecast"}
cfg = load_config("/etc/seasonalweather/config.yaml")
archive = PostgresArchiveRepository(cfg.database.postgres, cfg.network.postgresql)
asyncio.run(recover_recent(archive, [RecoveryResource("p4gate-forecast", ArchiveTable.FORECAST_SNAPSHOTS, fetch)]))'
backfill_pair() {
    pg "SELECT c.id, a.id FROM p4archive.backfill_checkpoints c
        JOIN p4archive.forecast_snapshots a ON c.payload->>'archive_id' = a.id::text
        WHERE c.payload->>'resource' = 'p4gate-forecast'
          AND a.payload = '{\"fixture\": \"p4gate-forecast\"}'::jsonb
          AND c.origin = 'upstream_gap_recovery' AND a.origin = 'upstream_gap_recovery'
          AND NOT c.broadcastable AND NOT a.broadcastable" "${1:-p4gate}"
}
pair=$(backfill_pair)
[[ $pair =~ ^[a-f0-9-]{36}\|[a-f0-9-]{36}$ ]]
printf 'OK: recovered archive row and checkpoint agree and are non-broadcastable (%s)\n' "$pair"

printf 'Backing up PostgreSQL and restoring into a separate disposable database\n'
compose_timeout=120 gate_exec postgres bash -euc "
    backup=\$(mktemp)
    trap 'rm -f -- \"\$backup\"' EXIT
    pg_dump -U p4gate -d p4gate --format=custom --file=\"\$backup\"
    createdb -U p4gate -T template0 p4gate_restore
    pg_restore -U p4gate -d p4gate_restore --exit-on-error --single-transaction \"\$backup\"
"
[[ $(migration_history p4gate_restore) == "$history" ]]
[[ $(backfill_pair p4gate_restore) == "$pair" ]]
snapshot_archived p4gate_restore
[[ $(pg 'SELECT EXISTS (SELECT 1 FROM public.spatial_ref_sys WHERE srid = 4326)' p4gate_restore) == t ]]
printf 'OK: restore preserved migration history, outbox replay, backfill/checkpoint pair, and PostGIS SRID 4326\n'
