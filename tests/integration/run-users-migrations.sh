#!/bin/bash
set -euo pipefail

branch_ref="feat/m6-classic-results"
branch_commit="4b183596229ce5ef5d79c9c6b7e782d6d8022e80"
source_url="https://github.com/DanielEspitiaAgu/users_service/archive/$branch_commit.tar.gz"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/users-m6-migration.XXXXXX")"
container_name="users-m6-migration-$(uuidgen | tr -d '-' | tr '[:upper:]' '[:lower:]')"

cleanup() {
  if docker container inspect "$container_name" >/dev/null 2>&1; then
    if ! docker rm -f "$container_name" >/dev/null; then
      echo "Aviso: no se pudo retirar el contenedor PostgreSQL $container_name." >&2
    fi
  fi
  if [ -d "$tmp_dir" ]; then
    if ! rm -rf "$tmp_dir"; then
      echo "Aviso: no se pudo retirar el directorio temporal de migraciones." >&2
    fi
  fi
}
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  echo "Docker y curl son necesarios para las pruebas aisladas." >&2
  exit 1
fi
java_version="$(java -version 2>&1)"
if ! printf '%s\n' "$java_version" | grep -Eq '"21([.]|")'; then
  echo "Se requiere Java 21." >&2
  exit 1
fi

curl --fail --location --silent --show-error "$source_url" --output "$tmp_dir/source.tar.gz"
tar -xzf "$tmp_dir/source.tar.gz" -C "$tmp_dir" --strip-components=1

docker run --rm --detach \
  --name "$container_name" \
  --publish '127.0.0.1::5432' \
  --mount 'type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=1073741824' \
  --env POSTGRES_DB=migration_test \
  --env POSTGRES_USER=postgres \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16.15-alpine >/dev/null

port=''
postgres_ready=0
for attempt in $(seq 1 60); do
  port="$(docker port "$container_name" 5432/tcp 2>/dev/null | awk -F: 'NR == 1 {print $NF}')"
  if [ -n "$port" ] && docker exec "$container_name" pg_isready -U postgres -d migration_test >/dev/null 2>&1; then
    postgres_ready=1
    break
  fi
  sleep 1
done
if [ "$postgres_ready" -ne 1 ]; then
  echo "PostgreSQL efímero no estuvo listo en 60 segundos." >&2
  exit 1
fi

marker="$(uuidgen | tr -d '-' | tr '[:upper:]' '[:lower:]')"
gateway_secret="$(openssl rand -hex 32)"
internal_token="$(openssl rand -hex 32)"
docker exec "$container_name" psql -U postgres -d migration_test \
  -c "COMMENT ON DATABASE migration_test IS '$marker'" >/dev/null

export MIGRATION_TEST_JDBC_URL="jdbc:postgresql://127.0.0.1:$port/migration_test"
export MIGRATION_TEST_DB_USER=postgres
export MIGRATION_TEST_DB_PASSWORD=''
export MIGRATION_TEST_INSTANCE_MARKER="$marker"
export MIGRATION_TEST_GATEWAY_SECRET="$gateway_secret"
export MIGRATION_TEST_INTERNAL_TOKEN="$internal_token"
export DB_URL="$MIGRATION_TEST_JDBC_URL"
export DB_USERNAME=postgres
export DB_PASSWORD=''
export GATEWAY_SECRET="$gateway_secret"
export INTERNAL_API_TOKEN="$internal_token"
export EUREKA_CLIENT_ENABLED=false
export EUREKA_CLIENT_REGISTER_WITH_EUREKA=false
export EUREKA_CLIENT_FETCH_REGISTRY=false
export SERVER_PORT=0

cd "$tmp_dir"
if command -v mvn >/dev/null 2>&1; then
  mvn -B test
elif [ -x ./mvnw ]; then
  ./mvnw -B test
else
  echo "Se requiere Maven o el wrapper incluido en users_service." >&2
  exit 1
fi

printf 'Migraciones y contrato B3 verificados en %s (%s).\n' "$branch_ref" "$branch_commit"
