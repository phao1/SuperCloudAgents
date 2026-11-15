#!/usr/bin/env bash
# Local objects only. Back up S3 buckets with the store's own tooling
# (versioning/replication), alongside the database dump.
# Stop writers while backing up so database and objects form a consistent copy.
# Overrides: SCA_POSTGRES_CONTAINER, SCA_BACKUP_DATABASE, SCA_POSTGRES_USER,
# SCA_OBJECTS_DIR (or SCA_DATA_DIR/objects), DOCKER_BIN.
set -euo pipefail
[[ $# == 1 ]] || { echo "Usage: $0 <output-dir>" >&2; exit 1; }
container=${SCA_POSTGRES_CONTAINER:-sca-postgres-1}
database=${SCA_BACKUP_DATABASE:-sca}
user=${SCA_POSTGRES_USER:-sca}
objects=${SCA_OBJECTS_DIR:-${SCA_DATA_DIR:-./.data}/objects}
docker_bin=${DOCKER_BIN:-docker}
out=$1
[[ -d "$objects" ]] || { echo "Missing objects directory: $objects" >&2; exit 1; }
# Refuse reuse, including incomplete backups.
mkdir -- "$out"
"$docker_bin" exec "$container" pg_dump --username="$user" --dbname="$database" \
    --format=custom > "$out/database.dump"
cp -a -- "$objects" "$out/objects"
{
    printf 'timestamp_utc=%s\ndatabase=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$database"
    printf 'database_bytes=%s\n' "$(wc -c < "$out/database.dump" | tr -d ' ')"
    printf 'objects_disk_kib=%s\n' "$(du -sk "$out/objects" | awk '{print $1}')"
} > "$out/manifest.txt"
printf 'Backed up database %s and objects from %s to %s\n' "$database" "$objects" "$out"
