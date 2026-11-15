#!/usr/bin/env bash
# Local objects only. Restore S3 buckets using the store's own tooling
# (versioning/replication), separately from this database restore.
# Overrides: SCA_POSTGRES_CONTAINER, SCA_POSTGRES_USER, DOCKER_BIN,
# SCA_RESTORE_OBJECTS_DIR (default ./.data/objects).
# Configure the restored application to use that objects directory. Artifact keys
# remain tasks/<task-id>/<name>; stored file:// URIs still describe the old location.
set -euo pipefail
[[ $# == 2 ]] || { echo "Usage: $0 <backup-dir> <target-database-name>" >&2; exit 1; }
backup=$1
database=$2
container=${SCA_POSTGRES_CONTAINER:-sca-postgres-1}
user=${SCA_POSTGRES_USER:-sca}
docker_bin=${DOCKER_BIN:-docker}
objects=${SCA_RESTORE_OBJECTS_DIR:-${SCA_DATA_DIR:-./.data}/objects}
[[ "$database" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]] || {
    echo 'Target database must be a simple SQL identifier (at most 63 characters)' >&2; exit 1;
}
[[ -s "$backup/database.dump" && -d "$backup/objects" && -f "$backup/manifest.txt" ]] || {
    echo 'Incomplete backup' >&2; exit 1;
}
[[ ! -e "$objects" && ! -L "$objects" ]] || {
    echo "Refusing existing objects target: $objects" >&2; exit 1;
}
# createdb itself atomically refuses an existing database, including sca/litellm.
"$docker_bin" exec "$container" createdb --username="$user" -- "$database"
"$docker_bin" exec -i "$container" pg_restore --username="$user" --dbname="$database" \
    --exit-on-error < "$backup/database.dump"
mkdir -p -- "$(dirname -- "$objects")"
cp -a -- "$backup/objects" "$objects"
printf 'Restored database %s and objects to %s from %s\n' "$database" "$objects" "$backup"
